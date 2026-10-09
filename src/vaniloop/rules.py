"""AUDIT step 2: apply the VANI QA rubric as plain Python rules over the ledger + audio signals.

Each rule maps to a row of data/reference/vani_quality_matrix.png. Every finding carries evidence and
says how we know it: observed (audio/transcript), spoken_proxy (inferred from what the bot said back,
because the captured Buy Lead is not available), or unavailable.
"""

from __future__ import annotations

import re

from .models import AuditRecord, CallRecord, Evidence, Finding, Ledger

DEAD_AIR_SEC = 5.0  # matrix: dead air > 5 s
# TODO: matrix allows 8 s on noisy lines; noise detection is not built yet, so this is not used.
DEAD_AIR_NOISY_SEC = 8.0
LOOP_MAX_ASKS = 3  # default ask limit for slots without their own limit
# VANI prompt L69: product 4, quantity 3, each spec 2, name 3, city 3. The first ask past the limit is the onset.
SLOT_LIMITS = {"product": 4, "quantity": 3, "spec": 2, "name": 3, "city": 3}
RESET_SLOTS = ("product", "quantity", "spec")  # a product change (L83) restarts these allowances
LOOP_END_TURNS = 2  # call ending within this many turns of the first ask past the limit counts as a fatal loop
FRUSTRATED_P = 0.6
HUMAN_P = 0.6
SHORT_CALL_SEC = 10.0
MIN_COVERAGE = 0.8
LOOP_SLOTS = ("product", "quantity", "unit", "name", "city")
ENRICH_FORBIDDEN_SLOTS = ("quantity", "unit", "spec", "name", "city", "handoff")  # collecting for the new product
BL_SCOPES = ("bl_collection", "oncall_astbuy")
# Assumption: quantity is a defined field for every category in a BL call. If False, qty_probing is Unknown.
QTY_ALWAYS_DEFINED = True
QTY_MINOR_ABS = 20.0  # matrix minor band: abs diff <= 20 AND relative diff <= 20%
QTY_MINOR_REL = 0.20
# Unknown on these parameters never forces NeedsReview (audio can be absent).
UNKNOWN_EXEMPT = ("dead_air",)


def _ev(call: CallRecord, idx: int) -> Evidence:
    """Evidence for turn idx, resolved by the turn's idx field (not list position). Never raises."""
    for t in call.turns:
        if t.idx == idx:
            return Evidence(turn_idx=idx, start_sec=t.start_sec, end_sec=t.end_sec, quote=t.text[:160])
    return Evidence(turn_idx=idx)


# ---------------------------------------------------------------- quantity parsing
_ONES = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
    # Hindi / Hinglish
    "ek": 1,
    "do": 2,
    "teen": 3,
    "char": 4,
    "chaar": 4,
    "paanch": 5,
    "panch": 5,
    "chhe": 6,
    "chhah": 6,
    "che": 6,
    "chah": 6,
    "saat": 7,
    "aath": 8,
    "nau": 9,
    "das": 10,
    "gyarah": 11,
    "barah": 12,
    "terah": 13,
    "chaudah": 14,
    "pandrah": 15,
    "solah": 16,
    "satrah": 17,
    "atharah": 18,
    "unnis": 19,
    "bees": 20,
    "pachis": 25,
    "pachchis": 25,
    "tees": 30,
    "chalis": 40,
    "pachaas": 50,
    "pachas": 50,
    "saath": 60,
    "sattar": 70,
    "assi": 80,
    "nabbe": 90,
    "dedh": 1.5,
    "dhai": 2.5,
    "adhai": 2.5,
}
_MULT = {
    "hundred": 100,
    "sau": 100,
    "thousand": 1000,
    "hazaar": 1000,
    "hazar": 1000,
    "hajar": 1000,
    "lakh": 100000,
    "lac": 100000,
}
_TOKEN = re.compile(r"\d[\d,]*\.?\d*|\.\d+|[a-z]+|[-\u2013]")
_RANGE_SEP = {"to", "se", "-", "\u2013"}

