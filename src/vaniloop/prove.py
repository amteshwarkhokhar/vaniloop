"""TEST stage runner: replay a patch on real calls and let the ship gate decide.

`run_prove` joins what Fix wrote (`runs/patches.json`) with what Audit saw (calls, ledgers, audits). It picks
target calls (failed the cause's parameter) and guard calls (passed), replays old and new prompt on both,
and records the gate result and every replay. Every check that can stop the run happens before the first
replay, so a bad input costs nothing.
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass
from pathlib import Path

from . import gate, llm, prompt, replay
from .fix import PATCHES_PATH, PromptPatch, _count_overlapping, apply_patch
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
AUDIO_PARAMETERS = {"dead_air"}  # judged from audio silences; a text replay has none


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


def target_fork(call: CallRecord, audit: AuditRecord, parameter: str) -> int | None:
    """Fork at the failing finding's onset turn, else at the audit's first fault turn, else None."""
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


def select_targets(
    calls: dict[str, CallRecord],
    ledgers: dict[str, Ledger],
    audits: dict[str, AuditRecord],
    parameter: str,
    n: int,
) -> tuple[list[Item], set[str]]:
    """Main-variant calls whose saved audit fails the parameter and that have a fork turn.

    Returns the n picked items and the ids of every candidate (the guards must stay clear of all of them).
    """
    forks: dict[str, int] = {}
    for cid, audit in audits.items():
        if cid in calls and cid in ledgers and ledgers[cid].variant == "main" and gate.failed(audit, parameter):
            fork = target_fork(calls[cid], audit, parameter)
            if fork is not None:
                forks[cid] = fork
    return [(calls[c], ledgers[c], forks[c]) for c in pick(list(forks), n)], set(forks)


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


def build_prompts(cause_id: str, prompt_path: Path, patches_path: Path) -> tuple[str, str]:
    """(old, new) prompt text. Stop with SystemExit when the cause has no patch or its anchor does not fit."""
    path = Path(patches_path)
    doc = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    rows = [x for x in doc.get("patches", []) if x.get("cause_id") == cause_id]
    patch = PromptPatch.model_validate(rows[-1]) if rows else None  # the latest patch for the cause
    if patch is None:
        raise SystemExit(f"no patch for {cause_id} in {patches_path}; run `pipeline fix` first")
    old = prompt.load(prompt_path)
    n = _count_overlapping(old, patch.old_text)
    if n != 1:
        raise SystemExit(
            f"the patch for {cause_id} does not fit the old prompt: old_text occurs {n} times "
            f"(it must occur exactly once in lines 1-{prompt.MAIN_VARIANT_LINES} of the clean prompt)"
        )
    return old, apply_patch(old, patch)


def _cost_since(rows_before: int) -> float:
    """Sum cost_usd over the cost rows written after the first `rows_before` rows."""
    costs = [row.get("cost_usd") for row in read_jsonl(llm.COSTS_PATH)[rows_before:]]
    return sum(c for c in costs if isinstance(c, (int, float)) and not isinstance(c, bool) and 0 <= c < math.inf)


def real_replay_fn(max_turns: int) -> ReplayFn:
    def fn(call: CallRecord, ledger: Ledger, fork: int, bot_prompt: str, seed: int):
        return replay.replay_result(call, fork, bot_prompt, replay.buyer_brief(call, ledger), max_turns, seed)

    return fn


def real_audit_fn(call: CallRecord) -> AuditRecord:
    """The same two steps as pipeline.run_audit: LLM ledger, then the Python rules."""
    return audit_call(call, extract_ledger(call))


def run_prove(
    cause_id: str,
    prompt_path: Path,
    n: int = 5,
    k: int = 3,
    guards: int = 10,
    max_turns: int = 12,
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
) -> Outcome:
    """Check, pick calls, then (unless dry_run) replay, decide and record. Every stop comes before the first replay.

    A run writes one gates row and one replays row per replay. A dry run makes no LLM call and writes no file.
    """
    if not cause_id.startswith("cause_"):
        raise SystemExit(f"cause id '{cause_id}' does not look like cause_<parameter>")
    parameter = cause_id.removeprefix("cause_")
    if parameter in AUDIO_PARAMETERS:
        raise SystemExit(f"{cause_id} needs audio timing and a text replay has none, so the gate cannot judge it")
    if k < 2:
        raise SystemExit(f"--k must be at least 2, got {k}")
    old, new = build_prompts(cause_id, prompt_path, patches_path)
    calls = {
        r["call_id"]: CallRecord.model_validate(r) for r in dedupe_by_call_id(read_jsonl(calls_path), last_wins=False)
    }
    ledgers = {r["call_id"]: Ledger.model_validate(r) for r in dedupe_by_call_id(read_jsonl(ledgers_path))}
    audits = {a.call_id: a for a in load_audits(Path(audits_path))}
    targets, candidates = select_targets(calls, ledgers, audits, parameter, n)
    if len(targets) < gate.MIN_CALLS:
        raise SystemExit(f"only {len(targets)} target call(s) for '{parameter}'; the gate needs {gate.MIN_CALLS}")
    plan = Plan(
        cause_id, parameter, old, new, targets, select_guards(calls, ledgers, audits, candidates, guards), k, max_turns
    )
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
        audit = audit_fn(call)
        row = {
            **pending.pop(call.call_id),
            "call": call.model_dump(mode="json"),
            "audit": audit.model_dump(mode="json"),
        }
        append_jsonl(replays_path, [row])  # row by row: a stopped paid run keeps the replays it finished
        written += 1
        return audit

    costs_before = len(read_jsonl(llm.COSTS_PATH))
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
    )
    gates_path = Path(gates_path)
    gates_path.parent.mkdir(parents=True, exist_ok=True)
    append_jsonl(gates_path, [result.model_dump(mode="json")])
    return Outcome(plan, result, written, _cost_since(costs_before))


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
    ]
    return lines
