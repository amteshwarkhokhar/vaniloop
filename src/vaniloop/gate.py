"""TEST stage: the ship gate. Decide ship / reject / inconclusive for one prompt patch from replays.

Per target call the old and new prompts are each replayed K times on the same seeds, and the decision
uses an exact one-sided stratified permutation test (exact conditional CMH / stratified Fisher): with
f_i failures among a call's 2K outcomes, the failures on old under H0 are Hypergeometric, so S (total
old failures) gets an exact null distribution by convolving the per-call pmfs, and p_value =
P(S >= S_obs) must land at or below alpha for a ship. The patch was written from these same calls, so
the test has no hold-out unless the caller passes held-out calls, and each attempt is a separate test
at alpha.

Pure functions (min_achievable_p, exact_p_value, decide) hold the logic. run_gate orchestrates: it
raises before any replay when the design can never reach alpha, replays old and new interleaved,
runs the guard and futility checks, and hands the trials to decide. No noise floor: the permutation
test calibrates seed noise exactly.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from fractions import Fraction
from typing import Literal

from pydantic import BaseModel, Field

from .models import AuditRecord, CallRecord, Finding, Ledger
from .replay import ReplayResult

MIN_CALLS = 3
DEFAULT_ALPHA = 0.05
DEFAULT_MAX_NEW_FAIL_RATE = 0.5
Decision = Literal["ship", "reject", "inconclusive"]


class Trial(BaseModel):
    """One replay of one call with one prompt and one seed, after audit."""

    call_id: str
    prompt: Literal["old", "new"]
    seed: int
    failed: bool  # the cause finding is Fatal or NonFatal from the fork on, or the replay was invalid_output
    # a guard: the audit verdict is Fatal. A target: some finding is Fatal with its onset at or after the fork
    fatal: bool
    # a target: the parameters other than the cause with a Fatal finding at or after the fork (side effects)
    side_fatals: list[str] = Field(default_factory=list)
    # evidence is missing: Unknown, absent, NotApplicable or pre-fork cause finding, NeedsReview, invalid_output
    unresolved: bool = False
    outcome: str  # how the replay ended: end_call | hang_up | max_turns | invalid_output


class PerCall(BaseModel):
    """One target call's fail counts, each out of k replays."""

    call_id: str
    old_fails: int
    new_fails: int
    k: int


class GuardCheck(BaseModel):
    """One guard's verdict after run_gate's re-check sequence (seed 0 NEW, then seed 1 NEW and seed 1 OLD)."""

    call_id: str
    unresolved: bool = False  # at least one guard replay lacked conclusive evidence
    fatal_new: bool = False  # the seed-0 NEW replay is Fatal
    fatal_after_recheck: bool | None = None  # the seed-1 NEW replay is Fatal (None = no re-check)
    fatal_old_seed1: bool | None = None  # the seed-1 OLD replay is Fatal (None = not replayed)


class GateResult(BaseModel):
    cause_id: str
    decision: Decision
    attempt: int = 1  # which patch attempt this run is (the caller counts rejected patches' replacements)
    p_value: float  # nan when no test was run: an early abort (futility, pseudo-replication) or thin evidence
    alpha: float
    lift: float  # pooled old fail rate - pooled new fail rate
    per_call: list[PerCall]
    min_achievable_p: float  # the smallest p the exact test can ever produce for this (n, k)
    old_fail_rate: float
    new_fail_rate: float
    guard_breaks: list[str] = Field(default_factory=list)  # guards with a CONFIRMED fatal break (reject)
    guard_flaky: list[str] = Field(default_factory=list)  # guards Fatal under both prompts; not a reject
    n_calls: int  # distinct target calls covered by old and new
    k_seeds: int
    reason: str


def min_achievable_p(n: int, k: int) -> float:
    """The smallest p the exact stratified test can ever produce with n calls and K seeds.

    Best case: every old replay fails and every new replay passes, so each call's hypergeometric null
    puts mass 1/comb(2k, k) on its extreme and the n calls compound to (1 / comb(2k, k))**n. If that
    already exceeds alpha, no outcome can ever ship under the design.
    """
    if n < 0 or k <= 0:
        raise ValueError(f"n must be non-negative and k at least 1, got n={n}, k={k}")
    return (1 / math.comb(2 * k, k)) ** n


