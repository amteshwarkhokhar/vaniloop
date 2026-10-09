"""The Fix view in a headless Streamlit run on synthetic data: cards, diffs, rejections and the empty state."""

import json
import shutil
from pathlib import Path

from streamlit.testing.v1 import AppTest

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


def test_fix_view_shows_each_cause_card_with_its_diff_and_l_refs(tmp_path, monkeypatch):
    """The Fix view draws one card per ranked cause in rank order, with struck old lines, added lines and L refs.

    If this breaks, the owner cannot see which prompt lines a patch changes, so the fix cannot be reviewed.
    """
    html = _html(_open(tmp_path, monkeypatch, "Fix"))
    names = ["Specs kept after product change", "Bot repeats the same question", "Long silence before reply"]
    assert [html.index(n) for n in names] == sorted(html.index(n) for n in names)
    assert html.count('class="vl-card"') == 3
    assert 'class="ln">L70</span><span class="sg">−</span><del class="tx">Synthetic rule B: keep specs' in html
    assert '<ins class="tx">Synthetic rule B: drop specs when the product changes.</ins>' in html
    assert 'class="ln">L120</span>' in html and "Edits prompt lines L69 to L71." in html
    assert "Synthetic rationale" in html and "Synthetic risk" in html
    assert "Patch rejected" in html and "old_text not found in prompt" in html


def test_fix_view_says_when_a_cause_has_no_patch(tmp_path, monkeypatch):
    """A ranked cause with neither a patch nor a rejection shows that it has no patch.

    If this breaks, a cause the fix stage skipped looks the same as one that is still loading.
    """
    for f in FIX.iterdir():
        shutil.copy(f, tmp_path / f.name)
    doc = json.loads((FIX / "synthetic_patches.json").read_text())
    doc["patches"] = doc["patches"][:1]  # drop the C-03 patch
    (tmp_path / "synthetic_patches.json").write_text(json.dumps(doc))
    monkeypatch.setenv("VANILOOP_DATA", str(tmp_path))
    at = AppTest.from_file(str(APP), default_timeout=30).run()
    at.radio(key="view").set_value("Fix").run()
    assert "There is no patch for this cause yet." in _html(at)


def test_missing_patches_file_keeps_the_directive_empty_state(tmp_path, monkeypatch):
    """With no patches file the Fix view shows the empty state that names the command that writes it.

    If this breaks, a fresh checkout shows a blank page instead of telling the owner which stage to run first.
    """
    at = _open(tmp_path, monkeypatch, "Fix", drop=("synthetic_patches.json",))
    assert any("No fixes to show yet" in i.value and "pipeline fix" in i.value for i in at.info)
    assert 'class="vl-card"' not in _html(at)


def test_all_views_render_without_error_on_the_fixtures(tmp_path, monkeypatch):
    """Every view renders on the synthetic data without an exception.

    If the new import or loader broke the app, the Audit, Grade and Issues views would fail with it.
    """
    at = _open(tmp_path, monkeypatch, "Audit")
    for view in ("Grade", "Issues", "Fix", "Test", "Audit"):
        at.radio(key="view").set_value(view).run()
        assert not at.exception, view
