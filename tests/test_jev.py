"""Jev judgement labels. Synthetic calls and a mocked transport: no network, no key."""

from __future__ import annotations

import json

import pytest

from vaniloop import jev, llm, pipeline
from vaniloop.models import BotTurnLabel, CallRecord, Ledger, Turn, UserTurnLabel
from vaniloop.rules import audit_call


def _call() -> CallRecord:
    texts = [
        ("bot", "Namaste, aap drone camera chahte hain?"),
        ("user", "Nahi, mujhe laptop chahiye."),
        ("bot", "Laptop ka brand kaunsa chahiye, DJI ya MI?"),
        ("user", "Maine kaha laptop, ab bas karo."),
        ("bot", "Kitni quantity chahiye?"),
    ]
    turns = [Turn(idx=i, speaker=s, start_sec=i * 3.0, end_sec=i * 3.0 + 2, text=t) for i, (s, t) in enumerate(texts)]
    return CallRecord(call_id="c1", duration_sec=15.0, turns=turns)


def _ledger() -> Ledger:
    return Ledger(
        call_id="c1",
        scope="bl_collection",
        initial_product="drone camera",
        final_product="laptop",
        product_changed_at_turn=1,
        product_match=True,  # Gemini's label, to be replaced
        buyer_facts={"quantity": "5"},
        bot_confirmed={"product": "drone camera", "quantity": "5"},
        bot_turns=[
            BotTurnLabel(idx=0, slot="product"),
            BotTurnLabel(
                idx=2,
                slot="spec",
                options_offered=["DJI", "MI"],
                fits_confirmed_product=True,
                question_fits_product=True,
                spec_name="brand",
            ),  # fmt: skip
            BotTurnLabel(idx=4, slot="quantity"),
        ],
        user_turns=[
            UserTurnLabel(idx=1, frustrated=0.0, declined=False, gave_value_for="product"),
            UserTurnLabel(idx=3, frustrated=0.0, declined=True),
        ],
        model="gemini",
    )


