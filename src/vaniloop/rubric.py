"""The audit rubric as one editable file: overrides for rule constants, Jev thresholds and Jev question texts.

The file is `<runs>/rubric.json` (or VANILOOP_RUBRIC): {"params": {id: number}, "questions": {id: {...}}}.
No file means no overrides, so every default is today's behaviour. `apply` writes the overrides into the `rules`
and `jev` modules, which read those names at call time. `describe` is what the console shows.
"""

# ruff: noqa: E501  (the param and check tables hold one-line sentences)
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path

from . import jev, rules


class RubricError(ValueError):
    """A bad rubric body or file. `field` names the offending id, so the console can mark it."""

    def __init__(self, field: str, msg: str):
        super().__init__(f"{field}: {msg}")
        self.field, self.msg = field, msg


# ------------------------------------------------------------------ params
_JEV_NAMES = ("Q_TRUE_AT", "Q_FALSE_AT", "OPT_TRUE_AT", "OPT_FALSE_AT", "PM_TRUE_AT", "PM_FALSE_AT", "N_DROP_AT")


def _p(pid: str, group: str, label: str, help_: str, typ: str, lo: float, hi: float, step: float) -> dict:
    mod, _, name = pid.partition(".")
    if name.startswith("SLOT_LIMITS."):
        default = rules.SLOT_LIMITS[name.split(".", 1)[1]]
    else:
        default = getattr(rules if mod == "rules" else jev, name)
    return {"id": pid, "group": group, "label": label, "help": help_, "type": typ, "min": lo, "max": hi,
            "step": step, "default": default}  # fmt: skip


PARAMS: list[dict] = [
    _p("rules.DEAD_AIR_SEC", "audit", "Dead air limit (s)", "A bot gap longer than this many seconds is dead air.",
       "float", 1, 30, 0.5),
    _p("rules.FRUSTRATED_P", "audit", "Irritation mark", "A buyer turn with irritation at or above this counts as irritated.",
       "float", 0, 1, 0.05),
    _p("rules.HUMAN_P", "audit", "Human request mark", "A buyer turn asking for a human at or above this raises the human_request flag.",
       "float", 0, 1, 0.05),
    _p("rules.LOOP_IRRITATION_TURNS", "audit", "Irritation window (buyer turns)", "A loop is Fatal when the buyer is irritated within this many buyer turns of the ask past the limit.",
       "int", 1, 10, 1),
    _p("rules.LOOP_MAX_ASKS", "audit", "Default ask limit", "How many times the bot may ask for a slot that has no limit of its own.",
       "int", 1, 10, 1),
    _p("rules.SHORT_CALL_SEC", "audit", "Short call (s)", "A call shorter than this is sent to review without grading.",
       "float", 0, 120, 1),
    _p("rules.MIN_COVERAGE", "audit", "Minimum label coverage", "A call whose labelled turns fall under this share is sent to review.",
       "float", 0, 1, 0.05),
    _p("rules.SLOT_LIMITS.product", "slot limits", "Product asks", "How many times the bot may ask for the product.",
       "int", 1, 10, 1),
    _p("rules.SLOT_LIMITS.quantity", "slot limits", "Quantity asks", "How many times the bot may ask for the quantity.",
       "int", 1, 10, 1),
    _p("rules.SLOT_LIMITS.spec", "slot limits", "Asks per spec", "How many times the bot may ask for one specification.",
       "int", 1, 10, 1),
    _p("rules.SLOT_LIMITS.name", "slot limits", "Name asks", "How many times the bot may ask for the buyer name.",
       "int", 1, 10, 1),
    _p("rules.SLOT_LIMITS.city", "slot limits", "City asks", "How many times the bot may ask for the city.",
       "int", 1, 10, 1),
    _p("rules.QTY_MINOR_ABS", "quantity", "Minor quantity gap (absolute)", "A quantity mismatch up to this many units can be minor.",
       "float", 0, 1000, 1),
    _p("rules.QTY_MINOR_REL", "quantity", "Minor quantity gap (share)", "A quantity mismatch up to this share of the quantity can be minor.",
       "float", 0, 1, 0.05),
    _p("rules.TIMING_MIN_COVERAGE", "timing", "Timing coverage", "The last turn must end at this share of the audio, or the timing is not trusted.",
       "float", 0, 1, 0.05),
    _p("rules.TIMING_MAX_BACK_TO_BACK", "timing", "Back-to-back turns", "More than this share of turns touching each other means the timing is not trusted.",
       "float", 0, 1, 0.05),
    _p("rules.TIMING_MAX_ZERO_TURNS", "timing", "Zero-length turns", "More than this many zero-length turns means the timing is not trusted.",
       "int", 0, 20, 1),
    _p("jev.Q_TRUE_AT", "jev", "Question fits: true at", "A spec question is judged to fit the product at or above this probability.",
       "float", 0, 1, 0.05),
    _p("jev.Q_FALSE_AT", "jev", "Question fits: false at", "A spec question is judged not to fit the product at or below this probability.",
       "float", 0, 1, 0.05),
    _p("jev.OPT_TRUE_AT", "jev", "Option fits: true at", "One option is judged a sensible choice at or above this probability.",
       "float", 0, 1, 0.05),
    _p("jev.OPT_FALSE_AT", "jev", "Option fits: false at", "One option is judged not sensible at or below this probability.",
       "float", 0, 1, 0.05),
    _p("jev.PM_TRUE_AT", "jev", "Same product: true at", "Two product names are judged the same product at or above this probability.",
       "float", 0, 1, 0.05),
    _p("jev.PM_FALSE_AT", "jev", "Same product: false at", "Two product names are judged different products at or below this probability.",
       "float", 0, 1, 0.05),
    _p("jev.N_DROP_AT", "jev", "Name check: drop at", "The question label is dropped to unsure when the name check is at or below this probability.",
       "float", 0, 1, 0.05),
]  # fmt: skip
_BY_ID = {p["id"]: p for p in PARAMS}
_PAIRS = (("jev.Q_FALSE_AT", "jev.Q_TRUE_AT"), ("jev.OPT_FALSE_AT", "jev.OPT_TRUE_AT"),
          ("jev.PM_FALSE_AT", "jev.PM_TRUE_AT"))  # fmt: skip

