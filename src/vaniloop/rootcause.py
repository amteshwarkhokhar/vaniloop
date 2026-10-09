"""TEST stage, root cause step 2: the channel counterfactual. Is a cause's fault in the audio or in the prompt?

For each root call of a cause, replay the OLD prompt K times with the call's own noisy channel and K times with a
clean one, on the same seeds. The cause parameter is judged after the fork, as the gate does (gate._trial). A fault
that shows only under noise is `channel`. A classifier: no gate decision, no patch.

The experiment is buyer text masking: in the noisy arm a share of buyer turns reaches the bot as `[unclear audio]`.
It never recreates a wrong STT value (a misheard number or word), so `channel` means "fails when buyer text is
masked" and says nothing about audio attribution beyond that. Every saved row carries `"simulation":
"buyer_text_masking"` and the CLI output says the same.

Labels, with K seeds per arm, `f` = failed trials and `u` = unresolved trials (no evidence) of an arm:
  channel       noisy f > K/2 (most seeds) and clean f + u <= K/3 (none or few)
  not_channel   noisy f > K/2 and clean f > K/2: it fails with clean audio too, a candidate prompt fault
  not_reproduced   no failed and no unresolved trial in either arm
  unresolved    anything else, including a call whose replays or audits raised
An unresolved trial is never counted as a pass, so it cannot support `channel` or `not_reproduced`.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from jinja2 import TemplateError

from . import llm, replay
from . import prompt as prompt_mod
from .gate import AuditFn, Item, _trial
from .ingest import OUT_PATH as CALLS_PATH
from .issue import AUDITS_PATH, CAUSES_PATH, load_audits
from .jsonl import dedupe_by_call_id, read_jsonl
from .models import CallRecord, Ledger
from .prove import AUDIO_PARAMETERS, LEDGERS_PATH, Plan, _run_cost, _sent_prompt, _sent_tags, real_audit_fn, target_fork
from .replay import ReplayResult

ROOTCAUSE_PATH = AUDITS_PATH.parent / "rootcause.jsonl"
DEFAULT_PROMPT = AUDITS_PATH.parents[1] / "data" / "reference" / "vani_prompt_sarvam.md"
CHANNEL_CAUSE_PREFIX = "cause_channel_"
LABELS = ("channel", "not_channel", "not_reproduced", "unresolved")
SIMULATION = "buyer_text_masking"  # what the noisy arm simulates; saved on every row and printed by the CLI

# The noisy arm must fail on MORE than this share of the K seeds ("most"); the clean arm may fail (or be unresolved)
# on AT MOST this share ("none or few"). Fractions are compared on integers, so no float rounding decides a label.
MOST_ABOVE = (1, 2)
FEW_AT_MOST = (1, 3)

# call, ledger, fork turn, old prompt, seed, unclear_rate to replay with (the noisy rate of the call, or 0.0)
ChannelReplayFn = Callable[[CallRecord, Ledger, int, str, int, float], ReplayResult]


def _most(n: int, k: int) -> bool:
    return n * MOST_ABOVE[1] > k * MOST_ABOVE[0]


def _few(n: int, k: int) -> bool:
    return n * FEW_AT_MOST[1] <= k * FEW_AT_MOST[0]


def label_call(noisy_fails: int, clean_fails: int, k: int, noisy_unresolved: int = 0, clean_unresolved: int = 0) -> str:
    """The label for one call from its fail and unresolved counts per arm, each out of k seeds (see module doc)."""
    if _most(noisy_fails, k) and _few(clean_fails + clean_unresolved, k):
        return "channel"
    if _most(noisy_fails, k) and _most(clean_fails, k):
        return "not_channel"
    if not (noisy_fails or clean_fails or noisy_unresolved or clean_unresolved):
        return "not_reproduced"
    return "unresolved"


def real_channel_replay_fn(max_turns: int) -> ChannelReplayFn:
    """Replay with the prompt rendered for the call (as prove does). The buyer brief gets the `unclear_rate` given:
    0.0 forces a clean channel without touching replay.py. The buyer's own prompt never carries the rate."""

    def fn(call: CallRecord, ledger: Ledger, fork: int, bot_prompt: str, seed: int, unclear_rate: float):
        sent = _sent_prompt(bot_prompt, call, ledger)
        brief = replay.buyer_brief(call, ledger, fork).model_copy(update={"unclear_rate": unclear_rate})
        return replay.replay_result(call, fork, sent, brief, max_turns, seed)

    return fn