@pytest.fixture
def transport(monkeypatch, tmp_path):
    """Mock the Jev transport: `answers` maps a question id to a noul value (default 0.5), `bodies` records requests."""
    monkeypatch.setattr(llm, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key-not-real")
    monkeypatch.delenv("VANILOOP_CACHE", raising=False)
    state = {"answers": {}, "bodies": []}

    def fake(url, api_key, body):
        state["bodies"].append(body)
        ans = {i: {"type": "noul", "noul": state["answers"].get(i, 0.5)} for i in body["questions"]}
        return {"model": "jev-1", "answers": ans, "usage": {"input_tokens": 100, "output_tokens": 4}}

    monkeypatch.setattr(llm, "_jev_post", fake)
    return state


def test_jev_labels_replace_gemini_and_structure_is_kept(transport):
    """Protects the split of work: Jev sets the four judgement labels and nothing else changes.

    If it failed, Gemini's labels would still decide Fatals, or Jev would overwrite slots, values, declined or scope.
    """
    transport["answers"] = {"q2": 0.1, "o2": 0.2, "pm": 0.9, "f1": 0.3, "f3": 0.8}
    led, out = _ledger(), jev.judge_ledger(_call(), _ledger())
    spec = next(b for b in out.bot_turns if b.idx == 2)
    assert (spec.question_fits_product, spec.fits_confirmed_product) == (False, False)
    assert out.product_match is True
    assert [u.frustrated for u in out.user_turns] == [0.3, 0.8]
    keep = {
        "bot_turns": {"idx", "slot", "options_offered", "spec_name"},
        "user_turns": {"idx", "gave_value_for", "declined"},
    }
    for f in ("scope", "variant", "initial_product", "final_product", "product_changed_at_turn", "buyer_facts",
              "bot_confirmed", "call_id", "model"):  # fmt: skip
        assert getattr(out, f) == getattr(led, f)
    for f, keys in keep.items():
        assert [b.model_dump(include=keys) for b in getattr(out, f)] == [
            b.model_dump(include=keys) for b in getattr(led, f)
        ]
    assert led.product_match is True and led.user_turns[0].frustrated == 0.0  # the input is not mutated


def test_thresholds_and_unknown_product_change(transport):
    """Protects the 0.65 / 0.35 map and that a None label gives Unknown, never Pass.

    If it failed, a middling Jev answer would turn into a Pass or a Fatal.
    """
    assert (jev.to_bool(0.7), jev.to_bool(0.3), jev.to_bool(0.5)) == (True, False, None)
    assert (jev.to_bool(0.65), jev.to_bool(0.35)) == (True, False)
    transport["answers"] = {"pm": 0.5}
    out = jev.judge_ledger(_call(), _ledger())
    assert out.product_match is None
    finding = next(f for f in audit_call(_call(), out).findings if f.parameter == "product_change")
    assert finding.status == "Unknown"


def test_one_request_per_call_then_cached(transport):
    """Protects the cost: one request carries every question, and a rerun reads the disk cache.

    If it failed, a regrade or a resumed run would pay for Jev again.
    """
    jev.judge_ledger(_call(), _ledger())
    assert len(transport["bodies"]) == 1
    body = transport["bodies"][0]
    assert body["model"] == "jev-latest" and set(body) == {"state", "model", "questions"}
    jev.judge_ledger(_call(), _ledger())
    assert len(transport["bodies"]) == 1


def test_every_question_is_complete_on_its_own(transport):
    """Protects Jev's rule that question ids are never sent: each question names its product and quotes its turn.

    If it failed, Jev would answer a question about "it" with no product or no turn to read.
    """
    qs = jev.build_questions(_call(), _ledger())
    assert set(qs) == {"q2", "o2", "pm", "f1", "f3"}
    for qid, q in qs.items():
        assert q["type"] == "noul" and q["criteria"]["true"] and q["criteria"]["false"]
        assert qid not in q["instructions"]
    assert "laptop" in qs["q2"]["instructions"] and "Laptop ka brand kaunsa chahiye" in qs["q2"]["instructions"]
    assert "DJI, MI" in qs["o2"]["instructions"]
    assert "drone camera" in qs["pm"]["instructions"] and '"laptop"' in qs["pm"]["instructions"]
    assert "Nahi, mujhe laptop chahiye." in qs["f1"]["instructions"]
    assert (
        "Laptop ka brand kaunsa chahiye" in qs["f3"]["instructions"] and "Maine kaha laptop" in qs["f3"]["instructions"]
    )


def test_spec_turn_is_judged_against_the_product_after_the_change(transport):
    """Protects the product-change rule of the issue: a spec turn after the change turn uses the final product.

    If it failed, a laptop spec question would be judged against the drone the buyer dropped.
    """
    led = _ledger()
    led.bot_turns.insert(1, BotTurnLabel(idx=0, slot="spec", options_offered=[]))  # before the change at turn 1
    qs = jev.build_questions(_call(), led)
    assert 'wants the product "drone camera"' in qs["q0"]["instructions"]
    assert 'wants the product "laptop"' in qs["q2"]["instructions"]
    assert "o0" not in qs  # no options: the question alone is used for both labels


def test_missing_key_stops_with_the_variable_name(monkeypatch, tmp_path):
    """Protects the no-key behaviour: a cache miss without TYPESAFE_API_KEY exits and names the variable.

    If it failed, a run would crash with an opaque HTTP 401 or send an empty bearer token.
    """
    monkeypatch.setattr(llm, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(llm, "_jev_post", lambda *a: pytest.fail("network call"))
    with pytest.raises(SystemExit, match="TYPESAFE_API_KEY"):
        jev.judge_ledger(_call(), _ledger())


def test_cost_row_is_written(transport):
    """Protects the cost log: a Jev request writes a row with model, cache_tag, token counts and cost.

    If it failed, Jev spend would be invisible next to the Gemini rows in costs.jsonl.
    """
    jev.judge_ledger(_call(), _ledger())
    rows = [json.loads(x) for x in llm.COSTS_PATH.read_text().splitlines()]
    row = rows[-1]
    assert (row["model"], row["cache_tag"], row["prompt_tokens"], row["completion_tokens"]) == (
        "jev-latest",
        jev.JEV_VERSION,
        100,
        4,
    )
    assert row["cost_usd"] == 0.0 and row["status"] == "ok"


def test_judge_agreement_table(tmp_path):
    """Protects the agreement table: per-field counts, agreement and kappa on two synthetic ledger files.

    If it failed, the owner could not see where Jev and Gemini disagree before trusting Jev's Fatals.
    """

    def row(cid, pm, fit, fr):
        led = _ledger().model_copy(update={"call_id": cid, "product_match": pm})
        led.bot_turns[1] = led.bot_turns[1].model_copy(update={"question_fits_product": fit})
        led.user_turns[1] = led.user_turns[1].model_copy(update={"frustrated": fr})
        return led.model_dump(mode="json")

    a = [
        row("x1", True, True, 0.9),
        row("x2", False, False, 0.1),
        row("x3", True, True, 0.2),
        row("x4", False, None, 0.8),
    ]
    b = [
        row("x1", True, True, 0.7),
        row("x2", False, True, 0.0),
        row("x3", False, True, 0.3),
        row("x4", None, True, 0.9),
    ]
    rows = {r["field"]: r for r in jev.judge_agreement(a, b)}
    assert (rows["product_match"]["n"], rows["product_match"]["agree"], rows["product_match"]["none_b"]) == (3, 2, 1)
    assert rows["product_match"]["kappa"] == pytest.approx(0.4)
    assert (rows["question_fits_product"]["n"], rows["question_fits_product"]["agree"]) == (3, 2)
    fr = rows[f"frustrated>={jev.FRUSTRATED_P}"]
    assert (fr["n"], fr["agree"], fr["kappa"]) == (8, 8, 1.0)
    text = pipeline.describe_agreement(list(rows.values()))
    assert "product_match" in text and "0.40" in text


def test_judge_command_resumes(transport, tmp_path):
    """Protects `pipeline judge`: it writes ledgers_jev.jsonl and a second run skips every done call id.

    If it failed, a resumed overnight run would redo and repay every call.
    """
    calls, ledgers, out = tmp_path / "calls.jsonl", tmp_path / "ledgers.jsonl", tmp_path / "ledgers_jev.jsonl"
    calls.write_text(json.dumps(_call().model_dump(mode="json")) + "\n")
    ledgers.write_text(json.dumps(_ledger().model_dump(mode="json")) + "\n")
    kw = dict(calls_path=calls, ledgers_path=ledgers, out_path=out, errors_path=tmp_path / "e.jsonl", workers=1)
    assert pipeline.run_judge(**kw) == {"judged": 1, "skipped": 0, "errors": 0}
    assert pipeline.run_judge(**kw) == {"judged": 0, "skipped": 1, "errors": 0}
    assert len(out.read_text().splitlines()) == 1


def test_real_audit_fn_uses_jev_only_when_asked(transport, monkeypatch):
    """Protects the default path: prove's audit calls Jev only with VANILOOP_JUDGE=jev.

    If it failed, gate replays would silently use another judge than the main grade, or Jev would run unasked.
    """
    from vaniloop import prove

    monkeypatch.setattr(prove, "extract_ledger", lambda call: _ledger())
    monkeypatch.delenv("VANILOOP_JUDGE", raising=False)
    prove.real_audit_fn(_call())
    assert transport["bodies"] == []
    monkeypatch.setenv("VANILOOP_JUDGE", "jev")
    prove.real_audit_fn(_call())
    assert len(transport["bodies"]) == 1


def test_frustration_question_asks_for_irritation_not_repetition():
    """The irritation test (matrix: Fatal when the buyer gets irritated) must ask about irritation with the bot and
    say that repeated agreement words are not irritation. With "repeating themselves" in the question, Jev scored a
    plain "Haan, haan. Haan." at 0.73 on a real call, which would make most loops and calls Fatal or flagged."""
    call = CallRecord(
        call_id="syn_irritation",
        duration_sec=4,
        turns=[
            Turn(idx=0, speaker="bot", start_sec=0, end_sec=1, text="Aapko packing machine chahiye na?"),
            Turn(idx=1, speaker="user", start_sec=1, end_sec=2, text="Haan, haan. Haan."),
        ],
    )
    led = Ledger.model_validate(
        {
            "call_id": "syn_irritation",
            "scope": "bl_collection",
            "user_turns": [{"idx": 1}],
            "bot_turns": [{"idx": 0, "slot": "confirm"}],
        }
    )
    q = jev.build_questions(call, led)["f1"]
    text = q["instructions"].lower()
    assert "irritated or annoyed" in text and "repeating themselves" not in text and "haan haan" in text
    assert jev.JEV_VERSION != "1"
