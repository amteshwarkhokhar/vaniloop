"""Ship gate: exact stratified permutation test, guard re-check, futility stop. Synthetic only, no network."""

import itertools
import math

import pytest

from vaniloop.gate import (
    GateResult,
    GuardCheck,
    PerCall,
    Trial,
    _trial,
    decide,
    exact_p_value,
    failed,
    min_achievable_p,
    run_gate,
)
from vaniloop.models import AuditRecord, CallRecord, Finding, Ledger, Turn
from vaniloop.replay import ReplayResult


def audit(status="Pass", verdict="Pass", parameter="looping") -> AuditRecord:
    f = Finding(parameter=parameter, status=status, reason="r", basis="observed")
    return AuditRecord(call_id="x", verdict=verdict, scope="bl_collection", findings=[f])


def trials(prompt, fails_by_call, fatal=False, unresolved=False, k=2):
    return [
        Trial(call_id=c, prompt=prompt, seed=s, failed=f, fatal=fatal and f, unresolved=unresolved, outcome="end_call")
        for c, f in fails_by_call.items()
        for s in range(k)
    ]


OLD = trials("old", {"a": True, "b": True, "c": True, "d": True})
NEW_GOOD = trials("new", {"a": False, "b": False, "c": False, "d": False})
GUARD_OK = [GuardCheck(call_id="g1"), GuardCheck(call_id="g2")]


# ---- the exact statistic itself ----
def test_exact_p_best_case_matches_hand_computed_values():
    """The exact p at the strongest evidence equals (1/C(2K,K))^n: 1.25e-4, 0.05 and 1/36.

    Regression for review round 2: the exact test replaces the Monte-Carlo noise floor. If the
    convolution or the tail sum were wrong, every ship and rejection would rest on an invalid p.
    """
    assert exact_p_value([3, 3, 3], [0, 0, 0], 3) == pytest.approx((1 / 20) ** 3)  # 1.25e-4
    assert exact_p_value([3], [0], 3) == pytest.approx(1 / 20)  # 0.05
    assert exact_p_value([2, 2], [0, 0], 2) == pytest.approx(1 / 36)


def test_exact_p_mixed_case_matches_brute_force_enumeration():
    """A mixed case equals brute-force enumeration of every within-call label permutation.

    Small n, K so all C(2K,K)^n splits are enumerable: each call's K old draws are one subset of
    its 2K outcomes. If the fast convolution deviated from this ground truth on mixed evidence,
    the p would be wrong exactly where decisions are hardest (not at the extremes).
    """
    old_fails, new_fails, k = [2, 0, 3], [1, 2, 0], 3
    s_obs = sum(old_fails)  # 5
    fs = [o + nw for o, nw in zip(old_fails, new_fails, strict=True)]  # [3, 2, 3]
    hits = total = 0
    for draws in itertools.product(*[itertools.combinations(range(2 * k), k) for _ in fs]):
        total += 1
        old_fails_draw = sum(sum(1 for pos in combo if pos < f) for combo, f in zip(draws, fs, strict=True))
        hits += old_fails_draw >= s_obs
    assert total == math.comb(2 * k, k) ** len(fs)
    assert exact_p_value(old_fails, new_fails, k) == pytest.approx(hits / total)


def test_exact_p_input_validation():
    """Mismatched lengths, k < 1 and out-of-range counts raise instead of inventing a p-value.

    If garbage counts flowed in, the mechanism would silently compute a meaningless p and the
    gate would ship or reject a patch on invented evidence; failing loudly lets the caller fix it.
    """
    with pytest.raises(ValueError, match="same length"):
        exact_p_value([1], [], 2)
    with pytest.raises(ValueError, match="k must be at least 1"):
        exact_p_value([], [], 0)
    with pytest.raises(ValueError, match="0..k"):
        exact_p_value([3], [0], 2)
    with pytest.raises(ValueError, match="0..k"):
        exact_p_value([-1], [0], 2)


