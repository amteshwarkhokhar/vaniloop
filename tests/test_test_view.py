"""The Test view in a headless Streamlit run on synthetic data: gate cards, replays and the empty state."""

import json
import shutil
from pathlib import Path

from streamlit.testing.v1 import AppTest

from vaniloop.dashboard import load_replays

FIX = Path(__file__).parent / "fixtures"
APP = Path(__file__).resolve().parents[1] / "src" / "vaniloop" / "dashboard.py"


def _open(tmp_path, monkeypatch, view: str, drop: tuple[str, ...] = ()) -> AppTest:
    """Copy the synthetic fixtures to tmp_path, minus `drop`, and open the dashboard on `view`."""
    for f in FIX.iterdir():
        if f.name not in drop:
            shutil.copy(f, tmp_path / f.name)
    monkeypatch.setenv("VANILOOP_DATA", str(tmp_path))
    at = AppTest.from_file(str(APP), default_timeout=30).run()
    at.radio(key="view").set_value(view).run()
    assert not at.exception
    return at


def _html(at: AppTest) -> str:
    return "\n".join(e.proto.body for e in at.get("html"))


def test_test_view_shows_the_latest_gate_result_per_cause(tmp_path, monkeypatch):
    """The Test view shows badge, p-value against alpha, fail rates, lift, per-call counts and guards.

    It shows only the newest attempt of a cause. If an older rejected attempt showed instead, a shipped patch
    would look rejected in the demo.
    """
    html = _html(_open(tmp_path, monkeypatch, "Test"))
    assert html.count('class="vl-card gate"') == 2  # C-01 attempt 2 replaced attempt 1; C-03 is the other
    assert ">Ship</span>Specs kept after product change<small>attempt 2</small>" in html
    assert ">Reject</span>" not in html
    assert "0.0020 vs 0.05" in html and "at or below alpha" in html
    assert "89%" in html and "11%" in html and "+78 points" in html
    assert "3 of 3" in html and "2 of 3" in html and "0 of 3" in html
    assert "<b>Guard breaks</b>none" in html and "<b>Flaky guards</b>syn_030" in html
    assert ">Inconclusive</span>Long silence before reply" in html and "No test run" in html
    assert "<b>1 of 2</b> tested fixes ship." in html


def test_test_view_shows_old_and_new_seed_0_replays_of_the_selected_call(tmp_path, monkeypatch):
    """For the selected target call the view shows the old and the new seed-0 replay from the fork turn.

    If this breaks, the demo cannot show the same call going wrong with the old prompt and right with the new one.
    """
    at = _open(tmp_path, monkeypatch, "Test")
    assert at.selectbox(key="replay_cause").value == "C-01"
    assert at.selectbox(key="replay_call_C-01").value == "syn_002"
    html = _html(at)
    assert "Synthetic old reply that keeps the stale spec." in html
    assert "Synthetic new reply that asks the specs again." in html
    assert "Synthetic second old reply." not in html  # seed 1 is not shown
    assert "Synthetic spec question." not in html and "Namaste" not in html  # turns before the fork are left out
    assert "Old prompt" in html and "New prompt" in html and "category_isq" not in html
    assert "Seed 0, from turn 3" in html and "Fatal" in html and "Pass" in html
    at.selectbox(key="replay_call_C-01").select("syn_010").run()
    html = _html(at)
    assert "Synthetic old reply B." in html and "Synthetic new reply B." in html
    assert "keeps the stale spec" not in html


def test_test_view_says_when_a_call_has_no_recorded_replay(tmp_path, monkeypatch):
    """A target call with no seed-0 replay in replays.jsonl shows a plain note on each side, not an error.

    If this breaks, one call missing from the replay file would crash the whole Test view.
    """
    at = _open(tmp_path, monkeypatch, "Test")
    at.selectbox(key="replay_call_C-01").select("syn_018").run()
    assert not at.exception
    notes = [c.value for c in at.caption]
    assert any("No seed-0 replay with the old prompt" in n for n in notes)
    assert any("No seed-0 replay with the new prompt" in n for n in notes)


def test_test_view_keeps_the_gate_cards_when_replays_are_missing(tmp_path, monkeypatch):
    """With gates.jsonl but no replays.jsonl the gate cards still draw and each replay side says it is missing.

    If this breaks, running the gate before the replays are saved would blank the whole Test view.
    """
    at = _open(tmp_path, monkeypatch, "Test", drop=("synthetic_replays.jsonl",))
    assert 'class="vl-card gate"' in _html(at)
    assert any("No seed-0 replay with the old prompt" in c.value for c in at.caption)


def test_replay_rows_with_non_finite_times_are_skipped_and_the_page_survives(tmp_path, monkeypatch):
    """A replay row with a NaN turn time or an infinite duration is skipped, and the view shows the missing note.

    Found in review: such a row passed validation and crashed the page when selected, because mmss() cannot format it.
    """
    for f in FIX.iterdir():
        shutil.copy(f, tmp_path / f.name)
    rows = [json.loads(line) for line in (FIX / "synthetic_replays.jsonl").read_text().splitlines()]
    rows[1]["call"]["turns"][3]["start_sec"] = float("nan")  # the new seed-0 replay of syn_002
    rows[4]["call"]["duration_sec"] = float("inf")  # the new seed-0 replay of syn_010
    (tmp_path / "synthetic_replays.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    assert len(load_replays(tmp_path)) == len(rows) - 2
    monkeypatch.setenv("VANILOOP_DATA", str(tmp_path))
    at = AppTest.from_file(str(APP), default_timeout=30).run()
    at.radio(key="view").set_value("Test").run()
    assert not at.exception
    assert any("No seed-0 replay with the new prompt" in c.value for c in at.caption)
    assert "Synthetic old reply that keeps the stale spec." in _html(at)


def test_missing_gates_file_keeps_the_directive_empty_state(tmp_path, monkeypatch):
    """With no gates or replays file the Test view shows the empty state that names the command that writes them.

    If this breaks, a fresh checkout shows a blank page instead of telling the owner which stage to run first.
    """
    at = _open(tmp_path, monkeypatch, "Test", drop=("synthetic_gates.jsonl", "synthetic_replays.jsonl"))
    assert any("No replays to show yet" in i.value and "pipeline prove" in i.value for i in at.info)
    assert 'class="vl-card gate"' not in _html(at)
