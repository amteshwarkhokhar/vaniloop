"""Test drawings: the gate cards and the replay panel. Pure HTML, no Streamlit."""

import math
from pathlib import Path

from vaniloop.dashboard import load_audits, load_calls, load_gates
from vaniloop.gate import GateResult, PerCall
from vaniloop.models import AuditRecord, CallRecord, Finding, Turn
from vaniloop.viz import CSS, gate_cards, replay_panel

FIX = Path(__file__).parent / "fixtures"


def _gate(**kw) -> GateResult:
    base = {
        "cause_id": "C-01",
        "decision": "ship",
        "attempt": 2,
        "p_value": 0.002,
        "alpha": 0.05,
        "lift": 0.75,
        "per_call": [PerCall(call_id="callA", old_fails=3, new_fails=0, k=3)],
        "min_achievable_p": 0.000125,
        "old_fail_rate": 0.75,
        "new_fail_rate": 0.0,
        "n_calls": 1,
        "k_seeds": 3,
        "reason": "fine",
    }
    return GateResult(**{**base, **kw})


def test_gate_and_replay_drawings_escape_every_string_from_data():
    """Cause names, reasons, guard ids, call ids and replay text reach the page as text, never as markup.

    The dashboard renders these strings as HTML. A reason or a replayed line with a tag could inject script.
    """
    bad = "<script>alert(1)</script>"
    per_call = [PerCall(call_id=bad, old_fails=1, new_fails=0, k=2)]
    gate = _gate(reason=bad, guard_breaks=[bad], guard_flaky=[bad], per_call=per_call)
    call = CallRecord(call_id="c", duration_sec=1, turns=[Turn(idx=0, speaker="bot", start_sec=0, end_sec=1, text=bad)])
    for out in (gate_cards([gate], {"C-01": bad}), replay_panel(bad, call, None, 0, bad)):
        assert "<script>" not in out and "&lt;script&gt;" in out


def test_gate_cards_show_badge_p_against_alpha_rates_lift_per_call_counts_and_guards():
    """Each gate card carries the badge, p-value against alpha, both fail rates, lift, per-call counts and guards.

    These are the numbers the demo uses to prove a patch beats the old prompt. A missing one weakens the proof.
    """
    out = gate_cards([_gate(guard_flaky=["guardB"])], {"C-01": "Specs kept"})
    assert ">Ship</span>Specs kept" in out and "attempt 2" in out
    assert "0.0020 vs 0.05" in out and "at or below alpha" in out
    assert "75%" in out and "0%" in out and "+75 points" in out
    assert "3 of 3" in out and "0 of 3" in out and out.count('<i class="on">') == 3
    assert "<b>Guard breaks</b>none" in out and "<b>Flaky guards</b>guardB" in out
    assert "<b>1 of 1</b> tested fixes ship." in out
    rejected = gate_cards([_gate(decision="reject", p_value=0.4, guard_breaks=["guardA"])])
    assert ">Reject</span>" in rejected and "0.4000 vs 0.05" in rejected and "above alpha" in rejected
    assert "<b>Guard breaks</b>guardA" in rejected


def test_gate_card_says_no_test_ran_when_the_p_value_is_nan():
    """An early stop has p = nan. The card says no test ran and shows no nan or a made-up p-value.

    A printed nan would read as a broken page, and a made-up p would claim a test that never ran.
    """
    out = gate_cards([_gate(decision="inconclusive", p_value=math.nan, lift=0.0)])
    assert "No test run" in out and "nan" not in out.lower() and ">Inconclusive</span>" in out


def test_per_call_table_scrolls_inside_its_own_box():
    """The per-call table sits in a wrapper that scrolls sideways, so a narrow page does not scroll sideways.

    Without the wrapper, the table wraps its columns on a phone or overflows and drags the whole page.
    """
    out = gate_cards([_gate()])
    assert '<div class="vl-scroll"><div class="vl-pc">' in out
    assert ".vl-scroll { overflow-x: auto;" in CSS


def test_replay_panel_starts_at_the_fork_turn_and_names_the_cause_check():
    """The panel leaves out the original turns before the fork and says whether the cause's fault showed up.

    Showing the shared opening twice would bury the one place where the old and new prompts differ.
    """
    turns = [
        Turn(idx=i, speaker="bot" if i % 2 == 0 else "user", start_sec=6 * i, end_sec=6 * i + 6, text=f"line {i}")
        for i in range(5)
    ]
    call = CallRecord(call_id="c__replay_x_0_y", duration_sec=30, turns=turns)
    finding = Finding(parameter="looping", status="Fatal", reason="r", basis="observed", onset_turn=3)
    audit = AuditRecord(
        call_id=call.call_id, verdict="Fatal", scope="bl_collection", findings=[finding], first_fault_turn=3
    )
    out = replay_panel("Old prompt", call, audit, 3, "max_turns", 0, "looping", {"looping": "Asks again"})
    assert "line 3" in out and "line 4" in out and "line 2" not in out
    assert "Old prompt" in out and ">Fatal</span>" in out
    assert "from turn 3" in out and "ended by max turns" in out and "Asks again: fault shown" in out
    assert "other_parameter: unresolved" in replay_panel("New prompt", call, audit, 3, "end_call", 0, "other_parameter")
    assert "Fatal" not in replay_panel("New prompt", call, None, 3, "end_call")


def test_replay_panel_reads_missing_evidence_as_unresolved_never_as_no_fault():
    """An Unknown or absent finding, a NeedsReview verdict, an invalid_output ending or no audit reads unresolved.

    Found in review: missing evidence showed as "no fault", so a replay nobody could judge looked like a pass.
    """
    call = CallRecord(
        call_id="c__replay_x_0_y", duration_sec=6, turns=[Turn(idx=0, speaker="bot", start_sec=0, end_sec=6, text="hi")]
    )

    def audit(verdict, status=None):
        findings = [Finding(parameter="looping", status=status, reason="r", basis="observed")] if status else []
        return AuditRecord(call_id=call.call_id, verdict=verdict, scope="bl_collection", findings=findings)

    def state(audit_, ended_by="end_call"):
        return replay_panel("New prompt", call, audit_, 0, ended_by, 0, "looping", {"looping": "Asks again"})

    assert "Asks again: unresolved" in state(audit("Pass", "Unknown"))
    assert "Asks again: unresolved" in state(audit("Pass"))  # no finding for the parameter
    assert "Asks again: unresolved" in state(audit("NeedsReview", "Pass"))
    assert "Asks again: unresolved" in state(audit("Pass", "Pass"), "invalid_output")
    assert "Asks again: unresolved" in state(None)
    assert "Asks again: no fault" in state(audit("Pass", "Pass"))
    assert "Asks again: fault shown" in state(audit("NonFatal", "NonFatal"))


def test_gates_loader_keeps_the_latest_gate_per_cause_and_reads_a_null_p_value():
    """The loader returns one gate per cause with the newest attempt winning, and reads a null p_value as nan.

    If an older attempt won, the Test view would show a rejected patch that a later attempt already replaced.
    """
    gates = {g.cause_id: g for g in load_gates(FIX)}
    assert set(gates) == {"C-01", "C-03"} and gates["C-01"].attempt == 2 and gates["C-01"].decision == "ship"
    assert math.isnan(gates["C-03"].p_value)
    assert load_gates(FIX / "nowhere") == []
    assert load_audits(FIX) and load_calls(FIX)  # the new fixture files sit beside the old ones
