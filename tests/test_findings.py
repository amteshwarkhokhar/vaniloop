"""Self-contained tests for the review findings. No local data needed (CallRecord objects built in code)."""

import json
import subprocess
import sys
import types

import pytest

from vaniloop import issue, ledger, llm, rules
from vaniloop.ingest import detect_silences
from vaniloop.models import BotTurnLabel, CallRecord, Ledger, Silence, Turn, UserTurnLabel
from vaniloop.rules import audit_call, parse_quantity


def mk_call(n_turns=12, audio=None, silences=(), duration=100.0, idx_offset=0):
    turns = [
        Turn(
            idx=i + idx_offset,
            speaker="bot" if i % 2 == 0 else "user",
            start_sec=i * 5.0,
            end_sec=i * 5.0 + 4,
            text=f"t{i}",
        )
        for i in range(n_turns)
    ]
    return CallRecord(call_id="c", audio_path=audio, duration_sec=duration, turns=turns, silences=list(silences))


def mk_led(call, bots=None, users=None, **kw):
    """Complete ledger: unlabelled turns get neutral labels."""
    bots = {b.idx: b for b in (bots or [])}
    users = {u.idx: u for u in (users or [])}
    for t in call.turns:
        if t.speaker == "bot":
            bots.setdefault(t.idx, BotTurnLabel(idx=t.idx, slot="other"))
        else:
            users.setdefault(t.idx, UserTurnLabel(idx=t.idx))
    kw.setdefault("scope", "bl_collection")
    kw.setdefault("buyer_facts", {"quantity": "10"})
    kw.setdefault("bot_confirmed", {"product": "pump", "quantity": "10"})
    kw.setdefault("final_product", "pump")
    kw.setdefault("product_match", True)
    led = Ledger(
        call_id=call.call_id,
        bot_turns=sorted(bots.values(), key=lambda b: b.idx),
        user_turns=sorted(users.values(), key=lambda u: u.idx),
        **kw,
    )
    # a quantity ask so qty_probing passes
    return led


def bt(idx, slot, **kw):
    return BotTurnLabel(idx=idx, slot=slot, **kw)


def by(a):
    return {f.parameter: f for f in a.findings}


def clean():
    call = mk_call()
    led = mk_led(call, [bt(0, "quantity")])
    return call, led


def test_baseline_clean_call_passes():
    """A fully labelled clean call with the quantity ask must grade Pass with no fault findings.

    A false fault here would push a good call into the prompt-fix loop and waste the fix budget.
    """
    call, led = clean()
    a = audit_call(call, led)
    assert a.verdict == "Pass", [(f.parameter, f.status) for f in a.findings]


# ---- F1
def test_f1_low_coverage_needs_review():
    """Regression for review round 1 finding: a ledger that labels less than the full transcript must grade NeedsReview.

    If it passed, an audit would approve a call after reading only half its turns.
    """
    call, led = clean()
    led.bot_turns = led.bot_turns[:3]  # 3 of 6 bot turns
    a = audit_call(call, led)
    assert a.verdict == "NeedsReview" and a.review_reason.startswith("incomplete ledger (coverage 50%)")


def test_f1_low_user_coverage_and_scope_missing():
    """Regression for review round 1 finding: missing user labels or an unclear scope must force NeedsReview.

    A silent Pass on incomplete evidence would let an unread call count as a clean call.
    """
    call, led = clean()
    led.user_turns = []
    assert audit_call(call, led).verdict == "NeedsReview"
    call, led = clean()
    led.scope = "unclear"
    a = audit_call(call, led)
    assert a.verdict == "NeedsReview" and "scope missing" in a.review_reason


def test_f1_null_spec_fit_is_unknown_and_needs_review():
    """Regression for review round 1 finding: a spec question with null fit information must be Unknown and NeedsReview.

    Grading it Pass would hide a spec question that the audit could not judge.
    """
    call = mk_call()
    led = mk_led(call, [bt(0, "quantity"), bt(2, "spec", fits_confirmed_product=None, spec_name="brand")])
    a = audit_call(call, led)
    assert by(a)["category_isq"].status == "Unknown"
    assert a.verdict == "NeedsReview"


def test_f1_precedence_fatal_over_unknown_and_missing_audio_exempt():
    """Regression for review round 1 finding: a Fatal finding must outrank Unknown, and missing audio alone must not.

    The wrong precedence would either bury a real fault or send a silent call to manual review.
    """
    call = mk_call()
    led = mk_led(
        call,
        [
            bt(0, "quantity"),
            bt(2, "spec", fits_confirmed_product=False, question_fits_product=False, spec_name="x"),
            bt(4, "spec", fits_confirmed_product=None, spec_name="y"),
        ],
    )
    assert audit_call(call, led).verdict == "Fatal"
    call, led = clean()  # audio_path None -> dead_air Unknown, must not force NeedsReview
    a = audit_call(call, led)
    assert by(a)["dead_air"].status == "Unknown" and a.verdict == "Pass"


# ---- F2
def test_f2_normalise_garbage_never_raises():
    """Regression for review round 1 finding: wrong-typed LLM output must become safe defaults without a crash.

    A crash would abort the whole audit run on one malformed model response.
    """
    call = mk_call()
    d = {
        "scope": ["x"],
        "buyer_facts": [1],
        "bot_confirmed": "s",
        "bot_turns": {"a": 1},
        "user_turns": "zz",
        "product_changed_at_turn": "3",
        "product_match": "yes",
    }
    out = ledger._normalise(d, call)
    assert out["scope"] == "unclear" and out["bot_turns"] == [] and out["user_turns"] == []
    assert out["buyer_facts"] == {} and out["product_changed_at_turn"] is None and out["product_match"] is None
    assert ledger._normalise([], call)["scope"] == "unclear"
    assert ledger._normalise(None, call)["bot_turns"] == []


