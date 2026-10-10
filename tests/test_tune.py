"""Live threshold scoring from raw Jev answers. Synthetic rows only: no network, no key, no call data."""

from __future__ import annotations

import json
import time

import pytest

from vaniloop import jev, pipeline, rubric, tune

DEV, TEST = "call-0", "call-1"  # sha256 first byte: call-0 is even, call-1 is odd


@pytest.fixture(autouse=True)
def _defaults(tmp_path):
    """Keep every rubric override out of these tests, and out of the next ones."""
    rubric.apply(tmp_path / "no_such_rubric.json")
    yield
    rubric.apply(tmp_path / "no_such_rubric.json")


def _raw(call_id: str, q: float = 0.6, f: float = 0.7, pm: float = 0.9) -> dict:
    """One call: a spec turn (idx 2) with two options, a buyer turn (idx 3) and a product match."""
    return {
        "call_id": call_id,
        "jev_fp": "x",
        "requests": [
            {"kind": "spec", "idx": 2, "options": 2, "named": True,
             "answers": {"q": q, "n": 0.9, "opt0": 0.9, "opt1": 0.9}},
            {"kind": "buyer", "idx": 3, "options": 0, "named": False, "answers": {"f": f}},
            {"kind": "pm", "idx": None, "options": 0, "named": False, "answers": {"pm": pm}},
        ],
    }  # fmt: skip


def _review(call_id: str, truth_q=True, truth_irr=True, truth_pm=True, gemini=True) -> list[dict]:
    def lab(label, truth):
        return {"label": label, "jev": None, "gemini": gemini, "truth": truth, "gemini_ok": gemini}

    return [
        {"item_id": f"{call_id}-s", "kind": "spec", "call_id": call_id, "idx": 2, "control": False,
         "labels": [lab("question_fits", truth_q), lab("with_options_fits", truth_q)]},
        {"item_id": f"{call_id}-b", "kind": "irritation", "call_id": call_id, "idx": 3, "control": False,
         "labels": [lab("irritated", truth_irr)]},
        {"item_id": f"{call_id}-p", "kind": "product_match", "call_id": call_id, "idx": None, "control": True,
         "labels": [lab("product_match", truth_pm)]},
    ]  # fmt: skip


def _write(d, raw, review):
    (d / "jev_raw.jsonl").write_text("".join(json.dumps(r) + "\n" for r in raw))
    (d / "jev_review.jsonl").write_text("".join(json.dumps(r) + "\n" for r in review))


def _row(res, label, split, key="labels"):
    return next(r for r in res[key] if r["label"] == label and r["split"] == split)


def test_score_moves_with_the_thresholds(tmp_path):
    """Protects the live tuning: a raised Q_TRUE_AT turns a right True into unsure, and the counts and flips move.

    If it failed, the console sliders would show the same accuracy whatever the thresholds are.
    """
    _write(tmp_path, [_raw(DEV, q=0.6), _raw(TEST, q=0.6)], _review(DEV) + _review(TEST))
    now = tune.score(tmp_path, {})
    q = _row(now, "question_fits", "dev")
    assert (q["n"], q["right"], q["wrong"], q["unsure"], q["gemini_right"]) == (1, 1, 0, 0, 1)
    assert now["flips"] == dict.fromkeys(tune.LABELS, 0) and now["calls"] == 2 and now["labels"] == now["current"]
    up = tune.score(tmp_path, {"question_fits": {"true_at": 0.7}})
    q, cur = _row(up, "question_fits", "dev"), _row(up, "question_fits", "dev", "current")
    assert (q["right"], q["unsure"]) == (0, 1) and (cur["right"], cur["unsure"]) == (1, 0)
    assert _row(up, "with_options_fits", "test")["unsure"] == 1  # q None makes the option label None
    assert up["flips"] == {"question_fits": 2, "with_options_fits": 2, "product_match": 0, "irritated": 0}
    assert _row(up, "product_match", "dev")["right"] == 1  # untouched labels stay


def test_score_other_labels_follow_their_own_thresholds(tmp_path):
    """Protects the other three labels: irritated, product match and the name check each follow their own key.

    If it failed, a slider for one label would move another label or do nothing.
    """
    _write(tmp_path, [_raw(DEV, f=0.7, pm=0.5)], _review(DEV, truth_pm=None))
    base = tune.score(tmp_path, {})
    assert _row(base, "irritated", "dev")["right"] == 1 and _row(base, "product_match", "dev")["n"] == 0
    hi = tune.score(tmp_path, {"frustrated_p": 0.8})
    assert _row(hi, "irritated", "dev")["wrong"] == 1 and hi["flips"]["irritated"] == 1
    pm = tune.score(tmp_path, {"product_match": {"true_at": 0.5}})
    assert pm["flips"]["product_match"] == 1 and pm["flips"]["irritated"] == 0
    drop = tune.score(tmp_path, {"name_check_drop_at": 0.95})
    assert _row(drop, "question_fits", "dev")["unsure"] == 1  # n 0.9 <= 0.95 drops q to unsure
    assert drop["flips"]["question_fits"] == 1 and drop["flips"]["irritated"] == 0


def test_split_is_the_documented_hash():
    """Protects the dev and test halves: the first byte of sha256(call_id) is even for dev, odd for test.

    If it failed, thresholds tuned on one half would be checked on calls that were not held out.
    """
    assert tune.split_of("call-0") == "dev" and tune.split_of("call-1") == "test"


