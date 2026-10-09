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
            bt(2, "spec", fits_confirmed_product=False, spec_name="x"),
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


def test_f6_call_ending_after_fourth_ask_is_fatal_and_worst_slot_reported():
    """Regression for review round 1 finding: a fourth repeat near the call end is Fatal and the worst slot is reported.

    Reporting the wrong slot would point the prompt fix at a question that was not the loop.
    """
    call = mk_call(12)  # turns 0..11
    bots = [bt(0, "quantity")] + [bt(i, "name") for i in (2, 4, 6, 8)] + [bt(10, "city")]
    f = by(audit_call(call, _loop_led(call, bots)))["looping"]  # 4th name ask at 8, 3 turns left (9,10,11)
    assert f.status == "NonFatal"
    call2 = mk_call(10)  # 0..9
    bots2 = [bt(0, "city"), bt(2, "city"), bt(4, "city"), bt(6, "city"), bt(8, "city")]  # 4th at 6, turns 7,8,9 after
    assert by(audit_call(call2, _loop_led(call2, bots2)))["looping"].status == "NonFatal"
    call3 = mk_call(9)  # 0..8; 4th at 6, turns 7,8 after -> within 2
    assert by(audit_call(call3, _loop_led(call3, bots2)))["looping"].status == "Fatal"
    # two looping slots: worst (Fatal one) is reported
    call4 = mk_call(30)
    bots4 = [bt(0, "quantity")] + [bt(i, "city") for i in (2, 4, 6, 8)] + [bt(i, "name") for i in (10, 12, 14, 16, 18)]
    users = [UserTurnLabel(idx=17, frustrated=0.9)]
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
        bt(4, "spec", spec_name="b", fits_confirmed_product=False),
        bt(6, "spec", spec_name="c", fits_confirmed_product=False),
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

    A changed constant would regrade every past call and break the stated no-behaviour-change promise.
    """
    assert rules.DEAD_AIR_NOISY_SEC == 8.0 and rules.DEAD_AIR_SEC == 5.0
    call = mk_call(audio="x.mp3", silences=[Silence(start_sec=1, end_sec=7)])  # 6 s: fatal-free NonFatal at 5 s
    assert by(audit_call(call, mk_led(call, [bt(0, "quantity")])))["dead_air"].status == "NonFatal"


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