def test_f2_idx_rules():
    """Regression for review round 1 finding: label indices must be checked against the real turn speaker and position.

    Duplicate, out-of-range or invalid indices would attach a label to the wrong turn.
    """
    call = mk_call()
    d = {
        "scope": "bl_collection",
        "product_changed_at_turn": 0,  # idx 0 is a bot turn
        "bot_turns": [
            {"idx": 0, "slot": "product"},
            {"idx": 0, "slot": "city"},
            {"idx": "2", "slot": "city"},
            {"idx": 99, "slot": "city"},
            {"idx": 1, "slot": "city"},
            5,
            {"idx": True, "slot": "x"},
            {"idx": 4, "slot": "bogus", "fits_confirmed_product": "yes"},
        ],
        "user_turns": [
            {"idx": 1, "frustrated": "nan"},
            {"idx": 1, "frustrated": 1},
            {"idx": 0},
            {"idx": 3, "frustrated": 7},
        ],
    }
    out = ledger._normalise(d, call)
    assert [(b["idx"], b["slot"]) for b in out["bot_turns"]] == [(0, "product"), (4, "other")]
    assert [(u["idx"], u["frustrated"]) for u in out["user_turns"]] == [(1, 0.0), (3, 1.0)]
    assert out["product_changed_at_turn"] is None
    d["product_changed_at_turn"] = 3
    assert ledger._normalise(d, call)["product_changed_at_turn"] == 3


def test_normalise_declined_accepts_true_and_the_string_true_only():
    """The declined label must pass through as True for a real true or the string "true", and be False otherwise.

    A string "false" read as truthy would give a false Fatal on intent. A string "true" dropped to False would hide a
    late cancel and give a false Pass (Codex round 2).
    """
    call = mk_call()
    d = {
        "scope": "refusal",
        "user_turns": [
            {"idx": 1, "declined": True},
            {"idx": 3, "declined": "false"},
            {"idx": 5, "declined": 1},
            {"idx": 7},
            {"idx": 9, "declined": " True "},
        ],
    }
    out = ledger._normalise(d, call)
    got = [(u["idx"], u["declined"]) for u in out["user_turns"]]
    assert got == [(1, True), (3, False), (5, False), (7, False), (9, True)]
    assert UserTurnLabel(idx=1).declined is False


def test_f2_evidence_resolves_by_idx_field_and_never_raises():
    """Regression for review round 1 finding: evidence must resolve by the turn idx field, not by list position.

    Position-based lookup would quote the wrong turn, and an unknown idx must return empty evidence.
    """
    call = mk_call(idx_offset=100)  # idx 100.. ; list positions 0..
    ev = rules._ev(call, 102)
    assert ev.turn_idx == 102 and ev.quote == "t2"
    assert rules._ev(call, 5).quote is None  # unknown idx does not raise
    led = mk_led(call, [bt(100, "quantity")])
    audit_call(call, led)


# ---- F3
def test_f3_qty_never_asked_explicit_assumption(monkeypatch):
    """A bot that never asks quantity is NonFatal with a spoken_proxy basis, as the human audit grades it.

    The rule must fall back to Unknown once quantity is not a defined field for the category.
    """
    call = mk_call()
    led = mk_led(call, buyer_facts={})
    f = by(audit_call(call, led))["qty_probing"]
    assert f.status == "NonFatal" and f.basis == "spoken_proxy"
    assert f.reason.endswith("(assumes quantity is a defined field for this category)")
    monkeypatch.setattr(rules, "QTY_ALWAYS_DEFINED", False)
    assert by(audit_call(call, led))["qty_probing"].status == "Unknown"


# ---- F4
@pytest.mark.parametrize(
    "text,val",
    [
        ("1 piece", 1),
        ("1.5 ton", 1.5),
        ("1,000 pcs", 1000),
        ("twenty five", 25),
        ("two hundred", 200),
        ("one thousand five hundred", 1500),
        ("do sau", 200),
        ("dedh", 1.5),
        ("dhai", 2.5),
        ("ek", 1),
        ("paanch hazaar", 5000),
        ("ek lakh", 100000),
        ("dedh sau piece", 150),
        ("pachaas", 50),
        ("2 lakh", 200000),
        ("das", 10),
        ("bees", 20),
        ("ek hazar", 1000),
        ("100", 100),
    ],
)
def test_f4_parse_quantity(text, val):
    """Regression for review round 1 finding: buyer quantities in digits, English or Hinglish words must parse right.

    Units such as lakh and hazaar must map correctly, or quantity accuracy grades the wrong number.
    """
    assert parse_quantity(text) == pytest.approx(val)


@pytest.mark.parametrize("text", ["", None, "some", "kuch bhi"])
def test_f4_parse_quantity_none(text):
    """Regression for review round 1 finding: empty, null or meaningless quantity text must parse to None.

    Guessing a number would make an uncaptured quantity look captured in the shadow Buy Lead.
    """
    assert parse_quantity(text) is None


def _qty(said, kept):
    call = mk_call()
    # the buyer answers at turn 1 and the bot asks again at turn 2 when nothing was captured (ignored)
    bots = [bt(0, "quantity")] + ([] if kept else [bt(2, "quantity")])
    led = mk_led(
        call,
        bots,
        [UserTurnLabel(idx=1, gave_value_for="quantity")],
        buyer_facts={"quantity": said},
        bot_confirmed={"product": "pump", **({"quantity": kept} if kept else {})},
    )
    return by(audit_call(call, led))


