"""Rules graded against the real VANI prompt (issue #7). Synthetic calls only."""

from test_findings import bt, by, mk_call, mk_led

from vaniloop import ledger
from vaniloop.models import Ledger, UserTurnLabel
from vaniloop.rules import audit_call


def _loop(bots, n=40, **kw):
    call = mk_call(n)
    return by(audit_call(call, mk_led(call, bots, **kw)))["looping"]


def _spec(i, name="brand"):
    return bt(i, "spec", spec_name=name, fits_confirmed_product=True)


# ---- 1 per-slot limits
def test_spec_limit_is_2():
    """A named spec may be asked twice; the third ask must be a NonFatal loop with its onset at that turn.

    Without the limit the bot could keep asking the same spec and the audit would never flag it.
    """
    assert _loop([bt(0, "quantity"), _spec(2), _spec(4)]).status == "Pass"
    f = _loop([bt(0, "quantity"), _spec(2), _spec(4), _spec(6)])
    assert f.status == "NonFatal" and f.onset_turn == 6


def test_quantity_limit_is_3():
    """The bot may ask quantity three times; the fourth ask must be NonFatal at onset six.

    A missing limit would let repeated quantity asks escape the loop rule on a real call.
    """
    assert _loop([bt(i, "quantity") for i in (0, 2, 4)]).status == "Pass"
    f = _loop([bt(i, "quantity") for i in (0, 2, 4, 6)])
    assert f.status == "NonFatal" and f.onset_turn == 6


def test_product_limit_is_4():
    """The bot may ask the product four times; a fifth ask must be flagged as a loop.

    A weak limit would let a stuck product question never reach the prompt-fix stage.
    """
    assert _loop([bt(i, "product") for i in (0, 2, 4, 6)] + [bt(8, "quantity")]).status == "Pass"
    assert _loop([bt(i, "product") for i in (0, 2, 4, 6, 8)] + [bt(10, "quantity")]).onset_turn == 8


def test_name_and_city_limit_is_3():
    """Name and city asks are limited to three; a fourth ask must register as a loop.

    Without it the bot could demand a city forever and the call would still grade clean.
    """
    assert _loop([bt(0, "quantity")] + [bt(i, "city") for i in (2, 4, 6)]).status == "Pass"
    assert _loop([bt(0, "quantity")] + [bt(i, "name") for i in (2, 4, 6, 8)]).onset_turn == 8


def test_product_change_resets_product_quantity_spec_but_not_name_city():
    """A buyer product change must reset product, quantity and spec ask counts but not name or city.

    A wrong reset would either miss a real loop or flag a legitimate new-product collection.
    """
    # 3 quantity asks before the change and 3 after: each side is within the limit of 3
    bots = [bt(i, "quantity") for i in (0, 2, 4, 8, 10, 12)]
    assert _loop(bots, product_changed_at_turn=5).status == "Pass"
    assert _loop(bots).status == "NonFatal"  # without the change: 6 asks
    # specs: 2 before + 2 after the change is fine
    bots = [bt(0, "quantity"), _spec(2), _spec(4), _spec(8), _spec(10)]
    assert _loop(bots, product_changed_at_turn=5).status == "Pass"
    assert _loop(bots).status == "NonFatal"
    # name asks are not reset
    bots = [bt(0, "quantity")] + [bt(i, "name") for i in (2, 4, 8, 10)]
    assert _loop(bots, product_changed_at_turn=5).onset_turn == 10


# ---- 2 qty_missed: no read-back needed
def _missed(bots, users, kept=None):
    call = mk_call(12)
    bc = {"product": "pump", **({"quantity": kept} if kept else {})}
    led = mk_led(call, bots, users, buyer_facts={"quantity": "10"}, bot_confirmed=bc)
    return by(audit_call(call, led))["qty_missed"]


def test_qty_missed_passes_when_bot_moved_on_without_readback():
    """If the buyer gave a quantity and the bot moved on, qty_missed must Pass without a read-back.

    A miss here would mark every terse but correct call as a capture fault.
    """
    u = [UserTurnLabel(idx=1, gave_value_for="quantity")]
    assert _missed([bt(0, "quantity"), bt(2, "city")], u).status == "Pass"


