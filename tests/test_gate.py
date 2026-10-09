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


def test_noise_floor_stable_is_zero():
    assert noise_floor({"a": [True] * 4, "b": [False] * 4}) == 0.0
    assert noise_floor({}) == 0.0 and noise_floor({"a": [True]}) == 0.0


def test_noise_floor_half_flip_and_percentile_is_nearest_rank():
    # [T,T,F,F]: 6 pairs, 4 differ -> 2/3
    assert noise_floor({"a": [True, True, False, False]}) == pytest.approx(2 / 3)
    assert noise_floor({"a": [True, False]}) == 1.0
    # 20 calls, 19 stable, 1 flipping: ceil(0.95*20)=19th value is 0.0 (the max is only the 20th)
    calls = {f"s{i}": [True, True] for i in range(19)} | {"flip": [True, False]}
    assert noise_floor(calls) == 0.0
    # 10 calls, 1 flipping: ceil(9.5)=10th value is the max
    calls = {f"s{i}": [True, True] for i in range(9)} | {"flip": [True, False]}
    assert noise_floor(calls) == 1.0


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
    assert r.noise_floor == pytest.approx(2 / 3) and r.decision == "inconclusive"


def test_run_gate_fatal_guard_rejects():
    def b(cid, prompt, seed):
        if cid == "g1" and prompt == "NEW":
            return "Pass", "Fatal", "end_call"
        return ("Fatal", "Fatal", "max_turns") if prompt == "OLD" else ("Pass", "Pass", "end_call")

    r = gate(b)
    assert r.decision == "reject" and r.guard_breaks == ["g1"]


def test_invalid_output_counts_as_failed():
    def b(cid, prompt, seed):
        if prompt == "OLD" and not cid.startswith("g"):
            return "Pass", "Pass", "invalid_output"  # audit is clean but the replay was invalid
        return "Pass", "Pass", "end_call"

    r = gate(b)
    assert r.old_fail_rate == 1.0 and r.new_fail_rate == 0.0 and r.decision == "ship"
