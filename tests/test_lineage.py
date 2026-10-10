"""Prompt lineage (#130): ship, versioned prompts, the overlap guard and root-only targets. Synthetic data only."""

import json
import threading
import time

import pytest
from test_fix import PROMPT, cause, ev_audits, stub, write_run
from test_prove import CAUSE, run, synthetic

from vaniloop import prove, tracker
from vaniloop.fix import run_fix


def patch_row(cid, line, attempt=1, base=None):
    old = f"line {line}: rule text {line}"
    return {"cause_id": cid, "target_start_line": line, "target_end_line": line, "old_text": old,
            "new_text": f"{old} FIXED-{cid}", "rationale": "r", "risk": "low", "attempt": attempt,
            "base_version": base}  # fmt: skip


def signed(g, patches, base_text=PROMPT):
    """The gate row as prove writes it: with the fingerprints of the exact patch and of the replayed prompt text."""
    mine = [p for p in patches if p["cause_id"] == g["cause_id"]]
    if g.get("unsigned") or not mine:
        return {k: v for k, v in g.items() if k != "unsigned"}
    return {**g, "patch_fp": tracker.fingerprint(mine[-1]["old_text"], mine[-1]["new_text"]),
            "base_fp": tracker.fingerprint(base_text)}  # fmt: skip


def world(tmp_path, patches, gates):
    """A runs folder with patches.json, gates.jsonl and the base prompt file; returns (runs, prompt path)."""
    (tmp_path / "patches.json").write_text(json.dumps({"patches": patches}))
    (tmp_path / "gates.jsonl").write_text("".join(json.dumps(signed(g, patches)) + "\n" for g in gates))
    pf = tmp_path / "prompt.md"
    pf.write_text(PROMPT)
    return tmp_path, pf


def gate(cid, decision="ship", attempt=1):
    return {"cause_id": cid, "decision": decision, "attempt": attempt}


@pytest.mark.parametrize(
    ("row", "attempt"),
    [(gate("cause_a", "inconclusive"), 1), (gate("cause_a", "reject"), 1), (gate("cause_a", "ship", 1), 2)],
    ids=["inconclusive", "reject", "older attempt"],
)
def test_ship_needs_a_ship_gate_row_of_the_current_attempt(tmp_path, row, attempt):
    """ship refuses an inconclusive gate, a reject and a ship row of an older attempt, and writes nothing.

    A ship on any of these would put an unproven prompt edit into the live prompt version.
    """
    runs, pf = world(tmp_path, [patch_row("cause_a", 10, attempt=attempt)], [row])
    with pytest.raises(SystemExit):
        tracker.ship("cause_a", runs, pf)
    assert not list((runs / "prompts").glob("v*"))  # the lock file may exist, no version does


def test_ship_writes_a_version_and_the_next_patch_is_gated_on_top_of_it(tmp_path):
    """After A ships as v1, prove for B loads v1 (A's text present), not the clean base.

    If prove still loaded the base, two shipped patches would never be tested together.
    """
    runs, pf = world(tmp_path, [patch_row("cause_a", 10), patch_row("cause_b", 20, base="v1")], [gate("cause_a")])
    assert tracker.ship("cause_a", runs, pf) == "v1"
    assert (runs / "prompts" / "v1.md").read_text().count("FIXED-cause_a") == 1
    old, new = prove.build_prompts("cause_b", pf, runs / "patches.json")
    assert "FIXED-cause_a" in old and "FIXED-cause_b" in new
    assert tracker.state("cause_a", runs) == "shipped"
    assert [e["kind"] for e in tracker.events(runs, "cause_a")] == ["gated", "approved", "shipped"]
    assert tracker.board(runs)["current_prompt_version"] == "v1"


def test_prove_refuses_a_patch_on_a_stale_base(tmp_path):
    """A patch written on the clean base must not be proven once v1 exists: it says refix.

    Its line numbers and anchor describe a prompt that is no longer the live one.
    """
    runs, pf = world(tmp_path, [patch_row("cause_a", 10), patch_row("cause_b", 20)], [gate("cause_a")])
    tracker.ship("cause_a", runs, pf)
    with pytest.raises(SystemExit, match="refix"):
        prove.build_prompts("cause_b", pf, runs / "patches.json")


