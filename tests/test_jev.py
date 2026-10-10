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
    assert "laptop" in qs["q2"]["instructions"] and '"brand"' in qs["q2"]["instructions"]  # the spec name (#113)
    assert "Laptop ka brand kaunsa chahiye" in qs["o2"]["instructions"]
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
    assert '"drone camera"' in qs["q0"]["instructions"] and '"laptop"' not in qs["q0"]["instructions"]
    assert '"laptop"' in qs["q2"]["instructions"] and '"drone camera"' not in qs["q2"]["instructions"]
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


def test_unsure_question_fit_leaves_fits_confirmed_unjudged(transport):
    """Protects category_isq: when the question-alone answer is unsure, fits_confirmed_product is None too.

    If it failed, (None, True) would reach the rule and a spec turn nobody judged would Pass.
    """
    transport["answers"] = {"q2": 0.5, "o2": 0.9, "pm": 0.9}
    spec = next(b for b in jev.judge_ledger(_call(), _ledger()).bot_turns if b.idx == 2)
    assert (spec.question_fits_product, spec.fits_confirmed_product) == (None, None)


def test_handoff_with_unjudged_product_match_is_unknown():
    """Protects the handoff product_change rule: a confirmed product with product_match None is Unknown.

    If it failed, a Jev that was unsure would turn a possibly wrong handoff product into a Pass.
    """
    led = _ledger().model_copy(
        update={"scope": "seller_unavailable_handoff", "product_match": None, "bot_confirmed": {"product": "pump"}}
    )
    led.bot_turns.append(BotTurnLabel(idx=3, slot="handoff"))
    f = next(f for f in audit_call(_call(), led).findings if f.parameter == "product_change")
    assert (f.status, f.basis, f.reason) == ("Unknown", "spoken_proxy", "product match not judged")


def test_key_never_appears_in_errors_or_cost_rows(monkeypatch, transport):
    """Protects the secret: a failing request reports the error type and HTTP status only, never the key.

    If it failed, a urllib or proxy message carrying the Authorization header would land in logs and costs.jsonl.
    """
    key = "sk-secret-abc123"
    monkeypatch.setenv("TYPESAFE_API_KEY", key)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)

    class Boom(Exception):
        code = 500

    def fail(url, api_key, body):
        raise Boom(f"Authorization: Bearer {api_key} rejected")

    monkeypatch.setattr(llm, "_jev_post", fail)
    with pytest.raises(RuntimeError) as e:
        jev.judge_ledger(_call(), _ledger())
    assert key not in str(e.value) and "Boom HTTP 500" in str(e.value)
    assert key not in llm.COSTS_PATH.read_text() and "Boom HTTP 500" in llm.COSTS_PATH.read_text()


def test_malformed_key_stops_without_echoing_it(monkeypatch, transport):
    """Protects the secret: a key with a newline inside exits with a fixed message and sends nothing.

    If it failed, the HTTP library would raise an error that quotes the header value.
    """
    monkeypatch.setenv("TYPESAFE_API_KEY", "sk-part1\nsk-part2")
    transport["bodies"].clear()
    with pytest.raises(SystemExit) as e:
        jev.judge_ledger(_call(), _ledger())
    assert str(e.value) == "TYPESAFE_API_KEY is malformed (whitespace inside)"
    assert "sk-part" not in str(e.value) and transport["bodies"] == []


def test_judge_resume_redoes_a_stale_row(transport, tmp_path, monkeypatch):
    """Protects resume: a row is skipped only when its jev_fp matches, and VANILOOP_CACHE=refresh never skips.

    If it failed, a changed question or JEV_VERSION would keep old judgements in ledgers_jev.jsonl.
    """
    calls, ledgers, out = tmp_path / "calls.jsonl", tmp_path / "ledgers.jsonl", tmp_path / "ledgers_jev.jsonl"
    calls.write_text(json.dumps(_call().model_dump(mode="json")) + "\n")
    ledgers.write_text(json.dumps(_ledger().model_dump(mode="json")) + "\n")
    kw = dict(calls_path=calls, ledgers_path=ledgers, out_path=out, errors_path=tmp_path / "e.jsonl", workers=1)
    pipeline.run_judge(**kw)
    assert json.loads(out.read_text())["jev_fp"]
    assert pipeline.run_judge(**kw)["skipped"] == 1
    monkeypatch.setattr(jev, "JEV_VERSION", "99")
    assert pipeline.run_judge(**kw) == {"judged": 1, "skipped": 0, "errors": 0}
    monkeypatch.setenv("VANILOOP_CACHE", "refresh")
    assert pipeline.run_judge(**kw)["judged"] == 1


