"""Tests for the dashboard API: /api/config, /api/stage/<name> and /dash, over synthetic fixtures."""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

import pytest

from vaniloop import fix, gate, jev, llm, prove, rules, web

FIXTURE = Path(__file__).parent / "fixtures" / "web_runs"


@pytest.fixture(scope="module")
def server():
    """A real server on an ephemeral port reading the synthetic fixture folder."""
    srv = web.make_server(0, FIXTURE)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)


def get(srv, path: str, host: str | None = None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    try:
        conn.putrequest("GET", path, skip_host=True)
        conn.putheader("Host", host or f"127.0.0.1:{srv.server_address[1]}")
        conn.endheaders()
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def get_json(srv, path: str):
    status, body = get(srv, path)
    return status, json.loads(body)


def serve(folder: Path):
    srv = web.make_server(0, folder)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return srv, thread


def stop(srv, thread):
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)


def leaves(node, path=""):
    """Every {value, source} leaf in a config tree, with its dotted path."""
    if isinstance(node, dict) and set(node) == {"value", "source"}:
        yield path, node
    elif isinstance(node, dict):
        for k, v in node.items():
            if k != "files":
                yield from leaves(v, f"{path}.{k}" if path else k)


def test_config_has_every_key_equal_to_the_live_constant(server):
    """Protects the dashboard's config panel: each value must be the live constant, not a copy that drifts."""
    status, cfg = get_json(server, "/api/config")
    assert status == 200
    assert set(cfg) == {"judge", "audit", "fix_loop", "data"}
    assert set(cfg["judge"]) == {
        "active", "available", "jev_model", "jev_version", "structure_model", "thresholds"
    }  # fmt: skip
    assert set(cfg["audit"]) == {"frustrated_p", "loop_irritation_turns", "dead_air_sec", "timing"}
    assert set(cfg["audit"]["timing"]) == {"min_coverage", "max_back_to_back", "max_zero_turns"}
    assert set(cfg["fix_loop"]) == {
        "max_attempts", "alpha", "alpha_per_attempt", "screen_seeds", "screen_min_fails", "min_calls", "last_run"
    }  # fmt: skip
    got = {p: leaf["value"] for p, leaf in leaves(cfg)}
    assert got["judge.thresholds.true_at"] == jev.TRUE_AT
    assert got["judge.thresholds.false_at"] == jev.FALSE_AT
    assert got["judge.jev_version"] == jev.JEV_VERSION
    assert got["judge.jev_model"] == llm.jev_model()
    assert got["judge.structure_model"] == llm.model_name()
    assert got["audit.frustrated_p"] == rules.FRUSTRATED_P
    assert got["audit.loop_irritation_turns"] == rules.LOOP_IRRITATION_TURNS
    assert got["audit.dead_air_sec"] == rules.DEAD_AIR_SEC
    assert got["audit.timing.min_coverage"] == rules.TIMING_MIN_COVERAGE
    assert got["audit.timing.max_back_to_back"] == rules.TIMING_MAX_BACK_TO_BACK
    assert got["audit.timing.max_zero_turns"] == rules.TIMING_MAX_ZERO_TURNS
    assert got["fix_loop.max_attempts"] == fix.MAX_ATTEMPTS
    assert got["fix_loop.alpha"] == gate.DEFAULT_ALPHA
    assert got["fix_loop.alpha_per_attempt"] == gate.DEFAULT_ALPHA / prove.MAX_ATTEMPTS
    assert got["fix_loop.screen_seeds"] == prove.SCREEN_SEEDS
    assert got["fix_loop.screen_min_fails"] == prove.SCREEN_MIN_FAILS
    assert got["fix_loop.min_calls"] == gate.MIN_CALLS
    assert cfg["audit"]["frustrated_p"]["source"] == "rules.FRUSTRATED_P"
    assert all(leaf["source"] for _, leaf in leaves(cfg))


