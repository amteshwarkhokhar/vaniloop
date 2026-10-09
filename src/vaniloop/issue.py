"""ISSUE: audits -> ranked causes (v0: deterministic, one cause per failing rubric parameter)."""

from __future__ import annotations

import json
import math
import os
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from .jsonl import dedupe_by_call_id, read_jsonl
from .models import AuditRecord, Cause

ROOT = Path(__file__).resolve().parents[2]
AUDITS_PATH = ROOT / "runs" / "audits.jsonl"
CAUSES_PATH = ROOT / "runs" / "causes.json"

MIN_SUPPORT = 3
MIN_CATEGORIES = 2  # a pattern recurs in at least this many known product categories
UNKNOWN_CATEGORY = "unknown"  # same label as categorize.UNKNOWN (a test pins it); categorize imports this module
NOT_REPRODUCED_PREFIX = "replay did not reproduce the fault"  # start of the gate.decide reason (#63)
# Display order of the statuses. A pattern is ranked (gate-confirmed) or pattern_unconfirmed (no gate row yet).
STATUS_ORDER = {"ranked": 0, "pattern_unconfirmed": 0, "watch": 1, "outlier": 2, "single_category": 3, "isolated": 4}
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
        "Bot asks again for a quantity already given",
        "Buyer stated a quantity and the bot asked for it again (Fatal only if the buyer never repeats it).",
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
    causes.sort(key=lambda c: (STATUS_ORDER[c.status], -c.rank_score, -c.priority))
    return causes


def classify_causes(
    causes: list[Cause],
    category_of: Callable[[str], str],
    gate_rows: list[dict],
) -> tuple[list[Cause], list[dict]]:
    """Split causes into patterns and outliers. Returns (causes in display order, outlier log rows).

    A cause is a pattern only if (a) it has MIN_SUPPORT calls (build_causes: status ranked), (b) its calls span
    at least MIN_CATEGORIES known product categories and (c) its latest gate row is not "replay did not
    reproduce the fault". With no gate row yet it is `pattern_unconfirmed`, not an outlier. Else:
      - fails (b): `single_category` (a data signal). Fails (b) and (c): still single_category, both are logged.
      - fails (c) only: `outlier`.
      - fails (a): keeps `watch` or `isolated`, logged with condition `support`.
    A call with category `unknown` (no ledger, no product or an unlabelled name) is never a category: it adds
    nothing to the count, so unknowns cannot fake a second category. `other` is a real answer and counts as one.
    Scores and ranks are not changed: only the status and the order.
    """
    last_gate = {r.get("cause_id"): r for r in gate_rows}  # gates.jsonl is append-only: the last row wins
    out: list[Cause] = []
    outliers: list[dict] = []
    for c in causes:
        calls = c.fatal_calls + c.nonfatal_calls
        by_cat = Counter(category_of(call) for call in calls)
        unknown = by_cat.pop(UNKNOWN_CATEGORY, 0)
        gate = last_gate.get(c.cause_id)
        not_reproduced = gate is not None and str(gate.get("reason", "")).startswith(NOT_REPRODUCED_PREFIX)
        failed = []
        if c.status != "ranked":
            failed.append("support")
        elif len(by_cat) < MIN_CATEGORIES:
            failed.append("categories")
        if c.status == "ranked" and not_reproduced:
            failed.append("reproduction")
        if failed:
            status = c.status if failed == ["support"] else "single_category" if "categories" in failed else "outlier"
            outliers.append(
                {
                    "cause_id": c.cause_id,
                    "parameter": c.parameter,
                    "status": status,
                    "condition": failed[0],
                    "failed": failed,
                    "calls": len(calls),
                    "categories": dict(by_cat),
                    "unknown_calls": unknown,
                    "gate_reason": gate.get("reason") if gate else None,
                }
            )
        else:
            status = "ranked" if gate is not None else "pattern_unconfirmed"
        out.append(c.model_copy(update={"status": status}))
    out.sort(key=lambda c: (STATUS_ORDER[c.status], -c.rank_score, -c.priority))
    return out, outliers


def load_audits(path: Path) -> list[AuditRecord]:
    rows = dedupe_by_call_id(read_jsonl(path))
    out = []
    for r in rows:
        try:
            out.append(AuditRecord.model_validate(r))
        except Exception:  # a malformed row must not stop ranking
            continue
    return out


def _call_categories(runs: Path) -> Callable[[str], str]:
    """call_id -> category from `<runs>/ledgers.jsonl` and `<runs>/categories.json` (read once, not per call)."""
    from .categorize import load_categories, name_key, product_of  # lazy: categorize imports this module

    cats = load_categories(runs / "categories.json")
    by_call = {}
    for r in dedupe_by_call_id(read_jsonl(runs / "ledgers.jsonl"), last_wins=True):
        name = product_of(r)
        by_call[r["call_id"]] = cats.get(name_key(name), UNKNOWN_CATEGORY) if name else UNKNOWN_CATEGORY
    return lambda call_id: by_call.get(call_id, UNKNOWN_CATEGORY)


def run_issues(audits_path: Path = AUDITS_PATH, out_path: Path = CAUSES_PATH) -> list[Cause]:
    """Rank causes, split patterns from outliers, write causes.json and outliers.jsonl.

    categories.json, ledgers.jsonl and gates.jsonl are read from the folder of `out_path`; outliers.jsonl is
    written there and rewritten whole each run (it is a snapshot of today's outliers, not a history).
    """
    audits = load_audits(Path(audits_path))
    runs = Path(out_path).parent
    causes, outliers = classify_causes(build_causes(audits), _call_categories(runs), read_jsonl(runs / "gates.jsonl"))
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
    log = out.with_name("outliers.jsonl")
    tmp = log.with_name(log.name + f".{os.getpid()}.tmp")
    tmp.write_text("".join(json.dumps(o, ensure_ascii=False) + "\n" for o in outliers), encoding="utf-8")
    os.replace(tmp, log)
    return causes
