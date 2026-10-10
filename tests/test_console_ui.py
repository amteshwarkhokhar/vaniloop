"""Node checks for src/vaniloop/web/console.js (job dock, Run, Submit, read-only). Mocked fetch, synthetic data only."""

from __future__ import annotations

import re

from test_dash_js import run


def test_paid_stage_asks_for_confirm_then_posts_with_confirm_and_the_dock_follows_the_job():
    """Run on audit shows the confirm with the estimate, and only Confirm sends confirm true.

    Then the dock shows the running job by its plain name ("Grade calls") and its log. Without this, a paid stage
    could start with one click, or the dock would stay empty."""
    d = run("con-run")
    assert "This calls Gemini and spends money." in d["confirm"]
    assert "Estimate $1.50." in d["confirm"]
    assert d["posts"] == [{"stage": "audit", "args": {}}, {"stage": "audit", "args": {}, "confirm": True}]
    assert d["hidden"] is False
    assert "Running" in d["info1"] and "Grade calls" in d["info1"]
    assert "&#60;img src=x onerror=alert(1)&#62;" in d["log1"] and "<img" not in d["log1"] + d["info1"]
    assert "Done" in d["info2"] and "Cancel" not in d["actions"]
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
    """With read_only true every control in Run, Submit, Rubric, Tune, Tracker and the stage Run bar is disabled.

    Only a tag marked data-view stays on, because it changes the view and writes nothing (the judge toggle).
    If any other control stayed enabled the frozen demo folder could be written."""
    d = run("con-readonly")
    for view in ("run", "submit", "audit", "rubric", "tune", "tracker"):
        tags = re.findall(r"<(?:button|input|select|textarea)\b[^>]*>", d[view])
        assert tags
        writers = [t for t in tags if "data-view" not in t]
        assert all(" disabled" in t for t in writers), (view, [t for t in writers if " disabled" not in t])
    assert "read-only" in d["run"]


def test_audit_stage_button_prefills_run_and_chains_judge_and_regrade():
    """The Audit panel's Run button opens Run on audit and, after audit ends, runs judge then regrade.

    Without the chain, a fresh audit would leave the judge labels stale."""
    d = run("con-chain")
    assert "Run audit, judge and regrade" in d["bar"]
    assert "After it ends, the console runs Label with Jev, then Regrade with Jev labels." in d["run"]
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
    assert "Done" in d["info"]


def test_cancel_posts_json_headers_and_an_empty_object():
    """Cancel must send Content-Type application/json and the body {}. Without it the server answers 415."""
    (c,) = run("con-cancel")
    assert c["method"] == "POST" and c["body"] == {}
    assert c["headers"]["Content-Type"] == "application/json"


def test_rubric_hostile_check_name_renders_as_text():
    """A check name with an img tag must reach the Rubric page escaped. Unescaped, it would run as markup."""
    d = run("con-rubric")
    assert "&#60;img src=x onerror=alert(1)&#62;" in d["panel"] and "<img" not in d["panel"]


def test_rubric_is_rows_with_closed_disclosures_and_no_cards():
    """The Rubric page is one grouped list: a closed disclosure per check and per Jev question, no card per check.

    Each row names the check, says who judges it and how many numbers it holds. If a disclosure rendered open, or the
    cards came back, the page would again be a wall of fields that the owner rejected as cluttered."""
    d = run("con-rubric")
    rows = re.findall(r'<details class="rrow"( open)?>', d["panel"])
    assert len(rows) >= 2 and not any(rows), "every row starts closed"
    assert "op-card" not in d["panel"] and 'class="rlist' in d["panel"]
    assert "No looping" in d["panel"] and "numbers</span>" in d["panel"]


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
    assert "must be an object" in d["banner"], "a 400 whose field has no input must still show"
    (put,) = d["put"]
    assert put["params"] == {} and list(put["questions"]) == ["spec_fit"]
    assert put["questions"]["spec_fit"]["true"]["examples"] == ["one", "two"]
    assert "value" not in put["questions"]["spec_fit"], "the PUT carries the question texts, not the API wrapper"


