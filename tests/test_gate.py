"""Ship gate: pure decision logic and run_gate with stubs. Synthetic only, no network."""

import pytest

from vaniloop.gate import Trial, decide, failed, noise_floor, run_gate
from vaniloop.models import AuditRecord, CallRecord, Finding, Ledger, Turn
from vaniloop.replay import ReplayResult


def audit(status="Pass", verdict="Pass", parameter="looping") -> AuditRecord:
    f = Finding(parameter=parameter, status=status, reason="r", basis="observed")
    return AuditRecord(call_id="x", verdict=verdict, scope="bl_collection", findings=[f])


def test_failed_is_fatal_or_nonfatal_for_the_parameter():
    assert failed(audit("Fatal", "Fatal"), "looping") and failed(audit("NonFatal", "NonFatal"), "looping")
    assert not failed(audit("Pass"), "looping") and not failed(audit("Unknown"), "looping")
    assert not failed(audit("Fatal", "Fatal"), "dead_air")  # another parameter
    assert not failed(AuditRecord(call_id="x", verdict="Pass", scope="bl_collection", findings=[]), "looping")


def test_noise_floor_all_stable_calls_is_zero():
    assert noise_floor({"a": [True] * 4, "b": [False] * 4}, k=2) == 0.0
    assert noise_floor({}, k=2) == 0.0 and noise_floor({"a": [True]}, k=1) == 0.0  # too few outcomes per call


def test_noise_floor_half_failing_calls_clearly_positive():
    # every call flips 50/50 across seeds: two halves of the same prompt often differ
    calls = {f"c{i}": [True, False, True, False] for i in range(20)}
    floor = noise_floor(calls, k=2)
    assert floor > 0.0 and floor <= 0.5


def test_noise_floor_is_deterministic_for_the_same_seed():
    calls = {f"c{i}": [True, False, False, True] for i in range(10)}
    assert noise_floor(calls, k=2) == noise_floor(calls, k=2)
    # a different seed may draw differently but stays a valid percentile
    assert 0.0 <= noise_floor(calls, k=2, rng_seed=7) <= 0.5


def test_noise_floor_percentile_nearest_rank_on_one_flip_call():
    # [T,T,F,F] split into halves of 2: diff is 0 when each half holds one True, else 1
    # (probability 1/3), so the 95th percentile of 200 perms is 1.0
    assert noise_floor({"a": [True, True, False, False]}, k=2) == 1.0


def trials(prompt, fails_by_call, fatal=False, k=2):
    return [Trial(call_id=c, prompt=prompt, seed=s, failed=f, fatal=fatal and f, outcome="end_call")
            for c, f in fails_by_call.items() for s in range(k)]


OLD = trials("old", {"a": True, "b": True, "c": True, "d": True})
NEW_GOOD = trials("new", {"a": False, "b": False, "c": False, "d": False})
GUARD_OK = trials("new", {"g1": False, "g2": False})


def test_ship_when_lift_above_floor_and_guards_clean():
    r = decide(OLD, NEW_GOOD, GUARD_OK, 0.1)
    assert r.decision == "ship" and r.lift == 1.0 and r.n_calls == 4 and r.k_seeds == 2 and r.guard_breaks == []


def test_fatal_guard_rejects_even_with_big_lift():
    guards = trials("new", {"g1": False, "g2": True}, fatal=True)
    r = decide(OLD, NEW_GOOD, guards, 0.0)
    assert r.decision == "reject" and r.guard_breaks == ["g2"]


def test_no_lift_rejects():
    assert decide(OLD, OLD, GUARD_OK, 0.0).decision == "reject"
    worse = trials("new", {"a": True, "b": True, "c": True, "d": True})
    assert decide(trials("old", {"a": True, "b": False, "c": False, "d": False}), worse, [], 0.0).decision == "reject"


def test_small_lift_within_floor_is_inconclusive():
    new = trials("new", {"a": False, "b": True, "c": True, "d": True})  # lift 0.25
    assert decide(OLD, new, GUARD_OK, 0.25).decision == "inconclusive"  # equal to floor is not above it
    assert decide(OLD, new, GUARD_OK, 0.5).decision == "inconclusive"


