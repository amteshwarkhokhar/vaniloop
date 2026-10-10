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
import re
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from vaniloop import console, fix, gate, jev, llm, prove, rules
from vaniloop.jsonl import dedupe_by_call_id, read_jsonl

ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = Path(__file__).resolve().parent / "web"
HOST = "127.0.0.1"
DEFAULT_PORT = 8765
IST = timezone(timedelta(hours=5, minutes=30))
VERDICTS = ("Fatal", "NonFatal", "NeedsReview", "Pass")
SEVERITY = {v: i for i, v in enumerate(VERDICTS)}
ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
LIST_FIELDS = ("findings", "turns", "silences")
STAGES = ("ingest", "audit", "issues", "fix", "gate", "review")
JUDGES = ("jev", "gemini")
REVIEW_TEXT_CHARS = 200
DATA_FILES = (
    "calls",
    "ledgers",
    "audits",
    "audits_gemini",
    "causes",
    "patches",
    "gates",
    "replays",
    "costs",
    "jev_review",
)
DATA_FILE_EXT = {"causes": "json", "patches": "json"}
RUNNER_LOCK = threading.Lock()
MAX_BODY = 30 * 1024 * 1024
JOB_PATH = re.compile(r"^/api/jobs/([^/]+)/(log|cancel)$")
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}


def data_dir() -> Path:
    """The folder with the run files: VANILOOP_DATA when set, else runs/."""
    env = os.environ.get("VANILOOP_DATA")
    return Path(env) if env else ROOT / "runs"


def read_only(d: Path) -> bool:
    """True when the data folder is inside <repo>/.cache/ (the demo freeze): writes are refused."""
    return Path(d).resolve().is_relative_to((ROOT / ".cache").resolve())


def console_info(d: Path, runner) -> dict:
    """GET /api/console: read-only flag, folder name, the busy job and the stage list."""
    return {
        "read_only": read_only(d),
        "data_dir_name": Path(d).resolve().name,
        "busy_job_id": runner.busy_job_id(),
        "stages": console.describe(),
    }


def clean(value):
    """Replace NaN and infinity with None, so every response is strict JSON that JSON.parse accepts."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v) for v in value]
    return value


def _finite(value) -> bool:
    """True for a real, finite int or float. A huge int that cannot become a float (OverflowError) is False."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _count(value) -> int:
    """len() of a list; a wrong type counts as empty."""
    return len(value) if isinstance(value, list) else 0


def _list(value) -> list:
    """The value when it is a list; a wrong type is an empty list."""
    return value if isinstance(value, list) else []


def _valid(rows: list[dict]) -> list[dict]:
    """Keep rows a route can use, and make their fields safe: a non-empty string call_id, list-shaped list
    fields, and verdict, scope, duration and finding scalars the routes can hash and compare."""
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        cid = row.get("call_id")
        if not isinstance(cid, str) or not cid:
            continue
        for name in LIST_FIELDS:
            value = row.get(name)
            if value is not None and not isinstance(value, list):
                row[name] = []
        if "verdict" in row and row["verdict"] not in VERDICTS:
            row["verdict"] = "NeedsReview"
        if "scope" in row and row["scope"] is not None and not isinstance(row["scope"], str):
            row["scope"] = "unclear"
        duration = row.get("duration_sec")
        if duration is not None and not _finite(duration):
            row["duration_sec"] = None
        for finding in row.get("findings") or []:
            if isinstance(finding, dict):
                for name in ("parameter", "status", "reason"):
                    if name in finding and not isinstance(finding[name], str):
                        finding[name] = ""
        out.append(row)
    return out


def _audits(d: Path) -> list[dict]:
    return dedupe_by_call_id(_valid(read_jsonl(d / "audits.jsonl")))


def _calls(d: Path) -> dict[str, dict]:
    return {r["call_id"]: r for r in dedupe_by_call_id(_valid(read_jsonl(d / "calls.jsonl")))}