def test_spec_product_uses_only_its_own_segment():
    """Protects the product-change rule: before the change only initial_product counts, after it only final_product.

    If it failed, a missing product would borrow the other segment's and judge a turn against the wrong product.
    """
    led = _ledger()
    assert (jev.spec_product(led, 0), jev.spec_product(led, 2)) == ("drone camera", "laptop")
    no_initial = led.model_copy(update={"initial_product": None})
    assert jev.spec_product(no_initial, 0) is None and jev.spec_product(no_initial, 2) == "laptop"
    no_final = led.model_copy(update={"final_product": None})
    assert jev.spec_product(no_final, 2) is None and jev.spec_product(no_final, 0) == "drone camera"
    unchanged = led.model_copy(update={"product_changed_at_turn": None})
    assert jev.spec_product(unchanged, 2) == "drone camera"
    led.bot_turns.insert(1, BotTurnLabel(idx=0, slot="spec"))
    assert "q0" not in jev.build_questions(_call(), no_initial)


def test_identical_replies_give_different_questions():
    """Protects the questions: two identical buyer replies are named by idx, so their instructions differ.

    If it failed, Jev would get two identical questions and could not tell which turn each one is about.
    """
    texts = ["Kya chahiye?", "Pump", "Kya chahiye?", "Pump"]
    turns = [
        Turn(idx=i, speaker="bot" if i % 2 == 0 else "user", start_sec=i, end_sec=i + 1, text=t)
        for i, t in enumerate(texts)
    ]
    call = CallRecord(call_id="d", duration_sec=5.0, turns=turns)
    led = Ledger(call_id="d", scope="bl_collection", user_turns=[UserTurnLabel(idx=1), UserTurnLabel(idx=3)])
    qs = jev.build_questions(call, led)
    assert qs["f1"]["instructions"] != qs["f3"]["instructions"]
    assert "idx 1" in qs["f1"]["instructions"] and "idx 0" in qs["f1"]["instructions"]
    assert "idx 3" in qs["f3"]["instructions"] and "idx 2" in qs["f3"]["instructions"]
    assert "sales bot turn with idx 2" in jev.build_questions(_call(), _ledger())["q2"]["instructions"]


@pytest.mark.parametrize("bad", [2, -1, float("nan")])
def test_invalid_probability_is_rejected_and_never_cached(transport, monkeypatch, bad):
    """Protects the labels: a noul outside [0, 1] or NaN raises, and is neither returned nor cached.

    If it failed, 2 would count as True, -1 as False, and a bad answer would be replayed from the cache.
    """
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    transport["answers"] = {"pm": bad}
    with pytest.raises(RuntimeError):
        jev.judge_ledger(_call(), _ledger())
    assert not llm.CACHE_DIR.exists() or not list(llm.CACHE_DIR.glob("*.json"))


def test_invalid_cached_probability_is_refetched(transport):
    """Protects the cache: a cached answer outside [0, 1] counts as a miss and Jev is asked again.

    If it failed, an old bad cache file would keep feeding 2 or NaN into the labels.
    """
    jev.judge_ledger(_call(), _ledger())
    f = next(llm.CACHE_DIR.glob("*.json"))
    d = json.loads(f.read_text())
    d["answers"]["pm"] = 2
    f.write_text(json.dumps(d))
    jev.judge_ledger(_call(), _ledger())
    assert len(transport["bodies"]) == 2