def test_config_has_no_secret_and_only_the_folder_name(server):
    """Protects the secret rule: no config value may hold a key, token or full path."""
    status, body = get(server, "/api/config")
    text = body.decode().lower()
    assert status == 200
    for bad in ("key", "sk-", "bearer", "http", str(FIXTURE.parent).lower()):
        assert bad not in text
    assert json.loads(body)["data"]["folder"]["value"] == "web_runs"


def test_config_judge_data_and_last_run_from_fixtures(server):
    """Protects the judge marker, the file listing and last_run, which the sidebar shows per stage."""
    _, cfg = get_json(server, "/api/config")
    assert cfg["judge"]["active"]["value"] == "jev"
    assert cfg["judge"]["available"]["value"] == ["jev", "gemini"]
    files = {f["name"]: f for f in cfg["data"]["files"]}
    assert list(files) == [
        "calls.jsonl", "ledgers.jsonl", "audits.jsonl", "audits_gemini.jsonl", "causes.json",
        "patches.json", "gates.jsonl", "replays.jsonl", "costs.jsonl", "jev_review.jsonl",
    ]  # fmt: skip
    assert files["calls.jsonl"]["rows"] == 14
    assert files["audits.jsonl"]["updated_ist"]
    assert files["ledgers.jsonl"] == {"name": "ledgers.jsonl", "rows": None, "updated_ist": None}
    last = cfg["fix_loop"]["last_run"]
    assert (last["n"]["value"], last["k"]["value"]) == (3, 3)
    assert last["holdout"]["value"] is False and last["screen"]["value"] is False


def test_config_without_files_marks_gemini_and_null_last_run(tmp_path):
    """An empty folder must give judge gemini, no available judge and last_run null, not an error."""
    srv, thread = serve(tmp_path)
    try:
        _, cfg = get_json(srv, "/api/config")
    finally:
        stop(srv, thread)
    assert cfg["judge"]["active"]["value"] == "gemini"
    assert cfg["judge"]["available"]["value"] == []
    assert cfg["fix_loop"]["last_run"] is None


def test_stage_ingest(server):
    """Protects the ingest numbers: counts, minutes, median, p90 and short calls from calls.jsonl."""
    status, got = get_json(server, "/api/stage/ingest")
    assert status == 200
    rows = [json.loads(line) for line in (FIXTURE / "calls.jsonl").read_text().splitlines() if line]
    secs = sorted(r["duration_sec"] for r in rows)
    assert got["calls"] == len(rows)
    assert got["with_audio"] == sum(1 for r in rows if r.get("audio_path"))
    assert got["minutes_total"] == round(sum(secs) / 60, 2)
    assert got["median_sec"] in secs and got["p90_sec"] in secs and got["p90_sec"] >= got["median_sec"]
    assert got["short_calls"] == sum(1 for s in secs if s < rules.SHORT_CALL_SEC)


def test_stage_audit_jev_default(server):
    """Protects the audit stage: verdicts, scopes, per-parameter status counts and flags from audits.jsonl."""
    status, got = get_json(server, "/api/stage/audit")
    assert status == 200
    assert got["judge"] == "jev" and got["total"] == 14
    assert got["verdicts"] == {"Fatal": 4, "NonFatal": 4, "NeedsReview": 2, "Pass": 4}
    assert sum(got["scopes"].values()) == 14
    assert got["flags"]["loops"] >= 1
    assert got["by_parameter"]["qty_probing"]["Pass"] >= 1


def test_audit_judge_toggle_switches_the_file(server):
    """Protects the judge toggle: gemini reads audits_gemini.jsonl, so the verdict counts must differ from jev."""
    _, jev_view = get_json(server, "/api/stage/audit?judge=jev")
    _, gem_view = get_json(server, "/api/stage/audit?judge=gemini")
    assert gem_view["judge"] == "gemini"
    assert gem_view["verdicts"] != jev_view["verdicts"]
    assert gem_view["verdicts"]["Fatal"] == jev_view["verdicts"]["Fatal"] - 1


