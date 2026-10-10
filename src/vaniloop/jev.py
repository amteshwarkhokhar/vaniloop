"""AUDIT step 1b: Jev (TypeSafe) makes the judgement labels, Gemini keeps the structure.

Jev owns question_fits_product, fits_confirmed_product, product_match and frustrated. Every other ledger
field stays as Gemini extracted it, and the Python rules do not change. One focused request per judgement (a spec
turn, a buyer turn, the product match): the state holds only the fields that judgement needs, never the call.
"""

from __future__ import annotations

import hashlib
import json
import math

from . import rules
from .llm import complete_jev, jev_model
from .models import CallRecord, Ledger

JEV_VERSION = "4"  # bump whenever the questions or the thresholds change; part of the cache key
# Thresholds per label, tuned on the dev half of the Fable-reviewed set and checked on the held-out half (#121).
Q_TRUE_AT, Q_FALSE_AT = 0.55, 0.20  # question_fits_product
OPT_TRUE_AT, OPT_FALSE_AT = 0.50, 0.20  # one option of a spec turn
N_DROP_AT = 0.35  # name check: q is dropped when n <= this
PM_TRUE_AT, PM_FALSE_AT = 0.65, 0.35  # product match


def __getattr__(name: str):
    """jev.FRUSTRATED_P stays readable; it is rules.FRUSTRATED_P, read live so a rubric override shows."""
    if name == "FRUSTRATED_P":
        return rules.FRUSTRATED_P
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def to_bool(p: float, true_at: float | None = None, false_at: float | None = None) -> bool | None:
    """>= true_at True, <= false_at False, anything between None (the rule says Unknown). Default: product match.

    The defaults are read at call time, so a rubric override of PM_TRUE_AT or PM_FALSE_AT applies.
    """
    true_at = PM_TRUE_AT if true_at is None else true_at
    false_at = PM_FALSE_AT if false_at is None else false_at
    return True if p >= true_at else False if p <= false_at else None


def _noul(instructions: str, yes, no) -> dict:
    return {"type": "noul", "instructions": instructions, "criteria": {"true": yes, "false": no}}


def _criterion(c: dict):
    """A criterion with examples is sent as {description, examples}; one without is sent as its description."""
    return {"description": c["description"], "examples": list(c["examples"])} if c["examples"] else c["description"]


def _question(qid: str, instructions: str | None = None) -> dict:
    """The Jev question of an editable id, built from the rubric (the applied override, else the default text)."""
    from . import rubric  # late: rubric reads this module's thresholds at import

    q = rubric.question(qid)
    return _noul(instructions or q["instructions"], _criterion(q["true"]), _criterion(q["false"]))


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
    from . import rubric

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
        q_text = rubric.question("spec_fit")["instructions"]
        if not spec:  # no spec name: point the question at the turn
            ref = "`specification_asked` (the specification asked in `bot_turn`)"
            q_text = q_text.replace("`specification_asked`", ref, 1)
        questions = {"q": _question("spec_fit", q_text)}
        if spec:  # spec_name is Gemini's: check it names what the turn really asks
            questions["n"] = _question("spec_name_check")
        opt_text = rubric.question("option_fit")["instructions"]
        for i in range(len(b.options_offered)):
            questions[f"opt{i}"] = _question("option_fit", opt_text.replace("{i}", str(i)))
        out.append(("spec", b.idx, state, questions))
    asked = {u.idx for u in led.user_turns}
    last_bot = ""
    for t in call.turns:
        if t.speaker == "bot":
            last_bot = t.text
        elif t.idx in asked:
            out.append(("buyer", t.idx, {"bot_turn": last_bot, "buyer_reply": t.text}, {"f": _question("irritation")}))
    confirmed, final = led.bot_confirmed.get("product"), led.final_product
    if confirmed and final:
        state = {"product_the_bot_confirmed": confirmed, "product_the_buyer_finally_wants": final}
        q = _question("product_match")
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
    """sha256 of what a judged row depends on: every request (state and questions), the model, JEV_VERSION plus
    the rubric's Jev overrides (nothing is added without overrides) and the source ledger (the row copies its
    structural fields, so a changed Gemini field must redo the row)."""
    from . import rubric

    raw = json.dumps(
        [build_questions(call, led), jev_model(), JEV_VERSION + rubric.version_suffix(), led.model_dump(mode="json")],
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
        out[("frustrated", u["idx"])] = v >= rules.FRUSTRATED_P if ok else None  # missing stays None, never False
    return out


def judge_agreement(a_rows: list[dict], b_rows: list[dict]) -> list[dict]:
    """Per field: how often ledger files A (Gemini) and B (Jev) agree, and Cohen's kappa.

    Only labels that both files hold (not None) count. `none_a` and `none_b` count the labels dropped.
    frustrated is compared as `>= rules.FRUSTRATED_P`.
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
            name = k[0] if k[0] != "frustrated" else f"frustrated>={rules.FRUSTRATED_P}"
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