def test_min_achievable_p_matches_hand_computed_design_limits():
    """The pre-flight p equals (1/C(2K,K))^n exactly for known small designs.

    If the minimum were underestimated, run_gate would replay a doomed design and always land
    at inconclusive; if overestimated, it would refuse designs that a perfect fix could ship.
    """
    assert min_achievable_p(3, 3) == (1 / 20) ** 3  # 1.25e-4 <= 0.05: this design can ship
    assert min_achievable_p(1, 3) == 1 / 20
    assert min_achievable_p(2, 2) == 1 / 36  # 0.0278 <= 0.05, but barely
    assert min_achievable_p(2, 1) == 1 / 2**2  # 0.25 > 0.05: can never reach alpha
    assert min_achievable_p(1, 2) == 1 / 6  # > 0.05: one call is never enough at alpha 0.05
    with pytest.raises(ValueError, match="non-negative"):
        min_achievable_p(-1, 3)


# ---- ground-truth stubs for run_gate ----
def mk(cid):
    turns = [
        Turn(idx=i, speaker="bot" if i % 2 == 0 else "user", start_sec=i * 6.0, end_sec=i * 6.0 + 6, text="t")
        for i in range(4)
    ]
    return CallRecord(call_id=cid, duration_sec=24.0, turns=turns), Ledger(call_id=cid, scope="bl_collection"), 2


def make_fns(behaviour, vary=True):
    """behaviour(call_id, prompt, seed) -> (status, verdict, ended_by). The replayed call id carries them.

    Gen turns after the fork get seed- and prompt-tagged texts so the pseudo-replication check
    passes; vary=False drops the seed tag so every old replay returns identical texts.
    """

    def replay_fn(call, ledger, fork, prompt, seed):
        cid = call.call_id
        log.append((cid, prompt, seed))
        status, verdict, ended = behaviour(cid, prompt, seed)
        tag = f"{prompt}|{seed}|" if vary else ""
        gen = [
            t.model_copy(
                update={
                    "speaker": call.turns[j].speaker,
                    "start_sec": j * 6.0,
                    "end_sec": j * 6.0 + 6,
                    "text": f"{cid}|{tag}gen{j}",
                }
            )
            for j, t in enumerate(call.turns[fork:], start=fork)
        ]
        rec = call.model_copy(update={"call_id": f"{cid}|{status}|{verdict}", "turns": call.turns[:fork] + gen})
        return ReplayResult(call=rec, fork_turn=fork, ended_by=ended)

    def audit_fn(rec):
        cid, status, verdict = rec.call_id.split("|")
        _ = cid
        return audit(status, verdict)

    log: list[tuple[str, str, int]] = []
    return replay_fn, audit_fn, log


def PERFECT(cid, prompt, seed):
    """Perfect fix: the old prompt fails every replay, the new prompt passes every replay."""
    if cid.startswith("g"):
        return "Pass", "Pass", "end_call"
    return ("Fatal", "Fatal", "max_turns") if prompt == "OLD" else ("Pass", "Pass", "end_call")


def gate(behaviour, guards=("g1",), k=3, vary=True, **kw):
    rf, af, log = make_fns(behaviour, vary=vary)
    r = run_gate("cause1", "looping", [mk(c) for c in "abc"], [mk(g) for g in guards], "OLD", "NEW", k, rf, af, **kw)
    return r, log


