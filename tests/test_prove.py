"""prove: run the ship gate on saved calls. Synthetic records, stub replay and audit functions, no network."""

import json
import random
import threading

import pytest

from vaniloop import llm, pipeline, prompt, prove, replay
from vaniloop.models import AuditRecord, BotTurnLabel, CallRecord, Finding, Ledger, Turn
from vaniloop.replay import ReplayResult

CAUSE, PARAM, BOM = "cause_looping", "looping", chr(0xFEFF)
TURNS = "bot user bot user bot user bot user"


def make_call(cid, speakers=TURNS, idx_start=0):
    turns = [Turn(idx=idx_start + i, speaker=s, start_sec=6.0 * i, end_sec=6.0 * i + 5, text=f"t{i}")
             for i, s in enumerate(speakers.split())]  # fmt: skip
    return CallRecord(call_id=cid, duration_sec=60, turns=turns)


def make_audit(cid, status="Pass", onset=None, first_fault=None, parameter=PARAM, verdict=None):
    f = Finding(parameter=parameter, status=status, reason="r", basis="observed", onset_turn=onset)
    return AuditRecord(
        call_id=cid,
        verdict=verdict or status,
        scope="bl_collection",
        findings=[f],
        first_fault_turn=first_fault,
    )


def synthetic():
    """(calls, ledgers, audits): 13 looping targets, 15 passing guards and the traps between them."""
    calls, ledgers, audits = {}, {}, {}

    def add(cid, status="Pass", variant="main", speakers=TURNS, **kw):
        calls[cid] = make_call(cid, speakers)
        ledgers[cid] = Ledger(call_id=cid, scope="bl_collection", variant=variant)
        audits[cid] = make_audit(cid, status, **kw)

    for i in range(12):  # half carry the fork on the finding, half only on the audit
        add(f"t{i:02d}", "NonFatal", onset=3 if i % 2 == 0 else None, first_fault=None if i % 2 == 0 else 5)
    for i in range(15):
        add(f"p{i:02d}")
    add("t12", "NonFatal", onset=3, verdict="Pass")  # Pass verdict but the parameter fails: a target, never a guard
    add("enrich_fail", "Fatal", variant="enrich", onset=3)
    add("no_fork", "Fatal")
    add("other_param", "Fatal", onset=3, parameter="qty_probing")
    add("enrich_pass", variant="enrich")
    add("bot_only", speakers="bot bot bot")
    return calls, ledgers, audits


def write_inputs(tmp_path, patch_cause=CAUSE, old_text="rule text 10", new_text="rule text 10 PATCHED"):
    """Write calls, ledgers, audits, patches and a prompt file with BOMs and an escape; return (prompt, paths)."""
    rows = [f"line {i}: rule text {i}" for i in range(1, 31)]
    rows[9] = "line 10 \\- rule text 10"  # clean text reads "line 10 - rule text 10"
    pf = tmp_path / "prompt.md"
    pf.write_bytes("\n".join(BOM + r for r in rows).encode("utf-8"))
    patch = dict(cause_id=patch_cause, target_start_line=10, target_end_line=10, old_text=old_text)
    patch.update(new_text=new_text, rationale="r", risk="low")
    paths = {f"{n}_path": tmp_path / f"{n}.jsonl" for n in ("calls", "ledgers", "audits")}
    paths.update(patches_path=tmp_path / "patches.json")
    paths.update(gates_path=tmp_path / "out" / "gates.jsonl", replays_path=tmp_path / "out" / "replays.jsonl")
    for key, recs in zip(("calls_path", "ledgers_path", "audits_path"), synthetic(), strict=True):
        paths[key].write_text("".join(r.model_dump_json() + "\n" for r in recs.values()))
    paths["patches_path"].write_text(json.dumps({"patches": [patch], "rejected": []}))
    return pf, paths


class Stubs:
    """Stub replay and audit: the old prompt fails `looping` on target calls, the patched prompt passes.

    `new_fails` makes the patched prompt fail too (a futility reject). `fail_replay_at` and `fail_audit_at` raise on
    that one call only, counted over all calls. `log` keeps (thread name, call id, prompt label, seed) per replay call.
    """

    def __init__(self, fail_after=None, fail_audit_at=None, fail_replay_at=None, new_fails=False):
        self.replays = self.audits = 0
        self.prompts = set()
        self.fail_after, self.fail_audit_at, self.fail_replay_at = fail_after, fail_audit_at, fail_replay_at
        self.new_fails = new_fails
        self.log = []
        self.lock = threading.Lock()  # the warm-up calls the stubs from a thread pool

    def replay_fn(self, call, ledger, fork, bot_prompt, seed):
        tag = "new" if "PATCHED" in bot_prompt else "old"
        with self.lock:
            self.replays += 1
            n = self.replays
            self.log.append((threading.current_thread().name, call.call_id, tag, seed))
        if (self.fail_after is not None and n > self.fail_after) or n == self.fail_replay_at:
            raise RuntimeError("stop mid-run")
        self.prompts.add(bot_prompt)
        turns = [*call.turns[:fork], Turn(idx=fork, speaker="bot", start_sec=1, end_sec=2, text=f"{tag} reply {seed}")]
        res = CallRecord(call_id=f"{call.call_id}__{tag}_{seed}", duration_sec=60, turns=turns)
        return ReplayResult(call=res, fork_turn=fork, ended_by="end_call")

    def audit_fn(self, call):
        with self.lock:
            self.audits += 1
            n = self.audits
        if n == self.fail_audit_at:
            raise RuntimeError("ledger extraction failed")
        bad = call.call_id.startswith("t") and ("__old_" in call.call_id or self.new_fails)
        # the fault starts at the generated turn (the last one), at the fork: the gate counts only post-fork findings
        return make_audit(call.call_id, "NonFatal" if bad else "Pass", onset=call.turns[-1].idx)


