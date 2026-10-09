"""render: prompt variables from the ledger and Jinja rendering. Synthetic ledgers and templates, no network."""

import pytest
from jinja2 import TemplateError

from vaniloop.models import BotTurnLabel, CallRecord, Ledger
from vaniloop.render import call_variables, render

CALL = CallRecord(call_id="c1", duration_sec=0, turns=[])


def ledger(*slots, scope="bl_collection", variant="main", product=None, spec_names=()):
    """A synthetic ledger: one bot turn per slot, then one spec turn per name in `spec_names`."""
    turns = [BotTurnLabel(idx=i, slot=s) for i, s in enumerate(slots)]
    turns += [BotTurnLabel(idx=len(turns) + i, slot="spec", spec_name=n) for i, n in enumerate(spec_names)]
    return Ledger(call_id="c1", scope=scope, variant=variant, initial_product=product, bot_turns=turns)


def test_a_call_with_no_bot_questions_gets_the_call_start_values():
    """An empty ledger must map to the call-start values: no live seller, no quantity list, name and city on file.

    These are the defaults for every call the audit saw nothing in. A wrong default would send the replay bot down
    a branch the real call never took.
    """
    assert call_variables(CALL, ledger()) == {
        "ast_seller_pns": "",
        "product_name": "",
        "quantity_unit_options": "[]",
        "specification_options": "{}",
        "buyer_name": "BuyerA",
        "buyer_city": "CityA",
        "updated_buyer_name": "",
        "is_enrich": "0",
        "identity_revealed": "0",
        "bl_converted": "0",
        "identity_challenge_count": "0",
        "off_topic_counter": "0",
        "quantity_asked_count": "0",
        "seller_ask_count": "0",
        "pcd_pitch": "",
        "triangulation_response_value": "0",
        "triangulation_response_city": "",
        "language_name": "hindi",
    }


def test_each_bot_question_and_label_changes_its_own_variable():
    """Each ledger signal must set its mapped variable: product, quantity, specs, name, city, handoff and variant.

    A mapping that ignored one signal would render the prompt as if the bot never asked it, and the replay bot would
    skip or repeat that question.
    """
    led = ledger(
        "quantity",
        "name",
        "city",
        "handoff",
        scope="oncall_astbuy",
        variant="enrich",
        product="CCTV camera",
        spec_names=("brand", "resolution"),
    )
    v = call_variables(CALL, led)
    assert v["product_name"] == "CCTV camera"
    assert v["ast_seller_pns"] == "LIVE"
    assert v["quantity_unit_options"] == '["Piece"]'
    assert v["specification_options"] == '{"brand": [], "resolution": []}'
    assert v["buyer_name"] == "" and v["buyer_city"] == ""
    assert v["is_enrich"] == "1"
    assert v["updated_buyer_name"] == "" and v["language_name"] == "hindi"


def test_handoff_means_a_live_seller_only_on_an_oncall_ast_call():
    """ast_seller_pns must be LIVE only for a handoff turn together with the oncall_astbuy scope.

    A handoff on a seller-unavailable call, or an oncall_astbuy call without a handoff, had no live seller. Marking
    them LIVE would show the replay bot the live-seller branch, which skips the quantity question.
    """
    live = call_variables(CALL, ledger("handoff", scope="oncall_astbuy"))["ast_seller_pns"]
    other_scope = call_variables(CALL, ledger("handoff", scope="seller_unavailable_handoff"))["ast_seller_pns"]
    no_handoff = call_variables(CALL, ledger("quantity", scope="oncall_astbuy"))["ast_seller_pns"]
    assert (live, other_scope, no_handoff) == ("LIVE", "", "")


def test_a_unit_question_alone_also_sends_the_unit_list():
    """A bot turn with slot unit and none with slot quantity must still give the unit list.

    The bot may ask the unit alone after the buyer stated a number. Without the list the rendered prompt would take
    the branch for a platform that sent no unit options.
    """
    assert call_variables(CALL, ledger("unit"))["quantity_unit_options"] == '["Piece"]'
    assert call_variables(CALL, ledger("name"))["quantity_unit_options"] == "[]"


def test_spec_names_are_kept_once_in_order_and_unnamed_spec_turns_are_skipped():
    """specification_options must list each asked spec name once, in order, with no escaping, and skip unnamed turns.

    A repeated or None key would put a bad entry into the JSON object, and \\u escapes would change the text the
    prompt shows for a non-ASCII spec name.
    """
    led = ledger(spec_names=("brand", "रंग", "brand", None))
    assert call_variables(CALL, led)["specification_options"] == '{"brand": [], "रंग": []}'


TEMPLATE = (
    "{% if ast_seller_pns == 'LIVE' %}SELLER-LIVE{% elif is_enrich == '1' %}ENRICH{% else %}NO-SELLER{% endif %}\n"
    "{% if quantity_unit_options != '[]' %}ASK-QTY {{ quantity_unit_options }}{% else %}SKIP-QTY{% endif %}\n"
    "Hello {{ buyer_name }} from {{ buyer_city }}, product {{ product_name }}, "
    "{% if identity_challenge_count|int > 2 %}FIRM{% else %}SOFT{% endif %}"
)


def test_only_the_branch_the_call_saw_is_rendered():
    """A call with a live seller and a quantity question must render only those branches, and a call with neither must
    render the other two. The unrendered template shows the replay bot every branch, so it can take the live-seller
    branch on a call that had no live seller and skip the quantity question."""
    live = ledger("quantity", "name", "city", "handoff", scope="oncall_astbuy", product="CCTV camera")
    out = render(TEMPLATE, call_variables(CALL, live))
    assert "SELLER-LIVE" in out and "ASK-QTY" in out and 'ASK-QTY ["Piece"]' in out
    assert "NO-SELLER" not in out and "ENRICH" not in out and "SKIP-QTY" not in out
    assert "Hello  from , product CCTV camera, SOFT" in out  # name and city were asked, so they are empty

    none = render(TEMPLATE, call_variables(CALL, ledger("greeting")))
    assert "NO-SELLER" in none and "SKIP-QTY" in none
    assert "SELLER-LIVE" not in none and "ASK-QTY" not in none and "FIRM" not in none
    assert "Hello BuyerA from CityA" in none

    enrich = render(TEMPLATE, call_variables(CALL, ledger(variant="enrich")))
    assert "ENRICH" in enrich and "NO-SELLER" not in enrich and "SELLER-LIVE" not in enrich


def test_unknown_variables_render_empty_and_plain_text_is_unchanged():
    """A variable the mapping lacks must render empty, and a text with no tags must come back byte for byte.

    The real prompt uses a few variables the ledger cannot supply. A raise there would stop every proof run, and a
    dropped trailing newline would change the prompt tag of a template with no tags.
    """
    assert render("a {{ missing }} b {% if missing %}X{% else %}Y{% endif %}", {}) == "a  b Y"
    assert render("line 1\nline 2\n", {"buyer_name": "x"}) == "line 1\nline 2\n"


def test_a_template_error_raises_and_the_template_cannot_reach_python_internals():
    """A broken tag must raise, not render half a prompt, and a tag must not reach Python objects.

    A Fix patch is model-written text. Rendering a half prompt would grade a bot that never saw the whole prompt,
    and an open template would run code from a patch.
    """
    with pytest.raises(TemplateError):
        render("{% if %}", {})
    with pytest.raises(TemplateError):
        render("{{ ''.__class__.__mro__[1].__subclasses__() }}", {})