# unit token -> (group, canonical name, factor to the group's base unit)
_UNITS: dict[str, tuple[str, str, float]] = {}


def _add_units(group: str, canon: str, factor: float, *names: str) -> None:
    for n in (canon, *names):
        _UNITS[n] = (group, canon, factor)


_add_units("mass", "g", 1, "gm", "gram", "grams", "gms")
_add_units("mass", "kg", 1000, "kgs", "kilo", "kilos", "kilogram", "kilograms")
_add_units("mass", "quintal", 100000, "quintals")
_add_units("mass", "ton", 1000000, "tons", "tonne", "tonnes")
_add_units("count", "piece", 1, "pieces", "pc", "pcs", "nos", "number", "numbers", "unit", "units", "set", "sets")
_add_units("count", "pair", 2, "pairs")
_add_units("count", "dozen", 12, "dozens")
_add_units("length", "mm", 1, "millimeter", "millimetre")
_add_units("length", "cm", 10, "centimeter", "centimetre", "centimeters", "centimetres")
_add_units("length", "m", 1000, "meter", "metre", "meters", "metres", "mtr")
_add_units("length", "ft", 304.8, "feet", "foot")
_add_units("length", "inch", 25.4, "inches")
_add_units("volume", "ml", 1, "milliliter", "millilitre")
_add_units("volume", "l", 1000, "litre", "liter", "litres", "liters", "ltr")


def _number_at(toks: list[str], i: int, skip_junk: bool = True) -> tuple[float, int] | None:
    """Parse a number starting at toks[i]. Returns (value, index of the first token after it)."""
    total = current = 0.0
    started = False
    prev_digit = False
    while i < len(toks):
        tok = toks[i]
        if tok[0].isdigit() or tok[0] == ".":
            if started and (prev_digit or current):
                break  # a second separate number: stop at the first
            try:
                val = float(tok.replace(",", ""))
            except ValueError:
                i += 1
                continue
            current += val
            started = prev_digit = True
        elif tok in _ONES:
            current += _ONES[tok]
            started, prev_digit = True, False
        elif tok in _MULT:
            if not started:
                i += 1
                continue
            m = _MULT[tok]
            if m == 100:
                current = (current or 1) * 100
            else:
                total += (current or 1) * m
                current = 0.0
            prev_digit = False
        elif tok == "and" and started:
            pass
        elif started:
            break
        elif not skip_junk:
            return None
        i += 1
    return (total + current, i) if started else None


def parse_quantity(text: str | None) -> float | None:
    """Parse the first quantity in text: digits, decimals, English or Hindi/Hinglish number words."""
    if not text:
        return None
    r = _number_at(_TOKEN.findall(str(text).lower()), 0)
    return r[0] if r else None


def norm_unit(text: str | None) -> str | None:
    """Canonical unit name for a unit word (first token that is a known unit), else None."""
    for tok in _TOKEN.findall(str(text or "").lower()):
        if tok in _UNITS:
            return _UNITS[tok][1]
    return None


def unit_group(unit: str | None) -> str | None:
    return _UNITS[unit][0] if unit in _UNITS else None


def to_base(value: float, unit: str | None) -> float:
    return value * _UNITS[unit][2] if unit in _UNITS else value


def parse_quantity_full(text: str | None) -> tuple[float, float, str | None] | None:
    """(low, high, unit|None). A range "2-3", "2 se 3" or "2 to 3" gives low=2, high=3; a single value low=high.
    The unit is the canonical name of the unit word right after a number (None if absent). None if no number."""
    if not text:
        return None
    toks = _TOKEN.findall(str(text).lower())
    r = _number_at(toks, 0)
    if not r:
        return None
    low, i = r
    high, unit = low, None
    if i < len(toks) and toks[i] in _UNITS:
        unit, i = _UNITS[toks[i]][1], i + 1
    if i < len(toks) and toks[i] in _RANGE_SEP:
        r2 = _number_at(toks, i + 1, skip_junk=False)
        if r2:
            high, j = r2
            if j < len(toks) and toks[j] in _UNITS and unit is None:
                unit = _UNITS[toks[j]][1]
            low, high = min(low, high), max(low, high)
    return low, high, unit