def test_tracker_groups_causes_into_columns_and_escapes_hostile_text():
    """Each state lands in its column, a regressed cause is red, and hostile names and event text show as text.

    Without the grouping a cause could vanish from the board or sit in the wrong column. Unescaped text would run."""
    d = run("con-tracker")
    found = re.findall(r'<section class="col" aria-label="([^"]+)">(.*?)</section>', d["board"], re.S)
    cols = {m[0]: m[1] for m in found}
    assert list(cols) == ["Open", "In fix", "Ready to ship", "Shipped", "Tickets", "Watching", "Done or failed"]
    assert "Cause c1" in cols["Open"] and "Cause c2" in cols["In fix"] and "Cause c3" in cols["Ready to ship"]
    assert "Cause c4" in cols["Tickets"] and "Cause c5" in cols["Tickets"] and "Cause c9" in cols["Done or failed"]
    assert "chip t-fatal" in cols["Done or failed"] and "regressed" in cols["Done or failed"]
    assert "&#60;img src=x onerror=alert(1)&#62;" in d["board"] and "<img" not in d["board"]
    assert "v2" in d["board"] and "current" in d["board"] and "from v1" in d["board"]


def test_tracker_actions_post_the_contract_bodies_and_show_errors():
    """Mark ticket sent needs a ref and posts the trimmed ref. Approve and ship asks first and shows the gate numbers.

    A 400 and a 409 show their text in the card. Without the check a ship could run with one click and no sight of p."""
    d = run("con-tracker")
    assert "Enter the ticket id or link first." in d["empty"]
    assert "ref is required" in d["bad"]
    for part in ("p value <b>0.01</b>", "lift <b>0.25</b>", "guard <b>pass</b>"):
        assert part in d["confirm"]
    assert "gate no longer ships" in d["conflict"]
    assert "Ship a passed patch" in d["dock"] and "Done" in d["dock"], "the 202 job must reach the dock"
    assert d["rereads"] >= 1 and "from v2" in d["after"], "a finished ship must reload the board and prompt versions"
    assert d["posts"] == [
        ["/api/tracker/c4/ticket", {"action": "sent", "ref": "T-1"}],
        ["/api/tracker/c4/ticket", {"action": "sent", "ref": "T-1"}],
        ["/api/tracker/c3/ship", {"confirm": True}],
        ["/api/tracker/c3/ship", {"confirm": True}],
    ]


def test_sidebar_lists_tracker_then_results_configuration_and_tools_and_tracker_times_read_as_ist():
    """The sidebar shows Tracker with "2 waiting" first, then Results (the five steps in order), then Configuration
    (Rubric and Scoring criteria among them), then Tools.

    The count comes from GET /api/tracker at boot, before the Tracker page is opened. Tracker times show as
    "09:00 10 Oct", not raw ISO. If the order broke, the waiting count, the rubric and Run would be hard to find."""
    d = run("con-tracker")
    nav = d["nav"]
    assert nav.index("Tracker") < nav.index("Results") < nav.index("Configuration") < nav.index("Tools")
    steps = [nav.index(n) for n in ("Ingest", "Audit", "Issues", "Fix", "Test", "Configuration")]
    assert steps == sorted(steps)
    conf = nav[nav.index("Configuration") : nav.index("Tools")]
    assert "Rubric" in conf and "Scoring criteria" in conf and "Judges" in conf
    assert "2 waiting" in nav[: nav.index("Results")] and "Run one step" in nav[nav.index("Tools") :]
    assert "since 09:00 10 Oct" in d["board"] and "2026-10-10T" not in d["board"]
    assert "10:00 10 Oct" in d["board"], "a time that is not ISO passes through unchanged"


def test_tab_marks_its_page_and_its_family_row():
    """On #/review (a tab of Audit) the page says "Step 2 of 5", the Judge accuracy tab has aria-current page and the
    Audit row is current. On #/thresholds (a tab of Scoring criteria) the kicker says Configuration.

    Without it a tab page would not show where it belongs, and the owner could not find the way back. A tab is not a
    sidebar row, so the sidebar stays short."""
    d = run("con-tabs")
    assert "Step 2 of 5" in d["review"]
    assert '<a href="#/review" aria-current="page">Judge accuracy</a>' in d["review"]
    assert '<a href="#/audit">Results</a>' in d["review"] and 'aria-current="page">Results' not in d["review"]
    assert re.search(r'data-go="audit" aria-current="step"', d["nav"])
    assert 'data-go="review"' not in d["nav"] and 'data-go="thresholds"' not in d["nav"]
    assert '<p class="kicker">Configuration</p>' in d["thresholds"]
    assert '<a href="#/tune">Tune</a>' in d["thresholds"]
    assert '<a href="#/thresholds" aria-current="page">All constants</a>' in d["thresholds"]


