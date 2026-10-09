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


def get(srv, path: str, host: str | None = ...):
    """GET a path with http.client. host='' really omits Host (putrequest), because request() would add it back."""
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    try:
        if host is ...:
            conn.request("GET", path)
        else:
            conn.putrequest("GET", path, skip_host=True)
            if host:
                conn.putheader("Host", host)
            conn.endheaders()
        resp = conn.getresponse()
        return resp.status, resp.getheader("Content-Type") or "", resp.read()
    finally:
        conn.close()


def get_json(srv, path: str):
    status, ctype, body = get(srv, path)
    assert "application/json" in ctype
    return status, json.loads(body)


def test_static_nul_byte_returns_400(server):
    """A NUL byte in a static path is a 400 before any filesystem call; resolve() raises on NUL and kills the GET."""
    status, ctype, body = get(server, "/static/%00")
    assert status == 400
    assert "application/json" in ctype
    assert json.loads(body) == {"error": "bad request"}


def _server_with(tmp_path: Path, calls_lines: list[str], audits_lines: list[str]):
    """A server on an ephemeral port over tmp_path with the given synthetic rows, shut down by the caller."""
    (tmp_path / "calls.jsonl").write_text("\n".join(calls_lines) + "\n", encoding="utf-8")
    (tmp_path / "audits.jsonl").write_text("\n".join(audits_lines) + "\n", encoding="utf-8")
    srv = web.make_server(0, tmp_path)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return srv, thread


def _stop(srv, thread):
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=5)


def test_calls_survive_malformed_rows(tmp_path):
    """A bad audit row must not drop the /api/calls or /api/call connection; the valid rows stay there."""
    lines = [
        json.dumps({"call_id": [], "verdict": "Fatal"}),
        json.dumps(
            {
                "call_id": "good_1",
                "verdict": "Fatal",
                "scope": "bl_collection",
                "findings": [
                    {"parameter": "looping", "status": "NonFatal", "onset_turn": 2},
                    {"parameter": "intent", "status": "Fatal", "onset_turn": 8},
                ],
            }
        ),
        json.dumps({"call_id": "bad_findings", "verdict": "NonFatal", "findings": {"status": "Fatal"}}),
        json.dumps({"call_id": "bad_finding", "verdict": "NonFatal", "findings": [42, {"status": "Unknown"}]}),
    ]
    (tmp_path / "audits.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (tmp_path / "calls.jsonl").write_text(
        json.dumps({"call_id": "good_1", "turns": "x", "silences": 7})
        + "\n"
        + json.dumps({"call_id": 9, "duration_sec": 1.0})
        + "\n",
        encoding="utf-8",
    )
    srv = web.make_server(0, tmp_path)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        status, rows = get_json(srv, "/api/calls")
        assert status == 200
        assert [r["call_id"] for r in rows] == ["good_1", "bad_findings", "bad_finding"]
        assert rows[0]["first_fault"] == {"parameter": "looping", "status": "NonFatal", "reason": None}
        status, body = get_json(srv, "/api/call/good_1")
        assert status == 200
        assert body["call"].get("turns") == [] and body["call"].get("silences") == []
        assert body["audit"]["findings"][0] == {"parameter": "looping", "status": "NonFatal", "onset_turn": 2}
        status, body = get_json(srv, "/api/call/bad_findings")
        assert status == 200 and body["audit"]["findings"] == []
        status, _ = get_json(srv, "/api/summary")
        assert status == 200
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def test_calls_survive_bad_call_id_row_next_to_good_rows(tmp_path):
    """A calls.jsonl row whose call_id is a list must not drop /api/calls or /api/call; dedupe used to run
    before validation and dict-keyed the unhashable list, killing the connection for good neighbours too."""
    calls_lines = [
        json.dumps({"call_id": "good_1", "duration_sec": 12}),
        json.dumps({"call_id": []}),
        json.dumps({"call_id": "good_2", "duration_sec": 5}),
    ]
    audits_lines = [
        json.dumps({"call_id": "good_1", "verdict": "Pass"}),
        json.dumps({"call_id": "good_2", "verdict": "Fatal"}),
    ]
    srv, thread = _server_with(tmp_path, calls_lines, audits_lines)
    try:
        status, rows = get_json(srv, "/api/calls")
        assert status == 200
        assert {r["call_id"] for r in rows} == {"good_1", "good_2"}
        status, body = get_json(srv, "/api/call/good_1")
        assert status == 200 and body["call"]["duration_sec"] == 12
    finally:
        _stop(srv, thread)


def test_malformed_scalars_become_safe_defaults(tmp_path):
    """Bad verdict, scope, duration and finding scalars must not drop /api/summary, /api/calls or /api/call
    and must fall back to NeedsReview, unclear, None and empty strings; unhashable lists crashed the routes."""
    audits_lines = [
        json.dumps({"call_id": "bad_verdict", "verdict": []}),
        json.dumps({"call_id": "bad_scope", "verdict": "Pass", "scope": ["x"]}),
        json.dumps({"call_id": "bad_dur", "verdict": "Fatal"}),
        json.dumps(
            {
                "call_id": "bad_findings",
                "verdict": "Pass",
                "findings": [{"parameter": 1, "status": [2], "reason": {}}],
            }
        ),
        json.dumps({"call_id": "good_1", "verdict": "Pass", "scope": "bl_collection"}),
    ]
    calls_lines = [
        json.dumps({"call_id": "bad_dur", "duration_sec": [30]}),
        json.dumps({"call_id": "good_1", "duration_sec": 30}),
    ]
    srv, thread = _server_with(tmp_path, calls_lines, audits_lines)
    try:
        status, body = get_json(srv, "/api/summary")
        assert status == 200
        assert body["verdicts"]["NeedsReview"] == 1 and body["verdicts"]["Pass"] == 3
        assert body["scopes"] == {"unclear": 4, "bl_collection": 1}
        status, rows = get_json(srv, "/api/calls")
        assert status == 200
        by_id = {r["call_id"]: r for r in rows}
        assert by_id["bad_verdict"]["verdict"] == "NeedsReview"
        assert by_id["bad_scope"]["scope"] == "unclear"
        assert by_id["bad_dur"]["duration_sec"] is None
        status, body = get_json(srv, "/api/call/bad_findings")
        assert status == 200
        assert body["audit"]["findings"][0] == {"parameter": "", "status": "", "reason": ""}
    finally:
        _stop(srv, thread)


