"""TEST stage: the ship gate. Decide ship / reject / inconclusive for one prompt patch from replays.

Pure functions (failed, noise_floor, decide) hold the logic. run_gate only orchestrates: it forks each call at its
first-fault turn, replays the old prompt K times (seeds 0..K-1) and 2K times for the noise floor, replays the new
prompt K times, audits every replay, and hands the trials to decide.
The replay and the audit are injected, so tests use stubs and need no network.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, Field

from .models import AuditRecord, CallRecord, Ledger
from .replay import ReplayResult

PERCENTILE = 0.95
MIN_CALLS = 3
Decision = Literal["ship", "reject", "inconclusive"]


class Trial(BaseModel):
    """One replay of one call with one prompt and one seed, after audit."""

    call_id: str
    prompt: Literal["old", "new"]
    seed: int
    failed: bool  # the cause's parameter is Fatal or NonFatal, or the replay was invalid_output
    fatal: bool  # the audit verdict is Fatal (used for guards)
    unresolved: bool = False  # evidence is missing: Unknown or absent finding, NeedsReview verdict, or invalid_output
    outcome: str  # how the replay ended: end_call | hang_up | max_turns | invalid_output


class GateResult(BaseModel):
    cause_id: str
    decision: Decision
    old_fail_rate: float
    new_fail_rate: float
    lift: float  # old_fail_rate - new_fail_rate
    noise_floor: float
    guard_breaks: list[str] = Field(default_factory=list)  # call ids with a fatal guard trial
    n_calls: int  # distinct calls matched between old and new
    k_seeds: int
    reason: str


def failed(audit: AuditRecord, parameter: str) -> bool:
    """True if the finding for the parameter is Fatal or NonFatal. No such finding means not failed."""
    return any(f.parameter == parameter and f.status in ("Fatal", "NonFatal") for f in audit.findings)


def noise_floor(old_by_call: dict[str, list[bool]], k: int, n_perm: int = 200, rng_seed: int = 0) -> float:
    """How high the lift can get from seed noise alone, on the same pooled scale as the lift.

    A permutation estimate of how much two same-size runs of the SAME prompt differ: for each of n_perm
    permutations, every call's 2K old outcomes are randomly split into two halves of K and the pooled
    fail-rate difference (half A minus half B) is computed across all calls. Because that difference is
    pooled the same way as the lift (old fail rate minus new fail rate), the floor is on the same scale
    as the lift. Floor: the 95th percentile (nearest rank) of the n_perm absolute differences. The rng is
    random.Random(rng_seed), so repeated calls give the same floor. Calls with fewer than 2K outcomes are
    skipped; no usable data gives 0.0.
    """
    rng = random.Random(rng_seed)
    diffs: list[float] = []
    outcomes = list(old_by_call.values())
    for _ in range(n_perm):
        half_a: list[bool] = []
        half_b: list[bool] = []
        for values in outcomes:
            if len(values) < 2 * k:
                continue
            shuffled = list(values)
            rng.shuffle(shuffled)
            half_a.extend(shuffled[:k])
            half_b.extend(shuffled[k : 2 * k])
        if half_a:
            rate_a = sum(half_a) / len(half_a)
            rate_b = sum(half_b) / len(half_b)
            diffs.append(abs(rate_a - rate_b))
    if not diffs:
        return 0.0
    diffs.sort()
    return diffs[max(1, math.ceil(PERCENTILE * len(diffs))) - 1]


def _rate(trials: list[Trial]) -> float:
    return sum(t.failed for t in trials) / len(trials) if trials else 0.0


def decide(
    old: list[Trial],
    new: list[Trial],
    guards_new: list[Trial],
    floor: float | None,
    min_calls: int = MIN_CALLS,
    cause_id: str = "",
    k_seeds: int | None = None,
) -> GateResult:
    """ship: lift > floor and no fatal guard. reject: a fatal guard, or lift <= 0. Else inconclusive.

    A fatal guard still rejects first: a break is a break, whatever the evidence. Otherwise, no decision is
    made when the evidence is incomplete: an old (seeds 0..K-1), new or guard trial is unresolved (Unknown or
    missing finding, a NeedsReview verdict, or invalid_output), there is no calibration data for the floor,
    old and new do not cover the same (call_id, seed) pairs for seeds 0..K-1, or fewer than min_calls matched
    distinct calls. An unresolved old trial blocks too: counting it as a failure would inflate the old fail
    rate and so inflate the lift.
    """
    k = k_seeds if k_seeds is not None else max((t.seed for t in old), default=-1) + 1
    old_matches = {(t.call_id, t.seed) for t in old if t.seed < k}
    new_matches = {(t.call_id, t.seed) for t in new if t.seed < k}
    old_rate = _rate([t for t in old if t.seed < k])
    new_rate = _rate(new)
    lift = old_rate - new_rate
    breaks = sorted({t.call_id for t in guards_new if t.fatal})

    def unresolved_count(group: list[Trial]) -> int:
        return sum(1 for t in group if t.unresolved)

    u_old = unresolved_count([t for t in old if t.seed < k])
    u_new = unresolved_count(new)
    u_guards = unresolved_count(guards_new)
    unresolved = u_old + u_new + u_guards
    matched = {call_id for call_id, _seed in old_matches & new_matches}
    if breaks:
        decision, reason = "reject", f"patch breaks {len(breaks)} guard call(s): {', '.join(breaks)}"
    elif unresolved:
        decision, reason = (
            "inconclusive",
            f"{unresolved} trial(s) lack conclusive evidence "
            f"(old={u_old}, new={u_new}, guards={u_guards}; "
            f"Unknown/missing finding, NeedsReview or invalid_output)",
        )
    elif not old:
        decision, reason = "inconclusive", "no calibration data to estimate the noise floor"
    elif old_matches != new_matches:
        decision, reason = (
            "inconclusive",
            f"old and new cover different (call, seed) samples for seeds 0..{k - 1}: "
            f"{len(old_matches - new_matches)} only in old, "
            f"{len(new_matches - old_matches)} only in new",
        )
    elif len(matched) < min_calls:
        decision, reason = "inconclusive", f"only {len(matched)} matched call(s), need at least {min_calls}"
    elif lift <= 0:
        decision, reason = "reject", f"no improvement: lift {lift:.3f} <= 0"
    elif lift > floor:
        decision, reason = "ship", f"lift {lift:.3f} exceeds noise floor {floor:.3f}, no guard broken"
    else:
        decision, reason = "inconclusive", f"lift {lift:.3f} is within noise floor {floor:.3f}"
    return GateResult(
        cause_id=cause_id,
        decision=decision,
        old_fail_rate=old_rate,
        new_fail_rate=new_rate,
        lift=lift,
        noise_floor=floor if floor is not None else 0.0,
        guard_breaks=breaks,
        n_calls=len(matched),
        k_seeds=k,
        reason=reason,
    )


Item = tuple[CallRecord, Ledger, int]  # call, its ledger, the fork turn (first-fault turn)
ReplayFn = Callable[[CallRecord, Ledger, int, str, int], ReplayResult]  # call, ledger, fork_turn, prompt, seed
AuditFn = Callable[[CallRecord], AuditRecord]


def _trial(
    call_id: str, which: Literal["old", "new"], seed: int, res: ReplayResult, audit_fn: AuditFn, parameter: str
) -> Trial:
    audit = audit_fn(res.call)
    invalid = res.ended_by == "invalid_output"
    finding = next((f for f in audit.findings if f.parameter == parameter), None)
    unresolved = invalid or audit.verdict == "NeedsReview" or finding is None or finding.status == "Unknown"
    return Trial(
        call_id=call_id,
        prompt=which,
        seed=seed,
        failed=invalid or failed(audit, parameter),
        fatal=audit.verdict == "Fatal",
        unresolved=unresolved,
        outcome=res.ended_by,
    )


def run_gate(
    cause_id: str,
    parameter: str,
    calls: list[Item],
    guards: list[Item],
    old_prompt: str,
    new_prompt: str,
    k_seeds: int,
    replay_fn: ReplayFn,
    audit_fn: AuditFn,
) -> GateResult:
    """Replay each failing call K times per prompt and each guard K times with the new prompt, then decide.

    A guard is a call that passed before. It breaks if a replay with the new prompt gets a Fatal verdict.
    An invalid_output replay counts as a failed trial (not fatal), for old and new alike, and as unresolved
    evidence. Old is replayed with 2K seeds per call: seeds 0..K-1 pair with the new prompt for the lift,
    the full 0..2K-1 set calibrates the permutation noise floor.
    """
    if k_seeds < 2:
        raise ValueError(f"k_seeds must be at least 2, got {k_seeds}")
    old: list[Trial] = []
    new: list[Trial] = []
    guards_new: list[Trial] = []
    for call, ledger, fork in calls:
        for seed in range(2 * k_seeds):
            res = replay_fn(call, ledger, fork, old_prompt, seed)
            old.append(_trial(call.call_id, "old", seed, res, audit_fn, parameter))
            if seed < k_seeds:
                res = replay_fn(call, ledger, fork, new_prompt, seed)
                new.append(_trial(call.call_id, "new", seed, res, audit_fn, parameter))
    for call, ledger, fork in guards:
        for seed in range(k_seeds):
            res = replay_fn(call, ledger, fork, new_prompt, seed)
            guards_new.append(_trial(call.call_id, "new", seed, res, audit_fn, parameter))
    by_call: dict[str, list[bool]] = {}
    for t in old:
        by_call.setdefault(t.call_id, []).append(t.failed)
    return decide(old, new, guards_new, noise_floor(by_call, k_seeds), cause_id=cause_id, k_seeds=k_seeds)
