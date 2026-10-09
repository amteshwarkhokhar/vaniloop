"""The one VANI prompt text that Fix and the replays use: clean text, main variant only.

The prompt file is a Google Docs export. It holds U+FEFF characters and Markdown escapes such as `\\_`.
The model copies the visible text, so an anchor taken from the file as it is never matches.
"""

from __future__ import annotations

import re
from pathlib import Path

MAIN_VARIANT_LINES = 1722  # buyer BL flow; later lines are other variants (ideation/06_vani_prompt_map.md)

# Google Docs escapes a backslash before these characters.
_ESCAPE = re.compile(r"\\([_*\[\]#>~\\`.\-!])")


def clean(text: str) -> str:
    """Remove U+FEFF and unescape Markdown escapes. Never adds or removes a newline, so L refs stay valid."""
    return _ESCAPE.sub(r"\1", text.replace("\ufeff", ""))


def main_variant(text: str) -> str:
    """Return lines 1 to MAIN_VARIANT_LINES: the buyer BL flow that nearly all audited calls use."""
    return "\n".join(text.split("\n")[:MAIN_VARIANT_LINES])


def load(path: Path) -> str:
    """Read the prompt file as bytes (keeps CRLF), clean it and cut it to the main variant."""
    return main_variant(clean(Path(path).read_bytes().decode("utf-8")))
