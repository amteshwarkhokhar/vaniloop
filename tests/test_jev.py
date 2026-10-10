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
    """Mock the Jev transport: `answers` maps a tag to a noul value (default 0.5), `bodies` records requests.

    A tag names the judgement: "q2", "n2" and "opt2_0" for the spec turn at idx 2, "f1" for the buyer turn at idx 1,
    "pm" for the product match. The mock finds the turn idx from the state text of the synthetic call.
    """
    monkeypatch.setattr(llm, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key-not-real")
    monkeypatch.delenv("VANILOOP_CACHE", raising=False)
    state = {"answers": {}, "bodies": []}
    idx_of = {t.text: t.idx for t in _call().turns}

    def tag(body, qid):
        st = body["state"]
        if "bot_turn" in st and "buyer_reply" not in st:
            i = idx_of[st["bot_turn"]]
            return f"{qid}{i}" if qid in ("q", "n") else f"opt{i}_{qid[3:]}"
        if "buyer_reply" in st:
            return f"f{idx_of[st['buyer_reply']]}"
        return qid

    def fake(url, api_key, body):
        state["bodies"].append(body)
        ans = {i: {"type": "noul", "noul": state["answers"].get(tag(body, i), 0.5)} for i in body["questions"]}
        return {"model": "jev-1", "answers": ans, "usage": {"input_tokens": 100, "output_tokens": 4}}

    monkeypatch.setattr(llm, "_jev_post", fake)
    return state


def test_jev_labels_replace_gemini_and_structure_is_kept(transport):
    """Protects the split of work: Jev sets the four judgement labels and nothing else changes.

    If it failed, Gemini's labels would still decide Fatals, or Jev would overwrite slots, values, declined or scope.
    """
    transport["answers"] = {"q2": 0.1, "n2": 0.9, "opt2_0": 0.9, "opt2_1": 0.9, "pm": 0.9, "f1": 0.3, "f3": 0.8}
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


def test_one_request_per_judgement_then_cached(transport):
    """Protects the cost and the focus: the call makes one request per spec turn, buyer turn and product match.

    If it failed, the whole call would go back into one state (v3) or a regrade would pay for Jev again.
    """
    jev.judge_ledger(_call(), _ledger())
    assert len(transport["bodies"]) == 4  # 1 spec turn, 2 buyer turns, 1 product match
    for body in transport["bodies"]:
        assert body["model"] == "jev-latest" and set(body) == {"state", "model", "questions"}
    jev.judge_ledger(_call(), _ledger())
    assert len(transport["bodies"]) == 4


def test_each_judgement_gets_only_its_own_state():
    """Protects the v4 design (#121): a spec turn's state holds four fields and a buyer turn's holds two.

    If it failed, a request would again carry the whole call and Jev's irritation and fit answers would drop (v3).
    """
    reqs = jev.build_questions(_call(), _ledger())
    states = [st for st, _ in reqs]
    assert [set(st) for st in states] == [
        {"product_the_buyer_wants", "bot_turn", "specification_asked", "options_offered"},
        {"bot_turn", "buyer_reply"},
        {"bot_turn", "buyer_reply"},
        {"product_the_bot_confirmed", "product_the_buyer_finally_wants"},
    ]
    assert states[0] == {
        "product_the_buyer_wants": "laptop",
        "bot_turn": "Laptop ka brand kaunsa chahiye, DJI ya MI?",
        "specification_asked": "brand",
        "options_offered": ["DJI", "MI"],
    }
    assert states[1] == {
        "bot_turn": "Namaste, aap drone camera chahte hain?",
        "buyer_reply": "Nahi, mujhe laptop chahiye.",
    }
    assert states[3] == {"product_the_bot_confirmed": "drone camera", "product_the_buyer_finally_wants": "laptop"}
    assert [list(qs) for _, qs in reqs] == [["q", "n", "opt0", "opt1"], ["f"], ["f"], ["pm"]]
    for _, qs in reqs:
        for q in qs.values():
            assert q["type"] == "noul" and q["criteria"]["true"] and q["criteria"]["false"]


def test_questions_carry_criteria_objects_with_examples():
    """Protects the tuned wording: spec, option, irritation and product questions give description and examples.

    If it failed, the definitions that lifted the held-out scores would be lost and Jev would judge on thin criteria.
    """
    qs = [q for _, qs in jev.build_questions(_call(), _ledger()) for q in qs.values()]
    spec_q = next(q for q in qs if q["instructions"].startswith("Could a sales agent"))
    assert "voltage for an electric bike switch" in spec_q["criteria"]["true"]["examples"]
    assert "flight time for a dome camera" in spec_q["criteria"]["false"]["examples"]
    for q in qs:
        if q["instructions"].startswith(("Could", "Is `options", "In `buyer", "Would")):
            assert q["criteria"]["true"]["description"] and q["criteria"]["false"]["examples"]


def test_spec_turn_is_judged_against_the_product_after_the_change(transport):
    """Protects the product-change rule of the issue: a spec turn after the change turn uses the final product.

    If it failed, a laptop spec question would be judged against the drone the buyer dropped.
    """
    led = _ledger()
    led.bot_turns.insert(1, BotTurnLabel(idx=0, slot="spec", options_offered=[]))  # before the change at turn 1
    specs = [st for st, qs in jev.build_questions(_call(), led) if "specification_asked" in st]
    assert [st["product_the_buyer_wants"] for st in specs] == ["drone camera", "laptop"]
    assert list(jev.build_questions(_call(), led)[0][1]) == ["q"]  # no spec_name and no options: the question only
    q = jev.build_questions(_call(), led)[0][1]["q"]["instructions"]
    assert "(the specification asked in `bot_turn`)" in q  # a missing spec_name points at the turn


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
    assert len(transport["bodies"]) == 4


def test_frustration_question_asks_for_irritation_with_calm_examples():
    """The irritation test (matrix: Fatal when the buyer gets irritated) must ask about irritation with the bot and
    list the calm replies (checking the line, repeated answers) as not irritated. Asked about repetition, Jev scored a
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
    ((state, qs),) = jev.build_questions(call, led)
    assert state == {"bot_turn": "Aapko packing machine chahiye na?", "buyer_reply": "Haan, haan. Haan."}
    q = qs["f"]
    assert "irritated or annoyed" in q["instructions"] and "`buyer_reply`" in q["instructions"]
    assert "answering, agreeing or repeating an answer" in q["criteria"]["false"]["examples"]
    assert "complains the bot keeps asking or repeating" in q["criteria"]["true"]["examples"]
    assert jev.JEV_VERSION == "4"


def test_unsure_question_fit_leaves_fits_confirmed_unjudged(transport):
    """Protects category_isq: when the question-alone answer is unsure, fits_confirmed_product is None too.

    If it failed, (None, True) would reach the rule and a spec turn nobody judged would Pass.
    """
    transport["answers"] = {"q2": 0.5, "n2": 0.9, "opt2_0": 0.9, "opt2_1": 0.9, "pm": 0.9}
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
    no_initial.bot_turns.insert(1, BotTurnLabel(idx=0, slot="spec"))
    specs = [st for st, _ in jev.build_questions(_call(), no_initial) if "specification_asked" in st]
    assert [st["product_the_buyer_wants"] for st in specs] == ["laptop"]  # the idx 0 turn has no product: no request


def test_identical_buyer_replies_each_get_their_own_request():
    """Protects the buyer turns: two identical buyer replies make two requests, each with its own bot turn.

    If it failed, a repeated "Pump" would be judged once and the second turn would reuse the first one's answer.
    """
    texts = ["Kya chahiye?", "Pump", "Kitna chahiye?", "Pump"]
    turns = [
        Turn(idx=i, speaker="bot" if i % 2 == 0 else "user", start_sec=i, end_sec=i + 1, text=t)
        for i, t in enumerate(texts)
    ]
    call = CallRecord(call_id="d", duration_sec=5.0, turns=turns)
    led = Ledger(call_id="d", scope="bl_collection", user_turns=[UserTurnLabel(idx=1), UserTurnLabel(idx=3)])
    states = [st for st, _ in jev.build_questions(call, led)]
    assert states == [
        {"bot_turn": "Kya chahiye?", "buyer_reply": "Pump"},
        {"bot_turn": "Kitna chahiye?", "buyer_reply": "Pump"},
    ]


@pytest.mark.parametrize("bad", [2, -1, float("nan")])
def test_invalid_probability_is_rejected_and_never_cached(transport, monkeypatch, bad):
    """Protects the labels: a noul outside [0, 1] or NaN raises, and is neither returned nor cached.

    If it failed, 2 would count as True, -1 as False, and a bad answer would be replayed from the cache.
    """
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    transport["answers"] = {"pm": bad}
    with pytest.raises(RuntimeError):
        jev.judge_ledger(_call(), _ledger())
    cached = list(llm.CACHE_DIR.glob("*.json")) if llm.CACHE_DIR.exists() else []
    assert not any("pm" in json.loads(f.read_text())["answers"] for f in cached)  # the other requests may be cached


def test_invalid_cached_probability_is_refetched(transport):
    """Protects the cache: a cached answer outside [0, 1] counts as a miss and Jev is asked again.

    If it failed, an old bad cache file would keep feeding 2 or NaN into the labels.
    """
    jev.judge_ledger(_call(), _ledger())
    for f in llm.CACHE_DIR.glob("*.json"):
        d = json.loads(f.read_text())
        if "pm" in d["answers"]:
            d["answers"]["pm"] = 2
            f.write_text(json.dumps(d))
    jev.judge_ledger(_call(), _ledger())
    assert len(transport["bodies"]) == 5  # only the product-match request is asked again


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


def test_question_alone_is_asked_about_the_spec_name_and_each_option_apart():
    """question_fits_product judges the spec name alone and each option gets its own question.

    With the whole utterance ("Voltage, 60V or 48V?") Jev judged the options too (#113). If this failed, wrong
    options would make category_isq Fatal instead of Non-fatal.
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
    ((state, qs),) = jev.build_questions(call, led)
    assert state["specification_asked"] == "voltage" and state["options_offered"] == ["60V", "48V"]
    assert "options" in qs["q"]["instructions"] and "60V" not in qs["q"]["instructions"]
    assert "options_offered[1]" in qs["opt1"]["instructions"]
    assert jev.JEV_VERSION == "4"


def test_spec_name_answer_counts_only_when_jev_confirms_the_turn_asks_it(transport, monkeypatch):
    """The question-alone label trusts Gemini's spec_name only when the name check n is above 0.35.

    If this failed, a spec name Gemini mislabelled as something generic ("performance" for "flight time" on a dome
    camera) would make a wrong question pass (Codex review on #114).
    """
    transport["answers"].update({"q2": 0.9, "n2": 0.35, "opt2_0": 0.9, "opt2_1": 0.9})
    b = next(b for b in jev.judge_ledger(_call(), _ledger()).bot_turns if b.idx == 2)
    assert b.question_fits_product is None and b.fits_confirmed_product is None
    transport["answers"]["n2"] = 0.36
    monkeypatch.setenv("VANILOOP_CACHE", "refresh")  # the first answer set is cached
    b = next(b for b in jev.judge_ledger(_call(), _ledger()).bot_turns if b.idx == 2)
    assert b.question_fits_product is True and b.fits_confirmed_product is True


def test_spec_name_answer_without_its_check_is_not_used(transport, monkeypatch):
    """A spec-name answer with no name-check answer at all must stay unjudged, not trusted.

    If this failed, a response that dropped the n answer would let a mislabelled spec name pass (Codex round 2
    on #114).
    """
    monkeypatch.setattr(
        jev, "complete_jev", lambda state, questions, cache_tag="": {k: 0.9 for k in questions if k != "n"}
    )
    b = next(b for b in jev.judge_ledger(_call(), _ledger()).bot_turns if b.idx == 2)
    assert b.question_fits_product is None and b.fits_confirmed_product is None


def test_per_label_thresholds():
    """Protects the tuned thresholds: q is True at 0.55 and False at 0.20, an option is True at 0.50.

    If it failed, the thresholds tuned on the Fable-reviewed set would drift back to the old 0.65 / 0.35.
    """
    f = jev._spec_labels
    assert f(0, {"q": 0.56}, False)[0] is True
    assert f(0, {"q": 0.21}, False)[0] is None
    assert f(0, {"q": 0.19}, False)[0] is False
    assert f(1, {"q": 0.9, "opt0": 0.5}, False) == (True, True)
    assert f(1, {"q": 0.9, "opt0": 0.21}, False) == (True, None)
    assert f(1, {"q": 0.9, "opt0": 0.2}, False) == (True, False)
    assert f(0, {"q": 0.9, "n": 0.35}, True) == (None, None)  # name check: dropped at n <= 0.35
    assert f(0, {"q": 0.9, "n": 0.36}, True)[0] is True
    assert (jev.to_bool(0.65), jev.to_bool(0.64), jev.to_bool(0.35), jev.to_bool(0.36)) == (True, None, False, None)


def test_combining_question_and_option_labels():
    """Protects fits_confirmed_product: q False gives False, any False option gives False, any unsure gives None.

    If it failed, a wrong option could pass a turn, or an unjudged option could count as fitting.
    """
    f = jev._spec_labels
    assert f(2, {"q": 0.1, "opt0": 0.9, "opt1": 0.9}, False) == (False, False)  # q False: options False
    assert f(2, {"q": 0.9, "opt0": 0.9, "opt1": 0.1}, False) == (True, False)  # one option False
    assert f(2, {"q": 0.9, "opt0": 0.9, "opt1": 0.4}, False) == (True, None)  # one option unsure
    assert f(2, {"q": 0.9, "opt0": 0.9}, False) == (True, None)  # one option missing
    assert f(2, {"q": 0.9, "opt0": 0.9, "opt1": 0.9}, False) == (True, True)
    assert f(0, {"q": 0.9}, False) == (True, True)  # no options: fits_confirmed_product = q
    assert f(0, {"q": 0.1}, False) == (False, False)
    assert f(2, {"q": 0.4, "opt0": 0.9, "opt1": 0.9}, False) == (None, None)  # q unsure: nothing judged


def test_frustrated_is_the_probability_and_fingerprint_covers_every_request(transport):
    """Protects two rules: frustrated is Jev's probability unchanged, and the fingerprint moves with any request.

    If it failed, a rounded probability would shift the 0.6 irritation line, or a changed state would keep a stale row.
    """
    transport["answers"] = {"f1": 0.4321, "f3": 0.9}
    out = jev.judge_ledger(_call(), _ledger())
    assert [u.frustrated for u in out.user_turns] == [0.4321, 0.9]
    base = jev.fingerprint(_call(), _ledger())
    assert base == jev.fingerprint(_call(), _ledger())
    other = _call().model_copy(
        update={"turns": [t.model_copy(update={"text": t.text + "!"}) if t.idx == 3 else t for t in _call().turns]}
    )
    assert jev.fingerprint(other, _ledger()) != base  # only the buyer turn at idx 3 changed


def test_product_match_is_asked_only_with_both_products(transport):
    """Protects product match: no request and product_match None when the bot confirmed no product.

    If it failed, Jev would be asked about a missing product and the handoff rule could not say Unknown.
    """
    led = _ledger().model_copy(update={"bot_confirmed": {"quantity": "5"}})
    jev.judge_ledger(_call(), led)
    assert not any("product_the_bot_confirmed" in b["state"] for b in transport["bodies"])
    assert jev.judge_ledger(_call(), led).product_match is None


@pytest.mark.parametrize("drop", ["f", "pm"])
def test_missing_answer_fails_the_call(monkeypatch, drop):
    """Protects the Unknown rule: a Jev reply without the irritation or product-match answer must fail the whole
    call, so the judge step retries it. If this fails, a missing answer could be written as calm or as a label."""

    def fake(state, questions, cache_tag=""):
        return {k: 0.9 for k in questions if k != drop}

    monkeypatch.setattr(jev, "complete_jev", fake)
    with pytest.raises(ValueError, match="jev: no"):
        jev.judge_ledger(_call(), _ledger())


def test_user_turn_without_transcript_turn_fails_the_call(monkeypatch):
    """Protects the Unknown rule: a ledger buyer turn that matches no transcript turn gets no request, and must
    fail the call rather than default to frustrated 0.0. If this fails, an unjudged turn reads as a calm buyer."""
    monkeypatch.setattr(jev, "complete_jev", lambda state, questions, cache_tag="": {k: 0.9 for k in questions})
    led = _ledger()
    led = led.model_copy(update={"user_turns": [*led.user_turns, UserTurnLabel(idx=9, frustrated=0.0)]})
    with pytest.raises(ValueError, match="no transcript turn"):
        jev.judge_ledger(_call(), led)


def test_failed_request_fails_the_call(monkeypatch):
    """Protects the retry path: an error from one Jev request must reach the judge step, which logs the call and
    retries it. If this fails, a call could be written with some labels from a request that never answered."""

    def boom(state, questions, cache_tag=""):
        if "buyer_reply" in state:
            raise RuntimeError("jev 503")
        return {k: 0.9 for k in questions}

    monkeypatch.setattr(jev, "complete_jev", boom)
    with pytest.raises(RuntimeError):
        jev.judge_ledger(_call(), _ledger())


def test_judge_writes_raw_rows_that_reproduce_the_labels(transport, tmp_path):
    """Protects jev_raw.jsonl: each judged call gets one raw row, and labels(raw) rebuilds the ledger row.

    If it failed, the console would tune thresholds on raw answers that do not match the labels the judge wrote.
    """
    transport["answers"] = {"q2": 0.6, "n2": 0.9, "opt2_0": 0.9, "opt2_1": 0.9, "pm": 0.9, "f1": 0.3, "f3": 0.8}
    calls, ledgers, out = tmp_path / "calls.jsonl", tmp_path / "ledgers.jsonl", tmp_path / "ledgers_jev.jsonl"
    calls.write_text(json.dumps(_call().model_dump(mode="json")) + "\n")
    ledgers.write_text(json.dumps(_ledger().model_dump(mode="json")) + "\n")
    kw = dict(calls_path=calls, ledgers_path=ledgers, out_path=out, errors_path=tmp_path / "e.jsonl", workers=1)
    pipeline.run_judge(**kw)
    (raw_row,) = [json.loads(ln) for ln in (tmp_path / "jev_raw.jsonl").read_text().splitlines()]
    (led_row,) = [json.loads(ln) for ln in out.read_text().splitlines()]
    assert raw_row["call_id"] == "c1" and raw_row["jev_fp"] == led_row["jev_fp"] == jev.fingerprint(_call(), _ledger())
    assert [(r["kind"], r["idx"], r["options"], r["named"]) for r in raw_row["requests"]] == [
        ("spec", 2, 2, True),
        ("buyer", 1, 0, False),
        ("buyer", 3, 0, False),
        ("pm", None, 0, False),
    ]
    assert raw_row["requests"][0]["answers"] == {"q": 0.6, "n": 0.9, "opt0": 0.9, "opt1": 0.9}
    assert jev.labels(raw_row["requests"], _ledger()) == jev.judge_ledger(_call(), _ledger())
    assert jev.labels(raw_row["requests"], _ledger()).model_dump(mode="json") == {
        k: v for k, v in led_row.items() if k != "jev_fp"
    }


def test_judge_backfills_a_missing_raw_row_without_a_request(transport, tmp_path):
    """Protects old data: a call judged before jev_raw.jsonl existed is judged again, from the cache, to add its row.

    If it failed, the console would have no raw answers for calls judged earlier and tuning would stay empty.
    """
    calls, ledgers, out = tmp_path / "calls.jsonl", tmp_path / "ledgers.jsonl", tmp_path / "ledgers_jev.jsonl"
    calls.write_text(json.dumps(_call().model_dump(mode="json")) + "\n")
    ledgers.write_text(json.dumps(_ledger().model_dump(mode="json")) + "\n")
    kw = dict(calls_path=calls, ledgers_path=ledgers, out_path=out, errors_path=tmp_path / "e.jsonl", workers=1)
    pipeline.run_judge(**kw)
    sent = len(transport["bodies"])
    (tmp_path / "jev_raw.jsonl").unlink()
    assert pipeline.run_judge(**kw) == {"judged": 1, "skipped": 0, "errors": 0}
    assert len(transport["bodies"]) == sent and (tmp_path / "jev_raw.jsonl").exists()
    assert pipeline.run_judge(**kw)["skipped"] == 1


def test_judge_raw_write_failure_leaves_no_ledger_row_and_the_next_run_repairs_it(transport, tmp_path, monkeypatch):
    """Protects the pair of files: the raw row is written first, so a failed raw write leaves no ledger row.

    If it failed, a call would have a ledger row but no raw row, and the run would abort or skip it as done.
    """
    calls, ledgers, out = tmp_path / "calls.jsonl", tmp_path / "ledgers.jsonl", tmp_path / "ledgers_jev.jsonl"
    calls.write_text(json.dumps(_call().model_dump(mode="json")) + "\n")
    ledgers.write_text(json.dumps(_ledger().model_dump(mode="json")) + "\n")
    errors = tmp_path / "e.jsonl"
    kw = dict(calls_path=calls, ledgers_path=ledgers, out_path=out, errors_path=errors, workers=1)
    real = pipeline.append_jsonl

    def flaky(path, rows):
        if str(path).endswith("jev_raw.jsonl"):
            raise OSError("disk full")
        return real(path, rows)

    monkeypatch.setattr(pipeline, "append_jsonl", flaky)
    assert pipeline.run_judge(**kw) == {"judged": 0, "skipped": 0, "errors": 1}
    assert not out.exists() or out.read_text() == ""
    (err,) = [json.loads(ln) for ln in errors.read_text().splitlines()]
    assert err["call_id"] == "c1" and "OSError" in err["error"]
    monkeypatch.setattr(pipeline, "append_jsonl", real)
    assert pipeline.run_judge(**kw) == {"judged": 1, "skipped": 0, "errors": 0}
    assert len(out.read_text().splitlines()) == 1 and (tmp_path / "jev_raw.jsonl").exists()
