"""Render the VANI prompt template the way the call saw it.

The real prompt is a Jinja template with every branch in it (live seller or not, quantity asked or not, ...). A
replay bot that gets the raw template reads all branches and may follow one the real call never took. `call_variables`
infers the values the call saw from its ledger, and `render` keeps only the branch those values select.
"""

from __future__ import annotations

import json

from jinja2.sandbox import SandboxedEnvironment

from .models import CallRecord, Ledger

# Sandboxed: a Fix patch is model-written text, so the template must not reach Python objects. Undefined variables
# render empty (Jinja's default); a syntax or render error raises.
_ENV = SandboxedEnvironment(keep_trailing_newline=True)


def call_variables(call: CallRecord, ledger: Ledger) -> dict[str, str]:
    """The prompt variables the call saw, inferred from its ledger. Every value is a string, as the platform sends it.

    `call` is part of the signature for variables read from the transcript; the current mapping needs only the ledger.
    """
    asked = {t.slot for t in ledger.bot_turns}  # the slots the bot asked about or handed off on
    spec_names = [t.spec_name for t in ledger.bot_turns if t.slot == "spec" and t.spec_name]
    return {
        # a seller handoff on an on-call AST call means the seller was live
        "ast_seller_pns": "LIVE" if "handoff" in asked and ledger.scope == "oncall_astbuy" else "",
        # the product the buyer named first
        "product_name": ledger.initial_product or "",
        # a bot turn that asks quantity or unit means the platform sent a unit list
        "quantity_unit_options": '["Piece"]' if asked & {"quantity", "unit"} else "[]",
        # the spec names the bot asked, each with no options (the ledger keeps no option lists)
        "specification_options": json.dumps({n: [] for n in spec_names}, ensure_ascii=False),
        # a bot turn that asks the name or the city means it was not on file
        "buyer_name": "" if "name" in asked else "BuyerA",
        "buyer_city": "" if "city" in asked else "CityA",
        # no ledger signal: the buyer has not corrected the name
        "updated_buyer_name": "",
        # the ledger's variant label
        "is_enrich": "1" if ledger.variant == "enrich" else "0",
        # no ledger signal for the counters and flags: the call-start value
        "identity_revealed": "0",
        "bl_converted": "0",
        "identity_challenge_count": "0",
        "off_topic_counter": "0",
        "quantity_asked_count": "0",
        "seller_ask_count": "0",
        # no ledger signal: empty or the call-start value
        "pcd_pitch": "",
        "triangulation_response_value": "0",
        "triangulation_response_city": "",
        # fixed: the VANI calls in scope are Hindi
        "language_name": "hindi",
    }


def render(template: str, variables: dict[str, str]) -> str:
    """Render the Jinja template. A variable that is not in `variables` renders empty. A template error raises."""
    return _ENV.from_string(template).render(**variables)
