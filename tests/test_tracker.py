"""Tracker (#130): the state machine, the issues sync, tickets and the web routes. Synthetic data only."""

import json

import pytest
from test_fix import PROMPT, cause, ev_audits, stub, write_run
from test_prove import CAUSE, run

from vaniloop import tracker
from vaniloop.fix import run_fix


def ev(kind, **detail):
    return {"ts_ist": "t", "kind": kind, "detail": detail}


TRANSITIONS = [
    ("open", [ev("detected")]),
    ("watching", [ev("detected"), ev("routed", action="watch")]),
    ("open", [ev("detected"), ev("routed", action="patch")]),
    ("in_fix", [ev("detected"), ev("patch_written")]),
    ("gated_inconclusive", [ev("patch_written"), ev("gated", decision="inconclusive", last_attempt=False)]),
    ("rejected", [ev("patch_written"), ev("gated", decision="reject", last_attempt=False)]),
    ("ready_to_ship", [ev("patch_written"), ev("gated", decision="ship")]),
    ("ready_to_ship", [ev("gated", decision="ship"), ev("approved")]),
    ("shipped", [ev("gated", decision="ship"), ev("approved"), ev("shipped")]),
    ("exhausted", [ev("patch_written"), ev("gated", decision="reject", last_attempt=True)]),
    ("exhausted", [ev("patch_written"), ev("gated", decision="inconclusive", last_attempt=True)]),
    ("in_fix", [ev("gated", decision="reject"), ev("patch_written")]),  # a refix
    ("ticket_open", [ev("detected"), ev("ticket_written")]),
    ("ticket_sent", [ev("ticket_written"), ev("ticket_sent")]),
    ("ticket_resolved", [ev("ticket_written"), ev("ticket_sent"), ev("ticket_resolved")]),
    ("ticket_sent", [ev("ticket_written"), ev("ticket_sent"), ev("ticket_written")]),  # a rerun keeps the state
    ("closed", [ev("shipped"), ev("closed")]),
    ("regressed", [ev("shipped"), ev("regressed")]),
    ("shipped", [ev("shipped"), ev("routed", action="patch"), ev("gated", decision="reject")]),  # ignored when shipped
    ("open", [ev("detected"), ev("count_changed"), ev("rubric_changed"), ev("screened")]),  # no state change
]


@pytest.mark.parametrize(("want", "events"), TRANSITIONS)
def test_state_transition(want, events):
    """Every lifecycle transition of the contract's state list, including exhausted and regressed.

    The board shows a cause only by its state. A wrong fold would show a shipped fix as open or a dead end as live.
    """
    assert tracker.fold(events)[0] == want


def test_fold_state_since_is_the_event_that_changed_the_state():
    """state_since_ist is the time of the last change, not of the last event; a later count event must not reset it."""
    evs = [
        {**ev("detected"), "ts_ist": "a"},
        {**ev("patch_written"), "ts_ist": "b"},
        {**ev("count_changed"), "ts_ist": "c"},
    ]
    assert tracker.fold(evs) == ("in_fix", "b")


def test_record_rejects_an_unknown_kind(tmp_path):
    """A typo in a stage's event kind must fail loudly; a silent row would be skipped by every reader."""
    with pytest.raises(ValueError):
        tracker.record("shiped", "cause_x", runs=tmp_path)


def cause_row(cid="cause_looping", fatal=2, nonfatal=1):
    return {
        "cause_id": cid,
        "name": "n",
        "description": "d",
        "parameter": "looping",
        "type": "prompt",
        "owner": "prompt",
        "fatal_calls": [f"f{i}" for i in range(fatal)],
        "nonfatal_calls": [f"n{i}" for i in range(nonfatal)],
    }


def kinds(runs, cid="cause_looping"):
    return [e["kind"] for e in tracker.events(runs, cid)]


def test_sync_detects_then_reports_count_change_once(tmp_path):
    """issues emits detected for a new cause, count_changed when fatal or nonfatal change, and nothing on a rerun.

    Without the baseline compare every rerun would add events and the history would be noise.
    """
    tracker.sync_causes([cause_row()], tmp_path)
    tracker.sync_causes([cause_row()], tmp_path)
    tracker.sync_causes([cause_row(fatal=5)], tmp_path)
    assert kinds(tmp_path) == ["detected", "count_changed"]
    d = tracker.events(tmp_path)[-1]["detail"]
    assert (d["old"]["fatal"], d["new"]["fatal"]) == (2, 5)


def test_sync_never_compares_counts_across_rubric_versions(tmp_path, monkeypatch):
    """A rubric change between two issues runs gives a rubric_changed event, not count_changed.

    Counts under two rubrics measure different things; a diff would show a fake regression or a fake gain.
    """
    monkeypatch.setattr(tracker, "rubric_version", lambda: "4")
    tracker.sync_causes([cause_row()], tmp_path)
    monkeypatch.setattr(tracker, "rubric_version", lambda: "4+abc123")
    tracker.sync_causes([cause_row(fatal=9)], tmp_path)
    assert kinds(tmp_path) == ["detected", "rubric_changed"]
    assert tracker.events(tmp_path)[-1]["detail"]["note"] == "rubric changed"


