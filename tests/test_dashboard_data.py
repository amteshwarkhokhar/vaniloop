from pathlib import Path

from vaniloop.dashboard import load_audits, load_calls, load_causes
from vaniloop.models import AuditRecord, CallRecord, Cause

FIX = Path(__file__).parent / "fixtures"


def test_load_audits():
    a = load_audits(FIX)
    assert len(a) == 40 and all(isinstance(x, AuditRecord) for x in a)
    counts = {v: sum(x.verdict == v for x in a) for v in ("Pass", "Fatal", "NonFatal", "NeedsReview")}
    assert counts == {"Pass": 15, "Fatal": 10, "NonFatal": 10, "NeedsReview": 5}


def test_load_causes():
    c = load_causes(FIX)
    assert len(c) == 5 and all(isinstance(x, Cause) for x in c)
    assert c[0].cause_id == "C-01"


def test_load_calls():
    c = load_calls(FIX)
    assert len(c) == 40 and all(isinstance(v, CallRecord) for v in c.values())
    assert "syn_001" in c