def _fmt_pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def rule_dead_air(call: CallRecord, led: Ledger) -> Finding:
    gaps = [s for s in call.silences if s.duration > DEAD_AIR_SEC]
    if not call.audio_path:
        return Finding(
            parameter="dead_air", status="Unknown", reason="no audio to measure silence", basis="unavailable"
        )
    if not gaps:
        return Finding(
            parameter="dead_air", status="Pass", reason=f"no silence over {DEAD_AIR_SEC:.0f} s", basis="observed"
        )
    worst = max(gaps, key=lambda s: s.duration)
    return Finding(
        parameter="dead_air",
        status="NonFatal",
        basis="observed",
        reason=f"{len(gaps)} silence(s) over {DEAD_AIR_SEC:.0f} s, longest {worst.duration:.1f} s",
        evidence=[Evidence(start_sec=s.start_sec, end_sec=s.end_sec) for s in gaps],
    )


def _end_of_call_within(call: CallRecord, idx: int, n: int) -> bool:
    return sum(1 for t in call.turns if t.idx > idx) <= n


def _limit(key: str) -> int:
    return (
        SLOT_LIMITS["spec" if key.startswith("spec:") else key]
        if (key.startswith("spec:") or key in SLOT_LIMITS)
        else LOOP_MAX_ASKS
    )


def rule_looping(call: CallRecord, led: Ledger) -> Finding:
    """Per-slot ask limits (VANI prompt L69). A product change (L83) starts a fresh allowance for product, quantity
    and each spec: asks before the change and asks after it are counted separately. Name and city are never reset."""
    change = led.product_changed_at_turn
    asks: dict[tuple[str, int], list[int]] = {}
    unnamed_specs: dict[int, list[int]] = {}  # segment -> idx of spec asks with no spec name
    for b in sorted(led.bot_turns, key=lambda b: b.idx):
        if b.slot in LOOP_SLOTS:
            key = b.slot
        elif b.slot == "spec" and b.spec_name:
            key = f"spec:{b.spec_name.strip().lower()}"
        elif b.slot == "spec":
            key = None
        else:
            continue
        seg = 1 if change is not None and b.idx > change and (b.slot in RESET_SLOTS) else 0
        if key is None:
            unnamed_specs.setdefault(seg, []).append(b.idx)
            continue
        asks.setdefault((key, seg), []).append(b.idx)
    results = []  # (fatal, n_asks, key, idxs, why, limit)
    for (key, _seg), idxs in asks.items():
        limit = _limit(key)
        if len(idxs) <= limit:
            continue
        over = idxs[limit]
        irritated = any(u.frustrated >= FRUSTRATED_P for u in led.user_turns if u.idx > over)
        ends = _end_of_call_within(call, over, LOOP_END_TURNS)
        why = (
            "buyer irritated after the ask past the limit"
            if irritated
            else "call ended right after the ask past the limit"
            if ends
            else "no sign of irritation"
        )
        results.append((irritated or ends, len(idxs), key, idxs, f"limit {limit}; {why}", limit))
    if not results:
        spec_limit = SLOT_LIMITS["spec"]
        for seg, idxs in sorted(unnamed_specs.items()):
            named_max = max((len(v) for (k, sg), v in asks.items() if k.startswith("spec:") and sg == seg), default=0)
            if len(idxs) + named_max > spec_limit:
                return Finding(
                    parameter="looping",
                    status="Unknown",
                    basis="observed",
                    reason=f"{len(idxs)} spec question(s) without a spec name (plus {named_max} for the most asked "
                    f"named spec) could pass the limit of {spec_limit}; repeats cannot be told apart",
                    evidence=[_ev(call, i) for i in idxs],
                )
        return Finding(
            parameter="looping",
            status="Pass",
            basis="observed",
            reason="no slot asked more than its limit (product 4, quantity 3, each spec 2, other 3)",
        )
    fatal, n, key, idxs, why, _lim = max(results, key=lambda r: (r[0], r[1]))
    label = key.replace("spec:", "spec '") + ("'" if key.startswith("spec:") else "")
    others = f"; {len(results) - 1} other looping slot(s)" if len(results) > 1 else ""
    return Finding(
        parameter="looping",
        status="Fatal" if fatal else "NonFatal",
        basis="observed",
        reason=f"bot asked for {label} {n} times ({why}){others}",
        evidence=[_ev(call, i) for i in idxs],
        onset_turn=min(r[3][r[5]] for r in results),
    )