def test_fewer_than_three_calls_is_inconclusive():
    old, new = trials("old", {"a": True, "b": True}), trials("new", {"a": False, "b": False})
    r = decide(old, new, GUARD_OK, 0.0)
    assert r.decision == "inconclusive" and r.n_calls == 2
    assert decide(old, new, GUARD_OK, 0.0, min_calls=2).decision == "ship"


# ---- run_gate with stubs ----
def mk(cid):
    turns = [Turn(idx=i, speaker="bot" if i % 2 == 0 else "user", start_sec=i * 6.0, end_sec=i * 6.0 + 6, text="t")
             for i in range(4)]
    return CallRecord(call_id=cid, duration_sec=24.0, turns=turns), Ledger(call_id=cid, scope="bl_collection"), 2


def make_fns(behaviour):
    """behaviour(call_id, prompt, seed) -> (looping status, verdict, ended_by). The replay call id carries status and verdict."""
    def replay_fn(call, ledger, fork, prompt, seed):
        s, v, e = behaviour(call.call_id, prompt, seed)
        rec = call.model_copy(update={"call_id": f"{call.call_id}|{s}|{v}"})
        return ReplayResult(call=rec, fork_turn=fork, ended_by=e)

    def audit_fn(rec):
        _, s, v = rec.call_id.split("|")
        return audit(s, v)

    return replay_fn, audit_fn


def gate(behaviour, guards=("g1",), k=3):
    rf, af = make_fns(behaviour)
    return run_gate("cause1", "looping", [mk(c) for c in "abcd"], [mk(g) for g in guards], "OLD", "NEW", k, rf, af)


def test_run_gate_looping_old_fixed_new_ships():
    def b(cid, prompt, seed):
        if cid.startswith("g"):
            return "Pass", "Pass", "end_call"
        return ("Fatal", "Fatal", "max_turns") if prompt == "OLD" else ("Pass", "Pass", "end_call")

    r = gate(b)
    assert r.decision == "ship" and r.old_fail_rate == 1.0 and r.new_fail_rate == 0.0
    assert r.noise_floor == 0.0 and r.n_calls == 4 and r.k_seeds == 3 and r.cause_id == "cause1"


def test_run_gate_identical_prompts_do_not_ship():
    r = gate(lambda cid, p, s: ("Fatal", "Fatal", "max_turns") if not cid.startswith("g") else ("Pass", "Pass", "end_call"))
    assert r.decision == "reject" and r.lift == 0.0


def test_run_gate_noisy_old_prompt_is_inconclusive():
    # old prompt fails on seed 0 only for every call, new never fails: lift 1/3, floor 2/3
    def b(cid, prompt, seed):
        if prompt == "OLD" and not cid.startswith("g") and seed == 0:
            return "Fatal", "Fatal", "end_call"
        return "Pass", "Pass", "end_call"

    r = gate(b)
    # floor is the permutation null on the same 1/3-noisy outcomes: lift 1/3 does not beat it
    assert r.noise_floor == pytest.approx(1 / 3) and r.decision == "inconclusive"


def test_run_gate_fatal_guard_rejects():
    def b(cid, prompt, seed):
        if cid == "g1" and prompt == "NEW":
            return "Pass", "Fatal", "end_call"
        return ("Fatal", "Fatal", "max_turns") if prompt == "OLD" else ("Pass", "Pass", "end_call")

    r = gate(b)
    assert r.decision == "reject" and r.guard_breaks == ["g1"]


def test_invalid_output_counts_as_failed_and_unresolved():
    def b(cid, prompt, seed):
        if prompt == "OLD" and not cid.startswith("g"):
            return "Pass", "Pass", "invalid_output"  # audit is clean but the replay was invalid
        return "Pass", "Pass", "end_call"

    r = gate(b)
    assert r.old_fail_rate == 1.0 and r.new_fail_rate == 0.0  # invalid still counts as failed for rates
    # old trials only fail, they do not block; a new or guard invalid trial would (see fix 1 tests)
    assert r.decision == "ship"


# ---- fix 1: unresolved evidence ----
def test_unresolved_new_trial_is_inconclusive():
    old = trials("old", {"a": True, "b": True, "c": True})
    new = trials("new", {"a": False, "b": False, "c": False})
    new[0] = new[0].model_copy(update={"unresolved": True})
    r = decide(old, new, GUARD_OK, 0.0)
    assert r.decision == "inconclusive" and "1 trial(s) lack conclusive evidence" in r.reason