def test_home_shows_the_calls_one_run_button_and_five_steps_in_order():
    """Home is the first page: one square per call (worst first, ids escaped), the plan line, and the five steps.

    It is the owner's main page. If the order, the escaping or the plan broke, the one-button loop would show the
    wrong thing or run untrusted text."""
    d = run("con-home")
    idle = d["idle"]
    assert idle.count('class="cd ') == 3
    assert idle.index("c2&#60;img src=x&#62;") < idle.index('title="c1'), "Fatal comes before Pass"
    assert "<img" not in idle and 'href="/#/call/c1"' in idle
    assert "No new calls to grade · 2 patches to test · About $0.90" in idle
    assert "&#60;b&#62;2&#60;/b&#62;" in idle, "the plan's why text is escaped"
    heads = [idle.index(f"<h3>{n}</h3>") for n in ("Ingest", "Audit", "Issues", "Fix", "Test")]
    assert heads == sorted(heads)
    run_btn = re.search(r'<button[^>]*data-op="home-run"[^>]*>', idle).group(0)
    assert " disabled" not in run_btn
    assert d["nav"].index("Home") < d["nav"].index("Tracker")


def test_home_run_confirms_then_each_step_lights_up_from_the_loop_log():
    """Run the loop asks to confirm (Cancel first), then posts confirm true. LOOP_STEP lines light the cards.

    Mid-run Ingest is done, Audit runs and the rest wait. After LOOP_DONE all five are done and the summary shows.
    Without it the main page would not show the loop working, or a paid run could start with one click."""
    d = run("con-home")
    assert d["posts"] == [{"stage": "loop", "args": {}}, {"stage": "loop", "args": {}, "confirm": True}]
    assert "This run calls paid models." in d["confirm"] and "Estimate $0.90." in d["confirm"]
    assert d["confirm"].index('data-op="home-cancel"') < d["confirm"].index('data-op="home-go"')
    mid = d["mid"]
    assert 'class="st st-done" data-step="ingest"' in mid and 'class="st st-run" data-step="audit"' in mid
    assert 'class="st st-wait" data-step="issues"' in mid and "Jev labels each turn" in mid
    assert all(f'class="st st-done" data-step="{s}"' in d["done"] for s in ("ingest", "audit", "issues", "fix", "gate"))
    assert "1 patch ready to ship" in d["done"] and "2 tickets" in d["done"] and "$0.42" in d["done"]


def test_home_read_only_turns_run_off_and_replay_writes_nothing():
    """On a read-only folder Run is disabled and only Replay (data-view) stays on. Replay walks all five steps.

    Replay reads the saved results and posts no job. If Run stayed on, the frozen demo folder could be written."""
    d = run("con-home-ro")
    tags = re.findall(r"<(?:button|input|select|textarea)\b[^>]*>", d["ro"])
    writers = [t for t in tags if "data-view" not in t]
    assert writers and all(" disabled" in t for t in writers)
    assert "read-only" in d["ro"]
    assert 'class="st st-run" data-step="ingest"' in d["replay"]
    assert all(f'class="st st-done" data-step="{s}"' in d["end"] for s in ("ingest", "audit", "issues", "fix", "gate"))
    assert "Nothing was graded again." in d["end"] and d["posts"] == []


def test_home_a_real_job_replaces_a_replay():
    """A job started from another page (Run one step) after a Replay shows on Home as that job, not as the replay.

    Codex found Home kept the replay's progress. Then the owner would see a fake step while a real one ran."""
    d = run("con-home-job")
    assert 'class="st st-run" data-step="audit"' in d["home"]
    assert 'class="st st-run" data-step="ingest"' not in d["home"], "the replay's first step must be gone"


