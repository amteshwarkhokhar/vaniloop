import re

from vaniloop import prompt

# Escapes that Google Docs writes: a backslash before one of these characters.
ESCAPE = re.compile(r"\\[_*\[\]#>~\\`.\-!]")

RAW = "\n".join(
    [
        "\ufeff# Role\ufeff",
        "Ask the \\_quantity\\_ and the \\*unit\\*.",
        "",
        "Step 1\\. Greet \\- then \\[confirm\\] the product\\!",
        "Use \\> and \\~ and \\# and a \\` too.",
        "\ufeff",
        "A path C:\\temp keeps its backslash.",
        "last line with no newline after it",
    ]
)


def test_clean_removes_boms_and_escapes_and_keeps_every_line():
    """clean() must drop every U+FEFF and every Markdown escape and keep the line count.

    A changed line count would shift every L ref in a patch and every line of the replays' prompt.
    """
    out = prompt.clean(RAW)
    assert "\ufeff" not in out
    assert not ESCAPE.search(out)
    assert out.count("\n") == RAW.count("\n")
    assert len(out.split("\n")) == len(RAW.split("\n")) == 8
    lines = out.split("\n")
    assert lines[0] == "# Role"
    assert lines[1] == "Ask the _quantity_ and the *unit*."
    assert lines[2] == "" and lines[5] == ""  # blank lines and BOM-only lines stay in place
    assert lines[3] == "Step 1. Greet - then [confirm] the product!"
    assert lines[4] == "Use > and ~ and # and a ` too."


def test_clean_leaves_other_backslashes_and_unescapes_once():
    """A backslash before an ordinary letter stays, and an escaped backslash becomes one backslash.

    Over-eager unescaping would change real prompt text the bot is meant to say.
    """
    assert prompt.clean("C:\\temp and \\n") == "C:\\temp and \\n"
    assert prompt.clean("a\\\\b") == "a\\b"
    assert prompt.clean("a\\\\_b") == "a\\_b"  # one pass only: the unescaped backslash is not an escape


def test_main_variant_keeps_only_lines_1_to_1722():
    """main_variant() must keep lines 1-1722 and drop line 1723 onward, and leave a short text alone.

    Line 1723 onward holds the other variants, so a patch there would edit text the main calls never use.
    """
    text = "\n".join(f"row {i}" for i in range(1, 1801))
    out = prompt.main_variant(text)
    lines = out.split("\n")
    assert len(lines) == 1722
    assert lines[0] == "row 1" and lines[-1] == "row 1722"
    assert "row 1723" not in out
    short = "a\nb\nc\n"
    assert prompt.main_variant(short) == short


def test_load_reads_a_clean_main_variant_from_a_file(tmp_path):
    """load() must give the cleaned main variant, so line N of the result is line N of the file.

    Fix and the replays share this one text. If it drifted from the file, L refs would point at the wrong line.
    """
    rows = [f"\ufeffrow \\_{i}\\_" for i in range(1, 1801)]
    path = tmp_path / "prompt.md"
    path.write_bytes("\n".join(rows).encode("utf-8"))
    out = prompt.load(path)
    assert "\ufeff" not in out and not ESCAPE.search(out)
    lines = out.split("\n")
    assert len(lines) == 1722
    assert lines[0] == "row _1_" and lines[1721] == "row _1722_"


def test_load_keeps_crlf(tmp_path):
    """load() must read bytes and keep CRLF line ends, because an anchor can span a CRLF.

    A universal-newline read would turn CRLF into LF, so every multi-line anchor would miss.
    """
    path = tmp_path / "prompt.md"
    path.write_bytes("\ufeffone\\_x\r\ntwo\r\n".encode())
    assert prompt.load(path) == "one_x\r\ntwo\r\n"
