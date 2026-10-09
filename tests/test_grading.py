"""Tests for blind hand-grading: sample spread, grade storage, reveal and agreement numbers."""

import json
from pathlib import Path

from vaniloop import dashboard
from vaniloop.grading import (
    agreement,
    grade_sample,
    load_grades,
    reveal,
    save_grade,
)
from vaniloop.models import AuditRecord, Finding

FIX = Path(__file__).parent / "fixtures"


def mk_audit(call_id: str, verdict: str, params: tuple[str, ...] = (), status: str = "Fatal") -> AuditRecord:
    findings = [Finding(parameter=p, status=status, reason="synthetic", basis="observed") for p in params]
    return AuditRecord(call_id=call_id, verdict=verdict, scope="bl_collection", findings=findings)


def test_grade_sample_spread_determinism_and_overflow():
    """grade_sample must spread picks across verdicts, repeat for the same seed, and cap at the data size.

    A non-deterministic or verdict-skewed sample would make calibration runs incomparable over time.
    """
    audits = (
        [mk_audit(f"f{i:02d}", "Fatal", ("looping",)) for i in range(5)]
        + [mk_audit(f"n{i:02d}", "NonFatal", ("dead_air",)) for i in range(5)]
        + [mk_audit(f"r{i:02d}", "NeedsReview") for i in range(3)]
        + [mk_audit(f"p{i:02d}", "Pass") for i in range(7)]
    )
    first = grade_sample(audits, n=8, seed=0)
    assert first == grade_sample(audits, n=8, seed=0)
    assert len(first) == 8 and len(set(first)) == 8
    verdict_by_id = {a.call_id: a.verdict for a in audits}
    assert {verdict_by_id[cid] for cid in first} == {"Fatal", "NonFatal", "NeedsReview", "Pass"}
    assert first[0][0] == "f" and first[1][0] == "n" and first[2][0] == "r" and first[3][0] == "p"
    overflow = grade_sample(audits, n=99)
    assert len(overflow) == len(audits) and set(overflow) == set(verdict_by_id)


def test_save_and_load_last_wins(tmp_path):
    """save_grade must append a timestamped row and load_grades must keep the newest row per call.

    A last-first bug would show a stale hand grade and corrupt every agreement number after a re-grade.
    """
    path = tmp_path / "hand_grades.jsonl"
    save_grade(path, {"call_id": "a", "verdict": "Pass", "parameters": [], "first_fault_turn": None, "note": "one"})
    save_grade(
        path,
        {"call_id": "a", "verdict": "Fatal", "parameters": ["looping"], "first_fault_turn": 2, "note": "two"},
    )
    save_grade(path, {"call_id": "b", "verdict": "NonFatal", "parameters": ["dead_air"], "note": "b"})
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    assert len(rows) == 3 and all(r["ts"] for r in rows)
    grades = load_grades(path)
    assert set(grades) == {"a", "b"}
    assert grades["a"]["verdict"] == "Fatal" and grades["a"]["note"] == "two"
    assert grades["b"]["parameters"] == ["dead_air"]


def test_reveal_hidden_before_and_shown_after():
    """reveal must return None until a grade exists, then hand, audit and the match fields.

    If reveal leaked before a grade, the owner would see the audit answer and grade blind no more.
    """
    audits = [mk_audit("a1", "Fatal", ("looping", "dead_air"))]
    assert reveal("a1", {}, audits) is None
    grades = {"a1": {"call_id": "a1", "verdict": "Fatal", "parameters": ["looping"], "note": ""}}
    rev = reveal("a1", grades, audits)
    assert rev is not None
    assert rev["hand"]["verdict"] == "Fatal"
    assert rev["audit"].verdict == "Fatal"
    assert rev["verdict_match"] is True
    assert rev["param_overlap"] == ["looping"]
    mismatch = reveal("a1", {"a1": {"verdict": "Pass", "parameters": []}}, audits)
    assert mismatch["verdict_match"] is False and mismatch["param_overlap"] == []


def test_agreement_counts_and_mismatch_list():
    """agreement must report the graded count, verdict rate, per-parameter rate and mismatch list.

    A wrong count or rate would tell the owner the audit agrees with him when it does not.
    """
    audits = [
        mk_audit("a1", "Pass"),
        mk_audit("a2", "Fatal", ("looping",)),
        mk_audit("a3", "NonFatal", ("dead_air", "looping"), status="NonFatal"),
        mk_audit("a4", "Pass"),
    ]
    grades = {
        "a1": {"verdict": "Pass", "parameters": []},
        "a2": {"verdict": "Fatal", "parameters": ["looping"]},
        "a3": {"verdict": "Fatal", "parameters": ["dead_air"]},
        "a4": {"verdict": "Pass", "parameters": ["dead_air"]},
    }
    agree = agreement(grades, audits)
    assert agree["graded"] == 4
    assert agree["verdict_agree"] == 3
    assert agree["verdict_rate"] == 0.75
    assert agree["per_parameter"]["looping"] == {"agree": 3, "rate": 0.75}
    assert agree["per_parameter"]["dead_air"] == {"agree": 3, "rate": 0.75}
    assert {m["call_id"] for m in agree["mismatches"]} == {"a3", "a4"}
    a3 = next(m for m in agree["mismatches"] if m["call_id"] == "a3")
    assert a3 == {"call_id": "a3", "hand": "Fatal", "audit": "NonFatal"}


def test_dashboard_grade_helpers_on_fixtures():
    """The Grade view helpers must pick the fallback sample and build transcript rows on fixtures.

    A broken helper would leave the Grade view empty or hide the tone and overlap signals.
    """
    audits = dashboard.load_audits(FIX)
    sample = dashboard.load_grade_sample(FIX, audits)
    assert len(sample) == 20 and sample[0] == "syn_001"
    calls = dashboard.load_calls(FIX)
    frame = dashboard.transcript_frame(calls["syn_001"])
    assert list(frame.columns) == ["idx", "speaker", "start_sec", "text", "tone", "overlap"]
    assert len(frame) == 6
    assert frame.iloc[0]["speaker"] == "bot" and frame.iloc[0]["tone"] is None