def test_f4_qty_missed_vs_accuracy_split():
    """Regression for review round 1 finding: qty_missed and qty_accuracy must separate missing from wrong capture.

    The tolerance bands must match the prompt, or a correct read-back is marked faulty and vice versa.
    """
    f = _qty("10", None)
    assert f["qty_missed"].status == "Fatal" and f["qty_accuracy"].status == "NotApplicable"
    f = _qty("10", "10")
    assert f["qty_missed"].status == "Pass" and f["qty_accuracy"].status == "Pass"
    assert _qty("10", "50")["qty_accuracy"].status == "Fatal"
    assert _qty("10", "50")["qty_missed"].status == "Pass"
    assert _qty("100", "110")["qty_accuracy"].status == "NonFatal"  # abs 10, rel 10%
    assert _qty("100", "130")["qty_accuracy"].status == "Fatal"  # rel 30%
    assert _qty("1000", "1010")["qty_accuracy"].status == "NonFatal"
    assert _qty("10", "13")["qty_accuracy"].status == "Fatal"  # abs 3 but rel 30%
    assert _qty("500", "450")["qty_accuracy"].status == "Fatal"  # rel 10% but abs 50
    assert _qty("kuch", "10")["qty_accuracy"].status == "Unknown"
    assert _qty("do sau", "200")["qty_accuracy"].status == "Pass"


def test_f4_issue_name():
    """Regression for review round 1 finding: the qty_accuracy cause must keep its audited display name and prompt type.

    A renamed cause would break the issue report that the fix stage keys on.
    """
    assert issue.KNOWN["qty_accuracy"][0] == "Quantity captured wrong" and issue.KNOWN["qty_accuracy"][2] == "prompt"


# ---- F5
def test_f5_product_accuracy():
    """Regression for review round 1 finding: product_change must judge matching by the stored match flag, not a word.

    Without confirmation the finding must be Unknown; a stale overlap helper would fake a match.
    """

    def run(**kw):
        call = mk_call()
        led = mk_led(call, [bt(0, "quantity")], **kw)
        return by(audit_call(call, led))["product_change"]

    assert (
        run(
            product_match=False, bot_confirmed={"product": "pump motor", "quantity": "10"}, final_product="pump cover"
        ).status
        == "Fatal"
    )  # shares word "pump", no change event
    assert run(product_match=True).status == "Pass"
    assert run(product_match=None).status == "Unknown"
    assert run(bot_confirmed={"quantity": "10"}).status == "Unknown"
    assert not hasattr(rules, "_overlap")


# ---- F6
def _loop_led(call, bots, users=()):
    return mk_led(call, bots, list(users))


def test_f6_per_spec_name_and_severity():
    """Regression for review round 1 finding: looping must count spec asks per spec_name, not per slot.

    Merging different interleaved specs would flag a false loop where each spec was asked once.
    """
    call = mk_call(20)
    # 4 asks for brand (spec), interleaved with other specs -> loop by spec_name, not by slot "spec"
    bots = [
        bt(0, "quantity"),
        bt(2, "spec", spec_name="brand", fits_confirmed_product=True),
        bt(4, "spec", spec_name="size", fits_confirmed_product=True),
        bt(6, "spec", spec_name="brand", fits_confirmed_product=True),
        bt(8, "spec", spec_name="brand", fits_confirmed_product=True),
        bt(10, "spec", spec_name="brand", fits_confirmed_product=True),
    ]
    f = by(audit_call(call, _loop_led(call, bots)))["looping"]
    assert f.status == "NonFatal" and "brand" in f.reason and f.onset_turn == 8  # spec limit is 2
    # different specs, each asked once -> no loop (old code merged them under slot "spec")
    bots = [bt(0, "quantity")] + [
        bt(i, "spec", spec_name=f"s{i}", fits_confirmed_product=True) for i in (2, 4, 6, 8, 10)
    ]
    assert by(audit_call(call, _loop_led(call, bots)))["looping"].status == "Pass"


def test_f6_frustration_before_threshold_does_not_make_fatal_but_after_does():
    """Regression for review round 1 finding: buyer frustration only raises looping to Fatal at or after the repeat.

    A wrong threshold would either soften a real loop or harden a call the buyer tolerated.
    """
    call = mk_call(30)
    bots = [bt(0, "quantity")] + [bt(i, "name") for i in (2, 4, 6, 8)]  # 4th ask at 8
    early = [UserTurnLabel(idx=5, frustrated=0.9)]
    assert by(audit_call(call, _loop_led(call, bots, early)))["looping"].status == "NonFatal"
    late = [UserTurnLabel(idx=9, frustrated=0.9)]
    assert by(audit_call(call, _loop_led(call, bots, late)))["looping"].status == "Fatal"


def test_f6_call_ending_after_fourth_ask_is_nonfatal_and_worst_slot_reported():
    """Regression for review round 1 finding: the worst looping slot is reported. A call end alone is NonFatal (#95).

    Reporting the wrong slot would point the prompt fix at a question that was not the loop. When both slots are
    Fatal, the one with more asks must win the tie-break; the test irritates the buyer after both onsets for that.
    """
    call = mk_call(12)  # turns 0..11
    bots = [bt(0, "quantity")] + [bt(i, "name") for i in (2, 4, 6, 8)] + [bt(10, "city")]
    f = by(audit_call(call, _loop_led(call, bots)))["looping"]  # 4th name ask at 8, 3 turns left (9,10,11)
    assert f.status == "NonFatal"
    call2 = mk_call(10)  # 0..9
    bots2 = [bt(0, "city"), bt(2, "city"), bt(4, "city"), bt(6, "city"), bt(8, "city")]  # 4th at 6, turns 7,8,9 after
    assert by(audit_call(call2, _loop_led(call2, bots2)))["looping"].status == "NonFatal"
    call3 = mk_call(9)  # 0..8; 4th at 6, turns 7,8 after -> the call ends right after, but nobody is irritated
    assert by(audit_call(call3, _loop_led(call3, bots2)))["looping"].status == "NonFatal"
    # two looping slots, both Fatal: the tie-break must pick the one with more asks
    call4 = mk_call(30)
    bots4 = [bt(0, "quantity")] + [bt(i, "city") for i in (2, 4, 6, 8)] + [bt(i, "name") for i in (10, 12, 14, 16, 18)]
    users = [UserTurnLabel(idx=9, frustrated=0.9), UserTurnLabel(idx=17, frustrated=0.9)]
    f = by(audit_call(call4, _loop_led(call4, bots4, users)))["looping"]
    assert f.status == "Fatal" and "name" in f.reason  # both Fatal, name has more asks
    f2 = by(audit_call(call4, _loop_led(call4, bots4, [UserTurnLabel(idx=9, frustrated=0.9)])))["looping"]
    assert f2.status == "Fatal" and "city" in f2.reason  # 9 is after city's 4th (8) but before name's (16)


