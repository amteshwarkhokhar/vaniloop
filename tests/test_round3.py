"""Review round 3 regression tests."""

import json

import pytest

from test_findings import mk_call, mk_led
from test_ingest import synthetic_transcripts
from vaniloop.ingest import build_call_records
from vaniloop.rules import rule_qty_accuracy, unit_group, to_base


def _acc(buyer, bot):
    call = mk_call(12)
    led = mk_led(call, buyer_facts=buyer, bot_confirmed={"product": "pump", **bot})
    return rule_qty_accuracy(call, led).status


# ---- 1 torn last line on resume
def test_r3_1_resume_after_torn_last_line_keeps_new_record(tmp_path):
    out = tmp_path / "calls.jsonl"
    src = synthetic_transcripts(tmp_path / "t")
    build_call_records(src, tmp_path, out)
    lines = out.read_text().splitlines()
    assert len(lines) == 3
    out.write_text("\n".join(lines[:2]) + "\n" + lines[2][:40])  # torn third record, no newline
    recs = build_call_records(src, tmp_path, out)
    assert len(recs) == 3
    good = [ln for ln in out.read_text().splitlines() if ln.strip().endswith("}")]
    parsed = [json.loads(ln)["call_id"] for ln in good]
    assert len(set(parsed)) == 3


def test_r3_1_append_jsonl_starts_fresh_line(tmp_path):
    from vaniloop.jsonl import append_jsonl, read_jsonl
    p = tmp_path / "x.jsonl"
    p.write_text('{"a": 1}\n{"b"')
    append_jsonl(p, [{"c": 3}, {"d": 4}])
    with pytest.warns(UserWarning):
        assert read_jsonl(p) == [{"a": 1}, {"c": 3}, {"d": 4}]


# ---- 2 separate quantity and unit fields
def test_r3_2_separate_unit_fields_mismatch_is_fatal():
    assert _acc({"quantity": "2", "unit": "kg"}, {"quantity": "2", "unit": "g"}) == "Fatal"


def test_r3_2_separate_unit_fields_convert_to_pass():
    assert _acc({"quantity": "2", "unit": "kg"}, {"quantity": "2000", "unit": "g"}) == "Pass"


# ---- 3 tolerance applies in the buyer's unit
def test_r3_3_minor_band_in_buyer_unit():
    assert _acc({"quantity": "100 kg"}, {"quantity": "110 kg"}) == "NonFatal"
    assert _acc({"quantity": "100 kg"}, {"quantity": "150 kg"}) == "Fatal"
    assert _acc({"quantity": "100 kg"}, {"quantity": "110000 g"}) == "NonFatal"


# ---- pair is 2 count units
def test_r3_4_pair_is_two_units():
    assert unit_group("pair") == "count" and to_base(1, "pair") == 2
    assert _acc({"quantity": "4 piece"}, {"quantity": "2 pair"}) == "Pass"