CALL_LINK = re.compile(r'href="/#/call/([^"]+)"')
SCRIPT_ID = "%3Cscript%3Ealert(1)%3C%2Fscript%3E"


def test_audit_calls_list_fatal_first_with_transcript_links_and_filter_without_a_refetch():
    """The Audit page's Calls section sits after Checks and opens on the Fatal calls in the API's order. Each row links
    to /#/call/<id> with a Transcript label, names the check and shows the length as m:ss.

    Pass shows 25 of its 31 rows and "Show 6 more", and more shows all 31. Both clicks reuse the one boot read of
    /api/calls. If this broke, the owner could not reach the transcript behind a grade, or each click refetched."""
    d = run("con-calls")
    fatal = d["fatal"]
    assert fatal.index(">Checks<") < fatal.index(">Calls<") < fatal.index(">Flags<")
    assert 'data-view data-calls="Fatal" aria-pressed="true">Fatal <span class="n">2</span>' in fatal
    assert 'Pass <span class="n">31</span>' in fatal and 'All <span class="n">35</span>' in fatal
    assert CALL_LINK.findall(fatal) == ["call-001", SCRIPT_ID]
    link = 'href="/#/call/call-001">call-001 <small>Transcript</small></a>'
    assert link + '<span class="ck">Quantity asked</span>' in fatal
    assert '<span class="dur">2:05</span>' in fatal and '<span class="dur">1:01</span>' in fatal
    assert 'data-calls="Pass" aria-pressed="true"' in d["pass"]
    links = CALL_LINK.findall(d["pass"])
    assert len(links) == 25 and all(x.startswith("call-p") for x in links)
    assert 'data-view data-more="calls">Show 6 more</button>' in d["pass"]
    assert len(CALL_LINK.findall(d["more"])) == 31 and "data-more" not in d["more"]
    assert d["reads"] == 1, "a filter or more click must redraw from memory, not fetch /api/calls again"


def test_audit_calls_show_a_hostile_call_id_and_reason_as_text():
    """A call id with a script tag and a reason with an img tag reach the Calls rows escaped, and the link is
    percent-encoded. Unescaped, a call id from the data folder would run as markup on the Audit page."""
    fatal = run("con-calls")["fatal"]
    assert "&#60;script&#62;alert(1)&#60;/script&#62; <small>Transcript</small>" in fatal
    assert 'title="Asked &#60;img src=x onerror=alert(1)&#62;"' in fatal
    assert "<script" not in fatal and "<img" not in fatal


def test_issues_cause_lists_its_own_calls_and_a_failed_read_stays_in_its_section():
    """Each Issues cause lists its own calls (fatal_calls and nonfatal_calls from GET /api/causes), most severe first.

    C-01 and its channel twin C-02 grade the same check. Matching on the first fault's check put the same calls under
    both, plus NeedsReview calls that no cause counts. On the real runs data 9 of 12 causes listed calls that were not
    theirs. C-01 must list call-001 and call-002 (whose first fault is another check), and C-02 only its own call.

    A refresh after a job reads both lists again. A failed /api/calls read shows its error only in the Audit page's
    Calls section, and a failed /api/causes read only drops the disclosures. Without this a cause showed another
    cause's calls as its proof, or one failed read blanked a whole page."""
    d = run("con-calls")
    boxes = [b.split("</details>")[0] for b in d["issues"].split('<details class="calls-d">')[1:]]
    assert len(boxes) == 2
    assert "<summary>Calls with this fault (2)</summary>" in boxes[0]
    assert CALL_LINK.findall(boxes[0]) == ["call-001", "call-002"]
    assert "<summary>Calls with this fault (1)</summary>" in boxes[1]
    assert CALL_LINK.findall(boxes[1]) == [SCRIPT_ID]
    assert "call-003" not in d["issues"]
    fail = d["auditFail"]
    assert fail.count("could not load") == 1 and "calls file unreadable" in fail
    assert fail.index(">Checks<") < fail.index(">Calls<") < fail.index("could not load") < fail.index(">Flags<")
    assert "<details" not in d["issuesFail"] and "Cause one" in d["issuesFail"]
    assert "could not load" not in d["auditCausesFail"] and len(CALL_LINK.findall(d["auditCausesFail"])) == 31
    assert "<details" not in d["issuesCausesFail"] and "Cause two" in d["issuesCausesFail"]
    assert "could not load" not in d["issuesCausesFail"]


