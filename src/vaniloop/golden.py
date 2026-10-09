"""Golden set: a frozen, versioned sample of hand-graded calls, and the per-rule grader gate.

The set answers one question: does the audit grade like the owner? `build_golden` freezes a stratified
sample of `runs/hand_grades.jsonl` into `runs/golden/v<N>.json`. `golden_agreement` then compares the audit
with the human labels rule by rule. A rule below the bar is advisory and may not block or pass a fix.

A hand grade holds one verdict and the list of failing parameters. A parameter in the list is a human
"fail" label for that rule. A parameter not in the list is a human "no fault" label. That is a binary
label per rule, which is what kappa needs.

`run_golden_check` is the last look before a patch set ships: all gate-shipped patches together, one replay of
each golden call under OLD and under NEW, one run per candidate (the hash of the patch set).

Fix must never read this directory: it would let a patch learn the answers (see tests/test_fix.py).
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import tempfile
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from jinja2 import TemplateError

from . import gate, llm, prove
from .fix import GATES_PATH, PATCHES_PATH, PromptPatch, _count_overlapping
from .grading import FAILING, HAND_VERDICTS, _decided, failing_parameters, load_grades
from .ingest import OUT_PATH as CALLS_PATH
from .issue import AUDITS_PATH, CAUSES_PATH, load_audits
from .jsonl import append_jsonl, dedupe_by_call_id, read_jsonl
from .models import AuditRecord, CallRecord, Ledger
from .prompt import load as load_prompt

RUNS = AUDITS_PATH.parent
GOLDEN_DIR = RUNS / "golden"
GOLDEN_RUNS_PATH = RUNS / "golden_runs.jsonl"
LEDGERS_PATH = RUNS / "ledgers.jsonl"
CHANNEL_FAULT = "channel fault"  # a skip that is reported as unverified coverage; every other skip blocks the check
BAR_KAPPA = 0.6  # a rule needs kappa at or above this to gate a fix
BAR_N = 10  # and at least this many calls the audit decided
MAX_CALLS = 150
PASS_SHARE = 0.6  # share of the set whose human verdict is Pass
BAR_COVERAGE = 0.8  # share of golden calls (and of human-fault calls) the audit must decide for a rule to gate


def sha_of(calls: list[dict]) -> str:
    """sha256 of the canonical JSON of the call rows. Any change to a label or a call id changes it."""
    return hashlib.sha256(json.dumps(calls, sort_keys=True).encode("utf-8")).hexdigest()


def _round_robin(rows: list[dict], quota: int, rng: random.Random) -> list[dict]:
    """Take quota rows, one per stratum per round, so each stratum gets a near-equal share."""
    strata: dict[tuple, list[dict]] = defaultdict(list)
    for r in sorted(rows, key=lambda r: r["call_id"]):
        strata[(r["scope"], r["variant"], r["cause"])].append(r)
    keys = sorted(strata)
    rng.shuffle(keys)
    for k in keys:
        rng.shuffle(strata[k])
    out: list[dict] = []
    i = 0
    while len(out) < quota and any(strata[k] for k in keys):
        for k in keys:
            if i < len(strata[k]) and len(out) < quota:
                out.append(strata[k][i])
        i += 1
    return out


def select_golden(rows: list[dict], n: int = MAX_CALLS, pass_share: float = PASS_SHARE, seed: int = 0) -> list[dict]:
    """Pick up to n rows: about pass_share human-Pass, the rest Fatal or NonFatal, each spread over strata.

    A stratum is (scope, variant, cause). When the Fail pool is too small, Pass fills the quota. When the Pass pool
    is too small, failures are capped to keep a Pass majority and the sample comes back smaller than n.
    Deterministic for one seed.
    """
    rng = random.Random(seed)
    passes = [r for r in rows if r["verdict"] == "Pass"]
    fails = [r for r in rows if r["verdict"] != "Pass"]
    target = min(n, len(rows))
    fail_q = min(target - min(round(target * pass_share), len(passes)), len(fails))
    pass_q = min(target - fail_q, len(passes))
    if pass_share > 0:  # a short Pass pool must not turn the set into a mostly failing one: cap the failures
        fail_q = min(fail_q, int(pass_q * (1 - pass_share) / pass_share + 1e-9), max(pass_q - 1, 0))
    picked = _round_robin(passes, pass_q, rng) + _round_robin(fails, fail_q, rng)
    return sorted(picked, key=lambda r: r["call_id"])


def _cause_by_call(causes_path: Path) -> dict[str, str]:
    """Map a call id to the first cause (file order = rank order) that lists it. Calls in no cause are absent."""
    p = Path(causes_path)
    if not p.exists():
        return {}
    out: dict[str, str] = {}
    for c in json.loads(p.read_text(encoding="utf-8")).get("causes", []):
        for cid in [*c.get("fatal_calls", []), *c.get("nonfatal_calls", [])]:
            out.setdefault(cid, c["cause_id"])
    return out


def _next_version(out_dir: Path) -> int:
    found = [int(m.group(1)) for p in out_dir.glob("v*.json") if (m := re.fullmatch(r"v(\d+)\.json", p.name))]
    return max(found, default=0) + 1


def build_golden(
    grades_path: Path = RUNS / "hand_grades.jsonl",
    audits_path: Path = AUDITS_PATH,
    ledgers_path: Path = RUNS / "ledgers.jsonl",
    causes_path: Path = CAUSES_PATH,
    out_dir: Path = GOLDEN_DIR,
    n: int = MAX_CALLS,
    pass_share: float = PASS_SHARE,
    seed: int = 0,
) -> Path:
    """Freeze a stratified sample of hand-graded calls as the next version. Returns the file path.

    Needs an audit per call (for the scope). `short_of` is set when the Pass majority forced a smaller sample.
    Never overwrites: the file is linked into place and an existing version makes the link fail, so the build
    retries on the next number.
    """
    grades = load_grades(grades_path)
    audits = {a.call_id: a for a in load_audits(Path(audits_path))}
    variants = {r["call_id"]: r.get("variant", "unknown") for r in read_jsonl(ledgers_path) if "call_id" in r}
    causes = _cause_by_call(causes_path)
    rows = [
        {
            "call_id": cid,
            "scope": audits[cid].scope,
            "variant": variants.get(cid, "unknown"),
            "cause": causes.get(cid, "none"),
            "verdict": g["verdict"],
            "parameters": sorted(g.get("parameters") or []),
            "first_fault_turn": g.get("first_fault_turn"),
        }
        for cid, g in sorted(grades.items())
        if cid in audits and g.get("verdict") in HAND_VERDICTS
    ]
    if not rows:
        raise SystemExit(f"no usable hand grades in {grades_path}: grade calls in the dashboard first")
    calls = select_golden(rows, n=n, pass_share=pass_share, seed=seed)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    while True:
        version = _next_version(out_dir)
        doc = {
            "version": version,
            "created": datetime.now(UTC).isoformat(timespec="seconds"),
            "n": len(calls),
            "short_of": min(n, len(rows)) if len(calls) < min(n, len(rows)) else None,
            "seed": seed,
            "pass_share": pass_share,
            "sha256": sha_of(calls),
            "calls": calls,
        }
        final = out_dir / f"v{version}.json"
        fd, tmp_name = tempfile.mkstemp(prefix=f".v{version}.", suffix=".tmp", dir=out_dir)  # unique per call
        tmp = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(doc, ensure_ascii=False, indent=1))
        try:
            os.link(tmp, final)  # fails when the version exists: a frozen file is never replaced
        except FileExistsError:
            continue
        finally:
            tmp.unlink(missing_ok=True)
        return final


def load_golden(path: Path) -> dict:
    """Read one golden version and check its sha256. Raises ValueError when the calls no longer match."""
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if sha_of(doc["calls"]) != doc.get("sha256"):
        raise ValueError(f"{path}: sha256 does not match the calls; the golden set was changed after the build")
    return doc


def latest_golden(out_dir: Path = GOLDEN_DIR) -> Path | None:
    """Path of the highest version in out_dir, or None when there is none."""
    v = _next_version(Path(out_dir)) - 1
    return Path(out_dir) / f"v{v}.json" if v else None


def cohen_kappa(hand: list[bool], audit: list[bool]) -> float | None:
    """Cohen's kappa for two binary raters. None when it is undefined: no pairs or chance agreement of 1."""
    n = len(hand)
    if n == 0:
        return None
    po = sum(h == a for h, a in zip(hand, audit, strict=True)) / n
    ph, pa = sum(hand) / n, sum(audit) / n
    pe = ph * pa + (1 - ph) * (1 - pa)
    if pe >= 1:
        return None
    return (po - pe) / (1 - pe)


