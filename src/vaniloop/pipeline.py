"""Pipeline runner. `python -m vaniloop.pipeline <command>`: audit, regrade, issues, categorize, fix, refix, prove,
rootcause, grade-sample, golden (build, agreement, check)."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from . import rubric, tune
from .categorize import CATEGORIES_PATH, run_categorize
from .fix import GATES_PATH, PATCHES_PATH, REPLAYS_PATH, TICKETS_DIR, run_fix, run_refix
from .golden import (
    GOLDEN_DIR,
    GOLDEN_RUNS_PATH,
    build_golden,
    describe_check,
    golden_agreement,
    latest_golden,
    load_golden,
    run_golden_check,
)
from .grading import grade_sample
from .ingest import OUT_PATH as CALLS_PATH
from .ingest import detect_silences, load_transcript
from .issue import AUDITS_PATH, CAUSES_PATH, load_audits, run_issues
from .jev import ask, fingerprint, judge_agreement, labels
from .jsonl import append_jsonl, dedupe_by_call_id, read_jsonl
from .ledger import extract_ledger
from .models import CallRecord, Ledger
from .prove import describe, run_prove
from .rootcause import DEFAULT_PROMPT, ROOTCAUSE_PATH, describe_rootcause, run_rootcause
from .rules import audit_call
from .tracker import ship, ticket
from .transcribe import transcribe_call

RUNS = AUDITS_PATH.parent
LEDGERS_PATH = RUNS / "ledgers.jsonl"
ERRORS_PATH = RUNS / "errors.jsonl"
LEDGERS_JEV_PATH = RUNS / "ledgers_jev.jsonl"
GRADE_SAMPLE_PATH = RUNS / "grade_sample.json"
GOLDEN_INPUTS = {
    "hand_grades.jsonl": RUNS / "hand_grades.jsonl",
    "audits.jsonl": AUDITS_PATH,
    "ledgers.jsonl": LEDGERS_PATH,
    "causes.json": CAUSES_PATH,
}
GOLDEN_CHECK_INPUTS = {  # run_golden_check keyword paths
    "golden_dir": GOLDEN_DIR,
    "calls_path": CALLS_PATH,
    "ledgers_path": LEDGERS_PATH,
    "audits_path": AUDITS_PATH,
    "patches_path": PATCHES_PATH,
    "gates_path": GATES_PATH,
    "runs_path": GOLDEN_RUNS_PATH,
}


LOCK_MSG = "another audit run is active"


def run_audit(
    calls_path=CALLS_PATH,
    ledgers_path=LEDGERS_PATH,
    audits_path=AUDITS_PATH,
    errors_path=ERRORS_PATH,
    workers: int = 4,
    limit: int | None = None,
    calls: set[str] | None = None,
    on_done=None,
) -> dict:
    """Hold an exclusive lock on <runs>/.audit.lock for the whole run. Exit if another run holds it.

    `calls` limits the run to those call ids and re-audits them: their old audit rows are dropped first.
    `on_done(call_id)` runs after each call finishes, with or without an error.
    """
    lock_path = Path(audits_path).parent / ".audit.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lf:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit(LOCK_MSG) from None
        try:
            return _run_audit(calls_path, ledgers_path, audits_path, errors_path, workers, limit, calls, on_done)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _run_audit(calls_path, ledgers_path, audits_path, errors_path, workers, limit, only=None, on_done=None) -> dict:
    # duplicate input calls: first wins
    calls = [CallRecord.model_validate(r) for r in dedupe_by_call_id(read_jsonl(calls_path), last_wins=False)]
    if only is not None:
        calls = [c for c in calls if c.call_id in only]
    if limit is not None:
        calls = calls[:limit]
    audits_path, ledgers_path, errors_path = Path(audits_path), Path(ledgers_path), Path(errors_path)
    if only is not None:  # a chosen call always gets a fresh grade: drop its old rows (the ledger cache keeps it cheap)
        _drop_audits(audits_path, {c.call_id for c in calls})
    done = {r["call_id"] for r in read_jsonl(audits_path) if "call_id" in r}  # done = has an audit row
    todo = [c for c in calls if c.call_id not in done]
    skipped = len(calls) - len(todo)
    for p in (audits_path, ledgers_path, errors_path):
        p.parent.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    verdicts: Counter = Counter()
    errors = 0

    def work(call: CallRecord):
        led = extract_ledger(call)
        return led, audit_call(call, led)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(work, c): c for c in todo}
        for fut in as_completed(futs):
            call = futs[fut]
            if on_done:
                on_done(call.call_id)
            try:
                led, aud = fut.result()
            except Exception as e:  # one bad call must not stop the run
                errors += 1
                with lock:
                    append_jsonl(errors_path, [{"call_id": call.call_id, "error": f"{type(e).__name__}: {e}"}])
                continue
            with lock:
                append_jsonl(ledgers_path, [led.model_dump(mode="json")])
                append_jsonl(audits_path, [aud.model_dump(mode="json")])  # audit last: it marks the call done
            verdicts[aud.verdict] += 1
    return {"processed": len(todo) - errors, "skipped": skipped, "errors": errors, "verdicts": dict(verdicts)}


def _drop_audits(path: Path, ids: set[str]) -> None:
    """Remove the audit rows of these call ids. The file is replaced atomically; a missing file is left alone."""
    rows = read_jsonl(path)
    if not any(r.get("call_id") in ids for r in rows):
        return
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text("".join(json.dumps(r) + "\n" for r in rows if r.get("call_id") not in ids), encoding="utf-8")
    os.replace(tmp, path)


def run_judge(
    calls_path=CALLS_PATH,
    ledgers_path=LEDGERS_PATH,
    out_path=LEDGERS_JEV_PATH,
    errors_path=ERRORS_PATH,
    workers: int = 4,
    calls_only: set[str] | None = None,
    on_done=None,
    raw_path=None,
) -> dict:
    """Replace the judgement labels of every saved ledger with Jev's. Writes ledgers_jev.jsonl; resumes by call id.

    Also writes jev_raw.jsonl next to it (raw_path): the raw Jev answers of each call, so the console can re-score
    thresholds with no request. A call is skipped only when both its ledger row and its raw row match the fingerprint,
    so a run backfills raw rows of calls judged before the file existed (Jev cache hits, no request).

    Several focused Jev requests per call. If any fails, the call goes to the errors file and the run goes on. A missing
    TYPESAFE_API_KEY stops the run.
    """
    calls = {r["call_id"]: r for r in dedupe_by_call_id(read_jsonl(calls_path), last_wins=False)}
    ledgers = dedupe_by_call_id(read_jsonl(ledgers_path), last_wins=True)
    if calls_only is not None:
        ledgers = [r for r in ledgers if r["call_id"] in calls_only]
    raw_path = Path(raw_path) if raw_path else Path(out_path).parent / "jev_raw.jsonl"
    stored = {r["call_id"]: r.get("jev_fp") for r in read_jsonl(out_path) if "call_id" in r}  # last row wins
    stored_raw = {r["call_id"]: r.get("jev_fp") for r in read_jsonl(raw_path) if "call_id" in r}
    refresh = os.environ.get("VANILOOP_CACHE") == "refresh"

    def fp(r: dict) -> str:
        return fingerprint(CallRecord.model_validate(calls[r["call_id"]]), Ledger.model_validate(r))

    lock = threading.Lock()
    errors = invalid = 0
    done: set[str] = set()
    todo = []
    for r in (r for r in ledgers if r.get("call_id") in calls):
        try:  # a malformed row goes to the errors file; it must not stop the run
            want = fp(r)
            same = not refresh and stored.get(r["call_id"]) == want and stored_raw.get(r["call_id"]) == want
        except Exception as e:
            invalid += 1
            append_jsonl(errors_path, [{"call_id": r["call_id"], "error": f"jev invalid row {type(e).__name__}"}])
            continue
        (done.add(r["call_id"]) if same else todo.append(r))

    def work(r: dict) -> tuple[dict, dict]:
        call, src = CallRecord.model_validate(calls[r["call_id"]]), Ledger.model_validate(r)
        raw = ask(call, src)
        key = fp(r)
        return {**labels(raw, src).model_dump(mode="json"), "jev_fp": key}, {
            "call_id": r["call_id"],
            "jev_fp": key,
            "requests": raw,
        }

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(work, r): r for r in todo}
        for fut in as_completed(futs):
            if on_done:
                on_done(futs[fut]["call_id"])
            try:
                led, raw_row = fut.result()
            except Exception as e:  # one bad call must not stop the run
                errors += 1
                with lock:
                    append_jsonl(
                        errors_path, [{"call_id": futs[fut]["call_id"], "error": f"jev {type(e).__name__}: {e}"}]
                    )
                continue
            try:
                with lock:  # raw row first: no ledger row without one, and a call without a ledger row is redone
                    append_jsonl(raw_path, [raw_row])
                    append_jsonl(out_path, [led])
            except Exception as e:  # a failed write is a call error; the run goes on
                errors += 1
                with lock:
                    append_jsonl(
                        errors_path, [{"call_id": futs[fut]["call_id"], "error": f"jev write {type(e).__name__}: {e}"}]
                    )
    return {"judged": len(todo) - errors, "skipped": len(done), "errors": errors + invalid}


def describe_agreement(rows: list[dict]) -> str:
    lines = ["field                  n  agree  kappa  none(gemini/jev)"]
    for r in rows:
        k = "n/a" if r["kappa"] is None else f"{r['kappa']:.2f}"
        lines.append(f"{r['field']:20} {r['n']:4} {r['agree']:6} {k:>6}  {r['none_a']}/{r['none_b']}")
    return "\n".join(lines)


def run_grade_sample(
    n: int = 20,
    audits_path: Path = AUDITS_PATH,
    out_path: Path = GRADE_SAMPLE_PATH,
) -> list[str]:
    """Pick a verdict-spread sample of audited calls and write the ids to grade_sample.json atomically."""
    ids = grade_sample(load_audits(Path(audits_path)), n=n)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(ids, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, out)  # atomic, same pattern as issue.py
    return ids


def run_regrade(
    calls_path=CALLS_PATH, ledgers_path=LEDGERS_PATH, audits_path=AUDITS_PATH, only: set[str] | None = None
) -> dict:
    """Re-apply the Python rules to saved ledgers and rewrite audits.jsonl. No LLM call, no cost.

    Calibration changes rules, not labels, so every rule change can be re-scored on all audited calls in
    seconds. Calls without a saved ledger are left out. The file is replaced atomically under the audit lock.
    With `only`, just those call ids are regraded and every other audit row stays.
    """
    lock_path = Path(audits_path).parent / ".audit.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lf:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit(LOCK_MSG) from None
        try:
            calls = {r["call_id"]: r for r in dedupe_by_call_id(read_jsonl(calls_path), last_wins=False)}
            ledgers = dedupe_by_call_id(read_jsonl(ledgers_path), last_wins=True)
            if only is not None:
                ledgers = [r for r in ledgers if r.get("call_id") in only]
            if not calls or not ledgers:  # never replace real audits with an empty file
                raise SystemExit(f"regrade needs calls and ledgers; found {len(calls)} calls, {len(ledgers)} ledgers")
            audits = [
                audit_call(CallRecord.model_validate(calls[r["call_id"]]), Ledger.model_validate(r))
                for r in ledgers
                if r.get("call_id") in calls
            ]
            if not audits:  # calls and ledgers share no call id: keep the audits that exist
                raise SystemExit(f"regrade found no ledger for any of the {len(calls)} calls; audits left unchanged")
            out = Path(audits_path)
            tmp = out.with_suffix(f".{os.getpid()}.tmp")
            keep = [r for r in read_jsonl(out) if only is not None and r.get("call_id") not in only]
            tmp.write_text(
                "".join(json.dumps(r) + "\n" for r in keep)
                + "".join(json.dumps(a.model_dump(mode="json")) + "\n" for a in audits),
                encoding="utf-8",
            )
            os.replace(tmp, out)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)
    return {"regraded": len(audits), "verdicts": dict(Counter(a.verdict for a in audits))}


def run_golden(cmd: str, n: int = 150, seed: int = 0) -> None:
    """`golden build` freezes the next version. `golden agreement` scores the audit on the latest version."""
    if cmd == "build":
        path = build_golden(
            grades_path=GOLDEN_INPUTS["hand_grades.jsonl"],
            audits_path=GOLDEN_INPUTS["audits.jsonl"],
            ledgers_path=GOLDEN_INPUTS["ledgers.jsonl"],
            causes_path=GOLDEN_INPUTS["causes.json"],
            out_dir=GOLDEN_DIR,
            n=n,
            seed=seed,
        )
        doc = load_golden(path)
        print(f"{path.name}: {doc['n']} calls sha256={doc['sha256']} -> {path}")
        if doc.get("short_of"):
            print(f"  Pass majority could not be met at full size: {doc['n']} calls instead of {doc['short_of']}")
        return
    path = latest_golden(GOLDEN_DIR)
    if path is None:
        raise SystemExit("no golden set yet: run `golden build` after grading calls")
    doc = load_golden(path)
    rules = golden_agreement(doc, load_audits(Path(GOLDEN_INPUTS["audits.jsonl"])))
    print(f"{path.name}: {doc['n']} calls (bar: kappa 0.6, n 10, coverage 0.8)")
    for param, r in rules.items():
        kappa = "n/a" if r["kappa"] is None else f"{r['kappa']:.2f}"
        sk = r["skipped"]
        print(
            f"  {r['status']:8} {param}: kappa={kappa} agree={r['agree']}/{r['n']} coverage={r['coverage']:.2f} "
            f"skipped(no_audit={sk['no_audit']} no_finding={sk['no_finding']} unknown={sk['unknown']})"
        )


def _upsert_calls(path: Path, records: list[CallRecord]) -> None:
    """Write these records into calls.jsonl. A call with the same id is replaced, so a resubmit is fresh."""
    new = {r.call_id: r.model_dump(mode="json") for r in records}
    path.parent.mkdir(parents=True, exist_ok=True)
    with (path.parent / ".calls.lock").open("a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)  # wait: read, modify and replace must not interleave with another submit
        try:
            rows = [r for r in read_jsonl(path) if r.get("call_id") not in new] + list(new.values())
            tmp = path.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
            os.replace(tmp, path)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


CALL_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,120}$")


def _check_id(call_id: str) -> None:
    """Reject a call id that is not a plain file-name token. It names files in the workspace and inbox."""
    if not CALL_ID_RE.fullmatch(call_id) or ".." in call_id:
        raise ValueError(f"invalid call id {call_id!r}")


def _inside(path: Path, root: Path) -> None:
    if not Path(path).resolve().is_relative_to(Path(root).resolve()):
        raise ValueError("path outside the folder")


def _step(name: str, i: int, n: int, call_id: str) -> None:
    print(f"STEP {name} {i}/{n} {call_id}", flush=True)


def run_submit(runs: Path, inbox: Path, calls: list[str] | None = None, from_runs: Path | None = None) -> dict:
    """Audit uploaded and existing calls end to end into the workspace `runs`.

    Inbox `*.mp3` is transcribed, inbox `*.json` is a transcript, `calls` are copied from `<from_runs>/calls.jsonl`.
    Then audit (Gemini ledger), judge (Jev) and regrade the Jev ledgers into `<runs>/audits.jsonl`. A bad input or a
    failed call gets one row in errors.jsonl and the run goes on. Prints one STEP line per step and SUBMIT_DONE.
    """
    runs, inbox = Path(runs), Path(inbox)
    p = workspace_paths(runs)
    src = [("mp3", f) for f in sorted(inbox.glob("*.mp3"))] + [("json", f) for f in sorted(inbox.glob("*.json"))]
    wanted = list(calls or [])
    n = len(src) + len(wanted)
    records, i = [], 0
    errors_before = len(read_jsonl(p["errors"]))

    def fail(call_id: str, msg: str) -> None:
        append_jsonl(p["errors"], [{"call_id": call_id, "error": msg}])

    for kind, f in src:
        i += 1
        _step("ingest", i, n, f.stem)
        try:
            _check_id(f.stem)
            if kind == "mp3":
                out = transcribe_call(f, out_dir=runs / "transcripts", log_path=runs / "transcribe_log.jsonl")
                if out.get("status") not in ("ok", "exists"):
                    raise ValueError(f"transcribe {out.get('error') or out.get('status')}")
                rec = load_transcript(runs / "transcripts" / f"{f.stem}.json", audio_dir=inbox)
                if rec.audio_path:
                    rec.silences = detect_silences(rec.audio_path)
            else:
                rec = load_transcript(f, audio_dir=inbox)
            _check_id(rec.call_id)
            if rec.audio_path:
                _inside(Path(rec.audio_path), inbox)
            records.append(rec)
        except Exception as e:  # one bad file must not stop the run
            fail(f.stem, f"ingest {type(e).__name__}: {e}"[:300])
    existing = {r["call_id"]: r for r in dedupe_by_call_id(read_jsonl(Path(from_runs or RUNS) / "calls.jsonl"))}
    for cid in wanted:
        i += 1
        _step("ingest", i, n, cid)
        try:
            _check_id(cid)
            records.append(CallRecord.model_validate(existing[cid]))
        except Exception as e:
            fail(cid, f"ingest {type(e).__name__}: {e}"[:300])
    ids = list(dict.fromkeys(r.call_id for r in records))
    if ids:
        _upsert_calls(p["calls"], records)
        for name, run in (
            (
                "audit",
                lambda cb: run_audit(p["calls"], p["ledgers"], p["audits"], p["errors"], calls=set(ids), on_done=cb),
            ),
            (
                "judge",
                lambda cb: run_judge(
                    p["calls"], p["ledgers"], p["ledgers_jev"], p["errors"], calls_only=set(ids), on_done=cb
                ),
            ),
        ):
            done = [0]

            def cb(cid, name=name, done=done):
                done[0] += 1
                _step(name, done[0], len(ids), cid)

            try:
                run(cb)  # a failed call has its own error row
            except SystemExit as e:  # the audit lock is held by another run
                fail("-", f"{name} {e}")
        _step("regrade", 1, 1, "all")
        jev = {r["call_id"]: r for r in read_jsonl(p["ledgers_jev"]) if "call_id" in r}  # last row wins
        gem = {r["call_id"]: r for r in read_jsonl(p["ledgers"]) if "call_id" in r}
        by_id = {r.call_id: r for r in records}
        current = set()
        for c in ids:  # a Jev ledger counts only if it was made from this call and this Gemini ledger
            try:
                if jev[c].get("jev_fp") == fingerprint(by_id[c], Ledger.model_validate(gem[c])):
                    current.add(c)
                    continue
            except Exception:
                pass
            fail(c, "jev ledger stale or missing")
        if current:
            try:
                run_regrade(p["calls"], p["ledgers_jev"], p["audits"], only=current)
            except (SystemExit, Exception) as e:
                fail("-", f"regrade {type(e).__name__}: {e}"[:300])
        _drop_audits(p["audits"], set(ids) - current)  # no current Jev ledger: no final grade
    audited = {r.get("call_id") for r in read_jsonl(p["audits"])}
    n_calls = sum(c in audited for c in ids)
    errors = len(read_jsonl(p["errors"])) - errors_before
    print(f"SUBMIT_DONE calls={n_calls} errors={errors}", flush=True)
    return {"calls": n_calls, "errors": errors}


def workspace_paths(runs: Path | None) -> dict[str, Path]:
    """Every file a stage reads or writes, inside `runs`. None gives the repo defaults, so nothing changes."""
    if runs is None:
        return {
            "calls": CALLS_PATH, "ledgers": LEDGERS_PATH, "ledgers_jev": LEDGERS_JEV_PATH, "audits": AUDITS_PATH,
            "errors": ERRORS_PATH, "causes": CAUSES_PATH, "patches": PATCHES_PATH, "gates": GATES_PATH,
            "replays": REPLAYS_PATH, "categories": CATEGORIES_PATH, "tickets": TICKETS_DIR,
        }  # fmt: skip
    r = Path(runs)
    return {
        "calls": r / "calls.jsonl", "ledgers": r / "ledgers.jsonl", "ledgers_jev": r / "ledgers_jev.jsonl",
        "audits": r / "audits.jsonl", "errors": r / "errors.jsonl", "causes": r / "causes.json",
        "patches": r / "patches.json", "gates": r / "gates.jsonl", "replays": r / "replays.jsonl",
        "categories": r / "categories.json", "tickets": r / "tickets",
    }  # fmt: skip


def cause_lines(causes: list, outliers: list[dict]) -> list[str]:
    """One CLI line per cause: its status and counts, and for a non-pattern the conditions it failed."""
    failed = {r["cause_id"]: r["failed"] for r in outliers}
    return [
        f"  [{c.status}] {c.parameter}: fatal={len(c.fatal_calls)} nonfatal={len(c.nonfatal_calls)} "
        f"priority={c.priority:g} symptoms={len(c.symptom_calls)}"
        + (f" failed: {', '.join(failed[c.cause_id])}" if c.cause_id in failed else "")
        for c in causes
    ]


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="vaniloop.pipeline")
    ap.add_argument("--runs", default=None, help="data folder for every stage's files (default: runs/)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("audit")
    a.add_argument("--workers", type=int, default=4)
    a.add_argument("--limit", type=int, default=None)
    a.add_argument("--calls", default=None, help="id1,id2: audit only these calls, fresh")
    sub.add_parser("issues")
    rg = sub.add_parser("regrade")
    rg.add_argument("--ledgers", default=None, help="ledgers file to grade, e.g. runs/ledgers_jev.jsonl")
    rg.add_argument("--audits", default=None, help="audits file to rewrite")
    jd = sub.add_parser("judge", help="Jev makes the judgement labels: ledgers.jsonl -> ledgers_jev.jsonl")
    jd.add_argument("--workers", type=int, default=4)
    jd.add_argument("--calls", default=None, help="id1,id2: judge only these calls")
    tn = sub.add_parser("tune", help="score Jev thresholds against the reviewed truth; prints JSON")
    tn.add_argument(
        "--thresholds", default="{}", help="JSON in the shape of POST /api/tune; missing keys keep the current"
    )
    ja = sub.add_parser("judge-agreement", help="Gemini vs Jev agreement and kappa per field")
    ja.add_argument("--a", default=None)
    ja.add_argument("--b", default=None)
    sub.add_parser("categorize")
    sm = sub.add_parser("submit", help="audit uploaded mp3 or transcript files and existing calls into --runs")
    sm.add_argument("--inbox", required=True, help="folder of *.mp3 and *.json transcripts")
    sm.add_argument("--calls", default=None, help="id1,id2: existing calls to copy from --from")
    sm.add_argument("--from", dest="from_runs", default=None, help="folder holding calls.jsonl (default: runs/)")
    f = sub.add_parser("fix")
    f.add_argument("--prompt", default=None, help="the VANI prompt file; not needed with --dry-run")
    f.add_argument("--top", type=int, default=3)
    f.add_argument("--dry-run", action="store_true", help="list patch / ticket / watch per cause; no LLM call")
    f.add_argument("--holdout", action="store_true", help="write the patch from fix-half calls only")
    rf = sub.add_parser("refix")
    rf.add_argument("--cause", required=True)
    rf.add_argument("--prompt", required=True)
    rf.add_argument("--holdout", action="store_true", help="fix-half evidence only; no failing replay lines")
    pv = sub.add_parser("prove")
    pv.add_argument("--cause", required=True)
    pv.add_argument("--prompt", required=True)
    pv.add_argument("--n", type=int, default=5)
    pv.add_argument("--k", type=int, default=3)
    pv.add_argument("--guards", type=int, default=10)
    pv.add_argument("--max-turns", type=int, default=12)
    pv.add_argument("--workers", type=int, default=1)
    pv.add_argument("--dry-run", action="store_true")
    pv.add_argument("--attempt", type=int, default=1)
    pv.add_argument("--holdout", action="store_true", help="targets from hold-out calls only")
    pv.add_argument("--screen", action="store_true", help="keep calls OLD fails on 2 of 3 seeds; gate on seeds 3..")
    sh = sub.add_parser("ship", help="make the next prompt version from a cause's patch that the gate shipped")
    sh.add_argument("--cause", required=True)
    sh.add_argument("--prompt", default=None, help="the clean base VANI prompt file (default: the reference copy)")
    tk = sub.add_parser("ticket", help="record that a ticket was sent or resolved")
    tk.add_argument("--cause", required=True)
    tk.add_argument("--sent", metavar="REF", default=None, help="the ticket id or link")
    tk.add_argument("--resolved", metavar="NOTE", default=None)
    rc = sub.add_parser("rootcause")
    rc.add_argument("--cause", required=True)
    rc.add_argument("--prompt", default=str(DEFAULT_PROMPT))
    rc.add_argument("--k", type=int, default=3)
    rc.add_argument("--max-turns", type=int, default=12)
    rc.add_argument("--workers", type=int, default=1)
    rc.add_argument("--dry-run", action="store_true")
    g = sub.add_parser("grade-sample")
    g.add_argument("--n", type=int, default=20)
    gd = sub.add_parser("golden")
    gsub = gd.add_subparsers(dest="golden_cmd", required=True)
    gb = gsub.add_parser("build")
    gb.add_argument("--n", type=int, default=150)
    gb.add_argument("--seed", type=int, default=0)
    gsub.add_parser("agreement")
    gc = gsub.add_parser("check")
    gc.add_argument("--version", required=True)
    gc.add_argument("--prompt", required=True)
    gc.add_argument("--max-turns", type=int, default=12)
    gc.add_argument("--workers", type=int, default=1)
    gc.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    P = workspace_paths(Path(args.runs) if args.runs else None)

    def extra(**names):  # path options a stage gets only under --runs, so a run without it passes what it always did
        return {k: P[v] for k, v in names.items()} if args.runs else {}

    try:  # overrides from VANILOOP_RUBRIC, else <--runs or runs/>/rubric.json; none when the file is absent
        rubric.apply(rubric.path(Path(args.runs) if args.runs else None))
    except rubric.RubricError as e:
        raise SystemExit(f"rubric: {e}") from None
    ids = [c for c in (getattr(args, "calls", None) or "").split(",") if c]
    if args.cmd == "submit":
        if not args.runs:
            ap.error("submit needs --runs WS")
        run_submit(Path(args.runs), Path(args.inbox), ids, Path(args.from_runs) if args.from_runs else None)
    elif args.cmd == "audit":
        s = run_audit(
            P["calls"], P["ledgers"], P["audits"], P["errors"],
            workers=args.workers, limit=args.limit, calls=set(ids) if ids else None,
        )  # fmt: skip
        print(f"processed={s['processed']} skipped={s['skipped']} errors={s['errors']} verdicts={s['verdicts']}")
    elif args.cmd == "regrade":
        s = run_regrade(P["calls"], args.ledgers or P["ledgers"], args.audits or P["audits"])
        print(f"regraded={s['regraded']} verdicts={s['verdicts']}")
    elif args.cmd == "judge":
        s = run_judge(
            P["calls"], P["ledgers"], P["ledgers_jev"], P["errors"],
            workers=args.workers, calls_only=set(ids) if ids else None,
        )  # fmt: skip
        print(f"judged={s['judged']} skipped={s['skipped']} errors={s['errors']}")
    elif args.cmd == "tune":
        try:
            print(
                json.dumps(
                    tune.score(Path(args.runs) if args.runs else RUNS, json.loads(args.thresholds)), ensure_ascii=False
                )
            )
        except (tune.NoRaw, ValueError) as e:
            raise SystemExit(f"tune: {e}") from None
    elif args.cmd == "judge-agreement":
        print(
            describe_agreement(
                judge_agreement(read_jsonl(args.a or P["ledgers"]), read_jsonl(args.b or P["ledgers_jev"]))
            )
        )
    elif args.cmd == "categorize":
        s = run_categorize(ledgers_path=P["ledgers"], **extra(out_path="categories"))
        print(
            f"names={s['names']} labelled={s['labelled']} cached={s['cached']} errors={s['errors']} "
            f"-> {P['categories']}"
        )
    elif args.cmd == "fix":
        if args.prompt is None and not args.dry_run:
            ap.error("fix needs --prompt unless --dry-run")
        kw = {"prompt_path": Path(args.prompt)} if args.prompt else {}
        if args.holdout:
            kw["holdout"] = True
        r = run_fix(
            causes_path=P["causes"],
            out_path=P["patches"],
            top_n=args.top,
            dry_run=args.dry_run,
            calls_path=P["calls"],
            **extra(audits_path="audits", gates_path="gates", tickets_dir="tickets"),
            **kw,
        )
        if args.dry_run:
            for x in r["plan"]:
                print(f"  {x['action']:6} {x['cause_id']} [{x['status']}] type={x['type']} "
                      f"suspected={x['suspected_type'] or '-'}: {x['why']}")  # fmt: skip
            return
        print(
            f"{len(r['patches'])} patches, {len(r['rejected'])} rejected, {len(r['tickets'])} tickets, "
            f"{len(r['watched'])} watched -> {P['patches']}"
        )
        for x in r["rejected"]:
            print(f"  rejected {x['cause_id']}: {x['reason']}")
        for cid in r["tickets"]:
            print(f"  ticket {cid}: runs/tickets/{cid}.md")
        for cid in r["watched"]:
            print(f"  watch {cid}")
    elif args.cmd == "refix":
        r = run_refix(
            args.cause,
            Path(args.prompt),
            calls_path=P["calls"],
            **extra(
                causes_path="causes",
                patches_path="patches",
                gates_path="gates",
                replays_path="replays",
                audits_path="audits",
            ),  # fmt: skip
            **({"holdout": True} if args.holdout else {}),
        )
        print(f"{len(r['patches'])} patches, {len(r['rejected'])} rejected -> {P['patches']}")
        for x in r["rejected"]:
            print(f"  rejected {x['cause_id']}: {x['reason']}")
        for p in r["patches"]:
            print(f"  new patch for {p['cause_id']}: attempt {p['attempt']}; next: prove --attempt {p['attempt']}")
    elif args.cmd == "prove":
        out = run_prove(
            args.cause,
            Path(args.prompt),
            n=args.n,
            k=args.k,
            guards=args.guards,
            max_turns=args.max_turns,
            workers=args.workers,
            dry_run=args.dry_run,
            attempt=args.attempt,
            causes_path=P["causes"],
            **extra(
                calls_path="calls",
                ledgers_path="ledgers",
                audits_path="audits",
                patches_path="patches",
                gates_path="gates",
                replays_path="replays",
            ),  # fmt: skip
            **({"holdout": True} if args.holdout else {}),
            **({"screen": True} if args.screen else {}),
        )
        print("\n".join(describe(out)))
    elif args.cmd == "ship":
        kw = {"prompt_path": Path(args.prompt)} if args.prompt else {}
        version = ship(args.cause, P["patches"].parent, patches_path=P["patches"], **kw)
        print(f"{args.cause} shipped in prompt version {version}")
    elif args.cmd == "ticket":
        print(f"{args.cause}: {ticket(args.cause, P['patches'].parent, sent=args.sent, resolved=args.resolved)}")
    elif args.cmd == "rootcause":
        out = run_rootcause(
            args.cause,
            Path(args.prompt),
            k=args.k,
            max_turns=args.max_turns,
            workers=args.workers,
            dry_run=args.dry_run,
        )
        print("\n".join(describe_rootcause(args.cause, out)))
        if not args.dry_run:
            print(f"rows -> {ROOTCAUSE_PATH}")
    elif args.cmd == "grade-sample":
        ids = run_grade_sample(n=args.n)
        print(f"{len(ids)} calls -> {GRADE_SAMPLE_PATH}")
        for call_id in ids:
            print(f"  {call_id}")
    elif args.cmd == "golden" and args.golden_cmd == "check":
        out = run_golden_check(
            args.version,
            Path(args.prompt),
            dry_run=args.dry_run,
            max_turns=args.max_turns,
            workers=args.workers,
            **GOLDEN_CHECK_INPUTS,
        )
        print("\n".join(describe_check(out)))
    elif args.cmd == "golden":
        run_golden(args.golden_cmd, getattr(args, "n", 150), getattr(args, "seed", 0))
    else:
        causes = run_issues(P["audits"], P["causes"])
        print(f"{len(causes)} causes -> {P['causes']}")
        for line in cause_lines(causes, read_jsonl(P["causes"].with_name("outliers.jsonl"))):
            print(line)


if __name__ == "__main__":
    main()