def rule_category_isq(call: CallRecord, led: Ledger) -> Finding:
    specs = [b for b in led.bot_turns if b.slot == "spec"]
    if not specs:
        return Finding(parameter="category_isq", status="NotApplicable", reason="no spec questions", basis="observed")
    bad = sorted((b for b in specs if b.fits_confirmed_product is False), key=lambda b: b.idx)
    if bad:
        return Finding(
            parameter="category_isq",
            status="Fatal",
            basis="spoken_proxy",
            reason=f"{len(bad)} spec question(s) belong to another category than '{led.final_product}'",
            evidence=[_ev(call, b.idx) for b in bad],
            onset_turn=bad[0].idx,
        )
    unknown = [b for b in specs if b.fits_confirmed_product is None]
    if unknown:
        return Finding(
            parameter="category_isq",
            status="Unknown",
            basis="spoken_proxy",
            reason=f"{len(unknown)} spec question(s) not labelled for fit",
            evidence=[_ev(call, b.idx) for b in unknown],
        )
    return Finding(
        parameter="category_isq", status="Pass", reason="spec questions fit the product", basis="spoken_proxy"
    )


def _handoff_turn(led: Ledger) -> int | None:
    h = sorted(b.idx for b in led.bot_turns if b.slot == "handoff")
    return h[0] if h else None


def rule_qty_probing(call: CallRecord, led: Ledger) -> Finding:
    if led.scope not in BL_SCOPES:
        return Finding(
            parameter="qty_probing", status="NotApplicable", reason=f"scope is {led.scope}", basis="observed"
        )
    asked = sorted(b.idx for b in led.bot_turns if b.slot == "quantity")
    volunteered = "quantity" in led.buyer_facts
    if asked or volunteered:
        return Finding(
            parameter="qty_probing",
            status="Pass",
            reason="quantity asked or given",
            basis="observed",
            evidence=[_ev(call, asked[0])] if asked else [],
        )
    note = "(assumes quantity is a defined field for this category)"
    if not QTY_ALWAYS_DEFINED:
        return Finding(
            parameter="qty_probing",
            status="Unknown",
            basis="spoken_proxy",
            reason=f"bot never asked for quantity; cannot tell if it is defined for this category {note}",
        )
    # NonFatal, not Fatal: IndiaMART's human audit (VANI Call Vital report, 23 Aug-26 Sep 2026) grades
    # "Qty Probe" mostly Non-Fatal, and fails it only when quantity is defined on the category, which a
    # transcript cannot show. The Fatal parameters are wrong or missed captured values (qty_missed).
    return Finding(
        parameter="qty_probing",
        status="NonFatal",
        basis="spoken_proxy",
        onset_turn=_handoff_turn(led),
        reason=f"bot never asked for quantity and the buyer did not give one {note}",
    )