def test_qty_missed_fatal_when_bot_asks_again_after_buyer_answer():
    """If the buyer gave a quantity and the bot asks again, qty_missed must be Fatal at the re-ask turn.

    A miss here would leave an ignored buyer answer ungraded in the audit.
    """
    u = [UserTurnLabel(idx=1, gave_value_for="quantity")]
    f = _missed([bt(0, "quantity"), bt(2, "quantity")], u)
    assert f.status == "Fatal" and f.onset_turn == 2


def test_qty_missed_nonfatal_when_the_buyer_repeats_after_the_reask():
    """A re-ask the buyer answers again is NonFatal at the re-ask turn: the quantity still reaches the lead.

    Found on 7 real calls where the bot misheard once and then captured the right value. As Fatal they
    inflated the Fatal rate against the human audit.
    """
    u = [UserTurnLabel(idx=1, gave_value_for="quantity"), UserTurnLabel(idx=3, gave_value_for="quantity")]
    f = _missed([bt(0, "quantity"), bt(2, "quantity"), bt(4, "city")], u, kept="10")
    assert f.status == "NonFatal" and f.onset_turn == 2


def test_qty_missed_fatal_when_any_reask_goes_unanswered():
    """Every re-ask is checked: an answered first re-ask must not hide a later re-ask the buyer never answered.

    Review found only the first re-ask was checked, so an ignored final ask graded NonFatal.
    """
    u = [UserTurnLabel(idx=1, gave_value_for="quantity"), UserTurnLabel(idx=3, gave_value_for="quantity")]
    f = _missed([bt(0, "quantity"), bt(2, "quantity"), bt(4, "quantity")], u, kept="10")
    assert f.status == "Fatal" and f.onset_turn == 4


def test_qty_missed_unknown_when_answered_but_nothing_was_captured():
    """A re-ask the buyer answers is downgraded only when a captured quantity is visible; otherwise Unknown.

    Review found a different or invalid answer with nothing captured became NonFatal, hiding a capture fault.
    """
    u = [UserTurnLabel(idx=1, gave_value_for="quantity"), UserTurnLabel(idx=3, gave_value_for="quantity")]
    assert _missed([bt(0, "quantity"), bt(2, "quantity"), bt(4, "city")], u).status == "Unknown"


def test_qty_missed_change_turn_starts_the_new_segment():
    """A quantity given in the same turn as the product change belongs to the new product.

    Review reproduced a Pass where an ignored re-ask for the new product followed that turn.
    """
    call = mk_call(12)
    led = mk_led(
        call,
        [bt(0, "quantity"), bt(4, "quantity")],
        [UserTurnLabel(idx=1, gave_value_for="quantity"), UserTurnLabel(idx=3, gave_value_for="quantity")],
        buyer_facts={"quantity": "10"},
        bot_confirmed={"product": "chair"},
        product_changed_at_turn=3,
    )
    assert by(audit_call(call, led))["qty_missed"].status == "Fatal"


def test_qty_missed_unknown_without_a_label_for_the_answer():
    """With no label marking a buyer answer as a quantity, qty_missed must be Unknown, not Pass or Fatal.

    Guessing either way would grade a call the audit could not actually read.
    """
    assert _missed([bt(0, "quantity")], []).status == "Unknown"


# ---- 3 range captured as its maximum
def _acc(said, kept):
    call = mk_call()
    led = mk_led(
        call, [bt(0, "quantity")], buyer_facts={"quantity": said}, bot_confirmed={"product": "pump", "quantity": kept}
    )
    return by(audit_call(call, led))["qty_accuracy"]


def test_range_max_is_pass_min_is_not():
    """A buyer quantity range must grade the captured maximum as Pass and the low or middle value as NonFatal.

    Grading the range wrongly would mark a correct maximum capture as a wrong quantity.
    """
    assert _acc("10 se 20 piece", "20").status == "Pass"
    assert _acc("10 se 20 piece", "10").status == "NonFatal"
    assert _acc("10 se 20 piece", "15").status == "NonFatal"


# ---- 4 variant
def _change_led(variant, later=("other",), n=7):
    """Bot opens (0), buyer changes the product (3), bot turns 4, 6, ... after the change carry the given slots."""
    call = mk_call(n)
    bots = [bt(0, "product")] + [bt(4 + 2 * i, s) for i, s in enumerate(later)]
    led = mk_led(
        call,
        bots,
        [UserTurnLabel(idx=3)],
        variant=variant,
        product_changed_at_turn=3,
        product_match=False,
        final_product="chair",
        bot_confirmed={"product": "pump"},
    )
    return call, led


