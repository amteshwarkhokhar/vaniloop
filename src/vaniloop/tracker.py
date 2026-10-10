"""Issue tracker (#130).

`<runs>/tracker.jsonl` is an append-only event log, one row per event. The state of a cause is a fold of its events:
`step` is the one pure function that holds every transition. The stages call `record`; they never compute a state.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .jsonl import append_jsonl, read_jsonl

IST = timezone(timedelta(hours=5, minutes=30))
BASE = "base"  # the clean prompt file, before any shipped patch; a patch row without base_version means this
TRACKER = "tracker.jsonl"
KINDS = (
    "detected", "routed", "patch_written", "screened", "gated", "approved", "shipped", "ticket_written",
    "ticket_sent", "ticket_resolved", "count_changed", "regressed", "closed",
    "rubric_changed",  # not in the contract list: counts under two rubric versions are never compared, so say so
)  # fmt: skip
TICKET_STATES = ("ticket_open", "ticket_sent", "ticket_resolved")


def now_ist() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


def rubric_version() -> str:
    """Jev version plus a short hash of the param overrides; "default" while vaniloop.rubric (#125) is absent."""
    try:
        from . import rubric  # lazy: #125 may not be merged

        d = rubric.describe()
        over = d.get("overrides") or {}
        return str(d["jev_version_effective"]) + (
            "+" + hashlib.sha1(json.dumps(over, sort_keys=True).encode()).hexdigest()[:6] if over else ""
        )
    except Exception:
        return "default"


def record(kind: str, cause_id: str, detail: dict | None = None, run_id: str | None = None, runs: Path | None = None):
    """Append one event. `runs` is the data folder; the stage passes the folder of the file it writes."""
    if kind not in KINDS:
        raise ValueError(f"unknown tracker event kind {kind!r}")
    row = {
        "ts_ist": now_ist(), "kind": kind, "cause_id": cause_id,
        "run_id": run_id or os.environ.get("VANILOOP_RUN_ID"), "rubric_version": rubric_version(),
        "detail": detail or {},
    }  # fmt: skip
    Path(runs).mkdir(parents=True, exist_ok=True)
    append_jsonl(Path(runs) / TRACKER, [row])


def events(runs: Path, cause_id: str | None = None) -> list[dict]:
    rows = [r for r in read_jsonl(Path(runs) / TRACKER) if isinstance(r, dict) and r.get("kind") in KINDS]
    return [r for r in rows if cause_id is None or r.get("cause_id") == cause_id]


def step(state: str | None, kind: str, d: dict) -> str | None:
    """One transition: the state after an event. A shipped or closed cause ignores later fix events."""
    if kind == "detected":
        return state or "open"
    if kind == "routed":  # only an unrouted cause moves: a rerun of fix must not undo a later state
        if state in (None, "open", "watching"):
            return {"watch": "watching"}.get(d.get("action"), "open")
        return state
    if kind == "patch_written":
        return state if state in ("shipped", "closed") else "in_fix"
    if kind == "gated":
        if state in ("shipped", "closed"):
            return state
        if d.get("decision") == "ship":
            return "ready_to_ship"
        if d.get("last_attempt"):
            return "exhausted"
        return "rejected" if d.get("decision") == "reject" else "gated_inconclusive"
    if kind == "ticket_written":
        return state if state in ("ticket_sent", "ticket_resolved") else "ticket_open"
    return {"shipped": "shipped", "ticket_sent": "ticket_sent", "ticket_resolved": "ticket_resolved",
            "regressed": "regressed", "closed": "closed"}.get(kind, state)  # fmt: skip


def fold(evs: list[dict]) -> tuple[str, str | None]:
    """(state, state_since_ist) of one cause from its events in file order."""
    state, since = None, None
    for e in evs:
        new = step(state, e["kind"], e.get("detail") or {})
        if new != state:
            state, since = new, e.get("ts_ist")
    return state or "open", since


def state(cause_id: str, runs: Path) -> str:
    return fold(events(runs, cause_id))[0]


# ---- counts: issues stage ----


def _counts(c: dict) -> dict:
    return {"fatal": len(c.get("fatal_calls") or []), "nonfatal": len(c.get("nonfatal_calls") or []),
            "symptoms": len(c.get("symptom_calls") or [])}  # fmt: skip


def sync_causes(causes: list[dict], runs: Path, run_id: str | None = None) -> None:
    """Emit detected, count_changed, rubric_changed, regressed and closed by comparing causes.json with the log.

    Counts recorded under another rubric version are never compared: that case writes rubric_changed and moves
    the baseline. After a ship, a higher count is regressed; a count of 0 or a vanished cause is closed.
    """
    runs, rv = Path(runs), rubric_version()
    by_cause: dict[str, list[dict]] = {}
    for e in events(runs):
        by_cause.setdefault(e["cause_id"], []).append(e)
    seen = set()
    for c in causes:
        cid = c["cause_id"]
        seen.add(cid)
        new, evs = _counts(c), by_cause.get(cid, [])
        if not evs:
            record(
                "detected", cid, {"counts": new, "name": c.get("name"), "parameter": c.get("parameter")}, run_id, runs
            )
            continue
        base = next((e for e in reversed(evs) if e["kind"] in ("detected", "count_changed", "rubric_changed")), evs[0])
        old = (base.get("detail") or {}).get("counts") or {}
        if (old.get("fatal"), old.get("nonfatal")) == (new["fatal"], new["nonfatal"]):
            continue
        st = fold(evs)[0]
        d = {"old": old, "new": new, "counts": new}
        if base.get("rubric_version") != rv:
            record(
                "rubric_changed",
                cid,
                {**d, "note": "rubric changed", "old_rubric": base.get("rubric_version")},
                run_id,
                runs,
            )
        elif st == "shipped" and new["fatal"] + new["nonfatal"] == 0:
            record("closed", cid, {**d, "counts": new}, run_id, runs)
        elif st == "shipped" and new["fatal"] + new["nonfatal"] > old.get("fatal", 0) + old.get("nonfatal", 0):
            record("regressed", cid, d, run_id, runs)
        else:
            record("count_changed", cid, d, run_id, runs)
    for cid, evs in by_cause.items():
        if cid not in seen and fold(evs)[0] == "shipped":
            record("closed", cid, {"note": "cause no longer detected"}, run_id, runs)


def _versions(runs: Path) -> list[dict]:
    try:
        v = json.loads((Path(runs) / "prompts" / "versions.json").read_text(encoding="utf-8")).get("versions", [])
    except (OSError, ValueError, AttributeError):
        return []
    return [x for x in v if isinstance(x, dict) and x.get("version")]


def _atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ---- tickets ----


def ticket_header(st: str) -> str:
    return f"State: {st}\n"


def _set_ticket_header(runs: Path, cause_id: str, st: str) -> None:
    path = Path(runs) / "tickets" / f"{cause_id}.md"
    if path.exists():
        body = path.read_text(encoding="utf-8")
        body = re.sub(r"\AState: \S+\n", "", body)
        _atomic(path, ticket_header(st) + body)


def ticket(cause_id: str, runs: Path, sent: str | None = None, resolved: str | None = None) -> str:
    """Record ticket_sent (a ref) or ticket_resolved (a note). Returns the new state."""
    runs = Path(runs)
    if (sent is None) == (resolved is None):
        raise SystemExit("ticket needs exactly one of --sent REF or --resolved NOTE")
    st = state(cause_id, runs)
    if sent is not None:
        if st != "ticket_open":
            raise SystemExit(f"{cause_id} is {st}: --sent needs ticket_open")
        record("ticket_sent", cause_id, {"ref": sent}, runs=runs)
    else:
        if st != "ticket_sent":
            raise SystemExit(f"{cause_id} is {st}: --resolved needs ticket_sent")
        record("ticket_resolved", cause_id, {"note": resolved}, runs=runs)
    new = state(cause_id, runs)
    _set_ticket_header(runs, cause_id, new)
    return new


# ---- board ----


def _event_view(e: dict) -> dict:
    return {k: e.get(k) for k in ("ts_ist", "kind", "run_id", "rubric_version", "detail")}


def cause_view(cause_id: str, runs: Path, raw: dict | None = None) -> dict:
    """One row of GET /api/tracker. `raw` is the causes.json row, if the cause still has one."""
    evs = events(runs, cause_id)
    st, since = fold(evs)
    raw = raw or {}
    route = next((e["detail"].get("action") for e in reversed(evs) if e["kind"] == "routed"), None)
    if route is None and raw:
        try:
            from .fix import route as fix_route
            from .models import Cause

            route = fix_route(Cause.model_validate(raw))[0]
        except Exception:
            route = None
    return {
        "cause_id": cause_id, "name": raw.get("name"), "parameter": raw.get("parameter"), "type": raw.get("type"),
        "owner": raw.get("owner"), "route": route, "state": st, "state_since_ist": since,
        "counts": _counts(raw), "rubric_version": evs[-1].get("rubric_version") if evs else None,
        "events": [_event_view(e) for e in evs],
    }  # fmt: skip


def board(runs: Path) -> dict:
    runs = Path(runs)
    try:
        raw = json.loads((runs / "causes.json").read_text(encoding="utf-8")).get("causes", [])
    except (OSError, ValueError, AttributeError):
        raw = []
    rows = {c["cause_id"]: c for c in raw if isinstance(c, dict) and c.get("cause_id")}
    ids = [*rows, *(e["cause_id"] for e in events(runs) if e["cause_id"] not in rows)]
    vers = _versions(runs)
    return {
        "causes": [cause_view(i, runs, rows.get(i)) for i in dict.fromkeys(ids)],
        "prompt_versions": [{k: v.get(k) for k in ("version", "created_ist", "causes", "base")} for v in vers],
        "current_prompt_version": vers[-1]["version"] if vers else None,
    }