def rule_qty_missed(call: CallRecord, led: Ledger) -> Finding:
    """VANI prompt L1261: a valid quantity is stored and the bot moves on WITHOUT a read-back, so a missing read-back
    is not a fault. It is a miss only if the bot asked for quantity again after the buyer gave it (ignored)."""
    said, kept = led.buyer_facts.get("quantity"), led.bot_confirmed.get("quantity")
    if led.scope not in BL_SCOPES or not said:
        return Finding(
            parameter="qty_missed", status="NotApplicable", reason="buyer gave no quantity", basis="observed"
        )
    gave = sorted(u.idx for u in led.user_turns if u.gave_value_for == "quantity")
    if kept and not gave:
        return Finding(parameter="qty_missed", status="Pass", reason="a quantity was captured", basis="spoken_proxy")
    if not gave:
        return Finding(
            parameter="qty_missed",
            status="Unknown",
            basis="spoken_proxy",
            reason=f"buyer said quantity '{said}' but no turn is labelled as giving it; cannot tell if it was captured",
        )
    change = led.product_changed_at_turn

    def seg(i: int) -> int:  # a product change starts a new quantity (L83): compare only inside one segment
        return 1 if change is not None and i > change else 0

    reasked = []
    for sg in (0, 1):
        first = [g for g in gave if seg(g) == sg]
        if first:
            reasked += sorted(
                b.idx for b in led.bot_turns if b.slot == "quantity" and seg(b.idx) == sg and b.idx > first[0]
            )
    reasked.sort()
    if reasked:
        return Finding(
            parameter="qty_missed",
            status="Fatal",
            basis="spoken_proxy",
            onset_turn=reasked[0],
            reason=f"buyer said quantity '{said}' but the bot asked for it again (ignored)",
            evidence=[_ev(call, reasked[0])],
        )
    return Finding(
        parameter="qty_missed",
        status="Pass",
        basis="spoken_proxy",
        reason="a quantity was captured and the bot did not ask again"
        if kept
        else "buyer gave a quantity and the bot moved on (no read-back needed)",
    )


def rule_qty_accuracy(call: CallRecord, led: Ledger) -> Finding:
    said, kept = led.buyer_facts.get("quantity"), led.bot_confirmed.get("quantity")
    if led.scope not in BL_SCOPES or not said or not kept:
        return Finding(
            parameter="qty_accuracy",
            status="NotApplicable",
            basis="observed",
            reason="needs both a buyer quantity and a bot-confirmed quantity",
        )
    pa, pb = parse_quantity_full(said), parse_quantity_full(kept)
    if pa is None or pb is None:
        return Finding(
            parameter="qty_accuracy",
            status="Unknown",
            basis="spoken_proxy",
            reason=f"cannot parse quantity (buyer '{said}', bot '{kept}')",
        )
    (alo, ahi, ua), (blo, bhi, ub) = pa, pb
    ua = ua or norm_unit((led.buyer_facts or {}).get("unit"))
    ub = ub or norm_unit((led.bot_confirmed or {}).get("unit"))
    ga, gb = unit_group(ua), unit_group(ub)
    if ga and gb and ga != gb:
        return Finding(
            parameter="qty_accuracy",
            status="Unknown",
            basis="spoken_proxy",
            reason=f"units differ in kind (buyer '{ua}', bot '{ub}'); see qty_unit",
        )
    if ua and ub:  # same group: convert the bot value into the buyer's unit, so the tolerance applies in that unit
        k = to_base(1, ub) / to_base(1, ua)
        blo, bhi = blo * k, bhi * k
    onset = _handoff_turn(led)
    if (alo, ahi) == (blo, bhi):
        return Finding(parameter="qty_accuracy", status="Pass", reason="quantities match", basis="spoken_proxy")
    if blo == bhi == ahi and alo < ahi:  # VANI prompt L1255: a range is stored as its maximum
        return Finding(
            parameter="qty_accuracy",
            status="Pass",
            basis="spoken_proxy",
            reason=f"range captured as its maximum (buyer said '{said}', bot kept '{kept}')",
        )
    if blo == bhi and alo < ahi and alo <= blo <= ahi:
        return Finding(
            parameter="qty_accuracy",
            status="NonFatal",
            basis="spoken_proxy",
            onset_turn=onset,
            reason=f"range collapsed to one value (buyer said '{said}', bot confirmed '{kept}')",
        )
    if blo != bhi and (blo <= ahi and alo <= bhi):  # bot range overlaps the buyer's but is not the same
        return Finding(
            parameter="qty_accuracy",
            status="NonFatal",
            basis="spoken_proxy",
            onset_turn=onset,
            reason=f"ranges differ (buyer '{said}', bot '{kept}')",
        )
    # nearest endpoint of the buyer's range to the bot's value (or nearest bot endpoint for a bot range)
    if blo == bhi:
        a, b = (alo if blo < alo else ahi), blo
    else:
        a, b = (alo, bhi) if bhi < alo else (ahi, blo)
    diff = abs(a - b)
    rel = diff / abs(a) if a else float("inf")
    minor = diff <= QTY_MINOR_ABS and rel <= QTY_MINOR_REL
    return Finding(
        parameter="qty_accuracy",
        status="NonFatal" if minor else "Fatal",
        basis="spoken_proxy",
        reason=f"buyer said '{said}' ({a:g}), bot confirmed '{kept}' ({b:g}); "
        f"diff {diff:g} ({_fmt_pct(rel) if rel != float('inf') else 'n/a'})",
        onset_turn=onset,
    )