# ---- F7
def test_f7_onset_and_first_fault():
    """Regression for review round 1 finding: onset_turn is the crossing turn and first_fault_turn the earliest fault.

    Using evidence turns that start earlier would move the fault onset to the wrong turn.
    """
    call = mk_call(30)
    bots = [
        bt(0, "quantity"),
        bt(2, "spec", spec_name="a", fits_confirmed_product=True),
        bt(4, "spec", spec_name="b", fits_confirmed_product=False, question_fits_product=False),
        bt(6, "spec", spec_name="c", fits_confirmed_product=False, question_fits_product=False),
    ] + [bt(i, "name") for i in (10, 12, 14, 16)]
    a = audit_call(call, mk_led(call, bots))
    f = by(a)
    assert f["category_isq"].onset_turn == 4 and f["looping"].onset_turn == 16
    assert a.first_fault_turn == 4
    # evidence lists context turns that start earlier than onset: first_fault must not use evidence
    assert min(e.turn_idx for e in f["looping"].evidence) < f["looping"].onset_turn


def test_f7_product_and_qty_onsets():
    """Regression for review round 1 finding: product_change and qty_missed onsets must point at the fault turn or None.

    A wrong onset would misplace the first fault and the replay fork that the fix stage tests.
    """
    call = mk_call(12)
    led = mk_led(
        call,
        [bt(0, "quantity"), bt(6, "confirm"), bt(8, "handoff")],
        product_match=False,
        product_changed_at_turn=3,
        bot_confirmed={"product": "old"},
    )
    f = by(audit_call(call, led))
    assert f["product_change"].onset_turn == 4  # first bot turn after the change at turn 3
    led2 = mk_led(
        call,
        [bt(0, "quantity"), bt(8, "quantity")],
        [UserTurnLabel(idx=1, gave_value_for="quantity")],
        buyer_facts={"quantity": "10"},
        bot_confirmed={"product": "pump"},
    )
    assert by(audit_call(call, led2))["qty_missed"].onset_turn == 8
    led3 = mk_led(
        call,
        [bt(0, "quantity")],
        [UserTurnLabel(idx=1, gave_value_for="quantity")],
        buyer_facts={"quantity": "10"},
        bot_confirmed={"product": "pump"},
    )  # moved on, no read-back
    assert by(audit_call(call, led3))["qty_missed"].onset_turn is None
    assert audit_call(call, led3).first_fault_turn is None


# ---- F8
def test_f8_noisy_constant_defined_no_behaviour_change():
    """Regression for review round 1 finding: the noisy dead-air threshold must stay at eight and the plain one at five.

    A changed constant would regrade every past call and break the stated no-behaviour-change promise. The silence
    now sits after a buyer turn and before the bot's answer, because only a buyer-then-bot gap counts as dead air.
    The call duration is 20 s so it matches the turns (#105): timing that stops short of the audio is untrusted.
    """
    assert rules.DEAD_AIR_NOISY_SEC == 8.0 and rules.DEAD_AIR_SEC == 5.0
    # 11 s: starts after the buyer turn and ends before the bot answers, with no speech inside, so it is a bot gap
    call = mk_call(audio="x.mp3", silences=[Silence(start_sec=4.0, end_sec=15.0)], duration=20.0)
    call.turns = [
        Turn(idx=0, speaker="user", start_sec=0.0, end_sec=4.0, text="buyer"),
        Turn(idx=1, speaker="bot", start_sec=15.0, end_sec=19.0, text="answer"),
    ]
    assert by(audit_call(call, mk_led(call, [bt(1, "quantity")])))["dead_air"].status == "NonFatal"


# ---- #105
def _timed(turns, duration, audio="x.mp3"):
    """A call whose turns are given as (speaker, start, end) triples."""
    call = mk_call(audio=audio, duration=duration)
    call.turns = [Turn(idx=i, speaker=s, start_sec=a, end_sec=b, text="t") for i, (s, a, b) in enumerate(turns)]
    return call


GOOD = [("bot", 0.0, 4.0), ("user", 5.0, 9.0), ("bot", 10.0, 14.0), ("user", 15.0, 19.0)]


def test_untrusted_coverage_gives_unknown_and_review_flag():
    """Turns that end at 59% of the audio must make dead_air Unknown (basis unavailable) with the dead_air_review flag.

    If this failed, a call whose timing cannot show a gap would Pass dead air, a check that was never made.
    """
    call = _timed(GOOD, duration=32.0)
    a = audit_call(call, mk_led(call))
    f = by(a)["dead_air"]
    assert f.status == "Unknown" and f.basis == "unavailable" and "does not match the audio" in f.reason
    assert "59%" in f.reason and "dead_air_review" in a.flags


