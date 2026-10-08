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
LOOP_MAX_ASKS = 3  # matrix: "probing more than 1+2 times" -> 4th ask of one slot is a loop
LOOP_END_TURNS = 2  # call ending within this many turns of the 4th ask counts as a fatal loop
FRUSTRATED_P = 0.6
HUMAN_P = 0.6
SHORT_CALL_SEC = 10.0
MIN_COVERAGE = 0.8
LOOP_SLOTS = ("product", "quantity", "unit", "name", "city")
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
_ONES = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
         "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
         "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30,
         "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
         # Hindi / Hinglish
         "ek": 1, "do": 2, "teen": 3, "char": 4, "chaar": 4, "paanch": 5, "panch": 5, "chhe": 6, "chhah": 6,
         "che": 6, "chah": 6, "saat": 7, "aath": 8, "nau": 9, "das": 10, "gyarah": 11, "barah": 12,
         "terah": 13, "chaudah": 14, "pandrah": 15, "solah": 16, "satrah": 17, "atharah": 18, "unnis": 19,
         "bees": 20, "pachis": 25, "pachchis": 25, "tees": 30, "chalis": 40, "pachaas": 50, "pachas": 50,
         "saath": 60, "sattar": 70, "assi": 80, "nabbe": 90, "dedh": 1.5, "dhai": 2.5, "adhai": 2.5}
_MULT = {"hundred": 100, "sau": 100, "thousand": 1000, "hazaar": 1000, "hazar": 1000, "hajar": 1000,
         "lakh": 100000, "lac": 100000}
_TOKEN = re.compile(r"\d[\d,]*\.?\d*|\.\d+|[a-z]+")


def parse_quantity(text: str | None) -> float | None:
    """Parse the first quantity in text: digits, decimals, English or Hindi/Hinglish number words."""
    if not text:
        return None
    total = current = 0.0
    started = False
    prev_digit = False
    for tok in _TOKEN.findall(str(text).lower()):
        if tok[0].isdigit() or tok[0] == ".":
            if started and (prev_digit or current):
                break  # a second separate number: stop at the first
            try:
                val = float(tok.replace(",", ""))
            except ValueError:
                continue
            current += val
            started = prev_digit = True
        elif tok in _ONES:
            current += _ONES[tok]
            started, prev_digit = True, False
        elif tok in _MULT:
            if not started:
                continue
            m = _MULT[tok]
            if m == 100:
                current = (current or 1) * 100
            else:
                total += (current or 1) * m
                current = 0.0
            prev_digit = False
        elif tok == "and" and started:
            continue
        elif started:
            break
    return total + current if started else None


def _fmt_pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def rule_dead_air(call: CallRecord, led: Ledger) -> Finding:
    gaps = [s for s in call.silences if s.duration > DEAD_AIR_SEC]
    if not call.audio_path:
        return Finding(parameter="dead_air", status="Unknown", reason="no audio to measure silence", basis="unavailable")
    if not gaps:
        return Finding(parameter="dead_air", status="Pass", reason=f"no silence over {DEAD_AIR_SEC:.0f} s", basis="observed")
    worst = max(gaps, key=lambda s: s.duration)
    return Finding(parameter="dead_air", status="NonFatal", basis="observed",
                   reason=f"{len(gaps)} silence(s) over {DEAD_AIR_SEC:.0f} s, longest {worst.duration:.1f} s",
                   evidence=[Evidence(start_sec=s.start_sec, end_sec=s.end_sec) for s in gaps])


def _end_of_call_within(call: CallRecord, idx: int, n: int) -> bool:
    return sum(1 for t in call.turns if t.idx > idx) <= n


def rule_looping(call: CallRecord, led: Ledger) -> Finding:
    asks: dict[str, list[int]] = {}
    unnamed_specs: list[int] = []
    for b in sorted(led.bot_turns, key=lambda b: b.idx):
        if b.slot in LOOP_SLOTS:
            asks.setdefault(b.slot, []).append(b.idx)
        elif b.slot == "spec":
            if b.spec_name:
                asks.setdefault(f"spec:{b.spec_name.strip().lower()}", []).append(b.idx)
            else:
                unnamed_specs.append(b.idx)
    results = []  # (fatal, n_asks, key, idxs, why)
    for key, idxs in asks.items():
        if len(idxs) <= LOOP_MAX_ASKS:
            continue
        fourth = idxs[LOOP_MAX_ASKS]
        irritated = any(u.frustrated >= FRUSTRATED_P for u in led.user_turns if u.idx > fourth)
        ends = _end_of_call_within(call, fourth, LOOP_END_TURNS)
        why = "buyer irritated after the 4th ask" if irritated else "call ended right after the 4th ask" if ends else "no sign of irritation"
        results.append((irritated or ends, len(idxs), key, idxs, why))
    if not results:
        if len(unnamed_specs) > LOOP_MAX_ASKS:
            return Finding(parameter="looping", status="Unknown", basis="observed",
                           reason=f"{len(unnamed_specs)} spec questions without a spec name; repeats cannot be told apart",
                           evidence=[_ev(call, i) for i in unnamed_specs])
        return Finding(parameter="looping", status="Pass", basis="observed",
                       reason=f"no slot asked more than {LOOP_MAX_ASKS} times")
    fatal, n, key, idxs, why = max(results, key=lambda r: (r[0], r[1]))
    label = key.replace("spec:", "spec '") + ("'" if key.startswith("spec:") else "")
    others = f"; {len(results) - 1} other looping slot(s)" if len(results) > 1 else ""
    return Finding(parameter="looping", status="Fatal" if fatal else "NonFatal", basis="observed",
                   reason=f"bot asked for {label} {n} times ({why}){others}",
                   evidence=[_ev(call, i) for i in idxs], onset_turn=idxs[LOOP_MAX_ASKS])