def raising(*_a, **_k):
    raise AssertionError("an LLM-backed function was called")


def run(tmp_path, stubs=None, **kw):
    pf, paths = write_inputs(tmp_path)
    stubs = stubs or Stubs()
    args = dict(n=3, k=2, guards=2, replay_fn=stubs.replay_fn, audit_fn=stubs.audit_fn, **paths)
    return prove.run_prove(CAUSE, pf, **{**args, **kw}), paths, stubs


def test_picks_are_stable_and_disjoint():
    """Targets and guards must be the same on every run, in any input order, and never overlap. Different picks per run
    would make two proofs of one patch use different calls."""
    calls, ledgers, audits = synthetic()
    shuffled = list(audits.items())
    random.Random(7).shuffle(shuffled)
    t1, cand = prove.select_targets(calls, ledgers, audits, PARAM, 5)
    t2, _ = prove.select_targets(calls, ledgers, dict(shuffled), PARAM, 5)
    assert [(c.call_id, f) for c, _, f in t1] == [(c.call_id, f) for c, _, f in t2] and len(t1) == 5
    assert {c.call_id for c, _, _ in t1} <= cand == {f"t{i:02d}" for i in range(13)}  # no enrich, other param, no fork
    g1 = prove.select_guards(calls, ledgers, audits, cand, 20)
    g2 = prove.select_guards(calls, ledgers, dict(shuffled), cand, 20)
    assert [c.call_id for c, _, _ in g1] == [c.call_id for c, _, _ in g2]
    assert {c.call_id for c, _, _ in g1} == {f"p{i:02d}" for i in range(15)}  # not t12, bot_only or enrich
    assert prove.pick(["b", "a", "c"], 2) == prove.pick(["c", "a", "b"], 2) and prove.pick(["a"], 5) == ["a"]


def test_fork_turns_follow_onset_then_first_fault_then_skip():
    """A target forks at the finding's onset turn, else the audit's first fault turn. A guard forks at the bot's reply.
    A wrong fork would replay from before or after the fault and test the wrong behaviour."""
    calls, ledgers, audits = synthetic()
    by_id = {c.call_id: f for c, _, f in prove.select_targets(calls, ledgers, audits, PARAM, 13)[0]}
    assert by_id["t00"] == 3 and by_id["t01"] == 5 and "no_fork" not in by_id
    shifted = make_call("s", idx_start=10)  # Turn.idx 10..17: onset 12 is list position 2
    assert prove.target_fork(shifted, make_audit("s", "Fatal", onset=12), PARAM) == 2
    assert prove.target_fork(shifted, make_audit("s", "Fatal", onset=99), PARAM) is None
    assert prove.guard_fork(make_call("g")) == 2 and prove.guard_fork(make_call("g", "user bot user")) == 1
    assert prove.guard_fork(make_call("g", "bot user user")) is None
    assert prove.guard_fork(make_call("g", "bot bot")) is None


def test_missing_or_misfitting_patch_stops_before_any_llm_call(tmp_path):
    """No patch for the cause, or a patch that is absent, repeated or a no-op, must stop with SystemExit. A patch that
    does not apply would spend the paid replays on the old prompt twice."""
    cases = {
        "nopatch": dict(patch_cause="cause_other"),
        "absent": dict(old_text="not in the prompt"),
        "repeated": dict(old_text="rule text"),
        "noop": dict(new_text="rule text 10"),  # equal prompts would label every NEW replay "old"
    }
    for sub, kw in cases.items():
        (tmp_path / sub).mkdir()
        pf, paths = write_inputs(tmp_path / sub, **kw)
        with pytest.raises(SystemExit):
            prove.run_prove(CAUSE, pf, replay_fn=raising, audit_fn=raising, **paths)
        assert not paths["gates_path"].exists() and not paths["replays_path"].exists()


def test_prove_attempt_must_match_the_current_patch(tmp_path):
    """`prove --attempt N` must stop with SystemExit, before any replay, when the current patch has another attempt.
    Without it a proof of the old patch could be recorded as attempt 2, and the gate row would name the wrong patch.
    A patch without an attempt field is attempt 1, and the matching attempt runs and is written to the rows."""
    pf, paths = write_inputs(tmp_path)
    for attempt in (2, 3):
        with pytest.raises(SystemExit, match="attempt 1"):
            prove.run_prove(CAUSE, pf, replay_fn=raising, audit_fn=raising, attempt=attempt, **paths)
    assert not paths["gates_path"].exists() and not paths["replays_path"].exists()
    doc = json.loads(paths["patches_path"].read_text())
    doc["patches"][0]["attempt"] = 2
    paths["patches_path"].write_text(json.dumps(doc))
    with pytest.raises(SystemExit, match="attempt 2"):
        prove.run_prove(CAUSE, pf, replay_fn=raising, audit_fn=raising, **paths)  # the default attempt is 1
    stubs = Stubs()
    out = prove.run_prove(CAUSE, pf, n=3, k=2, guards=2, replay_fn=stubs.replay_fn, audit_fn=stubs.audit_fn,
                          attempt=2, **paths)  # fmt: skip
    assert out.result.attempt == 2
    assert {json.loads(ln)["attempt"] for ln in paths["replays_path"].read_text().splitlines()} == {2}