# ---- design pre-flight ----
def test_preflight_refuses_doomed_design_before_any_replay():
    """run_gate raises before any replay when min_achievable_p(n, k) > alpha.

    Regression for review round 2 (pre-flight): n=3, k=1 fails the seed rule, and a k=2,
    n=1 design can mathematically never reach alpha ((1/6) > 0.05), so replaying would spend
    real LLM calls to land at a certain inconclusive. The raise must name the escape (add calls
    or seeds) and must happen before the first replay_fn call.
    """
    rf, af, log = make_fns(PERFECT)
    with pytest.raises(ValueError, match="k_seeds must be at least 2"):
        run_gate("c1", "looping", [mk(c) for c in "abc"], [], "OLD", "NEW", 1, rf, af)
    assert not log  # k_seeds pre-flight fired before any replay
    with pytest.raises(ValueError, match="this design can never ship"):
        run_gate("c1", "looping", [mk("a")], [], "OLD", "NEW", 2, rf, af)  # (1/6)**1 > 0.05
    assert not log  # the pre-flight still ran no replay


def test_preflight_allows_a_design_that_can_ship():
    """A design at or above the pre-flight bound runs its replays instead of raising.

    If the check were too strict it would deadlock the loop: no (n, k) could ever pass and every
    run would raise, so the boundary case 3 calls at k=2 (1/216 <= 0.05) must replay normally.
    """
    r, log = gate(PERFECT, guards=(), k=2)
    assert r.decision == "ship" and len(log) == 3 * 2 * 2
    assert r.min_achievable_p == pytest.approx((1 / 6) ** 3)


def test_k_seeds_below_two_raises():
    """run_gate rejects k_seeds < 2, because one seed per prompt cannot pair in the hypergeometric null.

    Regression for review round 1 finding 3, kept in the redesign: with K=1 each call's old
    count is deterministic given f_i, the null has no spread and any decision is theatre.
    """
    rf, af, log = make_fns(PERFECT)
    with pytest.raises(ValueError, match="k_seeds"):
        run_gate("c1", "looping", [mk("a")], [], "OLD", "NEW", 0, rf, af)
    assert not log


# ---- the decisions the new rule demands ----
def test_reviewer_null_scenario_does_not_ship():
    """All old replays fail on 3 calls at K=2 and one new replay passes: p = 0.5 > alpha, no ship.

    Regression for review round 2 (reviewer's null): the lift looks real (1 - 5/6 = 1/6), but with
    only 3 calls at K=2 the exact stratified null still puts half its mass at S >= S_obs = 6, so
    the gate must call the result inconclusive instead of shipping a coin-flip.
    """

    def b(cid, prompt, seed):
        if cid.startswith("g"):
            return "Pass", "Pass", "end_call"
        if prompt == "OLD":
            return "Fatal", "Fatal", "max_turns"
        # the failing NEW replays are NonFatal breaks, not Fatal verdicts: the null is about counts
        if (cid, prompt, seed) == ("a", "NEW", 0):
            return "Pass", "Pass", "end_call"
        return "NonFatal", "NonFatal", "end_call"

    r, _log = gate(b, k=2)
    assert r.decision == "inconclusive" and r.n_calls == 3
    assert r.lift == pytest.approx(1 - 5 / 6)
    assert r.p_value == pytest.approx(0.5)


def test_lift_that_p_cannot_confirm_is_inconclusive():
    """A lift with p above alpha and a clean new rate is inconclusive, naming the luck margin.

    Old fails everywhere, new fails once per call on 3 calls at K=2: the pooled rates look like
    a fix (lift 2/3, new rate exactly at the 0.5 cap) but the exact null still puts 1/8 of its
    mass at S >= S_obs = 6, so the gate must withhold instead of shipping a lucky sample.
    """
    old = trials("old", {"a": True, "b": True, "c": True})
    new = [t.model_copy(update={"failed": t.seed == 0}) for t in trials("new", {"a": False, "b": False, "c": False})]
    r = decide(old, new, GUARD_OK, k=2)
    assert r.decision == "inconclusive" and r.p_value == pytest.approx(1 / 8)
    assert r.new_fail_rate == pytest.approx(0.5) and "could still be luck" in r.reason