def rule_qty_unit(call: CallRecord, led: Ledger) -> Finding:
    said, kept = led.buyer_facts.get("quantity"), led.bot_confirmed.get("quantity")
    if led.scope not in BL_SCOPES or not said or not kept:
        return Finding(
            parameter="qty_unit",
            status="NotApplicable",
            basis="observed",
            reason="needs both a buyer quantity and a bot-confirmed quantity",
        )
    pa, pb = parse_quantity_full(said), parse_quantity_full(kept)
    ua = (pa[2] if pa else None) or norm_unit(led.buyer_facts.get("unit"))
    ub = (pb[2] if pb else None) or norm_unit(led.bot_confirmed.get("unit"))
    if ua is None and ub is None:
        return Finding(
            parameter="qty_unit", status="NotApplicable", basis="spoken_proxy", reason="no unit on either side"
        )
    if ua is None or ub is None:
        return Finding(
            parameter="qty_unit",
            status="Pass",
            basis="spoken_proxy",
            reason=f"only one side states a unit (buyer '{ua}', bot '{ub}')",
        )
    if unit_group(ua) != unit_group(ub):
        return Finding(
            parameter="qty_unit",
            status="Fatal",
            basis="spoken_proxy",
            onset_turn=_handoff_turn(led),
            reason=f"buyer unit '{ua}' ({unit_group(ua)}) but bot confirmed '{ub}' ({unit_group(ub)})",
        )
    return Finding(
        parameter="qty_unit",
        status="Pass",
        basis="spoken_proxy",
        reason="units are the same kind; values are checked by qty_accuracy",
    )


def _product_confirmed(led: Ledger) -> bool:
    """The bot confirmed a product: bot_confirmed has it, or the last bot product/confirm turn is answered by the buyer,
    the buyer gives no later product answer, and the buyer did not change the product at or after that turn."""
    if led.bot_confirmed.get("product"):
        return True
    handoff = _handoff_turn(led)  # evidence after the first handoff turn does not count
    before = lambda i: handoff is None or i < handoff  # noqa: E731
    asks = sorted(b.idx for b in led.bot_turns if b.slot in ("product", "confirm") and before(b.idx))
    if not asks:
        return False
    last = asks[-1]
    answers = sorted(u.idx for u in led.user_turns if u.gave_value_for == "product" and last < u.idx and before(u.idx))
    if len(answers) != 1:
        return False
    change = led.product_changed_at_turn
    return change is None or change < last


