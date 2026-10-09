"""The Grade view in a headless Streamlit run on synthetic data: blind until saved, one form per call."""

import json
import shutil
from pathlib import Path

from streamlit.testing.v1 import AppTest

from vaniloop.grading import load_grades

FIX = Path(__file__).parent / "fixtures"
APP = Path(__file__).resolve().parents[1] / "src" / "vaniloop" / "dashboard.py"


def _app(tmp_path, monkeypatch) -> tuple[AppTest, list[str]]:
    for f in FIX.iterdir():
        shutil.copy(f, tmp_path / f.name)
    ids = [json.loads(line)["call_id"] for line in (FIX / "synthetic_audits.jsonl").read_text().splitlines()][:3]
    (tmp_path / "grade_sample.json").write_text(json.dumps(ids))
    monkeypatch.setenv("VANILOOP_DATA", str(tmp_path))
    at = AppTest.from_file(str(APP), default_timeout=30).run()
    at.radio(key="view").set_value("Grade").run()
    return at, ids


def test_grade_view_hides_the_audit_until_saved_then_reveals_it(tmp_path, monkeypatch):
    """Before a grade is saved the page shows no audit verdict; after Save it shows the reveal and the agreement.

    If this breaks, the owner sees the audit answer before grading, or a saved grade never reaches hand_grades.jsonl.
    """
    at, ids = _app(tmp_path, monkeypatch)
    assert not at.exception
    assert not any("Your grade and the audit" in m.value for m in at.markdown)
    at.radio(key=f"grade_verdict_{ids[0]}").set_value("Fatal")
    at.button[0].click().run()
    assert load_grades(tmp_path / "hand_grades.jsonl")[ids[0]]["verdict"] == "Fatal"
    assert any("Your grade and the audit" in m.value for m in at.markdown)
    assert any("Agreement" in m.value for m in at.markdown)


def test_unsaved_choice_on_one_call_does_not_follow_to_the_next(tmp_path, monkeypatch):
    """A verdict picked but not saved on call A is not pre-filled on call B, and saving B stores B's own choice.

    If this breaks, a grade meant for one call can be saved under another call id and corrupt calibration.
    """
    at, ids = _app(tmp_path, monkeypatch)
    at.radio(key=f"grade_verdict_{ids[0]}").set_value("Fatal").run()
    at.selectbox(key="grade_sel").select(ids[1]).run()
    assert at.radio(key=f"grade_verdict_{ids[1]}").value == "Pass"
    at.button[0].click().run()
    grades = load_grades(tmp_path / "hand_grades.jsonl")
    assert set(grades) == {ids[1]} and grades[ids[1]]["verdict"] == "Pass"