def exact_p_value(old_fails: list[int], new_fails: list[int], k: int) -> float:
    """Exact p_value = P(S >= S_obs) of the one-sided stratified permutation test.

    For call i with f_i = old_fails[i] + new_fails[i] failures among its 2K outcomes, the number of
    failures on old under H0 (the patch has no effect) is Hypergeometric(N=2K, K_succ=f_i, draws=K).
    The counts are independent across calls, so S (total old failures) has as exact null distribution
    the convolution of the n per-call pmfs; a dynamic program over the support 0..nK builds it with
    exact fractions. No Monte Carlo, no randomness.
    """
    if len(old_fails) != len(new_fails):
        raise ValueError(f"old_fails and new_fails must have the same length, got {len(old_fails)} vs {len(new_fails)}")
    if k <= 0:
        raise ValueError(f"k must be at least 1, got {k}")
    if any(not 0 <= f <= k for f in [*old_fails, *new_fails]):
        raise ValueError(f"fail counts must lie in 0..k (k={k}), got old={old_fails}, new={new_fails}")
    s_obs = sum(old_fails)
    dp: list[Fraction] = [Fraction(1)]  # dp[s] = P(total old-failure count over the calls so far == s)
    for o, nw in zip(old_fails, new_fails, strict=True):
        f = o + nw
        pmf = {
            j: Fraction(math.comb(f, j) * math.comb(2 * k - f, k - j), math.comb(2 * k, k))
            for j in range(max(0, f - k), min(f, k) + 1)
        }
        nxt: list[Fraction] = [Fraction(0) for _ in range(len(dp) + k)]
        for s, pr in enumerate(dp):
            if pr:
                for j, pj in pmf.items():
                    nxt[s + j] += pr * pj
        dp = nxt
    return float(sum(dp[s_obs:], Fraction(0)))


def failed(audit: AuditRecord, parameter: str) -> bool:
    """True if the finding for the parameter is Fatal or NonFatal. No such finding means not failed."""
    return any(f.parameter == parameter and f.status in ("Fatal", "NonFatal") for f in audit.findings)


def _rate(trials: list[Trial]) -> float:
    return sum(t.failed for t in trials) / len(trials) if trials else 0.0


def _new_side_fatals(old: list[Trial], new: list[Trial]) -> list[tuple[str, str, int, int]]:
    """(call_id, parameter, new_count, old_count) where NEW has more side-effect Fatal replays than OLD."""
    counts: dict[tuple[str, str], list[int]] = {}
    for arm, i in ((old, 0), (new, 1)):
        for t in arm:
            for p in set(t.side_fatals):
                counts.setdefault((t.call_id, p), [0, 0])[i] += 1
    return [(c, p, n, o) for (c, p), (o, n) in sorted(counts.items()) if n > o]


def _per_call(old: list[Trial], new: list[Trial], k: int) -> list[PerCall]:
    counts: dict[str, list[int]] = {}
    for t in old:
        counts.setdefault(t.call_id, [0, 0])[0] += t.failed
    for t in new:
        counts.setdefault(t.call_id, [0, 0])[1] += t.failed
    return [PerCall(call_id=c, old_fails=o, new_fails=n, k=k) for c, (o, n) in sorted(counts.items())]


