"""TEST stage runner: replay a Fix patch on real target and guard calls and let the ship gate decide.

`run_prove` records the gate result and every replay. Every check that can stop the run comes before the first
replay, so a bad input costs nothing.
"""

from __future__ import annotations

import json
import math
import os
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from jinja2 import TemplateError

from . import gate, llm, prompt, render, replay
from .fix import (
    AUDIO_PARAMETERS,
    MAX_ATTEMPTS,
    PATCHES_PATH,
    PromptPatch,
    _count_overlapping,
    apply_patch,
    require_patchable,
)
from .gate import AuditFn, Item, ReplayFn
from .ingest import OUT_PATH as CALLS_PATH
from .issue import AUDITS_PATH, load_audits
from .jsonl import append_jsonl, dedupe_by_call_id, read_jsonl
from .ledger import extract_ledger
from .models import AuditRecord, CallRecord, Ledger
from .rules import audit_call

RUNS = AUDITS_PATH.parent
LEDGERS_PATH = RUNS / "ledgers.jsonl"
GATES_PATH = RUNS / "gates.jsonl"
REPLAYS_PATH = RUNS / "replays.jsonl"

PICK_SEED = 0
CALL_VALUES_HEADING = "Call values"


@dataclass
class Plan:
    """What a run would replay, fixed before any LLM call."""

    cause_id: str
    parameter: str
    old_prompt: str
    new_prompt: str
    targets: list[Item]
    guards: list[Item]
    k: int
    max_turns: int

    @property
    def max_replays(self) -> int:
        """Old and new for every target and seed, plus per guard one NEW replay and two more on a NEW Fatal."""
        return 2 * len(self.targets) * self.k + 3 * len(self.guards)

    @property
    def warm_replays(self) -> int:
        """What --workers computes ahead: old and new for every target and seed, one NEW seed-0 replay per guard."""
        return 2 * len(self.targets) * self.k + len(self.guards)


@dataclass
class Outcome:
    plan: Plan
    result: gate.GateResult | None = None  # None on a dry run
    replays: int = 0
    cost_usd: float = 0.0


def pick(ids: list[str], n: int) -> list[str]:
    """Pick up to n ids with a fixed seed from the sorted ids, so the same input always gives the same picks."""
    ids = sorted(ids)
    return sorted(random.Random(PICK_SEED).sample(ids, min(max(n, 0), len(ids))))


def _position(call: CallRecord, turn_idx: int | None) -> int | None:
    """List position of the turn with this Turn.idx, or None."""
    if turn_idx is None:
        return None
    return next((i for i, t in enumerate(call.turns) if t.idx == turn_idx), None)


def _after_product_change(call: CallRecord, ledger: Ledger) -> int | None:
    """Position of the first bot turn after the ledger's product change, or None without a change turn or bot turn."""
    if ledger.product_changed_at_turn is None:
        return None
    return next(
        (i for i, t in enumerate(call.turns) if t.speaker == "bot" and t.idx > ledger.product_changed_at_turn), None
    )


def target_fork(call: CallRecord, audit: AuditRecord, parameter: str, ledger: Ledger | None = None) -> int | None:
    """Fork at the failing finding's onset turn, else at the audit's first fault turn, else None.

    For `product_change` the fork is the first bot turn after the product change, in every scope: the fault is how the
    bot handles the change, and the audit's onset can be a later turn (the seller handoff). When the change turn is
    set but no bot turn follows it (the buyer changed on the last turn), the result is None: an earlier fork would
    drop the change from the replay history. Without a change turn the onset rule applies.
    """
    if parameter == "product_change" and ledger is not None and ledger.product_changed_at_turn is not None:
        return _after_product_change(call, ledger)
    finding = next((f for f in audit.findings if f.parameter == parameter and f.status in ("Fatal", "NonFatal")), None)
    for turn_idx in (finding.onset_turn if finding else None, audit.first_fault_turn):
        pos = _position(call, turn_idx)
        if pos is not None:
            return pos
    return None