def test_back_to_back_turns_give_unknown():
    """Turns with no gap between them (made-up lengths) must make dead_air Unknown.

    If this failed, a transcript that packs turns edge to edge would Pass on a gap it cannot represent.
    """
    turns = [("bot", 0.0, 4.0), ("user", 4.0, 8.0), ("bot", 8.0, 12.0), ("user", 12.0, 16.0)]
    call = _timed(turns, duration=17.0)
    f = by(audit_call(call, mk_led(call)))["dead_air"]
    assert f.status == "Unknown" and "back to back" in f.reason


def test_two_zero_length_turns_give_unknown():
    """Two zero-length turns (collapsed onto one time) must make dead_air Unknown, with one still trusted.

    If this failed, collapsed timing would Pass, or a single odd turn would wrongly void a good call.
    """
    turns = [("bot", 0.0, 4.0), ("user", 6.0, 6.0), ("bot", 9.0, 9.0), ("user", 12.0, 16.0)]
    call = _timed(turns, duration=17.0)
    assert "zero length" in by(audit_call(call, mk_led(call)))["dead_air"].reason
    one = _timed([("bot", 0.0, 4.0), ("user", 6.0, 6.0), ("bot", 9.0, 13.0), ("user", 15.0, 19.0)], duration=20.0)
    assert by(audit_call(one, mk_led(one)))["dead_air"].status == "Pass"


def test_trusted_timing_with_a_real_buyer_then_bot_gap_stays_nonfatal():
    """Timing that matches the audio and has a 7 s buyer-then-bot silence must still be NonFatal.

    If this failed, the sanity check would hide real dead air and the rule would never fire.
    """
    turns = [("bot", 0.0, 4.0), ("user", 5.0, 9.0), ("bot", 16.0, 20.0), ("user", 21.0, 25.0)]
    call = _timed(turns, duration=26.0)
    call.silences = [Silence(start_sec=9.0, end_sec=16.0)]
    a = audit_call(call, mk_led(call))
    assert by(a)["dead_air"].status == "NonFatal" and "dead_air_review" not in a.flags


def test_clean_call_with_untrusted_timing_keeps_verdict_pass():
    """A clean call with untrusted timing must keep verdict Pass: dead_air Unknown is exempt from NeedsReview.

    If this failed, most calls would flip to NeedsReview, because most transcripts have untrusted timing.
    """
    call = _timed(GOOD, duration=32.0)
    a = audit_call(call, mk_led(call))
    assert by(a)["dead_air"].status == "Unknown" and a.verdict == "Pass"


def test_no_audio_gives_no_dead_air_review_flag():
    """A call with no audio keeps dead_air Unknown ("no audio") and must not get the dead_air_review flag.

    If this failed, the review queue would fill with calls where there is no audio to review.
    """
    call = _timed(GOOD, duration=32.0, audio=None)
    a = audit_call(call, mk_led(call))
    assert by(a)["dead_air"].reason == "no audio to measure silence" and "dead_air_review" not in a.flags


def test_overlapping_turns_are_not_back_to_back_and_timing_stays_trusted():
    """An overlap (a negative gap) must not count as back to back: 20 s audio, buyer 0-12, bot 10-19.

    If this failed, honest overlapping speech would look like made-up edge-to-edge timing, and dead_air would
    flip to Unknown with a review flag on calls whose timing is fine.
    """
    call = _timed([("user", 0.0, 12.0), ("bot", 10.0, 19.0)], duration=20.0)
    assert rules._timing_trusted(call)[0] is True
    a = audit_call(call, mk_led(call))
    assert by(a)["dead_air"].status == "Pass" and "dead_air_review" not in a.flags


def test_turn_ending_after_the_audio_gives_unknown_and_review_flag():
    """A turn ending at 104 s on 20 s audio is impossible: dead_air must be Unknown (unavailable) and flagged.

    If this failed, a transcript whose timestamps run past the audio would Pass a gap check it cannot support.
    """
    call = _timed([("bot", 0.0, 4.0), ("user", 5.0, 9.0), ("bot", 100.0, 104.0)], duration=20.0)
    a = audit_call(call, mk_led(call))
    f = by(a)["dead_air"]
    assert f.status == "Unknown" and f.basis == "unavailable"
    assert "a turn ends at 104.0 s, after the 20.0 s audio" in f.reason
    assert "dead_air_review" in a.flags


def test_turn_ending_before_it_starts_gives_unknown_and_review_flag():
    """A turn whose end is before its start is impossible: dead_air must be Unknown (unavailable) and flagged.

    If this failed, reversed timestamps would be trusted and dead_air would Pass on timing that cannot be read.
    """
    call = _timed([("bot", 0.0, 4.0), ("user", 8.0, 12.0), ("bot", 18.0, 14.0)], duration=16.0)
    a = audit_call(call, mk_led(call))
    f = by(a)["dead_air"]
    assert f.status == "Unknown" and f.basis == "unavailable"
    assert "a turn ends before it starts" in f.reason
    assert "dead_air_review" in a.flags


def test_turn_starting_before_the_audio_gives_unknown_and_review_flag():
    """A turn with a negative start time is impossible: dead_air must be Unknown (unavailable) and flagged.

    If this failed, a turn placed before the audio would hide the real opening silence and dead_air would be graded.
    """
    call = _timed([("user", -8.0, -1.0), ("bot", 6.0, 19.0)], duration=20.0)
    a = audit_call(call, mk_led(call))
    f = by(a)["dead_air"]
    assert f.status == "Unknown" and f.basis == "unavailable"
    assert "a turn starts before the audio does" in f.reason
    assert "dead_air_review" in a.flags