def golden_agreement(doc: dict, audits: list[AuditRecord]) -> dict[str, dict]:
    """Per rule: audit vs human fail labels on the golden calls where the audit decided that rule.

    Returns {parameter: {kappa, agree, n, rate, coverage, fault_coverage, skipped, status}}.
    coverage is decided calls over all golden calls. fault_coverage is the same over the calls the human marked
    as failing this rule, so an audit that abstains only where humans saw faults cannot look perfect.
    skipped counts calls left out: no_audit (no audit row), no_finding (the rule did not run) and unknown.
    Status is "gating" when kappa >= BAR_KAPPA, n >= BAR_N and both coverages >= BAR_COVERAGE, else "advisory"
    (an undefined kappa is advisory).
    """
    audit_by_id = {a.call_id: a for a in audits}
    calls = doc["calls"]
    pairs = [(c, audit_by_id[c["call_id"]]) for c in calls if c["call_id"] in audit_by_id]
    no_audit = len(calls) - len(pairs)
    params = sorted({p for c, a in pairs for p in set(c["parameters"]) | {f.parameter for f in a.findings}})
    out: dict[str, dict] = {}
    for param in params:
        judged = [(c, a) for c, a in pairs if _decided(a, param)]
        hand = [param in c["parameters"] for c, _ in judged]
        audit = [param in failing_parameters(a) for _, a in judged]
        agree = sum(h == a for h, a in zip(hand, audit, strict=True))
        kappa = cohen_kappa(hand, audit)
        unknown = sum(1 for c, a in pairs if not _decided(a, param) and any(f.parameter == param for f in a.findings))
        no_finding = len(pairs) - len(judged) - unknown
        faults = [c for c in calls if param in c["parameters"]]
        decided_ids = {c["call_id"] for c, _ in judged}
        coverage = len(judged) / len(calls) if calls else 0.0
        fault_coverage = (sum(c["call_id"] in decided_ids for c in faults) / len(faults)) if faults else None
        gating = (
            kappa is not None
            and kappa >= BAR_KAPPA
            and len(judged) >= BAR_N
            and coverage >= BAR_COVERAGE
            and (fault_coverage is None or fault_coverage >= BAR_COVERAGE)
        )
        out[param] = {
            "kappa": kappa,
            "agree": agree,
            "n": len(judged),
            "rate": (agree / len(judged)) if judged else None,
            "coverage": coverage,
            "fault_coverage": fault_coverage,
            "skipped": {"no_audit": no_audit, "no_finding": no_finding, "unknown": unknown},
            "status": "gating" if gating else "advisory",
        }
    return out