def test_audit_unknown_judge_is_400(server):
    """A junk ?judge= value must be a 400 JSON error, not a silent fallback that shows the wrong judge's data."""
    status, got = get_json(server, "/api/stage/audit?judge=nope")
    assert status == 400 and got == {"error": "unknown judge"}
    status, got = get_json(server, "/api/stage/audit?judge=")
    assert status == 400


def test_audit_gemini_only_folder_serves_audits_for_gemini(tmp_path):
    """With no audits_gemini.jsonl the single audits file is Gemini's, and a jev request has no rows."""
    (tmp_path / "audits.jsonl").write_text(
        json.dumps({"call_id": "x1", "verdict": "Pass", "scope": "bl_collection", "findings": []}) + "\n"
    )
    srv, thread = serve(tmp_path)
    try:
        _, default = get_json(srv, "/api/stage/audit")
        _, jev_view = get_json(srv, "/api/stage/audit?judge=jev")
    finally:
        stop(srv, thread)
    assert default["judge"] == "gemini" and default["total"] == 1
    assert jev_view["total"] == 0


def test_stage_issues(server):
    """Protects the issues stage: one row per cause with counts for fatal, nonfatal and symptom calls."""
    status, got = get_json(server, "/api/stage/issues")
    assert status == 200
    first = got["causes"][0]
    assert first["cause_id"] == "C-01" and first["fatal"] == 2 and first["nonfatal"] == 0
    assert set(first) == {
        "cause_id", "name", "parameter", "status", "type", "owner", "fatal", "nonfatal", "symptoms", "priority"
    }  # fmt: skip
    assert len(got["causes"]) == 4


def test_stage_fix_patches_history_and_tickets(tmp_path):
    """Protects the fix stage: current patches, history with gate_decision, and tickets for tool and data causes."""
    (tmp_path / "patches.json").write_text((FIXTURE / "patches.json").read_text())
    causes = [
        {"cause_id": "C-1", "type": "tool"},
        {"cause_id": "C-2", "type": "prompt"},
        {"cause_id": "C-3", "type": "data"},
    ]
    (tmp_path / "causes.json").write_text(json.dumps({"causes": causes}))
    srv, thread = serve(tmp_path)
    try:
        status, got = get_json(srv, "/api/stage/fix")
    finally:
        stop(srv, thread)
    assert status == 200
    assert got["patches"][0]["attempt"] == 2 and "gate_decision" not in got["patches"][0]
    assert got["history"][0]["gate_decision"] == "reject"
    assert got["tickets"] == ["C-1", "C-3"]


def test_stage_fix_without_files(tmp_path):
    """A folder without patches.json must give empty lists, not an error."""
    srv, thread = serve(tmp_path)
    try:
        _, got = get_json(srv, "/api/stage/fix")
    finally:
        stop(srv, thread)
    assert got == {"patches": [], "history": [], "tickets": []}


def test_stage_gate(server):
    """Protects the gate stage: the gates rows as /api/gates gives them, plus the screens rows."""
    _, got = get_json(server, "/api/stage/gate")
    _, gates = get_json(server, "/api/gates")
    assert got["gates"] == gates
    assert got["screens"][0]["kept"] == 2


def test_stage_gate_without_screens(tmp_path):
    """No screens.jsonl must give screens [] so the page needs no special case."""
    srv, thread = serve(tmp_path)
    try:
        _, got = get_json(srv, "/api/stage/gate")
    finally:
        stop(srv, thread)
    assert got == {"gates": [], "screens": []}


