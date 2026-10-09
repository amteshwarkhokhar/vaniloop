"""Pipeline runner. `python -m vaniloop.pipeline audit|regrade|issues|fix|grade-sample`."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .fix import PATCHES_PATH, run_fix
from .grading import grade_sample
from .ingest import OUT_PATH as CALLS_PATH
from .issue import AUDITS_PATH, CAUSES_PATH, load_audits, run_issues
from .jsonl import append_jsonl, dedupe_by_call_id, read_jsonl
from .ledger import extract_ledger
from .models import CallRecord, Ledger
from .rules import audit_call

RUNS = AUDITS_PATH.parent
LEDGERS_PATH = RUNS / "ledgers.jsonl"
ERRORS_PATH = RUNS / "errors.jsonl"
GRADE_SAMPLE_PATH = RUNS / "grade_sample.json"


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
            audits = [
                audit_call(CallRecord.model_validate(calls[r["call_id"]]), Ledger.model_validate(r))
                for r in ledgers
                if r.get("call_id") in calls
            ]
            out = Path(audits_path)
            tmp = out.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text("".join(json.dumps(a.model_dump(mode="json")) + "\n" for a in audits), encoding="utf-8")
            os.replace(tmp, out)
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)
    return {"regraded": len(audits), "verdicts": dict(Counter(a.verdict for a in audits))}


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
    g = sub.add_parser("grade-sample")
    g.add_argument("--n", type=int, default=20)
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
    elif args.cmd == "grade-sample":
        ids = run_grade_sample(n=args.n)
        print(f"{len(ids)} calls -> {GRADE_SAMPLE_PATH}")
        for call_id in ids:
            print(f"  {call_id}")
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