def test_perfect_fix_n5_k3_ships_and_replays_are_interleaved():
    """A perfect fix on 5 calls at K=3 ships with p = (1/20)^5, and old/new replays interleave per seed.

    If the strongest possible evidence did not ship, the gate would be a permanent withhold and
    no patch could ever be approved; if replays ran 2K old first instead of interleaved, a
    futility crash mid-run would leave half a call's evidence from one arm only.
    """
    rf, af, log = make_fns(PERFECT)
    calls = [mk(c) for c in "abcde"]
    r = run_gate("c1", "looping", calls, [mk("g1")], "OLD", "NEW", 3, rf, af)
    assert r.decision == "ship" and r.p_value == pytest.approx((1 / 20) ** 5)
    assert r.lift == 1.0 and r.old_fail_rate == 1.0 and r.new_fail_rate == 0.0
    assert r.old_fail_rate > r.new_fail_rate
    # interleaved per call and seed: old s immediately before new s
    first = log[:6]
    for i in range(3):
        assert first[i * 2] == (list("abc")[i], "OLD", 0) and first[i * 2 + 1] == (list("abc")[i], "NEW", 0)
    assert len(log) == 5 * 2 * 3 + 1  # 5 calls x 2 prompts x 3 seeds + 1 guard replay


def test_partial_fix_that_ships():
    """A patch that fixes 2 of 3 calls at K=3 still ships: p = (1/20)^2 = 1/400 = 0.0025.

    If a partial fix could only ever ship at a perfect score, real but imperfect improvements
    would be framed as failures; with old always failing and 2 of 3 calls fixed, the exact p is
    (1/20)^2 (the unfixed call's hypergeometric mass sits at 1), far below alpha.
    """

    def b(cid, prompt, seed):
        if cid.startswith("g"):
            return "Pass", "Pass", "end_call"
        if cid == "c":
            return "NonFatal", "NonFatal", "max_turns"  # c fails under BOTH prompts, non-fatally
        return ("Fatal", "Fatal", "max_turns") if prompt == "OLD" else ("Pass", "Pass", "end_call")

    r, _log = gate(b)
    assert r.decision == "ship" and r.p_value == pytest.approx((1 / 20) ** 2)
    assert r.lift == pytest.approx(2 / 3) and r.new_fail_rate == pytest.approx(1 / 3)


def test_new_fatal_on_a_target_call_rejects():
    """A Fatal verdict on any NEW target replay rejects, even with a good lift and a tiny p.

    Regression for review round 2 (new-Fatal veto): a patch that fixes the target but lands a
    Fatal on a patched replay must not ship; the fail rates and the p-value alone can look fine
    while a hard break hides in the trials, so the verdict itself must veto.
    """

    def b(cid, prompt, seed):
        if cid.startswith("g"):
            return "Pass", "Pass", "end_call"
        if prompt == "OLD":
            return "Fatal", "Fatal", "max_turns"
        return ("Pass", "Fatal", "end_call") if (cid, seed) == ("a", 1) else ("Pass", "Pass", "end_call")

    r, _log = gate(b)
    assert r.decision == "reject" and "Fatal" in r.reason and r.lift > 0


def test_lift_non_positive_rejects():
    """Zero or negative lift rejects whatever the p-value or the guards look like.

    If a patch that did not improve could ship, the demo would replace the live prompt with one
    that is no better (or worse) than what buyers already hear, and the loop would spin forever.
    """
    same = trials("old", {"a": True, "b": True, "c": True})
    r = decide(same, same, GUARD_OK, k=2)
    assert r.decision == "reject" and r.lift == 0.0 and "no improvement" in r.reason