def test_unknown_and_needs_review_and_missing_finding_are_unresolved():
    from vaniloop.gate import _trial
    res = ReplayResult(call=mk("a")[0], fork_turn=0, ended_by="end_call")
    # Unknown finding status
    t = _trial("a", "new", 0, res, lambda _c: audit("Unknown"), "looping")
    assert t.unresolved and not t.failed
    # NeedsReview verdict
    t = _trial("a", "new", 0, res, lambda _c: AuditRecord(call_id="x", verdict="NeedsReview",
                                                          scope="bl_collection", findings=[]), "looping")
    assert t.unresolved and not t.failed
    # no finding at all for the target parameter
    t = _trial("a", "new", 0, res, lambda _c: AuditRecord(call_id="x", verdict="Pass",
                                                          scope="bl_collection", findings=[]), "dead_air")
    assert t.unresolved and not t.failed


def test_guard_with_unresolved_trial_is_inconclusive_not_ship():
    guards = trials("new", {"g1": False, "g2": False})
    guards[2] = guards[2].model_copy(update={"unresolved": True})  # e.g. one guard replay was invalid_output
    r = decide(OLD, NEW_GOOD, guards, 0.0)
    assert r.decision == "inconclusive" and "1 trial(s)" in r.reason


def test_fix_2_k_seeds_below_two_raises_and_no_calibration_is_inconclusive():
    rf, af = make_fns(lambda cid, p, s: ("Fatal", "Fatal", "max_turns"))
    with pytest.raises(ValueError, match="k_seeds"):
        run_gate("c1", "looping", [mk("a")], [], "OLD", "NEW", 1, rf, af)
    with pytest.raises(ValueError, match="k_seeds"):
        run_gate("c1", "looping", [mk("a")], [], "OLD", "NEW", 0, rf, af)
    # decide() with no calibration data: inconclusive, never ship
    r = decide([], [], [], None)
    assert r.decision == "inconclusive" and "calibration" in r.reason


def test_unmatched_old_and_new_is_inconclusive():
    guards = trials("new", {"g1": False})
    # old has only call a, new has b and c: no shared (call, seed) pairs at all
    old = trials("old", {"a": True})
    new = trials("new", {"b": False, "c": False})
    r = decide(old, new, guards, 0.0)
    assert r.decision == "inconclusive" and "different (call, seed) samples" in r.reason
    # partial match: a in both, d only in old: the (call, seed) sets differ
    old = trials("old", {"a": True, "d": True})
    new = trials("new", {"a": False})
    r = decide(old, new, guards, 0.0)
    assert r.decision == "inconclusive" and "different (call, seed) samples" in r.reason
    # fully matched on both sides but only one call: below min_calls
    old = trials("old", {"a": True})
    new = trials("new", {"a": False})
    r = decide(old, new, guards, 0.0)
    assert r.decision == "inconclusive" and "1 matched call(s)" in r.reason


# ---- fix 3: a perfect fix on noisy calls now ships because the floor is on the pooled scale ----
def test_perfect_fix_on_noisy_calls_ships_against_permutation_floor():
    # old fails on 1 of 3 seeds per call (a 1/3 noisy fail rate); new never fails. The old pair-flip floor
    # (near 1.0) blocked this; the pooled permutation floor is far below a lift of 1/3 over many calls.
    calls = {f"c{i}": [True, False, False, False, False, False] for i in range(30)}
    old = [Trial(call_id=c, prompt="old", seed=s, failed=f, fatal=False, outcome="end_call")
           for c, fs in calls.items() for s, f in enumerate(fs)]
    new = [Trial(call_id=c, prompt="new", seed=s, failed=False, fatal=False, outcome="end_call")
           for c in calls for s in range(3)]
    k = 3
    floor = noise_floor(calls, k=k)
    r = decide([t for t in old if t.seed < k], new, GUARD_OK, floor, cause_id="c1", k_seeds=k)
    assert r.decision == "ship" and r.lift == pytest.approx(1 / 3)
