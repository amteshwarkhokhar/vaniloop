"""AUDIT step 1b: Jev (TypeSafe) makes the judgement labels, Gemini keeps the structure.

Jev owns question_fits_product, fits_confirmed_product, product_match and frustrated. Every other ledger
field stays as Gemini extracted it, and the Python rules do not change. One focused request per judgement (a spec
turn, a buyer turn, the product match): the state holds only the fields that judgement needs, never the call.
"""

from __future__ import annotations

import hashlib
import json
import math

from .llm import complete_jev, jev_model
from .models import CallRecord, Ledger
from .rules import FRUSTRATED_P

JEV_VERSION = "4"  # bump whenever the questions or the thresholds change; part of the cache key
# Thresholds per label, tuned on the dev half of the Fable-reviewed set and checked on the held-out half (#121).
Q_TRUE_AT, Q_FALSE_AT = 0.55, 0.20  # question_fits_product
OPT_TRUE_AT, OPT_FALSE_AT = 0.50, 0.20  # one option of a spec turn
N_DROP_AT = 0.35  # name check: q is dropped when n <= this
PM_TRUE_AT, PM_FALSE_AT = 0.65, 0.35  # product match


def to_bool(p: float, true_at: float = PM_TRUE_AT, false_at: float = PM_FALSE_AT) -> bool | None:
    """>= true_at True, <= false_at False, anything between None (the rule says Unknown). Default: product match."""
    return True if p >= true_at else False if p <= false_at else None


def _noul(instructions: str, yes, no) -> dict:
    return {"type": "noul", "instructions": instructions, "criteria": {"true": yes, "false": no}}


def _crit(description: str, *examples: str) -> dict:
    return {"description": description, "examples": list(examples)}


_SPEC_TRUE = _crit(
    "A sales agent could reasonably ask a buyer of this product about it",
    "voltage for an electric bike switch",
    "texture (soft or hard) for copper wire",
    "a generic buying question that fits any product: order type (monthly or sample), payment terms, usage, "
    "any other details",
)
_SPEC_FALSE = _crit(
    "It belongs to a different kind of product, or makes no sense for this one",
    "flight time for a dome camera",
    "fabric type for a steel pipe",
    "what the buyer packs, asked about a machine belt",
)
_OPT_TRUE = _crit(
    "A real, sensible choice of this specification for this product",
    "soft or hard for copper wire texture",
    "two or three lanes for a vending machine",
)
_OPT_FALSE = _crit(
    "Not a sensible choice for this product, or not a kind of this specification",
    "mild steel as the work material of a glass cutting line",
    "HP or Dell as the brand of a car display",
    "repairing as the type of a charger being bought",
)
_IRR_TRUE = _crit(
    "The buyer shows annoyance at the bot",
    "complains the bot keeps asking or repeating",
    "says they already answered, or tells the bot to stop",
    "speaks angrily, rudely or sarcastically to the bot",
)
_IRR_FALSE = _crit(
    "Anything else",
    "checking the line: 'hello?'",
    "asking the bot to wait",
    "answering, agreeing or repeating an answer",
    "correcting the bot or disagreeing calmly",
    "confusion, language trouble or an off-topic remark",
)
_PM_TRUE = _crit("Same product", "'pen holder' and 'doctor coat shape pen stand'")
_PM_FALSE = _crit(
    "Different products, even if they share a word",
    "'chappal sheet' to buy and 'chappal sheet wastage' to sell",
    "'Nokia 535 back door' and 'Lumia 535 back door'",
)


def spec_product(led: Ledger, idx: int) -> str | None:
    """The product the buyer has confirmed at bot turn idx: the final one after the change turn, else the initial one.

    No fallback to the other segment: a missing product means no question, and the labels stay None.
    """
    changed = led.product_changed_at_turn
    return led.final_product if changed is not None and idx > changed else led.initial_product


