"""Tests for the local web server: every route, path safety, 404s and the bind address."""

from __future__ import annotations

import http.client
import json
import re
import threading
from pathlib import Path

import pytest

from vaniloop import web

FIXTURE = Path(__file__).parent / "fixtures" / "web_runs"


@pytest.fixture(scope="module")
def server():
    """A real server on an ephemeral port in a thread, reading the synthetic fixture folder."""
    srv = web.make_server(0, FIXTURE)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)


def get(srv, path: str):
    """GET a raw path with http.client, which sends it unchanged (no dot-segment cleanup)."""
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, resp.getheader("Content-Type") or "", resp.read()
    finally:
        conn.close()


def get_json(srv, path: str):
    status, ctype, body = get(srv, path)
    assert "application/json" in ctype
    return status, json.loads(body)


def test_server_binds_to_loopback_only(server):
    """The server must listen on 127.0.0.1 only; a wider bind would expose graded call data on the network."""
    assert server.server_address[0] == "127.0.0.1"
    assert web.HOST == "127.0.0.1"


def test_summary_counts_every_verdict(server):
    """The summary route reports the verdict counts, scopes, IST update time and spend the hero sentence reads."""
    status, body = get_json(server, "/api/summary")
    assert status == 200
    assert set(body) == {"total", "verdicts", "scopes", "updated_ist", "spend_usd"}
    assert body["total"] == 14
    assert body["verdicts"] == {"Fatal": 4, "NonFatal": 4, "NeedsReview": 2, "Pass": 4}
    assert sum(body["scopes"].values()) == 14
    assert body["spend_usd"] == pytest.approx(0.5575)
    assert re.fullmatch(r"\d\d:\d\d \d\d [A-Z][a-z]{2}", body["updated_ist"])


def test_calls_sorted_by_severity_then_duration(server):
    """The calls route sorts Fatal first, then longer calls first; a wrong order buries the worst calls."""
    status, rows = get_json(server, "/api/calls")
    assert status == 200
    assert len(rows) == 14
    assert set(rows[0]) == {"call_id", "verdict", "duration_sec", "scope", "first_fault"}
    order = [web.SEVERITY[r["verdict"]] for r in rows]
    assert order == sorted(order)
    for verdict in web.VERDICTS:
        durations = [r["duration_sec"] for r in rows if r["verdict"] == verdict]
        assert durations == sorted(durations, reverse=True)
    by_id = {r["call_id"]: r for r in rows}
    assert set(by_id["web_01"]["first_fault"]) == {"parameter", "status", "reason"}
    assert by_id["web_01"]["first_fault"]["parameter"] == "looping"
    assert by_id["web_09"]["first_fault"]["status"] == "Unknown"
    assert by_id["web_11"]["first_fault"] is None


def test_call_detail_returns_call_and_audit(server):
    """The call route returns the raw call record and its audit, with onset turns the tape draws its notches from."""
    status, body = get_json(server, "/api/call/web_04")
    assert status == 200
    assert set(body) == {"call", "audit"}
    assert body["call"]["call_id"] == body["audit"]["call_id"] == "web_04"
    assert body["call"]["silences"], "web_04 carries a silence longer than 5 s"
    onsets = [f["onset_turn"] for f in body["audit"]["findings"] if f["onset_turn"] is not None]
    assert onsets == [6, 8]


def test_unknown_call_returns_404_json(server):
    """An unknown call id gives a 404 with a JSON error body, so the UI shows a message and not a blank page."""
    status, body = get_json(server, "/api/call/nope")
    assert status == 404
    assert "error" in body


def test_causes_route_keeps_owner_fields_only_where_present(server):
    """The causes route returns causes.json as written: one cause has owner fields and one has none."""
    status, body = get_json(server, "/api/causes")
    assert status == 200
    assert [c["cause_id"] for c in body["causes"]] == ["C-01", "C-02", "C-03", "C-04"]
    assert "owner" in body["causes"][0] and "owner_evidence" in body["causes"][0]
    assert "owner" not in body["causes"][1]


def test_gates_route_returns_rows_in_file_order(server):
    """The gates route returns every gate row in file order with a null p-value where no test ran."""
    status, body = get_json(server, "/api/gates")
    assert status == 200
    assert [(g["cause_id"], g["decision"]) for g in body] == [
        ("C-01", "reject"),
        ("C-01", "ship"),
        ("C-03", "inconclusive"),
        ("C-02", "reject"),
    ]
    assert body[2]["p_value"] is None


def test_nan_in_gate_row_becomes_null(tmp_path):
    """A NaN p-value written by the gate stage must reach the browser as null, because JSON.parse rejects NaN."""
    (tmp_path / "gates.jsonl").write_text('{"cause_id": "C-09", "decision": "inconclusive", "p_value": NaN}\n')
    srv = web.make_server(0, tmp_path)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        status, ctype, raw = get(srv, "/api/gates")
        assert status == 200
        assert b"NaN" not in raw
        assert json.loads(raw)[0]["p_value"] is None
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def test_empty_data_folder_gives_empty_answers(tmp_path):
    """With no run files, every route still answers with an empty shape and the UI can show its empty state."""
    srv = web.make_server(0, tmp_path)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        assert get_json(srv, "/api/calls") == (200, [])
        assert get_json(srv, "/api/gates") == (200, [])
        assert get_json(srv, "/api/causes") == (200, {"causes": []})
        status, body = get_json(srv, "/api/summary")
        assert (status, body["total"], body["spend_usd"], body["updated_ist"]) == (200, 0, None, None)
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def test_root_serves_index_html(server):
    """The root route serves the page shell with the right title; without it the UI never loads."""
    status, ctype, body = get(server, "/")
    assert status == 200
    assert ctype.startswith("text/html")
    assert b"<title>vaniloop</title>" in body


def test_static_serves_files_from_web_folder(server):
    """The static route serves a file from src/vaniloop/web and answers 404 for a file that is not there."""
    status, _, body = get(server, "/static/index.html")
    assert status == 200 and b"<main" in body
    assert get(server, "/static/missing.js")[0] == 404


@pytest.mark.parametrize(
    "path",
    [
        "/static/../web.py",
        "/static/%2e%2e/web.py",
        "/static/..%2fweb.py",
        "/static/%2e%2e%2f%2e%2e%2fpyproject.toml",
        "/static//etc/passwd",
        "/static/views/../../web.py",
    ],
)
def test_static_path_traversal_is_refused(server, path):
    """No static URL may reach a file outside src/vaniloop/web; a leak would expose source files or call data."""
    status, _, body = get(server, path)
    assert status in (403, 404)
    assert b"HOST = " not in body and b"[project]" not in body and b"root:" not in body


def test_unknown_route_returns_404_json(server):
    """A route that does not exist gives a JSON 404, not an HTML error page or a stack trace."""
    status, body = get_json(server, "/api/nothing")
    assert status == 404
    assert "error" in body
