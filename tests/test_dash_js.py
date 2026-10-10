"""Node checks for src/vaniloop/web/dash.js: it runs against a small DOM stub with synthetic data."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "dash_harness.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def run(scenario: str) -> dict:
    out = subprocess.run(["node", str(HARNESS), scenario], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(out.stdout)


def test_judge_toggle_ignores_a_stale_response():
    """Click Gemini then Jev on Audit, and Jev's answer lands first. The panel must show Jev's numbers.

    The toggle sits in the panel (Audit, Results) and must show Jev pressed. Without a sequence guard the late
    Gemini answer overwrote the panel under a Jev toggle."""
    d = run("race")
    assert "Jev graded 3 calls" in d["panel"]
    assert "graded 7 calls" not in d["panel"]
    assert 'data-view data-judge="jev" aria-pressed="true"' in d["panel"]


def test_malformed_values_do_not_replace_the_panel():
    """An object status, a review item with no labels and an object call_id must render, not throw.

    Before the fix each one swapped the whole panel for 'could not load'."""
    d = run("panels")
    assert "could not load" not in d["issues"] + d["review"]
    assert "Cause one" in d["issues"]
    assert "2 of 2" in d["review"]
    assert "/#/call/call%201" in d["review"]
    assert "/#/call/%5Bobject" not in d["review"]


def test_tickets_given_as_cause_id_strings_render_their_id():
    """The API sends tickets as cause-id strings. Each must show as a card titled with that id."""
    assert "<h3>C-07</h3>" in run("panels")["fix"]


def test_phone_drawer_traps_focus_and_closes_on_escape():
    """With the drawer open the background is inert, Tab wraps inside it and Escape returns focus to the toggle.

    Without it, Tab reached controls hidden behind the open drawer."""
    d = run("drawer")
    assert d["opened"] == {"open": True, "inert": True, "focus": "a1"}
    assert d["wrapFwd"] == "menu"
    assert d["wrapBack"] == "a2"
    assert d["closed"] == {"open": False, "inert": False, "focus": "menu", "expanded": "false"}


def test_judge_panel_draws_one_band_per_label():
    """Protects the Judge panel after the per-label thresholds (#122): each label in the config gets its own band
    with its own marks. If this fails, the panel shows a wrong band or 'could not load'."""
    d = run("judge")
    assert "could not load" not in d["judge"]
    assert d["judge"].count('class="band"') == 2
    assert "Question fits the product: false below 0.2" in d["judge"]
    assert "Same product: false below 0.35" in d["judge"]
    assert 'at or below <span class="mono">0.35</span>' in d["judge"]