def guard_fork(call: CallRecord) -> int | None:
    """Position of the first bot turn after the first user turn, so the replay covers requirement collection."""
    first_user = next((i for i, t in enumerate(call.turns) if t.speaker == "user"), None)
    if first_user is None:
        return None
    return next((i for i in range(first_user + 1, len(call.turns)) if call.turns[i].speaker == "bot"), None)


def channel_fault(call: CallRecord, fork: int) -> bool:
    """True when the fork turn or the user turn right before it is flagged `bot_misheard` or `audio_unclear`.

    The fault then was a mishearing, which a text replay cannot reproduce: the replayed bot hears the clean text.
    """
    before = next((t for t in reversed(call.turns[:fork]) if t.speaker == "user"), None)
    return any(t is not None and (t.bot_misheard or t.audio_unclear) for t in (call.turns[fork], before))


def inert_patch(call: CallRecord, ledger: Ledger, old: str, new: str) -> bool:
    """True when the old and the new prompt render to the same text for this call, so the patch changes nothing."""
    variables = render.call_variables(call, ledger)
    return render.render(old, variables) == render.render(new, variables)


def select_targets(
    calls: dict[str, CallRecord],
    ledgers: dict[str, Ledger],
    audits: dict[str, AuditRecord],
    parameter: str,
    n: int,
    prompts: tuple[str, str] | None = None,
    dropped: dict[str, list[str]] | None = None,
) -> tuple[list[Item], set[str]]:
    """Main-variant calls whose saved audit fails the parameter and that have a fork turn.

    Returns the n picked items and the ids of every candidate (the guards must stay clear of all of them). A picked
    call is dropped, and the next candidate in the same seeded order takes its place, when it is a channel fault (see
    channel_fault) or, with `prompts` = (old, new), when the patch is inert for it (see inert_patch). The ids dropped
    are printed per reason and added to `dropped[reason]` when given.
    """
    forks: dict[str, int] = {}
    skipped: list[str] = []
    for cid, audit in audits.items():
        if cid in calls and cid in ledgers and ledgers[cid].variant == "main" and gate.failed(audit, parameter):
            fork = target_fork(calls[cid], audit, parameter, ledgers[cid])
            if fork is not None:
                forks[cid] = fork
            elif parameter == "product_change" and ledgers[cid].product_changed_at_turn is not None:
                skipped.append(cid)
    if skipped:
        print(f"skipped target(s), no bot turn after the product change: {', '.join(sorted(skipped))}")
    first = pick(list(forks), n)  # the plain pick first, so a run with no drop keeps its picks
    rest = sorted(set(forks) - set(first))
    random.Random(PICK_SEED).shuffle(rest)  # the same seeded order for the replacements
    reasons: dict[str, list[str]] = {"inert patch": [], "channel fault": []}
    kept: list[str] = []
    for cid in [*first, *rest]:
        if len(kept) >= n:
            break
        if prompts is not None and inert_patch(calls[cid], ledgers[cid], *prompts):
            reasons["inert patch"].append(cid)
        elif channel_fault(calls[cid], forks[cid]):
            reasons["channel fault"].append(cid)
        else:
            kept.append(cid)
    for reason, ids in reasons.items():
        if ids:
            print(f"dropped target(s), {reason}: {', '.join(sorted(ids))}")
            if dropped is not None:
                dropped.setdefault(reason, []).extend(ids)
    return [(calls[c], ledgers[c], forks[c]) for c in sorted(kept)], set(forks)


def select_guards(
    calls: dict[str, CallRecord],
    ledgers: dict[str, Ledger],
    audits: dict[str, AuditRecord],
    exclude: set[str],
    n: int,
) -> list[Item]:
    """Main-variant calls with verdict Pass, not in `exclude`, that have a guard fork turn."""
    forks: dict[str, int] = {}
    for cid, audit in audits.items():
        if cid in calls and cid in ledgers and cid not in exclude and ledgers[cid].variant == "main":
            fork = guard_fork(calls[cid]) if audit.verdict == "Pass" else None
            if fork is not None:
                forks[cid] = fork
    return [(calls[c], ledgers[c], forks[c]) for c in pick(list(forks), n)]


