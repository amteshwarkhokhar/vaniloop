"""Live threshold tuning: re-score the Jev labels against the reviewed truth from the raw answers. No network.

`pipeline judge` saves every raw Jev answer in `<data>/jev_raw.jsonl`. Thresholds only turn those probabilities into
labels, so a new threshold set re-labels every call in memory. The labels are scored against the reviewed truth in
`<data>/jev_review.jsonl`, on the dev half and the held-out test half of the calls.
"""

from __future__ import annotations

import math
from pathlib import Path

from . import jev
from .jsonl import read_jsonl
from .split import split_half

LABELS = ("question_fits", "with_options_fits", "product_match", "irritated")
_PAIRS = {"question_fits": ("Q_TRUE_AT", "Q_FALSE_AT"), "option_fits": ("OPT_TRUE_AT", "OPT_FALSE_AT"),
          "product_match": ("PM_TRUE_AT", "PM_FALSE_AT")}  # fmt: skip


class NoRaw(Exception):
    """jev_raw.jsonl is missing: run `pipeline judge` first."""


def split_of(call_id: str) -> str:
    """ "dev" when the first byte of sha256(call_id) is even, else "test" (the split the thresholds were tuned on)."""
    return "dev" if split_half(call_id) == "fix" else "test"


def _num(name: str, v) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0 <= v <= 1:
        raise ValueError(f"{name}: must be a number from 0 to 1")
    return float(v)


def resolve(thresholds: dict | None) -> dict[str, float]:
    """Thresholds in the POST /api/tune shape -> the jev.thresholds() shape. Missing keys keep the current value."""
    th, t = jev.thresholds(), thresholds or {}
    if not isinstance(t, dict) or not set(t) <= {*_PAIRS, "name_check_drop_at", "frustrated_p"}:
        raise ValueError("thresholds: unknown key")
    for key, (true_name, false_name) in _PAIRS.items():
        pair = t.get(key) or {}
        if not isinstance(pair, dict) or not set(pair) <= {"true_at", "false_at"}:
            raise ValueError(f"{key}: expected true_at and false_at")
        if "true_at" in pair:
            th[true_name] = _num(f"{key}.true_at", pair["true_at"])
        if "false_at" in pair:
            th[false_name] = _num(f"{key}.false_at", pair["false_at"])
        if not th[false_name] < th[true_name]:
            raise ValueError(f"{key}: false_at must be below true_at")
    if "name_check_drop_at" in t:
        th["N_DROP_AT"] = _num("name_check_drop_at", t["name_check_drop_at"])
    if "frustrated_p" in t:
        th["FRUSTRATED_P"] = _num("frustrated_p", t["frustrated_p"])
    return th


def _label_map(requests: list[dict], th: dict[str, float]) -> dict[tuple[str, int | None], bool | None]:
    """{(label, turn idx or None): value} of one call. Same combination code as jev.labels."""
    spec, frustrated, pm = jev.decide(requests, th)
    out: dict[tuple[str, int | None], bool | None] = {}
    for idx, (q, fits) in spec.items():
        out[("question_fits", idx)], out[("with_options_fits", idx)] = q, fits
    for idx, p in frustrated.items():
        out[("irritated", idx)] = p >= th["FRUSTRATED_P"]
    if any(r["kind"] == "pm" for r in requests):
        out[("product_match", None)] = pm
    return out


def _tally(maps: dict[str, dict], review: list[dict]) -> list[dict]:
    rows = {(lab, sp): dict(label=lab, split=sp, n=0, right=0, wrong=0, unsure=0, gemini_right=0)
            for lab in LABELS for sp in ("dev", "test")}  # fmt: skip
    for item in review:
        cid = item.get("call_id")
        if cid not in maps or not isinstance(item.get("labels"), list):
            continue
        for lab in item["labels"]:
            if not isinstance(lab, dict) or lab.get("label") not in LABELS or not isinstance(lab.get("truth"), bool):
                continue  # truth null is unscored
            name = lab["label"]
            got = maps[cid].get((name, None if name == "product_match" else item.get("idx")))
            row = rows[(name, split_of(cid))]
            row["n"] += 1
            row["unsure" if got is None else "right" if got == lab["truth"] else "wrong"] += 1
            row["gemini_right"] += lab.get("gemini_ok") is True
    return list(rows.values())


def score(data_dir: str | Path, thresholds: dict | None = None) -> dict:
    """Score the labels at `thresholds` against the reviewed truth.

    Returns {labels: [{label, split, n, right, wrong, unsure, gemini_right}], current: the same at the thresholds in
    force, flips: {label: calls' labels that differ from the current thresholds}, calls}. Raises NoRaw when
    jev_raw.jsonl is missing and ValueError for a bad threshold.
    """
    d = Path(data_dir)
    raw_path = d / "jev_raw.jsonl"
    if not raw_path.exists():
        raise NoRaw("no raw Jev answers: run judge")
    new_th, cur_th = resolve(thresholds), jev.thresholds()
    raw = {r["call_id"]: r["requests"] for r in read_jsonl(raw_path) if "call_id" in r and "requests" in r}  # last wins
    new, cur = {c: _label_map(q, new_th) for c, q in raw.items()}, {c: _label_map(q, cur_th) for c, q in raw.items()}
    review = read_jsonl(d / "jev_review.jsonl")
    flips = dict.fromkeys(LABELS, 0)
    for cid, labels in new.items():
        for key, v in labels.items():
            flips[key[0]] += v != cur[cid][key]
    return {"labels": _tally(new, review), "current": _tally(cur, review), "flips": flips, "calls": len(raw)}