# ------------------------------------------------------------------ questions
_SPEC_TRUE = {
    "description": "A sales agent could reasonably ask a buyer of this product about it",
    "examples": [
        "voltage for an electric bike switch",
        "texture (soft or hard) for copper wire",
        "a generic buying question that fits any product: order type (monthly or sample), payment terms, usage, "
        "any other details",
    ],
}
_SPEC_FALSE = {
    "description": "It belongs to a different kind of product, or makes no sense for this one",
    "examples": [
        "flight time for a dome camera",
        "fabric type for a steel pipe",
        "what the buyer packs, asked about a machine belt",
    ],
}
_OPT_TRUE = {
    "description": "A real, sensible choice of this specification for this product",
    "examples": ["soft or hard for copper wire texture", "two or three lanes for a vending machine"],
}
_OPT_FALSE = {
    "description": "Not a sensible choice for this product, or not a kind of this specification",
    "examples": [
        "mild steel as the work material of a glass cutting line",
        "HP or Dell as the brand of a car display",
        "repairing as the type of a charger being bought",
    ],
}
_IRR_TRUE = {
    "description": "The buyer shows annoyance at the bot",
    "examples": [
        "complains the bot keeps asking or repeating",
        "says they already answered, or tells the bot to stop",
        "speaks angrily, rudely or sarcastically to the bot",
    ],
}
_IRR_FALSE = {
    "description": "Anything else",
    "examples": [
        "checking the line: 'hello?'",
        "asking the bot to wait",
        "answering, agreeing or repeating an answer",
        "correcting the bot or disagreeing calmly",
        "confusion, language trouble or an off-topic remark",
    ],
}
_PM_TRUE = {"description": "Same product", "examples": ["'pen holder' and 'doctor coat shape pen stand'"]}
_PM_FALSE = {
    "description": "Different products, even if they share a word",
    "examples": [
        "'chappal sheet' to buy and 'chappal sheet wastage' to sell",
        "'Nokia 535 back door' and 'Lumia 535 back door'",
    ],
}