def _enrich_deflection(call: CallRecord, led: Ledger) -> bool | None:
    """L597-603 (enrich variant): after the product change the bot may ask whether the old product is still needed
    (product/confirm turns) but collects nothing for the new one, then closes: its LAST turn is labelled "other" and
    the call ends within 3 turns of the change. True = deflection, False = not, None = clarified but no close seen."""
    c = led.product_changed_at_turn
    later = [b for b in led.bot_turns if b.idx > c]
    if any(b.slot in ENRICH_FORBIDDEN_SLOTS for b in later):
        return False
    bot_idxs = [t.idx for t in call.turns if t.speaker == "bot"]
    last = max(bot_idxs, default=None)
    closing = last is not None and last > c and any(b.idx == last and b.slot == "other" for b in later)
    if closing and _end_of_call_within(call, c, 3):
        return True
    return None if any(b.slot in ("product", "confirm") for b in later) and not closing else False


def rule_product_accuracy(call: CallRecord, led: Ledger) -> Finding:
    """Parameter id stays "product_change" (dashboard and issue names depend on it)."""
    kept = led.bot_confirmed.get("product")
    ev = [_ev(call, led.product_changed_at_turn)] if led.product_changed_at_turn is not None else []
    if led.scope == "seller_unavailable_handoff":
        # L1175, L1701: a handoff needs a confirmed product first. Pass only with evidence of the confirmation.
        if led.product_match is False:
            return Finding(
                parameter="product_change",
                status="NonFatal",
                basis="spoken_proxy",
                evidence=ev,
                onset_turn=_handoff_turn(led),
                reason=f"wrong product handed off (buyer wants '{led.final_product}', bot confirmed '{kept}')",
            )
        if not _product_confirmed(led):
            return Finding(
                parameter="product_change",
                status="Unknown",
                basis="spoken_proxy",
                evidence=ev,
                onset_turn=_handoff_turn(led),
                reason="handoff call with no evidence the bot confirmed a product",
            )
        return Finding(
            parameter="product_change",
            status="Pass",
            basis="spoken_proxy",
            evidence=ev,
            reason="handoff call with a confirmed product",
        )
    if led.scope not in BL_SCOPES:
        return Finding(
            parameter="product_change", status="NotApplicable", reason=f"scope is {led.scope}", basis="observed"
        )
    if led.variant == "enrich" and led.product_changed_at_turn is not None:
        collected = sorted(
            b.idx for b in led.bot_turns if b.idx > led.product_changed_at_turn and b.slot in ENRICH_FORBIDDEN_SLOTS
        )
        if collected:  # L599-603: the enrich bot must not collect for the new product
            return Finding(
                parameter="product_change",
                status="NonFatal",
                basis="spoken_proxy",
                evidence=ev,
                onset_turn=collected[0],
                reason="enrich variant: bot collected requirements for the new product after the change",
            )
        d = _enrich_deflection(call, led)
        if d:
            return Finding(
                parameter="product_change",
                status="Pass",
                basis="spoken_proxy",
                evidence=ev,
                reason="enrich variant: buyer wanted another product and the bot closed the call without collecting it",
            )
        if d is None:
            return Finding(
                parameter="product_change",
                status="Unknown",
                basis="spoken_proxy",
                evidence=ev,
                reason="enrich variant: bot clarified the product change but the call shows no closing turn",
            )
    if not kept:
        return Finding(
            parameter="product_change",
            status="Unknown",
            basis="spoken_proxy",
            evidence=ev,
            reason="bot never confirmed a product",
        )
    if led.product_match is None:
        return Finding(
            parameter="product_change",
            status="Unknown",
            basis="spoken_proxy",
            evidence=ev,
            reason="no judgement whether the confirmed product matches what the buyer wants",
        )
    if led.product_match is False:
        after = sorted(
            b.idx
            for b in led.bot_turns
            if led.product_changed_at_turn is not None and b.idx > led.product_changed_at_turn
        )
        confirm = sorted(b.idx for b in led.bot_turns if b.slot == "confirm")
        onset = after[0] if after else confirm[0] if confirm else None
        return Finding(
            parameter="product_change",
            status="Fatal",
            basis="spoken_proxy",
            evidence=ev,
            onset_turn=onset,
            reason=f"buyer wants '{led.final_product}' but bot confirmed '{kept}'",
        )
    return Finding(
        parameter="product_change",
        status="Pass",
        basis="spoken_proxy",
        evidence=ev,
        reason=f"bot confirmed the product the buyer wants ('{led.final_product}')",
    )