def test_shipped_cause_regresses_when_it_comes_back_and_closes_when_it_goes(tmp_path):
    """After a ship, a higher count is regressed and a count of 0 is closed.

    A shipped fix that nobody re-checks would stay green while the fault returned.
    """
    tracker.sync_causes([cause_row(fatal=1, nonfatal=0)], tmp_path)
    tracker.record("shipped", "cause_looping", {"version": "v1"}, runs=tmp_path)
    tracker.sync_causes([cause_row(fatal=3, nonfatal=0)], tmp_path)
    assert tracker.state("cause_looping", tmp_path) == "regressed"
    tracker.record("shipped", "cause_looping", {"version": "v2"}, runs=tmp_path)
    tracker.sync_causes([cause_row(fatal=0, nonfatal=0)], tmp_path)
    assert tracker.state("cause_looping", tmp_path) == "closed"


def test_ticket_sent_and_resolved_move_state_and_header(tmp_path):
    """ticket --sent moves ticket_open to ticket_sent, --resolved to ticket_resolved, and the file header follows.

    Without it a ticket is a file with no sent or resolved state, which is the gap #130 closes. A resolved call
    on a ticket that was never sent must be refused.
    """
    (tmp_path / "tickets").mkdir()
    (tmp_path / "tickets" / "cause_x.md").write_text(tracker.ticket_header("ticket_open") + "# body\n")
    tracker.record("ticket_written", "cause_x", runs=tmp_path)
    with pytest.raises(SystemExit):
        tracker.ticket("cause_x", tmp_path, resolved="done")
    assert tracker.ticket("cause_x", tmp_path, sent="TKT-1") == "ticket_sent"
    assert (tmp_path / "tickets" / "cause_x.md").read_text().startswith("State: ticket_sent\n# body")
    assert tracker.ticket("cause_x", tmp_path, resolved="fixed") == "ticket_resolved"


def test_board_shape(tmp_path):
    """GET /api/tracker data: causes with state, counts and events, plus prompt_versions and the current one."""
    (tmp_path / "causes.json").write_text(json.dumps({"causes": [cause_row()]}))
    tracker.sync_causes([cause_row()], tmp_path)
    b = tracker.board(tmp_path)
    c = b["causes"][0]
    assert (c["cause_id"], c["state"], c["counts"]["fatal"], c["events"][0]["kind"]) == (
        "cause_looping",
        "open",
        2,
        "detected",
    )
    assert c["route"] == "patch"
    assert b["prompt_versions"] == [] and b["current_prompt_version"] is None


def test_fix_records_routed_and_patch_written_once(tmp_path):
    """run_fix records routed and patch_written for a patched cause, and a rerun adds no second routed event.

    Without them the board cannot show in_fix; without the dedupe every fix run would add noise to the history.
    """
    causes = [cause("c1")]
    cp, pp, ap, op = write_run(tmp_path, PROMPT.encode(), causes, ev_audits(causes))
    run_fix(cp, pp, op, llm=stub("line 3: rule text 3", "line 3: better"), audits_path=ap)
    run_fix(cp, pp, op, llm=stub("line 3: rule text 3", "line 3: better"), audits_path=ap)
    assert [e["kind"] for e in tracker.events(op.parent, "c1")] == ["routed", "patch_written"]
    assert tracker.state("c1", op.parent) == "in_fix"


def test_prove_records_a_gated_event(tmp_path):
    """run_prove records a gated event with the decision, the attempt and last_attempt, beside gates.jsonl.

    The board moves a cause to ready_to_ship or rejected from this event alone.
    """
    out, paths, _ = run(tmp_path)
    runs = paths["gates_path"].parent
    d = next(e["detail"] for e in tracker.events(runs, CAUSE) if e["kind"] == "gated")
    assert (d["decision"], d["attempt"], d["last_attempt"]) == (out.result.decision, 1, False)
    assert tracker.state(CAUSE, runs) == {"ship": "ready_to_ship", "reject": "rejected"}.get(
        out.result.decision, "gated_inconclusive"
    )


def test_ticket_command_uses_the_runs_folder(tmp_path):
    """`pipeline --runs DIR ticket` writes its event into DIR/tracker.jsonl, not into the repo's runs/.

    A console workspace must own its tracker log; a write into runs/ would mix two datasets.
    """
    from vaniloop import pipeline

    tracker.record("ticket_written", "cause_x", runs=tmp_path)
    pipeline.main(["--runs", str(tmp_path), "ticket", "--cause", "cause_x", "--sent", "TKT-2"])
    assert tracker.state("cause_x", tmp_path) == "ticket_sent"
