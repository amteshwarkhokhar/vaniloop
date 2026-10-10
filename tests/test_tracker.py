"""Tracker (#130): the state machine, the issues sync, tickets and the web routes. Synthetic data only."""

import json
import threading

import pytest
from test_fix import PROMPT, cause, ev_audits, stub, write_run
from test_prove import CAUSE, run

from vaniloop import tracker
from vaniloop.fix import run_fix


def ev(kind, **detail):
    return {"ts_ist": "t", "kind": kind, "detail": detail}


SHIP = [
    {"ts_ist": "t", "kind": "gated", "detail": {"decision": "ship"}},
    {"ts_ist": "t", "kind": "shipped", "detail": {}},
]

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
    # an event that skips its prerequisite is kept in the log but changes nothing
    ("open", [ev("detected"), ev("shipped")]),
    ("in_fix", [ev("patch_written"), ev("shipped")]),
    ("rejected", [ev("gated", decision="reject"), ev("shipped")]),
    ("open", [ev("detected"), ev("ticket_sent")]),
    ("ticket_open", [ev("ticket_written"), ev("ticket_resolved")]),
    ("ticket_resolved", [ev("ticket_written"), ev("ticket_sent"), ev("ticket_resolved"), ev("ticket_sent")]),
    ("open", [ev("detected"), ev("closed")]),
    ("open", [ev("detected"), ev("regressed")]),
    ("exhausted", [ev("patch_written"), ev("gated", decision="reject", last_attempt=True)]),
    ("exhausted", [ev("patch_written"), ev("gated", decision="inconclusive", last_attempt=True)]),
    ("in_fix", [ev("gated", decision="reject"), ev("patch_written")]),  # a refix
    ("ticket_open", [ev("detected"), ev("ticket_written")]),
    ("ticket_sent", [ev("ticket_written"), ev("ticket_sent")]),
    ("ticket_resolved", [ev("ticket_written"), ev("ticket_sent"), ev("ticket_resolved")]),
    ("ticket_sent", [ev("ticket_written"), ev("ticket_sent"), ev("ticket_written")]),  # a rerun keeps the state
    ("closed", [*SHIP, ev("closed")]),
    ("regressed", [*SHIP, ev("regressed")]),
    ("closed", [*SHIP, ev("regressed"), ev("closed")]),
    ("regressed", [*SHIP, ev("closed"), ev("regressed")]),
    ("shipped", [*SHIP, ev("routed", action="patch"), ev("gated", decision="reject")]),  # ignored when shipped
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


def ship(runs, cid="cause_looping"):
    """What a real ship leaves in the log: a gated ship event, then shipped."""
    tracker.record("gated", cid, {"decision": "ship"}, runs=runs)
    tracker.record("shipped", cid, {"version": "v1"}, runs=runs)


def test_shipped_cause_regresses_when_it_comes_back_and_closes_when_it_goes(tmp_path):
    """After a ship, a higher count is regressed and a count of 0 is closed.

    A shipped fix that nobody re-checks would stay green while the fault returned.
    """
    tracker.sync_causes([cause_row(fatal=1, nonfatal=0)], tmp_path)
    ship(tmp_path)
    tracker.sync_causes([cause_row(fatal=3, nonfatal=0)], tmp_path)
    assert tracker.state("cause_looping", tmp_path) == "regressed"
    tracker.sync_causes([cause_row(fatal=0, nonfatal=0)], tmp_path)
    assert tracker.state("cause_looping", tmp_path) == "closed"


def test_a_closed_fault_that_returns_is_regressed_and_closes_again_when_it_goes(tmp_path):
    """detect, ship, vanish, reappear gives regressed (even at the same count); vanishing again gives closed.

    Without the closed -> regressed path a returning fault stayed closed, hidden from the board.
    """
    tracker.sync_causes([cause_row(fatal=2, nonfatal=0)], tmp_path)
    ship(tmp_path)
    tracker.sync_causes([], tmp_path)
    assert tracker.state("cause_looping", tmp_path) == "closed"
    tracker.sync_causes([cause_row(fatal=2, nonfatal=0)], tmp_path)  # the same count as at the ship
    assert tracker.state("cause_looping", tmp_path) == "regressed"
    tracker.sync_causes([], tmp_path)
    assert tracker.state("cause_looping", tmp_path) == "closed"


def test_a_vanished_shipped_cause_is_not_closed_under_a_new_rubric(tmp_path, monkeypatch):
    """A shipped cause that disappears after the rubric changed gets rubric_changed and stays shipped.

    A new rubric can stop counting the fault; that is not proof the fix worked.
    """
    monkeypatch.setattr(tracker, "rubric_version", lambda: "4")
    tracker.sync_causes([cause_row()], tmp_path)
    ship(tmp_path)
    monkeypatch.setattr(tracker, "rubric_version", lambda: "4+abc123")
    tracker.sync_causes([], tmp_path)
    assert tracker.state("cause_looping", tmp_path) == "shipped"
    assert kinds(tmp_path)[-1] == "rubric_changed"
    tracker.sync_causes([], tmp_path)  # a rerun must not close it either: the ship was under the old rubric
    assert tracker.state("cause_looping", tmp_path) == "shipped"


