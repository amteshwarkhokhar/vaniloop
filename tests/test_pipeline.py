import json
from pathlib import Path

from vaniloop import ledger, pipeline
from vaniloop.models import CallRecord

# Three synthetic calls, so the counts below do not change with the local call data in runs/.
CALLS = Path(__file__).resolve().parent / "fixtures" / "synthetic_calls.jsonl"

FAKE = {
    "scope": "bl_collection",
    "initial_product": "x",
    "final_product": "x",
    "product_changed_at_turn": None,
    "buyer_facts": {},
    "bot_confirmed": {},
    "bot_turns": [],
    "user_turns": [],
}


def paths(tmp):
    calls = tmp / "calls.jsonl"
    calls.write_text("".join(CALLS.read_text().splitlines(keepends=True)[:3]))
    return dict(
        calls_path=calls, ledgers_path=tmp / "l.jsonl", audits_path=tmp / "a.jsonl", errors_path=tmp / "e.jsonl"
    )


def lines(p):
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()] if p.exists() else []


def test_run_resume_and_limit(tmp_path, monkeypatch):
    """The audit loop must honour the limit, skip already-audited calls and write one ledger and audit row per call.

    A bug would redo finished work and drift the processed, skipped and error counts an operator reads.
    """
    monkeypatch.setattr(ledger, "complete_json", lambda *a, **k: dict(FAKE))
    p = paths(tmp_path)
    s = pipeline.run_audit(**p, limit=2)
    assert (s["processed"], s["skipped"], s["errors"]) == (2, 0, 0)
    s = pipeline.run_audit(**p)
    assert (s["processed"], s["skipped"], s["errors"]) == (1, 2, 0)
    assert len(lines(tmp_path / "a.jsonl")) == 3 and len(lines(tmp_path / "l.jsonl")) == 3
    assert pipeline.run_audit(**p)["processed"] == 0


def test_error_is_captured_and_run_continues(tmp_path, monkeypatch):
    """One failing call must be written to errors.jsonl and retried next run while the other calls still finish.

    A bug would let a single bad call stop the whole audit and hide the failure from the operator.
    """
    first = CallRecord.model_validate_json(CALLS.read_text().splitlines()[0]).call_id

    def fake(system, user, *a, **k):
        if f"call {first}" in user:
            raise RuntimeError("boom")
        return dict(FAKE)

    monkeypatch.setattr(ledger, "complete_json", fake)
    p = paths(tmp_path)
    s = pipeline.run_audit(**p, workers=2)
    assert s["errors"] == 1 and s["processed"] == 2
    err = lines(tmp_path / "e.jsonl")
    assert err[0]["call_id"] == first and "boom" in err[0]["error"]
    # failed call is retried on the next run
    monkeypatch.setattr(ledger, "complete_json", lambda *a, **k: dict(FAKE))
    assert pipeline.run_audit(**p)["processed"] == 1


def test_regrade_rescores_saved_ledgers_without_any_llm_call(tmp_path, monkeypatch):
    """regrade re-applies the rules to saved ledgers, rewrites one audit per call and never calls the model.

    If this breaks, each calibration round pays for a new LLM pass, or old audit rows survive a rule change.
    """
    monkeypatch.setattr(ledger, "complete_json", lambda *a, **k: dict(FAKE))
    p = paths(tmp_path)
    pipeline.run_audit(**p)
    (tmp_path / "a.jsonl").write_text('{"call_id": "stale", "verdict": "Pass"}\n')

    def no_llm(*a, **k):
        raise AssertionError("regrade must not call the model")

    monkeypatch.setattr(ledger, "complete_json", no_llm)
    s = pipeline.run_regrade(p["calls_path"], p["ledgers_path"], p["audits_path"])
    rows = lines(tmp_path / "a.jsonl")
    assert s["regraded"] == 3 and len(rows) == 3
    assert "stale" not in {r["call_id"] for r in rows}