def test_turn_with_a_nan_time_gives_unknown_and_review_flag():
    """A NaN timestamp fails every comparison, so it slips past the other timing checks: it must give Unknown.

    If this failed, a turn with no real time would be trusted and dead_air would be graded on it.
    """
    call = _timed([("user", float("nan"), 4.0), ("bot", 6.0, 19.0)], duration=20.0)
    a = audit_call(call, mk_led(call))
    f = by(a)["dead_air"]
    assert f.status == "Unknown" and f.basis == "unavailable"
    assert "a turn has no finite time" in f.reason
    assert "dead_air_review" in a.flags


# ---- F9
class FakeClient:
    calls = 0
    outputs: list = []

    def __init__(self, **kw):
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self.create))

    def create(self, **kw):
        FakeClient.calls += 1
        out = FakeClient.outputs.pop(0)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=out))])


@pytest.fixture
def fake_llm(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setenv("LLM_BASE_URL", "http://a")
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.delenv("VANILOOP_CACHE", raising=False)
    import openai

    monkeypatch.setattr(openai, "OpenAI", FakeClient)
    FakeClient.calls, FakeClient.outputs = 0, []
    return tmp_path / "cache"


def test_f9_cache_key_includes_endpoint_model_and_tag(fake_llm, monkeypatch):
    """Regression for review round 1 finding: the LLM cache key must include the endpoint, model and cache tag.

    A weak key would serve one provider's answer under another and corrupt the audit labels.
    """
    FakeClient.outputs = ['{"a": 1}'] * 4
    llm.complete_json("s", "u", model="m", cache_tag="1")
    llm.complete_json("s", "u", model="m", cache_tag="1")
    assert FakeClient.calls == 1
    llm.complete_json("s", "u", model="m", cache_tag="2")
    monkeypatch.setenv("LLM_BASE_URL", "http://b")
    llm.complete_json("s", "u", model="m", cache_tag="1")
    llm.complete_json("s", "u", model="m2", cache_tag="1")
    assert FakeClient.calls == 4
    assert ledger.PROMPT_VERSION


def test_f9_invalid_output_retried_and_never_cached(fake_llm):
    """Regression for review round 1 finding: invalid LLM JSON must be retried and never cached.

    The cache must hold only dicts, or a bad answer would replay the broken labels into every future audit run.
    """
    FakeClient.outputs = ["[1, 2]", "not json", '{"a": 1}']
    assert llm.complete_json("s", "u", model="m") == {"a": 1}
    assert FakeClient.calls == 3
    FakeClient.outputs = ['{"a": 1}', '{"ok": true}']
    out = llm.complete_json("s2", "u", model="m", validate=lambda d: "ok" in d)
    assert out == {"ok": True}
    FakeClient.outputs = ["[1]"] * 3
    with pytest.raises(RuntimeError):
        llm.complete_json("s3", "u", model="m")
    assert not list(fake_llm.glob("*.tmp"))
    for p in fake_llm.glob("*.json"):
        assert isinstance(json.loads(p.read_text()), dict)


def test_f9_corrupt_or_invalid_cache_is_miss_and_write_is_atomic(fake_llm, monkeypatch):
    """Regression for review round 1 finding: a corrupt or non-dict cache entry is a miss and writes replace atomically.

    A non-atomic write would leave a half-written cache file that poisons later audit runs.
    """
    FakeClient.outputs = ['{"a": 1}']
    llm.complete_json("s", "u", model="m")
    (p,) = fake_llm.glob("*.json")
    p.write_text("{trunc")
    FakeClient.outputs = ['{"a": 2}']
    assert llm.complete_json("s", "u", model="m") == {"a": 2}
    p.write_text("[1]")
    FakeClient.outputs = ['{"a": 3}']
    assert llm.complete_json("s", "u", model="m") == {"a": 3}
    seen = []
    real = llm.os.replace
    monkeypatch.setattr(llm.os, "replace", lambda a, b: (seen.append((str(a), str(b))), real(a, b))[1])
    FakeClient.outputs = ['{"a": 4}']
    llm.complete_json("s", "u2", model="m")
    assert seen and seen[0][0].endswith(".tmp") and seen[0][1].endswith(".json")


# ---- F10
def test_f10_scientific_notation_in_ffmpeg_output(monkeypatch, tmp_path):
    """Regression for review round 1 finding: ffmpeg silence output in scientific notation must parse.

    A parse miss would drop a real dead-air gap and leave the fault ungraded on the call.
    """
    stderr = (
        "  Duration: 00:00:20.00, start: 0.0\n"
        "[silencedetect @ 0x1] silence_start: 2.26757e-05\n"
        "[silencedetect @ 0x1] silence_end: 6.5 | silence_duration: 6.49998\n"
        "[silencedetect @ 0x1] silence_start: 1.5E+1\n"
    ).replace("\\n", "\n")
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=0, stderr=stderr, stdout="")
    )
    sil = detect_silences(tmp_path / "x.mp3")
    assert len(sil) == 2
    assert sil[0].start_sec == pytest.approx(0.0, abs=1e-4) and sil[0].end_sec == pytest.approx(6.5)
    assert sil[1].start_sec == pytest.approx(15.0) and sil[1].end_sec == pytest.approx(20.0)


# ---- F11
def test_f11_core_rubric_tests_do_not_depend_on_local_data():
    """Regression for review round 1 finding: the core rubric module must run with no local call data.

    A module-level skip would hide rubric regressions on a clean checkout and in CI.
    """
    me = sys.modules[__name__]  # this module has no skipif and builds calls in code
    assert not hasattr(me, "pytestmark")
    assert mk_call().turns and audit_call(*clean()).verdict == "Pass"


