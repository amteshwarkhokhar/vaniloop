import json

import pytest

from vaniloop import categorize, gate
from vaniloop.issue import (
    KNOWN,
    NOT_REPRODUCED_PREFIX,
    UNKNOWN_CATEGORY,
    build_causes,
    classify_causes,
    require_fixable,
    run_issues,
    wilson,
)
from vaniloop.models import AuditRecord, Finding, Ledger, Turn


def aud(cid, verdict="Fatal", **statuses):
    fs = [Finding(parameter=p, status=s, reason="r", basis="observed") for p, s in statuses.items()]
    return AuditRecord(call_id=cid, verdict=verdict, scope="bl_collection", findings=fs)


def test_wilson_known_values():
    """Wilson intervals must match known published values and handle zero trials and full success.

    Wrong bounds would misstate the frequency confidence and mislead cause ranking for an auditor.
    """
    lo, hi = wilson(0, 10)
    assert lo == 0.0 and hi == pytest.approx(0.2775, abs=1e-3)
    lo, hi = wilson(5, 10)
    assert (lo, hi) == (pytest.approx(0.2366, abs=1e-3), pytest.approx(0.7634, abs=1e-3))
    assert wilson(10, 10)[1] == 1.0
    assert wilson(0, 0) == (0.0, 0.0)


def test_counts_once_per_call_highest_severity_and_ranking():
    """A cause must count each call once at its highest severity and rank by the documented priority formula.

    Double counting or wrong ranking would send the fix team after a cause that is not the top risk.
    """
    a = [
        aud("a", looping="Fatal"),
        aud("b", looping="NonFatal"),
        aud("c", looping="NonFatal"),
        aud("d", "Fatal", dead_air="Fatal"),
        aud("e", "Pass", looping="Pass"),
    ]
    c = {x.parameter: x for x in build_causes(a)}
    assert c["looping"].fatal_calls == ["a"] and c["looping"].nonfatal_calls == ["b", "c"]
    assert c["looping"].status == "ranked" and c["looping"].priority == (5 + 2) * 0.5
    assert c["dead_air"].status == "watch"
    assert c["looping"].freq_ci_low == pytest.approx(wilson(3, 5)[0])


def test_status_thresholds_sort_and_needs_review_excluded():
    """Cause status thresholds, the sort order and exclusion of NeedsReview calls must all hold together.

    A breach would misrank the cause list and let ungraded calls into the denominator.
    """
    a = [
        aud("a", category_isq="Fatal"),
        aud("b", category_isq="NonFatal"),
        aud("c", category_isq="NonFatal"),
        aud("d", "NonFatal", qty_probing="NonFatal"),
        aud("e", "Fatal", dead_air="Fatal"),
        aud("f", "NeedsReview", category_isq="Fatal"),
    ]
    cs = build_causes(a)
    assert [x.parameter for x in cs] == ["category_isq", "dead_air", "qty_probing"]
    assert [x.status for x in cs] == ["ranked", "watch", "isolated"]
    assert "f" not in cs[0].fatal_calls
    assert cs[0].type == "prompt" and cs[1].type == "flow_logic"
    assert cs[0].freq_ci_low == pytest.approx(wilson(3, 5)[0])  # n excludes NeedsReview


# ---- pattern / outlier (issue #70). Synthetic calls and categories only.
NOT_REPRO = {
    "cause_id": "cause_looping",
    "decision": "inconclusive",
    "reason": "replay did not reproduce the fault (old 0/15)",
}
SHIPPED = {"cause_id": "cause_looping", "decision": "ship", "reason": "exact permutation p 0.001 <= alpha 0.05"}


def looping_causes(ids=("a", "b", "c")):
    """Build causes for one failing parameter with one Fatal call per id."""
    return build_causes([aud(i, looping="Fatal") for i in ids])


def cat_map(**by_call):
    """A category lookup over a synthetic call -> category map; a missing call is unknown."""
    return lambda call_id: by_call.get(call_id, UNKNOWN_CATEGORY)


def test_one_category_cause_is_single_category_and_not_ranked():
    """A cause whose calls sit in one category must be single_category, logged, and never ranked.

    Without it a data signal for one product line would be fixed in the shared prompt as if it were a pattern.
    """
    causes = looping_causes()
    assert causes[0].status == "ranked"  # today's rule alone would rank it
    out, outliers = classify_causes(causes, cat_map(a="food", b="food", c="food"), [SHIPPED])
    assert [c.status for c in out] == ["single_category"]
    assert outliers[0]["status"] == "single_category" and outliers[0]["condition"] == "categories"
    assert outliers[0]["categories"] == {"food": 3}