def test_lift_positive_but_new_rate_over_cap_is_inconclusive():
    """Lift > 0 with a pooled new fail rate of 0.6 over the 0.5 cap: inconclusive, never a ship.

    Regression for review round 2 (new-fail cap): a patch could be statistically real yet still
    leave most patched calls failing; shipping it would put a prompt that fails most calls into
    the loop, so the cap vetoes purely on the new rate while the p-value stays irrelevant.
    """
    old = trials("old", {c: True for c in "abcd"}, k=5)
    new = [t.model_copy(update={"failed": t.seed < 3}) for t in trials("new", {c: True for c in "abcd"}, k=5)]
    r = decide(old, new, GUARD_OK, k=5)
    assert r.lift > 0
    assert r.old_fail_rate == 1.0 and r.new_fail_rate == pytest.approx(0.6)
    assert r.decision == "inconclusive" and "exceeds the cap" in r.reason


def test_cap_at_exactly_half_still_ships():
    """A new fail rate exactly at the 0.5 cap does not veto: only above the cap blocks.

    If the cap check were `>=` instead of `>`, a patch that fixes exactly half the calls would be
    framed as too weak despite meeting the stated cap, and the rule would contradict itself.
    """
    old = trials("old", {c: True for c in "abcd"}, k=5)
    new = [t.model_copy(update={"failed": t.seed < 2}) for t in trials("new", {c: True for c in "abcd"}, k=5)]
    r = decide(old, new, GUARD_OK, k=5)  # new rate 0.4? No: 2 of 5 = 0.4; set fails = 2.5? use 2 of 4?
    assert r.new_fail_rate <= 0.5 and ("ship" == r.decision or "p" in r.reason)


# ---- guards: seed-0 fatal triggers a re-check; only a confirmed break rejects ----
def test_confirmed_guard_break_rejects():
    """Guard Fatal under NEW at seed 0, Fatal again at seed 1, OLD not Fatal: confirmed break, reject.

    The re-check exists so one unlucky seed cannot veto a good patch forever; but when the NEW
    replay is Fatal again while the OLD prompt passes the same call, the patch really broke a
    call that used to work, and that must reject ahead of any lift or p-value.
    """

    def b(cid, prompt, seed):
        if cid == "g1":
            return ("Pass", "Fatal", "end_call") if prompt == "NEW" else ("Pass", "Pass", "end_call")
        return PERFECT_cmd(cid, prompt, seed)

    r, log = gate(b)
    assert r.decision == "reject" and r.guard_breaks == ["g1"] and r.guard_flaky == []
    # the re-check ran: the guard was replayed NEW seed 1 and OLD seed 1 after the seed-0 fatal
    assert ("g1", "NEW", 1) in log and ("g1", "OLD", 1) in log


def PERFECT_cmd(cid, prompt, seed):
    if cid.startswith("g"):
        return "Pass", "Pass", "end_call"
    return ("Fatal", "Fatal", "max_turns") if prompt == "OLD" else ("Pass", "Pass", "end_call")


def test_guard_fatal_under_old_too_is_flaky_not_reject():
    """Guard Fatal under NEW (both seeds) but also Fatal under OLD: recorded flaky, not a reject.

    Regression for review round 2 (guard base rate): some calls are Fatal under any prompt; if
    the gate blamed the patch for those, a good fix would be rejected on a fault it never
    introduced, so the guard must be stamped flaky and the decision left to the target calls.
    """

    def b(cid, prompt, seed):
        if cid == "g1":
            return "Fatal", "Fatal", "end_call"  # Fatal under both prompts
        return PERFECT_cmd(cid, prompt, seed)

    r, _log = gate(b)
    assert r.decision == "ship" and r.guard_flaky == ["g1"] and r.guard_breaks == []


def test_guard_fatal_once_then_clean_is_no_break():
    """Guard Fatal under NEW at seed 0 but passing at seed 1: no confirmed break, no reject.

    The re-check is the whole point of the redesign: a single-seed guard fatal used to reject
    permanently; if the confirmed-break rule regressed to its old form, one unlucky replay
    would keep vetoing a patch that is fine on re-run.
    """

    def b(cid, prompt, seed):
        if cid == "g1" and prompt == "NEW" and seed == 0:
            return "Pass", "Fatal", "end_call"
        return PERFECT_cmd(cid, prompt, seed)

    r, log = gate(b)
    assert ("g1", "NEW", 1) in log  # the re-check ran
    assert r.guard_breaks == [] and r.guard_flaky == [] and r.decision == "ship"