def test_agreement_excludes_missing_frustrated():
    """Protects the agreement table: a missing frustrated value is excluded from the pair, not counted as False.

    If it failed, a ledger without the value would show false agreement or disagreement on frustration.
    """
    a, b = _ledger().model_dump(mode="json"), _ledger().model_dump(mode="json")
    a["user_turns"][0]["frustrated"] = None
    a["user_turns"][1]["frustrated"] = 0.9
    b["user_turns"][0]["frustrated"] = 0.0
    b["user_turns"][1]["frustrated"] = 0.9
    row = next(r for r in jev.judge_agreement([a], [b]) if r["field"].startswith("frustrated"))
    assert (row["n"], row["agree"], row["none_a"]) == (1, 1, 1)


def test_judge_redoes_a_row_whose_gemini_ledger_changed(transport, tmp_path):
    """Protects resume: the judged row copies Gemini's structural fields, so a changed source ledger must redo it.

    If it failed, a corrected buyer quantity in ledgers.jsonl would keep the old value in ledgers_jev.jsonl and the
    regrade would grade stale structure (Codex round 2 on #112).
    """
    calls, ledgers, out = tmp_path / "calls.jsonl", tmp_path / "ledgers.jsonl", tmp_path / "ledgers_jev.jsonl"
    calls.write_text(json.dumps(_call().model_dump(mode="json")) + "\n")
    led = _ledger()
    ledgers.write_text(json.dumps(led.model_dump(mode="json")) + "\n")
    kw = dict(calls_path=calls, ledgers_path=ledgers, out_path=out, errors_path=tmp_path / "e.jsonl", workers=1)
    pipeline.run_judge(**kw)
    changed = led.model_copy(update={"buyer_facts": {**led.buyer_facts, "quantity": "10"}})
    ledgers.write_text(json.dumps(changed.model_dump(mode="json")) + "\n")
    assert pipeline.run_judge(**kw) == {"judged": 1, "skipped": 0, "errors": 0}


def test_judge_skips_a_malformed_ledger_row_and_goes_on(transport, tmp_path):
    """Protects the batch: one ledger row that fails validation goes to the errors file and the others are judged.

    If it failed, one bad row (for example scope "invalid") would stop the whole judge run before any call.
    """
    good, bad = _ledger(), {**_ledger().model_dump(mode="json"), "call_id": "bad_row", "scope": "invalid"}
    calls, ledgers, out = tmp_path / "calls.jsonl", tmp_path / "ledgers.jsonl", tmp_path / "ledgers_jev.jsonl"
    calls.write_text(
        json.dumps(_call().model_dump(mode="json"))
        + "\n"
        + json.dumps({**_call().model_dump(mode="json"), "call_id": "bad_row"})
        + "\n"
    )
    ledgers.write_text(json.dumps(good.model_dump(mode="json")) + "\n" + json.dumps(bad) + "\n")
    errs = tmp_path / "e.jsonl"
    r = pipeline.run_judge(calls_path=calls, ledgers_path=ledgers, out_path=out, errors_path=errs, workers=1)
    assert r == {"judged": 1, "skipped": 0, "errors": 1} and "bad_row" in errs.read_text()


def test_question_alone_is_asked_about_the_spec_name_not_the_utterance():
    """question_fits_product judges the question without its options, so Jev must get the spec name alone.

    With the whole utterance ("Voltage, 60V or 48V?") Jev judged the options too: on 42 real turns where Gemini said
    the question fits, Jev said it did not, and a pilot asking about the spec name alone agreed with Gemini on 35.
    If this failed, wrong options would make category_isq Fatal instead of Non-fatal.
    """
    call = CallRecord(
        call_id="syn_spec",
        duration_sec=2,
        turns=[Turn(idx=0, speaker="bot", start_sec=0, end_sec=1, text="Voltage mein 60V chahiye ya 48V?")],
    )
    led = Ledger.model_validate(
        {
            "call_id": "syn_spec",
            "scope": "bl_collection",
            "initial_product": "bike switch",
            "bot_turns": [{"idx": 0, "slot": "spec", "spec_name": "voltage", "options_offered": ["60V", "48V"]}],
        }
    )
    q = jev.build_questions(call, led)
    assert '"voltage"' in q["q0"]["instructions"] and "60V" not in q["q0"]["instructions"]
    assert "60V" in q["o0"]["instructions"]  # the options stay in the question-with-options label
    assert jev.JEV_VERSION == "3"
