"""Pipeline runner. `python -m vaniloop.pipeline <command>`: audit, regrade, issues, categorize, fix, refix, prove,
rootcause, grade-sample, golden (build, agreement, check)."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .categorize import CATEGORIES_PATH, run_categorize
from .fix import GATES_PATH, PATCHES_PATH, run_fix, run_refix
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
from .issue import AUDITS_PATH, CAUSES_PATH, load_audits, run_issues
from .jev import fingerprint, judge_agreement, judge_ledger
from .jsonl import append_jsonl, dedupe_by_call_id, read_jsonl
from .ledger import extract_ledger
from .models import CallRecord, Ledger
from .prove import describe, run_prove
from .rootcause import DEFAULT_PROMPT, ROOTCAUSE_PATH, describe_rootcause, run_rootcause
from .rules import audit_call

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
) -> dict:
    """Hold an exclusive lock on <runs>/.audit.lock for the whole run. Exit if another run holds it."""
    lock_path = Path(audits_path).parent / ".audit.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lf:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit(LOCK_MSG) from None
        try:
            return _run_audit(calls_path, ledgers_path, audits_path, errors_path, workers, limit)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _run_audit(calls_path, ledgers_path, audits_path, errors_path, workers, limit) -> dict:
    # duplicate input calls: first wins
    calls = [CallRecord.model_validate(r) for r in dedupe_by_call_id(read_jsonl(calls_path), last_wins=False)]
    if limit is not None:
        calls = calls[:limit]
    audits_path, ledgers_path, errors_path = Path(audits_path), Path(ledgers_path), Path(errors_path)
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


def run_judge(
    calls_path=CALLS_PATH,
    ledgers_path=LEDGERS_PATH,
    out_path=LEDGERS_JEV_PATH,
    errors_path=ERRORS_PATH,
    workers: int = 4,
) -> dict:
    """Replace the judgement labels of every saved ledger with Jev's. Writes ledgers_jev.jsonl; resumes by call id.

    One Jev request per call. A failed call goes to the errors file and the run goes on. A missing
    TYPESAFE_API_KEY stops the run.
    """
    calls = {r["call_id"]: r for r in dedupe_by_call_id(read_jsonl(calls_path), last_wins=False)}
    ledgers = dedupe_by_call_id(read_jsonl(ledgers_path), last_wins=True)
    stored = {r["call_id"]: r.get("jev_fp") for r in read_jsonl(out_path) if "call_id" in r}  # last row wins
    refresh = os.environ.get("VANILOOP_CACHE") == "refresh"

    def fp(r: dict) -> str:
        return fingerprint(CallRecord.model_validate(calls[r["call_id"]]), Ledger.model_validate(r))

    keep = [r for r in ledgers if r.get("call_id") in calls]
    done = set() if refresh else {r["call_id"] for r in keep if stored.get(r["call_id"]) == fp(r)}  # same fingerprint
    todo = [r for r in keep if r["call_id"] not in done]
    lock = threading.Lock()
    errors = 0

    def work(r: dict) -> dict:
        led = judge_ledger(CallRecord.model_validate(calls[r["call_id"]]), Ledger.model_validate(r))
        return {**led.model_dump(mode="json"), "jev_fp": fp(r)}

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(work, r): r for r in todo}
        for fut in as_completed(futs):
            try:
                led = fut.result()
            except Exception as e:  # one bad call must not stop the run
                errors += 1
                with lock:
                    append_jsonl(
                        errors_path, [{"call_id": futs[fut]["call_id"], "error": f"jev {type(e).__name__}: {e}"}]
                    )
                continue
            with lock:
                append_jsonl(out_path, [led])
    return {"judged": len(todo) - errors, "skipped": len(done), "errors": errors}


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


def run_regrade(calls_path=CALLS_PATH, ledgers_path=LEDGERS_PATH, audits_path=AUDITS_PATH) -> dict:
    """Re-apply the Python rules to saved ledgers and rewrite audits.jsonl. No LLM call, no cost.

    Calibration changes rules, not labels, so every rule change can be re-scored on all audited calls in
    seconds. Calls without a saved ledger are left out. The file is replaced atomically under the audit lock.
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
            tmp.write_text("".join(json.dumps(a.model_dump(mode="json")) + "\n" for a in audits), encoding="utf-8")
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
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("audit")
    a.add_argument("--workers", type=int, default=4)
    a.add_argument("--limit", type=int, default=None)
    sub.add_parser("issues")
    rg = sub.add_parser("regrade")
    rg.add_argument("--ledgers", default=str(LEDGERS_PATH), help="ledgers file to grade, e.g. runs/ledgers_jev.jsonl")
    rg.add_argument("--audits", default=str(AUDITS_PATH), help="audits file to rewrite")
    jd = sub.add_parser("judge", help="Jev makes the judgement labels: ledgers.jsonl -> ledgers_jev.jsonl")
    jd.add_argument("--workers", type=int, default=4)
    ja = sub.add_parser("judge-agreement", help="Gemini vs Jev agreement and kappa per field")
    ja.add_argument("--a", default=str(LEDGERS_PATH))
    ja.add_argument("--b", default=str(LEDGERS_JEV_PATH))
    sub.add_parser("categorize")
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
    if args.cmd == "audit":
        s = run_audit(workers=args.workers, limit=args.limit)
        print(f"processed={s['processed']} skipped={s['skipped']} errors={s['errors']} verdicts={s['verdicts']}")
    elif args.cmd == "regrade":
        s = run_regrade(ledgers_path=args.ledgers, audits_path=args.audits)
        print(f"regraded={s['regraded']} verdicts={s['verdicts']}")
    elif args.cmd == "judge":
        s = run_judge(workers=args.workers)
        print(f"judged={s['judged']} skipped={s['skipped']} errors={s['errors']}")
    elif args.cmd == "judge-agreement":
        print(describe_agreement(judge_agreement(read_jsonl(args.a), read_jsonl(args.b))))
    elif args.cmd == "categorize":
        s = run_categorize(ledgers_path=LEDGERS_PATH)
        print(
            f"names={s['names']} labelled={s['labelled']} cached={s['cached']} errors={s['errors']} "
            f"-> {CATEGORIES_PATH}"
        )
    elif args.cmd == "fix":
        if args.prompt is None and not args.dry_run:
            ap.error("fix needs --prompt unless --dry-run")
        kw = {"prompt_path": Path(args.prompt)} if args.prompt else {}
        if args.holdout:
            kw["holdout"] = True
        r = run_fix(
            causes_path=CAUSES_PATH,
            out_path=PATCHES_PATH,
            top_n=args.top,
            dry_run=args.dry_run,
            calls_path=CALLS_PATH,
            **kw,
        )
        if args.dry_run:
            for x in r["plan"]:
                print(f"  {x['action']:6} {x['cause_id']} [{x['status']}] type={x['type']} "
                      f"suspected={x['suspected_type'] or '-'}: {x['why']}")  # fmt: skip
            return
        print(
            f"{len(r['patches'])} patches, {len(r['rejected'])} rejected, {len(r['tickets'])} tickets, "
            f"{len(r['watched'])} watched -> {PATCHES_PATH}"
        )
        for x in r["rejected"]:
            print(f"  rejected {x['cause_id']}: {x['reason']}")
        for cid in r["tickets"]:
            print(f"  ticket {cid}: runs/tickets/{cid}.md")
        for cid in r["watched"]:
            print(f"  watch {cid}")
    elif args.cmd == "refix":
        r = run_refix(
            args.cause, Path(args.prompt), calls_path=CALLS_PATH, **({"holdout": True} if args.holdout else {})
        )
        print(f"{len(r['patches'])} patches, {len(r['rejected'])} rejected -> {PATCHES_PATH}")
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
            causes_path=CAUSES_PATH,
            **({"holdout": True} if args.holdout else {}),
            **({"screen": True} if args.screen else {}),
        )
        print("\n".join(describe(out)))
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
        causes = run_issues()
        print(f"{len(causes)} causes -> {CAUSES_PATH}")
        for line in cause_lines(causes, read_jsonl(CAUSES_PATH.with_name("outliers.jsonl"))):
            print(line)


if __name__ == "__main__":
    main()