def load_cause(cause_id: str, causes_path: Path) -> dict:
    """The cause row from causes.json. Stop with SystemExit when the file or the cause is missing."""
    try:
        rows = json.loads(Path(causes_path).read_text(encoding="utf-8")).get("causes", [])
    except (OSError, ValueError, AttributeError):
        raise SystemExit(f"cannot read causes from {causes_path}; run `pipeline issues` first") from None
    row = next((c for c in rows if isinstance(c, dict) and c.get("cause_id") == cause_id), None)
    if row is None:
        raise SystemExit(f"{cause_id} is not in {causes_path}")
    return row


def root_calls(cause: dict) -> list[str]:
    """The cause's root calls, sorted: Fatal and NonFatal ones. A symptom call is another cause's root, left out."""
    return sorted({*cause.get("fatal_calls", []), *cause.get("nonfatal_calls", [])})


def _run_call(
    item: Item,
    parameter: str,
    old_prompt: str,
    k: int,
    noisy_rate: float,
    replay_fn: ChannelReplayFn,
    audit_fn: AuditFn,
) -> dict:
    """Replay one call K times per arm on seeds 0..K-1 and count failed and unresolved trials after the fork."""
    call, ledger, fork = item
    counts = {"noisy": [0, 0], "clean": [0, 0]}  # arm -> [failed, unresolved]
    for seed in range(k):
        for arm, rate in (("noisy", noisy_rate), ("clean", 0.0)):
            res = replay_fn(call, ledger, fork, old_prompt, seed, rate)
            trial = _trial(call.call_id, "old", seed, res, audit_fn, parameter)
            counts[arm][0] += trial.failed and not trial.unresolved  # an invalid trial is no evidence either way
            counts[arm][1] += trial.unresolved
    (nf, nu), (cf, cu) = counts["noisy"], counts["clean"]
    return {
        "call_id": call.call_id,
        "noisy_rate": noisy_rate,
        "noisy_fails": nf,
        "clean_fails": cf,
        "noisy_unresolved": nu,
        "clean_unresolved": cu,
        "k": k,
        "label": label_call(nf, cf, k, nu, cu),
    }


def _write_rows(path: Path, cause_id: str, rows: list[dict]) -> None:
    """Replace this cause's rows in the file and keep every other cause's rows. Atomic, like issue.py."""
    path = Path(path)
    kept = [r for r in read_jsonl(path) if r.get("cause_id") != cause_id]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in [*kept, *rows]), encoding="utf-8")
    os.replace(tmp, path)