def test_score_counts_each_call_in_its_own_split(tmp_path):
    """Protects the scoring split: a call counts in dev or test only, never both.

    If it failed, the held-out accuracy would include dev calls and look better than it is.
    """
    _write(tmp_path, [_raw(DEV), _raw(TEST)], _review(DEV, truth_q=True) + _review(TEST, truth_q=False))
    res = tune.score(tmp_path)
    assert (_row(res, "question_fits", "dev")["right"], _row(res, "question_fits", "test")["wrong"]) == (1, 1)
    assert sum(r["n"] for r in res["labels"] if r["label"] == "question_fits") == 2


def test_last_raw_row_per_call_wins(tmp_path):
    """Protects re-runs: when a call has two raw rows, the later one is scored.

    If it failed, a call judged again would be scored on its old answers.
    """
    _write(tmp_path, [_raw(DEV, q=0.1), _raw(DEV, q=0.6)], _review(DEV))
    res = tune.score(tmp_path)
    assert res["calls"] == 1 and _row(res, "question_fits", "dev")["right"] == 1


def test_missing_raw_file_raises_noraw(tmp_path):
    """Protects the console message: no jev_raw.jsonl raises NoRaw, so the API can answer 409 "run judge".

    If it failed, the console would show an empty table as if every label were unscored.
    """
    (tmp_path / "jev_review.jsonl").write_text("")
    with pytest.raises(tune.NoRaw):
        tune.score(tmp_path)


@pytest.mark.parametrize(
    "bad",
    [{"question_fits": {"true_at": 0.1}}, {"question_fits": {"true_at": 1.5}}, {"frustrated_p": "high"},
     {"question_fits": False}, {"option_fits": []}, {"product_match": "x"},
     {"question_fits": {"true_at": float("nan")}},
     {"frustrated_p": float("inf")}, {"name_check_drop_at": True}, [], "x", 3,
     {"nope": 1}, {"product_match": {"true_at": True}}],
)  # fmt: skip
def test_bad_thresholds_raise_value_error(tmp_path, bad):
    """Protects the input: a bad or crossed threshold raises ValueError, so the API can answer 400.

    If it failed, a crossed pair would give a table of nonsense instead of an error.
    """
    _write(tmp_path, [_raw(DEV)], _review(DEV))
    with pytest.raises(ValueError):
        tune.score(tmp_path, bad)


def test_score_is_fast_on_712_calls(tmp_path):
    """Protects the live feel: scoring 712 calls with their review rows takes under one second, with no network.

    If it failed, dragging a slider in the console would lag.
    """
    ids = [f"call-{i}" for i in range(712)]
    _write(tmp_path, [_raw(c, q=0.3 + (i % 7) / 10) for i, c in enumerate(ids)], [r for c in ids for r in _review(c)])
    t0 = time.perf_counter()
    res = tune.score(tmp_path, {"question_fits": {"true_at": 0.5, "false_at": 0.25}, "frustrated_p": 0.5})
    assert time.perf_counter() - t0 < 1.0
    assert res["calls"] == 712 and sum(r["n"] for r in res["labels"] if r["label"] == "irritated") == 712


def test_score_uses_the_thresholds_in_force_for_current(tmp_path):
    """Protects `current`: it is scored at the rubric's thresholds, so the console can show before and after.

    If it failed, an overridden threshold would show as the baseline instead of as a change.
    """
    _write(tmp_path, [_raw(DEV, q=0.6)], _review(DEV))
    p = tmp_path / "rubric.json"
    rubric.save(p, {"params": {"jev.Q_TRUE_AT": 0.7}})
    rubric.apply(p)
    res = tune.score(tmp_path, {})
    assert _row(res, "question_fits", "dev", "current")["unsure"] == 1 and jev.thresholds()["Q_TRUE_AT"] == 0.7
    assert tune.score(tmp_path, {"question_fits": {"true_at": 0.55}})["flips"]["question_fits"] == 1


def test_pipeline_tune_prints_the_score_as_json(tmp_path, monkeypatch, capsys):
    """Protects `pipeline tune`: it prints score() as one JSON object and exits cleanly on a bad input.

    If it failed, the console job or a person at the terminal would get a traceback instead of the table.
    """
    monkeypatch.setattr(pipeline, "RUNS", tmp_path)
    monkeypatch.setenv("VANILOOP_RUBRIC", str(tmp_path / "none.json"))
    _write(tmp_path, [_raw(DEV)], _review(DEV))
    pipeline.main(["tune", "--thresholds", '{"question_fits": {"true_at": 0.7}}'])
    out = json.loads(capsys.readouterr().out)
    assert out["calls"] == 1 and out["flips"]["question_fits"] == 1
    with pytest.raises(SystemExit, match="tune"):
        pipeline.main(["tune", "--thresholds", '{"nope": 1}'])
    (tmp_path / "jev_raw.jsonl").unlink()
    with pytest.raises(SystemExit, match="run judge"):
        pipeline.main(["tune"])


def test_malformed_threshold_error_names_the_field(tmp_path):
    """Protects the API message: a malformed section raises ValueError that names the field.

    If it failed, {"question_fits": false} would pass as "keep the current value" and the console would not tell.
    """
    _write(tmp_path, [_raw(DEV)], _review(DEV))
    with pytest.raises(ValueError, match="question_fits"):
        tune.score(tmp_path, {"question_fits": False})
    with pytest.raises(ValueError, match="thresholds"):
        tune.score(tmp_path, [])
