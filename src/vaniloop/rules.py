"""AUDIT step 2: apply the VANI QA rubric as plain Python rules over the ledger + audio signals.

Each rule maps to a row of data/reference/vani_quality_matrix.png. Every finding carries evidence and
says how we know it: observed (audio/transcript), spoken_proxy (inferred from what the bot said back,
because the captured Buy Lead is not available), or unavailable.
"""

from __future__ import annotations

import re

from .models import AuditRecord, CallRecord, Evidence, Finding, Ledger

DEAD_AIR_SEC = 5.0  # matrix: dead air > 5 s (8 s on noisy lines; noise detection not built yet)
LOOP_MAX_ASKS = 3  # matrix: "probing more than 1+2 times" -> 4th ask of one slot is a loop
FRUSTRATED_P = 0.6
HUMAN_P = 0.6
SHORT_CALL_SEC = 10.0
LOOP_SLOTS = ("product", "quantity", "unit", "name", "city")
BL_SCOPES = ("bl_collection", "oncall_astbuy")


def _ev(call: CallRecord, idx: int) -> Evidence:
    t = call.turns[idx]
    return Evidence(turn_idx=idx, start_sec=t.start_sec, end_sec=t.end_sec, quote=t.text[:160])


def _digits(s: str | None) -> str:
    return "".join(re.findall(r"\d+", s or ""))


def _overlap(a: str | None, b: str | None) -> bool:
    wa = {w for w in re.findall(r"[a-z0-9]+", (a or "").lower()) if len(w) > 2}
    wb = {w for w in re.findall(r"[a-z0-9]+", (b or "").lower()) if len(w) > 2}
    return bool(wa & wb)


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


def rule_looping(call: CallRecord, led: Ledger) -> Finding:
    asks: dict[str, list[int]] = {}
    for b in led.bot_turns:
        if b.slot in LOOP_SLOTS:
            asks.setdefault(b.slot, []).append(b.idx)
    loops = {s: ix for s, ix in asks.items() if len(ix) > LOOP_MAX_ASKS}
    if not loops:
        return Finding(parameter="looping", status="Pass", basis="observed",
                       reason=f"no slot asked more than {LOOP_MAX_ASKS} times")
    slot, idxs = max(loops.items(), key=lambda kv: len(kv[1]))
    after = [u for u in led.user_turns if u.idx > idxs[0]]
    irritated = any(u.frustrated >= FRUSTRATED_P for u in after)
    status = "Fatal" if irritated else "NonFatal"
    why = "buyer irritated" if irritated else "no sign of irritation"
    return Finding(parameter="looping", status=status, basis="observed",
                   reason=f"bot asked for {slot} {len(idxs)} times ({why})",
                   evidence=[_ev(call, i) for i in idxs])


def rule_category_isq(call: CallRecord, led: Ledger) -> Finding:
    bad = [b for b in led.bot_turns if b.slot == "spec" and b.fits_confirmed_product is False]
    if not any(b.slot == "spec" for b in led.bot_turns):
        return Finding(parameter="category_isq", status="NotApplicable", reason="no spec questions", basis="observed")
    if not bad:
        return Finding(parameter="category_isq", status="Pass", reason="spec questions fit the product", basis="spoken_proxy")
    return Finding(parameter="category_isq", status="Fatal", basis="spoken_proxy",
                   reason=f"{len(bad)} spec question(s) belong to another category than '{led.final_product}'",
                   evidence=[_ev(call, b.idx) for b in bad])


def rule_qty_probing(call: CallRecord, led: Ledger) -> Finding:
    if led.scope not in BL_SCOPES:
        return Finding(parameter="qty_probing", status="NotApplicable", reason=f"scope is {led.scope}", basis="observed")
    asked = [b.idx for b in led.bot_turns if b.slot == "quantity"]
    volunteered = "quantity" in led.buyer_facts
    if asked or volunteered:
        return Finding(parameter="qty_probing", status="Pass", reason="quantity asked or given", basis="observed",
                       evidence=[_ev(call, asked[0])] if asked else [])
    # Assumption: quantity is a defined field for B2B leads, so never asking it is "defined and not asked".
    return Finding(parameter="qty_probing", status="Fatal", basis="observed",
                   reason="bot never asked for quantity and the buyer did not give one")


def rule_qty_missed(call: CallRecord, led: Ledger) -> Finding:
    said, kept = led.buyer_facts.get("quantity"), led.bot_confirmed.get("quantity")
    if led.scope not in BL_SCOPES or not said:
        return Finding(parameter="qty_missed", status="NotApplicable", reason="buyer gave no quantity", basis="observed")
    if not kept:
        return Finding(parameter="qty_missed", status="Fatal", basis="spoken_proxy",
                       reason=f"buyer said quantity '{said}' but the bot never confirmed it")
    if _digits(said) and _digits(kept) and _digits(said) != _digits(kept):
        return Finding(parameter="qty_missed", status="Fatal", basis="spoken_proxy",
                       reason=f"buyer said '{said}', bot confirmed '{kept}'")
    return Finding(parameter="qty_missed", status="Pass", reason="quantity captured", basis="spoken_proxy")


def rule_product_change(call: CallRecord, led: Ledger) -> Finding:
    if led.product_changed_at_turn is None:
        return Finding(parameter="product_change", status="NotApplicable", reason="product did not change", basis="observed")
    kept = led.bot_confirmed.get("product")
    if kept and led.final_product and not _overlap(kept, led.final_product):
        return Finding(parameter="product_change", status="Fatal", basis="spoken_proxy",
                       reason=f"buyer switched to '{led.final_product}' but bot kept '{kept}'",
                       evidence=[_ev(call, led.product_changed_at_turn)])
    return Finding(parameter="product_change", status="Pass", basis="spoken_proxy",
                   reason=f"bot followed the change to '{led.final_product}'",
                   evidence=[_ev(call, led.product_changed_at_turn)])


RULES = [rule_category_isq, rule_product_change, rule_qty_probing, rule_qty_missed, rule_looping, rule_dead_air]


def audit_call(call: CallRecord, led: Ledger) -> AuditRecord:
    if call.duration_sec < SHORT_CALL_SEC or not any(t.speaker == "user" for t in call.turns):
        return AuditRecord(call_id=call.call_id, verdict="NeedsReview", scope=led.scope, findings=[],
                           flags=["low_confidence"], review_reason="call too short or buyer never spoke")
    findings = [r(call, led) for r in RULES]
    flags: list = []
    if any(u.frustrated >= FRUSTRATED_P for u in led.user_turns):
        flags.append("frustration")
    if any(u.asks_for_human >= HUMAN_P for u in led.user_turns):
        flags.append("human_request")
    if any(f.parameter == "looping" and f.status in ("Fatal", "NonFatal") for f in findings):
        flags.append("loops")
    if any(f.parameter in ("product_change", "qty_missed", "category_isq") and f.status == "Fatal" for f in findings):
        flags.append("wrong_requirement")
    if led.scope == "unclear":
        flags.append("low_confidence")
    statuses = {f.status for f in findings}
    verdict = "Fatal" if "Fatal" in statuses else "NonFatal" if "NonFatal" in statuses else "Pass"
    faults = [e.turn_idx for f in findings if f.status in ("Fatal", "NonFatal") for e in f.evidence if e.turn_idx is not None]
    return AuditRecord(call_id=call.call_id, verdict=verdict, scope=led.scope, findings=findings, flags=flags,
                       first_fault_turn=min(faults) if faults else None)