# id -> the Jev question id (qid) and the default text. `{i}` in option_fit stands for the option's position, and
# the first `specification_asked` in spec_fit gets a pointer to bot_turn when the ledger has no spec name.
QUESTIONS: dict[str, dict] = {
    "spec_fit": {
        "qid": "q",
        "label": "Does the spec question fit the product?",
        "instructions": "Could a sales agent reasonably ask a buyer of `product_the_buyer_wants` about "
        "`specification_asked`? Judge the specification only, not the options.",
        "true": _SPEC_TRUE,
        "false": _SPEC_FALSE,
    },
    "spec_name_check": {
        "qid": "n",
        "label": "Does the turn ask about the named spec?",
        "instructions": "Ignoring any example options, does `bot_turn` ask the buyer about `specification_asked`?",
        "true": {"description": "The turn asks about this specification.", "examples": []},
        "false": {"description": "The turn asks about something else.", "examples": []},
    },
    "option_fit": {
        "qid": "opt",
        "label": "Is one option a sensible choice?",
        "instructions": "Is `options_offered[{i}]` a sensible choice of `specification_asked` for "
        "`product_the_buyer_wants`?",
        "true": _OPT_TRUE,
        "false": _OPT_FALSE,
    },
    "irritation": {
        "qid": "f",
        "label": "Is the buyer irritated?",
        "instructions": "In `buyer_reply`, is the buyer irritated or annoyed with the sales bot?",
        "true": _IRR_TRUE,
        "false": _IRR_FALSE,
    },
    "product_match": {
        "qid": "pm",
        "label": "Is it the same product?",
        "instructions": "Would a seller treat `product_the_bot_confirmed` and `product_the_buyer_finally_wants` as "
        "the same product to quote for, even if one name adds details such as size, form, brand, model or "
        "concentration?",
        "true": _PM_TRUE,
        "false": _PM_FALSE,
    },
}
_DEFAULTS = copy.deepcopy(QUESTIONS)

# ------------------------------------------------------------------ checks
_SLOTS = [f"rules.SLOT_LIMITS.{k}" for k in ("product", "quantity", "spec", "name", "city")]
CHECKS: list[dict] = [
    {"id": "intent", "name": "Buying intent",
     "what": "A buyer who declines must not be collected into a lead.",
     "fatal_when": "After the buyer declines, the bot asks for a quantity, unit or spec, or reads a lead back, and the buyer has not given a new value.",
     "nonfatal_when": "Never.", "prompt_ref": "", "params": [], "judged_by": "gemini+code"},
    {"id": "category_isq", "name": "Right questions",
     "what": "The spec questions and their options must fit the product the buyer wants.",
     "fatal_when": "A spec question belongs to another kind of product.",
     "nonfatal_when": "The question fits but one option does not.", "prompt_ref": "L1279",
     "params": ["jev.Q_TRUE_AT", "jev.Q_FALSE_AT", "jev.OPT_TRUE_AT", "jev.OPT_FALSE_AT", "jev.N_DROP_AT"],
     "judged_by": "jev+code"},
    {"id": "product_change", "name": "Right product",
     "what": "The product the bot confirmed must be the product the buyer finally wants.",
     "fatal_when": "A lead is built for a different product than the buyer wants.",
     "nonfatal_when": "A handoff happens with a wrong confirmed product.", "prompt_ref": "L1175, L1701",
     "params": ["jev.PM_TRUE_AT", "jev.PM_FALSE_AT"], "judged_by": "jev+code"},
    {"id": "qty_probing", "name": "Quantity asked",
     "what": "The bot must ask for the quantity, unless the buyer gave it first.",
     "fatal_when": "The bot never asked and the buyer never gave a quantity.",
     "nonfatal_when": "Never.", "prompt_ref": "L1247", "params": [], "judged_by": "gemini+code"},
    {"id": "qty_missed", "name": "Not asked twice",
     "what": "A quantity the buyer gave is stored and the bot moves on without asking again.",
     "fatal_when": "The bot asks again, the re-ask goes unanswered and no matching quantity was captured.",
     "nonfatal_when": "The bot asks again but the quantity was captured or the buyer answers the re-ask.",
     "prompt_ref": "L1261", "params": [], "judged_by": "gemini+code"},
    {"id": "qty_accuracy", "name": "Right number",
     "what": "The quantity the bot confirmed must match what the buyer said.",
     "fatal_when": "The numbers differ by more than the minor band.",
     "nonfatal_when": "The numbers differ by a minor gap.", "prompt_ref": "",
     "params": ["rules.QTY_MINOR_ABS", "rules.QTY_MINOR_REL"], "judged_by": "gemini+code"},
    {"id": "qty_unit", "name": "Right unit",
     "what": "The unit the bot confirmed must match the unit the buyer said.",
     "fatal_when": "The units are of a different kind.", "nonfatal_when": "The units are of the same kind but differ.",
     "prompt_ref": "", "params": [], "judged_by": "gemini+code"},
    {"id": "looping", "name": "No looping",
     "what": "The bot must not ask for the same slot more often than its limit.",
     "fatal_when": "A slot is asked past its limit and the buyer is irritated soon after.",
     "nonfatal_when": "A slot is asked past its limit and the buyer shows no irritation.", "prompt_ref": "L69, L83",
     "params": ["rules.LOOP_MAX_ASKS", "rules.LOOP_IRRITATION_TURNS", "rules.FRUSTRATED_P", *_SLOTS],
     "judged_by": "jev+code"},
    {"id": "dead_air", "name": "No dead air",
     "what": "The bot must not leave a long silence after the buyer speaks.",
     "fatal_when": "Never.", "nonfatal_when": "A bot gap is longer than the dead air limit.", "prompt_ref": "",
     "params": ["rules.DEAD_AIR_SEC", "rules.TIMING_MIN_COVERAGE", "rules.TIMING_MAX_BACK_TO_BACK",
                "rules.TIMING_MAX_ZERO_TURNS"], "judged_by": "code"},
]  # fmt: skip