def test_pipeline_refix_subcommand_passes_its_options_and_prints(monkeypatch, capsys):
    """`pipeline refix` must pass the cause and prompt path to run_refix and print the new attempt and the next
    command. A dropped option would refix the wrong cause or read the wrong prompt."""
    seen = {}

    def fake(cause, prompt_path, **kw):
        seen.update(cause=cause, prompt=str(prompt_path), **kw)
        return {"patches": [dict(cause_id=cause, attempt=2)], "rejected": []}

    monkeypatch.setattr(pipeline, "run_refix", fake)
    pipeline.main(["refix", "--cause", CAUSE, "--prompt", "p.md"])
    assert seen == dict(cause=CAUSE, prompt="p.md")
    assert "prove --attempt 2" in capsys.readouterr().out


def test_run_records_gate_result_and_every_replay(tmp_path):
    """One gates row and one replays row per replay must be written, with role, prompt, seed and a matching audit.
    Without them the demo cannot show which replay of which call under which prompt led to a decision."""
    out, paths, stubs = run(tmp_path)
    gates = [json.loads(ln) for ln in paths["gates_path"].read_text().splitlines()]
    assert len(gates) == 1 and gates[0]["cause_id"] == CAUSE == out.result.cause_id and gates[0]["decision"] == "ship"
    rows = [json.loads(ln) for ln in paths["replays_path"].read_text().splitlines()]
    assert len(rows) == stubs.replays == stubs.audits == out.replays == 14  # 3 targets x 2 seeds x 2 prompts + 2 guards
    assert list(rows[0]) == ["cause_id", "attempt", "role", "prompt", "seed", "fork_turn", "ended_by", "call", "audit"]
    counts = {}
    for r in rows:
        counts[(r["role"], r["prompt"])] = counts.get((r["role"], r["prompt"]), 0) + 1
        assert r["cause_id"] == CAUSE and r["attempt"] == 1 and r["audit"]["call_id"] == r["call"]["call_id"]
        assert r["call"]["call_id"].split("__")[1].startswith(r["prompt"])  # the stub names a replay after its prompt
    assert counts == {("target", "old"): 6, ("target", "new"): 6, ("guard", "new"): 2}
    assert {r["seed"] for r in rows if r["role"] == "target"} == {0, 1}
    old = prompt.load(tmp_path / "prompt.md")  # the clean main prompt is the old arm
    assert BOM not in old and "line 10 - rule text 10" in old
    assert stubs.prompts == {old, old.replace("rule text 10", "rule text 10 PATCHED")}


def test_dead_air_stops_before_any_llm_call(tmp_path):
    """A dead_air cause must stop with SystemExit before any replay, because a text replay has no audio timing. The
    gate would otherwise spend replays on a parameter it can never judge."""
    pf, paths = write_inputs(tmp_path, patch_cause="cause_dead_air")  # a patch exists: only the audio rule stops it
    with pytest.raises(SystemExit) as exc:
        prove.run_prove("cause_dead_air", pf, replay_fn=raising, audit_fn=raising, **paths)
    assert "audio" in str(exc.value) and not paths["gates_path"].exists()


def test_dry_run_makes_no_llm_call_and_writes_no_file(tmp_path):
    """A dry run must plan the run without calling any LLM function and without creating or changing a file. A dry run
    that spent money or wrote to runs/ would not be safe to run before the paid proof."""
    pf, paths = write_inputs(tmp_path)

    def snap():
        return sorted((str(p), p.read_bytes()) for p in tmp_path.rglob("*") if p.is_file())

    before = snap()
    out = prove.run_prove(CAUSE, pf, n=3, k=2, guards=2, dry_run=True, replay_fn=raising, audit_fn=raising, **paths)
    assert snap() == before and out.result is None
    assert len(out.plan.targets) == 3 and len(out.plan.guards) == 2 and out.plan.max_replays == 18
    text = "\n".join(prove.describe(out))
    assert f"old prompt: {len(prompt.load(pf))} characters" in text and "at most 18 replays" in text
    assert "fork_turn=" in text


def test_too_few_targets_or_seeds_stop_before_any_llm_call(tmp_path):
    """Fewer than 3 target calls, or fewer than 2 seeds, must stop before any replay. The gate would spend every replay
    and then report 'only N matched calls' or raise on k."""
    pf, paths = write_inputs(tmp_path)
    for kw in (dict(n=2), dict(k=1)):
        with pytest.raises(SystemExit):
            prove.run_prove(CAUSE, pf, replay_fn=raising, audit_fn=raising, **{"n": 3, "k": 2, **kw}, **paths)
    assert not paths["gates_path"].exists()


def test_cost_counts_only_replay_rows_that_belong_to_this_run(tmp_path):
    """The printed cost must sum only rows written during the run for this plan's prompts and calls. Rows of a parallel
    run, earlier rows and unattributable ledger rows would inflate a cost that this proof spent."""
    llm.COSTS_PATH.write_text('{"cost_usd": 5.0, "cache_tag": "replay:bot:aaaaaaaa:0:t00:3"}\n')
    stubs = Stubs()

    def replay_with_cost(call, ledger, fork, bot_prompt, seed):
        sent = prove._sent_prompt(bot_prompt, call, ledger)  # the real replay tags its rows with the sent prompt
        tag, tail = replay.prompt_tag(sent), f"{seed}:{call.call_id}:{fork}"
        rows = [
            {"cache_tag": f"replay:bot:{tag}:{tail}", "cost_usd": 0.25},  # this run
            {"cache_tag": f"replay:user:{tag}:{tail}", "cost_usd": 0.25},  # this run
            {"cache_tag": "4", "cost_usd": 1.0},  # ledger extraction: no call id, left out
            {"cache_tag": f"replay:bot:deadbeef:{tail}", "cost_usd": 2.0},  # another prompt
            {"cache_tag": f"replay:bot:{tag}:{seed}:other_call:{fork}", "cost_usd": 4.0},  # another call
            {"kind": "reserve", "id": "x", "usd": 9.0},
            {"cache_tag": f"replay:bot:{tag}:{tail}", "cost_usd": None},
        ]
        llm.COSTS_PATH.open("a").write("".join(json.dumps(r) + "\n" for r in rows))
        return stubs.replay_fn(call, ledger, fork, bot_prompt, seed)

    out = run(tmp_path, stubs, replay_fn=replay_with_cost)[0]
    assert out.replays == 14 and out.cost_usd == pytest.approx(0.5 * 14)
    text = "\n".join(prove.describe(out))
    assert f"${0.5 * 14:.4f}" in text and "attributed rows only" in text