def test_fix_refuses_a_patch_that_overlaps_a_shipped_range_and_an_in_flight_patch(tmp_path):
    """run_fix rejects a patch whose lines were changed by a shipped version, or that another in-flight patch targets.

    Two edits on the same lines cannot both be gated meaningfully; the second would silently undo the first.
    """
    causes = [cause("c1"), cause("c2")]
    cp, pp, ap, op = write_run(tmp_path, PROMPT.encode(), causes, ev_audits(causes))
    runs = op.parent
    runs.mkdir(parents=True, exist_ok=True)
    op.write_text(json.dumps({"patches": [patch_row("cause_a", 10)]}))
    (runs / "gates.jsonl").write_text(json.dumps(signed(gate("cause_a"), [patch_row("cause_a", 10)])) + "\n")
    tracker.ship("cause_a", runs, pp)  # v1: line 10 changed
    inflight = patch_row("cause_z", 5, base="v1")
    op.write_text(json.dumps({"patches": [patch_row("cause_a", 10), inflight]}))

    def llm(system, user, **kw):
        line = 10 if "c1" in user else 5  # c1 hits the shipped line, c2 the in-flight line of cause_z
        text = tracker.current_prompt(pp, runs)[0].split("\n")[line - 1]
        return {"old_text": text, "new_text": text + " x", "rationale": "r", "risk": "low"}

    r = run_fix(cp, pp, op, llm=llm, audits_path=ap)
    rows = r["rejected"]
    assert {p["cause_id"] for p in r["patches"]} == {"cause_a", "cause_z"}  # nothing new was written
    assert "shipped" in next(x["reason"] for x in rows if x["cause_id"] == "c1")
    assert "in-flight" in next(x["reason"] for x in rows if x["cause_id"] == "c2")


def test_fix_records_base_version_and_events(tmp_path):
    """A written patch carries base_version and fix records routed and patch_written events.

    The base version is what prove compares with the current version; the events feed the tracker board.
    """
    causes = [cause("c1")]
    cp, pp, ap, op = write_run(tmp_path, PROMPT.encode(), causes, ev_audits(causes))
    r = run_fix(cp, pp, op, llm=stub("line 3: rule text 3", "line 3: better"), audits_path=ap)
    assert r["patches"][0]["base_version"] == "base"
    assert [e["kind"] for e in tracker.events(op.parent, "c1")] == ["routed", "patch_written"]


# ---- root-only targets ----


def test_symptom_calls_are_not_targets():
    """A call whose root is another cause (a ticket one) and that only fails looping is dropped as a symptom.

    Otherwise the gate would test a prompt fix on a fault the prompt did not cause.
    """
    calls, ledgers, audits = synthetic()
    dropped = {}
    roots = {f"t{i:02d}" for i in range(12)} | {"t12"}
    ticket_calls = {"t00"}
    items, _ = prove.select_targets(calls, ledgers, audits, "looping", 20, dropped=dropped, roots=roots - {"t05"},
                                    ticket_calls=ticket_calls)  # fmt: skip
    ids = {c.call_id for c, _, _ in items}
    assert "t05" not in ids and dropped["symptom of another root"] == ["t05"]
    assert "t00" not in ids and dropped["root is a ticket cause"] == ["t00"]


def test_drop_counts_reach_the_gate_row(tmp_path):
    """The gates.jsonl row carries the drop counts per reason, so the demo can show why calls were not targets.

    Counts that only print vanish from the record the dashboard reads.
    """
    roots = [f"t{i:02d}" for i in range(1, 12)] + ["t12"]
    row = {"cause_id": CAUSE, "name": "n", "description": "d", "type": "prompt", "parameter": "looping",
           "status": "ranked", "fatal_calls": roots, "nonfatal_calls": []}  # fmt: skip
    pf_causes = {"causes": [row]}
    (tmp_path / "causes.json").write_text(json.dumps(pf_causes))  # t00 is a symptom: not in the root calls
    out, paths, _ = run(tmp_path, n=12, guards=2)
    row = json.loads(paths["gates_path"].read_text().splitlines()[-1])
    assert row["dropped"] == {"symptom of another root": 1}
    assert len(row["patch_fp"]) == len(row["base_fp"]) == 64  # ship binds the row to the exact patch and prompt
    gated = [e for e in tracker.events(paths["gates_path"].parent, CAUSE) if e["kind"] == "gated"]
    assert gated[0]["detail"]["decision"] == out.result.decision


def test_ship_command_uses_the_runs_folder(tmp_path):
    """`pipeline --runs DIR ship` reads DIR's patches and gates and writes DIR/prompts, not the repo's runs/.

    A console workspace must keep its prompt versions beside its own data.
    """
    from vaniloop import pipeline

    runs, pf = world(tmp_path, [patch_row("cause_a", 10)], [gate("cause_a")])
    pipeline.main(["--runs", str(runs), "ship", "--cause", "cause_a", "--prompt", str(pf)])
    assert (runs / "prompts" / "v1.md").exists()


