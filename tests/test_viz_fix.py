"""Fix drawings: the patch diff and the fix cards. Pure HTML, no Streamlit."""

from pathlib import Path

from vaniloop.dashboard import load_causes, load_patches_doc
from vaniloop.fix import PromptPatch
from vaniloop.models import Cause
from vaniloop.viz import diff_rows, fix_cards, patch_diff

FIX = Path(__file__).parent / "fixtures"


def _patch(**kw) -> PromptPatch:
    base = {
        "cause_id": "C-01",
        "target_start_line": 69,
        "target_end_line": 71,
        "old_text": "keep A\nold B\nkeep C",
        "new_text": "keep A\nnew B\nnew B2\nkeep C",
        "rationale": "because",
        "risk": "maybe",
    }
    return PromptPatch(**{**base, **kw})


def test_diff_strikes_old_lines_adds_new_lines_and_counts_l_refs_from_the_start_line():
    """Old lines carry L refs counted from target_start_line, new lines have none, kept lines stay plain.

    If the refs drifted, the reader would open the wrong line of the real VANI prompt to check a patch.
    """
    assert diff_rows(_patch()) == [
        ("ctx", "L69", "keep A"),
        ("del", "L70", "old B"),
        ("add", "", "new B"),
        ("add", "", "new B2"),
        ("ctx", "L71", "keep C"),
    ]
    out = patch_diff(_patch())
    assert "<del " in out and "old B</del>" in out and "<ins " in out and "new B2</ins>" in out
    assert "lines L69 to L71" in out
    assert "Edits prompt line L69." in patch_diff(_patch(target_end_line=69, old_text="x", new_text="y"))


def test_fix_cards_follow_rank_order_and_say_why_a_cause_has_no_patch():
    """Ranked causes get one card each in order. A watch cause gets none. A rejected cause shows its reason.

    If a cause with no patch showed nothing, the owner could not tell a rejected patch from a missing stage.
    """
    causes = load_causes(FIX)
    patches, rejected = load_patches_doc(FIX)
    out = fix_cards(causes, patches, rejected)
    names = [c.name for c in causes if c.status == "ranked"]
    assert out.count('class="vl-card"') == len(names) == 3
    assert [out.index(n) for n in names] == sorted(out.index(n) for n in names)
    assert not any(c.name in out for c in causes if c.status != "ranked")
    assert "Patch rejected" in out and "old_text not found in prompt" in out
    assert "There is no patch for this cause yet." not in out  # every ranked cause has a patch or a reason
    assert "There is no patch for this cause yet." in fix_cards(causes, patches[:1], [])
    assert "5 Fatal calls" in out and "Synthetic rationale" in out and "Synthetic risk" in out
    assert "<b>2 of 3</b> ranked causes have a patch ready to test. 1 had a patch rejected." in out


def test_fix_cards_still_show_a_patch_for_a_cause_missing_from_the_causes_file():
    """A patch whose cause is not in causes.json still gets a card, named by its id.

    Without it, a stale causes file would silently hide a patch the owner wrote.
    """
    out = fix_cards([], [_patch(cause_id="cause_x")], [])
    assert "cause_x" in out and "new B2" in out


def test_fix_cards_escape_every_string_from_data():
    """Names, prompt lines, rationale, risk and reasons reach the page as text, never as markup.

    The dashboard renders these strings as HTML. A prompt line or a reason with a tag could inject script.
    """
    bad = "<script>alert(1)</script>"
    cause = Cause(cause_id="C-01", name=bad, description=bad, type="prompt", parameter="p")
    patch = _patch(old_text=bad, new_text=bad + "2", rationale=bad, risk=bad)
    for out in (
        fix_cards([cause], [patch], []),
        fix_cards([cause], [], [{"cause_id": "C-01", "reason": bad}]),
    ):
        assert "<script>" not in out and "&lt;script&gt;" in out


def test_patches_loader_reads_the_fixture_and_reports_a_missing_file_as_none():
    """The loader returns the patches and the rejected list, and None when patches.json is missing.

    If a missing file returned an empty pair, the Fix view would hide its directive empty state.
    """
    patches, rejected = load_patches_doc(FIX)
    assert [p.cause_id for p in patches] == ["C-01", "C-03"] and rejected[0]["cause_id"] == "C-02"
    assert load_patches_doc(FIX / "nowhere") is None
