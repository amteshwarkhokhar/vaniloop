"""The Grade view in a headless Streamlit run on synthetic data: blind until saved, one form per call."""

import json
import shutil
from pathlib import Path

from streamlit.testing.v1 import AppTest

from vaniloop.grading import load_grades

FIX = Path(__file__).parent / "fixtures"
APP = Path(__file__).resolve().parents[1] / "src" / "vaniloop" / "dashboard.py"


def _app(tmp_path, monkeypatch) -> tuple[AppTest, list[str]]:
    for f in (p for p in FIX.iterdir() if p.is_file()):
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


def test_a_call_link_opens_that_call_in_the_audit_view(tmp_path, monkeypatch):
    """Opening the dashboard with ?call=<id> shows that call's detail, and choosing a call updates the link.

    If this breaks, a link sent for review opens the wrong call or the first call in the list.
    """
    for f in (p for p in FIX.iterdir() if p.is_file()):
        shutil.copy(f, tmp_path / f.name)
    monkeypatch.setenv("VANILOOP_DATA", str(tmp_path))
    ids = [json.loads(line)["call_id"] for line in (FIX / "synthetic_audits.jsonl").read_text().splitlines()]
    at = AppTest.from_file(str(APP), default_timeout=30)
    at.query_params["call"] = ids[2]
    at.run()
    assert not at.exception
    assert at.selectbox(key="call_sel").value == ids[2]
    at.selectbox(key="call_sel").select(ids[3]).run()
    assert at.query_params["call"] in (ids[3], [ids[3]])