def test_review_with_file_totals_accuracy_and_cut(server):
    """Protects the review stage: outcome totals and accuracy per kind and group, and the 200-character cut."""
    status, got = get_json(server, "/api/stage/review")
    assert status == 200
    assert got["totals"]["spec"] == {"jev_only": 1, "both_right": 1}
    assert got["totals"]["irritation"] == {"gemini_only": 1, "both_right": 1}
    assert got["accuracy"]["spec"]["contested"] == {"jev_right": 1, "gemini_right": 0, "n": 1}
    assert got["accuracy"]["spec"]["control"] == {"jev_right": 1, "gemini_right": 1, "n": 1}
    assert got["accuracy"]["irritation"]["contested"] == {"jev_right": 1, "gemini_right": 2, "n": 2}
    assert len(got["items"]) == 3
    assert len(got["items"][0]["reason"]) == 200


def test_review_without_file(tmp_path):
    """No jev_review.jsonl must give the documented empty shape."""
    srv, thread = serve(tmp_path)
    try:
        _, got = get_json(srv, "/api/stage/review")
    finally:
        stop(srv, thread)
    assert got == {"items": [], "totals": {}, "accuracy": {}}


def test_unknown_stage_is_404(server):
    """An unknown stage name must be a 404 JSON error, not a 500 or an empty 200."""
    status, got = get_json(server, "/api/stage/nope")
    assert status == 404 and got == {"error": "unknown stage"}


@pytest.mark.parametrize("path", ["/api/config", "/api/stage/review", "/dash"])
def test_dash_routes_keep_the_host_allowlist(server, path):
    """A rebound DNS name must not read the new routes: the Host allowlist applies to them too."""
    status, _ = get(server, path, host="evil.example")
    assert status == 403


def test_dash_serves_the_placeholder_page(server):
    """GET /dash must serve web/dash.html as HTML, so issue B can fill the page."""
    status, body = get(server, "/dash")
    assert status == 200 and body.lstrip().lower().startswith(b"<!doctype html>")


def _write(folder: Path, name: str, text: str) -> None:
    (folder / name).write_text(text, encoding="utf-8")


def test_wrong_typed_fields_do_not_crash_endpoints(tmp_path):
    """Protects the endpoints from malformed run files: a null patches list, a numeric flags field and a
    numeric fatal_calls must each read as empty instead of raising TypeError and giving a 500."""
    _write(tmp_path, "patches.json", json.dumps({"patches": None}))
    _write(tmp_path, "audits.jsonl", json.dumps({"call_id": "synthetic", "flags": 1, "verdict": "Pass"}) + "\n")
    _write(tmp_path, "causes.json", json.dumps({"causes": [{"cause_id": "c1", "fatal_calls": 1}]}))
    srv, thread = serve(tmp_path)
    try:
        status, cfg = get_json(srv, "/api/config")
        audit_status, audit = get_json(srv, "/api/stage/audit")
        issues_status, issues = get_json(srv, "/api/stage/issues")
    finally:
        stop(srv, thread)
    assert status == 200 and audit_status == 200 and issues_status == 200
    assert next(f for f in cfg["data"]["files"] if f["name"] == "patches.json")["rows"] == 0
    assert audit["flags"] == {}
    assert issues["causes"][0]["fatal"] == 0


def test_huge_duration_becomes_null_not_overflow(tmp_path):
    """Protects /api/calls and the ingest stage: a duration_sec of 10**400 raises OverflowError in
    math.isfinite, so it must become null instead of a 500."""
    row = '{"call_id": "synthetic", "duration_sec": 1' + "0" * 400 + "}\n"
    _write(tmp_path, "calls.jsonl", row)
    _write(tmp_path, "audits.jsonl", json.dumps({"call_id": "synthetic", "verdict": "Pass"}) + "\n")
    srv, thread = serve(tmp_path)
    try:
        calls_status, rows = get_json(srv, "/api/calls")
        ingest_status, ingest = get_json(srv, "/api/stage/ingest")
    finally:
        stop(srv, thread)
    assert calls_status == 200 and rows[0]["duration_sec"] is None
    assert ingest_status == 200 and ingest["median_sec"] is None


