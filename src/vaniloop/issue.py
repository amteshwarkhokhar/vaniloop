"""ISSUE: audits -> ranked causes (v0: deterministic, one cause per failing rubric parameter)."""

from __future__ import annotations

import json
import math
from pathlib import Path

from .models import AuditRecord, Cause

ROOT = Path(__file__).resolve().parents[2]
AUDITS_PATH = ROOT / "runs" / "audits.jsonl"
CAUSES_PATH = ROOT / "runs" / "causes.json"

MIN_SUPPORT = 3
FIXABILITY = {"prompt": 1.0, "flow_logic": 0.5, "asr": 0.25}

# parameter -> (name, description, type)
KNOWN = {
    "category_isq": ("Spec questions from the wrong category", "Bot asks spec questions that belong to another product category.", "prompt"),
    "looping": ("Bot keeps asking for the same detail", "Bot repeats the same question although the buyer already answered.", "flow_logic"),
    "qty_probing": ("Quantity never asked", "Bot never asks the buyer for the quantity.", "prompt"),
    "qty_missed": ("Quantity given but not captured", "Buyer stated a quantity but the bot did not record it.", "prompt"),
    "product_change": ("Product change not followed", "Buyer changed the product but the bot kept the old one.", "prompt"),
    "dead_air": ("Long silences on the call", "Long silent gaps with no one speaking.", "flow_logic"),
}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for k successes in n trials. Returns (0, 0) when n == 0."""
    if n <= 0:
        return 0.0, 0.0
    p = k / n
    z2 = z * z
    denom = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def build_causes(audits: list[AuditRecord]) -> list[Cause]:
    audited = [a for a in audits if a.verdict != "NeedsReview"]
    n = len(audited)
    params: dict[str, dict[str, set[str]]] = {}
    for a in audited:
        for f in a.findings:
            if f.status not in ("Fatal", "NonFatal"):
                continue
            d = params.setdefault(f.parameter, {"Fatal": set(), "NonFatal": set()})
            d[f.status].add(a.call_id)
    causes: list[Cause] = []
    for param, d in params.items():
        fatal = sorted(d["Fatal"])
        nonfatal = sorted(d["NonFatal"] - d["Fatal"])  # once per call, highest severity
        affected = len(fatal) + len(nonfatal)
        name, desc, typ = KNOWN.get(param, (param, f"Rubric parameter '{param}' failed.", "prompt"))
        fix = FIXABILITY[typ] if typ in FIXABILITY else 1.0
        lo, hi = wilson(affected, n)
        status = "ranked" if affected >= MIN_SUPPORT else "watch" if fatal else "isolated"
        causes.append(Cause(
            cause_id=f"cause_{param}", name=name, description=desc, type=typ, parameter=param,
            fatal_calls=fatal, nonfatal_calls=nonfatal, fixability=fix,
            priority=(5 * len(fatal) + len(nonfatal)) * fix,
            freq_ci_low=lo, freq_ci_high=hi, status=status,
        ))
    order = {"ranked": 0, "watch": 1, "isolated": 2}
    causes.sort(key=lambda c: (order[c.status], -c.priority))
    return causes


def run_issues(audits_path: Path = AUDITS_PATH, out_path: Path = CAUSES_PATH) -> list[Cause]:
    audits = [AuditRecord.model_validate_json(l) for l in Path(audits_path).read_text(encoding="utf-8").splitlines() if l.strip()]
    causes = build_causes(audits)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps([c.model_dump() for c in causes], ensure_ascii=False, indent=1), encoding="utf-8")
    return causes