# ---- golden check: all shipped patches together, one run per candidate ----


@dataclass
class CheckPlan:
    """What a check would replay, fixed before any LLM call. `items` are (call, ledger, fork, golden row)."""

    version: int
    golden_sha256: str
    candidate: str
    patches: list[PromptPatch]
    old_prompt: str
    new_prompt: str
    causes: dict[str, str]  # cause_id -> "gating" or "advisory", from the golden agreement of its parameter
    gating: set[str]  # every gating parameter: the only ones whose Fatal counts as a rise
    items: list[tuple[CallRecord, Ledger, int, dict]]
    skipped: dict[str, str] = field(default_factory=dict)  # call_id -> why it is not replayed
    already_checked: bool = False
    max_turns: int = 12

    @property
    def replays(self) -> int:
        """One replay under OLD and one under NEW for every item."""
        return 2 * len(self.items)


@dataclass
class CheckOutcome:
    plan: CheckPlan
    row: dict | None = None  # the golden_runs.jsonl row; None on a dry run
    cost_usd: float = 0.0


def shipped_patches(patches_path: Path = PATCHES_PATH, gates_path: Path = GATES_PATH) -> list[PromptPatch]:
    """Current patches whose latest gate row, for their cause and attempt, says decision "ship". Sorted by cause."""
    path = Path(patches_path)
    doc = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    gates = read_jsonl(gates_path)
    out = []
    for raw in doc.get("patches", []):
        patch = PromptPatch.model_validate(raw)
        rows = [r for r in gates if r.get("cause_id") == patch.cause_id and r.get("attempt", 1) == patch.attempt]
        if rows and rows[-1].get("decision") == "ship":
            out.append(patch)
    return sorted(out, key=lambda p: p.cause_id)


