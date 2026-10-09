import json
from pathlib import Path

import pytest

from vaniloop import ledger, pipeline
from vaniloop.models import CallRecord, Ledger
from vaniloop.rules import audit_call

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
    assert s["regraded"] == 3 and "stale" not in {r["call_id"] for r in rows}
    calls = {r["call_id"]: CallRecord.model_validate(r) for r in lines(p["calls_path"])}
    expected = {
        r["call_id"]: audit_call(calls[r["call_id"]], Ledger.model_validate(r)).model_dump(mode="json")
        for r in lines(p["ledgers_path"])
    }
    assert {r["call_id"]: r for r in rows} == expected and len(rows) == len(expected)


def test_regrade_refuses_to_replace_audits_when_inputs_are_missing(tmp_path):
    """regrade stops before touching audits.jsonl when the calls or ledgers file is missing or empty.

    Review found missing inputs read as empty lists, so regrade replaced real audits with an empty file.
    """
    audits = tmp_path / "a.jsonl"
    audits.write_text('{"call_id": "keep", "verdict": "Pass"}\n')
    with pytest.raises(SystemExit):
        pipeline.run_regrade(tmp_path / "no_calls.jsonl", tmp_path / "no_ledgers.jsonl", audits)
    assert "keep" in audits.read_text()


def test_regrade_keeps_audits_when_calls_and_ledgers_share_no_call(tmp_path, monkeypatch):
    """regrade stops before touching audits.jsonl when no ledger matches any call.

    Review round 2 found that non-empty but disjoint inputs passed the guard and replaced audits with an empty file.
    """
    monkeypatch.setattr(ledger, "complete_json", lambda *a, **k: dict(FAKE))
    p = paths(tmp_path)
    pipeline.run_audit(**p)
    p["ledgers_path"].write_text(
        "".join(json.dumps({**r, "call_id": f"other-{i}"}) + "\n" for i, r in enumerate(lines(p["ledgers_path"])))
    )
    p["audits_path"].write_text('{"call_id": "keep", "verdict": "Pass"}\n')
    with pytest.raises(SystemExit):
        pipeline.run_regrade(p["calls_path"], p["ledgers_path"], p["audits_path"])
    assert "keep" in p["audits_path"].read_text()


def test_issues_lines_show_the_status_and_the_failed_condition_of_a_non_pattern():
    """The issues printout must show each cause's status and, for a non-pattern, the condition it failed.

    Without it the owner could not tell from the terminal why a cause got no fix.
    """
    from vaniloop.models import Cause

    def c(cid, status):
        return Cause(
            cause_id=cid, name=cid, description="", type="prompt", parameter=cid, status=status, fatal_calls=["a"]
        )

    out = pipeline.cause_lines(
        [c("p", "pattern_unconfirmed"), c("s", "single_category")],
        [{"cause_id": "s", "failed": ["categories", "reproduction"]}],
    )
    assert out[0].startswith("  [pattern_unconfirmed] p:") and "failed" not in out[0]
    assert out[1].startswith("  [single_category] s:") and out[1].endswith("failed: categories, reproduction")


def test_cause_lines_show_the_symptom_count():
    """The issues printout must show how many calls are symptoms of each cause, 0 when there are none.

    Without it the root/symptom split could only be read from causes.json, not from the terminal.
    """
    from vaniloop.models import Cause

    def c(cid, symptoms):
        return Cause(
            cause_id=cid, name=cid, description="", type="prompt", parameter=cid, fatal_calls=["a"],
            symptom_calls=symptoms,
        )  # fmt: skip

    out = pipeline.cause_lines([c("p", ["x", "y", "z"]), c("q", [])], [])
    assert "symptoms=3" in out[0] and "symptoms=0" in out[1]