def _first_fault(audit: dict) -> dict | None:
    """The showing fault: earliest failing onset, ties Fatal first, no-onset last. Unknown if no finding fails."""
    findings = [f for f in audit.get("findings") or [] if isinstance(f, dict)]
    failing = [f for f in findings if f.get("status") in ("Fatal", "NonFatal")]
    pool = failing or [f for f in findings if f.get("status") == "Unknown"]
    if not pool:
        return None

    def order(f: dict):
        onset = f.get("onset_turn")
        has_onset = _finite(onset)
        return (not has_onset, float(onset) if has_onset else 0, SEVERITY.get(f.get("status"), len(SEVERITY)))

    pool.sort(key=order)
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
        if _finite(cost) and cost >= 0:
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
    rows.sort(key=lambda r: (SEVERITY.get(r["verdict"], len(SEVERITY)), *duration_key(r["duration_sec"])))
    return rows


def duration_key(value):
    """Sort key for duration_sec: finite numbers descending, anything else unknown and last (it serialises as null)."""
    if not _finite(value):
        return (1, 0)
    return (0, -float(value))


def host_name(host: str | None) -> str | None:
    """The host part of a Host header with any port removed, including IPv6 literals like [::1]:8765."""
    if not host:
        return None
    if host.startswith("["):
        end = host.find("]")
        if end == -1:
            return None
        rest = host[end + 1 :]
        if rest and not (rest.startswith(":") and rest[1:].isdigit()):
            return None
        return host[1:end]
    name, sep, port = host.rpartition(":")
    if not sep or not port.isdigit() or not name:
        return host
    return name


def host_allowed(host: str | None) -> bool:
    """True only when the Host header names loopback, so a rebound DNS name cannot read the API."""
    name = host_name(host)
    return name is not None and name.lower() in ALLOWED_HOSTS


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


_MODEL_ID = re.compile(r"^[A-Za-z0-9._/:-]{1,80}$")
_SECRET_PREFIX = re.compile(r"^(sk|pk|rk|ghp|gho|github_pat|xox[abp]|AIza|ya29|Bearer|key|token)[-_.]", re.IGNORECASE)


def _safe_model(name: str) -> str:
    """A model id for the config view. Anything that looks like a URL, a path, a token or a credential is hidden."""
    if not _MODEL_ID.match(name) or any(bad in name for bad in ("://", "?", "=", "@", "..", "//")):
        return "(hidden)"
    if name[0] in "/.~" or name.count("/") > 2 or _SECRET_PREFIX.match(name):
        return "(hidden)"
    if any(len(part) >= 32 for part in re.split(r"[/:]", name)):
        return "(hidden)"
    return name


def _leaf(value, source: str) -> dict:
    """One config value with the module and constant (or file) that sets it."""
    return {"value": value, "source": source}


def _stamp(path: Path) -> str:
    stamp = datetime.fromtimestamp(path.stat().st_mtime, IST)
    return f"{stamp:%H:%M} {stamp.day:02d} {stamp:%b}"


def active_judge(d: Path) -> str:
    """Jev graded when audits_gemini.jsonl exists: that file is the kept Gemini run, audits.jsonl then holds Jev's."""
    return "jev" if (d / "audits_gemini.jsonl").exists() else "gemini"


def _data_files(d: Path) -> list[dict]:
    out = []
    for name in DATA_FILES:
        path = d / f"{name}.{DATA_FILE_EXT.get(name, 'jsonl')}"
        if not path.is_file():
            out.append({"name": path.name, "rows": None, "updated_ist": None})
            continue
        if path.suffix == ".jsonl":
            rows = len(read_jsonl(path))
        else:
            rows = _count(read_json_doc(path).get(name))
        out.append({"name": path.name, "rows": rows, "updated_ist": _stamp(path)})
    return out