def build_prompts(cause_id: str, prompt_path: Path, patches_path: Path, attempt: int = 1) -> tuple[str, str]:
    """(old, new) prompt text. Stop with SystemExit when the cause has no patch, the patch is not this attempt or
    its anchor does not fit."""
    path = Path(patches_path)
    doc = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    rows = [x for x in doc.get("patches", []) if x.get("cause_id") == cause_id]
    patch = PromptPatch.model_validate(rows[-1]) if rows else None  # the latest patch for the cause
    if patch is None:
        raise SystemExit(f"no patch for {cause_id} in {patches_path}; run `pipeline fix` first")
    if max(attempt, patch.attempt) > MAX_ATTEMPTS:
        raise SystemExit(
            f"at most {MAX_ATTEMPTS} attempts per cause: the current patch for {cause_id} is attempt "
            f"{patch.attempt} and --attempt is {attempt}"
        )
    if patch.attempt != attempt:
        raise SystemExit(
            f"the current patch for {cause_id} is attempt {patch.attempt}, not {attempt}; "
            f"pass --attempt {patch.attempt}"
        )
    old = prompt.load(prompt_path)
    n = _count_overlapping(old, patch.old_text)
    if n != 1:
        raise SystemExit(
            f"the patch for {cause_id} does not fit the old prompt: old_text occurs {n} times "
            f"(it must occur exactly once in lines 1-{prompt.MAIN_VARIANT_LINES} of the clean prompt)"
        )
    new = apply_patch(old, patch)
    if new == old:  # the old/new label in replays.jsonl comes from comparing the two texts
        raise SystemExit(f"the patch for {cause_id} leaves the prompt unchanged; a no-op patch cannot be tested")
    return old, new


def call_values(ledger: Ledger) -> dict:
    """The values the call carried, from the ledger: the product, the specs the bot offered (name -> options, in the
    order offered) and whether the bot asked for units."""
    specs: dict[str, list[str]] = {}
    for t in ledger.bot_turns:
        if t.slot == "spec" and t.spec_name:
            options = specs.setdefault(t.spec_name, [])
            options += [o for o in t.options_offered if o not in options]
    return {
        "product_name": ledger.initial_product or "",
        "specs": specs,
        "units_asked": any(t.slot == "unit" for t in ledger.bot_turns),
    }


def _sent_prompt(bot_prompt: str, call: CallRecord, ledger: Ledger) -> str:
    """The text the replay bot gets: the template rendered with the variables this call saw, then a "Call values"
    block. Rendering only picks branches, so the bot would not see the values; the block is the same for old and new."""
    block = json.dumps(call_values(ledger), ensure_ascii=False, indent=2)
    return render.render(bot_prompt, render.call_variables(call, ledger)) + f"\n\n## {CALL_VALUES_HEADING}\n{block}\n"


def _sent_tags(plan: Plan) -> dict[str, set[str]]:
    """Per call id, the tags of the rendered old and new prompts that call's replays send. Raises on a render error."""
    return {
        c.call_id: {replay.prompt_tag(_sent_prompt(p, c, ledger)) for p in (plan.old_prompt, plan.new_prompt)}
        for c, ledger, _ in [*plan.targets, *plan.guards]
    }


