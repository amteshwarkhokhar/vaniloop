"""Intent rule (issue #96): a refusal call must not be collected into a lead. Synthetic calls only, no network."""

import pytest
from test_findings import bt, by, mk_call, mk_led

from vaniloop import issue, rules
from vaniloop.models import UserTurnLabel
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


def _u(*idxs, **kw):
    """User turn labels with the same fields for each idx."""
    return [UserTurnLabel(idx=i, **kw) for i in idxs]


def test_refusal_call_that_confirms_a_lead_is_fatal_with_onset():
    """A refusal call where the bot goes on to confirm a lead after the buyer declined must grade Fatal at the confirm.

    This is the buying-intent row of the matrix (always Fatal). A miss would let a lead be posted for a non-buyer.
    """
    call, led = _refusal(
        bt(0, "greeting"),
        bt(2, "other"),
        bt(4, "spec", spec_name="brand"),
        bt(6, "confirm"),
        users=_u(1, declined=True),
    )
    f = rule_intent(call, led)
    assert f.status == "Fatal" and f.onset_turn == 4 and f.basis == "spoken_proxy"
    assert [e.turn_idx for e in f.evidence] == [1, 4]
    a = audit_call(call, led)
    assert a.verdict == "Fatal" and a.first_fault_turn == 4 and by(a)["intent"].status == "Fatal"


@pytest.mark.parametrize("slot", ["quantity", "unit", "spec", "confirm", "handoff"])
def test_any_collecting_slot_after_the_decline_is_fatal(slot):
    """A quantity, unit, spec, confirm or handoff turn while the buyer stands declined must grade Fatal at that turn.

    If one slot were left out, a bot could collect through it and a refusal call would still pass.
    """
    call, led = _refusal(bt(4, slot), users=_u(3, declined=True))
    f = rule_intent(call, led)
    assert f.status == "Fatal" and f.onset_turn == 4


@pytest.mark.parametrize("slot", ["quantity", "unit", "spec"])
def test_an_ask_before_the_decline_is_not_a_fault(slot):
    """VANI's normal opening asks for a quantity, unit or spec before the buyer refuses. An ask before it must Pass.

    Grading it Fatal would mark most real refusal calls Fatal and rank intent first by mistake (verifier probe E2).
    """
    call, led = _refusal(bt(2, slot), bt(4, "other"), users=_u(5, declined=True))
    assert rule_intent(call, led).status == "Pass"


def test_codex_case_slot_confirm_of_a_decline_is_a_pass():
    """Regression for the Codex review: "Confirm you don't need this?" then "Correct" must Pass, not Fatal.

    The bot confirms the refusal itself, not a lead. The confirm turn comes before the declining "yes", so the old
    rule (any confirm after the first user turn) gave a false Fatal.
    """
    call, led = _refusal(bt(2, "confirm"), bt(4, "other"), users=_u(3, declined=True))
    f = rule_intent(call, led)
    assert f.status == "Pass" and f.onset_turn is None


def test_codex_case_collecting_after_two_refusals_is_fatal():
    """Regression for the Codex review: refuse, quantity ask, refuse again, unit ask, close must be Fatal at turn 2.

    The old rule saw no confirm or handoff and passed. The bot asked for a quantity right after the buyer declined.
    """
    call, led = _refusal(bt(2, "quantity"), bt(4, "unit"), bt(6, "other"), users=_u(1, 3, declined=True))
    f = rule_intent(call, led)
    assert f.status == "Fatal" and f.onset_turn == 2


def test_codex_case_late_cancel_then_handoff_is_fatal():
    """Regression for the Codex review: a quantity, a confirm, then a cancel and a handoff must be Fatal at the handoff.

    The old rule saw the quantity and gave Unknown, so a bot that handed off after "cancel" went to review only.
    """
    call, led = _refusal(
        bt(2, "confirm"),
        bt(4, "handoff"),
        users=[UserTurnLabel(idx=1, gave_value_for="quantity"), UserTurnLabel(idx=3, declined=True)],
    )
    f = rule_intent(call, led)
    assert f.status == "Fatal" and f.onset_turn == 4


def test_buyer_who_changes_their_mind_passes():
    """A decline, then a value, then a confirm must Pass, because the value renews the buyer's consent.

    A Fatal here would blame the bot for serving a buyer who now wants the item (verifier probe E1).
    """
    call, led = _refusal(
        bt(4, "other"),
        bt(6, "confirm"),
        users=[UserTurnLabel(idx=1, declined=True), UserTurnLabel(idx=5, gave_value_for="quantity")],
    )
    assert rule_intent(call, led).status == "Pass"


def test_decline_again_after_a_mind_change_is_fatal():
    """A decline, a value, a second decline and then a confirm must be Fatal at the confirm.

    If a value cleared the decline for good, a buyer who withdraws a second time would go unprotected.
    """
    call, led = _refusal(
        bt(6, "confirm"),
        users=[
            UserTurnLabel(idx=1, declined=True),
            UserTurnLabel(idx=3, gave_value_for="quantity"),
            UserTurnLabel(idx=5, declined=True),
        ],
    )
    f = rule_intent(call, led)
    assert f.status == "Fatal" and f.onset_turn == 6


def test_refusal_scope_without_a_declining_turn_is_unknown():
    """A refusal scope with no turn labelled declined must grade Unknown with no onset, not Pass or Fatal.

    The ledger is inconsistent, so a Pass would hide a possible bad lead and a Fatal would have no turn to blame.
    """
    call, led = _refusal(bt(4, "confirm"))
    f = rule_intent(call, led)
    assert f.status == "Unknown" and f.onset_turn is None and f.basis == "spoken_proxy"
    assert f.reason == "refusal scope but no declining turn is labelled"


def test_unknown_intent_does_not_hide_a_nonfatal_finding():
    """Intent Unknown next to a NonFatal finding must give verdict NonFatal, because the highest severity wins.

    If Unknown always forced NeedsReview, a call with a real NonFatal fault would drop out of the fix ranking.
    """
    brand = dict(spec_name="brand", fits_confirmed_product=True)
    call, led = _refusal(bt(2, "spec", **brand), bt(4, "spec", **brand), bt(6, "spec", **brand))
    a = audit_call(call, led)
    f = by(a)
    assert f["intent"].status == "Unknown" and f["looping"].status == "NonFatal"
    assert a.verdict == "NonFatal"


def test_refusal_call_that_closes_politely_passes():
    """A refusal call where the bot only greets, asks about the product, and closes must grade Pass for intent.

    Failing here would punish the correct behaviour, and every polite close would show as a Fatal cause.
    """
    call, led = _refusal(bt(0, "greeting"), bt(2, "product"), bt(4, "other"), users=_u(1, declined=True))
    f = rule_intent(call, led)
    assert f.status == "Pass" and f.basis == "spoken_proxy" and f.onset_turn is None
    a = audit_call(call, led)
    assert a.verdict == "Pass" and by(a)["intent"].status == "Pass"


def test_collecting_before_the_decline_is_not_a_refusal_fault():
    """A collecting bot turn before the buyer has declined must not count, because no refusal has been heard yet.

    Counting it would fail a call for something the bot said before the buyer could refuse.
    """
    call, led = _refusal(bt(0, "handoff"), bt(2, "other"), users=_u(1, declined=True))
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
    call, led = _refusal(bt(2, "quantity"), users=_u(1, declined=True))
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
