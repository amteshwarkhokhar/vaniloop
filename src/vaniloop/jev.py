"""AUDIT step 1b: Jev (TypeSafe) makes the judgement labels, Gemini keeps the structure.

Jev owns question_fits_product, fits_confirmed_product, product_match and frustrated. Every other ledger
field stays as Gemini extracted it, and the Python rules do not change. One request per call, all questions
batched. Question ids are not sent to the model, so each question states the product and quotes the turn.
"""

from __future__ import annotations

import hashlib
import json
import math

from .llm import complete_jev, jev_model
from .models import CallRecord, Ledger
from .rules import FRUSTRATED_P

JEV_VERSION = "3"  # bump whenever the questions or the thresholds change; part of the cache key
TRUE_AT, FALSE_AT = 0.65, 0.35


def to_bool(p: float) -> bool | None:
    """>= 0.65 True, <= 0.35 False, anything between None (the rule then says Unknown)."""
    return True if p >= TRUE_AT else False if p <= FALSE_AT else None


def _noul(instructions: str, yes: str, no: str) -> dict:
    return {"type": "noul", "instructions": instructions, "criteria": {"true": yes, "false": no}}


def spec_product(led: Ledger, idx: int) -> str | None:
    """The product the buyer has confirmed at bot turn idx: the final one after the change turn, else the initial one.

    No fallback to the other segment: a missing product means no question, and the labels stay None.
    """
    changed = led.product_changed_at_turn
    return led.final_product if changed is not None and idx > changed else led.initial_product


def build_questions(call: CallRecord, led: Ledger) -> dict[str, dict]:
    """One yes/no question per label. Each names its turns by idx, so identical replies still differ."""
    text = {t.idx: t.text for t in call.turns}
    questions: dict[str, dict] = {}
    for b in led.bot_turns:
        product = spec_product(led, b.idx)
        if b.slot != "spec" or not product or b.idx not in text:
            continue
        head = f'A buyer wants the product "{product}". In `call.turns`, the sales bot turn with idx {b.idx} asked: '
        ask = f'"{text[b.idx]}".'
        if b.spec_name:  # the attribute alone: with the whole utterance Jev judged the options too (pilot, #113)
            spec = b.spec_name
            questions[f"q{b.idx}"] = _noul(
                f'A buyer wants to buy "{product}". In `call.turns`, the sales bot turn with idx {b.idx} asks the '
                f'buyer about the specification "{spec}". Is "{spec}" a property that a product like "{product}" '
                "can have, so the question makes sense?",
                f'"{spec}" is a sensible property of {product}.',
                f'"{spec}" belongs to a different kind of product, not {product}.',
            )
        else:
            questions[f"q{b.idx}"] = _noul(
                f"{head}{ask} Is the specification the bot asks about (ignore any example options) "
                f"one that makes sense for {product}?",
                f"The specification belongs to {product}.",
                "The specification belongs to a different kind of product.",
            )
        if b.options_offered:
            opts = ", ".join(b.options_offered)
            questions[f"o{b.idx}"] = _noul(
                f"{head}{ask} It offered these options: {opts}. "
                f"Do the question and all its options make sense for {product}?",
                f"The question and its options all fit {product}.",
                "The question or an option belongs to a different kind of product.",
            )
    confirmed, final = led.bot_confirmed.get("product"), led.final_product
    if confirmed and final:
        questions["pm"] = _noul(
            f'A sales bot confirmed the product "{confirmed}". The buyer finally wants "{final}". '
            "Is the product the bot confirmed the same product the buyer finally wants? Sharing a word is not enough.",
            "Same product.",
            "Different product.",
        )
    asked = {u.idx for u in led.user_turns}
    last_bot = None
    for t in call.turns:
        if t.speaker == "bot":
            last_bot = t
        elif t.idx in asked:
            who = f"In `call.turns`, the buyer turn with idx {t.idx} "
            if last_bot:
                who += f'replies to the sales bot turn with idx {last_bot.idx}. Bot: "{last_bot.text}". '
            else:
                who += "opens the call. "
            questions[f"f{t.idx}"] = _noul(
                f'{who}Buyer: "{t.text}". In this reply, is the buyer irritated or annoyed with the bot '
                "(for example complaining that it keeps asking, telling it to stop, or speaking angrily)? "
                'Short agreement words repeated, such as "haan haan" or "ji ji", and a plain answer are not '
                "irritation.",
                "The buyer is irritated or annoyed with the bot.",
                "The buyer is calm: agreeing, answering or asking normally.",
            )
    return questions


def judge_ledger(call: CallRecord, led: Ledger) -> Ledger:
    """Return a copy of led with Jev's labels in place of Gemini's. Every other field is unchanged."""
    questions = build_questions(call, led)
    ans = complete_jev(_state(call), questions, cache_tag=JEV_VERSION) if questions else {}
    bots = []
    for b in led.bot_turns:
        if b.slot != "spec":
            bots.append(b)
            continue
        q = ans.get(f"q{b.idx}")
        o = ans.get(f"o{b.idx}", q)  # no options offered: the question alone is the whole turn
        if q is None or to_bool(q) is None:
            o = None  # unsure or missing whether the question fits: the turn is not judged, never (None, True)
        bots.append(
            b.model_copy(
                update={
                    "question_fits_product": None if q is None else to_bool(q),
                    "fits_confirmed_product": None if o is None else to_bool(o),
                }
            )
        )
    users = [u.model_copy(update={"frustrated": ans.get(f"f{u.idx}", 0.0)}) for u in led.user_turns]
    pm = ans.get("pm")
    return led.model_copy(
        update={"bot_turns": bots, "user_turns": users, "product_match": None if pm is None else to_bool(pm)}
    )


def fingerprint(call: CallRecord, led: Ledger) -> str:
    """sha256 of what a judged row depends on: the state, the questions, the model, JEV_VERSION and the source
    ledger (the row copies its structural fields, so a changed Gemini field must redo the row)."""
    raw = json.dumps(
        [_state(call), build_questions(call, led), jev_model(), JEV_VERSION, led.model_dump(mode="json")],
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _state(call: CallRecord) -> dict:
    """The call as named JSON turns (TypeSafe docs, State: an object keeps each part named and related)."""
    who = {"bot": "sales bot", "user": "buyer"}
    return {
        "call": {
            "turns": [{"idx": t.idx, "speaker": who.get(t.speaker, t.speaker), "text": t.text} for t in call.turns]
        }
    }


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