def test_test_page_shows_paired_replays_per_call_and_hides_statistics():
    """The Test page shows a verdict sentence, a paired-replay row per call and the statistics in a closed disclosure.

    It protects the one-glance approval view: the better/worse counts, one old and one new strip per call, the escaped
    call id, and a single "N of M tests passed." line in place of the repeated "Nothing has passed" banner. Malformed
    per_call rows (empty id, null, a number id) must be dropped. If it failed, a reviewer would again have to read
    p-values to approve a fix, or a hostile call id could inject markup."""
    p = run("con-gate")["panel"]
    assert "Old prompt failed 50% of replays, new prompt 50%. 2 calls got better, 1 got worse." in p
    assert p.count('class="trial"') == 6
    assert p.count('class="trial" role="img" data-l="Old"') == 3, "a strip is an image with a text label on phones"
    squares = len(re.findall(r'<i class="(?:bad|good)">', p))
    assert 'prompt: 45 of 50 replays failed"' in p and squares == 4 * 3 + 2 * 40, (
        "k above 40 draws 40 scaled squares but speaks the true counts"
    )
    for label in ("3 of 3 replays failed", "1 of 3 replays failed", "0 of 3 replays failed", "2 of 3 replays failed"):
        assert f'prompt: {label}"' in p, "the strip label names the prompt and the count"
    assert p.count('class="pairs"') == 1 and p.count("<li>") == 3
    assert "better</span>" in p and "worse</span>" in p
    assert "<img" not in p and "g&#60;img src=x onerror=1&#62;" in p
    assert 'href="/#/call/g%3Cimg%20src%3Dx%20onerror%3D1%3E"' in p
    assert '<details class="gate-d">' in p and '<details class="gate-d" open' not in p
    assert "0 of 2 tests passed." in p and "1 of 2 tests passed." not in p
    assert "Nothing has passed" not in p
    assert "Cause one" in p and "C-09" in p
    assert p.count('class="verdict"') == 2


def test_pipeline_page_shows_every_step_and_one_call_with_its_faults_in_red():
    """The Pipeline page lays out the five steps in one lane under the run waveform. Picking a call shows its
    transcript with the fault turn marked in its tone, the evidence quote marked, the Jev label, and one pane per step.

    The owner asked to see the real transcript with the fault in red and each step's output for the call. If this
    broke, the page would show totals only, or unescaped call text."""
    d = run("con-flow")
    assert d["all"].count('<section class="pn') == 5 and 'class="wave"' in d["all"]
    assert d["all"].count("<i class=") == 70, "ten waveform bars per loop step"
    assert "Last run: done at 17:34:52 10 Oct IST" in d["all"] and d["all"].count('class="w-done"') == 70
    assert 'data-pick="c1&#60;b&#62;"' in d["all"] and "<b>c1" not in d["all"]
    c = d["call"]
    heads = [c.index(f"<h2>{n}</h2>") for n in ("Transcript", "Ingest", "Audit", "Judge", "Issues", "Fix", "Test")]
    assert heads == sorted(heads)
    assert 'id="ft-2" class="tt bot fault t-fatal"' in c and '<mark class="t-fatal">Which colour</mark>' in c
    assert "&#60;img src=x&#62;" in c and "<img" not in c
    assert c.count("<mark") == 1, "an overlapping quote or one that only matches inside an entity marks nothing"
    assert "✕ question does not fit" in c and "Wrong spec question" in c and "ask grade" in c
    assert "old prompt failed 3 of 3 · new failed 1 of 3" in c and "c1.mp3" in c and "/x/y" not in c


CALL_LINK = re.compile(r'href="/#/call/([^"]+)"')
SCRIPT_ID = "%3Cscript%3Ealert(1)%3C%2Fscript%3E"


