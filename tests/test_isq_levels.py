"""Spec question level vs option level for category_isq (issue #46). Synthetic calls only, stub LLM, no network."""

from test_findings import bt, mk_call, mk_led

from vaniloop import ledger
from vaniloop.models import BotTurnLabel
from vaniloop.rules import audit_call, rule_category_isq


def _isq(*spec_labels):
    """Grade category_isq on a 12-turn call whose bot turns 2, 4, ... carry the given spec labels."""
    call = mk_call()
    bots = [bt(0, "quantity")] + [bt(2 * (i + 1), "spec", spec_name=f"s{i}", **kw) for i, kw in enumerate(spec_labels)]
    return call, mk_led(call, bots)


def test_wrong_spec_question_is_fatal():
    """A spec question that belongs to another category must grade Fatal at the question level.

    The human rubric fails a wrong question, so a miss here would pass a call that sends the buyer a drone question.
    """
    call, led = _isq({"fits_confirmed_product": False, "question_fits_product": False})
    f = rule_category_isq(call, led)
    assert f.status == "Fatal" and f.onset_turn == 2 and [e.turn_idx for e in f.evidence] == [2]
    assert "question level" in f.reason
    assert audit_call(call, led).verdict == "Fatal"
    call, led = _isq({"question_fits_product": False})  # the question label alone is enough
    assert rule_category_isq(call, led).status == "Fatal"


def test_fitting_question_with_wrong_options_is_nonfatal():
    """A fitting question with wrong options must grade NonFatal at the option level, with evidence and onset.

    The human rubric makes a wrong option NonFatal, so Fatal here would over-fail calls the owner grades NF.
    """
    call, led = _isq(
        {"fits_confirmed_product": True, "question_fits_product": True},
        {"fits_confirmed_product": False, "question_fits_product": True},
    )
    f = rule_category_isq(call, led)
    assert f.status == "NonFatal" and f.onset_turn == 4 and [e.turn_idx for e in f.evidence] == [4]
    assert "options did not" in f.reason
    assert audit_call(call, led).verdict == "NonFatal"


def test_old_ledger_without_question_label_is_unknown_not_fatal():
    """A ledger saved before question_fits_product, with fits false, must grade Unknown and NeedsReview, not Fatal.

    Old ledgers cannot separate a wrong question from wrong options, so a guess would invent a Fatal or hide one.
    """
    call, led = _isq({"fits_confirmed_product": False})
    f = rule_category_isq(call, led)
    assert f.status == "Unknown" and "cannot tell question level from option level" in f.reason
    assert [e.turn_idx for e in f.evidence] == [2]
    assert audit_call(call, led).verdict == "NeedsReview"


def test_wrong_question_outranks_option_level_and_old_label_turns():
    """One wrong question must make the call Fatal even when other spec turns are option-level or unlabelled.

    If a milder turn won, a call with a real wrong question would drop from Fatal to NonFatal or Unknown.
    """
    call, led = _isq(
        {"fits_confirmed_product": False, "question_fits_product": True},
        {"fits_confirmed_product": False},
        {"fits_confirmed_product": False, "question_fits_product": False},
    )
    f = rule_category_isq(call, led)
    assert f.status == "Fatal" and f.onset_turn == 6 and [e.turn_idx for e in f.evidence] == [6]


def test_unresolved_old_label_turn_outranks_option_level_turn():
    """An unresolved old-label turn must keep the rule Unknown even when another turn is option-level.

    The unresolved turn may be a wrong question, and NonFatal would hide a possible Fatal from review.
    """
    call, led = _isq(
        {"fits_confirmed_product": False, "question_fits_product": True},
        {"fits_confirmed_product": False},
    )
    f = rule_category_isq(call, led)
    assert f.status == "Unknown" and [e.turn_idx for e in f.evidence] == [4]


def test_fitting_questions_and_missing_fit_labels_keep_old_results():
    """Fitting spec turns must Pass, and a spec turn with no fit label must stay Unknown, as before this change.

    These branches did not change, so a shift here means the new levels leaked into the Pass and Unknown paths.
    """
    call, led = _isq({"fits_confirmed_product": True, "question_fits_product": True}, {"fits_confirmed_product": True})
    assert rule_category_isq(call, led).status == "Pass"
    call, led = _isq({"fits_confirmed_product": None})
    f = rule_category_isq(call, led)
    assert f.status == "Unknown" and "not labelled for fit" in f.reason


def _stub_extract(monkeypatch, reply, seen=None):
    """Run extract_ledger on a 6-turn call with a stub LLM that returns the given reply and records its inputs."""
    call = mk_call(6)

    def fake(system, user, **kw):
        if seen is not None:
            seen.update(user=user, **kw)
        return reply

    monkeypatch.setattr(ledger, "complete_json", fake)
    monkeypatch.setattr(ledger, "model_name", lambda: "stub-model")
    return ledger.extract_ledger(call)


def test_ledger_asks_for_and_parses_question_fits_product(monkeypatch):
    """The ledger prompt must ask for question_fits_product and the parser must keep it on spec turns only.

    If the prompt omitted it the audit would stay on old ledgers, and a value on a non-spec turn would mislead rules.
    """
    reply = {
        "scope": "bl_collection",
        "bot_turns": [
            {"idx": 0, "slot": "quantity", "question_fits_product": True},
            {
                "idx": 2,
                "slot": "spec",
                "spec_name": "Brand",
                "fits_confirmed_product": False,
                "question_fits_product": True,
            },
            {
                "idx": 4,
                "slot": "spec",
                "spec_name": "size",
                "fits_confirmed_product": False,
                "question_fits_product": False,
            },
        ],
    }
    seen: dict = {}
    led = _stub_extract(monkeypatch, reply, seen)
    assert '"question_fits_product"' in seen["user"]
    assert seen["cache_tag"] != "4"  # old cached ledgers have no question_fits_product and must not be reused
    labels = {b.idx: b for b in led.bot_turns}
    assert isinstance(labels[2], BotTurnLabel)
    assert (labels[2].fits_confirmed_product, labels[2].question_fits_product) == (False, True)
    assert (labels[4].fits_confirmed_product, labels[4].question_fits_product) == (False, False)
    assert labels[0].question_fits_product is None  # not a spec turn


def test_ledger_question_fits_product_is_none_when_missing_or_not_a_bool(monkeypatch):
    """A missing or non-bool question_fits_product on a spec turn must parse to None, never to a guessed bool.

    A guessed value would turn an old-style reply into a Fatal or NonFatal instead of Unknown.
    """
    reply = {
        "scope": "bl_collection",
        "bot_turns": [
            {"idx": 0, "slot": "spec", "spec_name": "brand", "fits_confirmed_product": False},
            {
                "idx": 2,
                "slot": "spec",
                "spec_name": "size",
                "fits_confirmed_product": False,
                "question_fits_product": "yes",
            },
        ],
    }
    led = _stub_extract(monkeypatch, reply)
    assert [b.question_fits_product for b in led.bot_turns] == [None, None]
    call = mk_call(6)
    assert rule_category_isq(call, mk_led(call, led.bot_turns)).status == "Unknown"