def _requests(call: CallRecord, led: Ledger) -> list[tuple[str, int | None, dict, dict[str, dict]]]:
    """Every Jev request of a call as (kind, idx, state, questions). One focused state per judgement, never the call.

    kind is "spec" (one per spec turn with a product), "buyer" (one per buyer turn) or "pm" (one per call).
    """
    text = {t.idx: t.text for t in call.turns}
    out: list[tuple[str, int | None, dict, dict[str, dict]]] = []
    for b in led.bot_turns:
        product = spec_product(led, b.idx)
        if b.slot != "spec" or not product or b.idx not in text:
            continue
        spec = b.spec_name or ""
        state = {
            "product_the_buyer_wants": product,
            "bot_turn": text[b.idx],
            "specification_asked": spec,
            "options_offered": list(b.options_offered),
        }
        questions = {
            "q": _noul(
                "Could a sales agent reasonably ask a buyer of `product_the_buyer_wants` about "
                "`specification_asked`" + ("" if spec else " (the specification asked in `bot_turn`)") + "? "
                "Judge the specification only, not the options.",
                _SPEC_TRUE,
                _SPEC_FALSE,
            )
        }
        if spec:  # spec_name is Gemini's: check it names what the turn really asks
            questions["n"] = _noul(
                "Ignoring any example options, does `bot_turn` ask the buyer about `specification_asked`?",
                "The turn asks about this specification.",
                "The turn asks about something else.",
            )
        for i in range(len(b.options_offered)):
            questions[f"opt{i}"] = _noul(
                f"Is `options_offered[{i}]` a sensible choice of `specification_asked` for `product_the_buyer_wants`?",
                _OPT_TRUE,
                _OPT_FALSE,
            )
        out.append(("spec", b.idx, state, questions))
    asked = {u.idx for u in led.user_turns}
    last_bot = ""
    for t in call.turns:
        if t.speaker == "bot":
            last_bot = t.text
        elif t.idx in asked:
            q = _noul("In `buyer_reply`, is the buyer irritated or annoyed with the sales bot?", _IRR_TRUE, _IRR_FALSE)
            out.append(("buyer", t.idx, {"bot_turn": last_bot, "buyer_reply": t.text}, {"f": q}))
    confirmed, final = led.bot_confirmed.get("product"), led.final_product
    if confirmed and final:
        state = {"product_the_bot_confirmed": confirmed, "product_the_buyer_finally_wants": final}
        q = _noul(
            "Would a seller treat `product_the_bot_confirmed` and `product_the_buyer_finally_wants` as the same "
            "product to quote for, even if one name adds details such as size, form, brand, model or concentration?",
            _PM_TRUE,
            _PM_FALSE,
        )
        out.append(("pm", None, state, {"pm": q}))
    return out


def build_questions(call: CallRecord, led: Ledger) -> list[tuple[dict, dict[str, dict]]]:
    """The (state, questions) of each Jev request of the call, in call order (spec and buyer turns, then product)."""
    return [(state, questions) for _, _, state, questions in _requests(call, led)]


def _spec_labels(options: int, ans: dict[str, float], named: bool) -> tuple[bool | None, bool | None]:
    """(question_fits_product, fits_confirmed_product) from the answers of one spec turn."""
    q = None if "q" not in ans else to_bool(ans["q"], Q_TRUE_AT, Q_FALSE_AT)
    if named and ans.get("n", 0.0) <= N_DROP_AT:  # a missing check counts as a failed one
        q = None
    if q is None:
        return None, None
    opts = [
        None if f"opt{i}" not in ans else to_bool(ans[f"opt{i}"], OPT_TRUE_AT, OPT_FALSE_AT) for i in range(options)
    ]
    if q is False or False in opts:
        return q, False
    return q, True if all(o is True for o in opts) else None