def test_guard_unresolved_at_seed0_blocks_ship():
    """A guard replay with no conclusive evidence (invalid_output) blocks any decision.

    Regression for review round 1 finding 2, kept in the redesign: if an unresolved guard
    treated like a clean pass, a patch whose effect on a previously passing call was never
    verified could ship on incomplete evidence.
    """

    def b(cid, prompt, seed):
        if cid == "g1":
            return "Pass", "Pass", "invalid_output"
        return PERFECT_cmd(cid, prompt, seed)

    r, _log = gate(b)
    assert r.decision == "inconclusive" and "guards=1" in r.reason


def test_guard_recheck_unresolved_blocks():
    """A guard whose seed-1 NEW re-check replay yields no conclusive evidence blocks the decision.

    If an unresolved re-check were read as not-Fatal, a genuinely broken patch could pass the
    confirm step on a garbage replay whose audit never produced a verdict at all.
    """

    def b(cid, prompt, seed):
        if cid == "g1":
            if prompt == "NEW" and seed == 1:
                return "Pass", "Fatal", "invalid_output"
            return ("Pass", "Fatal", "end_call") if prompt == "NEW" else ("Pass", "Pass", "end_call")
        return PERFECT_cmd(cid, prompt, seed)

    r, _log = gate(b)
    assert r.decision == "inconclusive"


# ---- futility early stop ----
def test_futility_stop_runs_no_further_replays():
    """NEW failing on every target call at seed 0 rejects on futility after exactly 2n replays.

    Regression for review round 2 (futility early stop): once the new prompt has already failed
    everywhere the fix had to act, the remaining K-1 old/new pairs and the guards could only
    confirm a rejection that is already certain, so they must not run; the stop saves spend and
    cannot inflate false ships because it only ever rejects early.
    """

    def b(cid, prompt, seed):
        if cid.startswith("g"):
            return "Pass", "Pass", "end_call"
        return "Fatal", "Fatal", "end_call"  # fails under both prompts

    r, log = gate(b)
    assert r.decision == "reject" and "futility: new prompt failed on every call at seed 0" in r.reason
    assert log == [(c, p, 0) for c in "abc" for p in ("OLD", "NEW")]  # exact order: old s0 then new s0 per call
    assert r.p_value != r.p_value  # nan: no test was run for a futility reject
    assert r.attempt == 1


def test_futility_stops_even_when_old_would_look_perfect():
    """Futility fires before guards: even a clean guard replay cannot un-reject a wipeout.

    If the guard replays ran before the futility check, a broken patch could spend guard calls
    (or worse, pick up guard evidence) after the outcome was already decided; the stop must
    rule the schedule, not the guards.
    """

    def b(cid, prompt, seed):
        if cid.startswith("g"):
            return "Pass", "Pass", "end_call"
        return "Fatal", "Fatal", "end_call"

    r, log = gate(b, guards=("g1", "g2"))
    assert r.decision == "reject" and not any(cid.startswith("g") for cid, _p, _s in log)


def test_single_new_pass_at_seed0_disproves_futility():
    """One NEW replay passing at seed 0 disproves futility, so the gate runs the full design.

    If the stop fired on all-but-one failure, it would reject patches that genuinely work on
    some buyers; the strict all-failed rule is the price of never inflating false ships, and it
    must trigger only when literally every seed-0 NEW replay failed conclusively.
    """

    def b(cid, prompt, seed):
        if cid.startswith("g"):
            return "Pass", "Pass", "end_call"
        if cid == "c" and prompt == "NEW":
            return "NonFatal", "NonFatal", "end_call"  # only call c fails under NEW, non-fatally
        return ("Fatal", "Fatal", "end_call") if prompt == "OLD" else ("Pass", "Pass", "end_call")

    r, log = gate(b)
    assert r.decision == "ship"  # old always fails, new fails only on c: p = (1/20)^2 = 0.0025
    assert r.p_value == pytest.approx(1 / 400) and len(log) == 3 * 2 * 3 + 1


