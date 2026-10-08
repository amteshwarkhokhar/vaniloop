"""Pipeline runner. `python -m vaniloop.pipeline audit|issues`."""

from __future__ import annotations

import argparse
import json
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .ingest import OUT_PATH as CALLS_PATH
from .issue import AUDITS_PATH, CAUSES_PATH, run_issues
from .ledger import extract_ledger
from .models import CallRecord
from .rules import audit_call

RUNS = AUDITS_PATH.parent
LEDGERS_PATH = RUNS / "ledgers.jsonl"
ERRORS_PATH = RUNS / "errors.jsonl"


def _read_lines(p: Path) -> list[str]:
    return [l for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []


def run_audit(calls_path=CALLS_PATH, ledgers_path=LEDGERS_PATH, audits_path=AUDITS_PATH,
              errors_path=ERRORS_PATH, workers: int = 4, limit: int | None = None) -> dict:
    calls = [CallRecord.model_validate_json(l) for l in _read_lines(Path(calls_path))]
    if limit is not None:
        calls = calls[:limit]
    audits_path, ledgers_path, errors_path = Path(audits_path), Path(ledgers_path), Path(errors_path)
    done = {json.loads(l)["call_id"] for l in _read_lines(audits_path)}
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

    def append(p: Path, line: str) -> None:
        with p.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {ex.submit(work, c): c for c in todo}
        for fut in as_completed(futs):
            call = futs[fut]
            try:
                led, aud = fut.result()
            except Exception as e:  # one bad call must not stop the run
                errors += 1
                with lock:
                    append(errors_path, json.dumps({"call_id": call.call_id, "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
                continue
            with lock:
                append(ledgers_path, led.model_dump_json())
                append(audits_path, aud.model_dump_json())  # audit last: it marks the call done
            verdicts[aud.verdict] += 1
    return {"processed": len(todo) - errors, "skipped": skipped, "errors": errors, "verdicts": dict(verdicts)}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="vaniloop.pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("audit")
    a.add_argument("--workers", type=int, default=4)
    a.add_argument("--limit", type=int, default=None)
    sub.add_parser("issues")
    args = ap.parse_args(argv)
    if args.cmd == "audit":
        s = run_audit(workers=args.workers, limit=args.limit)
        print(f"processed={s['processed']} skipped={s['skipped']} errors={s['errors']} verdicts={s['verdicts']}")
    else:
        causes = run_issues()
        print(f"{len(causes)} causes -> {CAUSES_PATH}")
        for c in causes:
            print(f"  [{c.status}] {c.parameter}: fatal={len(c.fatal_calls)} nonfatal={len(c.nonfatal_calls)} priority={c.priority:g}")


if __name__ == "__main__":
    main()