def test_audit_calls_list_fatal_first_with_transcript_links_and_filter_without_a_refetch():
    """The Audit page's Calls section sits after Checks and opens on the Fatal calls in the API's order. Each row links
    to /#/call/<id> with a Transcript label, names the check and shows the length as m:ss.

    Pass shows 25 of its 31 rows and "Show 6 more", and more shows all 31. Both clicks reuse the one boot read of
    /api/calls. If this broke, the owner could not reach the transcript behind a grade, or each click refetched."""
    d = run("con-calls")
    fatal = d["fatal"]
    assert fatal.index(">Checks<") < fatal.index(">Calls<") < fatal.index(">Flags<")
    assert 'data-view data-calls="Fatal" aria-pressed="true">Fatal <span class="n">2</span>' in fatal
    assert 'Pass <span class="n">31</span>' in fatal and 'All <span class="n">35</span>' in fatal
    assert CALL_LINK.findall(fatal) == ["call-001", SCRIPT_ID]
    link = 'href="/#/call/call-001">call-001 <small>Transcript</small></a>'
    assert link + '<span class="ck">Quantity asked</span>' in fatal
    assert '<span class="dur">2:05</span>' in fatal and '<span class="dur">1:01</span>' in fatal
    assert 'data-calls="Pass" aria-pressed="true"' in d["pass"]
    links = CALL_LINK.findall(d["pass"])
    assert len(links) == 25 and all(x.startswith("call-p") for x in links)
    assert 'data-view data-more="calls">Show 6 more</button>' in d["pass"]
    assert len(CALL_LINK.findall(d["more"])) == 31 and "data-more" not in d["more"]
    assert d["reads"] == 1, "a filter or more click must redraw from memory, not fetch /api/calls again"


def test_audit_calls_show_a_hostile_call_id_and_reason_as_text():
    """A call id with a script tag and a reason with an img tag reach the Calls rows escaped, and the link is
    percent-encoded. Unescaped, a call id from the data folder would run as markup on the Audit page."""
    fatal = run("con-calls")["fatal"]
    assert "&#60;script&#62;alert(1)&#60;/script&#62; <small>Transcript</small>" in fatal
    assert 'title="Asked &#60;img src=x onerror=alert(1)&#62;"' in fatal
    assert "<script" not in fatal and "<img" not in fatal


def test_issues_cause_lists_its_own_calls_and_a_failed_read_stays_in_its_section():
    """Each Issues cause lists its own calls (fatal_calls and nonfatal_calls from GET /api/causes), most severe first.

    C-01 and its channel twin C-02 grade the same check. Matching on the first fault's check put the same calls under
    both, plus NeedsReview calls that no cause counts. On the real runs data 9 of 12 causes listed calls that were not
    theirs. C-01 must list call-001 and call-002 (whose first fault is another check), and C-02 only its own call.

    A refresh after a job reads both lists again. A failed /api/calls read shows its error only in the Audit page's
    Calls section, and a failed /api/causes read only drops the disclosures. Without this a cause showed another
    cause's calls as its proof, or one failed read blanked a whole page."""
    d = run("con-calls")
    boxes = [b.split("</details>")[0] for b in d["issues"].split('<details class="calls-d">')[1:]]
    assert len(boxes) == 2
    assert "<summary>Calls with this fault (2)</summary>" in boxes[0]
    assert CALL_LINK.findall(boxes[0]) == ["call-001", "call-002"]
    assert "<summary>Calls with this fault (1)</summary>" in boxes[1]
    assert CALL_LINK.findall(boxes[1]) == [SCRIPT_ID]
    assert "call-003" not in d["issues"]
    fail = d["auditFail"]
    assert fail.count("could not load") == 1 and "calls file unreadable" in fail
    assert fail.index(">Checks<") < fail.index(">Calls<") < fail.index("could not load") < fail.index(">Flags<")
    assert "<details" not in d["issuesFail"] and "Cause one" in d["issuesFail"]
    assert "could not load" not in d["auditCausesFail"] and len(CALL_LINK.findall(d["auditCausesFail"])) == 31
    assert "<details" not in d["issuesCausesFail"] and "Cause two" in d["issuesCausesFail"]
    assert "could not load" not in d["issuesCausesFail"]
