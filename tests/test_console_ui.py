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
    assert "&#60;img src=x onerror=alert(1)&#62;" in d["log1"] and "<img" not in d["log1"] + d["info1"]
    assert "done" in d["info2"] and "Cancel" not in d["actions"]
    assert d["refetched"], "a finished job must refresh the stage data"


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
    """With read_only true every control in Run, Submit, Rubric, Tune and the stage Run bar is disabled.

    If one stayed enabled the frozen demo folder could be written."""
    d = run("con-readonly")
    for view in ("run", "submit", "audit", "rubric", "tune"):
        tags = re.findall(r"<(?:button|input|select|textarea)\b[^>]*>", d[view])
        assert tags
        assert all(" disabled" in t for t in tags if "chk" not in t), (view, [t for t in tags if " disabled" not in t])
    assert "read-only" in d["run"]


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


def test_log_fetch_errors_back_off_and_keep_polling_until_the_job_ends():
    """Six 503s from the log endpoint retry after 1, 2, 4, 8, 8 and 8 s, then a good answer ends the job.

    Before the fix the first 503 left no timer, so the dock froze on a running job for good."""
    d = run("con-backoff")
    assert d["delays"][:6] == [[1000], [2000], [4000], [8000], [8000], [8000]]
    assert "done" in d["info"]


def test_cancel_posts_json_headers_and_an_empty_object():
    """Cancel must send Content-Type application/json and the body {}. Without it the server answers 415."""
    (c,) = run("con-cancel")
    assert c["method"] == "POST" and c["body"] == {}
    assert c["headers"]["Content-Type"] == "application/json"


def test_rubric_hostile_check_name_renders_as_text():
    """A check name with an img tag must reach the Rubric page escaped. Unescaped, it would run as markup."""
    d = run("con-rubric")
    assert "&#60;img src=x onerror=alert(1)&#62;" in d["panel"] and "<img" not in d["panel"]


def test_rubric_saves_only_changed_fields_and_shows_a_400_next_to_its_field():
    """Two edits send a PUT with just those two ids, never the untouched Dead air param.

    The 400 text sits in the Frustrated field's block, before the next field. If it did not, the person could not
    tell which number to fix. After a good save the banner offers the re-judge chain: judge, regrade, issues."""
    d = run("con-rubric")
    first, second = d["put"]
    assert list(first["params"]) == ["rules.FRUSTRATED_P"] and first["params"]["rules.FRUSTRATED_P"] == 0.9
    assert list(first["questions"]) == ["spec_fit"] and first["questions"]["spec_fit"]["instructions"] == "New text"
    assert second["params"] == {"rules.FRUSTRATED_P": 0.7}
    field = d["err"].split('for="p-looping-rules.FRUSTRATED_P"')[1].split('for="p-looping-rules.DEAD_AIR_SEC"')[0]
    assert "must be at most 0.8" in field
    assert "must be at most 0.8" not in d["saved"]
    assert "Re-judge and regrade" in d["saved"]
    assert d["posts"] == [{"stage": "judge", "args": {}}] and d["chain"] == ["regrade", "issues"]


def test_tune_debounces_three_moves_into_one_post_and_renders_dev_and_test():
    """Three quick slider moves wait 250 ms and send one POST /api/tune with the last value.

    The table shows dev and held-out test rows with the change against the current values and the flips. Without
    the debounce each tick would call the server. A 409 shows its text with a Run judge button."""
    d = run("con-tune")
    assert d["timers"] == [250] and d["beforeFire"] == 0
    assert len(d["after"]) == 1 and d["after"][0]["thresholds"]["question_fits"]["true_at"] == 0.65
    assert "dev" in d["res"] and "held-out test" in d["res"] and '<span class="ok">+3</span>' in d["res"]
    assert "Labels that would flip over all 712 calls" in d["res"]
    assert d["put"] == [{"params": {"jev.Q_TRUE_AT": 0.65}, "questions": {}}]
    assert "no raw Jev answers: run judge" in d["noraw"] and "Run judge" in d["noraw"]


def test_editing_only_a_question_enables_save_and_sends_only_that_question():
    """A Jev question edit with no number edit must turn Save on, and the PUT carries just that question.

    Before the fix only number edits updated the dirty state, so a question-only edit left Save disabled."""
    d = run("con-rubric-q")
    assert d["afterEdit"] is False
    (put,) = d["put"]
    assert put["params"] == {} and list(put["questions"]) == ["spec_fit"]
    assert put["questions"]["spec_fit"]["true"]["examples"] == ["one", "two"]
    assert "value" not in put["questions"]["spec_fit"], "the PUT carries the question texts, not the API wrapper"
