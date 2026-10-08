"""Review round 2 regression tests."""

import fcntl
import json
import warnings

import pytest

from test_findings import bt, by, mk_call, mk_led
from vaniloop import ledger, pipeline
from vaniloop.issue import build_causes, run_issues, wilson
from vaniloop.jsonl import read_jsonl
from vaniloop.models import AuditRecord, BotTurnLabel, CallRecord, Finding, Turn, UserTurnLabel
from vaniloop.rules import audit_call, parse_quantity, parse_quantity_full


# ---- 1 coverage
def _partial(drop_users=(), drop_bots=()):
    call = mk_call(12)
    led = mk_led(call, [bt(0, "quantity")])
    led.user_turns = [u for u in led.user_turns if u.idx not in drop_users]
    led.bot_turns = [b for b in led.bot_turns if b.idx not in drop_bots]
    return call, led


def test_r2_1_partial_coverage_never_passes():
    call, led = _partial(drop_users=(11,))  # 11/12 labelled, 91%
    a = audit_call(call, led)
    assert a.verdict == "NeedsReview" and a.review_reason == "unlabelled turns: [11]"
    call, led = _partial(drop_bots=(10,))
    assert audit_call(call, led).review_reason == "unlabelled turns: [10]"


def test_r2_1_partial_coverage_keeps_found_fault():
    call, led = _partial(drop_users=(11,))
    led.bot_confirmed = {"product": "pump", "quantity": "50"}
    assert audit_call(call, led).verdict == "Fatal"


def test_r2_1_below_80_still_needs_review_and_full_passes():
    call = mk_call(12)
    led = mk_led(call, [bt(0, "quantity")])
    assert audit_call(call, led).verdict == "Pass"
    led.user_turns = led.user_turns[:2]
    assert "incomplete ledger" in audit_call(call, led).review_reason


# ---- 2 lock and duplicate inputs
def _calls_file(tmp_path, ids):
    p = tmp_path / "calls.jsonl"
    rows = []
    for i, cid in enumerate(ids):
        c = mk_call(12)
        c.call_id = cid
        c.language = f"l{i}"  # tells the duplicates apart
        rows.append(c.model_dump_json())
    p.write_text("\n".join(rows) + "\n")
    return p


FAKE = {"scope": "bl_collection", "initial_product": "x", "final_product": "x", "product_changed_at_turn": None,
        "buyer_facts": {}, "bot_confirmed": {}, "bot_turns": [], "user_turns": []}


def _paths(tmp_path, calls):
    return dict(calls_path=calls, ledgers_path=tmp_path / "l.jsonl", audits_path=tmp_path / "a.jsonl",
                errors_path=tmp_path / "e.jsonl")


def test_r2_2_duplicate_input_calls_processed_once_first_wins(tmp_path, monkeypatch):
    seen = []

    def fake(system, user, *a, **k):
        seen.append(user)
        return dict(FAKE)

    monkeypatch.setattr(ledger, "complete_json", fake)
    s = pipeline.run_audit(**_paths(tmp_path, _calls_file(tmp_path, ["a", "b", "a"])), workers=1)
    assert s["processed"] == 2 and len(read_jsonl(tmp_path / "a.jsonl")) == 2


def test_r2_2_lock_held_exits(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "complete_json", lambda *a, **k: dict(FAKE))
    with (tmp_path / ".audit.lock").open("a") as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SystemExit) as e:
            pipeline.run_audit(**_paths(tmp_path, _calls_file(tmp_path, ["a"])))
    assert str(e.value) == "another audit run is active"
    assert pipeline.run_audit(**_paths(tmp_path, _calls_file(tmp_path, ["a"])))["processed"] == 1  # lock released