def test_a_rubric_change_updates_the_baseline_even_when_the_counts_are_equal(tmp_path, monkeypatch):
    """Equal counts under a new rubric still write rubric_changed, so the next run compares like with like.

    Without it the baseline kept the old rubric and the next real change was called a rubric change forever.
    """
    monkeypatch.setattr(tracker, "rubric_version", lambda: "4")
    tracker.sync_causes([cause_row()], tmp_path)
    monkeypatch.setattr(tracker, "rubric_version", lambda: "5")
    tracker.sync_causes([cause_row()], tmp_path)
    tracker.sync_causes([cause_row(fatal=7)], tmp_path)
    assert kinds(tmp_path) == ["detected", "rubric_changed", "count_changed"]


def test_rubric_version_hashes_the_overridden_params_and_questions(monkeypatch):
    """The version is jev_version_effective plus a hash of overridden params and questions, nothing for defaults.

    The rubric has no `overrides` key; reading one made every rubric look like the default.
    """
    from vaniloop import rubric

    d = {"jev_version_effective": "4", "params": [{"id": "a", "value": 1, "overridden": False}], "questions": []}
    monkeypatch.setattr(rubric, "describe", lambda: d)
    assert tracker.rubric_version() == "4"
    d["params"][0] = {"id": "a", "value": 2, "overridden": True}
    v1 = tracker.rubric_version()
    d["params"][0]["value"] = 3
    assert v1.startswith("4+") and tracker.rubric_version() != v1


def test_a_failing_tracker_never_loses_a_stage_result(tmp_path, monkeypatch):
    """With the tracker raising, run_fix still saves its patches and returns them.

    The patches cost LLM calls. A full disk under tracker.jsonl must not throw them away.
    """

    def boom(*a, **k):
        raise OSError("disk full")

    causes = [cause("c1")]
    cp, pp, ap, op = write_run(tmp_path, PROMPT.encode(), causes, ev_audits(causes))
    monkeypatch.setattr(tracker, "_append", boom)
    r = run_fix(cp, pp, op, llm=stub("line 3: rule text 3", "line 3: better"), audits_path=ap)
    assert (
        [p["cause_id"] for p in json.loads(op.read_text())["patches"]]
        == ["c1"]
        == [p["cause_id"] for p in r["patches"]]
    )
    tracker.sync_causes([cause_row()], tmp_path)  # must not raise


def test_concurrent_writers_never_interleave_and_a_torn_tail_is_repaired(tmp_path):
    """Eight threads append 25 events each: every line parses, none is lost. A torn last line gets a newline first.

    Unlocked appends can interleave or join a torn line, and the lost events change a cause's state.
    """
    (tmp_path / "tracker.jsonl").write_text('{"kind": "detected", "cause_id": "torn"')  # no newline: a torn write

    def work(n):
        for i in range(25):
            tracker.record("count_changed", f"cause_{n}", {"i": i, "pad": "x" * 500}, runs=tmp_path)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = (tmp_path / "tracker.jsonl").read_text().splitlines()
    assert len(lines) == 201  # the torn line stays its own line
    assert sum(1 for line in lines[1:] if json.loads(line)["kind"] == "count_changed") == 200


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


def test_board_marks_an_event_that_skipped_its_prerequisite_as_ignored(tmp_path):
    """A shipped event on a cause that no gate said ship for stays in the log, is marked ignored and keeps the state.

    The history must stay complete for audit, and the board must not show a ship that never happened.
    """
    tracker.record("detected", "cause_x", runs=tmp_path)
    tracker.record("shipped", "cause_x", runs=tmp_path)
    c = tracker.board(tmp_path)["causes"][0]
    assert c["state"] == "open" and [e.get("ignored") for e in c["events"]] == [None, True]


def test_an_ignored_event_is_never_a_count_baseline(tmp_path):
    """An invalid regressed event (no ship yet) carries counts but is ignored: it must not hide a later real regression.

    detect at 1, a stray regressed at 5, ship, then 3 faults: the baseline is 1, so 3 is regressed. With the stray
    event as baseline, 3 < 5 gave a quiet count_changed and the regression was missed.
    """
    tracker.sync_causes([cause_row(fatal=1, nonfatal=0)], tmp_path)
    tracker.record("regressed", "cause_looping", {"counts": {"fatal": 5, "nonfatal": 0, "symptoms": 0}}, runs=tmp_path)
    ship(tmp_path)
    tracker.sync_causes([cause_row(fatal=3, nonfatal=0)], tmp_path)
    assert tracker.state("cause_looping", tmp_path) == "regressed"
