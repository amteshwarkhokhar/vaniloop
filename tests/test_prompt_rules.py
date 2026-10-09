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
    assert _loop([bt(0, "quantity"), _spec(2), _spec(4)]).status == "Pass"
    f = _loop([bt(0, "quantity"), _spec(2), _spec(4), _spec(6)])
    assert f.status == "NonFatal" and f.onset_turn == 6


def test_quantity_limit_is_3():
    assert _loop([bt(i, "quantity") for i in (0, 2, 4)]).status == "Pass"
    f = _loop([bt(i, "quantity") for i in (0, 2, 4, 6)])
    assert f.status == "NonFatal" and f.onset_turn == 6


def test_product_limit_is_4():
    assert _loop([bt(i, "product") for i in (0, 2, 4, 6)] + [bt(8, "quantity")]).status == "Pass"
    assert _loop([bt(i, "product") for i in (0, 2, 4, 6, 8)] + [bt(10, "quantity")]).onset_turn == 8


def test_name_and_city_limit_is_3():
    assert _loop([bt(0, "quantity")] + [bt(i, "city") for i in (2, 4, 6)]).status == "Pass"
    assert _loop([bt(0, "quantity")] + [bt(i, "name") for i in (2, 4, 6, 8)]).onset_turn == 8


def test_product_change_resets_product_quantity_spec_but_not_name_city():
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
    u = [UserTurnLabel(idx=1, gave_value_for="quantity")]
    assert _missed([bt(0, "quantity"), bt(2, "city")], u).status == "Pass"


def test_qty_missed_fatal_when_bot_asks_again_after_buyer_answer():
    u = [UserTurnLabel(idx=1, gave_value_for="quantity")]
    f = _missed([bt(0, "quantity"), bt(2, "quantity")], u)
    assert f.status == "Fatal" and f.onset_turn == 2


def test_qty_missed_unknown_without_a_label_for_the_answer():
    assert _missed([bt(0, "quantity")], []).status == "Unknown"


# ---- 3 range captured as its maximum
def _acc(said, kept):
    call = mk_call()
    led = mk_led(call, [bt(0, "quantity")], buyer_facts={"quantity": said}, bot_confirmed={"product": "pump", "quantity": kept})
    return by(audit_call(call, led))["qty_accuracy"]


def test_range_max_is_pass_min_is_not():
    assert _acc("10 se 20 piece", "20").status == "Pass"
    assert _acc("10 se 20 piece", "10").status == "NonFatal"
    assert _acc("10 se 20 piece", "15").status == "NonFatal"


# ---- 4 variant
def _change_led(variant, later=("other",), n=7):
    """Bot opens (0), buyer changes the product (3), bot turns 4, 6, ... after the change carry the given slots."""
    call = mk_call(n)
    bots = [bt(0, "product")] + [bt(4 + 2 * i, s) for i, s in enumerate(later)]
    led = mk_led(call, bots, [UserTurnLabel(idx=3)], variant=variant, product_changed_at_turn=3,
                 product_match=False, final_product="chair", bot_confirmed={"product": "pump"})
    return call, led


def _pc(call, led):
    return by(audit_call(call, led))["product_change"].status


def test_enrich_decline_and_end_is_pass_for_product_change():
    assert _pc(*_change_led("enrich", ("confirm", "other"))) == "Pass"  # clarify, then close
    assert _pc(*_change_led("enrich", ("other",))) == "Pass"
    assert _pc(*_change_led("unknown", ("other",))) == "Fatal"


def test_enrich_clarifying_product_turn_keeps_exemption():
    # L597-603: the bot first asks whether the old product is still needed (product/confirm), then closes
    assert _pc(*_change_led("enrich", ("product", "other"))) == "Pass"
    assert _pc(*_change_led("enrich", ("confirm", "other"))) == "Pass"


def test_enrich_but_bot_collects_is_not_pass():
    assert _pc(*_change_led("enrich", ("quantity", "other"))) == "Fatal"
    assert _pc(*_change_led("enrich", ("confirm", "spec"))) == "Fatal"


def test_enrich_but_bot_hands_off_after_change_is_not_pass():
    assert _pc(*_change_led("enrich", ("handoff",))) == "Fatal"


def test_enrich_without_closing_evidence_is_not_pass():
    assert _pc(*_change_led("enrich", ("greeting",), n=6)) == "Fatal"  # bot said nothing after the change
    assert _pc(*_change_led("enrich", ("other",), n=14)) == "Fatal"  # call goes on long after the change


