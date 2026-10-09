"""Intent rule (issue #96): a refusal call must not be collected into a lead. Synthetic calls only, no network."""

import pytest
from test_findings import bt, by, mk_call, mk_led

from vaniloop import issue, rules
from vaniloop.rules import audit_call, rule_intent


def _refusal(*bots, **kw):
    """A 12-turn refusal call: bot turns are even, user turns odd. The given bot labels replace the neutral ones."""
    call = mk_call()
    kw.setdefault("scope", "refusal")
    kw.setdefault("buyer_facts", {})
    kw.setdefault("bot_confirmed", {})
    kw.setdefault("final_product", None)
    kw.setdefault("product_match", None)
    return call, mk_led(call, list(bots), **kw)


def test_refusal_call_that_keeps_collecting_is_fatal_with_onset():
    """A refusal call where the bot asks for quantity after the buyer's first turn must grade Fatal at that bot turn.

    This is the buying-intent row of the matrix (always Fatal). A miss would let a lead be posted for a non-buyer.
    """
    call, led = _refusal(bt(0, "greeting"), bt(2, "quantity"), bt(4, "spec", spec_name="brand"), bt(6, "confirm"))
    f = rule_intent(call, led)
    assert f.status == "Fatal" and f.onset_turn == 2 and f.basis == "spoken_proxy"
    assert [e.turn_idx for e in f.evidence] == [2]
    a = audit_call(call, led)
    assert a.verdict == "Fatal" and a.first_fault_turn == 2 and by(a)["intent"].status == "Fatal"


@pytest.mark.parametrize("slot", ["quantity", "unit", "spec", "confirm", "handoff"])
def test_every_collecting_slot_counts_after_the_refusal(slot):
    """Each of quantity, unit, spec, confirm and handoff, spoken after the first user turn, must grade Fatal.

    If one slot were left out, a bot could collect through it and a refusal call would still pass.
    """
    call, led = _refusal(bt(4, slot))
    assert rule_intent(call, led).status == "Fatal" and rule_intent(call, led).onset_turn == 4


def test_refusal_call_that_closes_politely_passes():
    """A refusal call where the bot only greets, asks about the product, and closes must grade Pass for intent.

    Failing here would punish the correct behaviour, and every polite close would show as a Fatal cause.
    """
    call, led = _refusal(bt(0, "greeting"), bt(2, "product"), bt(4, "other"))
    f = rule_intent(call, led)
    assert f.status == "Pass" and f.basis == "spoken_proxy" and f.onset_turn is None
    a = audit_call(call, led)
    assert a.verdict == "Pass" and by(a)["intent"].status == "Pass"


def test_collecting_before_the_first_user_turn_is_not_a_refusal_fault():
    """A collecting bot turn before the buyer has spoken must not count, because no refusal has been heard yet.

    Counting it would fail a call for something the bot said before the buyer could refuse.
    """
    call, led = _refusal(bt(0, "quantity"), bt(2, "other"))
    assert rule_intent(call, led).status == "Pass"


@pytest.mark.parametrize("scope", ["bl_collection", "oncall_astbuy"])
def test_normal_lead_call_passes_intent(scope):
    """A lead call must grade Pass for intent even when the bot collects quantity, spec and handoff.

    Collecting is the job on a lead call. Grading it Fatal would flag every good call.
    """
    call = mk_call()
    led = mk_led(call, [bt(0, "quantity"), bt(2, "spec", spec_name="brand", fits_confirmed_product=True)], scope=scope)
    f = rule_intent(call, led)
    assert f.status == "Pass" and f.basis == "spoken_proxy"


@pytest.mark.parametrize("scope", ["seller_unavailable_handoff", "unclear"])
def test_other_scopes_are_not_applicable(scope):
    """A scope that is neither a lead call nor a refusal must grade NotApplicable for intent.

    A Pass there would claim a check that was never made, and a Fatal would blame a call that is not a lead.
    """
    call = mk_call()
    led = mk_led(call, [bt(2, "quantity")], scope=scope)
    assert rule_intent(call, led).status == "NotApplicable"


def test_intent_runs_as_the_ninth_rule_and_is_known_to_issue():
    """Every audit must carry one intent finding next to the eight older ones, and ISSUE must name the parameter.

    A rule left out of RULES never runs. A parameter left out of KNOWN would show with a generic name and type.
    """
    call, led = _refusal(bt(2, "quantity"))
    a = audit_call(call, led)
    assert rules.rule_intent in rules.RULES and len(a.findings) == len(rules.RULES) == 9
    assert [f.parameter for f in a.findings].count("intent") == 1
    name, _desc, typ = issue.KNOWN["intent"]
    assert typ == "prompt" and name != "intent"


def test_intent_does_not_change_older_findings_on_a_lead_call():
    """On a clean lead call the eight older findings must raise no fault and the verdict must stay Pass.

    The new rule must be additive. A change in an older finding would move existing verdicts and rankings.
    """
    call = mk_call()
    led = mk_led(call, [bt(0, "quantity")])
    a = audit_call(call, led)
    old = [f for f in a.findings if f.parameter != "intent"]
    assert len(old) == 8 and a.verdict == "Pass"
    assert not [f.parameter for f in a.findings if f.status in ("Fatal", "NonFatal")]