def decide(
    old: list[Trial],
    new: list[Trial],
    guards: list[GuardCheck] | None = None,
    k: int | None = None,
    cause_id: str = "",
    attempt: int = 1,
    alpha: float = DEFAULT_ALPHA,
    max_new_fail_rate: float = DEFAULT_MAX_NEW_FAIL_RATE,
    min_calls: int = MIN_CALLS,
) -> GateResult:
    """Ship iff the patch beats seed noise; reject on breakage; else inconclusive.

    Ship needs all of: complete arms (old and new cover the same call set with exactly one trial per
    seed 0..K-1 per call), lift > 0, p_value <= alpha, pooled new fail rate <= max_new_fail_rate, no
    unresolved trial in any arm (old seeds 0..K-1, new, guards), a guard's NEW Fatal always backed by
    a recorded re-check result, no side-effect Fatal that NEW adds, no confirmed guard break
    and at least min_calls matched calls. Reject on a confirmed guard break, a side-effect Fatal that NEW
    adds, or lift <= 0. If OLD never fails (0 failures in every old trial), the replay did not
    reproduce the fault, and the result is inconclusive before the lift rule runs. Target trials
    count only findings that start at or after the fork (see _trial). A side-effect Fatal is a Fatal on a
    parameter other than the cause; it adds when, for one call and parameter, NEW has more Fatal replays
    than OLD (the same Fatal under both prompts is background, not the patch). A guard Fatal under BOTH prompts
    is flaky and does not reject. Everything else is inconclusive with its reason.
    """
    k = k if k is not None else max((t.seed for t in [*old, *new]), default=-1) + 1
    old_k = [t for t in old if t.seed < k]
    u_old, u_new = sum(t.unresolved for t in old_k), sum(t.unresolved for t in new)
    checked = guards or []
    u_guards = sum(g.unresolved for g in checked)
    recheck_missing = [
        g.call_id for g in checked if g.fatal_new and (g.fatal_after_recheck is None or g.fatal_old_seed1 is None)
    ]
    u_guards += len(recheck_missing)
    unresolved = u_old + u_new + u_guards
    calls = sorted({t.call_id for t in old_k} | {t.call_id for t in new})
    incomplete: list[str] = []
    for c in calls:
        for arm, label in ((old_k, "old"), (new, "new")):
            for seed in range(k):
                n = sum(1 for t in arm if t.call_id == c and t.seed == seed)
                if n == 0:
                    incomplete.append(f"call '{c}' is missing its {label} seed-{seed} trial")
                elif n > 1:
                    incomplete.append(f"call '{c}' has {n} {label} trials for seed {seed}")
    new_fatals = _new_side_fatals(old_k, new)
    confirmed = [g for g in checked if g.fatal_new and g.fatal_after_recheck]
    breaks = [g.call_id for g in confirmed if g.fatal_old_seed1 is False]
    flaky = [g.call_id for g in confirmed if g.fatal_old_seed1 is True]
    old_rate, new_rate = _rate(old_k), _rate(new)
    lift = old_rate - new_rate
    map_ = min_achievable_p(len(calls), k)
    p = (
        exact_p_value(
            [sum(t.failed for t in old_k if t.call_id == c) for c in calls],
            [sum(t.failed for t in new if t.call_id == c) for c in calls],
            k,
        )
        if calls and not incomplete
        else 1.0
    )
    if unresolved:
        decision = "inconclusive"
        reason = (
            f"{unresolved} trial(s) lack conclusive evidence "
            f"(old={u_old}, new={u_new}, guards={u_guards}; "
            f"Unknown/missing finding, NeedsReview or invalid_output)"
        )
        if recheck_missing:
            reason += f"; guard {', '.join(recheck_missing)} has a NEW Fatal with no complete re-check evidence"
    elif incomplete:
        decision = "inconclusive"
        head = "; ".join(incomplete[:3])
        reason = f"incomplete design: {len(incomplete)} gap(s): {head}" + (
            f"; +{len(incomplete) - 3} more" if len(incomplete) > 3 else ""
        )
    elif len(calls) < min_calls:
        decision = "inconclusive"
        reason = f"only {len(calls)} matched call(s), need at least {min_calls}"
    elif breaks:
        decision = "reject"
        reason = f"patch breaks {len(breaks)} guard call(s): {', '.join(breaks)}"
    elif new_fatals:
        decision = "reject"
        reason = "NEW adds a side-effect Fatal: " + "; ".join(
            f"call '{c}' {p} (NEW {n} vs OLD {o} replays)" for c, p, n, o in new_fatals
        )
    elif old_k and not any(t.failed for t in old_k):
        decision = "inconclusive"
        reason = f"replay did not reproduce the fault (old 0/{len(old_k)})"
    elif lift <= 0:
        decision = "reject"
        reason = f"no improvement: lift {lift:.3f} <= 0"
    elif new_rate > max_new_fail_rate:
        decision = "inconclusive"
        reason = (
            f"new fail rate {new_rate:.3f} exceeds the cap {max_new_fail_rate}: "
            f"the fix is real but too many patched calls still fail"
        )
    elif p <= alpha:
        decision = "ship"
        reason = (
            f"exact permutation p {p:.6g} <= alpha {alpha}, lift {lift:.3f}, "
            f"new fail rate {new_rate:.3f} <= cap {max_new_fail_rate}, no new side-effect Fatal, no guard break"
        )
    else:
        decision = "inconclusive"
        reason = (
            f"exact permutation p {p:.6g} > alpha {alpha} (best achievable for this design is "
            f"{map_:.6g}); the lift {lift:.3f} could still be luck"
        )
    return GateResult(
        cause_id=cause_id,
        decision=decision,
        attempt=attempt,
        p_value=p if unresolved == 0 and not incomplete and len(calls) >= min_calls else float("nan"),
        alpha=alpha,
        lift=lift,
        per_call=_per_call(old_k, new, k),
        min_achievable_p=map_,
        old_fail_rate=old_rate,
        new_fail_rate=new_rate,
        guard_breaks=breaks,
        guard_flaky=flaky,
        n_calls=len(calls),
        k_seeds=k,
        reason=reason,
    )


