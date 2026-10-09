"""Blind hand-grading: pick a sample, store the owner's grade, compare it with the audit.

Pure functions, no streamlit import: the dashboard calls them and the tests call them
directly. The audit verdict stays hidden until a grade for that call exists, so the
owner grades blind.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime
from pathlib import Path

from .jsonl import append_jsonl, read_jsonl
from .models import AuditRecord

VERDICT_ORDER = ("Fatal", "NonFatal", "NeedsReview", "Pass")
FAILING = ("Fatal", "NonFatal")
HAND_VERDICTS = ("Pass", "NonFatal", "Fatal")

GRADES_NAME = "hand_grades.jsonl"


def grade_sample(audits: list[AuditRecord], n: int = 20, seed: int = 0) -> list[str]:
    """Pick n call ids spread across the four verdict groups, round-robin, deterministically.

    Each group is sorted by call_id then shuffled with random.Random(seed). Taking one
    from Fatal, NonFatal, NeedsReview, Pass in turn keeps a sample balanced even when the
    audit is mostly Pass or mostly Fatal.
    """
    rng = random.Random(seed)
    groups: list[list[str]] = []
    for verdict in VERDICT_ORDER:
        ids = sorted(a.call_id for a in audits if a.verdict == verdict)
        rng.shuffle(ids)
        groups.append(ids)
    out: list[str] = []
    i = 0
    while len(out) < n:
        before = len(out)
        for group in groups:
            if i < len(group):
                out.append(group[i])
                if len(out) == n:
                    break
        if len(out) == before:  # every group exhausted
            break
        i += 1
    return out


def save_grade(path: str | Path, row: dict) -> dict:
    """Append one hand grade as JSONL, stamping the save time in ts. Returns the stored row."""
    stored = dict(row)
    stored["ts"] = datetime.now(UTC).isoformat(timespec="seconds")
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    append_jsonl(p, [stored])
    return stored


def load_grades(path: str | Path) -> dict[str, dict]:
    """Index hand grades by call_id, last row wins after a re-grade."""
    return {r["call_id"]: r for r in read_jsonl(path) if "call_id" in r}


def failing_parameters(audit: AuditRecord) -> list[str]:
    """Parameters the audit marked Fatal or NonFatal, sorted."""
    return sorted(f.parameter for f in audit.findings if f.status in FAILING)


def known_parameters(audits: list[AuditRecord]) -> list[str]:
    """Every rule parameter that appears in any audit finding, sorted."""
    return sorted({f.parameter for a in audits for f in a.findings})


def reveal(call_id: str, grades: dict[str, dict], audits: list[AuditRecord]) -> dict | None:
    """Side-by-side hand vs audit for one call. None while the call has no saved grade.

    Returning None is what keeps the audit hidden: the dashboard has nothing to draw.
    """
    grade = grades.get(call_id)
    if grade is None:
        return None
    audit = next((a for a in audits if a.call_id == call_id), None)
    hand_params = set(grade.get("parameters") or [])
    audit_params = set(failing_parameters(audit)) if audit else set()
    return {
        "hand": grade,
        "audit": audit,
        "verdict_match": bool(audit is not None and grade.get("verdict") == audit.verdict),
        "param_overlap": sorted(hand_params & audit_params),
    }


def agreement(grades: dict[str, dict], audits: list[AuditRecord]) -> dict:
    """Calibration numbers over every graded call that has an audit.

    Returns graded count, verdict agreement count and rate, per-parameter agreement
    (a parameter agrees when both sides list it or neither does) and the mismatches
    whose hand verdict, audit verdict or failing-parameter set differ.
    """
    audit_by_id = {a.call_id: a for a in audits}
    pairs = [(cid, g, audit_by_id[cid]) for cid, g in grades.items() if cid in audit_by_id]
    graded = len(pairs)
    verdict_agree = sum(1 for _, g, a in pairs if g.get("verdict") == a.verdict)
    parameters = sorted({p for _, g, a in pairs for p in (set(g.get("parameters") or []) | set(failing_parameters(a)))})
    per_parameter: dict[str, dict] = {}
    for param in parameters:
        agreed = sum(
            1 for _, g, a in pairs if (param in (g.get("parameters") or [])) == (param in set(failing_parameters(a)))
        )
        per_parameter[param] = {"agree": agreed, "rate": (agreed / graded) if graded else 0.0}
    mismatches = []
    for cid, g, a in pairs:
        if g.get("verdict") != a.verdict or set(g.get("parameters") or []) != set(failing_parameters(a)):
            mismatches.append({"call_id": cid, "hand": g.get("verdict"), "audit": a.verdict})
    return {
        "graded": graded,
        "verdict_agree": verdict_agree,
        "verdict_rate": (verdict_agree / graded) if graded else 0.0,
        "per_parameter": per_parameter,
        "mismatches": mismatches,
    }