# ---- issue #95: align looping, qty_missed and unit aliases with the official matrix
def _loop95(users=(), tones=None, n=12):
    """A 4th name ask at turn 8 (onset). `tones` maps a turn idx to a Gemini tone on that user turn."""
    call = mk_call(n)
    for i, tone in (tones or {}).items():
        call.turns[i].tone = tone
    bots = [bt(0, "quantity")] + [bt(i, "name") for i in (2, 4, 6, 8)]
    return by(audit_call(call, _loop_led(call, bots, users)))["looping"]


def test_95_loop_that_ends_normally_without_irritation_is_nonfatal():
    """A loop whose call closes right after the onset, with a calm buyer, must be NonFatal.

    Matrix: looping is Fatal only if the buyer gets irritated. The old end-of-call trigger graded a normal close
    after a valid answer as Fatal and inflated the Fatal rate against the human audit.
    """
    f = _loop95(n=10)  # the onset is at 8: only turn 9 follows, the call ends
    assert f.status == "NonFatal" and f.onset_turn == 8 and "no sign of irritation" in f.reason


def test_95_loop_followed_by_irritated_buyer_is_fatal():
    """A frustrated label or an irritated tone within the next 2 user turns after the onset must make the loop Fatal.

    Without it a loop that made the buyer angry would be graded like a harmless repeat.
    """
    assert _loop95([UserTurnLabel(idx=9, frustrated=0.6)]).status == "Fatal"  # next user turn, at the threshold
    assert _loop95([UserTurnLabel(idx=11, frustrated=0.9)]).status == "Fatal"  # second user turn after the onset
    assert _loop95(tones={9: "irritated"}).status == "Fatal"  # tone alone, no frustration label
    assert _loop95(tones={11: "irritated"}).status == "Fatal"


def test_95_irritation_outside_the_two_turn_window_or_before_onset_is_nonfatal():
    """Irritation on the third user turn after the onset, or any time before it, must leave the loop NonFatal.

    The matrix ties Fatal to the loop leading to a disconnect: a later or earlier outburst is another fault.
    """
    assert _loop95([UserTurnLabel(idx=13, frustrated=0.9)], n=16).status == "NonFatal"  # third user turn after 8
    assert _loop95([UserTurnLabel(idx=7, frustrated=0.9)], tones={5: "irritated"}).status == "NonFatal"
    assert _loop95([UserTurnLabel(idx=9, frustrated=0.59)], tones={9: "calm"}).status == "NonFatal"


def _missed95(call, bots, users, kept="10", said="10"):
    led = mk_led(
        call,
        bots,
        users,
        buyer_facts={"quantity": said},
        bot_confirmed={"product": "pump", **({"quantity": kept} if kept else {})},
    )
    return by(audit_call(call, led))["qty_missed"]


_GAVE = [UserTurnLabel(idx=1, gave_value_for="quantity")]


def test_95_reask_after_a_correctly_captured_quantity_is_nonfatal():
    """An unanswered re-ask is NonFatal when bot_confirmed already holds the buyer's quantity (a redundant question).

    The lead is right, so grading it Fatal (wrong or missing buyer info) overstates the fault. A unit-converted
    match counts too, and a different captured value must stay Fatal.
    """
    call = mk_call(12)
    bots = [bt(0, "quantity"), bt(2, "quantity")]
    f = _missed95(call, bots, _GAVE)
    assert f.status == "NonFatal" and f.onset_turn == 2
    assert _missed95(call, bots, _GAVE, said="2 kg", kept="2000 g").status == "NonFatal"
    assert _missed95(call, bots, _GAVE, kept="50").status == "Fatal"


def test_95_unanswered_reask_on_unclear_audio_quantity_turn_is_nonfatal():
    """An unanswered re-ask is NonFatal when the buyer turn that gave the quantity has audio_unclear set.

    Matrix: "if voice not clear then Non Fatal". Here nothing was captured, so only the audio flag can excuse it.
    """
    call = mk_call(12)
    bots = [bt(0, "quantity"), bt(2, "quantity")]
    assert _missed95(call, bots, _GAVE, kept=None).status == "Fatal"  # same call without the flag
    call.turns[1].audio_unclear = True
    f = _missed95(call, bots, _GAVE, kept=None)
    assert f.status == "NonFatal" and f.onset_turn == 2 and "unclear" in f.reason
    call.turns[1].audio_unclear = False
    assert _missed95(call, bots, _GAVE, kept=None).status == "Fatal"


def test_95_unclear_audio_uses_the_latest_quantity_turn_before_the_reask():
    """An unanswered re-ask is excused only by unclear audio on the buyer's latest quantity turn before it.

    The rule must read the quantity turn the re-ask actually follows, not the first one in the segment. Reading the
    earliest turn inverts both orders. An unclear first answer with a clear repeat must stay Fatal because the re-ask
    follows the clear turn; the earliest-turn check excused it as NonFatal. A clear first answer with an unclear
    repeat must be NonFatal because the re-ask follows the unclear turn; the earliest-turn check graded it Fatal.
    """
    bots = [bt(0, "quantity"), bt(4, "quantity")]
    users = [UserTurnLabel(idx=1, gave_value_for="quantity"), UserTurnLabel(idx=3, gave_value_for="quantity")]
    # unclear earliest answer, clear repeat: the re-ask follows the clear turn -> Fatal, no captured quantity
    call = mk_call(12)
    call.turns[1].audio_unclear = True
    f = _missed95(call, bots, users, kept=None)
    assert f.status == "Fatal" and f.onset_turn == 4
    # clear earliest answer, unclear repeat: the re-ask follows the unclear turn -> NonFatal
    call2 = mk_call(12)
    call2.turns[3].audio_unclear = True
    f2 = _missed95(call2, bots, users, kept=None)
    assert f2.status == "NonFatal" and "unclear" in f2.reason