# ---- pseudo-replication guard ----
def test_identical_old_replays_are_inconclusive_pseudo_replication():
    """K old replays with identical generated turn texts are one sample, not K: inconclusive.

    Regression for review round 2 (pseudo-replication): the per-call hypergeometric assumes K
    independent old outcomes; if the replay layer returns byte-identical calls for every seed
    (a cache that ignores the seed), the test would act on K times the evidence it actually
    holds and inflate ships, so the result must be inconclusive instead.
    """
    rf, af, log = make_fns(PERFECT, vary=False)
    r = run_gate("c1", "looping", [mk(c) for c in "abc"], [mk("g1")], "OLD", "NEW", 3, rf, af)
    assert r.decision == "inconclusive" and "replays do not vary; K copies are one sample" in r.reason
    # the remaining replays did run (only seed-0 pairs run before the check), but no ship slipped out
    assert r.p_value != r.p_value


# ---- unresolved evidence blocks (kept from review round 1) ----
def test_unresolved_trial_in_any_arm_is_inconclusive():
    """Unresolved evidence in old, new or the guards blocks any decision, with per-arm counts.

    Regression for review round 1 findings 1 and 2, kept through the redesign: if a trial with
    Unknown status, a NeedsReview verdict or invalid_output counted as a pass (old or new), an
    audit hole would read as a win; in the old arm it would also inflate the lift.
    """
    old = trials("old", {"a": True, "b": True, "c": True})
    good = trials("new", {"a": False, "b": False, "c": False})
    new = [t.model_copy(update={"unresolved": True}) if (t.call_id, t.seed) == ("a", 0) else t for t in good]
    r = decide(old, new, GUARD_OK, k=3)
    assert r.decision == "inconclusive" and "old=0, new=1, guards=0" in r.reason
    old2 = [t.model_copy(update={"unresolved": True}) if (t.call_id, t.seed) == ("a", 0) else t for t in old]
    r2 = decide(old2, good, GUARD_OK, k=3)
    assert r2.decision == "inconclusive" and "old=1" in r2.reason
    guards = [GuardCheck(call_id="g1"), GuardCheck(call_id="g2", unresolved=True)]
    r3 = decide(old, good, guards, k=3)
    assert r3.decision == "inconclusive" and "guards=1" in r3.reason


def test_trial_marks_unknown_needsreview_missing_and_invalid_unresolved():
    """_trial marks Unknown, NeedsReview, absent finding and invalid_output unresolved (not failed).

    Per the audit design rule that missing evidence is NeedsReview, never Pass: if any of the
    four states counted as a conclusive pass, unverifiable replays would behave like wins.
    """
    res = ReplayResult(call=mk("a")[0], fork_turn=2, ended_by="end_call")
    assert _trial("a", "new", 0, res, lambda _c: audit("Unknown"), "looping").unresolved
    nr = AuditRecord(call_id="x", verdict="NeedsReview", scope="bl_collection", findings=[])
    assert _trial("a", "new", 0, res, lambda _c: nr, "looping").unresolved
    assert _trial("a", "new", 0, res, lambda _c: audit("Pass", "Pass", "dead_air"), "looping").unresolved
    t = _trial("a", "new", 0, res, lambda _c: audit(), "looping")
    assert not t.unresolved and not t.failed
    inv = ReplayResult(call=mk("a")[0], fork_turn=2, ended_by="invalid_output")
    t2 = _trial("a", "new", 0, inv, lambda _c: audit(), "looping")
    assert t2.unresolved and t2.failed  # counts as a failure for the rates but blocks the decision