# ------------------------------------------------------------------ state
_applied: dict = {"params": {}, "questions": {}}


def path(runs: Path | str | None = None) -> Path:
    """VANILOOP_RUBRIC when set, else rubric.json in the runs folder (`runs` when given, else the default runs/)."""
    env = os.environ.get("VANILOOP_RUBRIC")
    if env:
        return Path(env)
    if runs is not None:
        return Path(runs) / "rubric.json"
    from .issue import AUDITS_PATH  # the default runs folder; imported late to keep this module light

    return AUDITS_PATH.parent / "rubric.json"


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _check_text(field: str, s, lo: int, hi: int) -> None:
    if not isinstance(s, str) or len(s.strip()) < lo or len(s) > hi:
        raise RubricError(field, f"must be text of {lo} to {hi} characters")


def _check_question(qid: str, q) -> None:
    f = f"questions.{qid}"
    if not isinstance(q, dict) or set(q) != {"instructions", "true", "false"}:
        raise RubricError(f, "must hold exactly instructions, true and false")
    _check_text(f"{f}.instructions", q["instructions"], 1, 2000)
    for side in ("true", "false"):
        c = q[side]
        if not isinstance(c, dict) or set(c) != {"description", "examples"}:
            raise RubricError(f"{f}.{side}", "must hold exactly description and examples")
        _check_text(f"{f}.{side}.description", c["description"], 1, 2000)
        ex = c["examples"]
        if not isinstance(ex, list) or len(ex) > 12:
            raise RubricError(f"{f}.{side}.examples", "must be a list of at most 12 items")
        for i, e in enumerate(ex):
            if not isinstance(e, str) or len(e) > 300:
                raise RubricError(f"{f}.{side}.examples[{i}]", "must be text of at most 300 characters")


def _norm(pid: str, v):
    p = _BY_ID[pid]
    if not _is_num(v):
        raise RubricError(pid, "must be a number")
    if p["type"] == "int":
        if isinstance(v, float) and not v.is_integer():
            raise RubricError(pid, "must be a whole number")
        v = int(v)
    if not p["min"] <= v <= p["max"]:
        raise RubricError(pid, f"must be between {p['min']:g} and {p['max']:g}")
    return v


def _effective_params(overrides: dict) -> dict:
    return {p["id"]: overrides.get("params", {}).get(p["id"], p["default"]) for p in PARAMS}


