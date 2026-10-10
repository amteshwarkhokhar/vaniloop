import json
from pathlib import Path

from vaniloop import ledger, pipeline

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "synthetic_calls.jsonl"
REPO_RUNS = Path(pipeline.__file__).resolve().parents[2] / "runs"

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


def rows(p):
    return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()] if p.exists() else []


def snapshot(d):
    return {str(p): (p.stat().st_mtime_ns, p.stat().st_size) for p in d.rglob("*") if p.is_file()} if d.exists() else {}


def workspace(tmp_path, monkeypatch, n=3):
    """A tmp --runs folder with n synthetic calls, a mocked Gemini ledger and a mocked Jev judge."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "calls.jsonl").write_text("".join(FIXTURE.read_text().splitlines(keepends=True)[:n]))
    gemini = []
    monkeypatch.setattr(ledger, "complete_json", lambda *a, **k: gemini.append(1) or dict(FAKE))
    judged = []
    monkeypatch.setattr(pipeline, "judge_ledger", lambda call, led: judged.append(call.call_id) or led)
    return ws, gemini, judged


def test_runs_keeps_every_write_inside_the_folder(tmp_path, monkeypatch):
    """audit, judge, regrade and issues with --runs must read and write only inside that folder.

    A bug would write to the repo runs/ (the shared real data) while the console shows a private workspace.
    """
    ws, _, _ = workspace(tmp_path, monkeypatch)
    before = snapshot(REPO_RUNS)
    for argv in (["audit"], ["judge"], ["regrade", "--ledgers", str(ws / "ledgers_jev.jsonl")], ["issues"]):
        pipeline.main(["--runs", str(ws), *argv])
    assert snapshot(REPO_RUNS) == before
    for name in ("ledgers.jsonl", "audits.jsonl", "ledgers_jev.jsonl", "causes.json"):
        assert (ws / name).exists(), name
    assert len(rows(ws / "audits.jsonl")) == 3


def test_calls_audits_only_those_and_regrades_fresh(tmp_path, monkeypatch):
    """audit --calls c2 must replace c2's audit row once and leave the other audit rows and ledger calls alone.

    A bug would re-audit every call, or keep a stale grade for a submitted call after a rubric change.
    """
    ws, gemini, _ = workspace(tmp_path, monkeypatch)
    pipeline.main(["--runs", str(ws), "audit"])
    audits = rows(ws / "audits.jsonl")
    stale = [{**a, "verdict": "STALE"} if a["call_id"] == "syn_002" else a for a in audits]
    (ws / "audits.jsonl").write_text("".join(json.dumps(a) + "\n" for a in stale))
    gemini.clear()
    pipeline.main(["--runs", str(ws), "audit", "--calls", "syn_002"])
    now = rows(ws / "audits.jsonl")
    assert len(gemini) == 1
    assert sorted(a["call_id"] for a in now) == ["syn_001", "syn_002", "syn_003"]
    assert next(a for a in now if a["call_id"] == "syn_002")["verdict"] != "STALE"


def test_calls_on_a_fresh_folder_gives_one_audit_row(tmp_path, monkeypatch):
    """audit --calls c2 on a folder with no audits must write exactly one audit row, for c2.

    A bug would ignore the filter on a first run and audit all the calls the folder holds.
    """
    ws, _, _ = workspace(tmp_path, monkeypatch)
    pipeline.main(["--runs", str(ws), "audit", "--calls", "syn_002"])
    assert [a["call_id"] for a in rows(ws / "audits.jsonl")] == ["syn_002"]


def test_judge_calls_judges_only_those(tmp_path, monkeypatch):
    """judge --calls c2 must send only c2 to Jev.

    A bug would judge every ledger and spend Jev calls on calls the user did not pick.
    """
    ws, _, judged = workspace(tmp_path, monkeypatch)
    pipeline.main(["--runs", str(ws), "audit"])
    pipeline.main(["--runs", str(ws), "judge", "--calls", "syn_002"])
    assert judged == ["syn_002"]
    assert [r["call_id"] for r in rows(ws / "ledgers_jev.jsonl")] == ["syn_002"]


def test_workspace_paths_default_is_the_repo_files():
    """workspace_paths(None) must return the module constants.

    A bug would change where a run without --runs reads and writes.
    """
    p = pipeline.workspace_paths(None)
    assert p["audits"] == pipeline.AUDITS_PATH and p["calls"] == pipeline.CALLS_PATH
    assert p["ledgers_jev"] == pipeline.LEDGERS_JEV_PATH and p["causes"] == pipeline.CAUSES_PATH