def read_json_doc(path: Path) -> dict:
    """A .json run file as a dict; a missing, unreadable or non-object file gives {}."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def config(d: Path) -> dict:
    """Every configurable part, each value with its source. Model names only: no key, no URL."""
    active = active_judge(d)
    gemini_file, audits_file = (d / "audits_gemini.jsonl").exists(), (d / "audits.jsonl").exists()
    available = [j for j, ok in (("jev", gemini_file and audits_file), ("gemini", gemini_file or audits_file)) if ok]
    rows = [r for r in gates(d) if isinstance(r, dict)]
    last = rows[-1] if rows else None
    last_run = None
    if last is not None:
        last_run = {
            "n": _leaf(last.get("n_calls"), "gates.jsonl n_calls"),
            "k": _leaf(last.get("k_seeds"), "gates.jsonl k_seeds"),
            "holdout": _leaf(last.get("holdout") is True, "gates.jsonl holdout"),
            "screen": _leaf(last.get("screened") is not None, "gates.jsonl screened"),
        }
    return {
        "judge": {
            "active": _leaf(active, "audits_gemini.jsonl present"),
            "available": _leaf(available, "audits.jsonl, audits_gemini.jsonl present"),
            "jev_model": _leaf(_safe_model(llm.jev_model()), "llm.jev_model"),
            "jev_version": _leaf(jev.JEV_VERSION, "jev.JEV_VERSION"),
            "structure_model": _leaf(_safe_model(llm.model_name()), "llm.model_name"),
            "thresholds": {
                "question_fits": {
                    "true_at": _leaf(jev.Q_TRUE_AT, "jev.Q_TRUE_AT"),
                    "false_at": _leaf(jev.Q_FALSE_AT, "jev.Q_FALSE_AT"),
                },
                "option_fits": {
                    "true_at": _leaf(jev.OPT_TRUE_AT, "jev.OPT_TRUE_AT"),
                    "false_at": _leaf(jev.OPT_FALSE_AT, "jev.OPT_FALSE_AT"),
                },
                "product_match": {
                    "true_at": _leaf(jev.PM_TRUE_AT, "jev.PM_TRUE_AT"),
                    "false_at": _leaf(jev.PM_FALSE_AT, "jev.PM_FALSE_AT"),
                },
                "name_check_drop_at": _leaf(jev.N_DROP_AT, "jev.N_DROP_AT"),
            },
        },
        "audit": {
            "frustrated_p": _leaf(rules.FRUSTRATED_P, "rules.FRUSTRATED_P"),
            "loop_irritation_turns": _leaf(rules.LOOP_IRRITATION_TURNS, "rules.LOOP_IRRITATION_TURNS"),
            "dead_air_sec": _leaf(rules.DEAD_AIR_SEC, "rules.DEAD_AIR_SEC"),
            "timing": {
                "min_coverage": _leaf(rules.TIMING_MIN_COVERAGE, "rules.TIMING_MIN_COVERAGE"),
                "max_back_to_back": _leaf(rules.TIMING_MAX_BACK_TO_BACK, "rules.TIMING_MAX_BACK_TO_BACK"),
                "max_zero_turns": _leaf(rules.TIMING_MAX_ZERO_TURNS, "rules.TIMING_MAX_ZERO_TURNS"),
            },
        },
        "fix_loop": {
            "max_attempts": _leaf(fix.MAX_ATTEMPTS, "fix.MAX_ATTEMPTS"),
            "alpha": _leaf(gate.DEFAULT_ALPHA, "gate.DEFAULT_ALPHA"),
            "alpha_per_attempt": _leaf(
                gate.DEFAULT_ALPHA / prove.MAX_ATTEMPTS, "gate.DEFAULT_ALPHA / prove.MAX_ATTEMPTS"
            ),
            "screen_seeds": _leaf(prove.SCREEN_SEEDS, "prove.SCREEN_SEEDS"),
            "screen_min_fails": _leaf(prove.SCREEN_MIN_FAILS, "prove.SCREEN_MIN_FAILS"),
            "min_calls": _leaf(gate.MIN_CALLS, "gate.MIN_CALLS"),
            "last_run": last_run,
        },
        "data": {"folder": _leaf(d.name, "web.data_dir"), "files": _data_files(d)},
    }


def _audits_for(d: Path, judge: str | None) -> tuple[str, list[dict]]:
    """The judge label and its audit rows. Jev is audits.jsonl when audits_gemini.jsonl exists. Gemini is that
    file, or audits.jsonl alone when no Jev run was kept. A judge with no file gives no rows."""
    active = active_judge(d)
    judge = judge if judge in JUDGES else active
    if judge == "gemini" and active == "jev":
        path = d / "audits_gemini.jsonl"
    elif judge == active:
        path = d / "audits.jsonl"
    else:
        return judge, []
    return judge, dedupe_by_call_id(_valid(read_jsonl(path)))


def stage_ingest(d: Path) -> dict:
    rows = list(_calls(d).values())
    secs = sorted(float(r["duration_sec"]) for r in rows if _finite(r.get("duration_sec")))

    def pick(q: float) -> float | None:
        return secs[min(len(secs) - 1, math.ceil(q * len(secs)) - 1)] if secs else None

    return {
        "calls": len(rows),
        "with_audio": sum(1 for r in rows if r.get("audio_path")),
        "minutes_total": round(sum(secs) / 60, 2),
        "median_sec": pick(0.5),
        "p90_sec": pick(0.9),
        "short_calls": sum(1 for s in secs if s < rules.SHORT_CALL_SEC),
    }


def stage_audit(d: Path, judge: str | None) -> dict:
    judge, audits = _audits_for(d, judge)
    verdicts = dict.fromkeys(VERDICTS, 0)
    scopes: dict[str, int] = {}
    by_parameter: dict[str, dict[str, int]] = {}
    flags: dict[str, int] = {}
    for a in audits:
        if a.get("verdict") in verdicts:
            verdicts[a["verdict"]] += 1
        scope = a.get("scope") or "unclear"
        scopes[scope] = scopes.get(scope, 0) + 1
        for f in a.get("findings") or []:
            if isinstance(f, dict) and f.get("parameter"):
                counts = by_parameter.setdefault(f["parameter"], {})
                counts[f.get("status") or ""] = counts.get(f.get("status") or "", 0) + 1
        for flag in _list(a.get("flags")):
            if isinstance(flag, str):
                flags[flag] = flags.get(flag, 0) + 1
    return {
        "judge": judge,
        "total": len(audits),
        "verdicts": verdicts,
        "scopes": scopes,
        "by_parameter": by_parameter,
        "flags": flags,
    }


def stage_issues(d: Path) -> dict:
    keys = ("cause_id", "name", "parameter", "status", "type", "owner", "priority")
    out = []
    for c in causes(d)["causes"]:
        if not isinstance(c, dict):
            continue
        row = {k: c.get(k) for k in keys}
        row["fatal"] = _count(c.get("fatal_calls"))
        row["nonfatal"] = _count(c.get("nonfatal_calls"))
        row["symptoms"] = _count(c.get("symptom_calls"))
        out.append(row)
    return {"causes": out}


def _patch_row(p: dict, *, history: bool = False) -> dict:
    row = {k: p.get(k) for k in ("cause_id", "attempt", "old_text", "new_text", "rationale")}
    if history:
        row["gate_decision"] = p.get("gate_decision", p.get("decision"))
    return row


def stage_fix(d: Path) -> dict:
    doc = read_json_doc(d / "patches.json")
    patches, history = doc.get("patches"), doc.get("history")
    return {
        "patches": [_patch_row(p) for p in patches if isinstance(p, dict)] if isinstance(patches, list) else [],
        "history": [_patch_row(p, history=True) for p in history if isinstance(p, dict)]
        if isinstance(history, list)
        else [],
        "tickets": [
            c["cause_id"]
            for c in causes(d)["causes"]
            if isinstance(c, dict) and c.get("type") in fix.TICKET_TYPES and c.get("cause_id")
        ],
    }


def stage_gate(d: Path) -> dict:
    return {"gates": gates(d), "screens": read_jsonl(d / "screens.jsonl")}


def _cut(value):
    """Cut every string in a review row to REVIEW_TEXT_CHARS."""
    if isinstance(value, str):
        return value[:REVIEW_TEXT_CHARS]
    if isinstance(value, list):
        return [_cut(v) for v in value]
    if isinstance(value, dict):
        return {k: _cut(v) for k, v in value.items()}
    return value


def _outcome(label: dict) -> str:
    """both_right, jev_only, gemini_only or both_wrong from the label's jev_ok and gemini_ok. A label with no
    boolean truth, jev_ok or gemini_ok is unscored."""
    jev_ok, gemini_ok = label.get("jev_ok"), label.get("gemini_ok")
    if not isinstance(label.get("truth"), bool) or not isinstance(jev_ok, bool) or not isinstance(gemini_ok, bool):
        return "unscored"
    return {(True, True): "both_right", (True, False): "jev_only", (False, True): "gemini_only"}.get(
        (jev_ok, gemini_ok), "both_wrong"
    )


def stage_review(d: Path) -> dict:
    """Jev vs Gemini review rows. totals count labels by outcome per kind. accuracy is per kind and per group
    (control or contested): labels where each judge was right, and the labels scored (n)."""
    items = [r for r in read_jsonl(d / "jev_review.jsonl") if isinstance(r, dict)]
    totals: dict[str, dict[str, int]] = {}
    accuracy: dict[str, dict[str, dict[str, int]]] = {}
    for row in items:
        kind = row.get("kind") if isinstance(row.get("kind"), str) else "unknown"
        group = "control" if row.get("control") is True else "contested"
        acc = accuracy.setdefault(kind, {}).setdefault(group, {"jev_right": 0, "gemini_right": 0, "n": 0})
        for label in row.get("labels") if isinstance(row.get("labels"), list) else []:
            if not isinstance(label, dict):
                continue
            outcome = _outcome(label)
            totals.setdefault(kind, {})
            totals[kind][outcome] = totals[kind].get(outcome, 0) + 1
            if outcome != "unscored":
                acc["n"] += 1
                acc["jev_right"] += label["jev_ok"]
                acc["gemini_right"] += label["gemini_ok"]
    return {"items": [_cut(r) for r in items], "totals": totals, "accuracy": accuracy}


def stage(d: Path, name: str, judge: str | None = None) -> dict | None:
    """The result of one pipeline stage, or None for an unknown name."""
    if name == "ingest":
        return stage_ingest(d)
    if name == "audit":
        return stage_audit(d, judge)
    if name == "issues":
        return stage_issues(d)
    if name == "fix":
        return stage_fix(d)
    if name == "gate":
        return stage_gate(d)
    if name == "review":
        return stage_review(d)
    return None


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
        """Serve a file from STATIC_DIR. A NUL byte is a bad request; anything resolving outside is refused with 403."""
        if "\x00" in rel:
            self._json({"error": "bad request"}, 400)
            return
        base = STATIC_DIR.resolve()
        target = (base / rel).resolve()
        if not target.is_relative_to(base) or ".." in Path(rel).parts:
            self._json({"error": "forbidden"}, 403)
        elif not target.is_file():
            self._json({"error": "not found"}, 404)
        else:
            ctype = MIME.get(target.suffix) or mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            self._send(200, target.read_bytes(), ctype)

    runner: console.JobRunner | None = None

    def _runner(self) -> console.JobRunner:
        with RUNNER_LOCK:
            if self.runner is None:
                type(self).runner = console.JobRunner(self.data, read_only(self.data))
        return self.runner

    def _host_ok(self) -> bool:
        hosts = self.headers.get_all("Host") or []
        if len(hosts) != 1 or not host_allowed(hosts[0]):
            self._json({"error": "forbidden host"}, 403)
            return False
        return True

    def _origin_ok(self) -> bool:
        """The Origin header must name this server: an allowed host with this server's port."""
        origins = self.headers.get_all("Origin") or []
        try:
            u = urlsplit(origins[0]) if len(origins) == 1 else None
            ok = (
                bool(u)
                and u.scheme == "http"
                and u.hostname in ALLOWED_HOSTS
                and u.port == self.server.server_address[1]
            )
        except ValueError:
            ok = False
        return ok

    def _read_json(self):
        """For POST and PUT: run the security checks, then return the parsed JSON object, or None after an error
        reply. Order: host, origin, read-only folder, content type, body size, JSON shape."""
        self.close_connection = True  # an early refusal leaves the body unread, so do not reuse the socket
        if not self._host_ok():
            return None
        if not self._origin_ok():
            self._json({"error": "forbidden origin"}, 403)
            return None
        if read_only(self.data):
            self._json({"error": "read-only data folder"}, 403)
            return None
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            self._json({"error": "content type must be application/json"}, 415)
            return None
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            length = -1
        if length < 0:
            self._json({"error": "bad content length"}, 400)
            return None
        if length > MAX_BODY:
            self._json({"error": "body too large"}, 413)
            return None
        try:
            body = json.loads(self.rfile.read(length) or b"null")
        except ValueError:
            self._json({"error": "bad json"}, 400)
            return None
        if not isinstance(body, dict):
            self._json({"error": "body must be a JSON object"}, 400)
            return None
        return body

    def do_POST(self) -> None:  # noqa: N802
        body = self._read_json()
        if body is None:
            return
        path = unquote(urlsplit(self.path).path)
        try:
            d = self.data
            if path == "/api/jobs":
                spec, args = console.validate(d, body.get("stage"), body.get("args"), body.get("confirm"))
                self._json({"job": self._runner().start(spec["id"], args)}, 202)
            elif (m := JOB_PATH.match(path)) and m.group(2) == "cancel":
                job = self._runner().cancel(m.group(1))
                self._json({"job": job} if job else {"error": "unknown job"}, 200 if job else 404)
            elif path == "/api/submit":
                self._json(console.create_submission(d, body), 201)
            elif path == "/api/tune":
                self._json(self._tune(body))
            else:
                self._json({"error": "not found"}, 404)
        except console.ConsoleError as e:
            self._json(e.payload, e.status)

    def _tune(self, body: dict):
        """POST /api/tune through vaniloop.tune.score. Imported late: the module may not exist yet."""
        from vaniloop import tune

        thresholds = body.get("thresholds")
        if not isinstance(thresholds, dict):
            raise console.ConsoleError(400, {"error": "thresholds must be an object", "field": "thresholds"})
        if not (self.data / "jev_raw.jsonl").exists():
            raise console.ConsoleError(409, {"error": "no raw Jev answers: run judge"})
        try:
            return tune.score(self.data, thresholds)
        except tune.NoRaw:
            raise console.ConsoleError(409, {"error": "no raw Jev answers: run judge"}) from None
        except (ValueError, TypeError, KeyError) as e:
            raise console.ConsoleError(
                400, {"error": str(e)[:200] or "bad value", "field": getattr(e, "field", None)}
            ) from None

    def do_PUT(self) -> None:  # noqa: N802
        body = self._read_json()
        if body is None:
            return
        if unquote(urlsplit(self.path).path) != "/api/rubric":
            self._json({"error": "not found"}, 404)
            return
        from vaniloop import rubric

        path = self.data / "rubric.json"
        try:
            rubric.save(path, body.get("params") or {}, body.get("questions") or {})
            self._json(rubric.describe(path))
        except rubric.RubricError as e:
            self._json({"error": str(e)[:300], "field": getattr(e, "field", None)}, 400)

    def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        if not self._host_ok():
            return
        parts = urlsplit(self.path)
        path = unquote(parts.path)
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
        elif path == "/api/config":
            self._json(config(d))
        elif path.startswith("/api/stage/"):
            judge = parse_qs(parts.query, keep_blank_values=True).get("judge", [None])[0]
            if judge is not None and judge not in JUDGES:
                self._json({"error": "unknown judge"}, 400)
                return
            result = stage(d, path[len("/api/stage/") :], judge)
            if result is None:
                self._json({"error": "unknown stage"}, 404)
            else:
                self._json(result)
        elif path == "/api/rubric":
            from vaniloop import rubric

            self._json(rubric.describe(d / "rubric.json"))
        elif path == "/api/submit":
            self._json(console.submissions(d))
        elif path.startswith("/api/submit/"):
            out = console.submission(d, path[len("/api/submit/") :], self._runner())
            self._json(out if out else {"error": "unknown submission"}, 200 if out else 404)
        elif path == "/api/console":
            self._json(console_info(d, self._runner()))
        elif path == "/api/jobs":
            self._json({"jobs": self._runner().list()})
        elif (m := JOB_PATH.match(path)) and m.group(2) == "log":
            try:
                start = int(parse_qs(parts.query).get("from", ["0"])[0])
            except ValueError:
                start = 0
            out = self._runner().log(m.group(1), start)
            self._json(out if out else {"error": "unknown job"}, 200 if out else 404)
        elif path == "/dash":
            self._static("dash.html")
        else:
            self._json({"error": "not found"}, 404)


def make_server(port: int = DEFAULT_PORT, data: Path | str | None = None) -> ThreadingHTTPServer:
    """A server bound to 127.0.0.1 only. Port 0 picks a free port."""
    folder = Path(data) if data else data_dir()
    runner = console.JobRunner(folder, read_only(folder))
    handler = type("BoundHandler", (Handler,), {"data": folder, "runner": runner})
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