def test_first_fault_picks_earliest_onset_then_fatal():
    """first_fault picks the earliest failing onset, even when a later finding is Fatal; severity-first picks turn 8."""
    audit = {
        "findings": [
            {"parameter": "later_fatal", "status": "Fatal", "onset_turn": 8},
            {"parameter": "early", "status": "NonFatal", "onset_turn": 2},
        ]
    }
    assert web._first_fault(audit) == {"parameter": "early", "status": "NonFatal", "reason": None}


def test_first_fault_breaks_ties_by_fatal_before_nonfatal():
    """Two failing findings on the same turn must pick the Fatal one; the tie break decides what the UI calls out."""
    audit = {
        "findings": [
            {"parameter": "nf", "status": "NonFatal", "onset_turn": 5},
            {"parameter": "ftl", "status": "Fatal", "onset_turn": 5},
        ]
    }
    assert web._first_fault(audit)["parameter"] == "ftl"


def test_first_fault_put_findings_without_onset_last():
    """A failing finding with no onset must lose to one with an onset and serialise its None reason as null."""
    audit = {
        "findings": [
            {"parameter": "no_onset", "status": "Fatal"},
            {"parameter": "late", "status": "NonFatal", "onset_turn": 9},
        ]
    }
    assert web._first_fault(audit) == {"parameter": "late", "status": "NonFatal", "reason": None}


def test_calls_sorts_unknown_durations_last(tmp_path):
    """NaN, inf and missing durations sort as unknown, last per verdict, serialised as null; a bad compare raises."""
    audits = "\n".join(json.dumps({"call_id": c, "verdict": "Pass"}) for c in ("nan", "inf", "ok_10", "ok_100"))
    calls = "\n".join(
        json.dumps(c)
        for c in (
            {"call_id": "nan", "duration_sec": float("nan")},
            {"call_id": "inf", "duration_sec": float("inf")},
            {"call_id": "ok_10", "duration_sec": 10},
            {"call_id": "ok_100", "duration_sec": 100},
        )
    )
    (tmp_path / "audits.jsonl").write_text(audits + "\n", encoding="utf-8")
    (tmp_path / "calls.jsonl").write_text(calls + "\n", encoding="utf-8")
    srv = web.make_server(0, tmp_path)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        status, rows = get_json(srv, "/api/calls")
        assert status == 200
        assert [r["duration_sec"] for r in rows] == [100, 10, None, None]
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def test_host_allowlist_blocks_rebound_dns(server):
    """A non-loopback Host header must get 403, forbidden host, on every route; a rebound name would read the API."""
    for path in ("/api/calls", "/api/summary", "/api/call/web_01"):
        status, ctype, body = get(server, path, host="attacker.example:8765")
        assert status == 403
        assert "application/json" in ctype
        assert json.loads(body) == {"error": "forbidden host"}
    assert get(server, "/", host="attacker.example:8765")[0] == 403


def test_host_junk_after_ipv6_literal_is_refused(server):
    """Host values with anything after ] but a port must get 403; the old parser read up to ] and let
    [::1].attacker.example in as loopback."""
    for host in ("[::1].attacker.example", "[::1]:8765junk", "[::1]junk", "[::1]:"):
        status, ctype, body = get(server, "/api/calls", host=host)
        assert status == 403
        assert "application/json" in ctype
        assert json.loads(body) == {"error": "forbidden host"}


def test_duplicate_host_headers_are_refused(server):
    """Two Host headers must get 403; with the old single-value check, a duplicate could slip a spoofed
    header past a proxy that forwards both."""
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        conn.putrequest("GET", "/api/calls", skip_host=True)
        conn.putheader("Host", "127.0.0.1")
        conn.putheader("Host", "attacker.example")
        conn.endheaders()
        resp = conn.getresponse()
        assert resp.status == 403
        assert "application/json" in resp.getheader("Content-Type")
        assert json.loads(resp.read()) == {"error": "forbidden host"}
    finally:
        conn.close()


def test_missing_host_header_gets_403(server):
    """A request without any Host header must get 403 ("forbidden host"); a bare client could still read the API."""
    status, _, body = get(server, "/api/calls", host="")
    assert status == 403
    assert json.loads(body) == {"error": "forbidden host"}


def test_loopback_hosts_pass_the_allowlist(server):
    """127.0.0.1, localhost and [::1] remain in, in the bare form and with a port; the browser must keep working."""
    for host in ("127.0.0.1", "localhost", "[::1]", "127.0.0.1:8765", "LOCALHOST"):
        assert web.host_allowed(host) is True
    assert web.host_allowed("attacker.example") is False
    for host in ("127.0.0.1", "localhost", "127.0.0.1:8765", "[::1]:8765"):
        status, _, _ = get(server, "/api/calls", host=host)
        assert status == 200


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
