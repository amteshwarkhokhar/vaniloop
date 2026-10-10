"""LOOP: the whole fix loop in one command (#145).

`pipeline loop` runs audit, judge, regrade, issues, fix, test and report in order. It never ships: a cause that the gate
passes ends in ready_to_ship, and the owner approves it in the tracker. `plan(runs)` says what the loop would do and
what it may cost, with no LLM call and no file written. One cause failing is recorded and the loop goes on.
"""

from __future__ import annotations

import json
import math
import os
import signal
import statistics
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from . import tracker
from .fix import MAX_ATTEMPTS, run_fix, run_refix
from .issue import run_issues
from .jsonl import dedupe_by_call_id, read_jsonl
from .ledger import PROMPT_VERSION as LEDGER_VERSION
from .pipeline import run_audit, run_judge, run_regrade, workspace_paths
from .prove import run_prove

STEPS = ("audit", "judge", "regrade", "issues", "fix", "test", "report")
DEFAULT_TOP = 3
FILTERED_PREFIX = ".loop_causes."  # per-run copy of causes.json that fix works on, beside it; removed after fix
SKIPPED = {  # tracker state -> why the loop leaves the cause alone
    "ready_to_ship": "ready_to_ship: the gate said ship; the owner approves it in the tracker",
    "exhausted": f"exhausted: all {MAX_ATTEMPTS} attempts are used",
    **{s: f"{s}: already in a prompt version, no new patch" for s in ("shipped", "closed", "regressed")},
    **{s: f"{s}: a ticket, not a prompt patch" for s in tracker.TICKET_STATES},
}


class Cancelled(BaseException):
    """SIGTERM from the console cancel. Not an Exception: no per-cause or per-step handler may swallow it."""


def _cancel(signum, frame):
    raise Cancelled


def _cost(row: dict) -> float | None:
    x = row.get("cost_usd")
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x >= 0 else None


def _one_line(e: BaseException | str) -> str:
    """One line for a log or a summary: the message, led by the class name unless it is an exit message."""
    text = " ".join(str(e).split())
    if isinstance(e, Exception):
        text = f"{type(e).__name__}: {text}" if text else type(e).__name__
    return text[:300]


def _attempts(patches_path: Path) -> dict[str, int]:
    """cause_id -> attempt of the current patch in patches.json (a cause with no patch is missing)."""
    try:
        rows = json.loads(Path(patches_path).read_text(encoding="utf-8")).get("patches", [])
    except (OSError, ValueError, AttributeError):
        return {}
    return {x["cause_id"]: int(x.get("attempt", 1)) for x in rows if isinstance(x, dict) and "cause_id" in x}


def _decide(row: dict, route_why: str) -> None:
    """Set todo (fix, refix, prove or None), next_attempt and why. Worked on: a patch-routed cause in state open or
    watching (no patch yet), in_fix (a patch no gate has seen: prove it), rejected or gated_inconclusive (refix)."""
    st, att = row["state"], row["patch_attempt"]
    retry = st in ("rejected", "gated_inconclusive")
    if st in SKIPPED:
        row["why"] = SKIPPED[st]
    elif row["route"] != "patch":
        row["why"] = f"{row['route']}: {route_why}"
    elif retry and att >= MAX_ATTEMPTS:
        row["why"] = f"no attempts left: attempt {att} of {MAX_ATTEMPTS} is used"
    elif retry and att:
        row.update(
            todo="refix", next_attempt=att + 1, why=f"{st} at attempt {att}: refix, then prove attempt {att + 1}"
        )
    elif att:
        row.update(todo="prove", next_attempt=att, why=f"patch attempt {att} has no gate row: prove it")
    else:
        row.update(todo="fix", next_attempt=1, why=f"{st}: write a patch, then prove attempt 1")


