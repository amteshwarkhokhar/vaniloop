"""Tests for the console job API: security checks, job runner, confirm rule and secret masking."""

from __future__ import annotations

import http.client
import json
import sys
import threading
import time
from datetime import UTC

import pytest

from vaniloop import console, llm, web


def call(srv, method, path, body=None, *, origin="auto", ctype="application/json", raw=None, clen=None):
    port = srv.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
        conn.putrequest(method, path, skip_host=True)
        conn.putheader("Host", f"127.0.0.1:{port}")
        if origin == "auto":
            origin = f"http://127.0.0.1:{port}"
        if origin:
            conn.putheader("Origin", origin)
        if ctype:
            conn.putheader("Content-Type", ctype)
        conn.putheader("Content-Length", str(len(data) if clen is None else clen))
        conn.endheaders()
        conn.send(data)
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read() or b"{}")
    finally:
        conn.close()


@pytest.fixture
def data(tmp_path, monkeypatch):
    (tmp_path / "calls.jsonl").write_text(
        "\n".join(json.dumps({"call_id": c}) for c in ("c1", "c2")) + "\n", encoding="utf-8"
    )
    (tmp_path / "causes.json").write_text(json.dumps({"causes": [{"cause_id": "C-01"}]}), encoding="utf-8")
    prompt = tmp_path / "prompt.md"
    prompt.write_text("synthetic prompt")
    monkeypatch.setattr(console, "PROMPT_PATH", prompt)
    return tmp_path


@pytest.fixture
def srv(data, monkeypatch):
    monkeypatch.setattr(llm, "COSTS_PATH", data / "costs.jsonl")
    server = web.make_server(0, data)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield server
    runner = server.RequestHandlerClass.runner
    if runner:
        for proc in list(runner.procs.values()):
            proc.kill()
    server.shutdown()
    server.server_close()
    t.join(timeout=5)


def use_argv(monkeypatch, code, seen=None):
    def fake(d, stage, args):
        if seen is not None:
            seen.append((stage, args))
        return [sys.executable, "-c", code]

    monkeypatch.setattr(console, "build_argv", fake)


def wait_status(srv, job_id, want, secs=8):
    end = time.time() + secs
    while time.time() < end:
        _, out = call(srv, "GET", f"/api/jobs/{job_id}/log")
        if out["job"]["status"] in want:
            return out
        time.sleep(0.05)
    raise AssertionError(f"job stuck: {out['job']}")


def test_cross_site_and_bad_posts_are_refused(srv):
    """Protects the paid-job endpoint from drive-by sites: no Origin or a foreign Origin gives 403, a form
    content type 415 and an over-cap body 413. Without these checks any web page could spend the LLM budget."""
    body = {"stage": "issues"}
    assert call(srv, "POST", "/api/jobs", body, origin=None)[0] == 403
    assert call(srv, "POST", "/api/jobs", body, origin="http://evil.example")[0] == 403
    assert call(srv, "POST", "/api/jobs", body, origin="http://127.0.0.1:1")[0] == 403
    assert call(srv, "POST", "/api/jobs", body, ctype="application/x-www-form-urlencoded")[0] == 415
    assert call(srv, "POST", "/api/jobs", raw=b"{}", clen=web.MAX_BODY + 1)[0] == 413


def test_read_only_folder_refuses_writes(tmp_path, monkeypatch):
    """Protects the frozen demo data: a data folder under <repo>/.cache/ must refuse POST with 403, or a
    demo click could overwrite the frozen results."""
    monkeypatch.setattr(web, "ROOT", tmp_path)
    frozen = tmp_path / ".cache" / "demo"
    frozen.mkdir(parents=True)
    server = web.make_server(0, frozen)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        status, out = call(server, "POST", "/api/jobs", {"stage": "issues"})
        assert (status, out["error"]) == (403, "read-only data folder")
        assert call(server, "GET", "/api/console")[1]["read_only"] is True
    finally:
        server.shutdown()
        server.server_close()


def test_console_lists_stages(srv):
    """Protects the UI's stage form: GET /api/console must list the eight contract stages with paid flags and
    no busy job, or the UI cannot build its forms."""
    status, out = call(srv, "GET", "/api/console")
    assert status == 200 and out["busy_job_id"] is None and out["read_only"] is False
    paid = {s["id"]: s["paid"] for s in out["stages"]}
    assert paid == {"audit": True, "judge": False, "regrade": False, "issues": False, "agreement": False,
                    "fix": True, "prove": True, "submit": True}  # fmt: skip