def test_audit_failure_after_a_paid_replay_keeps_the_replay_and_ends_inconclusive(tmp_path):
    """If the audit of a replay raises, the replay row must stay with audit null, and the gate must end inconclusive.
    Otherwise a ledger failure after a paid replay loses the replay and crashes the gate with no result."""
    out, paths, stubs = run(tmp_path, Stubs(fail_audit_at=1))
    rows = [json.loads(ln) for ln in paths["replays_path"].read_text().splitlines()]
    assert len(rows) == stubs.replays == 6  # the seed-0 pairs of 3 targets, then the gate stops as inconclusive
    assert rows[0]["audit"] is None and rows[0]["audit_error"].startswith("RuntimeError")
    assert all(r["audit"] is not None and "audit_error" not in r for r in rows[1:])
    assert out.result.decision == "inconclusive" and "unresolved" in out.result.reason
    assert len(paths["gates_path"].read_text().splitlines()) == 1


def test_replays_already_run_are_kept_when_the_run_stops(tmp_path):
    """A run that stops mid-way must keep the replay rows it finished and write no gate row. The replays are paid for.
    Losing them with the stopped run would waste the spend."""
    with pytest.raises(RuntimeError):
        run(tmp_path, Stubs(fail_after=5))
    assert len((tmp_path / "out" / "replays.jsonl").read_text().splitlines()) == 5
    assert not (tmp_path / "out" / "gates.jsonl").exists()


def test_pipeline_prove_subcommand_passes_its_options_and_prints(monkeypatch, capsys):
    """`pipeline prove` must pass every option to run_prove and print the plan or the result lines. A dropped option
    would make the operator's --n, --k or --dry-run silently do nothing."""
    seen = {}

    def fake(cause, prompt_path, **kw):
        seen.update(cause=cause, prompt=str(prompt_path), **kw)
        return prove.Outcome(prove.Plan(CAUSE, PARAM, "old", "new", [], [], 3, 12))

    monkeypatch.setattr(pipeline, "run_prove", fake)
    argv = ["prove", "--cause", CAUSE, "--prompt", "p.md", "--n", "4", "--k", "2", "--guards", "6", "--max-turns", "9"]
    pipeline.main([*argv, "--workers", "3", "--dry-run"])
    assert seen == dict(cause=CAUSE, prompt="p.md", n=4, k=2, guards=6, max_turns=9, workers=3, dry_run=True, attempt=1)
    assert f"cause {CAUSE}" in capsys.readouterr().out
    pipeline.main(argv)
    assert seen["workers"] == 1 and seen["dry_run"] is False  # the default keeps today's sequential run
    pipeline.main([*argv, "--attempt", "2"])
    assert seen["attempt"] == 2


def test_workers_warm_every_first_round_replay_before_the_gate_starts(tmp_path, capsys):
    """With --workers 4, all 2*n*k target replays and one NEW seed-0 replay per guard must run in the pool before the
    gate's first call, and the header must name the trade. If the gate started first the run would stay sequential, and
    an unmentioned trade would hide that warming can spend on replays a futility stop skips."""
    out, _, stubs = run(tmp_path, workers=4)
    n, k, g = 3, 2, 2
    ids = [c.call_id for c, _, _ in out.plan.targets]
    assert len(ids) == n and len(out.plan.guards) == g and out.plan.warm_replays == 2 * n * k + g
    warm, gate = stubs.log[: 2 * n * k + g], stubs.log[2 * n * k + g :]
    assert all(t.startswith("warm") for t, *_ in warm)  # in the pool
    assert gate and not any(t.startswith("warm") for t, *_ in gate)  # then the gate, on the main thread
    expected = {(c, w, s) for c in ids for w in ("old", "new") for s in range(k)}
    expected |= {(c.call_id, "new", 0) for c, _, _ in out.plan.guards}
    assert sorted(tuple(x) for _, *x in warm) == sorted(expected)  # every first-round replay, each exactly once
    assert len(gate) == out.replays == 14 and stubs.audits == 28  # the gate then asks for the same replays again
    text = capsys.readouterr().out
    assert "warm-up: 14 replays" in text and "futility" in text and "warm-up done: 14 of 14 ok, 0 failed" in text