def _pc(call, led):
    return by(audit_call(call, led))["product_change"].status


def test_enrich_decline_and_end_is_pass_for_product_change():
    """On an enrich call a product change followed by a decline and close must Pass.

    An unknown variant must not get that exemption, or the bot could ignore a real change.
    """
    assert _pc(*_change_led("enrich", ("confirm", "other"))) == "Pass"  # clarify, then close
    assert _pc(*_change_led("enrich", ("other",))) == "Pass"
    assert _pc(*_change_led("unknown", ("other",))) == "Fatal"


def test_enrich_clarifying_product_turn_keeps_exemption():
    """On an enrich call a clarifying product or confirm turn before closing keeps the product-change exemption.

    A miss here would mark a compliant enrich call faulty and send it to prompt fixing.
    """
    # L597-603: the bot first asks whether the old product is still needed (product/confirm), then closes
    assert _pc(*_change_led("enrich", ("product", "other"))) == "Pass"
    assert _pc(*_change_led("enrich", ("confirm", "other"))) == "Pass"


def test_enrich_but_bot_collects_is_not_pass():
    """On an enrich call where the buyer changed product, collecting quantity or spec must not Pass.

    A Pass here would let the bot keep filling an old requirement the buyer moved off.
    """
    assert _pc(*_change_led("enrich", ("quantity", "other"))) == "NonFatal"
    assert _pc(*_change_led("enrich", ("confirm", "spec"))) == "NonFatal"


def test_enrich_but_bot_hands_off_after_change_is_not_pass():
    """On an enrich call, handing off after the buyer changed product must be NonFatal, not Pass.

    A Pass here would send the buyer to a seller for the wrong product.
    """
    assert _pc(*_change_led("enrich", ("handoff",))) == "NonFatal"


def test_enrich_without_closing_evidence_is_not_pass():
    """An enrich call with a product change and no closing evidence must be Fatal.

    Otherwise the bot could keep the call running after the buyer moved on, unnoticed.
    """
    assert _pc(*_change_led("enrich", ("greeting",), n=6)) == "Fatal"  # bot said nothing after the change
    assert _pc(*_change_led("enrich", ("other",), n=14)) == "Fatal"  # call goes on long after the change


def test_variant_normalise_clamps():
    """The variant label must accept enrich, clamp bogus or missing values to unknown, and keep prompt version six.

    A wrong clamp would feed an unknown variant into rules and change the cached prompt key.
    """
    call = mk_call()
    assert ledger._normalise({"variant": "enrich"}, call)["variant"] == "enrich"
    assert ledger._normalise({"variant": "bogus"}, call)["variant"] == "unknown"
    assert ledger._normalise({}, call)["variant"] == "unknown"
    assert Ledger(call_id="c", scope="refusal").variant == "unknown"
    assert ledger.PROMPT_VERSION == "6"


# ---- 5 handoff needs a confirmed product
def _handoff(**kw):
    call = mk_call(4)
    led = mk_led(
        call,
        [bt(0, "product"), bt(2, "handoff")],
        scope="seller_unavailable_handoff",
        buyer_facts={},
        bot_confirmed=kw.pop("bot_confirmed", {}),
        product_match=kw.pop("product_match", None),
        **kw,
    )
    return audit_call(call, led)


def test_handoff_with_confirmed_product_passes():
    """On a handoff call with a confirmed product the product_change finding and verdict must Pass.

    A false fault here would mark valid seller-unavailable handoffs as broken calls.
    """
    a = _handoff(bot_confirmed={"product": "pump"}, final_product="pump")
    assert by(a)["product_change"].status == "Pass" and a.verdict == "Pass"


def test_handoff_without_product_evidence_is_unknown_not_pass():
    """A handoff with no confirmation evidence must be Unknown and force NeedsReview, not Pass.

    A final product alone is not evidence, or an unconfirmed handoff would look confirmed.
    """
    a = _handoff(final_product=None)
    f = by(a)["product_change"]
    assert f.status == "Unknown" and a.verdict == "NeedsReview"
    assert by(a)["qty_probing"].status == "NotApplicable"
    # a final_product alone (no confirmation evidence) is not enough
    assert by(_handoff(final_product="pump"))["product_change"].status == "Unknown"


