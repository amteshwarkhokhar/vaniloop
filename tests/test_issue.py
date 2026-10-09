import pytest

from vaniloop.issue import build_causes, wilson
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