def rule_category_isq(call: CallRecord, led: Ledger) -> Finding:
    specs = [b for b in led.bot_turns if b.slot == "spec"]
    if not specs:
        return Finding(parameter="category_isq", status="NotApplicable", reason="no spec questions", basis="observed")
    bad = sorted((b for b in specs if b.fits_confirmed_product is False), key=lambda b: b.idx)
    if bad:
        return Finding(parameter="category_isq", status="Fatal", basis="spoken_proxy",
                       reason=f"{len(bad)} spec question(s) belong to another category than '{led.final_product}'",
                       evidence=[_ev(call, b.idx) for b in bad], onset_turn=bad[0].idx)
    unknown = [b for b in specs if b.fits_confirmed_product is None]
    if unknown:
        return Finding(parameter="category_isq", status="Unknown", basis="spoken_proxy",
                       reason=f"{len(unknown)} spec question(s) not labelled for fit",
                       evidence=[_ev(call, b.idx) for b in unknown])
    return Finding(parameter="category_isq", status="Pass", reason="spec questions fit the product", basis="spoken_proxy")


def _handoff_turn(led: Ledger) -> int | None:
    h = sorted(b.idx for b in led.bot_turns if b.slot == "handoff")
    return h[0] if h else None


def rule_qty_probing(call: CallRecord, led: Ledger) -> Finding:
    if led.scope not in BL_SCOPES:
        return Finding(parameter="qty_probing", status="NotApplicable", reason=f"scope is {led.scope}", basis="observed")
    asked = sorted(b.idx for b in led.bot_turns if b.slot == "quantity")
    volunteered = "quantity" in led.buyer_facts
    if asked or volunteered:
        return Finding(parameter="qty_probing", status="Pass", reason="quantity asked or given", basis="observed",
                       evidence=[_ev(call, asked[0])] if asked else [])
    note = "(assumes quantity is a defined field for this category)"
    if not QTY_ALWAYS_DEFINED:
        return Finding(parameter="qty_probing", status="Unknown", basis="spoken_proxy",
                       reason=f"bot never asked for quantity; cannot tell if it is defined for this category {note}")
    return Finding(parameter="qty_probing", status="Fatal", basis="spoken_proxy", onset_turn=_handoff_turn(led),
                   reason=f"bot never asked for quantity and the buyer did not give one {note}")


def rule_qty_missed(call: CallRecord, led: Ledger) -> Finding:
    said, kept = led.buyer_facts.get("quantity"), led.bot_confirmed.get("quantity")
    if led.scope not in BL_SCOPES or not said:
        return Finding(parameter="qty_missed", status="NotApplicable", reason="buyer gave no quantity", basis="observed")
    if not kept:
        return Finding(parameter="qty_missed", status="Fatal", basis="spoken_proxy", onset_turn=_handoff_turn(led),
                       reason=f"buyer said quantity '{said}' but the bot never confirmed it")
    return Finding(parameter="qty_missed", status="Pass", reason="a quantity was confirmed", basis="spoken_proxy")


def rule_qty_accuracy(call: CallRecord, led: Ledger) -> Finding:
    said, kept = led.buyer_facts.get("quantity"), led.bot_confirmed.get("quantity")
    if led.scope not in BL_SCOPES or not said or not kept:
        return Finding(parameter="qty_accuracy", status="NotApplicable", basis="observed",
                       reason="needs both a buyer quantity and a bot-confirmed quantity")
    a, b = parse_quantity(said), parse_quantity(kept)
    if a is None or b is None:
        return Finding(parameter="qty_accuracy", status="Unknown", basis="spoken_proxy",
                       reason=f"cannot parse quantity (buyer '{said}', bot '{kept}')")
    if a == b:
        return Finding(parameter="qty_accuracy", status="Pass", reason="quantities match", basis="spoken_proxy")
    diff = abs(a - b)
    rel = diff / abs(a) if a else float("inf")
    minor = diff <= QTY_MINOR_ABS and rel <= QTY_MINOR_REL
    return Finding(parameter="qty_accuracy", status="NonFatal" if minor else "Fatal", basis="spoken_proxy",
                   reason=f"buyer said '{said}' ({a:g}), bot confirmed '{kept}' ({b:g}); "
                          f"diff {diff:g} ({_fmt_pct(rel) if rel != float('inf') else 'n/a'})",
                   onset_turn=_handoff_turn(led))