@pytest.mark.parametrize("new_fails", [False, True], ids=["ship", "futility-reject"])
def test_warming_changes_neither_the_gate_result_nor_the_replays_rows(tmp_path, new_fails):
    """The same stubs with workers 1 and 4 must give an equal GateResult and equal replays and gates rows, also when
    the gate stops early on futility. If warming wrote rows or changed a decision, the speed-up would change what
    the demo proves."""
    seen = {}
    for workers in (1, 4):
        sub = tmp_path / f"w{workers}"
        sub.mkdir()
        out, paths, stubs = run(sub, Stubs(new_fails=new_fails), workers=workers)
        seen[workers] = (
            out.result.model_dump_json(),  # not the object: p_value is nan on a futility stop, and nan != nan
            out.replays,
            paths["replays_path"].read_text().splitlines(),
            paths["gates_path"].read_text().splitlines(),
        )
        assert stubs.replays == out.replays + (14 if workers > 1 else 0)  # warming adds calls, never rows
    assert seen[1] == seen[4]
    assert json.loads(seen[1][0])["decision"] == ("reject" if new_fails else "ship")
    assert seen[1][1] == (6 if new_fails else 14)


@pytest.mark.parametrize("stubs", [Stubs(fail_replay_at=3), Stubs(fail_audit_at=3)], ids=["replay", "audit"])
def test_a_failed_warm_up_job_does_not_stop_the_run(tmp_path, capsys, stubs):
    """A warm-up replay or audit that raises must be printed and skipped, and the gate must still end with a result.
    The gate runs the skipped job again. If one transient LLM error stopped a long parallel run, the operator
    would lose the warm-up that had already been paid for."""
    out, paths, _ = run(tmp_path, stubs, workers=4)
    assert out.result.decision == "ship" and out.replays == 14
    rows = [json.loads(ln) for ln in paths["replays_path"].read_text().splitlines()]
    assert len(rows) == 14 and all(r["audit"] is not None for r in rows)  # the gate redid the failed job
    text = capsys.readouterr().out
    assert "warm-up failed" in text and "RuntimeError" in text and "warm-up done: 13 of 14 ok, 1 failed" in text


def test_workers_1_makes_no_warm_up_call(tmp_path, capsys):
    """With workers 1 (the default) no replay or audit may run outside the gate's own calls, in no thread pool, and
    nothing is printed about a warm-up. Any extra call would double the spend of today's sequential run."""
    for kw in ({}, {"workers": 1}):
        sub = tmp_path / f"w{len(kw)}"
        sub.mkdir()
        out, _, stubs = run(sub, **kw)
        assert stubs.replays == stubs.audits == out.replays == 14
        assert {t for t, *_ in stubs.log} == {threading.current_thread().name}
    assert "warm-up" not in capsys.readouterr().out


def test_cost_includes_the_warm_up_spend(tmp_path):
    """The printed cost must count the replays computed during the warm-up. The gate then reads them from the cache
    and writes no cost row, so a count that starts at the gate would print $0 for a run that paid for every replay."""
    stubs, lock, paid = Stubs(), threading.Lock(), set()

    def replay_with_cache(call, ledger, fork, bot_prompt, seed):
        tag = replay.prompt_tag(prove._sent_prompt(bot_prompt, call, ledger))  # the tag of the sent prompt
        key = (call.call_id, bot_prompt, seed)
        with lock:
            if key not in paid:  # like the LLM cache: only the first call for a key costs money
                paid.add(key)
                row = {"cache_tag": f"replay:bot:{tag}:{seed}:{call.call_id}:{fork}", "cost_usd": 0.5}
                llm.COSTS_PATH.open("a").write(json.dumps(row) + "\n")
        return stubs.replay_fn(call, ledger, fork, bot_prompt, seed)

    out = run(tmp_path, stubs, replay_fn=replay_with_cache, workers=4)[0]
    assert out.replays == 14 and len(paid) == 14 and out.cost_usd == pytest.approx(0.5 * 14)


def test_workers_below_1_stop_before_any_llm_call(tmp_path):
    """--workers 0 or a negative number must stop with SystemExit before any replay. A pool of zero workers would
    raise inside the pool, after the plan was made, and a run without a pool would silently skip the warm-up."""
    pf, paths = write_inputs(tmp_path)
    for workers in (0, -2):
        with pytest.raises(SystemExit):
            prove.run_prove(CAUSE, pf, n=3, k=2, workers=workers, replay_fn=raising, audit_fn=raising, **paths)
    assert not paths["gates_path"].exists() and not paths["replays_path"].exists()


def test_cache_refresh_skips_the_warm_up(tmp_path, capsys, monkeypatch):
    """With VANILOOP_CACHE=refresh and workers 4, no warm-up call may run and one line must say why. In refresh mode
    the gate ignores the cache, so every warmed replay and audit would be paid for a second time."""
    monkeypatch.setenv("VANILOOP_CACHE", "refresh")
    out, _, stubs = run(tmp_path, workers=4)
    assert stubs.replays == stubs.audits == out.replays == 14
    assert not any(t.startswith("warm") for t, *_ in stubs.log)
    text = capsys.readouterr().out
    assert "warm-up skipped" in text and "VANILOOP_CACHE=refresh" in text and "warm-up done" not in text


def test_real_replay_fn_builds_the_brief_from_the_fork_turn(monkeypatch):
    """The real replay function must hand the fork turn to buyer_brief, so unclear_rate counts post-fork turns only.

    If the fork were dropped, buyer_brief would count the whole call and a replay forked late would get the noise of
    the early turns, which makes the replay channel noisier or cleaner than the part of the call it re-runs.
    """
    call = make_call("c1")
    call.turns[1].audio_unclear = True  # user turns are 1, 3, 5, 7; turns 1 and 3 are noisy
    call.turns[3].audio_unclear = True
    ledger = Ledger(call_id="c1", scope="bl_collection")
    briefs = []
    monkeypatch.setattr(replay, "replay_result", lambda c, f, p, brief, max_turns, seed: briefs.append((f, brief)))
    fn = prove.real_replay_fn(9)
    fn(call, ledger, 0, "P", 0)
    fn(call, ledger, 4, "P", 0)
    assert [(f, b.unclear_rate) for f, b in briefs] == [(0, 0.5), (4, 0.0)]


