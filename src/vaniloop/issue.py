"""ISSUE: audits -> ranked causes (v0: deterministic, one cause per failing rubric parameter)."""

from __future__ import annotations

import json
import math
import os
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from .jsonl import dedupe_by_call_id, read_jsonl
from .models import AuditRecord, Cause, Ledger, Turn

ROOT = Path(__file__).resolve().parents[2]
AUDITS_PATH = ROOT / "runs" / "audits.jsonl"
CAUSES_PATH = ROOT / "runs" / "causes.json"

MIN_SUPPORT = 3
MIN_CATEGORIES = 2  # a pattern recurs in at least this many known product categories
UNKNOWN_CATEGORY = "unknown"  # same label as categorize.UNKNOWN (a test pins it); categorize imports this module
NOT_REPRODUCED_PREFIX = "replay did not reproduce the fault"  # start of the gate.decide reason (#63)
# Display order of the statuses. A pattern is ranked (gate-confirmed) or pattern_unconfirmed (no gate row yet).
FIXABLE_STATUSES = frozenset({"ranked", "pattern_unconfirmed"})  # the causes fix and the Fix view work on
LEGACY_NO_IMPROVEMENT_PREFIX = "no improvement: lift"  # gate.decide reject text; pre-#63 rows with OLD 0/n carry it
STATUS_ORDER = {"ranked": 0, "pattern_unconfirmed": 0, "watch": 1, "outlier": 2, "single_category": 3, "isolated": 4}
FIXABILITY = {"prompt": 1.0, "flow_logic": 0.5, "asr": 0.25, "channel": 0.0}
CHANNEL_FLAGS = ("bot_misheard", "audio_unclear")  # Turn fields that say the line, not the prompt, failed
SUSPECTED_TYPE = {"product_change": "tool", "category_isq": "data"}  # flagged only: a real verdict needs traces
# parameter -> the ledger slot whose spoken value a channel rule (b) compares (#90). qty_unit falls back to
# quantity when the ledger has no unit slot (see _slot_for); looping and dead_air have no slot: rule (a) only.
SLOT_OF = {
    "qty_missed": "quantity",
    "qty_accuracy": "quantity",
    "qty_unit": "quantity",  # falls back to quantity when the ledger has no unit slot (see _slot_for)
    "qty_probing": "quantity",
    "product_change": "product",
    "category_isq": "spec",
}
# parameter -> (owner, owner_evidence, suspect, owner_note) (#90). No trace fields exist yet, so nothing here
# is proof. Every parameter not listed is owned by the prompt with likely evidence, as the type usually says.
OWNER_OF = {
    "dead_air": ("runtime", "likely", None, None),
    "category_isq": ("unresolved", "missing", "data", None),
    "product_change": ("unresolved", "missing", "tool", None),
    "qty_probing": ("prompt", "likely", None, "applicability assumed: unit list not visible"),
}
OTHER_OWNER = ("prompt", "likely", None, None)  # every other prompt or flow_logic cause
SPEECH_OWNER = ("speech", "likely", None, None)  # every channel cause: the line, not the prompt, failed

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


def _norm_value(text: str | None) -> str:
    """A slot value compared for equality: stripped, lowercased, inner whitespace collapsed to one space."""
    return " ".join(str(text).split()).lower()


def _value_mismatch(led: Ledger, slot: str) -> bool:
    """True when the ledger's buyer value and the bot's confirmed value for `slot` differ.

    Values are compared after normalisation (strip, lowercase, collapse spaces); for quantity the two sides are
    parsed with the rules.py helpers first, so '50' and 'fifty' agree. A value on only one side is a mismatch
    (the buyer said it, the bot never captured it); both empty is no mismatch.
    """
    said, kept = led.buyer_facts.get(slot), led.bot_confirmed.get(slot)
    if not _norm_value(said) and not _norm_value(kept):
        return False
    if slot == "quantity":
        from .rules import parse_quantity  # lazy: rules imports only models today, issue must not import it first

        pa, pb = parse_quantity(said), parse_quantity(kept)
        if pa is not None and pb is not None:
            return pa != pb
    return _norm_value(said) != _norm_value(kept)


def _slot_for(param: str, led: Ledger | None) -> str | None:
    """The ledger slot the channel rule (b) compares for this parameter, None when there is no such slot.

    qty_unit compares the unit slot when the ledger has one (either side stated a unit), else the quantity
    fallback of SLOT_OF. dead_air and looping have no slot, so only rule (a) can make them channel.
    """
    if param == "qty_unit" and led is not None and (led.buyer_facts.get("unit") or led.bot_confirmed.get("unit")):
        return "unit"
    return SLOT_OF.get(param)