def rule_product_accuracy(call: CallRecord, led: Ledger) -> Finding:
    """Parameter id stays "product_change" (dashboard and issue names depend on it)."""
    if led.scope not in BL_SCOPES:
        return Finding(parameter="product_change", status="NotApplicable", reason=f"scope is {led.scope}", basis="observed")
    kept = led.bot_confirmed.get("product")
    ev = [_ev(call, led.product_changed_at_turn)] if led.product_changed_at_turn is not None else []
    if not kept:
        return Finding(parameter="product_change", status="Unknown", basis="spoken_proxy", evidence=ev,
                       reason="bot never confirmed a product")
    if led.product_match is None:
        return Finding(parameter="product_change", status="Unknown", basis="spoken_proxy", evidence=ev,
                       reason="no judgement whether the confirmed product matches what the buyer wants")
    if led.product_match is False:
        after = sorted(b.idx for b in led.bot_turns
                       if led.product_changed_at_turn is not None and b.idx > led.product_changed_at_turn)
        confirm = sorted(b.idx for b in led.bot_turns if b.slot == "confirm")
        onset = after[0] if after else confirm[0] if confirm else None
        return Finding(parameter="product_change", status="Fatal", basis="spoken_proxy", evidence=ev, onset_turn=onset,
                       reason=f"buyer wants '{led.final_product}' but bot confirmed '{kept}'")
    return Finding(parameter="product_change", status="Pass", basis="spoken_proxy", evidence=ev,
                   reason=f"bot confirmed the product the buyer wants ('{led.final_product}')")


RULES = [rule_category_isq, rule_product_accuracy, rule_qty_probing, rule_qty_missed, rule_qty_accuracy,
         rule_looping, rule_dead_air]


def label_coverage(call: CallRecord, led: Ledger) -> tuple[float, float]:
    """(bot coverage, user coverage): labelled turns of the right speaker / turns of that speaker."""
    bots = {t.idx for t in call.turns if t.speaker == "bot"}
    users = {t.idx for t in call.turns if t.speaker == "user"}
    lb = {b.idx for b in led.bot_turns} & bots
    lu = {u.idx for u in led.user_turns} & users
    return (len(lb) / len(bots) if bots else 1.0, len(lu) / len(users) if users else 1.0)


def audit_call(call: CallRecord, led: Ledger) -> AuditRecord:
    if call.duration_sec < SHORT_CALL_SEC or not any(t.speaker == "user" for t in call.turns):
        return AuditRecord(call_id=call.call_id, verdict="NeedsReview", scope=led.scope, findings=[],
                           flags=["low_confidence"], review_reason="call too short or buyer never spoke")
    cov = min(label_coverage(call, led))
    scope_missing = led.scope not in ("bl_collection", "seller_unavailable_handoff", "oncall_astbuy", "refusal")
    if cov < MIN_COVERAGE or scope_missing:
        return AuditRecord(call_id=call.call_id, verdict="NeedsReview", scope=led.scope, findings=[],
                           flags=["low_confidence"],
                           review_reason=f"incomplete ledger (coverage {cov * 100:.0f}%)" + ("; scope missing" if scope_missing else ""))
    findings = [r(call, led) for r in RULES]
    flags: list = []
    if any(u.frustrated >= FRUSTRATED_P for u in led.user_turns):
        flags.append("frustration")
    if any(u.asks_for_human >= HUMAN_P for u in led.user_turns):
        flags.append("human_request")
    if any(f.parameter == "looping" and f.status in ("Fatal", "NonFatal") for f in findings):
        flags.append("loops")
    if any(f.parameter in ("product_change", "qty_missed", "qty_accuracy", "category_isq") and f.status == "Fatal" for f in findings):
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
        review = "unknown on: " + ", ".join(unknown)
        flags.append("low_confidence")
    else:
        verdict = "Pass"
    onsets = [f.onset_turn for f in findings if f.status in ("Fatal", "NonFatal") and f.onset_turn is not None]
    return AuditRecord(call_id=call.call_id, verdict=verdict, scope=led.scope, findings=findings, flags=flags,
                       first_fault_turn=min(onsets) if onsets else None, review_reason=review)