# A template with a live-seller branch and the product name; the rendered text differs per call.
TEMPLATE_TAIL = "\n{% if ast_seller_pns == 'LIVE' %}LIVE-BRANCH{% else %}NOSELLER-BRANCH{% endif %} {{ product_name }}"


def write_template_inputs(tmp_path, **kw):
    """write_inputs with a templated prompt. Calls t00..t09 had a live seller; every call has its own product."""
    pf, paths = write_inputs(tmp_path, **kw)
    pf.write_bytes(pf.read_bytes() + TEMPLATE_TAIL.encode())
    ledgers = []
    for line in paths["ledgers_path"].read_text().splitlines():
        cid = Ledger.model_validate_json(line).call_id
        live = [BotTurnLabel(idx=0, slot="handoff")] if cid.startswith("t0") else []
        ledgers.append(
            Ledger(call_id=cid, scope="oncall_astbuy", variant="main", initial_product=f"prod_{cid}", bot_turns=live)
        )
    paths["ledgers_path"].write_text("".join(x.model_dump_json() + "\n" for x in ledgers))
    return pf, paths


def test_real_replay_fn_sends_the_prompt_rendered_for_the_call(monkeypatch):
    """The real replay function must send the template rendered with the call's own variables, not the raw template.

    The raw template shows the bot every branch. A call with no live seller would then get the live-seller branch
    and the replay would skip the quantity question, which the audit grades as a fault of the prompt.
    """
    template = "{% if ast_seller_pns == 'LIVE' %}LIVE-BRANCH{% else %}NOSELLER-BRANCH{% endif %} {{ product_name }}"
    live = Ledger(call_id="c1", scope="oncall_astbuy", initial_product="CCTV camera",
                  bot_turns=[BotTurnLabel(idx=0, slot="handoff")])  # fmt: skip
    none = Ledger(call_id="c1", scope="bl_collection")
    sent = []
    monkeypatch.setattr(replay, "replay_result", lambda c, f, p, brief, max_turns, seed: sent.append((p, f, max_turns)))
    fn = prove.real_replay_fn(9)
    fn(make_call("c1"), live, 4, template, 0)
    fn(make_call("c1"), none, 2, template, 0)
    # the rendered branch comes first; the "Call values" block follows it (tested in test_call_values_block_*)
    assert [(p.split("\n\n## Call values")[0], f, m) for p, f, m in sent] == [
        ("LIVE-BRANCH CCTV camera", 4, 9),
        ("NOSELLER-BRANCH ", 2, 9),
    ]
    assert all("\n\n## Call values\n" in p for p, _, _ in sent)


def test_run_prove_sends_rendered_prompts_labels_old_new_and_attributes_cost_by_their_tags(tmp_path, monkeypatch):
    """A real run must send each call its rendered prompt, label rows old/new from the unrendered texts and count a
    cost row only under the tag of the rendered prompt its own call sent. Counting by the unrendered tags would print
    $0 for a run that paid for every replay, and a tag of another call's prompt would add that call's cost."""
    pf, paths = write_template_inputs(tmp_path)
    template = prompt.load(pf)
    sent = []
    stubs = Stubs()

    def stub_replay(call, fork, bot_prompt, brief, max_turns, seed):
        sent.append((call.call_id, bot_prompt))
        tag, tail = replay.prompt_tag(bot_prompt), f"{seed}:{call.call_id}:{fork}"
        other = next((replay.prompt_tag(t) for c, t in sent if c != call.call_id), "deadbeef")  # another call's prompt
        rows = [
            {"cache_tag": f"replay:bot:{tag}:{tail}", "cost_usd": 0.25},  # the rendered prompt this call sent
            {"cache_tag": f"replay:user:{tag}:{tail}", "cost_usd": 0.25},
            {"cache_tag": f"replay:bot:{replay.prompt_tag(template)}:{tail}", "cost_usd": 4.0},  # the raw template
            {"cache_tag": f"replay:bot:{other}:{tail}", "cost_usd": 2.0},  # the prompt of another call
        ]
        llm.COSTS_PATH.open("a").write("".join(json.dumps(r) + "\n" for r in rows))
        label = "new" if "PATCHED" in bot_prompt else "old"
        turns = [*call.turns[:fork], Turn(idx=fork, speaker="bot", start_sec=1, end_sec=2, text="reply")]
        res = CallRecord(call_id=f"{call.call_id}__{label}_{seed}", duration_sec=60, turns=turns)
        return ReplayResult(call=res, fork_turn=fork, ended_by="end_call")

    monkeypatch.setattr(replay, "replay_result", stub_replay)
    out = prove.run_prove(CAUSE, pf, n=3, k=2, guards=2, audit_fn=stubs.audit_fn, **paths)
    assert len(sent) == out.replays == 14
    for cid, text in sent:
        live = cid.startswith("t0")
        assert "{%" not in text and f"prod_{cid}" in text
        assert ("LIVE-BRANCH" in text, "NOSELLER-BRANCH" in text) == (live, not live)
    rows = [json.loads(x) for x in paths["replays_path"].read_text().splitlines()]
    assert len(rows) == 14 and all(f"__{r['prompt']}_" in r["call"]["call_id"] for r in rows)
    assert {r["prompt"] for r in rows} == {"old", "new"}
    assert out.cost_usd == pytest.approx(0.5 * 14)