def candidate_hash(patches: list[PromptPatch]) -> str:
    """sha256 of the patch set: cause, attempt and both texts of every patch, in cause order."""
    canon = [[p.cause_id, p.attempt, p.old_text, p.new_text] for p in patches]
    return hashlib.sha256(json.dumps(canon, ensure_ascii=False).encode("utf-8")).hexdigest()


def apply_all(old: str, patches: list[PromptPatch]) -> str:
    """Apply every patch to the clean prompt. Stops when an anchor does not occur once or two anchors intersect.

    Each anchor's interval is found in the clean prompt first, so overlap is judged before any patch is applied.
    """
    spans = []
    for p in patches:
        n = _count_overlapping(old, p.old_text)
        if n != 1:
            raise SystemExit(f"the patch for {p.cause_id} does not fit the old prompt: old_text occurs {n} times")
        start = old.find(p.old_text)
        spans.append((start, start + len(p.old_text), p))
    spans.sort(key=lambda x: x[:2])
    for (_, end, a), (start, _, b) in zip(spans, spans[1:], strict=False):
        if start < end:
            raise SystemExit(f"the patches for {a.cause_id} and {b.cause_id} overlap; they cannot apply together")
    new = old
    for start, end, p in reversed(spans):
        new = new[:start] + p.new_text + new[end:]
    if new == old:
        raise SystemExit("the shipped patches leave the prompt unchanged; there is nothing to check")
    return new


def golden_fork(call: CallRecord, audit: AuditRecord | None, row: dict, parameters: list[str], ledger: Ledger):
    """List position to fork a golden call at, or None.

    A Pass call forks at the first bot turn after the first user turn (prove.guard_fork), so the replay covers
    requirement collection. A failing call forks at the human's first-fault turn; without one it forks as prove does
    for the first patched parameter it fails (the audit's onset, else the audit's first fault turn).
    """
    if row["verdict"] == "Pass":
        return prove.guard_fork(call)
    pos = prove._position(call, row.get("first_fault_turn"))
    if pos is None and audit is not None and parameters:
        pos = prove.target_fork(call, audit, parameters[0], ledger)
    return pos


