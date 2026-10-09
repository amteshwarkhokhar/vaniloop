"""ISSUE: audits -> ranked causes (v0: deterministic, one cause per failing rubric parameter)."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

from .jsonl import dedupe_by_call_id, read_jsonl
from .models import AuditRecord, Cause

ROOT = Path(__file__).resolve().parents[2]
AUDITS_PATH = ROOT / "runs" / "audits.jsonl"
CAUSES_PATH = ROOT / "runs" / "causes.json"

MIN_SUPPORT = 3
FIXABILITY = {"prompt": 1.0, "flow_logic": 0.5, "asr": 0.25}

# parameter -> (name, description, type)
KNOWN = {
    "category_isq": (
        "Spec questions from the wrong category",
        "Bot asks spec questions that belong to another product category.",
        "prompt",
    ),
    "looping": (
        "Bot keeps asking for the same detail",
        "Bot repeats the same question although the buyer already answered.",
        "flow_logic",
    ),
    "qty_probing": ("Quantity never asked", "Bot never asks the buyer for the quantity.", "prompt"),
    "qty_missed": (
        "Quantity given but not captured",
        "Buyer stated a quantity but the bot did not record it.",
        "prompt",
    ),
    "qty_accuracy": (
        "Quantity captured wrong",
        "Buyer stated a quantity but the bot confirmed a different number.",
        "prompt",
    ),
    "qty_unit": (
        "Quantity unit captured wrong",
        "Buyer stated a quantity in one kind of unit but the bot confirmed another kind.",
        "prompt",
    ),
    "product_change": (
        "Product change not followed",
        "Buyer changed the product but the bot kept the old one.",
        "prompt",
    ),
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


def _dedupe(audits: list[AuditRecord]) -> list[AuditRecord]:
    """One audit per call_id, last wins."""
    return list({a.call_id: a for a in audits}.values())


def build_causes(audits: list[AuditRecord]) -> list[Cause]:
    audits = _dedupe(audits)
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
        causes.append(
            Cause(
                cause_id=f"cause_{param}",
                name=name,
                description=desc,
                type=typ,
                parameter=param,
                fatal_calls=fatal,
                nonfatal_calls=nonfatal,
                fixability=fix,
                priority=(5 * len(fatal) + len(nonfatal)) * fix,
                rank_score=lo * ((5 * len(fatal) + len(nonfatal)) / affected) * fix,
                freq_ci_low=lo,
                freq_ci_high=hi,
                status=status,
            )
        )
    order = {"ranked": 0, "watch": 1, "isolated": 2}
    causes.sort(key=lambda c: (order[c.status], -c.rank_score, -c.priority))
    return causes


def load_audits(path: Path) -> list[AuditRecord]:
    rows = dedupe_by_call_id(read_jsonl(path))
    out = []
    for r in rows:
        try:
            out.append(AuditRecord.model_validate(r))
        except Exception:  # a malformed row must not stop ranking
            continue
    return out


def run_issues(audits_path: Path = AUDITS_PATH, out_path: Path = CAUSES_PATH) -> list[Cause]:
    audits = load_audits(Path(audits_path))
    causes = build_causes(audits)
    review = sum(a.verdict == "NeedsReview" for a in audits)
    doc = {
        "denominator": len(audits) - review,
        "audited_total": len(audits),
        "needs_review": review,
        "causes": [c.model_dump() for c in causes],
    }
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, out)  # atomic: readers see the old or the new file, never a partial one
    return causes