def test_a_prompt_that_does_not_render_stops_before_any_replay_even_on_a_dry_run(tmp_path):
    """A patch whose new prompt has a broken Jinja tag must stop with SystemExit before any replay and write nothing.

    Without the early check the error would surface on the first NEW replay, after the OLD replays before it were paid
    for, or only at the cost sum after the whole run.
    """
    pf, paths = write_inputs(tmp_path, new_text="rule text 10 {% if %}")
    for kw in ({}, {"dry_run": True}):
        with pytest.raises(SystemExit, match="does not render"):
            prove.run_prove(CAUSE, pf, n=3, k=2, replay_fn=raising, audit_fn=raising, **kw, **paths)
    assert not paths["gates_path"].exists() and not paths["replays_path"].exists()


def test_a_patch_in_an_unrendered_branch_drops_the_call_and_stops_below_three_targets(tmp_path, capsys):
    """A patched line inside a Jinja branch that a call's variables do not render must drop that call, and a run
    with fewer than 3 calls left must stop before any LLM call. Otherwise the NEW replays are the OLD cache hits and
    the gate compares a prompt with itself."""
    # LIVE-BRANCH renders only for t00..t09 (a seller handoff); the patched line sits inside it.
    live_only = "{% if ast_seller_pns == 'LIVE' %}rule text 10{% endif %}"
    pf, paths = write_template_inputs(tmp_path, old_text="rule text 10", new_text="rule text 10 PATCHED")
    pf.write_bytes(pf.read_bytes().replace(b"rule text 10", live_only.encode(), 1))
    # make every target ledger a non-live one, so the patch is inert for all of them
    rows = [Ledger.model_validate_json(x) for x in paths["ledgers_path"].read_text().splitlines()]
    for r in rows:
        r.bot_turns = []
    paths["ledgers_path"].write_text("".join(r.model_dump_json() + "\n" for r in rows))
    with pytest.raises(SystemExit, match="patch does not reach this cause's calls"):
        prove.run_prove(CAUSE, pf, n=3, k=2, guards=2, dry_run=True, replay_fn=raising, audit_fn=raising, **paths)
    assert "inert patch" in capsys.readouterr().out
    assert not paths["gates_path"].exists() and not paths["replays_path"].exists()


def test_an_inert_call_is_replaced_by_the_next_candidate(tmp_path, capsys):
    """When the patch is inert for some calls only, those calls must be dropped by id and replaced from the other
    candidates, so n targets still run and only calls the patch can change are replayed."""
    live_only = "{% if ast_seller_pns == 'LIVE' %}rule text 10{% endif %}"
    pf, paths = write_template_inputs(tmp_path, old_text="rule text 10", new_text="rule text 10 PATCHED")
    pf.write_bytes(pf.read_bytes().replace(b"rule text 10", live_only.encode(), 1))
    out = prove.run_prove(CAUSE, pf, n=3, k=2, guards=2, dry_run=True, replay_fn=raising, audit_fn=raising, **paths)
    ids = [c.call_id for c, _, _ in out.plan.targets]
    assert len(ids) == 3 and all(i.startswith("t0") for i in ids)  # only t00..t09 render the live branch
    assert "inert patch" in capsys.readouterr().out  # t10 or t11 or t12 were picked first and dropped


def test_channel_fault_targets_are_replaced_by_the_next_candidate(capsys):
    """A target whose fork turn is flagged bot_misheard, or whose preceding user turn is flagged audio_unclear, must be
    dropped by id and replaced by the next candidate in the seeded order. A text replay hears clean text, so it cannot
    reproduce a mishearing, and such a target would only add noise to the gate."""
    calls, ledgers, audits = synthetic()
    plain, _ = prove.select_targets(calls, ledgers, audits, PARAM, 3)
    first, second = plain[0][0].call_id, plain[1][0].call_id
    fork_of = {c.call_id: f for c, _, f in plain}
    calls[first].turns[fork_of[first]].bot_misheard = True  # the fork turn itself
    before = next(t for t in reversed(calls[second].turns[: fork_of[second]]) if t.speaker == "user")
    before.audio_unclear = True  # the user turn right before the fork
    capsys.readouterr()
    dropped = {}
    items, _ = prove.select_targets(calls, ledgers, audits, PARAM, 3, dropped=dropped)
    ids = [c.call_id for c, _, _ in items]
    assert len(ids) == 3 and first not in ids and second not in ids
    assert sorted(dropped["channel fault"]) == sorted([first, second])
    assert f"channel fault: {', '.join(sorted([first, second]))}" in capsys.readouterr().out
    assert plain[2][0].call_id in ids  # the pick that was clean stays


def test_all_calls_channel_faulted_leave_too_few_targets(tmp_path):
    """When every candidate is a channel fault, the run must stop with the existing too-few-targets message before any
    LLM call, not replay calls that were dropped."""
    pf, paths = write_inputs(tmp_path)
    rows = [CallRecord.model_validate_json(x) for x in paths["calls_path"].read_text().splitlines()]
    for c in rows:
        for t in c.turns:
            t.audio_unclear = True
    paths["calls_path"].write_text("".join(c.model_dump_json() + "\n" for c in rows))
    with pytest.raises(SystemExit, match="only 0 target call"):
        prove.run_prove(CAUSE, pf, n=3, k=2, replay_fn=raising, audit_fn=raising, **paths)