def _run_cost(rows_before: int, plan: Plan) -> float:
    """Sum cost_usd of the rows written during the run for this plan's replays, so a parallel run adds nothing.

    A replay turn's cache_tag is replay:<bot|user>:<prompt tag>:<seed>:<call id>:<fork>. The tag is that of the
    rendered prompt the call sent, which differs per call, so a row counts only with the tags of its own call.
    Ledger extraction rows carry only a version tag, cannot be attributed and are left out.
    """
    tags = _sent_tags(plan)
    total = 0.0
    for row in read_jsonl(llm.COSTS_PATH)[rows_before:]:
        parts = str(row.get("cache_tag", "")).split(":")
        cost = row.get("cost_usd")
        if len(parts) < 6 or parts[0] != "replay" or parts[2] not in tags.get(":".join(parts[4:-1]), ()):
            continue
        if isinstance(cost, (int, float)) and not isinstance(cost, bool) and 0 <= cost < math.inf:
            total += cost
    return total


def real_replay_fn(max_turns: int) -> ReplayFn:
    """Replay with the prompt rendered for the call. The caller's old/new label still compares the unrendered texts."""

    def fn(call: CallRecord, ledger: Ledger, fork: int, bot_prompt: str, seed: int):
        sent = _sent_prompt(bot_prompt, call, ledger)
        return replay.replay_result(call, fork, sent, replay.buyer_brief(call, ledger, fork), max_turns, seed)

    return fn


def real_audit_fn(call: CallRecord) -> AuditRecord:
    """The same two steps as pipeline.run_audit: LLM ledger, then the Python rules."""
    return audit_call(call, extract_ledger(call))


def warm_cache(plan: Plan, replay_fn: ReplayFn, audit_fn: AuditFn, workers: int) -> int:
    """Run every first-round replay and its audit in a thread pool, so the sequential gate reads them from the cache.

    The jobs follow the gate's own order (seed-major), so the replays it needs first finish first. Nothing is
    recorded here: only the gate's own calls write rows. A failed job is printed and skipped, and the gate runs it
    again. Returns the number of failed jobs.
    """
    jobs = [(item, bot_prompt, seed) for seed in range(plan.k) for item in plan.targets
            for bot_prompt in (plan.old_prompt, plan.new_prompt)]  # fmt: skip
    jobs += [(item, plan.new_prompt, 0) for item in plan.guards]

    def work(job):
        (call, ledger, fork), bot_prompt, seed = job
        audit_fn(replay_fn(call, ledger, fork, bot_prompt, seed).call)

    failed = 0
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="warm")
    try:
        futures = {pool.submit(work, job): job for job in jobs}
        for future in as_completed(futures):
            error = future.exception()
            if error is not None:
                (call, _, _), bot_prompt, seed = futures[future]
                failed += 1
                label = "old" if bot_prompt == plan.old_prompt else "new"
                print(f"warm-up failed (the gate runs it again): {call.call_id} {label} seed {seed}: "
                      f"{type(error).__name__}: {str(error)[:200]}")  # fmt: skip
    finally:  # a stopped run (Ctrl-C) must not start the replays still queued
        pool.shutdown(wait=True, cancel_futures=True)
    return failed