def validate(body: dict, current: dict | None = None) -> None:
    """Raise RubricError(field, msg) for a bad body. A None value is a deletion and is always allowed.

    `current` is the override dict already on disk. The pair checks run on the merged values, because one edited
    threshold can break a pair with a stored one.
    """
    if not isinstance(body, dict) or not set(body) <= {"params", "questions"}:
        raise RubricError("body", "must be an object with params and questions only")
    params, questions = body.get("params") or {}, body.get("questions") or {}
    if not isinstance(params, dict) or not isinstance(questions, dict):
        raise RubricError("body", "params and questions must be objects")
    for pid, v in params.items():
        if pid not in _BY_ID:
            raise RubricError(str(pid), "unknown id")
        if v is not None:
            _norm(pid, v)
    for qid, q in questions.items():
        if qid not in QUESTIONS:
            raise RubricError(str(qid), "unknown id")
        if q is not None:
            _check_question(qid, q)
    merged = {"params": {**((current or {}).get("params") or {})}}
    for pid, v in params.items():
        if v is None:
            merged["params"].pop(pid, None)
        else:
            merged["params"][pid] = _norm(pid, v)
    eff = _effective_params(merged)
    for false_id, true_id in _PAIRS:
        if not eff[false_id] < eff[true_id]:
            raise RubricError(false_id, f"must be below {true_id} (now {eff[true_id]:g})")


def load(p: Path | str | None = None) -> dict:
    """The override dict ({"params": ..., "questions": ...}). A missing file is no overrides; a bad one raises."""
    p = Path(p) if p is not None else path()
    if not p.exists():
        return {"params": {}, "questions": {}}
    try:
        body = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise RubricError("file", f"cannot read {p.name}: {type(e).__name__}") from None
    validate(body)
    return {"params": {k: _norm(k, v) for k, v in (body.get("params") or {}).items()},
            "questions": dict(body.get("questions") or {})}  # fmt: skip


def jev_suffix(overrides: dict) -> str:
    """ "" with no Jev override, else "+<first 8 hex of sha256 of the Jev overrides>"."""
    jev_params = {k: v for k, v in (overrides.get("params") or {}).items() if k.startswith("jev.")}
    questions = overrides.get("questions") or {}
    if not jev_params and not questions:
        return ""
    raw = json.dumps({"params": jev_params, "questions": questions}, sort_keys=True, ensure_ascii=False)
    return "+" + hashlib.sha256(raw.encode()).hexdigest()[:8]


def save(p: Path | str, body: dict) -> dict:
    """Validate body, merge it into the file (None deletes an override) and write it atomically. Returns describe."""
    p = Path(p)
    cur = load(p)
    validate(body, cur)
    for key in ("params", "questions"):
        for k, v in (body.get(key) or {}).items():
            if v is None:
                cur[key].pop(k, None)
            else:
                cur[key][k] = _norm(k, v) if key == "params" else copy.deepcopy(v)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)
    return describe(p)


def describe(p: Path | str | None = None) -> dict:
    """What the console shows: every check, every param with its value, every question with its default."""
    p = Path(p) if p is not None else path()
    ov = load(p)
    params = [{**q, "value": ov["params"].get(q["id"], q["default"]), "overridden": q["id"] in ov["params"]}
              for q in PARAMS]  # fmt: skip
    questions = []
    for qid, d in _DEFAULTS.items():
        over = ov["questions"].get(qid)
        questions.append({"id": qid, "qid": d["qid"], "label": d["label"], "default": _public(d),
                          "value": over or _public(d), "overridden": over is not None})  # fmt: skip
    return {"path_name": p.name, "checks": copy.deepcopy(CHECKS), "params": params, "questions": questions,
            "jev_version_effective": jev.JEV_VERSION + jev_suffix(ov)}  # fmt: skip


def _public(d: dict) -> dict:
    return {"instructions": d["instructions"], "true": copy.deepcopy(d["true"]), "false": copy.deepcopy(d["false"])}


def question(qid: str) -> dict:
    """The effective {instructions, true, false} of an editable question: the applied override, else the default."""
    return copy.deepcopy(_applied["questions"].get(qid) or _public(_DEFAULTS[qid]))


def version_suffix() -> str:
    """jev_suffix of what apply last set. jev.fingerprint adds it to JEV_VERSION."""
    return jev_suffix(_applied)


def apply(p: Path | str | None = None) -> dict:
    """Reset every param to its default, then set the overrides of the file on the rules and jev modules."""
    ov = load(p)
    for q in PARAMS:
        _set(q["id"], q["default"])
    for pid, v in ov["params"].items():
        _set(pid, v)
    _applied["params"], _applied["questions"] = dict(ov["params"]), dict(ov["questions"])
    return ov


def _set(pid: str, v) -> None:
    mod, _, name = pid.partition(".")
    if name.startswith("SLOT_LIMITS."):
        rules.SLOT_LIMITS[name.split(".", 1)[1]] = v
    else:
        setattr(rules if mod == "rules" else jev, name, v)