RULES = [
    rule_category_isq,
    rule_product_accuracy,
    rule_qty_probing,
    rule_qty_missed,
    rule_qty_accuracy,
    rule_qty_unit,
    rule_looping,
    rule_dead_air,
]


def label_coverage(call: CallRecord, led: Ledger) -> tuple[float, float]:
    """(bot coverage, user coverage): labelled turns of the right speaker / turns of that speaker."""
    bots = {t.idx for t in call.turns if t.speaker == "bot"}
    users = {t.idx for t in call.turns if t.speaker == "user"}
    lb = {b.idx for b in led.bot_turns} & bots
    lu = {u.idx for u in led.user_turns} & users
    return (len(lb) / len(bots) if bots else 1.0, len(lu) / len(users) if users else 1.0)


def unlabelled_turns(call: CallRecord, led: Ledger) -> list[int]:
    """Turn idx values of bot and user turns that have no label of the right kind."""
    lb = {b.idx for b in led.bot_turns}
    lu = {u.idx for u in led.user_turns}
    return sorted(t.idx for t in call.turns if t.idx not in (lb if t.speaker == "bot" else lu))


def audit_call(call: CallRecord, led: Ledger) -> AuditRecord:
    if call.duration_sec < SHORT_CALL_SEC or not any(t.speaker == "user" for t in call.turns):
        return AuditRecord(
            call_id=call.call_id,
            verdict="NeedsReview",
            scope=led.scope,
            findings=[],
            flags=["low_confidence"],
            review_reason="call too short or buyer never spoke",
        )
    cov = min(label_coverage(call, led))
    scope_missing = led.scope not in ("bl_collection", "seller_unavailable_handoff", "oncall_astbuy", "refusal")
    if cov < MIN_COVERAGE or scope_missing:
        return AuditRecord(
            call_id=call.call_id,
            verdict="NeedsReview",
            scope=led.scope,
            findings=[],
            flags=["low_confidence"],
            review_reason=f"incomplete ledger (coverage {cov * 100:.0f}%)"
            + ("; scope missing" if scope_missing else ""),
        )
    missing = unlabelled_turns(call, led)
    findings = [r(call, led) for r in RULES]
    flags: list = []
    if any(u.frustrated >= FRUSTRATED_P for u in led.user_turns):
        flags.append("frustration")
    if any(u.asks_for_human >= HUMAN_P for u in led.user_turns):
        flags.append("human_request")
    if any(f.parameter == "looping" and f.status in ("Fatal", "NonFatal") for f in findings):
        flags.append("loops")
    if any(
        f.parameter in ("product_change", "qty_missed", "qty_accuracy", "qty_unit", "category_isq")
        and f.status == "Fatal"
        for f in findings
    ):
        flags.append("wrong_requirement")
    statuses = {f.status for f in findings}
    unknown = [f.parameter for f in findings if f.status == "Unknown" and f.parameter not in UNKNOWN_EXEMPT]
    review = None
    if "Fatal" in statuses:
        verdict = "Fatal"
    elif "NonFatal" in statuses:
        verdict = "NonFatal"
    elif unknown:
        verdict = "NeedsReview"
        review = "unknown on: " + ", ".join(unknown) + (f"; unlabelled turns: {missing}" if missing else "")
        flags.append("low_confidence")
    elif missing:
        verdict = "NeedsReview"  # Pass needs 100% label coverage
        review = f"unlabelled turns: {missing}"
        flags.append("low_confidence")
    else:
        verdict = "Pass"
    onsets = [f.onset_turn for f in findings if f.status in ("Fatal", "NonFatal") and f.onset_turn is not None]
    return AuditRecord(
        call_id=call.call_id,
        verdict=verdict,
        scope=led.scope,
        findings=findings,
        flags=flags,
        first_fault_turn=min(onsets) if onsets else None,
        review_reason=review,
    )
