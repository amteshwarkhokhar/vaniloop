"""Pipeline runner. `python -m vaniloop.pipeline audit|regrade|issues|fix|refix|prove|grade-sample|golden`."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .fix import PATCHES_PATH, run_fix, run_refix
from .golden import GOLDEN_DIR, build_golden, golden_agreement, latest_golden, load_golden
from .grading import grade_sample
from .ingest import OUT_PATH as CALLS_PATH
from .issue import AUDITS_PATH, CAUSES_PATH, load_audits, run_issues
from .jsonl import append_jsonl, dedupe_by_call_id, read_jsonl
from .ledger import extract_ledger
from .models import CallRecord, Ledger
from .prove import describe, run_prove
from .rules import audit_call

RUNS = AUDITS_PATH.parent
LEDGERS_PATH = RUNS / "ledgers.jsonl"
ERRORS_PATH = RUNS / "errors.jsonl"
GRADE_SAMPLE_PATH = RUNS / "grade_sample.json"
GOLDEN_INPUTS = {
    "hand_grades.jsonl": RUNS / "hand_grades.jsonl",
    "audits.jsonl": AUDITS_PATH,
    "ledgers.jsonl": LEDGERS_PATH,
    "causes.json": CAUSES_PATH,
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
        return
    path = latest_golden(GOLDEN_DIR)
    if path is None:
        raise SystemExit("no golden set yet: run `golden build` after grading calls")
    doc = load_golden(path)
    rules = golden_agreement(doc, load_audits(Path(GOLDEN_INPUTS["audits.jsonl"])))
    print(f"{path.name}: {doc['n']} calls (kappa bar 0.6, n bar 10)")
    for param, r in rules.items():
        kappa = "n/a" if r["kappa"] is None else f"{r['kappa']:.2f}"
        print(f"  {r['status']:8} {param}: kappa={kappa} agree={r['agree']}/{r['n']}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="vaniloop.pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("audit")
    a.add_argument("--workers", type=int, default=4)
    a.add_argument("--limit", type=int, default=None)
    sub.add_parser("issues")
    sub.add_parser("regrade")
    f = sub.add_parser("fix")
    f.add_argument("--prompt", required=True)
    f.add_argument("--top", type=int, default=3)
    rf = sub.add_parser("refix")
    rf.add_argument("--cause", required=True)
    rf.add_argument("--prompt", required=True)
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
    g = sub.add_parser("grade-sample")
    g.add_argument("--n", type=int, default=20)
    gd = sub.add_parser("golden")
    gsub = gd.add_subparsers(dest="golden_cmd", required=True)
    gb = gsub.add_parser("build")
    gb.add_argument("--n", type=int, default=150)
    gb.add_argument("--seed", type=int, default=0)
    gsub.add_parser("agreement")
    args = ap.parse_args(argv)
    if args.cmd == "audit":
        s = run_audit(workers=args.workers, limit=args.limit)
        print(f"processed={s['processed']} skipped={s['skipped']} errors={s['errors']} verdicts={s['verdicts']}")
    elif args.cmd == "regrade":
        s = run_regrade()
        print(f"regraded={s['regraded']} verdicts={s['verdicts']}")
    elif args.cmd == "fix":
        r = run_fix(prompt_path=Path(args.prompt), top_n=args.top)
        print(f"{len(r['patches'])} patches, {len(r['rejected'])} rejected -> {PATCHES_PATH}")
        for x in r["rejected"]:
            print(f"  rejected {x['cause_id']}: {x['reason']}")
    elif args.cmd == "refix":
        r = run_refix(args.cause, Path(args.prompt))
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
        )
        print("\n".join(describe(out)))
    elif args.cmd == "grade-sample":
        ids = run_grade_sample(n=args.n)
        print(f"{len(ids)} calls -> {GRADE_SAMPLE_PATH}")
        for call_id in ids:
            print(f"  {call_id}")
    elif args.cmd == "golden":
        run_golden(args.golden_cmd, getattr(args, "n", 150), getattr(args, "seed", 0))
    else:
        causes = run_issues()
        print(f"{len(causes)} causes -> {CAUSES_PATH}")
        for c in causes:
            print(
                f"  [{c.status}] {c.parameter}: fatal={len(c.fatal_calls)} "
                f"nonfatal={len(c.nonfatal_calls)} priority={c.priority:g}"
            )


if __name__ == "__main__":
    main()
