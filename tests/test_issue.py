import json

import pytest

from vaniloop import categorize, gate
from vaniloop.issue import (
    NOT_REPRODUCED_PREFIX,
    UNKNOWN_CATEGORY,
    build_causes,
    classify_causes,
    run_issues,
    wilson,
)
from vaniloop.models import AuditRecord, Finding


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
    shipped = {**legacy, "decision": "ship"}  # a shipped row with old rate 0 is not demoted by the rate rule
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
