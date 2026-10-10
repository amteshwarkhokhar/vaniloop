"""The editable rubric file. Synthetic calls and a mocked Jev transport: no network, no key."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vaniloop import jev, ledger, llm, pipeline, rubric, rules
from vaniloop.models import BotTurnLabel, CallRecord, Ledger, Turn, UserTurnLabel
from vaniloop.rules import audit_call


@pytest.fixture(autouse=True)
def _reset_rubric(tmp_path):
    """Put every default back after each test, so an applied override never leaks into another test."""
    yield
    rubric.apply(tmp_path / "no_such_rubric.json")


def _looping_call(frustrated: float = 0.7) -> tuple[CallRecord, Ledger]:
    """A call where the bot asks for the quantity four times (limit 3) and the buyer then sounds irritated."""
    texts = [("bot", "How many pieces?"), ("user", "Not sure."), ("bot", "How many pieces?"), ("user", "Hmm."),
             ("bot", "How many pieces?"), ("user", "Ten."), ("bot", "How many pieces, please?"),
             ("user", "I told you."), ("bot", "Thank you.")]  # fmt: skip
    turns = [Turn(idx=i, speaker=s, start_sec=i * 3.0, end_sec=i * 3.0 + 2, text=t) for i, (s, t) in enumerate(texts)]
    call = CallRecord(call_id="loop1", duration_sec=27.0, turns=turns)
    led = Ledger(
        call_id="loop1",
        scope="bl_collection",
        initial_product="pen",
        final_product="pen",
        bot_turns=[BotTurnLabel(idx=i, slot="quantity" if i < 8 else "other") for i in range(0, 9, 2)],
        user_turns=[UserTurnLabel(idx=i, frustrated=frustrated if i == 7 else 0.1) for i in (1, 3, 5, 7)],
        model="gemini",
    )
    return call, led


def _write(tmp_path, body) -> str:
    p = tmp_path / "rubric.json"
    p.write_text(json.dumps(body))
    return p


def _looping(call, led):
    return next(f for f in audit_call(call, led).findings if f.parameter == "looping")


def test_override_changes_grading(tmp_path):
    """Protects the main promise: a saved FRUSTRATED_P changes what audit_call grades, with no code edit.

    If it failed, the console could save a rubric that the rules never read, and edits would do nothing.
    """
    call, led = _looping_call(0.7)
    assert _looping(call, led).status == "Fatal"
    p = tmp_path / "rubric.json"
    rubric.save(p, {"params": {"rules.FRUSTRATED_P": 0.9}})
    rubric.apply(p)
    assert rules.FRUSTRATED_P == 0.9
    assert _looping(call, led).status == "NonFatal"
    rubric.apply(tmp_path / "gone.json")
    assert rules.FRUSTRATED_P == 0.6 and _looping(call, led).status == "Fatal"  # apply resets to the defaults


def test_slot_limit_override_reaches_the_rules(tmp_path):
    """Protects the SLOT_LIMITS.<slot> ids: the dict entry in rules must change, not a copy of it.

    If it failed, a raised quantity limit would still flag four asks as a loop.
    """
    call, led = _looping_call()
    p = _write(tmp_path, {"params": {"rules.SLOT_LIMITS.quantity": 4}})
    rubric.apply(p)
    assert rules.SLOT_LIMITS["quantity"] == 4
    assert _looping(call, led).status == "Pass"


@pytest.fixture
def transport(monkeypatch, tmp_path):
    """Mock the Jev transport. `answers` maps a question id to its noul value, `bodies` records the requests."""
    monkeypatch.setattr(llm, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key-not-real")
    monkeypatch.delenv("VANILOOP_CACHE", raising=False)
    state = {"answers": {}, "bodies": []}

    def fake(url, api_key, body):
        state["bodies"].append(body)
        ans = {i: {"type": "noul", "noul": state["answers"].get(i, 0.5)} for i in body["questions"]}
        return {"model": "jev-1", "answers": ans, "usage": {"input_tokens": 1, "output_tokens": 1}}

    monkeypatch.setattr(llm, "_jev_post", fake)
    return state


def _spec_call() -> tuple[CallRecord, Ledger]:
    turns = [Turn(idx=0, speaker="bot", start_sec=0, end_sec=2, text="Which colour do you want?")]
    led = Ledger(
        call_id="s1",
        scope="bl_collection",
        initial_product="pen",
        final_product="pen",
        bot_turns=[BotTurnLabel(idx=0, slot="spec", options_offered=["red", "blue"], spec_name="colour")],
        user_turns=[],
        model="gemini",
    )
    return CallRecord(call_id="s1", duration_sec=20.0, turns=turns), led


def test_threshold_override_flips_label_and_changes_fingerprint(tmp_path, transport):
    """Protects the Jev thresholds: Q_TRUE_AT must reach the labels and the fingerprint.

    If it failed, an edited threshold would not re-label on the next judge run, or would keep stale rows.
    """
    call, led = _spec_call()
    transport["answers"] = {"q": 0.6, "n": 0.9, "opt0": 0.9, "opt1": 0.9}
    fp_before = jev.fingerprint(call, led)
    assert jev.judge_ledger(call, led).bot_turns[0].question_fits_product is True
    p = tmp_path / "rubric.json"
    rubric.save(p, {"params": {"jev.Q_TRUE_AT": 0.7}})
    rubric.apply(p)
    assert jev.judge_ledger(call, led).bot_turns[0].question_fits_product is None
    assert jev.fingerprint(call, led) != fp_before
    assert rubric.describe(p)["jev_version_effective"].startswith("4+")


def test_threshold_edit_hits_the_jev_cache(tmp_path, transport):
    """Protects the cost promise: a threshold edit re-labels from cached answers and makes no new request.

    If it failed, every threshold tweak would send the whole set to Jev again.
    """
    call, led = _spec_call()
    jev.judge_ledger(call, led)
    n = len(transport["bodies"])
    p = tmp_path / "rubric.json"
    rubric.save(p, {"params": {"jev.OPT_TRUE_AT": 0.9, "jev.N_DROP_AT": 0.1}})
    rubric.apply(p)
    jev.judge_ledger(call, led)
    assert len(transport["bodies"]) == n


def test_question_text_override_changes_the_request(tmp_path, transport):
    """Protects the question texts: an override must be the text in the request body, with `{i}` filled in.

    If it failed, editing a question in the console would not change what Jev is asked.
    """
    call, led = _spec_call()
    q = rubric.describe(tmp_path / "x.json")["questions"]
    spec_fit = next(x for x in q if x["id"] == "spec_fit")["default"]
    option_fit = next(x for x in q if x["id"] == "option_fit")["default"]
    p = tmp_path / "rubric.json"
    rubric.save(p, {"questions": {
        "spec_fit": {**spec_fit, "instructions": "CUSTOM spec text about `specification_asked`."},
        "option_fit": {**option_fit, "instructions": "CUSTOM option {i} text."},
    }})  # fmt: skip
    rubric.apply(p)
    jev.judge_ledger(call, led)
    qs = transport["bodies"][-1]["questions"]
    assert qs["q"]["instructions"] == "CUSTOM spec text about `specification_asked`."
    assert (
        qs["opt0"]["instructions"] == "CUSTOM option 0 text." and qs["opt1"]["instructions"] == "CUSTOM option 1 text."
    )
    assert qs["n"]["instructions"].startswith("Ignoring any example options")  # untouched questions keep their text


def test_default_requests_are_byte_identical(tmp_path):
    """Protects the Jev cache: with no rubric the requests and the fingerprint are the old, hard-coded ones.

    If it failed, moving the texts into the rubric would invalidate every cached Jev answer.
    """
    call, led = _spec_call()
    named = jev.build_questions(call, led)[0][1]
    assert named["q"]["instructions"] == (
        "Could a sales agent reasonably ask a buyer of `product_the_buyer_wants` about `specification_asked`? "
        "Judge the specification only, not the options."
    )
    assert named["n"]["criteria"] == {"true": "The turn asks about this specification.",
                                      "false": "The turn asks about something else."}  # fmt: skip
    assert named["opt1"]["instructions"] == (
        "Is `options_offered[1]` a sensible choice of `specification_asked` for `product_the_buyer_wants`?"
    )
    unnamed = led.model_copy(update={"bot_turns": [led.bot_turns[0].model_copy(update={"spec_name": None})]})
    assert jev.build_questions(call, unnamed)[0][1]["q"]["instructions"].startswith(
        "Could a sales agent reasonably ask a buyer of `product_the_buyer_wants` about `specification_asked` "
        "(the specification asked in `bot_turn`)? Judge"
    )
    assert rubric.describe(tmp_path / "none.json")["jev_version_effective"] == "4"


def test_to_bool_reads_the_product_match_marks_at_call_time(tmp_path):
    """Protects the call-time read: to_bool with no marks must follow an override of PM_TRUE_AT.

    If it failed, the default args would keep the import-time marks and a product-match edit would do nothing.
    """
    assert jev.to_bool(0.7) is True
    p = _write(tmp_path, {"params": {"jev.PM_TRUE_AT": 0.8}})
    rubric.apply(p)
    assert jev.to_bool(0.7) is None


def test_frustrated_agreement_uses_the_applied_mark(tmp_path):
    """Protects the Gemini vs Jev agreement: it compares frustrated at the current mark, not the import-time one.

    If it failed, the agreement table would keep 0.6 after an edit of FRUSTRATED_P.
    """
    row = {"call_id": "a", "user_turns": [{"idx": 1, "frustrated": 0.7}], "bot_turns": []}
    rubric.apply(_write(tmp_path, {"params": {"rules.FRUSTRATED_P": 0.9}}))
    out = jev.judge_agreement([row], [row])
    assert [r["field"] for r in out] == ["product_match", "frustrated>=0.9"]


@pytest.mark.parametrize(
    "body,field",
    [
        ({"params": {"rules.NOPE": 1}}, "rules.NOPE"),
        ({"params": {"rules.FRUSTRATED_P": 1.5}}, "rules.FRUSTRATED_P"),
        ({"params": {"rules.SLOT_LIMITS.spec": 2.5}}, "rules.SLOT_LIMITS.spec"),
        ({"params": {"rules.DEAD_AIR_SEC": "5"}}, "rules.DEAD_AIR_SEC"),
        ({"params": {"jev.Q_TRUE_AT": 0.1}}, "jev.Q_FALSE_AT"),  # false_at 0.20 would be above true_at 0.1
        ({"params": {"jev.PM_FALSE_AT": 0.65}}, "jev.PM_FALSE_AT"),  # equal is not below
        ({"questions": {"nope": None}}, "nope"),
        ({"questions": {"irritation": {"instructions": "x" * 2001, "true": {"description": "a", "examples": []},
                                       "false": {"description": "b", "examples": []}}}},
         "questions.irritation.instructions"),
        ({"questions": {"irritation": {"instructions": "ok", "true": {"description": "a", "examples": ["e"] * 13},
                                       "false": {"description": "b", "examples": []}}}},
         "questions.irritation.true.examples"),
        ({"questions": {"irritation": {"instructions": "ok", "true": {"description": "a", "examples": []},
                                       "false": {"description": "b", "examples": ["y" * 301]}}}},
         "questions.irritation.false.examples[0]"),
    ],
)  # fmt: skip
def test_validate_refuses_bad_input_and_names_the_field(body, field):
    """Protects the file from bad edits: each bad body is refused and the error names the field to mark.

    If it failed, the console could write a rubric that crashes every pipeline run, or one that grades nonsense.
    """
    with pytest.raises(rubric.RubricError) as e:
        rubric.validate(body)
    assert e.value.field == field


def test_pair_check_uses_the_stored_values(tmp_path):
    """Protects the pair rule across saves: a new false_at is checked against the true_at already on disk.

    If it failed, two saves in a row could leave false_at above true_at.
    """
    p = tmp_path / "rubric.json"
    rubric.save(p, {"params": {"jev.Q_TRUE_AT": 0.4}})
    with pytest.raises(rubric.RubricError) as e:
        rubric.save(p, {"params": {"jev.Q_FALSE_AT": 0.5}})
    assert e.value.field == "jev.Q_FALSE_AT"
    assert rubric.load(p)["params"] == {"jev.Q_TRUE_AT": 0.4}  # the refused save changed nothing


def test_save_merges_and_null_deletes(tmp_path):
    """Protects the merge: a null removes one override, other overrides stay, and no tmp file is left behind.

    If it failed, the console could not reset one value, or a save would drop the other edits.
    """
    p = tmp_path / "rubric.json"
    rubric.save(p, {"params": {"rules.DEAD_AIR_SEC": 7, "rules.HUMAN_P": 0.7}})
    d = rubric.save(p, {"params": {"rules.DEAD_AIR_SEC": None}})
    row = {x["id"]: x for x in d["params"]}
    assert row["rules.DEAD_AIR_SEC"]["overridden"] is False and row["rules.DEAD_AIR_SEC"]["value"] == 5.0
    assert row["rules.HUMAN_P"]["overridden"] is True and row["rules.HUMAN_P"]["value"] == 0.7
    assert sorted(x.name for x in tmp_path.iterdir() if not x.name.endswith(".lock")) == ["rubric.json"]
    assert d["path_name"] == "rubric.json"


def test_load_missing_file_is_empty_and_bad_file_raises(tmp_path):
    """Protects the file rules: a missing file means no overrides, a broken or invalid one raises RubricError.

    If it failed, a typo in the file would be ignored silently, or a fresh checkout would crash.
    """
    assert rubric.load(tmp_path / "none.json") == {"params": {}, "questions": {}}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(rubric.RubricError):
        rubric.load(bad)
    bad.write_text(json.dumps({"params": {"rules.FRUSTRATED_P": 5}}))
    with pytest.raises(rubric.RubricError):
        rubric.load(bad)


def test_path_prefers_the_environment(tmp_path, monkeypatch):
    """Protects path(): VANILOOP_RUBRIC wins, else the file sits in the runs folder.

    If it failed, the console and the pipeline could read two different rubric files.
    """
    monkeypatch.setenv("VANILOOP_RUBRIC", str(tmp_path / "mine.json"))
    assert rubric.path() == tmp_path / "mine.json"
    monkeypatch.delenv("VANILOOP_RUBRIC")
    assert rubric.path().name == "rubric.json" and rubric.path().parent.name == "runs"


def test_describe_lists_every_rule_and_every_param(tmp_path):
    """Protects the console's view: every rule in rules.RULES and every param id is listed, and params exist.

    If it failed, a rule or constant could be added in code and stay invisible and uneditable in the console.
    """
    call, led = _looping_call()
    rule_ids = {r(call, led).parameter for r in rules.RULES}
    d = rubric.describe(tmp_path / "none.json")
    assert {c["id"] for c in d["checks"]} == rule_ids and len(d["checks"]) == len(rules.RULES)
    ids = {p["id"] for p in d["params"]}
    assert ids == {p["id"] for p in rubric.PARAMS} and len(ids) == len(d["params"])
    for c in d["checks"]:
        assert set(c["params"]) <= ids and c["judged_by"] in ("code", "jev+code", "gemini+code")
        assert all(c[k] for k in ("name", "what", "fatal_when", "nonfatal_when"))
    for p in d["params"]:
        assert p["value"] == p["default"] and p["overridden"] is False and p["min"] <= p["default"] <= p["max"]
    assert {q["qid"] for q in d["questions"]} == {"q", "n", "opt", "f", "pm"}
    assert {x for x in ids if x.startswith("jev.")} == {f"jev.{n}" for n in rubric._JEV_NAMES}


def test_no_rubric_file_changes_nothing_in_regrade(tmp_path, monkeypatch):
    """Protects the default: with no rubric file, regrade writes exactly the audits it wrote before the rubric.

    If it failed, shipping the rubric would silently move every verdict of an untouched install.
    """
    calls = tmp_path / "calls.jsonl"
    calls.write_text(
        "".join((Path(__file__).parent / "fixtures" / "synthetic_calls.jsonl").read_text().splitlines(True)[:3])
    )
    p = dict(calls_path=calls, ledgers_path=tmp_path / "l.jsonl", audits_path=tmp_path / "a.jsonl",
             errors_path=tmp_path / "e.jsonl")  # fmt: skip
    fake = {"scope": "bl_collection", "initial_product": "x", "final_product": "x", "product_changed_at_turn": None,
            "buyer_facts": {}, "bot_confirmed": {}, "bot_turns": [], "user_turns": []}  # fmt: skip
    monkeypatch.setattr(ledger, "complete_json", lambda *a, **k: dict(fake))
    pipeline.run_audit(**p)
    before = p["audits_path"].read_text()
    monkeypatch.setenv("VANILOOP_RUBRIC", str(tmp_path / "no_such_rubric.json"))
    assert rubric.apply() == {"params": {}, "questions": {}}
    pipeline.run_regrade(p["calls_path"], p["ledgers_path"], p["audits_path"])
    assert p["audits_path"].read_text() == before


def test_pipeline_main_applies_the_rubric(tmp_path, monkeypatch):
    """Protects the wiring: pipeline.main applies the rubric before any stage runs, and a bad file stops it.

    If it failed, runs started from the console would grade with the defaults, not the saved rubric.
    """
    seen = {}
    monkeypatch.setattr(
        pipeline, "run_regrade", lambda *a, **k: seen.update(p=rules.FRUSTRATED_P) or {"regraded": 0, "verdicts": {}}
    )
    monkeypatch.setenv("VANILOOP_RUBRIC", str(_write(tmp_path, {"params": {"rules.FRUSTRATED_P": 0.9}})))
    pipeline.main(["regrade"])
    assert seen["p"] == 0.9
    (tmp_path / "rubric.json").write_text("{broken")
    with pytest.raises(SystemExit, match="rubric"):
        pipeline.main(["regrade"])


@pytest.mark.parametrize(
    "body,field",
    [({"params": False}, "params"), ({"params": []}, "params"), ({"questions": []}, "questions"),
     ({"questions": "x"}, "questions"), ({"params": 0}, "params")],
)  # fmt: skip
def test_validate_refuses_malformed_sections(body, field):
    """Protects the body shape: params and questions must be objects (absent or null means none).

    If it failed, {"params": false} would pass as "no overrides" and a bad PUT would be saved silently.
    """
    with pytest.raises(rubric.RubricError) as e:
        rubric.validate(body)
    assert e.value.field == field
    rubric.validate({"params": None, "questions": None})
    rubric.validate({})


def test_load_refuses_a_file_with_a_malformed_section(tmp_path):
    """Protects the file: a stored {"params": false} raises RubricError instead of reading as no overrides.

    If it failed, a hand-edited bad file would be ignored and the pipeline would grade with the defaults.
    """
    p = tmp_path / "rubric.json"
    p.write_text('{"params": false}')
    with pytest.raises(rubric.RubricError) as e:
        rubric.load(p)
    assert e.value.field == "params"


def test_concurrent_saves_do_not_lose_or_mix_writes(tmp_path):
    """Protects the file under parallel saves: every save succeeds, returns its own value, and none is lost.

    If it failed, writers sharing one tmp name could return another writer's value or hit FileNotFoundError.
    """
    import threading

    p = tmp_path / "rubric.json"
    ids = [f"rules.SLOT_LIMITS.{k}" for k in ("product", "quantity", "spec", "name", "city")] + [
        "rules.LOOP_MAX_ASKS",
        "rules.LOOP_IRRITATION_TURNS",
    ]
    out, errs = {}, []

    def go(i, pid):
        try:
            for n in range(20):
                v = 1 + (n + i) % 9
                d = rubric.save(p, {"params": {pid: v}})
                out[(pid, n)] = (v, {x["id"]: x["value"] for x in d["params"]}[pid])
        except Exception as e:  # collected for the assert
            errs.append(repr(e))

    ts = [threading.Thread(target=go, args=(i, pid)) for i, pid in enumerate(ids)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert errs == []
    assert all(a == b for a, b in out.values())
    last = rubric.load(p)["params"]
    assert set(last) == set(ids)
    assert sorted(x.name for x in tmp_path.iterdir() if not x.name.endswith(".lock")) == ["rubric.json"]


def test_rubric_path_follows_the_runs_folder(tmp_path, monkeypatch):
    """Protects --runs: without VANILOOP_RUBRIC the rubric is read from the --runs folder, and the env var still wins.

    If it failed, a run on a workspace folder would grade with the repo's runs/rubric.json, not the workspace's.
    """
    monkeypatch.delenv("VANILOOP_RUBRIC", raising=False)
    assert rubric.path(tmp_path) == tmp_path / "rubric.json"
    (tmp_path / "rubric.json").write_text(json.dumps({"params": {"rules.FRUSTRATED_P": 0.9}}))
    seen = {}
    monkeypatch.setattr(
        pipeline, "run_regrade", lambda *a, **k: seen.update(p=rules.FRUSTRATED_P) or {"regraded": 0, "verdicts": {}}
    )
    pipeline.main(["--runs", str(tmp_path), "regrade"])
    assert seen["p"] == 0.9
    monkeypatch.setenv("VANILOOP_RUBRIC", str(tmp_path / "other.json"))
    assert rubric.path(tmp_path) == tmp_path / "other.json"