def _work(runs: Path, p: dict, top: int) -> list[dict]:
    """One row per cause of causes.json. The first `top` workable causes keep a todo. Routes come from a fix dry run."""
    try:
        doc = json.loads(p["causes"].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    raw = [c for c in doc.get("causes", []) if isinstance(c, dict) and isinstance(c.get("cause_id"), str)]
    if not raw:
        return []
    plan_rows = run_fix(
        causes_path=p["causes"], out_path=p["patches"], top_n=len(raw) + 1, dry_run=True, holdout=True,
        calls_path=p["calls"], audits_path=p["audits"], gates_path=p["gates"], tickets_dir=p["tickets"],
    )["plan"]  # fmt: skip
    routed, attempts, events = {x["cause_id"]: x for x in plan_rows}, _attempts(p["patches"]), {}
    for e in tracker.events(runs):
        events.setdefault(e["cause_id"], []).append(e)
    rows = []
    for c in raw:
        cid = c["cause_id"]
        row = {"cause_id": cid, "route": routed[cid]["action"], "state": tracker.fold(events.get(cid, []))[0],
               "patch_attempt": attempts.get(cid, 0), "todo": None, "next_attempt": None, "why": ""}  # fmt: skip
        _decide(row, routed[cid]["why"])
        rows.append(row)
    for n, row in enumerate(r for r in rows if r["todo"]):
        if n >= top:
            row.update(todo=None, next_attempt=None, why=f"beyond --top {top}: {row['why']}")
    return rows


def _needs_routing(rows: list[dict]) -> list[dict]:
    """Ticket and watch causes that no fix run has routed yet (state open): fix writes their ticket and routed event."""
    return [r for r in rows if r["route"] != "patch" and r["state"] == "open"]


def _estimate(runs: Path, p: dict, n_new: int, todo: list[dict]) -> tuple[float | None, str]:
    """(estimate_usd, basis) from costs.jsonl and gates.jsonl: new calls x mean audit cost (rows with the ledger tag)
    + per cause its remaining attempts x (median gate cost + mean fix cost: rows with no tag). None without history.
    Gate rows count replay turns only (not warm-ups), so the real cost may be higher."""
    paid = [
        r for r in read_jsonl(runs / "costs.jsonl")
        if r.get("kind") != "reserve" and "jev" not in str(r.get("model")) and _cost(r) is not None
    ]  # fmt: skip
    hist = {
        "audit": [_cost(r) for r in paid if r.get("cache_tag") == LEDGER_VERSION],
        "gate": [c for c in map(_cost, read_jsonl(p["gates"])) if c is not None],
        "fix": [_cost(r) for r in paid if r.get("cache_tag") in ("", None)],
    }  # fmt: skip
    attempts = sum(MAX_ATTEMPTS - r["next_attempt"] + 1 for r in todo)
    count = {"audit": n_new, "gate": attempts, "fix": attempts - sum(r["todo"] == "prove" for r in todo)}
    missing = [k for k in count if count[k] and not hist[k]]
    if missing:
        return None, "no history: no " + ", no ".join(
            f"{k} cost rows" for k in missing
        ) + " in costs.jsonl or gates.jsonl"
    price = {k: (statistics.median if k == "gate" else statistics.fmean)(hist[k]) for k in count if count[k]}
    if not price:
        return 0.0, "nothing to run that spends money"
    basis = " + ".join(f"{count[k]} {k} x ${price[k]:.4f}" for k in price)
    basis += " (mean audit/fix cost, median gate cost). Gate rows count replay turns only: the real cost may be higher."
    return round(sum(count[k] * price[k] for k in price), 4), basis


def plan(runs: Path, top: int = DEFAULT_TOP) -> dict:
    """What `loop` would do on this folder, with a cost estimate; no LLM call, no file written. The causes are those
    of the last `issues` run (the loop rebuilds them in step 4). Shape: see docs/console_api.md."""
    runs = Path(runs)
    p = workspace_paths(runs)
    calls = [r["call_id"] for r in dedupe_by_call_id(read_jsonl(p["calls"]), last_wins=False)]
    audited = {r.get("call_id") for r in read_jsonl(p["audits"])}
    new = [c for c in calls if c not in audited]  # what the audit step does: calls with no audit row (its resume)
    rows = _work(runs, p, top)
    todo = [r for r in rows if r["todo"]]
    fixing, routing = [r for r in todo if r["todo"] == "fix"], _needs_routing(rows)
    estimate, basis = _estimate(runs, p, len(new), todo)

    steps = [
        {"name": n, "will_run": w, "why": y, "count": c}
        for n, w, c, y in (
            ("audit", bool(new), len(new), "calls with no audit row (Gemini, paid)"),
            ("judge", True, len(new), "Jev labels new and changed calls; free"),
            ("regrade", True, len(calls), "re-applies the Python rules to the Jev ledgers; free"),
            ("issues", True, len(rows), "rebuilds causes.json and syncs the tracker; free; causes below may change"),
            ("fix", bool(fixing or routing), len(fixing), f"first patch (paid); routes {len(routing)} ticket/watch"),
            ("test", bool(todo), len(todo), f"gate run and refix per cause, {MAX_ATTEMPTS} attempts at most (paid)"),
            ("report", True, len(rows), "lists ready_to_ship, ticket and watch causes; never ships"),
        )
    ]
    public = ("cause_id", "route", "state", "next_attempt", "why")
    return {
        "steps": steps,
        "causes": [{k: r[k] for k in public} for r in rows],
        "new_calls": len(new),
        "estimate_usd": estimate,
        "estimate_basis": basis,
    }


def _test_cause(row: dict, start: int, p: dict, prompt: Path, n: int, k: int) -> dict:
    """Prove a cause from attempt `start` (a refix first when the row says so); on a decision other than ship, refix and
    prove again while attempts are left. A failure ends this cause only and is returned in `failed`."""
    cid, out = row["cause_id"], {"attempts": [], "failed": None}
    step, a = ("refix", start - 1) if row["todo"] == "refix" else ("prove", start)
    try:
        while True:
            if step == "refix":
                r = run_refix(
                    cid, prompt, causes_path=p["causes"], patches_path=p["patches"], gates_path=p["gates"],
                    replays_path=p["replays"], audits_path=p["audits"], calls_path=p["calls"], holdout=True,
                )  # fmt: skip
                if not r["patches"]:
                    raise SystemExit("refix wrote no patch: " + "; ".join(x.get("reason", "") for x in r["rejected"]))
                step, a = "prove", a + 1
                print(f"  refix {cid}: new patch for attempt {a}", flush=True)
            o = run_prove(
                cid, prompt, n=n, k=k, attempt=a, causes_path=p["causes"], calls_path=p["calls"],
                ledgers_path=p["ledgers"], audits_path=p["audits"], patches_path=p["patches"],
                gates_path=p["gates"], replays_path=p["replays"], holdout=True, screen=True,
            )  # fmt: skip
            out["attempts"].append({"attempt": a, "decision": o.result.decision})
            print(f"  prove {cid} attempt {a}: {o.result.decision} ({_one_line(o.result.reason)})", flush=True)
            if o.result.decision == "ship" or a >= MAX_ATTEMPTS:
                return out
            step = "refix"
    except (Exception, SystemExit) as e:  # one cause must not stop the others
        out["failed"] = {"step": step, "attempt": a, "reason": _one_line(e)}
        print(f"  FAILED {cid} {step} attempt {a}: {out['failed']['reason']}", flush=True)
    return out


def _fix_step(p: dict, prompt: Path, rows: list[dict]) -> tuple[dict, dict[str, dict]]:
    """Step 5. Write a first patch for the `fix` rows and route the ticket and watch causes no fix run has seen, on a
    copy of causes.json that holds only those causes (so --top counts workable causes). Returns (detail, failures):
    a cause with no patch afterwards fails."""
    fixing = {r["cause_id"] for r in rows if r["todo"] == "fix"}
    keep = fixing | {r["cause_id"] for r in _needs_routing(rows)}
    if not keep:
        return {"patches": 0}, {}
    doc = json.loads(p["causes"].read_text(encoding="utf-8"))
    with tempfile.NamedTemporaryFile("w", dir=p["causes"].parent, prefix=FILTERED_PREFIX, suffix=".json") as f:
        json.dump({**doc, "causes": [c for c in doc["causes"] if c.get("cause_id") in keep]}, f)
        f.flush()  # deleted on exit, also when a cancel raises
        r = run_fix(
            causes_path=Path(f.name), prompt_path=prompt, out_path=p["patches"], top_n=len(fixing), holdout=True,
            calls_path=p["calls"], audits_path=p["audits"], gates_path=p["gates"], tickets_dir=p["tickets"],
        )  # fmt: skip
    failed = {}
    for c in fixing - set(_attempts(p["patches"])):
        why = "; ".join(_one_line(y["reason"]) for y in r["rejected"] if y["cause_id"] == c)
        failed[c] = {"attempts": [], "failed": {"step": "fix", "attempt": 1, "reason": why or "fix wrote no patch"}}
    return {"patches": len(fixing) - len(failed), "rejected": len(r["rejected"])}, failed


def run_loop(runs: Path, prompt_path: Path, top: int = DEFAULT_TOP, n: int = 8, k: int = 3) -> dict:
    """Run the seven steps in order; print LOOP_STEP lines, LOOP_SUMMARY and LOOP_DONE. Ships nothing. A failed step 1
    to 5 prints LOOP_FAILED <step> <reason> and exits non-zero. SIGTERM (the console cancel) stops it the same way."""
    old = signal.signal(signal.SIGTERM, _cancel)
    try:
        return _run_loop(Path(runs), Path(prompt_path), top, n, k)
    finally:
        signal.signal(signal.SIGTERM, old)


def _run_loop(runs: Path, prompt_path: Path, top: int, n: int, k: int) -> dict:
    p = workspace_paths(runs)
    started, steps, rows, tested, rep = datetime.now(UTC), [], [], {}, {}

    def stage(i: int, fn) -> None:
        """Run step i. `fn` returns the step detail (JSON-safe). A failure ends the loop with LOOP_FAILED."""
        print(f"LOOP_STEP {i}/{len(STEPS)} {STEPS[i - 1]}", flush=True)
        try:
            detail = fn()
        except (Exception, SystemExit, Cancelled) as e:  # a cancel has cleaned up by now: stop, do not go on
            print(f"LOOP_FAILED {STEPS[i - 1]} {'cancelled' if isinstance(e, Cancelled) else _one_line(e)}", flush=True)
            raise SystemExit(1) from None
        print(f"  {detail}", flush=True)
        steps.append({"name": STEPS[i - 1], "status": "done", "detail": detail})

    def fix() -> dict:
        rows.extend(_work(runs, p, top))  # after issues: the causes and the tracker are fresh
        detail, tested_fix = _fix_step(p, prompt_path, rows)
        tested.update(tested_fix)  # a cause that got no patch is a failure; test skips it
        return detail

    def test() -> dict:
        os.environ["VANILOOP_JUDGE"] = "jev"  # replays are graded with Jev labels like the main grade (see prove)
        for r in (r for r in rows if r["todo"] and r["cause_id"] not in tested):
            start = r["next_attempt"] if r["todo"] != "fix" else _attempts(p["patches"]).get(r["cause_id"], 1)
            tested[r["cause_id"]] = _test_cause(r, start, p, prompt_path, n, k)
        return {"tested": len(tested), "failed": sum(bool(t["failed"]) for t in tested.values())}

    def report() -> dict:
        evs: dict[str, list[dict]] = {}
        for e in tracker.events(runs):
            evs.setdefault(e["cause_id"], []).append(e)
        st = {r["cause_id"]: tracker.fold(evs.get(r["cause_id"], []))[0] for r in rows}
        dec = {c: t["attempts"][-1]["decision"] for c, t in tested.items() if t["attempts"]}
        rep.update(state=st, decisions=dec, watch=[c for c, s in st.items() if s == "watching"],
                   ready_to_ship=[c for c, s in st.items() if s == "ready_to_ship" or dec.get(c) == "ship"],
                   tickets=[c for c, s in st.items() if s in tracker.TICKET_STATES])  # fmt: skip
        return {k: rep[k] for k in ("ready_to_ship", "tickets", "watch")}

    stage(1, lambda: run_audit(p["calls"], p["ledgers"], p["audits"], p["errors"]))
    stage(2, lambda: run_judge(p["calls"], p["ledgers"], p["ledgers_jev"], p["errors"]))
    stage(3, lambda: run_regrade(p["calls"], p["ledgers_jev"], p["audits"]))
    stage(4, lambda: {"causes": len(run_issues(p["audits"], p["causes"]))})
    stage(5, fix)
    stage(6, test)
    stage(7, report)
    from .console import job_cost  # lazy: the same cost window as a console job row

    summary = {
        "steps": steps, "decisions": rep["decisions"], "cost_usd": job_cost(started, datetime.now(UTC)),
        "ready_to_ship": rep["ready_to_ship"], "tickets": rep["tickets"], "watch": rep["watch"],
        "causes": [
            {"cause_id": r["cause_id"], "state": rep["state"][r["cause_id"]], "attempts": [], "failed": None,
             "skipped": None if r["todo"] else r["why"], **tested.get(r["cause_id"], {})}
            for r in rows
        ],
    }  # fmt: skip
    print("LOOP_SUMMARY " + json.dumps(summary, ensure_ascii=False), flush=True)
    print("LOOP_DONE", flush=True)
    return summary