def test_job_runs_logs_and_finishes(srv, data, monkeypatch):
    """Protects the job lifecycle: a job must start with 202, expose its log lines, end done with exit code 0
    and leave a row in jobs.jsonl. If any step broke the UI would show a stuck or empty job."""
    use_argv(monkeypatch, "print('STEP x 1/1 c1')")
    status, out = call(srv, "POST", "/api/jobs", {"stage": "issues"})
    assert status == 202 and out["job"]["status"] == "running"
    log = wait_status(srv, out["job"]["id"], {"done", "failed"})
    assert log["job"]["status"] == "done" and log["job"]["exit_code"] == 0
    assert log["lines"] == ["STEP x 1/1 c1"] and log["next"] == 1
    assert call(srv, "GET", f"/api/jobs/{out['job']['id']}/log?from=1")[1]["lines"] == []
    rows = [json.loads(x) for x in (data / "console" / "jobs.jsonl").read_text().splitlines()]
    assert rows[-1]["id"] == out["job"]["id"] and rows[-1]["status"] == "done"
    assert call(srv, "GET", "/api/jobs")[1]["jobs"][0]["id"] == out["job"]["id"]


def test_one_job_at_a_time_and_cancel(srv, monkeypatch):
    """Protects the budget and the log: a second POST during a running job gives 409 busy, and cancel ends the
    job as cancelled within 3 s. If not, two paid jobs could overlap or a runaway job could not be stopped."""
    use_argv(monkeypatch, "import time; time.sleep(60)")
    _, first = call(srv, "POST", "/api/jobs", {"stage": "issues"})
    jid = first["job"]["id"]
    status, out = call(srv, "POST", "/api/jobs", {"stage": "issues"})
    assert status == 409 and out == {"error": "busy", "job_id": jid}
    assert call(srv, "GET", "/api/console")[1]["busy_job_id"] == jid
    status, out = call(srv, "POST", f"/api/jobs/{jid}/cancel", {})
    assert status == 200
    assert wait_status(srv, jid, {"cancelled"}, 3)["job"]["status"] == "cancelled"
    assert call(srv, "POST", f"/api/jobs/{jid}/cancel", {})[0] == 409
    assert call(srv, "POST", "/api/jobs/jdeadbeef0000/cancel", {})[0] == 404


def test_paid_stage_needs_confirm(srv, monkeypatch):
    """Protects the LLM budget: a paid stage without confirm true must give 400 'confirm' and start nothing.
    If it ran, one stray request would spend money."""
    seen: list = []
    use_argv(monkeypatch, "pass", seen)
    status, out = call(srv, "POST", "/api/jobs", {"stage": "audit", "args": {"limit": 5}})
    assert status == 400 and out["error"] == "confirm" and "estimate_usd" in out
    assert call(srv, "POST", "/api/jobs", {"stage": "audit", "confirm": "yes"})[0] == 400
    assert seen == []
    status, out = call(srv, "POST", "/api/jobs", {"stage": "audit", "args": {"limit": 5}, "confirm": True})
    assert status == 202 and seen == [("audit", {"limit": 5})]


@pytest.mark.parametrize(
    "body",
    [
        {"stage": "audit", "args": {"calls": ["c1; rm -rf /"]}, "confirm": True},
        {"stage": "judge", "args": {"calls": ["c"]}},
        {"stage": "audit", "args": {"calls": ["c1", 7]}, "confirm": True},
        {"stage": "audit", "args": {"limit": 0}, "confirm": True},
        {"stage": "audit", "args": {"limit": True}, "confirm": True},
        {"stage": "audit", "args": {"extra": 1}, "confirm": True},
        {"stage": "rm", "args": {}},
        {"stage": "fix", "args": {"top": 9}, "confirm": True},
        {"stage": "fix", "args": {"holdout": "yes"}, "confirm": True},
        {"stage": "prove", "args": {"cause": "C-99"}, "confirm": True},
        {"stage": "prove", "args": {}, "confirm": True},
        {"stage": "prove", "args": {"cause": "C-01", "n": 1}, "confirm": True},
        {"stage": "submit", "args": {"submission_id": "../x"}, "confirm": True},
        {"stage": "issues", "args": []},
    ],
)
def test_bad_args_never_reach_argv(srv, monkeypatch, body):
    """Protects against shell and arg injection: shell text, unknown or exact-match-failing call ids, out of
    range ints, unknown causes and bad stages must give 400 and never call the argv builder."""
    seen: list = []
    use_argv(monkeypatch, "pass", seen)
    status, out = call(srv, "POST", "/api/jobs", body)
    assert status == 400 and "error" in out
    assert seen == []