def test_not_reproduced_gate_makes_a_two_category_cause_an_outlier(tmp_path):
    """A latest gate row saying the replay did not reproduce the fault must make the cause an outlier on disk.

    Without it a fault the old prompt cannot show would keep a fix slot and the fix could not be proven.
    """
    audits = tmp_path / "audits.jsonl"
    audits.write_text("\n".join(aud(i, looping="Fatal").model_dump_json() for i in "abc") + "\n")
    (tmp_path / "gates.jsonl").write_text(json.dumps(NOT_REPRO) + "\n")
    products = [("a", "Sofa"), ("b", "Sofa"), ("c", "Pump")]
    (tmp_path / "ledgers.jsonl").write_text(
        "\n".join(json.dumps({"call_id": i, "initial_product": p}) for i, p in products) + "\n"
    )
    (tmp_path / "categories.json").write_text(json.dumps({"sofa": "furniture_home", "pump": "machinery_equipment"}))
    causes = run_issues(audits, tmp_path / "causes.json")
    assert [c.status for c in causes] == ["outlier"]
    rows = [json.loads(x) for x in (tmp_path / "outliers.jsonl").read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["cause_id"] == "cause_looping" and rows[0]["condition"] == "reproduction"
    assert json.loads((tmp_path / "causes.json").read_text())["causes"][0]["status"] == "outlier"


def test_cause_without_a_gate_row_is_pattern_unconfirmed():
    """A two-category cause with no gate row yet must be pattern_unconfirmed and must not be logged as an outlier.

    Without it a cause nobody has replayed would be dropped as an outlier before the gate could ever test it.
    """
    out, outliers = classify_causes(looping_causes(), cat_map(a="food", b="food", c="services"), [])
    assert [c.status for c in out] == ["pattern_unconfirmed"] and outliers == []


def test_reproduced_two_category_cause_stays_ranked():
    """A two-category cause with a gate row that is not 'not reproduced' must stay ranked and unlogged.

    Without it confirmed patterns would lose their status and the fix stage would have nothing to fix.
    """
    out, outliers = classify_causes(looping_causes(), cat_map(a="food", b="food", c="services"), [SHIPPED])
    assert [c.status for c in out] == ["ranked"] and outliers == []


def test_unknown_category_never_counts_as_a_second_category():
    """Calls with an unknown category must not make a one-category cause look like it spans two categories.

    Without it a missing ledger product would turn every one-category cause into a false pattern.
    """
    out, outliers = classify_causes(looping_causes(), cat_map(a="food", b="food"), [SHIPPED])  # c is unknown
    assert out[0].status == "single_category"
    assert outliers[0]["categories"] == {"food": 2} and outliers[0]["unknown_calls"] == 1
    out, _ = classify_causes(looping_causes(), cat_map(), [SHIPPED])  # no category known at all
    assert out[0].status == "single_category"


def test_latest_gate_row_decides_and_other_reasons_do_not_demote():
    """The last gate row of a cause must decide, and only the 'not reproduced' reason may demote a cause.

    Without it an old rejected row or a plain 'no improvement' reject would wrongly turn a pattern into an outlier.
    """
    cats = cat_map(a="food", b="food", c="services")
    out, _ = classify_causes(looping_causes(), cats, [NOT_REPRO, SHIPPED])  # reproduced on the retry
    assert out[0].status == "ranked"
    out, _ = classify_causes(looping_causes(), cats, [SHIPPED, NOT_REPRO])
    assert out[0].status == "outlier"
    reject = {**SHIPPED, "decision": "reject", "reason": "no improvement: lift 0.000 <= 0"}
    out, _ = classify_causes(looping_causes(), cats, [reject])
    assert out[0].status == "ranked"


def test_low_support_cause_keeps_its_status_and_logs_every_failed_condition():
    """A cause under MIN_SUPPORT must keep watch or isolated and log all the conditions it failed, not only support.

    Without it the log would hide that a small cause also spans one category or was not reproduced.
    """
    causes = build_causes([aud("a", dead_air="Fatal"), aud("b", qty_probing="NonFatal"), aud("c", looping="Fatal")])
    out, outliers = classify_causes(causes, cat_map(a="food", b="services", c="food"), [NOT_REPRO])
    assert sorted(c.status for c in out) == ["isolated", "watch", "watch"]
    failed = {r["parameter"]: r["failed"] for r in outliers}
    assert failed["dead_air"] == ["support", "categories"]  # one call is one category
    assert failed["qty_probing"] == ["support", "categories"]
    assert failed["looping"] == ["support", "categories", "reproduction"]  # NOT_REPRO is the looping gate row
    assert {r["condition"] for r in outliers} == {"support"}


def test_legacy_gate_row_with_zero_old_failures_counts_as_not_reproduced():
    """A gate row from before #63 with old_fail_rate 0 and a 'no improvement' reason must demote the cause.

    Without it that old row would leave the cause ranked, though the OLD replay never showed the fault.
    """
    legacy = {
        "cause_id": "cause_looping",
        "decision": "reject",
        "reason": "no improvement: lift 0.000 <= 0",
        "old_fail_rate": 0.0,
    }
    cats = cat_map(a="food", b="food", c="services")
    out, outliers = classify_causes(looping_causes(), cats, [legacy])
    assert out[0].status == "outlier" and outliers[0]["failed"] == ["reproduction"]
    shipped = {**legacy, "decision": "ship", "reason": "exact permutation p 0.001 <= alpha 0.05"}
    assert classify_causes(looping_causes(), cats, [shipped])[0][0].status == "ranked"
    real_reject = {**legacy, "old_fail_rate": 0.4}  # a reject where OLD did fail stays a reject
    assert classify_causes(looping_causes(), cats, [real_reject])[0][0].status == "ranked"


def test_patterns_keep_rank_order_and_come_before_non_patterns():
    """Ranked and unconfirmed patterns must keep the rank-score order, ahead of watch, outlier and single_category.

    Without it a non-pattern could sit above a real pattern in the list the fix stage and the dashboard read.
    """
    a = (
        [aud(f"x{i}", looping="Fatal") for i in range(3)]  # strong: rank 1
        + [aud(f"y{i}", qty_probing="Fatal") for i in range(3)]
        + [aud(f"z{i}", category_isq="Fatal") for i in range(3)]
        + [aud("w", dead_air="Fatal")]
        + [aud(f"p{i}", "Pass") for i in range(6)]
    )
    base = build_causes(a)
    cats = {f"x{i}": c for i, c in enumerate(["food", "services", "food"])}
    cats.update({f"y{i}": "food" for i in range(3)})  # single category
    cats.update({f"z{i}": c for i, c in enumerate(["food", "services", "other"])})
    out, _ = classify_causes(base, cat_map(**cats), [])
    by = {c.parameter: c for c in out}
    assert by["looping"].status == "pattern_unconfirmed" and by["category_isq"].status == "pattern_unconfirmed"
    assert by["qty_probing"].status == "single_category" and by["dead_air"].status == "watch"
    assert [c.status for c in out][-2:] == ["watch", "single_category"]
    patterns = [c for c in out if c.status == "pattern_unconfirmed"]
    assert patterns == sorted(patterns, key=lambda c: -c.rank_score)
    assert [c.status for c in out].index("watch") > max(i for i, c in enumerate(out) if c in patterns)
    # the numbers are untouched: classification changes the status and the order, not the score
    assert {c.cause_id: c.rank_score for c in out} == {c.cause_id: c.rank_score for c in base}


def test_not_reproduced_prefix_matches_the_gate_reason_text():
    """The prefix issue.py matches must appear in gate.py and the unknown label must match categorize.py.

    Without it a reworded gate reason or category label would silently stop demoting outliers or counting unknowns.
    """
    assert NOT_REPRODUCED_PREFIX in open(gate.__file__, encoding="utf-8").read()
    assert UNKNOWN_CATEGORY == categorize.UNKNOWN


def test_zero_old_rate_rows_without_real_evidence_do_not_make_an_outlier():
    """Unresolved, futility and identical-replay gate rows with old_fail_rate 0 must not demote a cause.

    Without it a gate run that never saw OLD fail for lack of evidence would hide a real pattern as an outlier.
    """
    cats = cat_map(a="food", b="food", c="services")
    reasons = [
        "6 trial(s) lack conclusive evidence (old=6, new=0, guards=0; Unknown/missing finding)",
        "futility: new prompt failed on every call at seed 0",
        "replays do not vary; K copies are one sample",
    ]
    for reason in reasons:
        row = {"cause_id": "cause_looping", "decision": "inconclusive", "reason": reason, "old_fail_rate": 0.0}
        out, outliers = classify_causes(looping_causes(), cats, [row])
        assert out[0].status == "ranked" and outliers == [], reason


# ---- root vs symptom and channel (issue #71). Synthetic calls only.
def aud_on(cid, verdict="Fatal", **found):
    """An audit whose findings carry onsets: parameter=(status, onset_turn or None)."""
    fs = [Finding(parameter=p, status=s, reason="r", basis="observed", onset_turn=o) for p, (s, o) in found.items()]
    return AuditRecord(call_id=cid, verdict=verdict, scope="bl_collection", findings=fs)


def turns_for(n=12, **flags):
    """n alternating turns (even idx bot, odd idx user). flags: `<idx>` -> field set True, e.g. t5="bot_misheard"."""
    out = []
    for i in range(n):
        extra = {flags[f"t{i}"]: True} if f"t{i}" in flags else {}
        out.append(
            Turn(idx=i, speaker="bot" if i % 2 == 0 else "user", start_sec=i, end_sec=i + 0.5, text="x", **extra)
        )
    return out


def led(call_id, **slots):
    """A synthetic ledger: slot -> (buyer value, bot value). None drops that side."""
    return Ledger(
        call_id=call_id,
        scope="bl_collection",
        buyer_facts={s: v[0] for s, v in slots.items() if v[0] is not None},
        bot_confirmed={s: v[1] for s, v in slots.items() if v[1] is not None},
    )


def by_param(causes):
    return {c.parameter: c for c in causes}


def test_earliest_finding_is_the_root_and_later_ones_are_symptoms():
    """Looping at turn 9 and qty_missed at turn 4 must count the call for qty_missed and list looping as symptom.

    Without it one early fault that causes later ones would be counted for every parameter it trips.
    """
    audits = [aud_on(c, qty_missed=("Fatal", 4), looping=("Fatal", 9)) for c in "abc"]
    c = by_param(build_causes(audits))
    assert c["qty_missed"].fatal_calls == ["a", "b", "c"] and c["qty_missed"].symptom_calls == []
    assert "looping" not in c  # a cause with no root call is not listed
    audits.append(aud_on("d", looping=("Fatal", 2)))  # here looping is the root
    audits += [aud_on(x, looping=("NonFatal", 1)) for x in "ef"]
    c = by_param(build_causes(audits))
    assert c["looping"].fatal_calls == ["d"] and c["looping"].nonfatal_calls == ["e", "f"]
    assert c["looping"].symptom_calls == ["a", "b", "c"]
    assert c["looping"].status == "ranked"  # 3 root calls; the symptom calls add nothing
    assert c["qty_missed"].status == "ranked"


def test_symptom_calls_do_not_count_for_support_or_rank():
    """A cause with 2 root calls and 5 symptom calls must stay under MIN_SUPPORT and score as 2 calls.

    Without it a parameter that is only a downstream effect would climb the ranking and take a fix slot.
    """
    audits = [aud_on(f"r{i}", looping=("Fatal", 3)) for i in range(2)]
    audits += [aud_on(f"s{i}", qty_missed=("Fatal", 1), looping=("Fatal", 6)) for i in range(5)]
    audits += [aud(f"p{i}", "Pass") for i in range(3)]
    n = 10
    c = by_param(build_causes(audits))["looping"]
    assert (c.fatal_calls, len(c.symptom_calls)) == (["r0", "r1"], 5)
    assert c.status == "watch" and c.freq_ci_low == pytest.approx(wilson(2, n)[0])
    assert c.priority == pytest.approx(5 * 2 * 0.5)


def test_finding_without_onset_is_never_root_over_one_with_an_onset():
    """A failing finding with no onset must be a symptom of any finding that has an onset, even a late one.

    Without it a rule that cannot say where its fault started would steal the root from a precisely placed fault.
    """
    audits = [aud_on(c, looping=("Fatal", None), qty_probing=("NonFatal", 30)) for c in "abc"]
    c = by_param(build_causes(audits))
    assert c["qty_probing"].nonfatal_calls == ["a", "b", "c"] and "looping" not in c


def test_equal_onsets_and_all_missing_onsets_make_co_roots():
    """Findings that start on the same turn, or that all lack an onset, must each count as a root of that call.

    Neither order can be proven, so neither parameter is called a symptom. A later-onset finding on the same call
    is still a symptom, so the test fails without root selection (the tie rule of decide_roots).
    """
    audits = [aud_on(c, looping=("Fatal", 5), qty_missed=("NonFatal", 5), qty_accuracy=("Fatal", 9)) for c in "abc"]
    audits += [aud_on(c, dead_air=("Fatal", None), qty_probing=("Fatal", None)) for c in "def"]
    c = by_param(build_causes(audits))
    for p in ("looping", "qty_missed"):
        assert c[p].symptom_calls == [] and len(c[p].fatal_calls + c[p].nonfatal_calls) == 3
    assert "qty_accuracy" not in c  # onset 9 is later than the tied roots at 5: a symptom, no root call
    for p in ("dead_air", "qty_probing"):
        assert c[p].symptom_calls == [] and c[p].fatal_calls == ["d", "e", "f"]
    # a symptom is recorded on a cause that does have roots
    c = by_param(build_causes(audits + [aud_on(x, qty_accuracy=("Fatal", 1)) for x in "ghi"]))
    assert c["qty_accuracy"].symptom_calls == ["a", "b", "c"]


def test_misheard_onset_with_a_value_mismatch_is_channel_and_a_pure_re_ask_is_not():
    """A misheard re-ask is channel only when the ledger's bot-confirmed quantity differs from what the buyer said.

    Gemini sets bot_misheard on every re-ask, so without the ledger check every qty_missed root became a speech
    fault: a re-ask with equal buyer and bot quantities must stay a prompt cause owned by the prompt (#90). Both
    sides must state a value: the bot capturing nothing is a capture gap, not a spoken mismatch, so a one-sided
    ledger stays prompt-owned, or the plain re-ask would become the very false speech channel this issue removes
    (#90 review round 2, HIGH).
    """
    audits = [aud_on(c, qty_missed=("Fatal", 4)) for c in "abc"]
    turns = {c: turns_for(t4="bot_misheard") for c in "abc"}
    wrong = {c: led(c, quantity=("50 kg", "15 kg")) for c in "abc"}  # buyer said 50, the bot captured 15
    causes = build_causes(audits, turns, wrong)
    assert [(c.type, c.status, c.parameter) for c in causes] == [("channel", "watch", "qty_missed")]
    assert (causes[0].owner, causes[0].owner_evidence, causes[0].suspect, causes[0].owner_note) == (
        "speech",
        "likely",
        None,
        None,
    )
    assert causes[0].cause_id != "cause_qty_missed" and causes[0].fatal_calls == ["a", "b", "c"]
    assert causes[0].fixability == 0.0
    equal = {c: led(c, quantity=("50 kg", "50 kg")) for c in "abc"}
    causes = build_causes(audits, turns, equal)
    assert [(c.type, c.owner, c.owner_evidence) for c in causes] == [("prompt", "prompt", "likely")]
    one_side = {c: led(c, quantity=("50 kg", None)) for c in "abc"}
    assert [c.type for c in build_causes(audits, turns, one_side)] == ["prompt"]  # bot captured nothing: not a mismatch
    audits += [aud_on(c, qty_missed=("Fatal", 4)) for c in "de"]  # two clean calls (no turns flagged)
    turns.update({c: turns_for() for c in "de"})
    leds = {**wrong, "d": led("d"), "e": led("e")}
    c = {x.cause_id: x for x in build_causes(audits, turns, leds)}
    assert c["cause_qty_missed"].fatal_calls == ["d", "e"] and c["cause_channel_qty_missed"].fatal_calls == [
        "a",
        "b",
        "c",
    ]
    assert c["cause_qty_missed"].type == "prompt"
    assert [x.type for x in build_causes(audits, turns)] == ["prompt"]  # no ledgers: a re-ask stays a prompt fault


def test_qty_unit_channel_compares_units_not_numbers():
    """A misheard qty_unit root mismatches only when both sides name a unit and the units differ.

    The unit each side states is its unit slot when it has one, else the unit word inside its quantity, so
    '50 kg' vs '50 litres' reads as a unit fault with no separate unit slots; without that extraction the unit
    fault would hide behind equal numbers. Numbers alone ('5' vs '3') and a value on one side only are never a
    unit mismatch (#90 review round 2, MEDIUM: unit routing compared the parsed numbers instead).
    """
    audits = [aud_on(c, qty_unit=("Fatal", 4)) for c in "abc"]
    turns = {c: turns_for(t4="bot_misheard") for c in "abc"}
    units = {c: led(c, quantity=("500 g", "500 g"), unit=("kg", "g")) for c in "abc"}
    causes = build_causes(audits, turns, units)
    assert causes[0].type == "channel"  # quantities match, the units differ: the unit slot decided
    no_units = {c: led(c, quantity=("5", "3")) for c in "abc"}
    assert [c.type for c in build_causes(audits, turns, no_units)] == ["prompt"]  # numbers alone: no unit mismatch
    mixed = {c: led(c, quantity=("50 kg", "50 litres")) for c in "abc"}
    assert build_causes(audits, turns, mixed)[0].type == "channel"  # units inside the quantity text differ
    equal = {c: led(c, quantity=("5", "5")) for c in "abc"}
    assert [c.type for c in build_causes(audits, turns, equal)] == ["prompt"]  # nothing differs: a re-ask, not speech


def test_quantity_channel_converts_units_and_compares_the_range_maximum():
    """A misheard qty root agrees on the same quantity in two units and mismatches differing ranges.

    '1 kg' and '1000 g' are the same quantity (the bot side converts into the buyer's unit) and '2-3 kg' vs
    '2-3 kg' is a range stored as its maximum, so both stay a prompt fault. '2-3 kg' vs '2-9 kg' differs by its
    maximum. Without parse_quantity_full and the conversion, parse_quantity compared first numbers alone:
    1 kg vs 1000 g looked like a speech fault and a real range mismatch passed as a re-ask (#90 review round 2,
    MEDIUM).
    """
    audits = [aud_on(c, qty_accuracy=("Fatal", 4)) for c in "abc"]
    turns = {c: turns_for(t4="bot_misheard") for c in "abc"}
    converted = {c: led(c, quantity=("1 kg", "1000 g")) for c in "abc"}
    assert [c.type for c in build_causes(audits, turns, converted)] == ["prompt"]  # 1 kg == 1000 g: the same value
    as_max = {c: led(c, quantity=("2-3 kg", "3 kg")) for c in "abc"}
    assert [c.type for c in build_causes(audits, turns, as_max)] == ["prompt"]  # a range is stored as its maximum
    ranges = {c: led(c, quantity=("2-3 kg", "2-9 kg")) for c in "abc"}
    assert build_causes(audits, turns, ranges)[0].type == "channel"  # the maxima 3 and 9 differ


def test_quantity_channel_falls_back_to_the_unit_slot_when_the_text_has_no_unit():
    """A quantity string with no unit must read that side's separate unit slot before converting.

    Buyer quantity '1' with unit slot kg and bot quantity '1000' with unit slot g are the same 1 kg. Without
    the slot fallback the bot side converted nothing (to_base treats a missing unit as no unit), so 1 looked
    like 1000 and a misheard re-ask became a speech fault (channel) instead of staying prompt-owned (#90
    review round 2, MEDIUM).
    """
    audits = [aud_on(c, qty_accuracy=("Fatal", 4)) for c in "abc"]
    turns = {c: turns_for(t4="bot_misheard") for c in "abc"}
    slots = {c: led(c, quantity=("1", "1000"), unit=("kg", "g")) for c in "abc"}
    assert [c.type for c in build_causes(audits, turns, slots)] == ["prompt"]  # 1 kg == 1000 g via the slots


def test_quantity_channel_with_a_unit_on_one_side_only_compares_the_raw_maxima():
    """'50 kg' vs '50' in either direction must not count as a quantity mismatch.

    rule_qty_accuracy converts the bot side only when both sides name a unit of the same kind, and with a unit
    on one side only it compares the raw numbers. Without that guard the bare number was converted as if it
    had no unit at all (50 vs 50000), so a correct read-back became a speech fault instead of staying
    prompt-owned, in either ledger direction (#90 review round 2, MEDIUM).
    """
    audits = [aud_on(c, qty_accuracy=("Fatal", 4)) for c in "abc"]
    turns = {c: turns_for(t4="bot_misheard") for c in "abc"}
    bot_bare = {c: led(c, quantity=("50 kg", "50")) for c in "abc"}
    assert [c.type for c in build_causes(audits, turns, bot_bare)] == ["prompt"]  # the bot dropped the unit
    buyer_bare = {c: led(c, quantity=("50", "50 kg")) for c in "abc"}
    assert [c.type for c in build_causes(audits, turns, buyer_bare)] == ["prompt"]  # the buyer side was bare


def test_unclear_user_turn_right_before_the_onset_is_a_channel_fault():
    """audio_unclear on the nearest user turn before the onset must make a channel root, an earlier one must not.

    Without it a bot answer that follows a noisy user turn would be blamed on the prompt.
    """
    turns = {"a": turns_for(t3="audio_unclear"), "b": turns_for(t1="audio_unclear"), "c": turns_for(t4="audio_unclear")}
    audits = [aud_on(c, looping=("NonFatal", 4)) for c in "abc"]
    kinds = {c.cause_id: c for c in build_causes(audits, turns)}
    assert kinds["cause_channel_looping"].nonfatal_calls == ["a", "c"]  # a: user turn 3 before onset 4. c: turn 4
    assert kinds["cause_looping"].nonfatal_calls == ["b"]  # turn 1 is an earlier user turn, not the one before


def test_only_the_root_can_be_channel_and_no_turns_means_no_channel():
    """A misheard turn at a symptom's onset must not make a channel cause, and missing turns must not either.

    Without it every later fault on a call with one noisy moment would be labelled channel, and a missing
    calls.jsonl would change the ranking.
    """
    audits = [aud_on(c, qty_missed=("Fatal", 2), looping=("Fatal", 8)) for c in "abc"]
    turns = {c: turns_for(t8="bot_misheard") for c in "abc"}  # flags the symptom only
    c = by_param(build_causes(audits, turns))
    assert c["qty_missed"].type == "prompt" and "looping" not in c
    flagged = {c: turns_for(t2="bot_misheard") for c in "abc"}
    wrong = {c: led(c, quantity=("30 kg", "3 kg")) for c in "abc"}
    assert build_causes(audits, flagged, wrong)[0].type == "channel"  # misheard onset captures a different value
    assert build_causes(audits, flagged, {c: led(c, quantity=("30 kg", "30 kg")) for c in "abc"})[0].type == "prompt"
    assert build_causes(audits, flagged)[0].type == "prompt"  # no ledger: rule (b) cannot show
    assert build_causes(audits, {})[0].type == "prompt" and build_causes(audits)[0].type == "prompt"
    no_onset = [aud_on(c, looping=("Fatal", None)) for c in "abc"]
    assert build_causes(no_onset, {c: turns_for(t0="bot_misheard") for c in "abc"})[0].type == "flow_logic"


def test_channel_cause_stays_watch_and_is_refused_by_fix(tmp_path):
    """classify_causes must keep a channel cause at watch, and require_fixable must refuse it even if marked ranked.

    Without it the three-call support and two-category tests would rank a bad-audio cause and refix could patch it.
    """
    audits = [aud_on(c, looping=("Fatal", 4)) for c in "abc"]
    causes = build_causes(audits, {c: turns_for(t3="audio_unclear") for c in "abc"})  # looping: rule (a) only
    out, outliers = classify_causes(causes, cat_map(a="food", b="services", c="other"), [SHIPPED])
    assert [(c.type, c.status) for c in out] == [("channel", "watch")]
    assert outliers[0]["condition"] == "channel" and outliers[0]["status"] == "watch"
    path = tmp_path / "causes.json"
    path.write_text(json.dumps({"causes": [out[0].model_dump()]}))
    with pytest.raises(SystemExit, match="channel"):
        require_fixable(out[0].cause_id, path)
    forced = {**out[0].model_dump(), "status": "ranked"}  # a hand-edited file must still not allow a patch
    path.write_text(json.dumps({"causes": [forced]}))
    with pytest.raises(SystemExit, match="channel"):
        require_fixable(out[0].cause_id, path)


def test_suspected_type_flags_tool_and_data_without_changing_status():
    """product_change must carry suspected_type tool, category_isq data and any other parameter an empty string.

    Without it the report could not tell the owner which causes may be tool or data faults, which need traces.
    """
    audits = [aud(f"{p}{i}", **{p: "Fatal"}) for p in ("product_change", "category_isq", "looping") for i in "abc"]
    c = by_param(build_causes(audits))
    assert (c["product_change"].suspected_type, c["category_isq"].suspected_type, c["looping"].suspected_type) == (
        "tool",
        "data",
        "",
    )
    assert c["product_change"].type == "prompt" and c["product_change"].status == "ranked"


def test_owner_and_evidence_follow_the_mapping_and_no_cause_is_proof():
    """Every parameter's cause must carry its documented owner, evidence and suspect, and no cause is proof.

    No trace fields exist yet, so an owner_evidence of proof would overclaim, and a swapped mapping would send a
    prompt fault to a data ticket or hide who owns a speech fault (#90).
    """
    audits = [aud(p, **{p: "Fatal"}) for p in KNOWN]  # one failing call per known parameter
    audits.append(aud("zeta", zeta="Fatal"))  # an unknown parameter falls back to the default owner
    leds = {p: led(p) for p in (*KNOWN, "zeta")}
    causes = {x.parameter: x for x in build_causes(audits, {p: turns_for() for p in leds}, leds)}
    assert causes["dead_air"].owner == "runtime" and causes["dead_air"].owner_evidence == "likely"
    assert causes["category_isq"].owner == "unresolved" and causes["category_isq"].owner_evidence == "missing"
    assert causes["category_isq"].suspect == "data" and causes["product_change"].suspect == "tool"
    assert causes["product_change"].owner == "unresolved" and causes["product_change"].owner_evidence == "missing"
    assert causes["qty_probing"].owner == "prompt"
    assert causes["qty_probing"].owner_note == "applicability assumed: unit list not visible"
    for p in ("looping", "qty_missed", "qty_accuracy", "qty_unit", "zeta"):
        assert (causes[p].owner, causes[p].owner_evidence, causes[p].suspect, causes[p].owner_note) == (
            "prompt",
            "likely",
            None,
            None,
        )
    assert all(x.owner_evidence != "proof" for x in causes.values())


def test_run_issues_reads_calls_and_writes_symptoms_and_the_onset_count(tmp_path):
    """run_issues must read calls.jsonl beside causes.json, write the channel split and report calls it cannot order.

    Without it the file path would ignore channel faults and the owner could not check the stop rule (how many
    calls have no onset order) on real runs.
    """
    audits = tmp_path / "audits.jsonl"
    rows = [aud_on(c, qty_missed=("Fatal", 4)) for c in "abc"]  # call a is misheard and captured a wrong value
    rows += [aud_on("d", qty_missed=("Fatal", 4), looping=("Fatal", 9))]
    rows += [aud_on("e", looping=("Fatal", None), dead_air=("Fatal", None))]  # cannot be ordered
    audits.write_text("\n".join(r.model_dump_json() for r in rows) + "\n")
    calls = [{"call_id": "a", "duration_sec": 9, "turns": [t.model_dump() for t in turns_for(t4="bot_misheard")]}]
    (tmp_path / "calls.jsonl").write_text("\n".join(json.dumps(c) for c in calls) + "\n")
    rows = [  # call a's ledger: buyer said 50, the bot captured 15, so the misheard re-ask is channel (#90)
        {
            "call_id": "a",
            "scope": "bl_collection",
            "buyer_facts": {"quantity": "50 kg"},
            "bot_confirmed": {"quantity": "15 kg"},
        },
    ]
    (tmp_path / "ledgers.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    causes = run_issues(audits, tmp_path / "causes.json")
    doc = json.loads((tmp_path / "causes.json").read_text())
    got = {c["cause_id"]: c for c in doc["causes"]}
    assert (
        got["cause_channel_qty_missed"]["fatal_calls"] == ["a"] and got["cause_channel_qty_missed"]["status"] == "watch"
    )
    assert (got["cause_channel_qty_missed"]["owner"], got["cause_qty_missed"]["owner_evidence"]) == (
        "speech",
        "likely",
    )  # causes.json carries the new fields (#90)
    assert got["cause_qty_missed"]["fatal_calls"] == ["b", "c", "d"]
    assert got["cause_looping"]["fatal_calls"] == ["e"]
    assert got["cause_qty_missed"]["symptom_calls"] == [] and len(causes) == len(doc["causes"])
    assert doc["calls_with_several_faults"] == 2 and doc["calls_with_several_roots"] == 1
