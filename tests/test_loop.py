"""Tests for `pipeline loop` and `loop.plan` (#145). Every stage function is mocked and the data is synthetic."""

from __future__ import annotations

import http.client
import json
import os
import signal
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from vaniloop import fix as fixmod
from vaniloop import ledger, llm, loop, pipeline, tracker, web
from vaniloop.fix import MAX_ATTEMPTS
from vaniloop.models import AuditRecord, CallRecord, Cause, Evidence, Finding, Turn
from vaniloop.split import split_half

EVENTS = {  # tracker state -> the events that lead to it
    "open": [], "watching": [("routed", {"action": "watch"})], "in_fix": [("patch_written", {})],
    "rejected": [("patch_written", {}), ("gated", {"decision": "reject"})],
    "exhausted": [("patch_written", {}), ("gated", {"decision": "reject", "last_attempt": True})],
    "ready_to_ship": [("patch_written", {}), ("gated", {"decision": "ship"})], "ticket_open": [("ticket_written", {})],
}  # fmt: skip


def jl(rows) -> str:
    return "".join(json.dumps(r) + "\n" for r in rows)


def leftovers(w):
    return list(w.dir.glob(f"{loop.FILTERED_PREFIX}*"))


def real_world(w, *kinds):
    """Real cause rows for the real run_fix: a patch cause (with fix-half evidence), a ticket cause, a watch cause."""
    mid = next(f"m{i}" for i in range(99) if split_half(f"m{i}") == "fix")
    turn = Turn(idx=0, speaker="bot", start_sec=0, end_sec=2, text="t")
    calls = [CallRecord(call_id=c, duration_sec=4.0, turns=[turn]).model_dump(mode="json") for c in (mid, *w.new)]
    finding = Finding(parameter="p_patch", status="Fatal", reason="r", basis="observed",
                      evidence=[Evidence(turn_idx=0, quote="q")])  # fmt: skip
    audit = AuditRecord(call_id=mid, verdict="Fatal", scope="bl_collection", findings=[finding])
    kw = {"patch": {"owner": "prompt", "status": "ranked", "fatal_calls": [mid]},
          "ticket": {"owner": "unresolved", "suspect": "tool", "status": "ranked"},
          "watch": {"owner": "prompt", "status": "watch"}}  # fmt: skip
    causes = [Cause(cause_id=f"cause_{k}", name=k, description="d", type="prompt", parameter=f"p_{k}", **kw[k])
              for k in kinds]  # fmt: skip
    (w.dir / "calls.jsonl").write_text(jl(calls))
    (w.dir / "audits.jsonl").write_text(jl([audit.model_dump(mode="json")]))
    (w.dir / "causes.json").write_text(json.dumps({"causes": [c.model_dump() for c in causes]}))
    (w.dir / "prompt.md").write_text("line 1\n")