def test_valid_args_reach_argv_builder_clean(srv, data, monkeypatch):
    """Protects the argv contract: validated args arrive typed with defaults filled in, and the real argv is
    a list that starts with uv run python -m vaniloop.pipeline --runs <data>."""
    argv = console.build_argv(
        data, "prove", {"cause": "C-01", "n": 8, "k": 3, "attempt": 1, "holdout": True, "screen": False}
    )
    assert argv[:8] == ["uv", "run", "python", "-m", "vaniloop.pipeline", "--runs", str(data), "prove"]
    assert argv[8:] == ["--prompt", str(console.PROMPT_PATH), "--cause", "C-01", "--n", "8", "--k", "3",
                        "--attempt", "1", "--holdout"]  # fmt: skip
    ids = console.build_argv(data, "judge", {"calls": ["c1", "c2"]})
    assert ids[7:] == ["judge", "--calls", "c1,c2"]
    ws = data / "console" / "submissions" / "s1"
    ws.mkdir(parents=True)
    (ws / "meta.json").write_text(json.dumps({"call_ids": ["c1", "c2"]}))
    assert console.build_argv(data, "submit", {"submission_id": "s1"})[5:] == [
        "--runs", str(ws), "submit", "--inbox", str(ws / "inbox"), "--from", str(data), "--calls", "c1,c2",
    ]  # fmt: skip
    seen: list = []
    use_argv(monkeypatch, "pass", seen)
    call(srv, "POST", "/api/jobs", {"stage": "prove", "args": {"cause": "C-01"}, "confirm": True})
    assert seen == [("prove", {"cause": "C-01", "n": 8, "k": 3, "attempt": 1, "holdout": True, "screen": True})]


def test_log_masks_secrets_and_strips_ansi(srv, monkeypatch):
    """Protects keys: a secret-looking token in a job log must come back masked, and ANSI colour codes must be
    removed. If not, the log view could leak an API key."""
    use_argv(
        monkeypatch,
        "print('key sk-synthetic123 ok'); print('Authorization: Bearer abc.def'); print('api_key=zzz'); "
        "print('x AIzaSyntheticKey1'); print('\\x1b[31mred\\x1b[0m')",
    )
    _, out = call(srv, "POST", "/api/jobs", {"stage": "issues"})
    lines = wait_status(srv, out["job"]["id"], {"done"})["lines"]
    assert lines == ["key *** ok", "Authorization: ***", "***", "x ***", "red"]


def test_cost_sums_rows_in_window_without_reserve(srv, data, monkeypatch):
    """Protects the cost shown per job: cost_usd sums llm cost rows stamped during the job, skipping reserve
    rows and rows outside the window. A wrong sum would hide real spend."""
    from datetime import datetime, timedelta

    code = (
        "import json, datetime, pathlib\n"
        f"p = pathlib.Path({str(data / 'costs.jsonl')!r})\n"
        "now = datetime.datetime.now(datetime.timezone.utc).isoformat()\n"
        "rows = [{'ts': now, 'cost_usd': 0.25}, {'kind': 'reserve', 'id': 'r', 'usd': 5, 'ts': now}]\n"
        "p.write_text(''.join(json.dumps(r) + '\\n' for r in rows))\n"
    )
    use_argv(monkeypatch, code)
    _, out = call(srv, "POST", "/api/jobs", {"stage": "issues"})
    assert wait_status(srv, out["job"]["id"], {"done"})["job"]["cost_usd"] == 0.25
    old = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    (data / "costs.jsonl").write_text(json.dumps({"ts": old, "cost_usd": 9}) + "\n")
    now = datetime.now(UTC)
    assert console.job_cost(now - timedelta(minutes=1), now) == 0


def test_restart_marks_lost_running_job_failed(data):
    """Protects the job list after a server restart: a row still running whose process is gone must become
    failed. Otherwise the UI would show a job running forever."""
    (data / "console").mkdir()
    row = {"id": "j000000000001", "stage": "issues", "args": {}, "status": "running", "pid": 2**22 + 12345,
           "started_ist": "10:00:00 10 Oct", "started_utc": "2026-10-10T04:30:00+00:00"}  # fmt: skip
    (data / "console" / "jobs.jsonl").write_text(json.dumps(row) + "\n")
    runner = console.JobRunner(data)
    assert runner.list()[0]["status"] == "failed"
    assert runner.get("j000000000001")["status"] == "failed"


def test_fix_and_prove_refuse_without_prompt_file(srv, monkeypatch, tmp_path):
    """Protects fix and prove from starting a paid run with no VANI prompt: a missing prompt file must give
    400 'VANI prompt file missing' with field prompt, and start no job."""
    monkeypatch.setattr(console, "PROMPT_PATH", tmp_path / "absent.md")
    for body in ({"stage": "fix"}, {"stage": "prove", "args": {"cause": "C-01"}}):
        status, out = call(srv, "POST", "/api/jobs", {**body, "confirm": True})
        assert (status, out) == (400, {"error": "VANI prompt file missing", "field": "prompt"})