def test_review_null_or_missing_truth_is_unscored(tmp_path):
    """Protects review accuracy: a label with a null or missing truth must be unscored and left out of
    jev_right, gemini_right and n, not counted as both_wrong."""
    labels = [
        {"truth": None, "jev_ok": False, "gemini_ok": False},
        {"jev_ok": False, "gemini_ok": False},
        {"truth": True, "jev_ok": True, "gemini_ok": False},
    ]
    _write(tmp_path, "jev_review.jsonl", json.dumps({"kind": "spec", "control": False, "labels": labels}) + "\n")
    srv, thread = serve(tmp_path)
    try:
        _, got = get_json(srv, "/api/stage/review")
    finally:
        stop(srv, thread)
    assert got["totals"]["spec"] == {"unscored": 2, "jev_only": 1}
    assert got["accuracy"]["spec"]["contested"] == {"jev_right": 1, "gemini_right": 0, "n": 1}


@pytest.mark.parametrize("var", ["LLM_MODEL", "TYPESAFE_MODEL"])
def test_config_hides_a_model_value_that_looks_like_a_url(tmp_path, monkeypatch, var):
    """Protects the secret rule: a model env value holding a URL or token must show as (hidden) in
    /api/config, while a plain model id is shown as it is."""
    monkeypatch.setenv(var, "https://synthetic.invalid/?token=synthetic")
    srv, thread = serve(tmp_path)
    try:
        _, hidden = get_json(srv, "/api/config")
        monkeypatch.setenv(var, "vendor/model-name_1.5:free")
        _, shown = get_json(srv, "/api/config")
    finally:
        stop(srv, thread)
    key = "structure_model" if var == "LLM_MODEL" else "jev_model"
    assert hidden["judge"][key]["value"] == "(hidden)"
    assert "synthetic.invalid" not in json.dumps(hidden)
    assert shown["judge"][key]["value"] == "vendor/model-name_1.5:free"


@pytest.mark.parametrize("name", ["a=b", "user@host", "x?y", "a" * 81, "has space", ""])
def test_safe_model_hides_unsafe_ids(name):
    """Protects _safe_model: ids with =, @, ?, spaces, an empty string or over 80 characters must be hidden."""
    assert web._safe_model(name) == "(hidden)"


@pytest.mark.parametrize(
    "name",
    [
        "/Users/BuyerA/private/model",
        "./models/local",
        "~/models/x",
        "a/b/c/d",
        "models/../secret",
        "sk-syntheticCredential123",
        "ghp_syntheticToken",
        "AIza-syntheticKey",
        "vendor/" + "x" * 32,
    ],
)
def test_safe_model_hides_paths_and_credentials(name):
    """Protects the secret rule: a filesystem path or a credential-shaped value in LLM_MODEL or JEV_MODEL
    must show as (hidden). If this fails, /api/config leaks a local path or an API key to the browser."""
    assert web._safe_model(name) == "(hidden)"


@pytest.mark.parametrize(
    "name", ["gemini-2.5-flash", "openrouter/google/gemini-2.5-flash", "jev", "vendor/model-name_1.5:free"]
)
def test_safe_model_keeps_real_model_ids(name):
    """Protects the config view: ordinary model ids, including two-level provider paths, must still show."""
    assert web._safe_model(name) == name


def test_dash_page_links_its_assets_and_serves_them(server):
    """/dash must link dash.css and dash.js, and both must be served; a missing asset would leave the page blank."""
    _, body = get(server, "/dash")
    for asset in (b"/static/dash.css", b"/static/dash.js"):
        assert asset in body
        status, content = get(server, asset.decode())
        assert status == 200 and content


def test_hostile_review_text_reaches_the_page_as_data(server):
    """The fixture's <img onerror> string must come through the API as plain text, so the page's esc() is what keeps it inert."""
    _, body = get_json(server, "/api/stage/review")
    assert any("<img src=x onerror=alert(1)>" in it["reason"] for it in body["items"])
    _, js = get(server, "/static/dash.js")
    assert b"innerHTML = " in js and b"const esc = " in js