def run_prove(
    cause_id: str,
    prompt_path: Path,
    n: int = 5,
    k: int = 3,
    guards: int = 10,
    max_turns: int = 12,
    workers: int = 1,
    dry_run: bool = False,
    calls_path: Path = CALLS_PATH,
    ledgers_path: Path = LEDGERS_PATH,
    audits_path: Path = AUDITS_PATH,
    patches_path: Path = PATCHES_PATH,
    gates_path: Path = GATES_PATH,
    replays_path: Path = REPLAYS_PATH,
    replay_fn: ReplayFn | None = None,
    audit_fn: AuditFn | None = None,
    attempt: int = 1,
    causes_path: Path | None = None,
) -> Outcome:
    """Check, pick calls, then (unless dry_run) replay, decide and record. Every stop comes before the first replay.

    A run writes one gates row and one replays row per replay. A dry run makes no LLM call and writes no file.
    With workers > 1 the first-round replays are computed in a thread pool before the gate starts (see warm_cache);
    the gate and the rows stay the same, only the wall time shrinks. With VANILOOP_CACHE=refresh the gate cannot read
    the cache, so the warm-up is skipped. Attempts above MAX_ATTEMPTS stop here, and each attempt's gate runs at
    alpha / MAX_ATTEMPTS.
    A (cause, attempt) pair already in gates_path is not gated again (a dry run is still allowed).
    """
    if not cause_id.startswith("cause_"):
        raise SystemExit(f"cause id '{cause_id}' does not look like cause_<parameter>")
    parameter = cause_id.removeprefix("cause_")
    if parameter in AUDIO_PARAMETERS:
        raise SystemExit(f"{cause_id} needs audio timing and a text replay has none, so the gate cannot judge it")
    if k < 2:
        raise SystemExit(f"--k must be at least 2, got {k}")
    if workers < 1:
        raise SystemExit(f"--workers must be at least 1, got {workers}")
    if not dry_run:  # a demoted, tool or data cause keeps its old patch but takes no gate run
        require_patchable(cause_id, causes_path or Path(patches_path).with_name("causes.json"))  # sits by patches.json
    old, new = build_prompts(cause_id, prompt_path, patches_path, attempt)
    if not dry_run and any(
        r.get("cause_id") == cause_id and r.get("attempt", 1) == attempt for r in read_jsonl(gates_path)
    ):  # one look per (cause, attempt): a rerun is an extra look at the same calls and breaks the alpha split
        raise SystemExit(f"attempt {attempt} of {cause_id} was already gated; a new look needs a new attempt")
    calls = {
        r["call_id"]: CallRecord.model_validate(r) for r in dedupe_by_call_id(read_jsonl(calls_path), last_wins=False)
    }
    ledgers = {r["call_id"]: Ledger.model_validate(r) for r in dedupe_by_call_id(read_jsonl(ledgers_path))}
    audits = {a.call_id: a for a in load_audits(Path(audits_path))}
    dropped: dict[str, list[str]] = {}
    try:  # the inert check renders every candidate: a template error must stop here, before any LLM call
        targets, candidates = select_targets(calls, ledgers, audits, parameter, n, (old, new), dropped)
    except TemplateError as e:
        raise SystemExit(f"a prompt for cause {cause_id} does not render: {type(e).__name__}: {e}") from e
    if len(targets) < gate.MIN_CALLS:
        if dropped.get("inert patch"):
            raise SystemExit(
                f"patch does not reach this cause's calls: it renders the same old and new prompt for "
                f"{len(dropped['inert patch'])} of {len(candidates)} candidate call(s) and only {len(targets)} "
                f"target call(s) are left; the gate needs {gate.MIN_CALLS}"
            )
        raise SystemExit(f"only {len(targets)} target call(s) for '{parameter}'; the gate needs {gate.MIN_CALLS}")
    plan = Plan(
        cause_id, parameter, old, new, targets, select_guards(calls, ledgers, audits, candidates, guards), k, max_turns
    )
    try:
        _sent_tags(plan)  # render every prompt for every call now: a template error must not surface mid-run
    except TemplateError as e:
        raise SystemExit(f"a prompt for cause {cause_id} does not render: {type(e).__name__}: {e}") from e
    if dry_run:
        return Outcome(plan)
    replay_fn = replay_fn or real_replay_fn(max_turns)
    audit_fn = audit_fn or real_audit_fn
    target_ids = {c.call_id for c, _, _ in plan.targets}
    replays_path = Path(replays_path)
    replays_path.parent.mkdir(parents=True, exist_ok=True)
    pending: dict[str, dict] = {}  # replayed call_id -> how it was made; the audit that follows completes the row
    written = 0

    def record_replay(call: CallRecord, ledger: Ledger, fork: int, bot_prompt: str, seed: int):
        res = replay_fn(call, ledger, fork, bot_prompt, seed)
        pending[res.call.call_id] = {
            "cause_id": cause_id,
            "attempt": attempt,
            "role": "target" if call.call_id in target_ids else "guard",
            "prompt": "old" if bot_prompt == plan.old_prompt else "new",
            "seed": seed,
            "fork_turn": res.fork_turn,
            "ended_by": res.ended_by,
        }
        return res

    def record_audit(call: CallRecord) -> AuditRecord:
        nonlocal written
        row = {**pending.pop(call.call_id), "call": call.model_dump(mode="json")}
        try:
            audit = audit_fn(call)
            row["audit"] = audit.model_dump(mode="json")
        except Exception as e:  # the replay is paid for: keep it, and give the gate unresolved evidence
            reason = f"{type(e).__name__}: {str(e)[:200]}"
            row.update(audit=None, audit_error=reason)
            audit = AuditRecord(call_id=call.call_id, verdict="NeedsReview", scope="unclear", findings=[],
                                flags=["low_confidence"], review_reason=f"audit failed: {reason}")  # fmt: skip
        append_jsonl(replays_path, [row])  # row by row: a stopped paid run keeps the replays it finished
        written += 1
        return audit

    costs_before = len(read_jsonl(llm.COSTS_PATH))  # before the warm-up: its replays are part of this run's cost
    if workers > 1 and os.environ.get("VANILOOP_CACHE") == "refresh":
        print("warm-up skipped: VANILOOP_CACHE=refresh makes the gate ignore the cache, so warming would pay twice")
    elif workers > 1:
        print(
            f"warm-up: {plan.warm_replays} replays, each with its audit, on {workers} workers before the gate. "
            f"The gate stops early on a futility reject or on unresolved seed-0 evidence, and then never needs "
            f"some of them; warming has already paid for those."
        )
        failed = warm_cache(plan, replay_fn, audit_fn, workers)
        print(f"warm-up done: {plan.warm_replays - failed} of {plan.warm_replays} ok, {failed} failed")
    result = gate.run_gate(
        cause_id,
        plan.parameter,
        plan.targets,
        plan.guards,
        plan.old_prompt,
        plan.new_prompt,
        k_seeds=k,
        replay_fn=record_replay,
        audit_fn=record_audit,
        attempt=attempt,
        alpha=gate.DEFAULT_ALPHA / MAX_ATTEMPTS,
    )
    gates_path = Path(gates_path)
    gates_path.parent.mkdir(parents=True, exist_ok=True)
    append_jsonl(gates_path, [result.model_dump(mode="json")])
    return Outcome(plan, result, written, _run_cost(costs_before, plan))


