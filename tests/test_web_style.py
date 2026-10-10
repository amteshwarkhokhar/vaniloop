"""Scans the web assets for the plain look: no display font, italic, uppercase labels or letter spacing."""

from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).parent.parent / "src" / "vaniloop" / "web"
TITLES = ("h1", ".ptitle", ".view-title", ".call-id", ".hero-line")
FORBIDDEN = (
    r"Bodoni|Didot|Georgia|Hanken|(?<!sans-)serif|var\(--display\)|italic|oblique"
    r"|text-transform\s*:\s*(?!none)|font-variant-caps"
)


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


def test_web_uses_the_system_fonts_and_the_brief_colours():
    """The web assets use the system font stack and the brief's colours, with no Google Fonts request.

    A font link would make an external request, and a wrong accent or text colour would fail the brief's contrast."""
    app = (WEB / "app.css").read_text()
    assert 'system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif' in app
    assert "ui-monospace, Menlo, Consolas, monospace" in app
    for value in ("#1D1D1F", "#515154", "#0066CC", "#F5F5F7", "#D2D2D7", "#86868B"):
        assert value in app, value
    for name in ("dash.html", "index.html"):
        text = (WEB / name).read_text()
        assert "fonts.googleapis.com" not in text and "fonts.gstatic.com" not in text, name