def _channel_turn(turns: Sequence[Turn], onset: int, slot: str | None, led: Ledger | None) -> bool:
    """True only when the audio, not the prompt, failed at or before the onset turn.

    (a) audio_unclear on the onset turn or the nearest user turn before it, or (b) bot_misheard on one of them AND
    the ledger shows a spoken value mismatch for `slot` (see _value_mismatch). A re-ask alone, even with
    bot_misheard, is not channel: Gemini sets bot_misheard on every re-ask (#90). Without the call's ledger, (b)
    cannot show and only (a) makes the cause channel.
    """
    near = [t for t in turns if t.idx == onset]
    near += sorted((t for t in turns if t.speaker == "user" and t.idx < onset), key=lambda t: t.idx)[-1:]
    if any(t.audio_unclear is True for t in near):
        return True  # (a): the audio there is unclear, so the words may never have arrived
    return (
        slot is not None
        and led is not None
        and any(t.bot_misheard is True for t in near)
        and _value_mismatch(led, slot)
    )


def decide_roots(audit: AuditRecord) -> dict[str, dict]:
    """Per failing parameter of one call: its severity, its onset and whether it is a root or a symptom.

    A parameter fails when one of its findings is Fatal or NonFatal. Its severity is the highest of those
    findings and its onset is the lowest onset_turn among them (None if none has one). The root is the failing
    parameter with the lowest onset. Later ones are symptoms.
    Tie rule: parameters with the same lowest onset are all roots (co-roots). A parameter without an onset is
    never a root while any failing parameter has one. If no failing parameter has an onset they are all roots.
    Neither case has a provable order, so neither parameter is called a symptom, and the call counts for each
    as it did before root ordering existed. Returns {parameter: {"status", "onset", "root"}}.
    """
    found: dict[str, dict] = {}
    for f in audit.findings:
        if f.status not in ("Fatal", "NonFatal"):
            continue
        d = found.setdefault(f.parameter, {"status": f.status, "onset": f.onset_turn})
        if f.status == "Fatal":
            d["status"] = "Fatal"
        if f.onset_turn is not None and (d["onset"] is None or f.onset_turn < d["onset"]):
            d["onset"] = f.onset_turn
    onsets = [d["onset"] for d in found.values() if d["onset"] is not None]
    first = min(onsets) if onsets else None  # None: nobody has an onset, so everyone is a root
    for d in found.values():
        d["root"] = d["onset"] == first
    return found


def build_causes(
    audits: list[AuditRecord],
    turns: Mapping[str, Sequence[Turn]] | None = None,
    ledgers: Mapping[str, Ledger] | None = None,
) -> list[Cause]:
    """One cause per failing parameter, counted on the calls where that parameter is the call's root finding.

    A call where the parameter failed but an earlier finding is the root goes to `symptom_calls` and counts for
    nothing. A cause with no root call is not listed. See decide_roots for the tie rule.
    A root is a channel fault only when the audio, not the prompt, failed at its onset (see _channel_turn): the
    onset turn or the nearest user turn before it has audio_unclear, or it has bot_misheard and the call's ledger
    shows a spoken value mismatch for the parameter's slot (qty_* -> quantity, qty_unit -> unit when the ledger
    has a unit slot, product_change -> product, category_isq -> spec). A re-ask alone, even with bot_misheard, is
    a prompt fault (#90). With `turns` or `ledgers` None, or without the call, the missing part is out and the
    other rule still applies.
    Every cause gets an owner, an owner_evidence, a suspect (for unresolved ones) and an owner_note from
    OWNER_OF and the type (channel -> speech, dead_air -> runtime). No cause is ever marked proof: no trace
    fields exist yet.
    """
    calls = turns or {}
    leds = ledgers or {}
    audits = _dedupe(audits)
    audited = [a for a in audits if a.verdict != "NeedsReview"]
    n = len(audited)
    # (channel?, parameter) -> {"Fatal": calls, "NonFatal": calls, "symptom": calls}
    groups: dict[tuple[bool, str], dict[str, set[str]]] = {}
    for a in audited:
        for param, d in decide_roots(a).items():
            led = leds.get(a.call_id)
            channel = (
                d["root"]
                and d["onset"] is not None
                and _channel_turn(calls.get(a.call_id, ()), d["onset"], _slot_for(param, led), led)
            )
            g = groups.setdefault((channel, param), {"Fatal": set(), "NonFatal": set(), "symptom": set()})
            g[d["status"] if d["root"] else "symptom"].add(a.call_id)
    causes: list[Cause] = []
    for (channel, param), d in groups.items():
        fatal = sorted(d["Fatal"])
        nonfatal = sorted(d["NonFatal"])
        affected = len(fatal) + len(nonfatal)
        if not affected:
            continue  # only symptom calls: the cause of those calls is listed under its root's parameter
        name, desc, typ = KNOWN.get(param, (param, f"Rubric parameter '{param}' failed.", "prompt"))
        if channel:
            name, desc, typ = (
                f"Bad audio at the start of: {name.lower()}",
                f"{desc} The fault starts where the bot misheard or the line was unclear, so a prompt cannot fix it.",
                "channel",
            )
        fix = FIXABILITY[typ] if typ in FIXABILITY else 1.0
        lo, hi = wilson(affected, n)
        status = "watch" if channel else "ranked" if affected >= MIN_SUPPORT else "watch" if fatal else "isolated"
        owner, owner_ev, suspect, owner_note = SPEECH_OWNER if channel else OWNER_OF.get(param, OTHER_OWNER)
        causes.append(
            Cause(
                cause_id=f"cause_channel_{param}" if channel else f"cause_{param}",
                name=name,
                description=desc,
                type=typ,
                parameter=param,
                fatal_calls=fatal,
                nonfatal_calls=nonfatal,
                symptom_calls=[] if channel else sorted(d["symptom"]),
                suspected_type="" if channel else SUSPECTED_TYPE.get(param, ""),
                fixability=fix,
                priority=(5 * len(fatal) + len(nonfatal)) * fix,
                rank_score=lo * ((5 * len(fatal) + len(nonfatal)) / affected) * fix,
                freq_ci_low=lo,
                freq_ci_high=hi,
                status=status,
                owner=owner,
                owner_evidence=owner_ev,
                suspect=suspect,
                owner_note=owner_note,
            )
        )
    causes.sort(key=lambda c: (STATUS_ORDER[c.status], -c.rank_score, -c.priority))
    return causes


