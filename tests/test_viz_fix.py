"""Fix drawings: the patch diff and the fix cards. Pure HTML, no Streamlit."""

import html
import re
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


LONG_OLD = "Always confirm the city before you ask the product. Then read the lead back to the buyer."
LONG_NEW = (
    "Always confirm the city before you ask the product. Never ask the city twice. "
    "Then read the lead back to the buyer."
)


def _words(out: str) -> list[tuple[str, str]]:
    """The (tag, text) of every marked word range in a rendered diff."""
    return re.findall(r'<(ins|del) class="w">(.*?)</\1>', out)


def test_one_sentence_insert_marks_only_the_inserted_words():
    """A sentence added to one long line is the only marked text. The kept words and the L ref stay as they were.

    If the whole new line were marked again, the reader would compare two paragraphs to find one sentence.
    """
    out = patch_diff(_patch(target_start_line=5, target_end_line=5, old_text=LONG_OLD, new_text=LONG_NEW))
    assert _words(out) == [("ins", "Never ask the city twice.")]
    plain = re.sub(r'<(ins|del) class="w">.*?</\1>', "", out)
    assert "Always confirm the city before you ask the product." in plain and "Then read the lead back" in plain
    assert 'class="ln">L5</span>' in out and 'class="row del part"' in out and 'class="row add part"' in out
    assert "<del " not in plain and "<ins " not in plain  # no whole-line markup on a word-marked pair


def test_replaced_and_removed_words_are_struck_in_the_old_line_and_added_in_the_new_line():
    """A swapped word is struck in the old line and added in the new line. A deleted word is only struck.

    If the old line never showed what left, the reader would see the new words with nothing to compare them to.
    """
    swap = patch_diff(
        _patch(
            target_end_line=69,
            old_text="Ask the city first, then confirm.",
            new_text="Ask the pincode first, then confirm.",
        )
    )
    assert _words(swap) == [("del", "city"), ("ins", "pincode")]
    cut = patch_diff(
        _patch(
            target_end_line=69,
            old_text="Ask the city first, never twice, then confirm.",
            new_text="Ask the city first, then confirm.",
        )
    )
    assert _words(cut) == [("del", "never twice,")]


def test_mostly_different_pairs_and_one_sided_lines_keep_the_whole_line_style():
    """A pair with most words changed, a pair with no word change and lone added or removed lines stay whole.

    If they were word-marked, a rewritten rule would show as a scatter of marks that no reader can follow.
    """
    rewrite = patch_diff(
        _patch(target_end_line=69, old_text="alpha beta gamma delta epsilon", new_text="one two three four five six")
    )
    assert not _words(rewrite) and 'class="row del"' in rewrite and 'class="row add"' in rewrite
    assert '<del class="tx">alpha beta gamma delta epsilon</del>' in rewrite
    assert '<ins class="tx">one two three four five six</ins>' in rewrite
    spacing = patch_diff(_patch(target_end_line=69, old_text="same words here", new_text="same  words here "))
    assert not _words(spacing) and "part" not in spacing
    added = patch_diff(_patch(old_text="keep A", new_text="keep A\nnew line", target_end_line=69))
    removed = patch_diff(_patch(old_text="keep A\nold line", new_text="keep A", target_end_line=70))
    for out, tag in ((added, "ins"), (removed, "del")):
        assert not _words(out) and f'<{tag} class="tx">' in out and "part" not in out
    two_for_one = patch_diff(_patch(old_text="one two three", new_text="one two four\nfive", target_end_line=69))
    assert not _words(two_for_one) and "part" not in two_for_one  # a pair means one old line and one new line


def test_word_marked_lines_escape_the_patch_text():
    """Prompt text with a tag is escaped inside the word marks and in whole-line rows.

    The dashboard renders this string as HTML. An unescaped <script> in a prompt line would run in the owner's browser.
    """
    bad = "<script>alert(1)</script>"
    word = patch_diff(_patch(target_end_line=69, old_text=f"keep {bad} here", new_text=f"keep {bad} here {bad} too"))
    whole = patch_diff(_patch(target_end_line=69, old_text=bad, new_text=bad + "2"))
    assert _words(word) == [("ins", "&lt;script&gt;alert(1)&lt;/script&gt; too")]
    for out in (word, whole):
        assert "<script>" not in out and "&lt;script&gt;" in out


def test_an_insert_next_to_repeated_words_starts_at_the_sentence_start():
    """When the sentences around an insert start with the same words, the mark covers the new sentence only.

    Without the slide, difflib marks "G: ... once. Synthetic rule" and leaves its first two words plain.
    """
    old = "Synthetic rule E: greet the buyer. Synthetic rule F: ask the product."
    new = old.replace("buyer. ", "buyer. Synthetic rule G: say the city once. ")
    assert _words(patch_diff(_patch(target_end_line=69, old_text=old, new_text=new))) == [
        ("ins", "Synthetic rule G: say the city once.")
    ]
    assert _words(patch_diff(_patch(target_end_line=69, old_text=new, new_text=old))) == [
        ("del", "Synthetic rule G: say the city once.")
    ]


def test_word_marks_never_drop_duplicate_or_reorder_words_on_repeated_words():
    """With the markup removed, each shown line equals the real line. Repeats and extra spaces survive.

    Two slides that overlapped once drew "A C A C A" as "A CA C A C A", so the diff showed text the patch never had.
    """
    cases = [
        ("A A", "A C A C A"),
        ("A  A", "A  C  A  C  A"),
        ("go A. go B.", "go A. go X. go B."),
        ("x a a a y", "x a b a a a c y"),
    ]
    for old, new in cases:
        out = patch_diff(_patch(target_end_line=69, old_text=old, new_text=new))
        shown = {
            k: t for k, t in re.findall(r'<div class="row (del|add) part">.*?<span class="tx">(.*?)</span></div>', out)
        }
        plain = {k: html.unescape(re.sub(r"</?(?:ins|del)[^>]*>", "", t)) for k, t in shown.items()}
        assert plain == {"del": old, "add": new}, (old, new, shown)
