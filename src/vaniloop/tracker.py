"""Issue tracker and prompt lineage (#130).

`<runs>/tracker.jsonl` is an append-only event log, one row per event. The state of a cause is a fold of its events:
`step` is the one pure function that holds every transition. `<runs>/prompts/v<N>.md` and `versions.json` hold the
shipped prompt versions. The stages call `record`; they never compute a state themselves.

fix.py and prove.py import this module, so it imports fix lazily, inside the functions that need it.
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

from . import prompt
from .jsonl import read_jsonl

IST = timezone(timedelta(hours=5, minutes=30))
BASE = "base"  # the clean prompt file, before any shipped patch; a patch row without base_version means this
TRACKER = "tracker.jsonl"
KINDS = (
    "detected", "routed", "patch_written", "screened", "gated", "approved", "shipped", "ticket_written",
    "ticket_sent", "ticket_resolved", "count_changed", "regressed", "closed",
    "release_check",  # PR 4: the bundle check of every shipped patch together; no state change
    "rubric_changed",  # not in the contract list: counts under two rubric versions are never compared, so say so
)  # fmt: skip
TICKET_STATES = ("ticket_open", "ticket_sent", "ticket_resolved")
DEFAULT_PROMPT = Path(__file__).resolve().parents[2] / "data" / "reference" / "vani_prompt_sarvam.md"


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


# ---- prompt lineage ----


VERSION_NAME = re.compile(r"^v[1-9][0-9]*$")


def fingerprint(*parts: str) -> str:
    """sha256 of the parts, each length-prefixed so ("ab", "c") and ("a", "bc") differ."""
    h = hashlib.sha256()
    for part in parts:
        b = part.encode("utf-8")
        h.update(f"{len(b)}:".encode() + b)
    return h.hexdigest()


def _versions(runs: Path) -> list[dict]:
    """The shipped versions in order. A missing versions.json means none; a present but unreadable or malformed one
    stops with SystemExit, so fix, refix and prove never fall back to the clean base by mistake."""
    path = Path(runs) / "prompts" / "versions.json"
    if not path.exists():
        return []
    bad = f"{path} is unreadable or malformed: fix it or restore it before fix, refix, prove or ship"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise SystemExit(bad) from None
    rows = doc.get("versions") if isinstance(doc, dict) else None
    if not isinstance(rows, list) or not all(
        isinstance(x, dict) and VERSION_NAME.match(str(x.get("version"))) and isinstance(x.get("causes"), list)
        for x in rows
    ):
        raise SystemExit(bad)
    if [x["version"] for x in rows] != [f"v{n}" for n in range(1, len(rows) + 1)]:
        raise SystemExit(bad)
    for x in rows:  # every listed version must have its text, not only the last
        if not (Path(runs) / "prompts" / f"{x['version']}.md").is_file():
            raise SystemExit(
                f"{Path(runs) / 'prompts' / (x['version'] + '.md')} is missing but versions.json names it: restore it"
            )
    return rows


def current_version(runs: Path) -> str:
    v = _versions(runs)
    return v[-1]["version"] if v else BASE


def current_prompt(prompt_path: Path, runs: Path) -> tuple[str, str]:
    """(text, version): the latest shipped version, else the clean base prompt. Fix, refix and prove start here."""
    v = current_version(runs)
    if v == BASE:
        return prompt.load(prompt_path), BASE
    path = Path(runs) / "prompts" / f"{v}.md"
    try:
        return path.read_text(encoding="utf-8"), v
    except OSError:
        raise SystemExit(f"{path} is missing but versions.json names {v}: restore it") from None


def shipped_ranges(runs: Path) -> list[tuple[int, int]]:
    """Lines changed by every shipped patch, in the line numbers of the current version.

    Each version records its patch as start, end (the replaced span's last line in its base), new_end (the last
    changed line after the edit) and delta (newlines in new_text minus newlines in old_text). A later patch moves
    every line after its end by its delta, so the earlier ranges are shifted forward through each later version.
    """
    out: list[tuple[int, int]] = []
    for v in _versions(runs):
        p = v.get("patch") or {}
        if not all(isinstance(p.get(k), int) for k in ("start", "end", "new_end", "delta")):
            continue
        out = [(a + p["delta"], b + p["delta"]) if a > p["end"] else (a, b) for a, b in out]
        out.append((p["start"], p["new_end"]))
    return out


def overlap_reason(runs: Path, patch, patches: list[dict]) -> str | None:
    """Why a patch on the current version cannot be written: it edits lines a shipped version changed, or lines
    another in-flight patch (same base, not shipped) already targets. None when it is clear."""
    runs, cur = Path(runs), current_version(runs)
    lo, hi = patch.target_start_line, patch.target_end_line
    for a, b in shipped_ranges(runs):
        if lo <= b and a <= hi:
            return f"old_text overlaps lines {a}-{b} changed by a shipped prompt version"
    shipped = {c for v in _versions(runs) for c in v.get("causes", [])}
    for p in patches:
        if p.get("cause_id") in (patch.cause_id, *shipped) or (p.get("base_version") or BASE) != cur:
            continue
        if lo <= p.get("target_end_line", -1) and p.get("target_start_line", 1 << 30) <= hi:
            return f"old_text overlaps lines of the in-flight patch for {p['cause_id']}"
    return None


def _atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def edit_span(text: str, old: str, new: str) -> dict:
    """Where old (found once in text) is replaced by new, in line numbers: start; end = the last line of the replaced
    span in text; new_end = the last line changed after the edit; delta = newlines in new minus newlines in old.

    A span that ends in a newline ends on that newline's line. One that stops mid-line also touches the rest of
    that line, so removing or adding a trailing newline counts that line and shifts the lines after it by delta.
    """
    off = text.index(old)
    after = text.count("\n", 0, off + len(old) - 1 if old.endswith("\n") else off + len(old))
    patched = text[:off] + new + text[off + len(old) :]
    last = off + len(new) - 1 if old.endswith("\n") else off + len(new)
    return {"start": text.count("\n", 0, off) + 1, "end": after + 1,
            "new_end": patched.count("\n", 0, last) + 1, "delta": new.count("\n") - old.count("\n")}  # fmt: skip


def ship(cause_id: str, runs: Path, prompt_path: Path | None = None, patches_path: Path | None = None) -> str:
    """Make the next prompt version: the current version plus this cause's gated patch. Returns its name.

    The whole transaction runs under an flock on <runs>/prompts/.ship.lock, so two ships never take the same
    version name. Stops with SystemExit unless the latest gate row for the cause says ship for the current patch's
    attempt, carries the fingerprints of this exact patch and of the exact prompt text the gate replayed (a row
    without them cannot ship), the patch sits on the current version and its anchor occurs once.
    """
    from .fix import PromptPatch, _count_overlapping, apply_patch  # lazy: fix imports this module

    runs, prompt_path = Path(runs), prompt_path or DEFAULT_PROMPT
    (runs / "prompts").mkdir(parents=True, exist_ok=True)
    with (runs / "prompts" / ".ship.lock").open("a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            return _ship(cause_id, runs, prompt_path, patches_path, PromptPatch, _count_overlapping, apply_patch)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _ship(cause_id, runs, prompt_path, patches_path, PromptPatch, _count_overlapping, apply_patch) -> str:
    patches_file = patches_path or runs / "patches.json"
    doc = json.loads(patches_file.read_text(encoding="utf-8")) if patches_file.exists() else {}
    rows = [x for x in doc.get("patches", []) if x.get("cause_id") == cause_id]
    if not rows:
        raise SystemExit(f"no patch for {cause_id}; run `pipeline fix` first")
    patch = PromptPatch.model_validate(rows[-1])
    gate_row = next((r for r in reversed(read_jsonl(runs / "gates.jsonl")) if r.get("cause_id") == cause_id), None)
    if gate_row is None or gate_row.get("decision") != "ship":
        raise SystemExit(
            f"the latest gate decision for {cause_id} is '{(gate_row or {}).get('decision')}': ship needs 'ship'"
        )
    if gate_row.get("attempt", 1) != patch.attempt:
        raise SystemExit(
            f"the latest gate row for {cause_id} is for attempt {gate_row.get('attempt', 1)}, "
            f"the current patch is attempt {patch.attempt}"
        )
    # Re-check the tracker state under the ship lock: a console ship job is validated when it is queued, and a
    # refix or a new gate can land before it runs. With no events at all (a lost log), the signed gate row is the proof.
    st = state(cause_id, runs)
    if events(runs, cause_id) and st != "ready_to_ship":
        raise SystemExit(f"{cause_id} is in state '{st}', not ready_to_ship: ship refused")
    text, cur = current_prompt(prompt_path, runs)
    if any(cause_id in v.get("causes", []) for v in _versions(runs)):
        raise SystemExit(f"{cause_id} is already shipped")
    if (patch.base_version or BASE) != cur:
        raise SystemExit(
            f"the patch for {cause_id} is on {patch.base_version or BASE}, the current version is {cur}: refix"
        )
    if gate_row.get("patch_fp") != patch_fingerprint(patch) or gate_row.get("base_fp") != fingerprint(text):
        raise SystemExit(
            f"the gate row for {cause_id} does not match this patch and this prompt text (or has no fingerprints): "
            "run `pipeline prove` again"
        )
    if _count_overlapping(text, patch.old_text) != 1:
        raise SystemExit(f"the patch for {cause_id} does not fit {cur}: old_text must occur exactly once")
    name = f"v{len(_versions(runs)) + 1}"
    _atomic(runs / "prompts" / f"{name}.md", apply_patch(text, patch))
    entry = {
        "version": name, "created_ist": now_ist(), "causes": [cause_id], "base": None if cur == BASE else cur,
        "patch": {"cause_id": cause_id, **edit_span(text, patch.old_text, patch.new_text)},  # exact offsets
    }  # fmt: skip
    _atomic(runs / "prompts" / "versions.json", json.dumps({"versions": [*_versions(runs), entry]}, indent=1))
    if st != "ready_to_ship":  # the log is missing the gated event: the signed gate row is the proof
        emit("gated", cause_id, {"decision": "ship", "attempt": patch.attempt, "source": "gates.jsonl"}, runs=runs)
    emit("approved", cause_id, {"attempt": patch.attempt}, runs=runs)
    emit("shipped", cause_id, {"version": name, "attempt": patch.attempt}, runs=runs)
    return name


def patch_fingerprint(patch) -> str:
    """The exact edit a gate row vouches for: old_text and new_text."""
    return fingerprint(patch.old_text, patch.new_text)


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
    try:
        vers, vers_error = _versions(runs), None
    except SystemExit as e:
        vers, vers_error = [], str(e)
    return {
        "causes": [cause_view(i, runs, rows.get(i)) for i in dict.fromkeys(ids)],
        "prompt_versions": [{k: v.get(k) for k in ("version", "created_ist", "causes", "base")} for v in vers],
        "current_prompt_version": vers[-1]["version"] if vers else None,
        **({"prompt_versions_error": vers_error} if vers_error else {}),
    }