def test_variant_normalise_clamps():
    call = mk_call()
    assert ledger._normalise({"variant": "enrich"}, call)["variant"] == "enrich"
    assert ledger._normalise({"variant": "bogus"}, call)["variant"] == "unknown"
    assert ledger._normalise({}, call)["variant"] == "unknown"
    assert Ledger(call_id="c", scope="refusal").variant == "unknown"
    assert ledger.PROMPT_VERSION == "4"


# ---- 5 handoff needs a confirmed product
def _handoff(**kw):
    call = mk_call(4)
    led = mk_led(call, [bt(0, "product"), bt(2, "handoff")], scope="seller_unavailable_handoff", buyer_facts={},
                 bot_confirmed=kw.pop("bot_confirmed", {}), product_match=kw.pop("product_match", None), **kw)
    return audit_call(call, led)


def test_handoff_with_confirmed_product_passes():
    a = _handoff(bot_confirmed={"product": "pump"}, final_product="pump")
    assert by(a)["product_change"].status == "Pass" and a.verdict == "Pass"


def test_handoff_without_product_evidence_is_unknown_not_pass():
    a = _handoff(final_product=None)
    f = by(a)["product_change"]
    assert f.status == "Unknown" and a.verdict == "NeedsReview"
    assert by(a)["qty_probing"].status == "NotApplicable"
    # a final_product alone (no confirmation evidence) is not enough
    assert by(_handoff(final_product="pump"))["product_change"].status == "Unknown"


def test_handoff_confirmation_seen_in_turns_passes():
    call = mk_call(4)
    led = mk_led(call, [bt(0, "confirm"), bt(2, "handoff")], [UserTurnLabel(idx=1, gave_value_for="product")],
                 scope="seller_unavailable_handoff", buyer_facts={}, bot_confirmed={}, product_match=None)
    assert by(audit_call(call, led))["product_change"].status == "Pass"


def test_handoff_wrong_product_is_nonfatal():
    a = _handoff(bot_confirmed={"product": "pump"}, final_product="chair", product_match=False)
    f = by(a)["product_change"]
    assert f.status == "NonFatal" and a.verdict == "NonFatal" and f.onset_turn == 2


# ---- 6 unnamed spec asks cannot hide a loop
def _unnamed(i):
    return bt(i, "spec", spec_name=None, fits_confirmed_product=True)


def test_three_unnamed_spec_asks_are_unknown():
    assert _loop([bt(0, "quantity"), _unnamed(2), _unnamed(4)]).status == "Pass"
    assert _loop([bt(0, "quantity"), _unnamed(2), _unnamed(4), _unnamed(6)]).status == "Unknown"
    assert _loop([bt(0, "quantity"), _unnamed(2), _unnamed(4), _unnamed(6)], n=40).status == "Unknown"


def test_named_plus_unnamed_spec_asks_are_unknown():
    assert _loop([bt(0, "quantity"), _spec(2), _spec(4), _unnamed(6)]).status == "Unknown"
    assert _loop([bt(0, "quantity"), _spec(2), _unnamed(4)]).status == "Pass"  # 1 + 1 is within 2


def test_named_breach_still_wins_over_unnamed():
    f = _loop([bt(0, "quantity"), _spec(2), _spec(4), _spec(6), _unnamed(8)])
    assert f.status == "NonFatal" and f.onset_turn == 6


# ---- 7 qty_missed compares inside one product segment
def test_qty_missed_ignores_quantity_ask_for_a_new_product():
    call = mk_call(12)
    led = mk_led(call, [bt(0, "quantity"), bt(6, "quantity")],
                 [UserTurnLabel(idx=1, gave_value_for="quantity"), UserTurnLabel(idx=7, gave_value_for="quantity")],
                 buyer_facts={"quantity": "10"}, bot_confirmed={"product": "chair"}, product_changed_at_turn=3)
    assert by(audit_call(call, led))["qty_missed"].status == "Pass"
    led.product_changed_at_turn = None  # without the change the second ask looks like an ignored answer
    assert by(audit_call(call, led))["qty_missed"].status == "Fatal"


def test_qty_missed_still_fatal_when_reasked_inside_the_new_segment():
    call = mk_call(12)
    led = mk_led(call, [bt(0, "quantity"), bt(6, "quantity"), bt(8, "quantity")],
                 [UserTurnLabel(idx=1, gave_value_for="quantity"), UserTurnLabel(idx=7, gave_value_for="quantity")],
                 buyer_facts={"quantity": "10"}, bot_confirmed={"product": "chair"}, product_changed_at_turn=3)
    f = by(audit_call(call, led))["qty_missed"]
    assert f.status == "Fatal" and f.onset_turn == 8