def _arm(res, audit: AuditRecord, params: list[str], gating: set[str]) -> dict:
    """One replay's result: the patched parameters that fail from the fork on, and the gating parameters Fatal.

    Missing evidence is an error, never a fault-free result: a NeedsReview verdict, a missing, Unknown or
    NotApplicable finding for a patched parameter, a fault that starts before the fork or has no placeable onset,
    and, for every gating parameter, a missing or Unknown finding or a Fatal with no placeable onset. An advisory
    parameter never blocks, whatever its finding says.
    """

    def after(f):
        return gate._after_fork(res, f)  # None: the onset has no turn

    found = {f.parameter: f for f in audit.findings}
    bad = []
    for p in params:
        status = found[p].status if p in found else "missing"
        if status in ("missing", "Unknown", "NotApplicable"):
            bad.append(f"{p} finding {status}")
        elif status in FAILING and after(found[p]) is not True:
            bad.append(f"{p} fault not after the fork")
    for g in sorted(gating - set(found) - set(params)):
        bad.append(f"{g} finding missing")  # a Pass guard call patches nothing: its Fatal check needs this finding
    for f in audit.findings:
        if f.parameter in gating and (f.status == "Unknown" or (f.status == "Fatal" and after(f) is None)):
            bad.append(f"{f.parameter} {f.status} without evidence")
    if audit.verdict == "NeedsReview":
        bad.append("verdict NeedsReview")
    if bad:
        return {"error": "unresolved: " + ", ".join(bad)}
    faults = [p for p in params if found[p].status in FAILING]
    fatal = {f.parameter for f in audit.findings if f.status == "Fatal" and f.parameter in gating and after(f)}
    return {"faults": sorted(faults), "fatal": sorted(fatal), "ended_by": res.ended_by}


def _run_arm(item, bot_prompt: str, params: list[str], gating: set[str], replay_fn, audit_fn) -> dict:
    """Replay once (seed 0) and audit. A failed replay or audit, or invalid output, is an error: no evidence."""
    call, ledger, fork, _ = item
    try:
        res = replay_fn(call, ledger, fork, bot_prompt, 0)
        if res.ended_by == "invalid_output":
            return {"error": "invalid_output"}
        return _arm(res, audit_fn(res.call), params, gating)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}


def plan_check(
    version: str | int,
    prompt_path: Path,
    max_turns: int = 12,
    golden_dir: Path = GOLDEN_DIR,
    calls_path: Path = CALLS_PATH,
    ledgers_path: Path = LEDGERS_PATH,
    audits_path: Path = AUDITS_PATH,
    patches_path: Path = PATCHES_PATH,
    gates_path: Path = GATES_PATH,
    runs_path: Path = GOLDEN_RUNS_PATH,
) -> CheckPlan:
    """Everything a check needs, with no LLM call. Stops with SystemExit when there is no shipped patch."""
    num = int(str(version).lower().removeprefix("v"))
    gpath = Path(golden_dir) / f"v{num}.json"
    if not gpath.exists():
        raise SystemExit(f"no golden set {gpath.name} in {golden_dir}")
    doc = load_golden(gpath)
    patches = shipped_patches(patches_path, gates_path)
    if not patches:
        raise SystemExit("no shipped patch: a patch is shipped when its latest gate row says decision ship")
    old = load_prompt(prompt_path)
    new = apply_all(old, patches)
    audits = load_audits(Path(audits_path))
    agreement = golden_agreement(doc, audits)
    causes = {
        p.cause_id: agreement.get(p.cause_id.removeprefix("cause_"), {}).get("status", "advisory") for p in patches
    }
    patched = {c.removeprefix("cause_") for c, status in causes.items() if status == "gating"}
    calls = {r["call_id"]: r for r in dedupe_by_call_id(read_jsonl(calls_path), last_wins=False)}
    ledgers = {r["call_id"]: r for r in dedupe_by_call_id(read_jsonl(ledgers_path))}
    audit_by_id = {a.call_id: a for a in audits}
    items, skipped = [], {}
    for row in doc["calls"]:
        cid = row["call_id"]
        params = sorted(patched & set(row["parameters"]))
        if row["verdict"] != "Pass" and not params:
            continue  # a fault no gating patch targets: nothing to replay
        if cid not in calls or cid not in ledgers:
            skipped[cid] = "no call or no ledger"
            continue
        call, ledger = CallRecord.model_validate(calls[cid]), Ledger.model_validate(ledgers[cid])
        fork = golden_fork(call, audit_by_id.get(cid), row, params, ledger)
        if fork is None:
            skipped[cid] = "no fork turn"
        elif row["verdict"] != "Pass" and prove.channel_fault(call, fork):
            skipped[cid] = CHANNEL_FAULT  # a mishearing: a text replay cannot reproduce it
        else:
            items.append((call, ledger, fork, row))
    cand = candidate_hash(patches)
    done = any(r.get("candidate") == cand for r in read_jsonl(runs_path))
    gating = {p for p, r in agreement.items() if r["status"] == "gating"}
    plan = CheckPlan(num, doc["sha256"], cand, patches, old, new, causes, gating, items, skipped, done, max_turns)
    try:  # render every prompt for every call now: a template error must not surface mid-run
        prove._sent_tags(_cost_plan(plan))
    except TemplateError as e:
        raise SystemExit(f"a prompt does not render: {type(e).__name__}: {e}") from e
    return plan