def test_product_change_forks_at_the_first_bot_turn_after_the_change_in_every_scope():
    """For `product_change` the fork must be the first bot turn after ledger.product_changed_at_turn, also in the
    seller_unavailable_handoff scope where the audit's onset is the later handoff turn, and fall back to the onset
    when the change turn is missing. When the change turn is set but no bot turn follows it, there is no fork
    (None). A fork at the handoff would skip the bot's reply to the change, which is the behaviour under test."""
    call = make_call("c1", "bot user bot user bot user bot user bot")  # turns 0..8, bot on even idx
    audit = make_audit("c1", "Fatal", onset=7, parameter="product_change")  # the handoff turn is at 8; onset 7 here
    for scope in ("seller_unavailable_handoff", "bl_collection"):
        ledger = Ledger(call_id="c1", scope=scope, product_changed_at_turn=3)
        assert prove.target_fork(call, audit, "product_change", ledger) == 4
    on_bot = Ledger(call_id="c1", scope="seller_unavailable_handoff", product_changed_at_turn=2)  # a bot turn
    assert prove.target_fork(call, audit, "product_change", on_bot) == 3 + 1  # next bot turn after idx 2 is idx 4
    no_change = Ledger(call_id="c1", scope="seller_unavailable_handoff")
    assert prove.target_fork(call, audit, "product_change", no_change) == 7  # today's rule: the onset turn
    assert prove.target_fork(call, audit, "product_change") == 7  # no ledger given: the onset rule
    after_last = Ledger(call_id="c1", scope="bl_collection", product_changed_at_turn=8)  # no bot turn after it
    assert prove.target_fork(call, audit, "product_change", after_last) is None  # never an earlier fork
    other = make_audit("c1", "Fatal", onset=7, parameter="looping")
    changed = Ledger(call_id="c1", scope="bl_collection", product_changed_at_turn=3)
    assert prove.target_fork(call, other, "looping", changed) == 7  # other parameters keep the onset


def test_call_values_block_is_the_same_in_old_and_new_and_holds_the_ledger_values():
    """Every rendered prompt must end with a "Call values" JSON block holding the product, the specs with the options
    the bot offered and whether units were asked, identical for the old and the new prompt. Rendering only switches
    branches, so without the block the replay bot never sees the call's values and invents its own."""
    ledger = Ledger(
        call_id="c1", scope="bl_collection", initial_product="ProductA",
        bot_turns=[
            BotTurnLabel(idx=0, slot="spec", spec_name="size", options_offered=["small", "large"]),
            BotTurnLabel(idx=2, slot="spec", spec_name="size", options_offered=["large", "XL"]),
            BotTurnLabel(idx=4, slot="spec", spec_name="colour", options_offered=[]),
            BotTurnLabel(idx=6, slot="unit"),
        ],
    )  # fmt: skip
    call = make_call("c1")
    sent = [prove._sent_prompt(t, call, ledger) for t in ("rule {{ product_name }} A", "rule {{ product_name }} B")]
    blocks = [s.split("\n\n## Call values\n")[1] for s in sent]
    assert blocks[0] == blocks[1] and sent[0] != sent[1]
    values = json.loads(blocks[0])
    assert values == {
        "product_name": "ProductA",
        "specs": {"size": ["small", "large", "XL"], "colour": []},
        "units_asked": True,
    }
    bare = Ledger(call_id="c1", scope="bl_collection")
    assert json.loads(prove._sent_prompt("x", call, bare).split("\n\n## Call values\n")[1]) == {
        "product_name": "", "specs": {}, "units_asked": False,
    }  # fmt: skip


def test_product_change_target_with_no_bot_turn_after_the_change_is_skipped_and_printed(capsys):
    """A product_change target whose buyer changed product on the last turn must be skipped and printed, not forked at
    an earlier handoff onset. An earlier fork would drop the change from the replay history, so the replay would test a
    call without the change."""
    calls, ledgers, audits = {}, {}, {}
    for cid, change in (("a", 3), ("b", 3), ("c", 3), ("late", 7)):  # "late": change on the final user turn
        calls[cid] = make_call(cid)
        ledgers[cid] = Ledger(call_id=cid, scope="seller_unavailable_handoff", variant="main",
                                product_changed_at_turn=change)  # fmt: skip
        audits[cid] = make_audit(cid, "Fatal", onset=3, parameter="product_change")
    items, cand = prove.select_targets(calls, ledgers, audits, "product_change", 4)
    assert sorted(c.call_id for c, _, _ in items) == ["a", "b", "c"] and "late" not in cand
    assert "no bot turn after the product change: late" in capsys.readouterr().out


def test_prove_refuses_a_demoted_cause_before_any_replay_but_a_dry_run_still_plans(tmp_path):
    """A real prove run on a cause that causes.json demoted must stop with its status and failed conditions.

    Without it a demoted cause keeps its old patch and the gate could ship it, though it is no longer a pattern.
    """
    pf, paths = write_inputs(tmp_path)
    (tmp_path / "causes.json").write_text(json.dumps({"causes": [{"cause_id": CAUSE, "status": "single_category"}]}))
    (tmp_path / "outliers.jsonl").write_text(json.dumps({"cause_id": CAUSE, "failed": ["categories"]}) + "\n")
    with pytest.raises(SystemExit, match=r"single_category.*failed: categories"):
        prove.run_prove(CAUSE, pf, n=3, k=2, guards=2, replay_fn=raising, audit_fn=raising, **paths)
    assert not paths["gates_path"].exists()
    prove.run_prove(CAUSE, pf, n=3, k=2, guards=2, dry_run=True, replay_fn=raising, audit_fn=raising, **paths)
    (tmp_path / "causes.json").write_text(
        json.dumps({"causes": [{"cause_id": CAUSE, "status": "pattern_unconfirmed"}]})
    )
    out = prove.run_prove(
        CAUSE, pf, n=3, k=2, guards=2, replay_fn=Stubs().replay_fn, audit_fn=Stubs().audit_fn, **paths
    )
    assert out.result is not None