def run_rootcause(
    cause_id: str,
    prompt_path: Path,
    k: int = 3,
    max_turns: int = 12,
    workers: int = 1,
    dry_run: bool = False,
    calls_path: Path = CALLS_PATH,
    ledgers_path: Path = LEDGERS_PATH,
    audits_path: Path = AUDITS_PATH,
    causes_path: Path = CAUSES_PATH,
    out_path: Path = ROOTCAUSE_PATH,
    replay_fn: ChannelReplayFn | None = None,
    audit_fn: AuditFn | None = None,
) -> dict:
    """Label the root calls of one cause. Every check that can stop the run comes before the first replay.

    Returns {"plan": Plan, "rows": [...], "skipped": {call_id: reason}, "cost_usd": float}. A dry run makes no
    LLM call, writes no file and returns no rows. A call whose replay or audit raises gets an `unresolved` row with
    an `error`; a budget stop ends the run, and the rows of the calls finished by then are still written.
    """
    if not cause_id.startswith("cause_"):
        raise SystemExit(f"cause id '{cause_id}' does not look like cause_<parameter>")
    if k < 1:
        raise SystemExit(f"--k must be at least 1, got {k}")
    if workers < 1:
        raise SystemExit(f"--workers must be at least 1, got {workers}")
    cause = load_cause(cause_id, causes_path)
    parameter = cause.get("parameter") or cause_id.removeprefix(CHANNEL_CAUSE_PREFIX).removeprefix("cause_")
    if parameter in AUDIO_PARAMETERS:
        raise SystemExit(f"{cause_id} needs audio timing and a text replay has none, so it cannot be judged")
    calls = {
        r["call_id"]: CallRecord.model_validate(r) for r in dedupe_by_call_id(read_jsonl(calls_path), last_wins=False)
    }
    ledgers = {r["call_id"]: Ledger.model_validate(r) for r in dedupe_by_call_id(read_jsonl(ledgers_path))}
    audits = {a.call_id: a for a in load_audits(Path(audits_path))}
    items: list[Item] = []
    skipped: dict[str, str] = {}
    for cid in root_calls(cause):
        if cid not in calls or cid not in ledgers or cid not in audits:
            skipped[cid] = "missing call, ledger or audit"
        elif ledgers[cid].variant != "main":
            skipped[cid] = f"variant {ledgers[cid].variant}, the prompt is the main variant"
        else:
            fork = target_fork(calls[cid], audits[cid], parameter, ledgers[cid])
            if fork is None:
                skipped[cid] = "no fork turn"
            else:
                items.append((calls[cid], ledgers[cid], fork))
    if not items:
        raise SystemExit(f"{cause_id} has no call to replay ({len(skipped)} skipped: {skipped})")
    old = prompt_mod.load(Path(prompt_path))
    plan = Plan(cause_id, parameter, old, old, items, [], k, max_turns)
    try:  # render every prompt for every call now: a template error must not surface mid-run
        _sent_tags(plan)
    except TemplateError as e:
        raise SystemExit(f"the prompt for cause {cause_id} does not render: {type(e).__name__}: {e}") from e
    if dry_run:
        return {"plan": plan, "rows": [], "skipped": skipped, "cost_usd": 0.0}
    replay_fn = replay_fn or real_channel_replay_fn(max_turns)
    audit_fn = audit_fn or real_audit_fn
    costs_before = len(read_jsonl(llm.COSTS_PATH))

    def work(item: Item) -> dict:
        call, ledger, fork = item
        rate = replay.buyer_brief(call, ledger, fork).unclear_rate
        try:
            return _run_call(item, parameter, old, k, rate, replay_fn, audit_fn)
        except llm.BudgetExceeded:
            raise  # a budget stop must end the run
        except Exception as e:  # one bad call must not stop the others
            reason = f"{type(e).__name__}: {str(e)[:200]}"
            return {"call_id": call.call_id, "noisy_rate": rate, "k": k, "label": "unresolved", "error": reason}

    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="rootcause")
    try:
        futures = [pool.submit(work, item) for item in items]
        for future in as_completed(futures):
            future.result()  # re-raises a budget stop
    finally:  # a stopped run must not start the calls still queued, and keeps the rows it finished
        pool.shutdown(wait=True, cancel_futures=True)
        # read after shutdown: a worker that finishes while the pool stops still has its row kept
        rows = [f.result() for f in futures if f.done() and not f.cancelled() and f.exception() is None]
        rows.sort(key=lambda r: r["call_id"])
        saved = [{"cause_id": cause_id, "parameter": parameter, "simulation": SIMULATION, **r} for r in rows]
        _write_rows(out_path, cause_id, saved)
    return {"plan": plan, "rows": rows, "skipped": skipped, "cost_usd": _run_cost(costs_before, plan)}


def describe_rootcause(cause_id: str, out: dict) -> list[str]:
    """Text lines for the CLI: the plan on a dry run, the labels after a real run."""
    plan: Plan = out["plan"]
    lines = [f"cause {cause_id} (parameter {plan.parameter}), k={plan.k}, max_turns={plan.max_turns}"]
    lines.append(
        f"simulation: {SIMULATION}: the noisy arm masks buyer text as [unclear audio] and does not recreate wrong STT "
        "values, so `channel` is scoped to that simulation"
    )
    lines += [f"skipped {cid}: {why}" for cid, why in out["skipped"].items()]
    if not out["rows"]:
        lines += [f"  call {c.call_id} fork_turn={fork}" for c, _, fork in plan.targets]
        lines.append(
            f"{len(plan.targets)} call(s), at most {2 * len(plan.targets) * plan.k} replays "
            "(dry run: no LLM call made, no file written)"
        )
        return lines
    for r in out["rows"]:
        counts = (
            f"noisy fails {r['noisy_fails']}/{r['k']}, clean fails {r['clean_fails']}/{r['k']}, "
            f"noisy rate {r['noisy_rate']:.2f}"
            if "noisy_fails" in r
            else f"error: {r['error']}"
        )
        lines.append(f"  {r['label']:14} {r['call_id']}: {counts}")
    tally = {lab: sum(r["label"] == lab for r in out["rows"]) for lab in LABELS}
    lines.append("labels: " + ", ".join(f"{lab}={n}" for lab, n in tally.items()))
    flat = [r["call_id"] for r in out["rows"] if r.get("noisy_rate") == 0]
    if flat:
        lines.append(f"{len(flat)} call(s) have noisy rate 0, so both arms were the same replay: {', '.join(flat)}")
    lines.append(f"LLM cost this run ${out['cost_usd']:.4f} (replay turns only)")
    return lines
