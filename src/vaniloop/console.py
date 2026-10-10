"""Console backend: validated pipeline jobs run as subprocesses. Contract: docs/console_api.md.

The server never passes a client string to a shell. Client args are validated into plain ints, bools and
known ids first. Then `build_argv` makes an argv list from those values only.
"""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import signal
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from vaniloop import llm
from vaniloop.jsonl import append_jsonl, read_jsonl

PROMPT_PATH = Path(__file__).resolve().parents[2] / "data" / "reference" / "vani_prompt_sarvam.md"
IST = timezone(timedelta(hours=5, minutes=30))
MAX_JOBS_LISTED = 50
MAX_CALL_IDS = 1000
ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
JOB_ID_RE = re.compile(r"^j[0-9a-f]{12}$")
SID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
SECRET_RE = re.compile(
    r"sk-[A-Za-z0-9_\-]+|AIza[0-9A-Za-z_\-]+|Bearer\s+\S+|(?:api[_-]?key)\s*[=:]\s*\S+", re.IGNORECASE
)


def _arg(name, type_, required=False, default=None, **extra):
    return {"name": name, "type": type_, "required": required, "default": default, **extra}


# id, label, paid, args. Int ranges are (low, high); they are not part of the public description.
STAGES: list[dict] = [
    {"id": "audit", "label": "Audit calls", "paid": True,
     "args": [_arg("calls", "call_ids"), _arg("limit", "int")]},
    {"id": "judge", "label": "Judge with Jev", "paid": False, "args": [_arg("calls", "call_ids")]},
    {"id": "regrade", "label": "Regrade", "paid": False, "args": []},
    {"id": "issues", "label": "Group issues", "paid": False, "args": []},
    {"id": "agreement", "label": "Judge agreement", "paid": False, "args": []},
    {"id": "fix", "label": "Write fixes", "paid": True,
     "args": [_arg("top", "int", default=3), _arg("holdout", "bool", default=True)]},
    {"id": "prove", "label": "Prove a fix", "paid": True,
     "args": [_arg("cause", "str", required=True), _arg("n", "int", default=8), _arg("k", "int", default=3),
              _arg("attempt", "int", default=1), _arg("holdout", "bool", default=True),
              _arg("screen", "bool", default=True)]},
    {"id": "submit", "label": "Audit submitted calls", "paid": True,
     "args": [_arg("submission_id", "str", required=True)]},
]  # fmt: skip
STAGE_BY_ID = {s["id"]: s for s in STAGES}
INT_RANGES = {
    ("audit", "limit"): (1, 1000),
    ("fix", "top"): (1, 8),
    ("prove", "n"): (2, 20),
    ("prove", "k"): (1, 5),
    ("prove", "attempt"): (1, 2),
}
CLI_STAGE = {"agreement": "judge-agreement"}  # stage id -> pipeline subcommand when they differ


class ConsoleError(Exception):
    """A request the console refuses. `payload` is the JSON body, `status` the HTTP code."""

    def __init__(self, status: int, payload: dict):
        super().__init__(payload.get("error", ""))
        self.status = status
        self.payload = payload


def describe() -> list[dict]:
    """The stage list for GET /api/console: id, label, paid and args."""
    return [{**s, "args": [dict(a) for a in s["args"]]} for s in STAGES]


def mask(line: str) -> str:
    """Strip ANSI codes and hide anything that looks like a secret."""
    return SECRET_RE.sub("***", ANSI_RE.sub("", line))


def now_ist(t: datetime | None = None) -> str:
    return (t or datetime.now(IST)).astimezone(IST).strftime("%H:%M:%S %d %b")


def _bad(field: str, error: str = "invalid value") -> ConsoleError:
    return ConsoleError(400, {"error": error, "field": field})


def _call_ids(d: Path) -> set[str]:
    return {
        r["call_id"] for r in read_jsonl(d / "calls.jsonl") if isinstance(r, dict) and isinstance(r.get("call_id"), str)
    }