def _not_reproduced(row: dict) -> bool:
    """True if a gate row says the OLD replay did not show the fault.

    Rows written since #63 say so in the reason. A row from before #63 with 0 OLD failures was a plain
    "no improvement: lift" reject, so that reason with old_fail_rate 0 counts too. Any other row with rate 0
    (unresolved trials, a futility stop, identical replays) has no real evidence and does not count.
    """
    reason = str(row.get("reason", ""))
    if reason.startswith(NOT_REPRODUCED_PREFIX):
        return True
    return reason.startswith(LEGACY_NO_IMPROVEMENT_PREFIX) and row.get("old_fail_rate") == 0


def require_fixable(cause_id: str, causes_path: Path) -> None:
    """Stop with SystemExit when causes.json holds this cause with a status that is not in FIXABLE_STATUSES.

    A cause of type channel is refused whatever its status.

    A cause that was demoted to outlier or single_category keeps its old patch, so refix and prove call this
    before any LLM call. A missing file or a missing cause is not refused here (the caller has its own checks).
    The message names the status and the failed conditions from the outliers.jsonl next to causes.json.
    """
    path = Path(causes_path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8")).get("causes", [])
    except (OSError, ValueError, AttributeError):
        return
    row = next((c for c in raw if isinstance(c, dict) and c.get("cause_id") == cause_id), None)
    if row is None:
        return
    if row.get("type") == "channel":  # bad audio is never patched, whatever the status in a hand-edited file says
        raise SystemExit(f"{cause_id} is a channel cause in {path.name} (bad audio); it takes no patch and no gate run")
    if row.get("status") in FIXABLE_STATUSES:
        return
    why = next(
        (r.get("failed") for r in read_jsonl(path.with_name("outliers.jsonl")) if r.get("cause_id") == cause_id), None
    )
    raise SystemExit(
        f"{cause_id} has status '{row.get('status')}' in {path.name}, not a pattern"
        + (f" (failed: {', '.join(why)})" if why else "")
        + "; it takes no new patch and no gate run"
    )


def classify_causes(
    causes: list[Cause],
    category_of: Callable[[str], str],
    gate_rows: list[dict],
) -> tuple[list[Cause], list[dict]]:
    """Split causes into patterns and outliers. Returns (causes in display order, outlier log rows).

    A cause is a pattern only if (a) it has MIN_SUPPORT calls (build_causes: status ranked), (b) its calls span
    at least MIN_CATEGORIES known product categories and (c) its latest gate row is not "replay did not
    reproduce the fault". With no gate row yet it is `pattern_unconfirmed`, not an outlier. Else:
      - fails (b): `single_category` (a data signal). Fails (b) and (c): still single_category.
      - fails (c) only: `outlier`.
      - fails (a): keeps `watch` or `isolated`.
    Every cause that fails any condition is logged with all the conditions it failed, in the order
    support, categories, reproduction (`condition` is the first).
    A call with category `unknown` (no ledger, no product or an unlabelled name) is never a category: it adds
    nothing to the count, so unknowns cannot fake a second category. `other` is a real answer and counts as one.
    Scores and ranks are not changed: only the status and the order.
    A channel cause (build_causes) is never a pattern: it stays `watch` and is logged once with condition `channel`.
    """
    last_gate = {r.get("cause_id"): r for r in gate_rows}  # gates.jsonl is append-only: the last row wins
    out: list[Cause] = []
    outliers: list[dict] = []
    for c in causes:
        calls = c.fatal_calls + c.nonfatal_calls
        if c.type == "channel":
            outliers.append(
                {
                    "cause_id": c.cause_id,
                    "parameter": c.parameter,
                    "status": "watch",
                    "condition": "channel",
                    "failed": ["channel"],
                    "calls": len(calls),
                    "categories": {},
                    "unknown_calls": 0,
                    "gate_reason": None,
                }
            )
            out.append(c.model_copy(update={"status": "watch"}))
            continue
        by_cat = Counter(category_of(call) for call in calls)
        unknown = by_cat.pop(UNKNOWN_CATEGORY, 0)
        gate = last_gate.get(c.cause_id)
        not_reproduced = gate is not None and _not_reproduced(gate)
        failed = []  # every condition is judged on its own, so the log keeps all of them
        if len(calls) < MIN_SUPPORT:
            failed.append("support")
        if len(by_cat) < MIN_CATEGORIES:
            failed.append("categories")
        if not_reproduced:
            failed.append("reproduction")
        if failed:
            # the status follows the first failed condition; below MIN_SUPPORT it stays watch or isolated
            status = c.status if "support" in failed else "single_category" if "categories" in failed else "outlier"
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


def _call_turns(runs: Path) -> dict[str, list[Turn]]:
    """call_id -> turns from `<runs>/calls.jsonl`. A missing file or a malformed row gives no turns for that call."""
    out: dict[str, list[Turn]] = {}
    for r in dedupe_by_call_id(read_jsonl(runs / "calls.jsonl"), last_wins=True):
        try:
            out[r["call_id"]] = [Turn.model_validate(t) for t in r["turns"]]
        except Exception:  # one bad call must not stop ranking
            continue
    return out


def _call_ledgers(runs: Path) -> dict[str, Ledger]:
    """call_id -> ledger from `<runs>/ledgers.jsonl` (last row wins). A malformed row is skipped.

    The channel rule (b) reads the buyer's value and the bot's confirmed value from the ledger (#90).
    """
    out: dict[str, Ledger] = {}
    for r in dedupe_by_call_id(read_jsonl(runs / "ledgers.jsonl"), last_wins=True):
        try:
            out[r["call_id"]] = Ledger.model_validate(r)
        except Exception:  # one bad row must not stop ranking
            continue
    return out


def _root_stats(audits: list[AuditRecord]) -> dict[str, int]:
    """Counts that show whether root ordering works on these audits (the stop rule of issue #71).

    `calls_with_several_faults`: graded calls with 2 or more failing parameters. `calls_with_several_roots`: of
    those, calls where the order is undefined (equal onsets or no onsets), so more than one parameter is a root.
    """
    several = roots = 0
    for a in _dedupe([a for a in audits if a.verdict != "NeedsReview"]):
        found = decide_roots(a)
        several += len(found) > 1
        roots += sum(d["root"] for d in found.values()) > 1
    return {"calls_with_several_faults": several, "calls_with_several_roots": roots}


def run_issues(audits_path: Path = AUDITS_PATH, out_path: Path = CAUSES_PATH) -> list[Cause]:
    """Rank causes, split patterns from outliers, write causes.json and outliers.jsonl.

    categories.json, ledgers.jsonl, calls.jsonl and gates.jsonl are read from the folder of `out_path`;
    outliers.jsonl is written there and rewritten whole each run (it is a snapshot of today's outliers, not a history).
    """
    audits = load_audits(Path(audits_path))
    runs = Path(out_path).parent
    causes, outliers = classify_causes(
        build_causes(audits, _call_turns(runs), _call_ledgers(runs)),
        _call_categories(runs),
        read_jsonl(runs / "gates.jsonl"),
    )
    review = sum(a.verdict == "NeedsReview" for a in audits)
    doc = {
        "denominator": len(audits) - review,
        "audited_total": len(audits),
        "needs_review": review,
        **_root_stats(audits),
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