def describe(outcome: Outcome) -> list[str]:
    """Text lines for the CLI: the plan on a dry run, the gate result after a real run."""
    plan = outcome.plan
    lines = [f"cause {plan.cause_id} (parameter {plan.parameter}), k={plan.k}, max_turns={plan.max_turns}"]
    lines.append(f"old prompt: {len(plan.old_prompt)} characters; new prompt: {len(plan.new_prompt)} characters")
    for label, items in (("target", plan.targets), ("guard", plan.guards)):
        lines.append(f"{len(items)} {label} call(s):")
        lines += [f"  {label} {c.call_id} fork_turn={fork}" for c, _, fork in items]
    if outcome.result is None:
        lines.append(f"at most {plan.max_replays} replays (dry run: no LLM call made, no file written)")
        return lines
    r = outcome.result
    lines += [
        f"decision: {r.decision} ({r.reason})",
        f"p-value {r.p_value:.6g} (alpha {r.alpha}), lift {r.lift:.3f}",
        f"fail rate old {r.old_fail_rate:.3f}, new {r.new_fail_rate:.3f}",
        f"guard breaks {len(r.guard_breaks)}, guard flaky {len(r.guard_flaky)}",
        f"{outcome.replays} replays recorded, LLM cost this run ${outcome.cost_usd:.4f}",
        "(cost covers attributed rows only: replay turns. Ledger extraction calls cannot be attributed.)",
    ]
    return lines