Item = tuple[CallRecord, Ledger, int]  # call, its ledger, the fork turn (first-fault turn)
ReplayFn = Callable[[CallRecord, Ledger, int, str, int], ReplayResult]  # call, ledger, fork_turn, prompt, seed
AuditFn = Callable[[CallRecord], AuditRecord]


def _after_fork(res: ReplayResult, finding: Finding) -> bool | None:
    """True if the finding starts at or after the fork, False if before it, None if its onset has no turn.

    The fork is a list position, and onset_turn is a Turn.idx, so the idx is mapped to its position first.
    The turns before the fork are copied verbatim into every replay, under both prompts.
    """
    if finding.onset_turn is None:
        return None
    pos = next((i for i, t in enumerate(res.call.turns) if t.idx == finding.onset_turn), None)
    return None if pos is None else pos >= res.fork_turn


def _trial(
    call_id: str,
    which: Literal["old", "new"],
    seed: int,
    res: ReplayResult,
    audit_fn: AuditFn,
    parameter: str,
    target: bool = True,
) -> Trial:
    """Audit one replay. A target judges only findings from the fork on; a guard keeps the whole-call verdict.

    A target's cause finding that is Fatal or NonFatal counts as a failure only if its onset is at or after
    the fork. Before the fork (or with no placeable onset) the fault sits in the copied prefix and fails in
    both arms, and NotApplicable shows the replay never reached the situation, so the trial is unresolved.
    A target's `fatal` is any Fatal finding, on any parameter, with its onset at or after the fork, and
    `side_fatals` lists those parameters except the cause parameter (the permutation test judges that one).
    A Fatal finding on any parameter with an unplaceable onset makes the trial unresolved.
    """
    audit = audit_fn(res.call)
    invalid = res.ended_by == "invalid_output"
    finding = next((f for f in audit.findings if f.parameter == parameter), None)
    unresolved = invalid or audit.verdict == "NeedsReview" or finding is None or finding.status == "Unknown"
    if not target:
        return Trial(
            call_id=call_id,
            prompt=which,
            seed=seed,
            failed=invalid or failed(audit, parameter),
            fatal=audit.verdict == "Fatal",
            unresolved=unresolved,
            outcome=res.ended_by,
        )
    cause_failed = False
    if finding is not None and finding.status in ("Fatal", "NonFatal"):
        cause_failed = _after_fork(res, finding) is True
        unresolved = unresolved or not cause_failed
    unresolved = unresolved or (finding is not None and finding.status == "NotApplicable")
    # A Fatal on any parameter whose onset has no turn cannot be placed before or after the fork: missing evidence.
    unresolved = unresolved or any(f.status == "Fatal" and _after_fork(res, f) is None for f in audit.findings)
    return Trial(
        call_id=call_id,
        prompt=which,
        seed=seed,
        failed=invalid or cause_failed,
        fatal=any(f.status == "Fatal" and _after_fork(res, f) is True for f in audit.findings),
        side_fatals=sorted(
            {
                f.parameter
                for f in audit.findings
                if f.status == "Fatal" and f.parameter != parameter and _after_fork(res, f) is True
            }
        ),
        unresolved=unresolved,
        outcome=res.ended_by,
    )


