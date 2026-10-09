"""Golden set: a frozen, versioned sample of hand-graded calls, and the per-rule grader gate.

The set answers one question: does the audit grade like the owner? `build_golden` freezes a stratified
sample of `runs/hand_grades.jsonl` into `runs/golden/v<N>.json`. `golden_agreement` then compares the audit
with the human labels rule by rule. A rule below the bar is advisory and may not block or pass a fix.

A hand grade holds one verdict and the list of failing parameters. A parameter in the list is a human
"fail" label for that rule. A parameter not in the list is a human "no fault" label. That is a binary
label per rule, which is what kappa needs.

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
from datetime import UTC, datetime
from pathlib import Path

from .grading import HAND_VERDICTS, _decided, failing_parameters, load_grades
from .issue import AUDITS_PATH, CAUSES_PATH, load_audits
from .jsonl import read_jsonl
from .models import AuditRecord

RUNS = AUDITS_PATH.parent
GOLDEN_DIR = RUNS / "golden"
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