class World:
    """A synthetic runs folder. Fix, refix, prove and the cheap stages are mocked; `calls` records their order."""

    def __init__(self, tmp_path, mp, name="runs"):
        self.dir, self.calls, self.causes, self.new = tmp_path / name, [], [], []
        self.decisions, self.raises, self.no_patch, self.envs, self.cancel = {}, {}, set(), [], set()
        self.dir.mkdir()
        mp.setenv("VANILOOP_JUDGE", "gemini")  # the loop sets jev for its replays; monkeypatch restores this
        for stage in ("audit", "judge", "regrade", "issues"):
            mp.setattr(loop, f"run_{stage}", self.stage(stage))
        for stage in ("fix", "refix", "prove"):
            mp.setattr(loop, f"run_{stage}", getattr(self, stage))
        mp.setattr(tracker, "ship", lambda *a, **k: pytest.fail("the loop must never ship"))

    def add(self, cid, state="open", route="patch", attempt=0):
        self.causes.append({"cause_id": cid, "route": route})
        for kind, detail in [("detected", {}), *EVENTS[state]]:
            tracker.record(kind, cid, detail, runs=self.dir)
        if attempt:
            self.patch(cid, attempt)

    def patch(self, cid, attempt):
        path = self.dir / "patches.json"
        rows = json.loads(path.read_text())["patches"] if path.exists() else []
        rows = [r for r in rows if r["cause_id"] != cid] + [{"cause_id": cid, "attempt": attempt}]
        path.write_text(json.dumps({"patches": rows}))

    def write(self):
        (self.dir / "causes.json").write_text(json.dumps({"causes": self.causes}))
        (self.dir / "calls.jsonl").write_text(jl({"call_id": c} for c in self.new))
        return self

    def stage(self, name):
        def fn(*a, **k):
            self.calls.append((name,))
            if name in self.raises:
                raise self.raises[name]
            return [] if name == "issues" else {"processed": 0, "regraded": 0}

        return fn

    def fix(self, **kw):
        rows = json.loads(kw["causes_path"].read_text())["causes"]
        if kw.get("dry_run"):  # the plan's route lookup: the real run_fix routes by status and owner
            return {"plan": [{"cause_id": c["cause_id"], "action": c["route"], "why": "w"} for c in rows]}
        self.calls.append(("fix", [c["cause_id"] for c in rows]))
        if "fix" in self.raises:
            raise self.raises["fix"]
        if "fix" in self.cancel:
            os.kill(os.getpid(), signal.SIGTERM)  # the console cancel
        for c in (c["cause_id"] for c in rows if c["route"] == "patch" and c["cause_id"] not in self.no_patch):
            self.patch(c, 1)
        return {"rejected": [{"cause_id": c, "reason": "old_text not found"} for c in self.no_patch]}

    def refix(self, cid, prompt, **kw):
        self.calls.append(("refix", cid))
        attempt = json.loads((self.dir / "patches.json").read_text())["patches"][0]["attempt"] + 1
        self.patch(cid, attempt)
        return {"patches": [{"cause_id": cid, "attempt": attempt}], "rejected": []}

    def prove(self, cid, prompt, **kw):
        """A fake gate: records the call, the Jev setting and the flags, and writes the real gated event."""
        a = kw["attempt"]
        self.calls.append(("prove", cid, a))
        self.envs.append(os.environ.get("VANILOOP_JUDGE"))
        assert kw["holdout"] is True and kw["screen"] is True
        if (cid, a) in self.raises:
            raise self.raises[(cid, a)]
        if (cid, a) in self.cancel:
            os.kill(os.getpid(), signal.SIGTERM)
        d = self.decisions.get((cid, a), "inconclusive")
        tracker.record("gated", cid, {"decision": d, "attempt": a, "last_attempt": a >= MAX_ATTEMPTS}, runs=self.dir)
        return SimpleNamespace(result=SimpleNamespace(decision=d, reason="synthetic"), cost_usd=0.5)

    def run(self, capsys, **kw):
        summary = loop.run_loop(self.dir, self.dir / "prompt.md", **kw)
        return summary, capsys.readouterr().out


def test_loop_runs_the_steps_in_order_and_never_ships(tmp_path, monkeypatch, capsys):
    """The loop must run the steps in order (prove, refix, prove again for an inconclusive cause), list a shipped
    gate result as ready_to_ship, write no prompt version and grade replays with Jev. Shipping would skip the owner."""
    w = World(tmp_path, monkeypatch)
    for cid in ("cause_a", "cause_b"):
        w.add(cid)
    w.add("cause_c", "ticket_open", route="ticket")
    w.decisions[("cause_b", 1)] = "ship"
    summary, text = w.write().run(capsys)
    assert w.calls == [
        ("audit",), ("judge",), ("regrade",), ("issues",), ("fix", ["cause_a", "cause_b"]),
        ("prove", "cause_a", 1), ("refix", "cause_a"), ("prove", "cause_a", 2), ("prove", "cause_b", 1),
    ]  # fmt: skip
    assert [x for x in text.splitlines() if x.startswith("LOOP_STEP")] == [
        f"LOOP_STEP {i}/7 {n}" for i, n in enumerate(loop.STEPS, 1)]  # fmt: skip
    assert text.splitlines()[-1] == "LOOP_DONE" and f"LOOP_SUMMARY {json.dumps(summary)}" in text
    assert summary["ready_to_ship"] == ["cause_b"] and summary["tickets"] == ["cause_c"]
    assert summary["decisions"] == {"cause_a": "inconclusive", "cause_b": "ship"}
    assert tracker.state("cause_a", w.dir) == "exhausted" and not (w.dir / "prompts" / "versions.json").exists()
    assert w.envs == ["jev"] * 3 and not leftovers(w)


