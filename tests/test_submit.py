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