def test_handoff_confirmation_seen_in_turns_passes():
    """A handoff where the confirmation is visible in the labelled turns must Pass.

    Ignoring the turn evidence would mark a real read-back as unconfirmed.
    """
    call = mk_call(4)
    led = mk_led(
        call,
        [bt(0, "confirm"), bt(2, "handoff")],
        [UserTurnLabel(idx=1, gave_value_for="product")],
        scope="seller_unavailable_handoff",
        buyer_facts={},
        bot_confirmed={},
        product_match=None,
    )
    assert by(audit_call(call, led))["product_change"].status == "Pass"


def test_handoff_wrong_product_is_nonfatal():
    """A handoff with the wrong product must be NonFatal at the handoff turn.

    A Pass here would send the buyer to a seller for the wrong product ungraded.
    """
    a = _handoff(bot_confirmed={"product": "pump"}, final_product="chair", product_match=False)
    f = by(a)["product_change"]
    assert f.status == "NonFatal" and a.verdict == "NonFatal" and f.onset_turn == 2


# ---- 6 unnamed spec asks cannot hide a loop
def _unnamed(i):
    return bt(i, "spec", spec_name=None, fits_confirmed_product=True)


def test_three_unnamed_spec_asks_are_unknown():
    """Three or more spec asks with no spec name must be Unknown.

    A Pass here would let an unnamed spec loop hide from the audit.
    """
    assert _loop([bt(0, "quantity"), _unnamed(2), _unnamed(4)]).status == "Pass"
    assert _loop([bt(0, "quantity"), _unnamed(2), _unnamed(4), _unnamed(6)]).status == "Unknown"
    assert _loop([bt(0, "quantity"), _unnamed(2), _unnamed(4), _unnamed(6)], n=40).status == "Unknown"


def test_named_plus_unnamed_spec_asks_are_unknown():
    """A mix of named and unnamed spec asks must be Unknown when the spec limit is breached.

    Otherwise an unnamed ask would let a real named loop slip through as a pass.
    """
    assert _loop([bt(0, "quantity"), _spec(2), _spec(4), _unnamed(6)]).status == "Unknown"
    assert _loop([bt(0, "quantity"), _spec(2), _unnamed(4)]).status == "Pass"  # 1 + 1 is within 2


def test_named_breach_still_wins_over_unnamed():
    """A named spec breach must stay NonFatal with its onset even when an unnamed ask follows.

    Losing the named breach would hide the real cause and misdirect the prompt fix.
    """
    f = _loop([bt(0, "quantity"), _spec(2), _spec(4), _spec(6), _unnamed(8)])
    assert f.status == "NonFatal" and f.onset_turn == 6


# ---- 7 qty_missed compares inside one product segment
def test_qty_missed_ignores_quantity_ask_for_a_new_product():
    """A quantity ask in a new product segment must not count as ignoring an old answer.

    Without segment awareness every product change would look like a qty_missed fault.
    """
    call = mk_call(12)
    led = mk_led(
        call,
        [bt(0, "quantity"), bt(6, "quantity")],
        [UserTurnLabel(idx=1, gave_value_for="quantity"), UserTurnLabel(idx=7, gave_value_for="quantity")],
        buyer_facts={"quantity": "10"},
        bot_confirmed={"product": "chair"},
        product_changed_at_turn=3,
    )
    assert by(audit_call(call, led))["qty_missed"].status == "Pass"
    led.product_changed_at_turn = None  # without the change the second ask is a re-ask; answered, nothing captured
    assert by(audit_call(call, led))["qty_missed"].status == "Unknown"


def test_qty_missed_still_fatal_when_reasked_inside_the_new_segment():
    """A re-ask inside the same new product segment must still be Fatal at that turn.

    A miss here would let an ignored buyer answer after a product change go ungraded.
    """
    call = mk_call(12)
    led = mk_led(
        call,
        [bt(0, "quantity"), bt(6, "quantity"), bt(8, "quantity")],
        [UserTurnLabel(idx=1, gave_value_for="quantity"), UserTurnLabel(idx=7, gave_value_for="quantity")],
        buyer_facts={"quantity": "10"},
        bot_confirmed={"product": "chair"},
        product_changed_at_turn=3,
    )
    f = by(audit_call(call, led))["qty_missed"]
    assert f.status == "Fatal" and f.onset_turn == 8


