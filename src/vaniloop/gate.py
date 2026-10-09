"""TEST stage: the ship gate. Decide ship / reject / inconclusive for one prompt patch from replays.

Pure functions (failed, noise_floor, decide) hold the logic. run_gate only orchestrates: it forks each call at its
first-fault turn, replays the old and new prompt K times, audits every replay, and hands the trials to decide.
The replay and the audit are injected, so tests use stubs and need no network.
"""

from __future__ import annotations

import math
from itertools import combinations
from typing import Callable, Literal

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
    outcome: str  # how the replay ended: end_call | hang_up | max_turns | invalid_output


class GateResult(BaseModel):
    cause_id: str
    decision: Decision
    old_fail_rate: float
    new_fail_rate: float
    lift: float  # old_fail_rate - new_fail_rate
    noise_floor: float
    guard_breaks: list[str] = Field(default_factory=list)  # call ids with a fatal guard trial
    n_calls: int
    k_seeds: int
    reason: str


def failed(audit: AuditRecord, parameter: str) -> bool:
    """True if the finding for the parameter is Fatal or NonFatal. No such finding means not failed."""
    return any(f.parameter == parameter and f.status in ("Fatal", "NonFatal") for f in audit.findings)


def noise_floor(old_trials_by_call: dict[str, list[bool]]) -> float:
    """How much the old prompt disagrees with itself across seeds.

    Per call: the flip rate is the fraction of seed pairs (i < j) whose failed values differ.
    Floor: the 95th percentile of the per-call flip rates, nearest-rank (sorted ascending, rank = ceil(0.95 * n),
    take the value at that rank). Calls with fewer than 2 seeds have no pairs and are skipped. No data gives 0.0.
    """
    rates = []
    for values in old_trials_by_call.values():
        pairs = list(combinations(values, 2))
        if pairs:
            rates.append(sum(a != b for a, b in pairs) / len(pairs))
    if not rates:
        return 0.0
    rates.sort()
    return rates[max(1, math.ceil(PERCENTILE * len(rates))) - 1]


def _rate(trials: list[Trial]) -> float:
    return sum(t.failed for t in trials) / len(trials) if trials else 0.0


def decide(old: list[Trial], new: list[Trial], guards_new: list[Trial], floor: float, min_calls: int = MIN_CALLS,
           cause_id: str = "", k_seeds: int | None = None) -> GateResult:
    """ship: lift > floor and no fatal guard. reject: a fatal guard, or lift <= 0. Else inconclusive.

    Fewer than min_calls distinct calls (old and new together) is inconclusive, except that a fatal guard
    still rejects: a break is a break, whatever the sample size.
    """
    calls = {t.call_id for t in old} | {t.call_id for t in new}
    old_rate, new_rate = _rate(old), _rate(new)
    lift = old_rate - new_rate
    breaks = sorted({t.call_id for t in guards_new if t.fatal})
    k = k_seeds if k_seeds is not None else max((len({t.seed for t in old if t.call_id == c}) for c in calls), default=0)
    if breaks:
        decision, reason = "reject", f"patch breaks {len(breaks)} guard call(s): {', '.join(breaks)}"
    elif len(calls) < min_calls:
        decision, reason = "inconclusive", f"only {len(calls)} call(s), need at least {min_calls}"
    elif lift <= 0:
        decision, reason = "reject", f"no improvement: lift {lift:.3f} <= 0"
    elif lift > floor:
        decision, reason = "ship", f"lift {lift:.3f} exceeds noise floor {floor:.3f}, no guard broken"
    else:
        decision, reason = "inconclusive", f"lift {lift:.3f} is within noise floor {floor:.3f}"
    return GateResult(cause_id=cause_id, decision=decision, old_fail_rate=old_rate, new_fail_rate=new_rate, lift=lift,
                      noise_floor=floor, guard_breaks=breaks, n_calls=len(calls), k_seeds=k, reason=reason)


Item = tuple[CallRecord, Ledger, int]  # call, its ledger, the fork turn (first-fault turn)
ReplayFn = Callable[[CallRecord, Ledger, int, str, int], ReplayResult]  # call, ledger, fork_turn, prompt, seed
AuditFn = Callable[[CallRecord], AuditRecord]


def _trial(call_id: str, which: Literal["old", "new"], seed: int, res: ReplayResult, audit_fn: AuditFn,
           parameter: str) -> Trial:
    audit = audit_fn(res.call)
    invalid = res.ended_by == "invalid_output"
    return Trial(call_id=call_id, prompt=which, seed=seed, failed=invalid or failed(audit, parameter),
                 fatal=audit.verdict == "Fatal", outcome=res.ended_by)


def run_gate(cause_id: str, parameter: str, calls: list[Item], guards: list[Item], old_prompt: str, new_prompt: str,
             k_seeds: int, replay_fn: ReplayFn, audit_fn: AuditFn) -> GateResult:
    """Replay each failing call K times per prompt and each guard K times with the new prompt, then decide.

    A guard is a call that passed before. It breaks if a replay with the new prompt gets a Fatal verdict.
    An invalid_output replay counts as a failed trial (not fatal), for old and new alike.
    """
    old: list[Trial] = []
    new: list[Trial] = []
    guards_new: list[Trial] = []
    for call, ledger, fork in calls:
        for seed in range(k_seeds):
            old.append(_trial(call.call_id, "old", seed, replay_fn(call, ledger, fork, old_prompt, seed), audit_fn, parameter))
            new.append(_trial(call.call_id, "new", seed, replay_fn(call, ledger, fork, new_prompt, seed), audit_fn, parameter))
    for call, ledger, fork in guards:
        for seed in range(k_seeds):
            guards_new.append(_trial(call.call_id, "new", seed, replay_fn(call, ledger, fork, new_prompt, seed), audit_fn, parameter))
    by_call: dict[str, list[bool]] = {}
    for t in old:
        by_call.setdefault(t.call_id, []).append(t.failed)
    return decide(old, new, guards_new, noise_floor(by_call), cause_id=cause_id, k_seeds=k_seeds)
