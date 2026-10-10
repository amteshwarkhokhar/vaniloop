import json
from pathlib import Path

from vaniloop import ledger, pipeline

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "synthetic_calls.jsonl"
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


def transcript(call_id):
    return {
        "call_id": call_id,
        "duration_sec": 10.0,
        "turns": [
            {"speaker": "bot", "start_sec": 0.0, "end_sec": 3.0, "text": "Hello"},
            {"speaker": "seller", "start_sec": 3.5, "end_sec": 6.0, "text": "Hi"},
        ],
    }


def rows(p):
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()] if p.exists() else []


def test_submit_mp3_json_existing_call_and_a_bad_file(tmp_path, monkeypatch, capsys):
    """submit must audit an mp3, a transcript json and an existing call, record one bad json as an error row.

    A bug would stop at the bad file, skip a source kind, or print no SUBMIT_DONE line the console waits for.
    """
    inbox, ws, src = tmp_path / "ws" / "inbox", tmp_path / "ws", tmp_path / "src"
    inbox.mkdir(parents=True)
    src.mkdir()
    (inbox / "up_mp3.mp3").write_bytes(b"x")
    (inbox / "up_json.json").write_text(json.dumps(transcript("up_json")))
    (inbox / "bad.json").write_text("{not json")
    (src / "calls.jsonl").write_text(FIXTURE.read_text().splitlines()[0] + "\n")  # syn_001

    def fake_transcribe(mp3, out_dir, log_path):
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        (Path(out_dir) / f"{mp3.stem}.json").write_text(json.dumps(transcript(mp3.stem)))
        return {"call_id": mp3.stem, "status": "ok"}

    monkeypatch.setattr(pipeline, "transcribe_call", fake_transcribe)
    monkeypatch.setattr(pipeline, "detect_silences", lambda p: [])
    monkeypatch.setattr(ledger, "complete_json", lambda *a, **k: dict(FAKE))
    monkeypatch.setattr(pipeline, "judge_ledger", lambda call, led: led)
    pipeline.main(["--runs", str(ws), "submit", "--inbox", str(inbox), "--calls", "syn_001", "--from", str(src)])
    out = capsys.readouterr().out
    assert sorted(a["call_id"] for a in rows(ws / "audits.jsonl")) == ["syn_001", "up_json", "up_mp3"]
    assert [e["call_id"] for e in rows(ws / "errors.jsonl")] == ["bad"]
    assert "SUBMIT_DONE calls=3 errors=1" in out
    assert "STEP ingest 2/4 bad" in out and "STEP audit 3/3" in out and "STEP judge 3/3" in out


def setup_submit(tmp_path, monkeypatch, judge=lambda call, led: led):
    """A workspace with one valid transcript in the inbox and mocked Gemini and Jev."""
    ws = tmp_path / "ws"
    inbox = ws / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "up_json.json").write_text(json.dumps(transcript("up_json")))
    monkeypatch.setattr(ledger, "complete_json", lambda *a, **k: dict(FAKE))
    monkeypatch.setattr(pipeline, "judge_ledger", judge)
    return ws, inbox


def test_submit_keeps_unrelated_audit_rows(tmp_path, monkeypatch):
    """submit must regrade only the submitted ids and keep an older audit row that has no Jev ledger.

    A bug would rewrite the whole audits file from the Jev ledgers and silently delete other calls' grades.
    """
    ws, inbox = setup_submit(tmp_path, monkeypatch)
    (ws / "audits.jsonl").write_text(json.dumps({"call_id": "older", "verdict": "Pass"}) + "\n")
    pipeline.main(["--runs", str(ws), "submit", "--inbox", str(inbox)])
    assert sorted(a["call_id"] for a in rows(ws / "audits.jsonl")) == ["older", "up_json"]