def test_95_qty_captured_compares_converted_values_with_a_tolerance():
    """A captured quantity that matches after unit conversion must count as captured despite float rounding.

    "0.7 kg" converted into grams is 700.0000000000001, so exact equality called a correct capture missing and
    graded an unanswered re-ask Fatal. The lead is right, so the fault is at most NonFatal.
    """
    call = mk_call(12)
    bots = [bt(0, "quantity"), bt(2, "quantity")]
    f = _missed95(call, bots, _GAVE, said="0.7 kg", kept="700 g")
    assert f.status == "NonFatal" and f.onset_turn == 2


def test_95_unanswered_reask_with_no_captured_quantity_stays_fatal():
    """With no captured quantity, no unclear audio and an unanswered re-ask, qty_missed must stay Fatal.

    This is the real fault the rule exists for: the buyer answered and the lead likely has no quantity.
    """
    f = _missed95(mk_call(12), [bt(0, "quantity"), bt(2, "quantity")], _GAVE, kept=None)
    assert f.status == "Fatal" and f.onset_turn == 2


def test_95_set_is_not_an_alias_of_piece():
    """ "10 sets" against "10 pieces" must not be read as the same unit: "set" is no longer a piece alias.

    A set can hold several pieces, so the old alias passed a possible quantity error without comment. An unknown
    unit word now takes the existing no-unit path, which says that only one side states a unit.
    """
    assert rules.norm_unit("sets") is None and rules.norm_unit("set") is None
    assert rules.norm_unit("pieces") == "piece"
    call = mk_call(12)
    led = mk_led(
        call, [bt(0, "quantity")], buyer_facts={"quantity": "10 sets"}, bot_confirmed={"quantity": "10 pieces"}
    )
    f = by(audit_call(call, led))["qty_unit"]
    assert "only one side states a unit" in f.reason and "same kind" not in f.reason


def test_95_qty_missed_only_judges_reasks_in_the_final_product_segment():
    """Only re-asks in the FINAL product segment count, because the posted lead carries the final product.

    Review round 2: an earlier product's re-ask was judged Fatal and the final product's quantity was mixed into it.
    Turn 1 gives 10 pumps, the bot re-asks at turn 2, turn 3 switches to valves, turn 7 gives 20 valves with unclear
    audio, and the bot re-asks at turn 8. Turn 8 must be judged NonFatal on the unclear turn 7. A Fatal at turn 2
    would grade the pump ask with the valve quantity and inflate the Fatal rate.
    """
    call = mk_call(12)
    call.turns[7].audio_unclear = True
    led = mk_led(
        call,
        [bt(0, "quantity"), bt(2, "quantity"), bt(8, "quantity")],
        [UserTurnLabel(idx=1, gave_value_for="quantity"), UserTurnLabel(idx=7, gave_value_for="quantity")],
        buyer_facts={"quantity": "20"},
        bot_confirmed={"product": "valve"},
        product_changed_at_turn=3,
        final_product="valve",
    )
    f = by(audit_call(call, led))["qty_missed"]
    assert f.status == "NonFatal" and f.onset_turn == 8 and "unclear" in f.reason


def test_95_qty_captured_rejects_an_unrecognised_unit_word():
    """ "10 sets" against "10 pieces" is not a captured match: an unrecognised unit word must not read as no unit.

    Review round 2: "sets" parsed as no unit, the numbers matched, and an unanswered re-ask graded NonFatal. A set
    can hold several pieces, so a possible quantity error would ship on the lead. With the fix it is Fatal.
    """
    call = mk_call(12)
    bots = [bt(0, "quantity"), bt(2, "quantity")]
    f = _missed95(call, bots, _GAVE, said="10 sets", kept="10 pieces")
    assert f.status == "Fatal" and f.onset_turn == 2


@pytest.mark.parametrize("said", ["10 ka", "10 chahiye", "10 hi", "10 toh chahiye", "10 bhi"])
def test_95_qty_captured_treats_a_hinglish_filler_as_no_unit(said):
    """A filler word after the number ("10 ka", "10 chahiye") is not a unit, so it matches a captured "10".

    Review round 3: any word after the number counted as an unknown unit, so a matching quantity graded Fatal on an
    unanswered re-ask. Hinglish answers often carry such fillers, so this would inflate Fatal on real calls.
    """
    call = mk_call(12)
    bots = [bt(0, "quantity"), bt(2, "quantity")]
    f = _missed95(call, bots, _GAVE, said=said, kept="10")
    assert f.status == "NonFatal" and f.onset_turn == 2


@pytest.mark.parametrize("said", ["10 boxes", "10 packets", "10 pairs", "10 yards", "10 millimeters"])
def test_95_qty_captured_rejects_other_unknown_units(said):
    """A unit the parser does not know (a packing unit, or a length such as yards) must not equal 10 pieces.

    Review round 4: a packing-unit allowlist let "10 yards" match "10 pieces". Only listed fillers may read as no
    unit, so an unknown unit word keeps the safe answer, Fatal.
    """
    call = mk_call(12)
    bots = [bt(0, "quantity"), bt(2, "quantity")]
    f = _missed95(call, bots, _GAVE, said=said, kept="10 pieces")
    assert f.status == "Fatal" and f.onset_turn == 2


def test_95_qty_captured_keeps_a_bare_number_matching_a_stated_unit():
    """Guard: a bare "10" against "10 pieces" keeps today's NonFatal, because a bare number is still "no unit".

    The unit-word fix must not turn every one-sided unit into a capture fault, or calls where the buyer gave only a
    number and the bot recorded the category's default unit would be regraded Fatal.
    """
    call = mk_call(12)
    bots = [bt(0, "quantity"), bt(2, "quantity")]
    f = _missed95(call, bots, _GAVE, said="10", kept="10 pieces")
    assert f.status == "NonFatal" and f.onset_turn == 2
