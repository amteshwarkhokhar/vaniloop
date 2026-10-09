from pathlib import Path

from vaniloop.dashboard import load_audits, load_calls, load_causes
from vaniloop.models import AuditRecord, CallRecord, Cause

FIX = Path(__file__).parent / "fixtures"


def test_load_audits():
    """The dashboard must load the synthetic audits fixture into AuditRecord objects with exact verdict counts.

    A broken reader would show an auditor wrong Pass and Fatal totals over the fixture calls.
    """
    a = load_audits(FIX)
    assert len(a) == 40 and all(isinstance(x, AuditRecord) for x in a)
    counts = {v: sum(x.verdict == v for x in a) for v in ("Pass", "Fatal", "NonFatal", "NeedsReview")}
    assert counts == {"Pass": 15, "Fatal": 10, "NonFatal": 10, "NeedsReview": 5}


def test_load_causes():
    """The dashboard must load the synthetic causes fixture into ranked Cause objects in file order.

    A broken reader would hide the top-ranked cause from the person fixing the prompt.
    """
    c = load_causes(FIX)
    assert len(c) == 5 and all(isinstance(x, Cause) for x in c)
    assert c[0].cause_id == "C-01"


def test_load_calls():
    """The dashboard must index the synthetic calls fixture by call_id as CallRecord objects.

    A broken reader would make a real call impossible to open in the audit view.
    """
    c = load_calls(FIX)
    assert len(c) == 40 and all(isinstance(v, CallRecord) for v in c.values())
    assert "syn_001" in c