# ---- 3 tolerant reads and resume
def test_r2_3_read_jsonl_skips_bad_lines_and_warns_once(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text('{"call_id": "a"}\n{"call_id": "b"\n\nnot json\n{"call_id": "c"}\n')
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        rows = read_jsonl(p)
    assert [r["call_id"] for r in rows] == ["a", "c"]
    assert len(w) == 1 and "2" in str(w[0].message)
    assert read_jsonl(tmp_path / "missing.jsonl") == []


def test_r2_3_resume_after_torn_write_and_done_means_audit_row(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "complete_json", lambda *a, **k: dict(FAKE))
    calls = _calls_file(tmp_path, ["a", "b"])
    pth = _paths(tmp_path, calls)
    # "a" has a ledger row but no audit row; audits file ends in a torn line without newline
    (tmp_path / "l.jsonl").write_text(json.dumps({"call_id": "a", "scope": "unclear"}) + "\n")
    (tmp_path / "a.jsonl").write_text('{"call_id": "b", "verd')
    s = pipeline.run_audit(**pth, workers=1)
    assert s["processed"] == 2 and s["skipped"] == 0
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert sorted(r["call_id"] for r in read_jsonl(tmp_path / "a.jsonl")) == ["a", "b"]
    assert pipeline.run_audit(**pth)["processed"] == 0


def test_r2_3_issue_dedupes_audits_last_wins(tmp_path):
    p = tmp_path / "a.jsonl"
    old = AuditRecord(call_id="a", verdict="Fatal", scope="bl_collection",
                      findings=[Finding(parameter="looping", status="Fatal", reason="r", basis="observed")])
    new = AuditRecord(call_id="a", verdict="Pass", scope="bl_collection", findings=[])
    p.write_text(old.model_dump_json() + "\n{torn\n" + new.model_dump_json() + "\n")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert run_issues(p, tmp_path / "c.json") == []


# ---- 4 quantities
def test_r2_4_parse_quantity_full():
    assert parse_quantity_full("2-3") == (2, 3, None)
    assert parse_quantity_full("2 se 3") == (2, 3, None)
    assert parse_quantity_full("2 to 3 kg") == (2, 3, "kg")
    assert parse_quantity_full("5 kilo") == (5, 5, "kg")
    assert parse_quantity_full("do sau pieces") == (200, 200, "piece")
    assert parse_quantity_full("kuch") is None
    assert parse_quantity("2-3") == 2 and parse_quantity("2 se 3") == 2


def _q(said, kept, bot_unit=None):
    call = mk_call()
    led = mk_led(call, [bt(0, "quantity")], buyer_facts={"quantity": said}, bot_confirmed={"product": "pump", "quantity": kept})
    return by(audit_call(call, led))


def test_r2_4_range_collapsed_and_outside():
    assert _q("2-3", "2")["qty_accuracy"].status == "NonFatal"
    assert "range collapsed" in _q("10 se 20", "15")["qty_accuracy"].reason
    assert _q("2 to 3", "3")["qty_accuracy"].status == "NonFatal"
    assert _q("100-110", "105")["qty_accuracy"].status == "NonFatal"
    assert _q("100-110", "120")["qty_accuracy"].status == "NonFatal"  # nearest 110: diff 10, 9%
    assert _q("100-110", "200")["qty_accuracy"].status == "Fatal"
    assert _q("10-20", "10-20")["qty_accuracy"].status == "Pass"


def test_r2_4_units():
    assert _q("5 kg", "5 pieces")["qty_unit"].status == "Fatal"
    assert _q("5 kg", "5 pieces")["qty_accuracy"].status != "Pass"
    f = _q("2 kg", "2000 g")
    assert f["qty_unit"].status == "Pass" and f["qty_accuracy"].status == "Pass"
    assert _q("1 quintal", "100 kg")["qty_accuracy"].status == "Pass"
    assert _q("2 dozen", "24 pieces")["qty_accuracy"].status == "Pass"
    assert _q("2 kg", "3000 g")["qty_accuracy"].status == "Fatal"
    f = _q("5 kg", "5")
    assert f["qty_unit"].status == "Pass" and f["qty_unit"].basis == "spoken_proxy"
    assert _q("5", "5")["qty_unit"].status == "NotApplicable"
    assert _q("5 kg", "5 pieces")["qty_unit"].parameter == "qty_unit"


def test_r2_4_qty_unit_verdict_and_issue_name():
    call = mk_call()
    led = mk_led(call, [bt(0, "quantity")], buyer_facts={"quantity": "5 kg"}, bot_confirmed={"product": "pump", "quantity": "5 pieces"})
    a = audit_call(call, led)
    assert a.verdict == "Fatal" and "wrong_requirement" in a.flags
    from vaniloop.issue import KNOWN
    assert KNOWN["qty_unit"][0] == "Quantity unit captured wrong"


# ---- 5 loop onset
def test_r2_5_onset_is_earliest_crossing_across_slots():
    call = mk_call(30)
    bots = [bt(0, "quantity")] + [bt(i, "city") for i in (2, 4, 6, 8)] + [bt(i, "name") for i in (10, 12, 14, 16, 18)]
    users = [UserTurnLabel(idx=17, frustrated=0.9)]
    f = by(audit_call(call, mk_led(call, bots, users)))["looping"]
    assert "name" in f.reason and f.status == "Fatal"  # worst slot reported
    assert f.onset_turn == 8  # but onset is the earliest crossing (city's 4th ask)


# ---- 6, 7 issue
def aud(cid, verdict="Fatal", **st):
    fs = [Finding(parameter=p, status=s, reason="r", basis="observed") for p, s in st.items()]
    return AuditRecord(call_id=cid, verdict=verdict, scope="bl_collection", findings=fs)


def test_r2_6_causes_object_and_dedupe(tmp_path):
    p = tmp_path / "a.jsonl"
    rows = [aud("a", looping="Fatal"), aud("a", looping="Fatal"), aud("b", "Pass"), aud("c", "NeedsReview")]
    p.write_text("\n".join(r.model_dump_json() for r in rows) + "\n")
    run_issues(p, tmp_path / "c.json")
    doc = json.loads((tmp_path / "c.json").read_text())
    assert (doc["denominator"], doc["audited_total"], doc["needs_review"]) == (2, 3, 1)
    assert len(doc["causes"]) == 1 and doc["causes"][0]["fatal_calls"] == ["a"]
    assert not list(tmp_path.glob("*.tmp"))


def test_r2_7_rank_score_uses_wilson_low():
    # 2 fatal of 2 vs 1 fatal... small n must rank below larger evidence with equal severity
    a = [aud("a", looping="Fatal"), aud("b", looping="Fatal"), aud("c", looping="Fatal"), aud("d", looping="Fatal"),
         aud("e", looping="Fatal")] + [aud(f"p{i}", "Pass") for i in range(95)]
    cs = {c.parameter: c for c in build_causes(a)}
    c = cs["looping"]
    assert c.rank_score == pytest.approx(wilson(5, 100)[0] * 5.0 * 0.5)
    assert c.priority == 5 * 5 * 0.5


def test_r2_7_sort_by_rank_score_not_priority():
    # X: 3 fatal flow_logic-free prompt cause; Y: 10 nonfatal. Same n. priority X=15 > Y=10, but check rank order is by rank_score
    a = [aud(f"x{i}", "Fatal", qty_probing="Fatal") for i in range(3)] + \
        [aud(f"y{i}", "NonFatal", qty_missed="NonFatal") for i in range(12)] + [aud(f"p{i}", "Pass") for i in range(5)]
    cs = build_causes(a)
    assert [c.parameter for c in cs] == ["qty_missed", "qty_probing"]
    assert cs[0].priority < cs[1].priority and cs[0].rank_score > cs[1].rank_score


# ---- 8 dashboard
def test_r2_8_dashboard_reads_new_and_old_and_survives_malformed(tmp_path, monkeypatch):
    from vaniloop import dashboard
    warned = []
    monkeypatch.setattr(dashboard.st, "warning", lambda m, *a, **k: warned.append(m))
    c = build_causes([aud(f"a{i}", looping="Fatal") for i in range(3)])
    (tmp_path / "causes.json").write_text(json.dumps({"denominator": 7, "audited_total": 9, "needs_review": 2,
                                                       "causes": [x.model_dump() for x in c]}))
    causes, meta = dashboard.load_causes_doc(tmp_path)
    assert len(causes) == 1 and meta["denominator"] == 7 and not warned
    (tmp_path / "causes.json").write_text(json.dumps([x.model_dump() for x in c]))
    assert len(dashboard.load_causes(tmp_path)) == 1
    (tmp_path / "causes.json").write_text('{"denominator": 7, "cau')
    assert dashboard.load_causes(tmp_path) == [] and len(warned) == 1
    (tmp_path / "audits.jsonl").write_text(aud("a", "Pass").model_dump_json() + "\n{torn")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert len(dashboard.load_audits(tmp_path)) == 1