def _cause_ids(d: Path) -> set[str]:
    try:
        raw = json.loads((d / "causes.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    rows = raw if isinstance(raw, list) else raw.get("causes", []) if isinstance(raw, dict) else []
    return {c["cause_id"] for c in rows if isinstance(c, dict) and isinstance(c.get("cause_id"), str)}


def validate(d: Path, stage: object, args: object, confirm: object) -> tuple[dict, dict]:
    """Check a job request against the contract. Returns (stage spec, clean args) or raises ConsoleError."""
    if not isinstance(stage, str) or stage not in STAGE_BY_ID:
        raise _bad("stage", "unknown stage")
    spec = STAGE_BY_ID[stage]
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise _bad("args")
    known = {a["name"]: a for a in spec["args"]}
    for name in args:
        if name not in known:
            raise _bad(str(name)[:40], "unknown arg")
    clean: dict = {}
    for name, a in known.items():
        if name not in args or args[name] is None:
            if a["required"]:
                raise _bad(name, "required")
            if a["default"] is not None:
                clean[name] = a["default"]
            continue
        value = args[name]
        if a["type"] == "int":
            lo, hi = INT_RANGES[(stage, name)]
            if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
                raise _bad(name, f"must be an integer {lo}..{hi}")
        elif a["type"] == "bool":
            if not isinstance(value, bool):
                raise _bad(name, "must be true or false")
        elif a["type"] == "call_ids":
            if not isinstance(value, list) or not value or len(value) > MAX_CALL_IDS:
                raise _bad(name, "must be a non-empty list of call ids")
            known_ids = _call_ids(d)
            for cid in value:
                if not isinstance(cid, str) or not ID_RE.match(cid) or cid not in known_ids:
                    raise _bad(name, "unknown call id")
            value = list(dict.fromkeys(value))
        elif name == "cause":
            if not isinstance(value, str) or value not in _cause_ids(d):
                raise _bad(name, "unknown cause")
        elif name == "submission_id":
            if (
                not isinstance(value, str)
                or not SID_RE.match(value)
                or not (d / "console" / "submissions" / value).is_dir()
            ):
                raise _bad(name, "unknown submission")
        clean[name] = value
    if stage in ("fix", "prove") and not PROMPT_PATH.is_file():
        raise _bad("prompt", "VANI prompt file missing")
    if spec["paid"] and confirm is not True:
        raise ConsoleError(400, {"error": "confirm", "estimate_usd": None})
    return spec, clean


def build_argv(d: Path, stage: str, args: dict) -> list[str]:
    """The argv list for one job. Called only with values that `validate` returned. Tests may replace it."""
    argv = ["uv", "run", "python", "-m", "vaniloop.pipeline", "--runs", str(d), CLI_STAGE.get(stage, stage)]
    if stage in ("audit", "judge") and args.get("calls"):
        argv += ["--calls", ",".join(args["calls"])]
    if stage == "audit" and "limit" in args:
        argv += ["--limit", str(args["limit"])]
    if stage == "regrade":
        jev = d / "ledgers_jev.jsonl"
        argv += ["--ledgers", str(jev if jev.exists() else d / "ledgers.jsonl")]
    if stage in ("fix", "prove"):
        argv += ["--prompt", str(PROMPT_PATH)]
    if stage == "fix":
        argv += ["--top", str(args["top"])]
    if stage == "prove":
        argv += ["--cause", args["cause"]]
        for flag in ("n", "k", "attempt"):
            argv += [f"--{flag}", str(args[flag])]
    if stage in ("fix", "prove"):
        for flag in ("holdout", "screen"):
            if args.get(flag):
                argv.append(f"--{flag}")
    if stage == "submit":
        ws = d / "console" / "submissions" / args["submission_id"]
        argv[argv.index("--runs") + 1] = str(ws)
        argv += ["--inbox", str(ws / "inbox"), "--from", str(d)]
        try:
            ids = json.loads((ws / "meta.json").read_text(encoding="utf-8")).get("call_ids") or []
        except (OSError, ValueError):
            ids = []
        if ids:
            argv += ["--calls", ",".join(ids)]
    return argv


def _pid_alive(pid) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _parse_utc(text) -> datetime | None:
    try:
        t = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def job_cost(start: datetime, end: datetime) -> float:
    """Sum of cost_usd in llm.COSTS_PATH rows stamped between start and end. Reserve rows count zero."""
    total = 0.0
    for row in read_jsonl(llm.COSTS_PATH):
        if not isinstance(row, dict) or row.get("kind") == "reserve":
            continue
        ts, cost = _parse_utc(row.get("ts")), row.get("cost_usd")
        if ts is None or not start <= ts <= end or isinstance(cost, bool) or not isinstance(cost, (int, float)):
            continue
        if math.isfinite(cost) and cost >= 0:
            total += float(cost)
    return round(total, 6)


class JobRunner:
    """Runs one job at a time under a lock and keeps job rows in <data>/console/jobs.jsonl."""

    def __init__(self, data: Path, read_only: bool = False):
        self.data = Path(data)
        self.read_only = read_only
        self.overlay: dict[str, dict] = {}  # in-memory rows: recovery results in a read-only folder
        self.adopted: dict[str, int] = {}  # job id -> pgid of a job that survived a server restart
        self.dir = self.data / "console"
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.procs: dict[str, subprocess.Popen] = {}
        self.cancelled: set[str] = set()
        if (self.dir / "jobs.jsonl").exists():
            self._recover()

    def _rows(self) -> dict[str, dict]:
        rows: dict[str, dict] = {}
        for r in read_jsonl(self.dir / "jobs.jsonl"):
            if isinstance(r, dict) and isinstance(r.get("id"), str):
                rows[r["id"]] = r
        rows.update(self.overlay)
        return rows

    def _save(self, row: dict) -> None:
        with self.write_lock:
            append_jsonl(self.dir / "jobs.jsonl", [row])

    def _recover(self) -> None:
        """A job still marked running after a restart: if its process lives, adopt it (busy and cancel cover it).
        If the process is gone, mark it failed. In a read-only folder the change stays in memory."""
        for row in self._rows().values():
            if row.get("status") != "running" or row["id"] in self.procs:
                continue
            if _pid_alive(row.get("pid")):
                self.adopted[row["id"]] = row.get("pgid") or row["pid"]
                threading.Thread(target=self._watch, args=(row,), daemon=True).start()
            else:
                self._finish(row, "failed", None)

    def _finish(self, row: dict, status: str, code, cost=None) -> None:
        ended = datetime.now(UTC)
        done = {**row, "status": status, "exit_code": code, "ended_ist": now_ist(ended),
                "ended_utc": ended.isoformat(), "cost_usd": cost}  # fmt: skip
        if self.read_only:
            self.overlay[row["id"]] = done
        else:
            self._save(done)

    def _watch(self, row: dict) -> None:
        """Wait for an adopted job's process to end. Its exit code is unknown."""
        while _pid_alive(row["pid"]):
            time.sleep(0.5)
        self.adopted.pop(row["id"], None)
        self._finish(row, "cancelled" if row["id"] in self.cancelled else "failed", None)

    def _log_path(self, job_id: str) -> Path:
        return self.dir / "jobs" / f"{job_id}.log"

    def _lines(self, job_id: str, final: bool) -> list[str]:
        try:
            text = self._log_path(job_id).read_bytes().decode("utf-8", errors="replace")
        except OSError:
            return []
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        elif lines and not final:
            lines.pop()  # a half-written last line waits for its newline
        return [mask(ln.rstrip("\r")) for ln in lines]

    def public(self, row: dict) -> dict:
        keys = ("id", "stage", "args", "status", "started_ist", "ended_ist", "exit_code", "cost_usd")
        out = {k: row.get(k) for k in keys}
        out["log_lines"] = len(self._lines(row["id"], row.get("status") != "running"))
        return out

    def busy_job_id(self) -> str | None:
        for job_id, proc in list(self.procs.items()):
            if proc.poll() is None:
                return job_id
        for job_id, pgid in list(self.adopted.items()):
            if _pid_alive(pgid):
                return job_id
        return None

    def start(self, stage: str, args: dict) -> dict:
        """Start a job, or raise 409 busy. `args` must come from `validate`."""
        with self.lock:
            busy = self.busy_job_id()
            if busy:
                raise ConsoleError(409, {"error": "busy", "job_id": busy})
            argv = build_argv(self.data, stage, args)
            job_id = "j" + secrets.token_hex(6)
            (self.dir / "jobs").mkdir(parents=True, exist_ok=True)
            env = {**os.environ, "VANILOOP_RUBRIC": str(self.data / "rubric.json")}
            started = datetime.now(UTC)
            proc = subprocess.Popen(  # noqa: S603 - argv list, never a shell
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                start_new_session=True, env=env, cwd=Path(__file__).resolve().parents[2],
            )  # fmt: skip
            reader = threading.Thread(target=self._pump, args=(proc, self._log_path(job_id)), daemon=True)
            reader.start()
            row = {
                "id": job_id, "stage": stage, "args": args, "status": "running", "pid": proc.pid, "pgid": proc.pid,
                "started_ist": now_ist(started), "started_utc": started.isoformat(),
                "ended_ist": None, "exit_code": None, "cost_usd": None,
            }  # fmt: skip
            self.procs[job_id] = proc
            self._save(row)
            threading.Thread(target=self._wait, args=(job_id, proc, row, reader), daemon=True).start()
            return self.public(row)

    @staticmethod
    def _pump(proc: subprocess.Popen, path: Path) -> None:
        """Copy the child's output to the log, line by line, masked. The log never holds a secret."""
        with open(path, "wb") as log:
            for raw in proc.stdout:
                log.write(mask(raw.decode("utf-8", errors="replace").rstrip("\r\n")).encode("utf-8") + b"\n")
                log.flush()

    def _wait(self, job_id: str, proc: subprocess.Popen, row: dict, reader: threading.Thread) -> None:
        code = proc.wait()
        reader.join(timeout=10)
        ended = datetime.now(UTC)
        status = "cancelled" if job_id in self.cancelled else "done" if code == 0 else "failed"
        try:
            cost = job_cost(_parse_utc(row["started_utc"]), ended)
        except Exception:  # noqa: BLE001 - a bad cost file must not lose the job result
            cost = None
        self._save({**row, "status": status, "exit_code": code, "ended_ist": now_ist(ended),
                    "ended_utc": ended.isoformat(), "cost_usd": cost})  # fmt: skip
        self.procs.pop(job_id, None)

    def get(self, job_id: str) -> dict | None:
        if not JOB_ID_RE.match(job_id):
            return None
        return self._rows().get(job_id)

    def list(self) -> list[dict]:
        rows = sorted(self._rows().values(), key=lambda r: r.get("started_utc") or "", reverse=True)
        return [self.public(r) for r in rows[:MAX_JOBS_LISTED]]

    def log(self, job_id: str, start: int) -> dict | None:
        row = self.get(job_id)
        if row is None:
            return None
        lines = self._lines(job_id, row.get("status") != "running")
        start = max(0, start)
        return {"job": self.public(row), "lines": lines[start:], "next": max(start, len(lines))}

    def cancel(self, job_id: str) -> dict | None:
        row = self.get(job_id)
        if row is None:
            return None
        proc = self.procs.get(job_id)
        pgid = proc.pid if proc else self.adopted.get(job_id)
        if row.get("status") != "running" or pgid is None:
            raise ConsoleError(409, {"error": "not running", "job_id": job_id})
        self.cancelled.add(job_id)
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        return self.public(row)