def _cost_plan(plan: CheckPlan) -> prove.Plan:
    """A prove.Plan over every item, so prove's render check and cost attribution apply unchanged."""
    return prove.Plan("golden", "golden", plan.old_prompt, plan.new_prompt, [i[:3] for i in plan.items], [], 1,
                      plan.max_turns)  # fmt: skip


def summarise(plan: CheckPlan, results: list[dict]) -> dict:
    """The pass rule. Pass needs a gating cause, every gating cause dropped, no Fatal rise and no unresolved replay.

    A cause is "dropped" when fewer golden calls that have its fault fail under NEW than under OLD, "not_reproduced"
    when OLD never fails, "not_dropped" when NEW fails as often as OLD or more, "no_golden_fault" without such calls.
    A cause on an advisory rule is "advisory" and counts for nothing. The Fatal rise counts golden Pass calls with a
    Fatal on a gating parameter from the fork on, NEW against OLD. A replay with an error is left out and counted.
    """
    ok = [r for r in results if "error" not in r["old"] and "error" not in r["new"]]
    causes: dict[str, dict] = {}
    for cid, status in plan.causes.items():
        if status != "gating":
            causes[cid] = {"status": "advisory"}
            continue
        rows = [r for r in ok if r["role"] == "failing" and cid.removeprefix("cause_") in r["params"]]
        param = cid.removeprefix("cause_")
        old_f, new_f = (sum(param in r[arm]["faults"] for r in rows) for arm in ("old", "new"))
        state = "dropped" if new_f < old_f else "not_dropped"
        state = "not_reproduced" if not old_f else state
        state = "no_golden_fault" if not rows else state
        causes[cid] = {"status": state, "old_fail": old_f, "new_fail": new_f, "calls": len(rows)}
    fatal = {arm: sum(bool(r[arm]["fatal"]) for r in ok if r["role"] == "pass") for arm in ("old", "new")}
    unresolved = len(results) - len(ok)
    problems = [f"{c} {v['status']}" for c, v in causes.items() if v["status"] not in ("dropped", "advisory")]
    if not any(v["status"] == "dropped" for v in causes.values()):
        problems.append("no gating cause dropped")
    if fatal["new"] > fatal["old"]:
        problems.append(f"Fatal rose on golden Pass calls: {fatal['old']} to {fatal['new']}")
    if unresolved:
        problems.append(f"{unresolved} unresolved replay(s)")
    return {"passed": not problems, "reason": "; ".join(problems) or "passed", "causes": causes, "fatal": fatal,
            "unresolved": unresolved}  # fmt: skip