def _early(
    cause_id: str,
    decision: Decision,
    reason: str,
    k_seeds: int,
    n_calls: int,
    attempt: int,
    old: list[Trial],
    new: list[Trial],
    alpha: float,
) -> GateResult:
    """The GateResult for an abort that stopped before the full replays: no test was run, so p is nan."""
    return GateResult(
        cause_id=cause_id,
        decision=decision,
        attempt=attempt,
        p_value=float("nan"),
        alpha=alpha,
        lift=_rate(old) - _rate(new),
        per_call=_per_call(old, new, k_seeds),
        min_achievable_p=min_achievable_p(n_calls, k_seeds),
        old_fail_rate=_rate(old),
        new_fail_rate=_rate(new),
        n_calls=n_calls,
        k_seeds=k_seeds,
        reason=reason,
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
    attempt: int = 1,
    alpha: float = DEFAULT_ALPHA,
    max_new_fail_rate: float = DEFAULT_MAX_NEW_FAIL_RATE,
) -> GateResult:
    """Replay the target calls and guards, then decide.

    A target call is a call that failed with the old prompt; a guard is a call that passed before. It
    breaks if a replay with the new prompt gets a Fatal verdict. An invalid_output replay counts as a
    failed trial (not fatal), for old and new alike, and as unresolved evidence.

    Design pre-flight: raises ValueError before any replay when min_achievable_p(n, k) > alpha (this
    design can never ship whatever happens), with n = len(calls). Replays then run in interleaved
    order (old seed s, then new seed s, for s in 0..K-1, seed-major across calls). If any seed-0
    replay in either arm lacks conclusive evidence, the gate stops right there with inconclusive: a
    futility reject would otherwise rest on an invisible OLD baseline. After every target call's
    seed-0 pair has run, a futility check stops the gate with a reject if NEW failed
    conclusively on every call at seed 0, so the remaining replays and the guards never run. Each
    guard is replayed once under NEW (seed 0); if that is Fatal it is replayed once more under NEW
    (seed 1) and once under OLD (seed 1), and only
    NEW Fatal again with OLD not Fatal confirms the break and rejects; OLD Fatal too is recorded as
    flaky (Fatal under both prompts), not a reject. If for any call the K old replays produce identical
    generated turn texts after the fork, the result is inconclusive (replays do not vary; K copies are
    one sample), because the test would act on K times the evidence it has.
    """
    if k_seeds < 2:
        raise ValueError(f"k_seeds must be at least 2, got {k_seeds}")
    n_calls = len(calls)
    map_ = min_achievable_p(n_calls, k_seeds)
    if map_ > alpha:
        raise ValueError(
            f"this design can never ship: min_achievable_p({n_calls}, {k_seeds}) = {map_:.6g} "
            f"exceeds alpha {alpha}; add calls or seeds"
        )
    old: list[Trial] = []
    new: list[Trial] = []
    replayed: dict[tuple[str, int, str], ReplayResult] = {}  # (call_id, seed, which) -> result

    def run_one(item: Item, prompt: str, seed: int, which: Literal["old", "new"], target: bool = True) -> Trial:
        call, ledger, fork = item
        res = replay_fn(call, ledger, fork, prompt, seed)
        replayed[(call.call_id, seed, which)] = res
        return _trial(call.call_id, which, seed, res, audit_fn, parameter, target)

    # Seed-0 pairs first, interleave from there: old s then new s for every call and seed.
    for item in calls:
        old.append(run_one(item, old_prompt, 0, "old"))
        new.append(run_one(item, new_prompt, 0, "new"))
    u_old0, u_new0 = sum(t.unresolved for t in old), sum(t.unresolved for t in new)
    if u_old0 or u_new0:
        # Futility must not fire on an invisible baseline: an unresolved seed-0 trial in either arm
        # means the design never ran far enough to judge, so stop as inconclusive before the reject.
        return _early(
            cause_id,
            "inconclusive",
            f"unresolved seed-0 evidence (old={u_old0}, new={u_new0}); futility cannot be confirmed",
            k_seeds,
            n_calls,
            attempt,
            old,
            new,
            alpha,
        )
    if all(t.failed for t in new):
        # Futility: the new prompt failed on every target call at seed 0; stop here, spend nothing more.
        return _early(
            cause_id,
            "reject",
            "futility: new prompt failed on every call at seed 0",
            k_seeds,
            n_calls,
            attempt,
            old,
            new,
            alpha,
        )
    for item in calls:
        for seed in range(1, k_seeds):
            old.append(run_one(item, old_prompt, seed, "old"))
            new.append(run_one(item, new_prompt, seed, "new"))
    checks: list[GuardCheck] = []
    for item in guards:
        t_new0 = run_one(item, new_prompt, 0, "new", target=False)
        check = GuardCheck(
            call_id=item[0].call_id, unresolved=t_new0.unresolved, fatal_new=t_new0.fatal and not t_new0.unresolved
        )
        if check.fatal_new:
            t_new1 = run_one(item, new_prompt, 1, "new", target=False)
            t_old1 = run_one(item, old_prompt, 1, "old", target=False)
            check.fatal_after_recheck = t_new1.fatal and not t_new1.unresolved
            check.fatal_old_seed1 = t_old1.fatal and not t_old1.unresolved
            check.unresolved = check.unresolved or t_new1.unresolved or t_old1.unresolved
        checks.append(check)
    for item in calls:
        fork = item[2]
        texts = {tuple(t.text for t in replayed[(item[0].call_id, s, "old")].call.turns[fork:]) for s in range(k_seeds)}
        if len(texts) < k_seeds:
            return _early(
                cause_id,
                "inconclusive",
                "replays do not vary; K copies are one sample",
                k_seeds,
                n_calls,
                attempt,
                old,
                new,
                alpha,
            )
    return decide(
        old,
        new,
        checks,
        k_seeds,
        cause_id=cause_id,
        attempt=attempt,
        alpha=alpha,
        max_new_fail_rate=max_new_fail_rate,
    )