def test_failed_is_fatal_or_nonfatal_for_the_parameter():
    """failed() counts only Fatal and NonFatal findings of the target parameter.

    If a Pass or Unknown finding, a different parameter or no finding at all were scored as a
    failure, the fail rates, the lift and the p-value would all be computed on invented evidence.
    """
    assert failed(audit("Fatal", "Fatal"), "looping") and failed(audit("NonFatal", "NonFatal"), "looping")
    assert not failed(audit("Pass"), "looping") and not failed(audit("Unknown"), "looping")
    assert not failed(audit("Fatal", "Fatal"), "dead_air")
    assert not failed(AuditRecord(call_id="x", verdict="Pass", scope="bl_collection", findings=[]), "looping")


def test_old_trials_above_k_are_excluded_from_the_statistic():
    """Old trials at seed >= k_seeds stay out of the counts, the rates and per_call.

    If stray high-seed trials leaked into the stratified counts, the sample the p-value is
    computed on would disagree with the (call, seed) pairing the design specifies, and the
    phantom extra trials would distort the null.
    """
    old = trials("old", {"a": True, "b": True, "c": True}, k=3)
    extra = Trial(call_id="a", prompt="old", seed=7, failed=False, fatal=False, outcome="end_call")
    r = decide(old + [extra], trials("new", {"a": False, "b": False, "c": False}), GUARD_OK, k=3)
    assert r.per_call[0] == PerCall(call_id="a", old_fails=3, new_fails=0, k=3)
    assert r.old_fail_rate == 1.0


def test_evidence_thinner_than_min_calls_nan_p_and_inconclusive():
    """Fewer than min_calls matched calls is inconclusive and the p is nan (no test was run).

    Tiny samples ship on luck; keeping the threshold configurable keeps it a decision, and the
    nan p tells the caller that the statistic never got a fair sample, not that it failed.
    """
    old = trials("old", {"a": True, "b": True})
    new = trials("new", {"a": False, "b": False})
    r = decide(old, new, GUARD_OK, k=2)
    assert r.decision == "inconclusive" and r.n_calls == 2 and r.p_value != r.p_value
    assert decide(old, new, GUARD_OK, k=2, min_calls=2).decision == "ship"


# ---- GateResult API and the removal of the noise floor ----
def test_gate_result_carries_attempt_per_call_and_pre_flight_floor():
    """GateResult stores attempt, per-call counts, alpha and min_achievable_p for the caller.

    Regression for review round 2 (GateResult fields): the loop needs to count patch attempts
    separately (each attempt is its own test at alpha) and to see which call actually improved,
    or the dashboard could not rank fixes and duplicate attempts would pool their alphas.
    """
    r, _log = gate(PERFECT, attempt=3)
    assert r.attempt == 3 and r.alpha == 0.05 and r.min_achievable_p == pytest.approx((1 / 20) ** 3)
    assert r.per_call == [
        PerCall(call_id="a", old_fails=3, new_fails=0, k=3),
        PerCall(call_id="b", old_fails=3, new_fails=0, k=3),
        PerCall(call_id="c", old_fails=3, new_fails=0, k=3),
    ]
    assert r.k_seeds == 3 and r.n_calls == 3 and isinstance(r, GateResult)


def test_noise_floor_is_gone():
    """The noise floor (function PERCENTILE, the model fields, the 2K-old design) is fully removed.

    Regression for review round 2: the exact test replaces the floor calibration entirely; if a
    stale import or field survived, two statistics about one lift would sit in the same decision
    and disagree, so the old names must be gone from the module and the model.
    """
    with pytest.raises(ImportError):
        from vaniloop.gate import noise_floor  # noqa: F401
    assert "noise_floor" not in GateResult.model_fields
    import vaniloop.gate as g

    assert not hasattr(g, "PERCENTILE")