def test_upsert_calls_waits_for_the_workspace_lock(tmp_path):
    """_upsert_calls must wait while another process holds the calls lock.

    A bug would read and replace calls.jsonl with no lock, so two concurrent submits lose a call.
    """
    import fcntl
    import threading
    import time

    from vaniloop.models import CallRecord

    rec = CallRecord.model_validate_json(FIXTURE.read_text().splitlines()[0])
    path = tmp_path / "calls.jsonl"
    with (tmp_path / ".calls.lock").open("a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        t = threading.Thread(target=pipeline._upsert_calls, args=(path, [rec]))
        t.start()
        time.sleep(0.3)
        assert not path.exists()
        fcntl.flock(lf, fcntl.LOCK_UN)
    t.join(5)
    assert len(rows(path)) == 1


def test_submit_counts_a_jev_failure_as_errors_and_no_final_audit(tmp_path, monkeypatch, capsys):
    """When Jev fails, submit must report calls=0 and errors equal to the error rows of the run.

    A bug would count the Gemini-only audit row as a final grade and print only the regrade failure.
    """

    def boom(call, led):
        raise RuntimeError("jev down")

    ws, inbox = setup_submit(tmp_path, monkeypatch, judge=boom)
    pipeline.main(["--runs", str(ws), "submit", "--inbox", str(inbox)])
    n = len(rows(ws / "errors.jsonl"))
    assert n >= 2  # the judge failure and the regrade that had no ledger
    assert f"SUBMIT_DONE calls=0 errors={n}" in capsys.readouterr().out
    assert rows(ws / "audits.jsonl") == []


def test_submit_rejects_unsafe_call_ids(tmp_path, monkeypatch, capsys):
    """submit must reject a transcript call_id like ../outside and a --calls id like ../x with an error row.

    A bug would accept it, and its audio path would resolve outside the inbox.
    """
    ws, inbox = setup_submit(tmp_path, monkeypatch)
    (inbox / "evil.json").write_text(json.dumps(transcript("../outside")))
    src = tmp_path / "src"
    src.mkdir()
    pipeline.main(["--runs", str(ws), "submit", "--inbox", str(inbox), "--calls", "../x", "--from", str(src)])
    assert [a["call_id"] for a in rows(ws / "audits.jsonl")] == ["up_json"]
    assert sorted(e["call_id"] for e in rows(ws / "errors.jsonl")) == ["../x", "evil"]
    assert "SUBMIT_DONE calls=1 errors=2" in capsys.readouterr().out


def test_submit_ignores_a_stale_jev_ledger(tmp_path, monkeypatch, capsys):
    """A second submit with a failing Jev and a forced refresh must not count the first run's Jev ledger as current.

    A bug would regrade from the stale ledger and report calls=1 although Jev did not judge the call this time.
    """
    ws, inbox = setup_submit(tmp_path, monkeypatch)
    pipeline.main(["--runs", str(ws), "submit", "--inbox", str(inbox)])
    assert "SUBMIT_DONE calls=1 errors=0" in capsys.readouterr().out

    def boom(call, led):
        raise RuntimeError("jev down")

    monkeypatch.setattr(pipeline, "judge_ledger", boom)
    monkeypatch.setenv("VANILOOP_CACHE", "refresh")
    monkeypatch.setattr(pipeline, "fingerprint", lambda call, led: "changed")  # the ledger the refresh would judge
    pipeline.main(["--runs", str(ws), "submit", "--inbox", str(inbox)])
    out = capsys.readouterr().out
    assert "SUBMIT_DONE calls=0" in out
    assert any("stale or missing" in e["error"] for e in rows(ws / "errors.jsonl"))
    assert rows(ws / "audits.jsonl") == []


def test_check_id_rejects_a_trailing_newline():
    """_check_id must reject an id that ends in a newline.

    A bug (regex `$`) would accept it and name a file with a newline in its name.
    """
    import pytest

    with pytest.raises(ValueError):
        pipeline._check_id("valid\n")
    pipeline._check_id("valid")
