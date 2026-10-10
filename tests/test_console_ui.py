"""Node checks for src/vaniloop/web/console.js (job dock, Run, Submit, read-only). Mocked fetch, synthetic data only."""

from __future__ import annotations

import re

from test_dash_js import run


def test_paid_stage_asks_for_confirm_then_posts_with_confirm_and_the_dock_follows_the_job():
    """Run on audit shows the confirm with the estimate, and only Confirm sends confirm true.

    Then the dock shows the running job and its log. Without this, a paid stage could start with one click,
    or the dock would stay empty."""
    d = run("con-run")
    assert "This calls Gemini and spends money." in d["confirm"]
    assert "Estimate $1.50." in d["confirm"]
    assert d["posts"] == [{"stage": "audit", "args": {}}, {"stage": "audit", "args": {}, "confirm": True}]
    assert d["hidden"] is False
    assert "running" in d["info1"] and "audit" in d["info1"]
    assert "&#60;img src=x onerror=alert(1)&#62;" in d["log1"]
    assert "done" in d["info2"] and "Cancel" not in d["actions"]
    assert d["refetched"], "a finished job must refresh the stage data"


def test_hostile_log_line_renders_as_text():
    """A log line with an img tag must reach the dock escaped. If it did not, the line would run as markup."""
    d = run("con-run")
    assert "<img" not in d["log1"] + d["info1"]


def test_busy_and_bad_request_show_their_message_and_do_not_throw():
    """A 409 and a 400 from POST /api/jobs show their text in the card. A throw would leave the page frozen."""
    d = run("con-errors")
    assert "Another job is running (j9)." in d["busy"]
    assert "limit too big (field: limit)" in d["bad"]


def test_submit_sends_file_and_id_then_shows_one_row_per_call():
    """A mocked file and an id go to POST /api/submit as base64 and ids, then the submit job starts with confirm.

    The panel shows two result rows and escapes the finding reason. Without it, uploads never reached the audit."""
    d = run("con-submit")
    assert d["submit"] == {"files": [{"name": "a.mp3", "data_b64": "AQID"}], "call_ids": ["c1"]}
    assert d["jobs"] == [{"stage": "submit", "args": {"submission_id": "s1"}, "confirm": True}]
    assert d["panel"].count('class="res"') == 2
    assert "Fatal" in d["panel"] and "Pass" in d["panel"]
    assert "&#60;img src=x onerror=1&#62;" in d["panel"] and "<img" not in d["panel"]
    assert '<option value="c1">' in d["datalist"]


def test_read_only_leaves_no_enabled_control_in_operate():
    """With read_only true every button and input in Run, Submit and the stage Run bar is disabled, with a banner.

    If one stayed enabled the frozen demo folder could be written."""
    d = run("con-readonly")
    for view in ("run", "submit", "audit"):
        tags = re.findall(r"<(?:button|input|select)\b[^>]*>", d[view])
        assert tags
        assert all(" disabled" in t for t in tags if "chk" not in t), (view, [t for t in tags if " disabled" not in t])
    assert "read-only" in d["run"]
    assert d["dock"] == ""


def test_audit_stage_button_prefills_run_and_chains_judge_and_regrade():
    """The Audit panel's Run button opens Run on audit and, after audit ends, runs judge then regrade.

    Without the chain, a fresh audit would leave the judge labels stale."""
    d = run("con-chain")
    assert "Run audit, judge and regrade" in d["bar"]
    assert "After it ends, the console runs judge, regrade." in d["run"]
    assert "Estimate not known." in d["estimate"]
    assert [p["stage"] for p in d["posts"]] == ["audit", "audit", "judge", "regrade"]
    assert d["posts"][1]["confirm"] is True and "confirm" not in d["posts"][2]


def test_page_loads_the_console_assets_and_uses_no_inline_handlers():
    """dash.html links console.css and console.js, and neither the page nor console.js sets an on-handler attribute.

    A missing link leaves the Operate group dead. An inline handler breaks the escape-everything rule."""
    from pathlib import Path

    web = Path(__file__).parent.parent / "src" / "vaniloop" / "web"
    html = (web / "dash.html").read_text()
    assert "/static/console.css" in html and "/static/console.js" in html and 'id="dock"' in html
    assert not re.search(r"\son[a-z]+=", html + (web / "console.js").read_text())