def test_exhausted_ticket_and_watch_causes_are_skipped_with_a_reason(tmp_path, monkeypatch, capsys):
    """Exhausted, ready_to_ship, ticket and watch causes must get no fix, refix or prove, the plan must say why, and
    they must not use a --top slot. Otherwise the loop pays again for a cause that cannot be fixed."""
    w = World(tmp_path, monkeypatch)
    w.add("cause_a", "exhausted", attempt=2)
    w.add("cause_b", "ready_to_ship", attempt=1)
    w.add("cause_c", "ticket_open", route="ticket")
    w.add("cause_d", "watching", route="watch")
    w.add("cause_e")
    w.add("cause_f")
    w.write()
    rows = {c["cause_id"]: c for c in loop.plan(w.dir, top=1)["causes"]}
    assert [rows[c]["next_attempt"] for c in sorted(rows)] == [None, None, None, None, 1, None]
    for cid, word in (
        ("a", "exhausted"),
        ("b", "ready_to_ship"),
        ("c", "ticket"),
        ("d", "watch"),
        ("f", "beyond --top 1"),
    ):
        assert word in rows[f"cause_{cid}"]["why"]
    summary, _ = w.run(capsys, top=1)
    assert [c for c in w.calls if c[0] in ("fix", "prove", "refix")] == [
        ("fix", ["cause_e"]), ("prove", "cause_e", 1), ("refix", "cause_e"), ("prove", "cause_e", 2)]  # fmt: skip
    assert [c["cause_id"] for c in summary["causes"] if c["skipped"] is None] == ["cause_e"]


@pytest.mark.parametrize(("state", "first", "attempts"), [("in_fix", ("prove", "cause_a", 1), [1, 2]),
                                                          ("rejected", ("refix", "cause_a"), [2])])  # fmt: skip
def test_loop_resumes_from_the_tracker_state(tmp_path, monkeypatch, capsys, state, first, attempts):
    """A cause left in_fix must be proven again at its attempt with no new patch, and a rejected cause refixed first.
    If the loop started every cause over, it would reset attempts or leave a cause stuck after one failed prove."""
    w = World(tmp_path, monkeypatch)
    w.add("cause_a", state, attempt=1)
    w.write().run(capsys)
    assert [c for c in w.calls if c[0] in ("fix", "refix", "prove")][0] == first
    assert [c[2] for c in w.calls if c[0] == "prove"] == attempts and ("fix", ["cause_a"]) not in w.calls


def test_one_cause_failing_does_not_stop_the_others(tmp_path, monkeypatch, capsys):
    """When prove raises for cause A and fix writes no patch for cause C, the loop must still prove cause B and put both
    failures in the summary. If one bad cause ended the run, one flaky replay would hide every other result."""
    w = World(tmp_path, monkeypatch)
    for cid in ("cause_a", "cause_b", "cause_c"):
        w.add(cid)
    w.raises[("cause_a", 1)] = SystemExit("only 1 target call reproduces the fault")
    w.decisions[("cause_b", 1)] = "ship"
    w.no_patch = {"cause_c"}
    summary, text = w.write().run(capsys)
    failed = {c["cause_id"]: c["failed"] for c in summary["causes"]}
    assert failed["cause_a"] == {"step": "prove", "attempt": 1, "reason": "only 1 target call reproduces the fault"}
    assert (
        failed["cause_c"] == {"step": "fix", "attempt": 1, "reason": "old_text not found"} and failed["cause_b"] is None
    )
    assert [c for c in w.calls if c[0] == "prove"] == [("prove", "cause_a", 1), ("prove", "cause_b", 1)]
    assert summary["ready_to_ship"] == ["cause_b"] and text.splitlines()[-1] == "LOOP_DONE"


@pytest.mark.parametrize("step", ["audit", "judge", "regrade", "issues", "fix"])
def test_a_failed_step_stops_the_loop_with_loop_failed(tmp_path, monkeypatch, capsys, step):
    """A failure in steps 1 to 5 must print LOOP_FAILED <step> <reason>, exit non-zero and run no later step.
    Otherwise the loop would patch and prove on stale or missing grades."""
    w = World(tmp_path, monkeypatch)
    w.add("cause_a")
    w.raises[step] = SystemExit("another audit run is active")
    with pytest.raises(SystemExit) as e:
        w.write().run(capsys)
    out = capsys.readouterr().out
    assert e.value.code == 1 and f"LOOP_FAILED {step} another audit run is active" in out
    assert [c[0] for c in w.calls] == list(loop.STEPS[: loop.STEPS.index(step) + 1])
    assert "LOOP_SUMMARY" not in out and not leftovers(w)


