"""Rubric rules on the 3 golden calls, with hand-labelled ledgers (no LLM needed)."""

from pathlib import Path

import pytest

from vaniloop.ingest import load_transcript
from vaniloop.models import BotTurnLabel, Ledger, Silence, UserTurnLabel
from vaniloop.rules import audit_call

T = Path(__file__).resolve().parents[1] / "transcripts"
pytestmark = pytest.mark.skipif(not (T / "282_90685047_X413J1UG5.json").exists(), reason="local call data not present")


def full(call, led):
    """Label every unlabelled turn with a neutral label so the ledger is complete."""
    have_b = {b.idx for b in led.bot_turns}
    have_u = {u.idx for u in led.user_turns}
    for t in call.turns:
        if t.speaker == "bot" and t.idx not in have_b:
            led.bot_turns.append(BotTurnLabel(idx=t.idx, slot="other"))
        if t.speaker == "user" and t.idx not in have_u:
            led.user_turns.append(UserTurnLabel(idx=t.idx))
    return led


def bot(idx, slot, fits=None, opts=()):
    return BotTurnLabel(
        idx=idx, slot=slot, fits_confirmed_product=fits, question_fits_product=fits, options_offered=list(opts)
    )


def test_282_camera_call_is_fatal_for_wrong_category_and_name_loop():
    """Golden call 282: after the buyer switches to a dome camera, drone specs and a 5-ask name loop must
    grade Fatal from turn 12. If this fails, the audit misses the hero failure of the demo.
    """
    call = load_transcript(T / "282_90685047_X413J1UG5.json")
    led = Ledger(
        call_id=call.call_id,
        scope="bl_collection",
        initial_product="school bus CCTV camera",
        final_product="dome camera, mobile connect",
        product_changed_at_turn=7,
        buyer_facts={"product": "dome camera mobile connect", "quantity": "1 piece", "name": "BuyerA"},
        bot_confirmed={"product": "dome camera mobile connect", "quantity": "1 piece"},
        product_match=True,
        bot_turns=[
            bot(0, "product"),
            bot(2, "quantity"),
            bot(4, "quantity"),
            bot(6, "product"),
            bot(8, "spec", True),
            bot(10, "quantity"),
            bot(12, "spec", False, ["DJI", "MI"]),
            bot(14, "spec", False, ["4K", "5.1K"]),
            bot(16, "spec", None),
            bot(18, "spec", False, ["45 minute"]),
            bot(20, "city"),
            bot(22, "city"),
            bot(24, "name"),
            bot(26, "name"),
            bot(28, "name"),
            bot(30, "name"),
            bot(32, "name"),
            bot(34, "handoff"),
        ],
        user_turns=[
            UserTurnLabel(idx=27, frustrated=0.7),
            UserTurnLabel(idx=33, frustrated=0.8, gave_value_for="name"),
        ],
    )
    a = audit_call(call, full(call, led))
    by = {f.parameter: f for f in a.findings}
    assert a.verdict == "Fatal"
    assert by["category_isq"].status == "Fatal"
    assert [e.turn_idx for e in by["category_isq"].evidence] == [12, 14, 18]
    assert by["looping"].status == "Fatal" and len(by["looping"].evidence) == 5
    assert by["qty_probing"].status == "Pass" and by["qty_missed"].status == "Pass"
    assert {"loops", "frustration", "wrong_requirement"} <= set(a.flags)
    assert a.first_fault_turn == 12


def test_133_seller_unavailable_handoff_passes_without_quantity():
    """Golden call 133: a confirmed product then a seller-unavailable handoff must Pass with quantity
    NotApplicable. If this fails, correct handoff calls are flagged as faults.
    """
    call = load_transcript(T / "133_90852837_DSXGKXVH5.json")
    led = Ledger(
        call_id=call.call_id,
        scope="seller_unavailable_handoff",
        final_product="commercial office cleaning",
        bot_confirmed={"product": "commercial office cleaning"},
        product_match=True,
        bot_turns=[bot(0, "product"), bot(2, "handoff")],
    )
    a = audit_call(call, full(call, led))
    assert a.verdict == "Pass"
    assert {f.parameter: f.status for f in a.findings}["qty_probing"] == "NotApplicable"


def test_dead_air_over_5s_is_nonfatal_with_audio_evidence():
    """Golden call 10: a silence over 5 s in the audio must give a NonFatal dead-air finding that cites the
    silence times. If this fails, dead air goes unreported or uncited.
    """
    call = load_transcript(T / "10_90797297_EMBDVXFH5.json")
    call.audio_path = call.audio_path or "x.mp3"
    call.silences = [Silence(start_sec=29.0, end_sec=36.5)]
    led = Ledger(
        call_id=call.call_id,
        scope="bl_collection",
        final_product="quantum magnetic resonance",
        buyer_facts={},
        bot_turns=[bot(0, "product"), bot(3, "quantity")],
    )
    a = audit_call(call, full(call, led))
    f = {f.parameter: f for f in a.findings}["dead_air"]
    assert f.status == "NonFatal" and f.evidence[0].start_sec == 29.0
    assert a.verdict == "NonFatal"


def test_never_asking_quantity_in_a_bl_call_is_nonfatal():
    """A Buy Lead call where the bot never asks the quantity must grade qty_probing NonFatal, not Fatal.
    Human auditors grade an unprobed quantity mostly Non-Fatal. As Fatal, it gave 41% Fatal calls
    against a 4.7% human rate.
    """
    call = load_transcript(T / "10_90797297_EMBDVXFH5.json")
    led = Ledger(call_id=call.call_id, scope="bl_collection", bot_turns=[bot(0, "product")])
    a = audit_call(call, full(call, led))
    assert {f.parameter: f.status for f in a.findings}["qty_probing"] == "NonFatal"
