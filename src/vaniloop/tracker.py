"""Issue tracker (#130).

`<runs>/tracker.jsonl` is an append-only event log, one row per event. The state of a cause is a fold of its events:
`step` is the one pure function that holds every transition. The stages call `record`; they never compute a state.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .jsonl import read_jsonl

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
    """jev_version_effective plus a short hash of the overridden params and questions; "default" without a rubric."""
    try:
        from . import rubric  # lazy: the console rubric may be absent

        d = rubric.describe()
        over = {
            "params": {x["id"]: x["value"] for x in d.get("params", []) if x.get("overridden")},
            "questions": {x["id"]: x["value"] for x in d.get("questions", []) if x.get("overridden")},
        }
        tag = hashlib.sha1(json.dumps(over, sort_keys=True).encode()).hexdigest()[:6]
        return str(d["jev_version_effective"]) + (f"+{tag}" if over["params"] or over["questions"] else "")
    except Exception:
        return "default"


def _append(path: Path, row: dict) -> None:
    """Append one JSON line under an flock on <path>.lock. The tail check, the newline repair and the row go out in
    one write call, so two writers never interleave and a torn last line never joins a new row."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
    with path.with_name(path.name + ".lock").open("a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            torn = False
            if path.exists() and path.stat().st_size:
                with path.open("rb") as f:
                    f.seek(-1, 2)
                    torn = f.read(1) != b"\n"
            with path.open("ab", buffering=0) as f:
                f.write((b"\n" if torn else b"") + data)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def record(kind: str, cause_id: str, detail: dict | None = None, run_id: str | None = None, runs: Path | None = None):
    """Append one event. `runs` is the data folder; the stage passes the folder of the file it writes."""
    if kind not in KINDS:
        raise ValueError(f"unknown tracker event kind {kind!r}")
    row = {
        "ts_ist": now_ist(), "kind": kind, "cause_id": cause_id,
        "run_id": run_id or os.environ.get("VANILOOP_RUN_ID"), "rubric_version": rubric_version(),
        "detail": detail or {},
    }  # fmt: skip
    _append(Path(runs) / TRACKER, row)


def emit(kind: str, cause_id: str, detail: dict | None = None, run_id: str | None = None, runs: Path | None = None):
    """record() for a stage: a failure warns on stderr and the stage goes on. Saves come first."""
    try:
        record(kind, cause_id, detail, run_id, runs)
    except Exception as e:
        print(f"warning: tracker could not record {kind} for {cause_id}: {type(e).__name__}: {e}", file=sys.stderr)


def emit_routed(runs: Path, cause_id: str, action: str, why: str) -> None:
    """Best-effort routed event, skipped when the cause's last routed event says the same."""
    try:
        last = next((e for e in reversed(events(runs, cause_id)) if e["kind"] == "routed"), None)
        if last is not None and last["detail"] == {"action": action, "why": why}:
            return
    except Exception:
        pass
    emit("routed", cause_id, {"action": action, "why": why}, runs=runs)


def events(runs: Path, cause_id: str | None = None) -> list[dict]:
    rows = [r for r in read_jsonl(Path(runs) / TRACKER) if isinstance(r, dict) and r.get("kind") in KINDS]
    return [r for r in rows if cause_id is None or r.get("cause_id") == cause_id]


def step(state: str | None, kind: str, d: dict) -> str | None:
    """One transition: the state after an event. An event whose predecessor state is wrong changes nothing
    (see `valid`). A shipped or closed cause ignores later fix events."""
    if not valid(state, kind):
        return state
    if kind == "detected":
        return state or "open"
    if kind == "routed":  # only an unrouted cause moves: a rerun of fix must not undo a later state
        if state in (None, "open", "watching"):
            return {"watch": "watching"}.get(d.get("action"), "open")
        return state
    if kind == "patch_written":
        return state if state in ("shipped", "closed", "regressed") else "in_fix"
    if kind == "gated":
        if state in ("shipped", "closed", "regressed"):
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


PREDECESSOR = {  # event kind -> the states it may follow. Other kinds have no guard.
    "shipped": ("ready_to_ship",),
    "ticket_sent": ("ticket_open",),
    "ticket_resolved": ("ticket_sent",),
    "regressed": ("shipped", "closed"),
    "closed": ("shipped", "regressed"),
}


def valid(state: str | None, kind: str) -> bool:
    """False when the event skips its prerequisite, e.g. shipped on a cause that no gate said ship for.
    ready_to_ship has no entry: only a gated ship event leads there."""
    return kind not in PREDECESSOR or state in PREDECESSOR[kind]


def walk(evs: list[dict]) -> list[tuple[str, bool]]:
    """(state after the event, event was valid) for every event in order."""
    out, st = [], None
    for e in evs:
        d = e.get("detail") or {}
        ok = valid(st, e["kind"])
        st = step(st, e["kind"], d)
        out.append((st or "open", ok))
    return out


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


BASELINE = ("detected", "count_changed", "rubric_changed", "regressed", "closed")  # events that carry counts
ZERO = {"fatal": 0, "nonfatal": 0, "symptoms": 0}


def sync_causes(causes: list[dict], runs: Path, run_id: str | None = None) -> None:
    """Best-effort `_sync`: a tracker failure warns on stderr and `issues` goes on."""
    try:
        _sync(causes, Path(runs), run_id)
    except Exception as e:
        print(f"warning: tracker could not sync causes: {type(e).__name__}: {e}", file=sys.stderr)


def _sync(causes: list[dict], runs: Path, run_id: str | None) -> None:
    """Emit detected, count_changed, rubric_changed, regressed and closed by comparing causes.json with the log.

    The rubric version is checked first: a change always writes rubric_changed with the new counts as the baseline
    and nothing else, because counts under two rubrics are never compared. After that, per state:
    shipped: a higher count is regressed, a count of 0 (or a vanished cause) is closed;
    closed: any count again is regressed; regressed: a count of 0 (or vanished) is closed again.
    A close needs the same rubric version as the event that set the baseline (the ship, or the regression), so a
    cause is never closed by a rubric that changed the counting.
    """
    rv = rubric_version()
    by_cause: dict[str, list[dict]] = {}
    for e in events(runs):
        by_cause.setdefault(e["cause_id"], []).append(e)
    present = {c["cause_id"]: c for c in causes}
    for cid in [*present, *(i for i in by_cause if i not in present)]:
        evs = by_cause.get(cid, [])
        c = present.get(cid)
        new = _counts(c) if c else ZERO
        if not evs:
            d = {"counts": new, "name": c.get("name"), "parameter": c.get("parameter")}
            record("detected", cid, d, run_id, runs)
            continue
        st = fold(evs)[0]
        if c is None and st not in ("shipped", "regressed"):
            continue  # a vanished cause only matters after a ship
        accepted = [e for e, (_, ok) in zip(evs, walk(evs), strict=True) if ok]  # an ignored event is no baseline
        base = next((e for e in reversed(accepted) if e["kind"] in BASELINE), None)
        if base is None:  # only ignored events so far: the cause counts as new
            record(
                "detected",
                cid,
                {"counts": new, "name": c and c.get("name"), "parameter": c and c.get("parameter")},
                run_id,
                runs,
            )
            continue
        old = (base.get("detail") or {}).get("counts") or {}
        d = {"old": old, "new": new, "counts": new}
        if base.get("rubric_version") != rv:
            record(
                "rubric_changed",
                cid,
                {**d, "note": "rubric changed", "old_rubric": base.get("rubric_version")},
                run_id,
                runs,
            )
            continue
        total, old_total = new["fatal"] + new["nonfatal"], old.get("fatal", 0) + old.get("nonfatal", 0)
        if st == "closed":
            if total:
                record("regressed", cid, d, run_id, runs)
            continue
        if (old.get("fatal"), old.get("nonfatal")) == (new["fatal"], new["nonfatal"]):
            continue
        anchor = next((e for e in reversed(accepted) if e["kind"] in ("shipped", "regressed")), {})
        if st in ("shipped", "regressed") and not total:
            if anchor.get("rubric_version") == rv:
                record("closed", cid, d, run_id, runs)
            else:
                record("rubric_changed", cid, {**d, "note": "rubric changed since the ship: not closed"}, run_id, runs)
        elif st == "shipped" and total > old_total:
            record("regressed", cid, d, run_id, runs)
        else:
            record("count_changed", cid, d, run_id, runs)


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


def _event_view(e: dict, ok: bool = True) -> dict:
    return {
        **{k: e.get(k) for k in ("ts_ist", "kind", "run_id", "rubric_version", "detail")},
        **({} if ok else {"ignored": True}),
    }


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
        "events": [_event_view(e, ok) for e, (_, ok) in zip(evs, walk(evs), strict=True)],
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