def run_golden_check(
    version: str | int,
    prompt_path: Path,
    dry_run: bool = False,
    max_turns: int = 12,
    workers: int = 1,
    runs_path: Path = GOLDEN_RUNS_PATH,
    replay_fn=None,
    audit_fn=None,
    **paths,
) -> CheckOutcome:
    """Plan, then (unless dry_run) replay every golden call once under OLD and once under NEW, and record the run.

    Every stop comes before the first replay: no golden set, no shipped patch, patches that do not apply together,
    no gating cause (nothing could pass) and a candidate already in runs_path. A dry run makes no LLM call and
    writes no file. A real run appends one row to runs_path at the end, so a run that crashes can be retried.
    """
    plan = plan_check(version, prompt_path, max_turns, runs_path=runs_path, **paths)
    if dry_run:
        return CheckOutcome(plan)
    if plan.already_checked:
        raise SystemExit(f"candidate {plan.candidate[:12]} was already checked: one run per patch set")
    blocked = {c: why for c, why in plan.skipped.items() if why != CHANNEL_FAULT}
    if blocked:
        raise SystemExit(f"{len(blocked)} golden call(s) cannot be replayed, so the check cannot judge them: {blocked}")
    if "gating" not in plan.causes.values():
        raise SystemExit("no patched cause has a gating rule on this golden set, so the check could not pass")
    replay_fn = replay_fn or prove.real_replay_fn(max_turns)
    audit_fn = audit_fn or prove.real_audit_fn
    costs_before = len(read_jsonl(llm.COSTS_PATH))

    def work(item):
        _, _, _, row = item
        params = [p for p in row["parameters"] if plan.causes.get(f"cause_{p}") == "gating"]
        arms = {
            arm: _run_arm(item, text, params, plan.gating, replay_fn, audit_fn)
            for arm, text in (("old", plan.old_prompt), ("new", plan.new_prompt))
        }
        return {"call_id": row["call_id"], "role": "pass" if row["verdict"] == "Pass" else "failing",
                "fork_turn": item[2], "params": params, **arms}  # fmt: skip

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = list(pool.map(work, plan.items))
    row = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "candidate": plan.candidate,
        "golden_version": plan.version,
        "golden_sha256": plan.golden_sha256,
        "patches": [{"cause_id": p.cause_id, "attempt": p.attempt} for p in plan.patches],
        **summarise(plan, results),
        "unverified": sorted(plan.skipped),  # channel-fault calls the check did not replay (others block the run)
        "replays": plan.replays,
        "calls": results,
    }
    Path(runs_path).parent.mkdir(parents=True, exist_ok=True)
    append_jsonl(runs_path, [row])
    return CheckOutcome(plan, row, prove._run_cost(costs_before, _cost_plan(plan)))


def describe_check(outcome: CheckOutcome) -> list[str]:
    """Text lines for the CLI: the plan on a dry run, the plan and the result after a real run."""
    plan = outcome.plan
    lines = [f"golden v{plan.version}: candidate {plan.candidate[:12]}, {len(plan.patches)} shipped patch(es) together"]
    lines += [f"  {p.cause_id} attempt {p.attempt}: {plan.causes[p.cause_id]} rule" for p in plan.patches]
    kinds = [r["verdict"] == "Pass" for *_, r in plan.items]
    lines.append(f"{len(plan.items)} golden call(s): {sum(kinds)} Pass, {len(kinds) - sum(kinds)} failing")
    lines += [f"  skipped {cid}: {why}" for cid, why in sorted(plan.skipped.items())]
    if any(why != CHANNEL_FAULT for why in plan.skipped.values()):
        lines.append("a skipped call other than a channel fault blocks a real run")
    if plan.already_checked:
        lines.append("this candidate was already checked: a real run is refused")
    if outcome.row is None:
        lines.append(f"{plan.replays} replays (dry run: no LLM call made, no file written)")
        return lines
    r = outcome.row
    lines.append(f"{'PASS' if r['passed'] else 'FAIL'}: {r['reason']} ({len(r['unverified'])} unverified)")
    lines += [f"  {c}: {v}" for c, v in r["causes"].items()]
    lines.append(f"  Fatal on golden Pass calls: old {r['fatal']['old']}, new {r['fatal']['new']}")
    lines.append(f"{plan.replays} replays, LLM cost this run ${outcome.cost_usd:.4f} (replay turns only)")
    return lines