def test_concurrent_ships_never_share_a_version_name(tmp_path, monkeypatch):
    """Two ships at once: one makes v1 and the other is refused (its base is now stale); v1 is written once.

    Without the ship lock both read "no versions", both wrote v1, and one patch vanished from the live prompt.
    """
    runs, pf = world(tmp_path, [patch_row("cause_a", 10), patch_row("cause_b", 20)], [gate("cause_a"), gate("cause_b")])
    real = tracker._atomic
    monkeypatch.setattr(tracker, "_atomic", lambda path, text: (time.sleep(0.2), real(path, text))[1])  # widen the race
    results = []

    def go(cid):
        try:
            results.append(tracker.ship(cid, runs, pf))
        except SystemExit as e:
            results.append(str(e))

    threads = [threading.Thread(target=go, args=(c,)) for c in ("cause_a", "cause_b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(r for r in results if r.startswith("v")) == ["v1"] and len(results) == 2
    assert [v["version"] for v in json.loads((runs / "prompts" / "versions.json").read_text())["versions"]] == ["v1"]


@pytest.mark.parametrize("how", ["unsigned", "patch edited", "prompt edited"])
def test_ship_binds_the_gate_row_to_the_exact_patch_and_prompt(tmp_path, how):
    """A gate row without fingerprints, or for another patch or another base text, cannot ship.

    The gate proved one edit on one prompt. A patch rewritten or a prompt file changed after the gate is unproven.
    """
    patch = patch_row("cause_a", 10)
    runs, pf = world(tmp_path, [patch], [{**gate("cause_a"), "unsigned": how == "unsigned"}])
    if how == "patch edited":
        (runs / "patches.json").write_text(json.dumps({"patches": [{**patch, "new_text": "line 10: other"}]}))
    if how == "prompt edited":
        pf.write_text(PROMPT.replace("rule text 1:", "rule text one:").replace("line 1:", "line 1 edited:"))
    with pytest.raises(SystemExit, match="does not match"):
        tracker.ship("cause_a", runs, pf)
    assert not list((runs / "prompts").glob("v*"))


def test_a_trailing_newline_does_not_inflate_the_range(tmp_path):
    """old "line\n" -> new "line\n" is one changed line, no shift: the range is (10, 10) and delta 0.

    Counting the newline as a line moved every later range by one and made overlap checks wrong.
    """
    patch = {**patch_row("cause_a", 10), "old_text": "line 10: rule text 10\n", "new_text": "line 10: better\n"}
    runs, pf = world(tmp_path, [patch], [gate("cause_a")])
    tracker.ship("cause_a", runs, pf)
    assert tracker.shipped_ranges(runs) == [(10, 10)]
    assert json.loads((runs / "prompts" / "versions.json").read_text())["versions"][0]["patch"]["delta"] == 0


@pytest.mark.parametrize(("old_nl", "new_nl", "shift"), [("\n", "", -1), ("", "\n", 1)], ids=["removed", "added"])
def test_adding_or_removing_a_trailing_newline_shifts_the_ranges_below(tmp_path, old_nl, new_nl, shift):
    """Removing a trailing newline merges the next line (shift -1), adding one splits it (shift +1).

    A shipped range below line 10 must follow the text: A ships at line 20, then B changes the newline at line 10,
    and A's range must be where its text really is in v2.
    """
    a = patch_row("cause_a", 20)
    b = {
        **patch_row("cause_b", 10, base="v1"),
        "old_text": f"line 10: rule text 10{old_nl}",
        "new_text": f"line 10: better{new_nl}",
    }
    runs, pf = world(tmp_path, [a, b], [gate("cause_a"), gate("cause_b")])
    tracker.ship("cause_a", runs, pf)
    # B's gate row was signed against the base text; re-sign it against v1 as prove would
    v1 = (runs / "prompts" / "v1.md").read_text()
    rows = [json.loads(x) for x in (runs / "gates.jsonl").read_text().splitlines()]
    rows[1] = signed(rows[1], [b], base_text=v1)
    (runs / "gates.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    tracker.ship("cause_b", runs, pf)
    v2 = (runs / "prompts" / "v2.md").read_text()
    line_a = next(i for i, ln in enumerate(v2.split("\n"), 1) if "FIXED-cause_a" in ln)
    assert line_a == 20 + shift
    assert (line_a, line_a) in tracker.shipped_ranges(runs)


def test_prove_fingerprints_the_patch_it_replayed_not_the_file_after_the_run(tmp_path):
    """patches.json edited during the replays: the gate row still carries the fingerprint of the patch that was tested.

    Fingerprinting after the run let ship accept an edit nobody replayed.
    """
    from test_prove import Stubs, write_inputs

    pf, paths = write_inputs(tmp_path)
    original = json.loads(paths["patches_path"].read_text())
    stubs, done = Stubs(), []

    def replay_fn(*a):
        if not done:  # the owner edits the patch while the gate runs
            done.append(1)
            edited = json.loads(json.dumps(original))
            edited["patches"][0]["new_text"] += " EDITED"
            paths["patches_path"].write_text(json.dumps(edited))
        return stubs.replay_fn(*a)

    prove.run_prove(CAUSE, pf, n=3, k=2, guards=2, replay_fn=replay_fn, audit_fn=stubs.audit_fn, **paths)
    row = json.loads(paths["gates_path"].read_text().splitlines()[-1])
    tested = original["patches"][0]
    assert row["patch_fp"] == tracker.fingerprint(tested["old_text"], tested["new_text"])
    edited_row = json.loads(paths["patches_path"].read_text())["patches"][0]
    assert row["patch_fp"] != tracker.fingerprint(edited_row["old_text"], edited_row["new_text"])


def test_a_malformed_versions_manifest_fails_closed(tmp_path):
    """A present but invalid versions.json stops fix, refix and prove with SystemExit; a missing one means "base".

    Falling back to the clean base would test and ship patches against a prompt that is not the live one.
    """
    runs, pf = world(tmp_path, [patch_row("cause_a", 10)], [])
    assert tracker.current_prompt(pf, runs)[1] == "base"
    (runs / "prompts").mkdir()
    for bad in ("{not json", '{"versions": "x"}', '{"versions": [{"version": "v2", "causes": []}]}'):
        (runs / "prompts" / "versions.json").write_text(bad)
        with pytest.raises(SystemExit, match="malformed"):
            tracker.current_prompt(pf, runs)
    (runs / "prompts" / "versions.json").write_text('{"versions": [{"version": "v1", "causes": []}]}')
    with pytest.raises(SystemExit, match="missing"):  # the manifest names v1 but v1.md is gone
        tracker.current_prompt(pf, runs)
    two = '{"versions": [{"version": "v1", "causes": []}, {"version": "v2", "causes": []}]}'
    (runs / "prompts" / "versions.json").write_text(two)
    (runs / "prompts" / "v2.md").write_text("v2 text")  # only the last version exists: v1.md is missing
    with pytest.raises(SystemExit, match="v1.md is missing"):
        tracker.current_prompt(pf, runs)


def test_an_explicit_empty_root_list_means_no_targets_not_no_filter(tmp_path):
    """A cause whose causes.json row has fatal_calls [] and nonfatal_calls [] has no roots, so no calls can target it.

    Reading [] as "no list" turned root filtering off and let symptom calls become targets.
    """
    row = {"cause_id": CAUSE, "name": "n", "description": "d", "type": "prompt", "parameter": "looping",
           "status": "ranked", "fatal_calls": [], "nonfatal_calls": []}  # fmt: skip
    (tmp_path / "causes.json").write_text(json.dumps({"causes": [row]}))
    assert prove.root_sets(CAUSE, tmp_path / "causes.json")[0] == set()
    old = {k: v for k, v in row.items() if not k.endswith("_calls")}
    (tmp_path / "causes.json").write_text(json.dumps({"causes": [old]}))
    assert prove.root_sets(CAUSE, tmp_path / "causes.json")[0] is None  # an old file: no call lists at all
    calls, ledgers, audits = synthetic()
    items, _ = prove.select_targets(calls, ledgers, audits, "looping", 5, roots=set())
    assert items == []


def test_ship_rechecks_the_state_under_the_lock(tmp_path):
    """A ship validated as ready_to_ship must be refused when a new patch lands before it runs (state in_fix).

    The console queues a ship job after its own state check. Without the re-check under the ship lock, an edit
    written between the check and the job would be shipped on an old gate row's word.
    """
    runs, pf = world(tmp_path, [patch_row("cause_a", 10)], [gate("cause_a")])
    tracker.record("gated", "cause_a", {"decision": "ship", "attempt": 1}, runs=runs)
    assert tracker.state("cause_a", runs) == "ready_to_ship"
    tracker.record("patch_written", "cause_a", {"attempt": 1, "changed_lines": 1}, runs=runs)
    assert tracker.state("cause_a", runs) == "in_fix"
    with pytest.raises(SystemExit, match="not ready_to_ship"):
        tracker.ship("cause_a", runs, pf)
    assert not list((runs / "prompts").glob("v*"))