def test_new_ticket_and_watch_causes_are_routed_by_the_loop(tmp_path, monkeypatch, capsys):
    """A new ticket-routed cause (no tracker events) must end the loop as ticket_open with its ticket file written and
    be listed in tickets, and a new watch cause as watching, all with no LLM call. Otherwise the one-button flow never
    writes a ticket and the report misses it."""
    w = World(tmp_path, monkeypatch)
    real_world(w, "ticket", "watch")
    monkeypatch.setattr(loop, "run_fix", lambda **kw: fixmod.run_fix(**{**kw, "llm": pytest.fail}))  # no patch pass
    summary, _ = w.run(capsys)
    assert (w.dir / "tickets" / "cause_ticket.md").exists()
    assert tracker.state("cause_ticket", w.dir) == "ticket_open" and tracker.state("cause_watch", w.dir) == "watching"
    assert summary["tickets"] == ["cause_ticket"] and summary["watch"] == ["cause_watch"]
    assert [c for c in w.calls if c[0] in ("prove", "refix")] == []


def test_two_loops_never_share_the_filtered_causes_file(tmp_path, monkeypatch):
    """A second fix step that starts while the first is inside fix must get its own causes file, and ending it must not
    delete or change the first one's file. A shared name made one loop fix another's causes, then lose the file."""
    w = World(tmp_path, monkeypatch)
    p, seen = loop.workspace_paths(w.dir), []
    row = lambda cid: [{"cause_id": cid, "todo": "fix", "route": "patch", "state": "open"}]  # noqa: E731
    w.add("cause_a")
    w.add("cause_b")
    w.write()

    def nested(**kw):
        seen.append(kw["causes_path"])
        if len(seen) == 1:  # another loop reaches its fix step while this one is inside fix
            loop._fix_step(p, w.dir / "prompt.md", row("cause_b"))
            assert [c["cause_id"] for c in json.loads(seen[0].read_text())["causes"]] == ["cause_a"]
        return {"rejected": []}

    monkeypatch.setattr(loop, "run_fix", nested)
    loop._fix_step(p, w.dir / "prompt.md", row("cause_a"))
    assert len(set(seen)) == 2 and not leftovers(w)


@pytest.mark.parametrize("where", ["fix", ("cause_a", 1)])
def test_a_cancel_stops_the_loop_and_cleans_up(tmp_path, monkeypatch, capsys, where):
    """SIGTERM (the console cancel) during fix or during the test of cause A must print LOOP_FAILED <step> cancelled,
    exit non-zero, never prove cause B, leave no temp file and restore the signal handler. If a per-cause handler
    swallowed it, the loop would go on to the next paid cause after the owner pressed cancel."""
    w, old = World(tmp_path, monkeypatch), signal.getsignal(signal.SIGTERM)
    w.add("cause_a")
    w.add("cause_b")
    w.cancel = {where}
    with pytest.raises(SystemExit) as e:
        w.write().run(capsys)
    step = "fix" if where == "fix" else "test"
    assert e.value.code == 1 and f"LOOP_FAILED {step} cancelled" in capsys.readouterr().out
    assert [c for c in w.calls if c[0] == "prove" and c[1] == "cause_b"] == [] and not leftovers(w)
    assert signal.getsignal(signal.SIGTERM) is old


def snapshot(d):
    return {str(p): (p.stat().st_mtime_ns, p.stat().st_size) for p in sorted(d.rglob("*")) if p.is_file()}


def test_plan_spends_nothing_and_writes_nothing(tmp_path, monkeypatch, capsys):
    """The plan, with the real fix dry run, must make no LLM request, change no file and create none (no tracker, lock
    or patch file): every LLM entry point raises and every file is compared. Otherwise the confirm itself costs."""
    w = World(tmp_path, monkeypatch)
    w.new = ["new_1", "new_2"]
    real_world(w, "patch", "ticket")
    monkeypatch.setattr(loop, "run_fix", fixmod.run_fix)  # the real fix dry run, not the mock

    def boom(*a, **k):
        pytest.fail("the plan must not reach this")

    for name in ("run_audit", "run_judge", "run_regrade", "run_issues", "run_refix", "run_prove"):
        monkeypatch.setattr(loop, name, boom)
    for name in ("_admit", "metered_create", "complete_json"):
        monkeypatch.setattr(llm, name, boom)
    before = snapshot(w.dir)
    plan = loop.plan(w.dir)
    pipeline.main(["--runs", str(w.dir), "loop", "--plan"])
    assert json.loads(capsys.readouterr().out) == plan and snapshot(w.dir) == before
    for name in ("tracker.jsonl", "tracker.jsonl.lock", "patches.json", ".audit.lock", "tickets"):
        assert not (w.dir / name).exists()
    assert plan["new_calls"] == 2 and [(c["route"], c["next_attempt"]) for c in plan["causes"]] == [
        ("patch", 1),
        ("ticket", None),
    ]
    assert [s["name"] for s in plan["steps"]] == list(loop.STEPS) and plan["steps"][0]["will_run"] is True


