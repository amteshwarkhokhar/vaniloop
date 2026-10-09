"""Local web server for the vaniloop UI: a JSON API over the run files plus the static page.

Stdlib only. It binds to 127.0.0.1 and reads the data folder on every request, so a new audit shows
up on reload. Routes are listed in docs/ui_spec.md.

Run: uv run python -m vaniloop.web [--port 8765]
"""

from __future__ import annotations

import argparse
import json
import math
import mimetypes
import os
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from vaniloop.jsonl import dedupe_by_call_id, read_jsonl

ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = Path(__file__).resolve().parent / "web"
HOST = "127.0.0.1"
DEFAULT_PORT = 8765
IST = timezone(timedelta(hours=5, minutes=30))
VERDICTS = ("Fatal", "NonFatal", "NeedsReview", "Pass")
SEVERITY = {v: i for i, v in enumerate(VERDICTS)}
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}


def data_dir() -> Path:
    """The folder with the run files: VANILOOP_DATA when set, else runs/."""
    env = os.environ.get("VANILOOP_DATA")
    return Path(env) if env else ROOT / "runs"


def clean(value):
    """Replace NaN and infinity with None, so every response is strict JSON that JSON.parse accepts."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v) for v in value]
    return value


def _audits(d: Path) -> list[dict]:
    return dedupe_by_call_id(read_jsonl(d / "audits.jsonl"))


def _calls(d: Path) -> dict[str, dict]:
    return {r["call_id"]: r for r in dedupe_by_call_id(read_jsonl(d / "calls.jsonl"))}


def _first_fault(audit: dict) -> dict | None:
    """The finding to show on a call row: the worst failing finding, earliest onset first. Unknown if none fails."""
    findings = [f for f in audit.get("findings") or [] if isinstance(f, dict)]
    failing = [f for f in findings if f.get("status") in ("Fatal", "NonFatal")]
    pool = failing or [f for f in findings if f.get("status") == "Unknown"]
    if not pool:
        return None
    pool.sort(key=lambda f: (f.get("status") != "Fatal", f.get("onset_turn") is None, f.get("onset_turn") or 0))
    f = pool[0]
    return {"parameter": f.get("parameter"), "status": f.get("status"), "reason": f.get("reason")}


def summary(d: Path) -> dict:
    audits = _audits(d)
    verdicts = dict.fromkeys(VERDICTS, 0)
    scopes: dict[str, int] = {}
    for a in audits:
        if a.get("verdict") in verdicts:
            verdicts[a["verdict"]] += 1
        scope = a.get("scope") or "unclear"
        scopes[scope] = scopes.get(scope, 0) + 1
    audits_file = d / "audits.jsonl"
    updated = None
    if audits_file.exists():
        stamp = datetime.fromtimestamp(audits_file.stat().st_mtime, IST)
        updated = f"{stamp:%H:%M} {stamp.day:02d} {stamp:%b}"
    return {
        "total": len(audits),
        "verdicts": verdicts,
        "scopes": scopes,
        "updated_ist": updated,
        "spend_usd": _spend(d),
    }


def _spend(d: Path) -> float | None:
    """Sum of valid cost_usd in costs.jsonl. None when the file is missing, so the UI shows no figure."""
    path = d / "costs.jsonl"
    if not path.exists():
        return None
    total = 0.0
    for row in read_jsonl(path):
        cost = row.get("cost_usd")
        if isinstance(cost, bool) or not isinstance(cost, (int, float)):
            continue
        if math.isfinite(cost) and cost >= 0:
            total += float(cost)
    return round(total, 6)


def calls(d: Path) -> list[dict]:
    durations = {cid: c.get("duration_sec") for cid, c in _calls(d).items()}
    rows = []
    for a in _audits(d):
        cid = a["call_id"]
        rows.append(
            {
                "call_id": cid,
                "verdict": a.get("verdict"),
                "duration_sec": durations.get(cid),
                "scope": a.get("scope"),
                "first_fault": _first_fault(a),
            }
        )
    rows.sort(key=lambda r: (SEVERITY.get(r["verdict"], len(SEVERITY)), -(r["duration_sec"] or 0)))
    return rows


def call_detail(d: Path, call_id: str) -> dict | None:
    call = _calls(d).get(call_id)
    audit = next((a for a in _audits(d) if a["call_id"] == call_id), None)
    if call is None and audit is None:
        return None
    return {"call": call, "audit": audit}


def causes(d: Path) -> dict:
    """causes.json as written. The older list format is wrapped. A missing or unreadable file gives no causes."""
    path = d / "causes.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"causes": []}
    if isinstance(raw, list):
        return {"causes": raw}
    if isinstance(raw, dict) and isinstance(raw.get("causes"), list):
        return raw
    return {"causes": []}


def gates(d: Path) -> list[dict]:
    return read_jsonl(d / "gates.jsonl")


class Handler(BaseHTTPRequestHandler):
    server_version = "vaniloop-web"
    data: Path = Path("runs")

    def log_message(self, format, *args):  # noqa: A002 - signature fixed by BaseHTTPRequestHandler
        """Keep the terminal quiet."""

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, status: int = 200) -> None:
        body = json.dumps(clean(payload), ensure_ascii=False, allow_nan=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _static(self, rel: str) -> None:
        """Serve a file from STATIC_DIR. Anything that resolves outside it is refused with 403."""
        base = STATIC_DIR.resolve()
        target = (base / rel).resolve()
        if not target.is_relative_to(base) or ".." in Path(rel).parts or "\x00" in rel:
            self._json({"error": "forbidden"}, 403)
        elif not target.is_file():
            self._json({"error": "not found"}, 404)
        else:
            ctype = MIME.get(target.suffix) or mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self._send(200, target.read_bytes(), ctype)

    def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        path = unquote(urlsplit(self.path).path)
        d = self.data
        if path in ("/", "/index.html"):
            self._static("index.html")
        elif path.startswith("/static/"):
            self._static(path[len("/static/") :])
        elif path == "/api/summary":
            self._json(summary(d))
        elif path == "/api/calls":
            self._json(calls(d))
        elif path.startswith("/api/call/"):
            detail = call_detail(d, path[len("/api/call/") :])
            if detail is None:
                self._json({"error": "unknown call id"}, 404)
            else:
                self._json(detail)
        elif path == "/api/causes":
            self._json(causes(d))
        elif path == "/api/gates":
            self._json(gates(d))
        else:
            self._json({"error": "not found"}, 404)


def make_server(port: int = DEFAULT_PORT, data: Path | str | None = None) -> ThreadingHTTPServer:
    """A server bound to 127.0.0.1 only. Port 0 picks a free port."""
    handler = type("BoundHandler", (Handler,), {"data": Path(data) if data else data_dir()})
    return ThreadingHTTPServer((HOST, port), handler)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Serve the vaniloop UI on 127.0.0.1.")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args(argv)
    server = make_server(args.port)
    print(
        f"vaniloop UI: http://{HOST}:{server.server_address[1]}  (data: {server.RequestHandlerClass.data})", flush=True
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