def judge_ledger(call: CallRecord, led: Ledger) -> Ledger:
    """Return a copy of led with Jev's labels in place of Gemini's. Every other field is unchanged."""
    spec: dict[int, tuple[bool | None, bool | None]] = {}
    frustrated: dict[int, float] = {}
    pm: bool | None = None
    opts = {b.idx: len(b.options_offered) for b in led.bot_turns}
    named = {b.idx: bool(b.spec_name) for b in led.bot_turns}
    # A failed request or a missing answer raises: the whole call fails, the judge step logs it and retries it,
    # and no row with a made-up label is written.
    for kind, idx, state, questions in _requests(call, led):
        ans = complete_jev(state, questions, cache_tag=JEV_VERSION)
        if kind == "spec":
            spec[idx] = _spec_labels(opts[idx], ans, named[idx])
        elif kind == "buyer":
            if "f" not in ans:
                raise ValueError(f"jev: no irritation answer for user turn {idx}")
            frustrated[idx] = ans["f"]  # the probability, used directly
        else:
            if "pm" not in ans:
                raise ValueError("jev: no product match answer")
            pm = to_bool(ans["pm"])
    unjudged = [u.idx for u in led.user_turns if u.idx not in frustrated]
    if unjudged:  # a ledger buyer turn with no transcript turn: never default it to calm
        raise ValueError(f"jev: user turns {unjudged} have no transcript turn")
    bots = [
        b.model_copy(update={"question_fits_product": spec[b.idx][0], "fits_confirmed_product": spec[b.idx][1]})
        if b.idx in spec
        else b.model_copy(update={"question_fits_product": None, "fits_confirmed_product": None})
        if b.slot == "spec"
        else b
        for b in led.bot_turns
    ]
    users = [u.model_copy(update={"frustrated": frustrated[u.idx]}) for u in led.user_turns]
    return led.model_copy(update={"bot_turns": bots, "user_turns": users, "product_match": pm})


def fingerprint(call: CallRecord, led: Ledger) -> str:
    """sha256 of what a judged row depends on: every request (state and questions), the model, JEV_VERSION and the
    source ledger (the row copies its structural fields, so a changed Gemini field must redo the row)."""
    raw = json.dumps(
        [build_questions(call, led), jev_model(), JEV_VERSION, led.model_dump(mode="json")],
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _kappa(pairs: list[tuple[bool, bool]]) -> float | None:
    n = len(pairs)
    if not n:
        return None
    po = sum(a == b for a, b in pairs) / n
    pa, pb = sum(a for a, _ in pairs) / n, sum(b for _, b in pairs) / n
    pe = pa * pb + (1 - pa) * (1 - pb)
    return None if pe == 1 else (po - pe) / (1 - pe)


def _fields(led: dict) -> dict[tuple, bool | None]:
    out: dict[tuple, bool | None] = {("product_match", ""): led.get("product_match")}
    for b in led.get("bot_turns", []):
        for f in ("question_fits_product", "fits_confirmed_product"):
            if b.get("slot") == "spec":
                out[(f, b["idx"])] = b.get(f)
    for u in led.get("user_turns", []):
        v = u.get("frustrated")
        ok = isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
        out[("frustrated", u["idx"])] = v >= FRUSTRATED_P if ok else None  # missing stays None, never False
    return out


def judge_agreement(a_rows: list[dict], b_rows: list[dict]) -> list[dict]:
    """Per field: how often ledger files A (Gemini) and B (Jev) agree, and Cohen's kappa.

    Only labels that both files hold (not None) count. `none_a` and `none_b` count the labels dropped.
    frustrated is compared as `>= FRUSTRATED_P`.
    """
    b_by = {r["call_id"]: r for r in b_rows if "call_id" in r}
    pairs: dict[str, list[tuple[bool, bool]]] = {}
    none: dict[str, list[int]] = {}
    for ra in a_rows:
        rb = b_by.get(ra.get("call_id"))
        if rb is None:
            continue
        fa, fb = _fields(ra), _fields(rb)
        for k, va in fa.items():
            vb = fb.get(k)
            name = k[0] if k[0] != "frustrated" else f"frustrated>={FRUSTRATED_P}"
            n = none.setdefault(name, [0, 0])
            pairs.setdefault(name, [])
            n[0] += va is None
            n[1] += vb is None
            if va is not None and vb is not None:
                pairs[name].append((va, vb))
    return [
        {
            "field": name,
            "n": len(p),
            "agree": sum(a == b for a, b in p),
            "kappa": _kappa(p),
            "none_a": none[name][0],
            "none_b": none[name][1],
        }
        for name, p in pairs.items()
    ]
