"""Rubric rules on the 3 golden calls, with hand-labelled ledgers (no LLM needed)."""

from pathlib import Path

import pytest

from vaniloop.ingest import load_transcript
from vaniloop.models import BotTurnLabel, CallRecord, Ledger, Silence, Turn, UserTurnLabel
from vaniloop.rules import audit_call, rule_dead_air

T = Path(__file__).resolve().parents[1] / "transcripts"
GOLDEN = pytest.mark.skipif(not (T / "282_90685047_X413J1UG5.json").exists(), reason="local call data not present")


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


@GOLDEN
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


@GOLDEN
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


@GOLDEN
def test_dead_air_over_5s_is_nonfatal_with_audio_evidence():
    """Golden call 10: a silence over 5 s that starts after the buyer's turn and ends before the bot answers must
    give a NonFatal dead-air finding that cites the silence times. The turns are pinned around the silence because
    only a buyer-then-bot gap counts now, and the real transcript's turn edges are not known to line up. If this
    fails, dead air goes unreported or uncited.
    """
    call = load_transcript(T / "10_90797297_EMBDVXFH5.json")
    call.audio_path = call.audio_path or "x.mp3"
    call.turns = [
        Turn(idx=0, speaker="bot", start_sec=0.0, end_sec=5.0, text="hello"),
        Turn(idx=1, speaker="user", start_sec=25.0, end_sec=28.9, text="buyer line"),
        Turn(idx=2, speaker="bot", start_sec=37.0, end_sec=42.0, text="bot answer"),
        Turn(idx=3, speaker="bot", start_sec=50.0, end_sec=55.0, text="quantity?"),
    ]
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


@GOLDEN
def test_never_asking_quantity_in_a_bl_call_is_nonfatal():
    """A Buy Lead call where the bot never asks the quantity must grade qty_probing NonFatal, not Fatal.
    Human auditors grade an unprobed quantity mostly Non-Fatal. As Fatal, it gave 41% Fatal calls
    against a 4.7% human rate.
    """
    call = load_transcript(T / "10_90797297_EMBDVXFH5.json")
    led = Ledger(call_id=call.call_id, scope="bl_collection", bot_turns=[bot(0, "product")])
    a = audit_call(call, full(call, led))
    assert {f.parameter: f.status for f in a.findings}["qty_probing"] == "NonFatal"


# ---- dead air: only a long silence after the buyer and before VANI's answer counts
def _turn(idx, speaker, start, end):
    return Turn(idx=idx, speaker=speaker, start_sec=start, end_sec=end, text="t")


def _call(turns, silences):
    return CallRecord(call_id="c", audio_path="x.mp3", duration_sec=120.0, turns=turns, silences=silences)


def _led():
    return Ledger(call_id="c", scope="bl_collection")


def test_dead_air_pause_between_two_buyer_turns_passes():
    """A long silence with a buyer turn on both sides is a buyer or line pause, not VANI dead air, so it must
    Pass. If this failed, every thinking pause by the buyer would be graded as a bot fault.
    """
    call = _call(
        [_turn(0, "user", 0.0, 5.0), _turn(1, "user", 11.0, 16.0)],
        [Silence(start_sec=5.0, end_sec=11.0)],
    )
    f = rule_dead_air(call, _led())
    assert f.status == "Pass" and "buyer or line pauses" in f.reason


def test_dead_air_after_buyer_before_bot_is_nonfatal_with_onset():
    """A long gap that starts after the buyer finishes and ends before the bot answers is VANI dead air. It must
    be NonFatal with onset_turn at that answering bot turn, or the issue stage cannot tie the fault to a fix.
    """
    call = _call(
        [_turn(0, "user", 0.0, 5.0), _turn(1, "bot", 11.0, 16.0)],
        [Silence(start_sec=5.0, end_sec=11.0)],
    )
    f = rule_dead_air(call, _led())
    assert f.status == "NonFatal" and f.onset_turn == 1
    assert "1 bot gap(s)" in f.reason and "longest 6.0 s" in f.reason


def test_dead_air_counts_a_silence_starting_just_before_the_user_turn_ends():
    """Turn and silence edges drift, so a silence that starts 0.3 s before the buyer's turn ends must still count
    as dead air when the bot answers late. Without the slack, a real bot gap would be missed.
    """
    call = _call(
        [_turn(0, "user", 5.0, 10.3), _turn(1, "bot", 16.0, 20.0)],
        [Silence(start_sec=10.0, end_sec=16.0)],
    )
    f = rule_dead_air(call, _led())
    assert f.status == "NonFatal" and f.onset_turn == 1


def test_dead_air_ignores_silences_before_the_first_and_after_the_last_turn():
    """A silence before the first turn or after the last turn has no buyer-then-bot pair, so it must not be
    graded. Without this, leading and trailing room tone would read as bot faults.
    """
    call = _call(
        [_turn(0, "user", 10.0, 15.0), _turn(1, "bot", 20.0, 25.0)],
        [Silence(start_sec=0.0, end_sec=8.0), Silence(start_sec=30.0, end_sec=38.0)],
    )
    f = rule_dead_air(call, _led())
    assert f.status == "Pass" and "2 long silence(s) were buyer or line pauses" in f.reason