def history(w, gates=(0.5, 1.5, 1.0)):
    """Synthetic costs and gates. Reserve, Jev, replay and no-cost rows must not count."""
    tag = ledger.PROMPT_VERSION
    rows = [{"kind": "reserve", "id": "r1", "usd": 5.0}, {"model": "jev-latest", "cache_tag": tag, "cost_usd": 7.0}]
    rows += [{"model": "g", "cache_tag": tag, "cost_usd": c} for c in (0.01, 0.03, None)]
    rows += [{"model": "g", "cache_tag": "replay:bot:x", "cost_usd": 9.0}]
    rows += [{"model": "g", "cache_tag": "", "cost_usd": c} for c in (0.004, 0.006)]
    (w.dir / "costs.jsonl").write_text(jl(rows))
    (w.dir / "gates.jsonl").write_text(jl([{"cause_id": "z", "cost_usd": c} for c in gates] + [{"cause_id": "z"}]))


def test_estimate_comes_from_the_history_and_is_null_without_it(tmp_path, monkeypatch):
    """The estimate must be new calls x mean audit cost + remaining attempts x (median gate + mean fix cost), null
    with a reason without history, and 0 when nothing runs. Wrong rows or counts misstate what the owner approves."""
    w = World(tmp_path, monkeypatch)
    w.add("cause_a")
    w.new = ["new_1", "new_2"]
    w.write()
    p = loop.plan(w.dir)
    assert p["estimate_usd"] is None and p["estimate_basis"].startswith("no history: no audit cost rows, no gate cost")
    history(w)
    p = loop.plan(w.dir)
    assert (
        p["estimate_usd"] == pytest.approx(2 * 0.02 + 2 * (1.0 + 0.005)) and "2 audit x $0.0200" in p["estimate_basis"]
    )
    assert "Upper bound" not in p["estimate_basis"] and "replay turns only" in p["estimate_basis"]
    w2 = World(tmp_path, monkeypatch, "r2")
    w2.add("cause_a", "in_fix", attempt=1)  # 2 gate runs, 1 patch write
    w2.add("cause_b", "rejected", attempt=1)  # 1 gate run, 1 patch write
    history(w2)
    assert loop.plan(w2.write().dir)["estimate_usd"] == pytest.approx(3 * 1.0 + 2 * 0.005)
    assert loop.plan(World(tmp_path, monkeypatch, "r3").write().dir)["estimate_usd"] == 0.0


def get(srv, path):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read())
    finally:
        conn.close()


def test_plan_route_works_on_a_read_only_folder(tmp_path, monkeypatch):
    """GET /api/loop/plan must answer 200 with the plan JSON for a folder under <repo>/.cache/ and change no file there,
    and refuse a bad top. If the route needed a write, the frozen demo could not show a plan."""
    monkeypatch.setattr(web, "ROOT", tmp_path)
    (tmp_path / ".cache").mkdir()
    w = World(tmp_path / ".cache", monkeypatch, "demo")
    w.add("cause_a")
    w.new = ["new_1"]
    history(w)
    w.write()
    before = snapshot(w.dir)
    server = web.make_server(0, w.dir)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        status, out = get(server, "/api/loop/plan")
        assert status == 200 and out == loop.plan(w.dir) and out["estimate_usd"] > 0
        assert set(out) == {"steps", "causes", "new_calls", "estimate_usd", "estimate_basis"}
        assert set(out["causes"][0]) == {"cause_id", "route", "state", "next_attempt", "why"}
        assert get(server, "/api/loop/plan?top=0")[0] == 400 and get(server, "/api/loop/plan?top=x")[0] == 400
        assert snapshot(w.dir) == before
    finally:
        server.shutdown()
        server.server_close()
        t.join(timeout=5)


def test_pipeline_loop_wiring(tmp_path, monkeypatch):
    """`pipeline loop` must pass --prompt, --top, --n and --k to run_loop and need --prompt unless --plan. If the wiring
    dropped an option, a console click would run with the wrong sizes."""
    seen = []
    monkeypatch.setattr(loop, "run_loop", lambda *a, **k: seen.append((a, k)))
    pipeline.main(["--runs", str(tmp_path), "loop", "--prompt", "p.md", "--top", "2", "--n", "5", "--k", "2"])
    assert seen == [((tmp_path, Path("p.md")), {"top": 2, "n": 5, "k": 2})]
    with pytest.raises(SystemExit) as e:
        pipeline.main(["--runs", str(tmp_path), "loop"])
    assert e.value.code == 2
