"""Scans the web assets for the plain look: no display font, italic, uppercase labels or letter spacing."""

from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).parent.parent / "src" / "vaniloop" / "web"
TITLES = ("h1", ".ptitle", ".view-title", ".call-id", ".hero-line")
FORBIDDEN = r"Bodoni|Didot|Georgia|Hanken|(?<!sans-)serif|var\(--display\)|italic|oblique|text-transform\s*:\s*(?!none)|font-variant-caps"


def test_web_css_has_no_display_font_italic_caps_or_tracking():
    """No CSS or HTML under src/vaniloop/web has a display font, italic, uppercase, small caps or letter spacing.

    Only -0.01em on a title (h1) is allowed. The owner found the calligraphic look hard to read, so a rule that
    brings it back must fail here before it reaches the page."""
    bad = []
    for path in [*WEB.rglob("*.css"), *WEB.rglob("*.html")]:
        text = path.read_text()
        bad += [f"{path.name}: {m.group(0)}" for m in re.finditer(FORBIDDEN, text)]
        for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", text):
            for value in re.findall(r"letter-spacing\s*:\s*([^;}]+)", body):
                if value.strip() not in ("-.01em", "-0.01em") or not any(t in selector for t in TITLES):
                    bad.append(f"{path.name}: {selector.strip()} letter-spacing {value.strip()}")
    assert not bad, bad