# ---- round 2
def test_handoff_buyer_correction_without_reconfirm_is_unknown():
    """Regression for review round 2 finding: on a handoff a buyer correction without re-confirmation must be Unknown.

    A Pass here would treat an uncorrected product as confirmed and send the wrong seller details.
    """
    call = mk_call(4)
    led = mk_led(
        call,
        [bt(0, "product"), bt(2, "handoff")],
        [UserTurnLabel(idx=1, gave_value_for="product")],
        scope="seller_unavailable_handoff",
        buyer_facts={},
        bot_confirmed={},
        product_match=None,
        product_changed_at_turn=1,
        final_product="chair",
    )
    assert by(audit_call(call, led))["product_change"].status == "Unknown"


def test_handoff_reconfirmed_after_correction_passes():
    """Regression for review round 2 finding: a handoff re-confirmed after a buyer correction must Pass.

    A false fault here would mark a correctly corrected handoff as a broken call.
    """
    call = mk_call(6)
    led = mk_led(
        call,
        [bt(0, "product"), bt(2, "confirm"), bt(4, "handoff")],
        [UserTurnLabel(idx=1, gave_value_for="product"), UserTurnLabel(idx=3, gave_value_for="product")],
        scope="seller_unavailable_handoff",
        buyer_facts={},
        bot_confirmed={},
        product_match=None,
        product_changed_at_turn=1,
        final_product="chair",
    )
    assert by(audit_call(call, led))["product_change"].status == "Pass"


def test_enrich_clarification_then_disconnect_is_unknown():
    """Regression for review round 2 finding: an enrich call that clarifies then disconnects is Unknown.

    Grading it Pass would treat a dropped call as a clean close of the product change.
    """
    assert _pc(*_change_led("enrich", ("confirm",), n=6)) == "Unknown"
    assert _pc(*_change_led("enrich", ("confirm", "other"))) == "Pass"


def test_enrich_other_before_clarification_is_not_a_close():
    """Regression for review round 2 finding: an 'other' turn before the clarification is not a valid close.

    Counting it as a close would grant the enrich exemption to a call that never closed.
    """
    assert _pc(*_change_led("enrich", ("other", "confirm"))) == "Unknown"


def test_qty_missed_captured_quantity_does_not_bypass_reask():
    """Regression for review round 2 finding: a captured quantity must not excuse a later re-ask.

    If it did, a bot that ignored the answer but stored it earlier would escape the fault.
    """
    call = mk_call(12)
    led = mk_led(
        call,
        [bt(0, "quantity"), bt(4, "quantity")],
        [UserTurnLabel(idx=1, gave_value_for="quantity")],
        buyer_facts={"quantity": "10"},
        bot_confirmed={"product": "pump", "quantity": "10"},
    )
    f = by(audit_call(call, led))["qty_missed"]
    assert f.status == "Fatal" and f.onset_turn == 4
    led.bot_turns[2].slot = "city"
    assert by(audit_call(call, led))["qty_missed"].status == "Pass"


# ---- round 3
def test_enrich_collection_after_change_is_nonfatal_even_if_product_matches():
    """Regression for review round 3 finding: collecting after an enrich product change is NonFatal even on a match.

    A product_match shortcut would otherwise Pass a call where the bot still collected the old requirement.
    """
    call, led = _change_led("enrich", ("quantity", "other"))
    led.product_match = True  # would otherwise fall through to Pass
    f = by(audit_call(call, led))["product_change"]
    assert f.status == "NonFatal" and f.onset_turn == 4


def test_handoff_confirmation_after_the_handoff_does_not_count():
    """Regression for review round 3 finding: a confirmation after the handoff turn must not count as confirmed.

    Counting later turns would let a handoff with no prior confirmation grade as a Pass.
    """
    call = mk_call(6)
    led = mk_led(
        call,
        [bt(0, "handoff"), bt(2, "confirm")],
        [UserTurnLabel(idx=3, gave_value_for="product")],
        scope="seller_unavailable_handoff",
        buyer_facts={},
        bot_confirmed={},
        product_match=None,
    )
    assert by(audit_call(call, led))["product_change"].status == "Unknown"
    led = mk_led(
        call,
        [bt(0, "confirm"), bt(2, "handoff")],
        [UserTurnLabel(idx=1, gave_value_for="product")],
        scope="seller_unavailable_handoff",
        buyer_facts={},
        bot_confirmed={},
        product_match=None,
    )
    assert by(audit_call(call, led))["product_change"].status == "Pass"
