"""prove: run the ship gate on saved calls. Synthetic records, stub replay and audit functions, no network."""

import json
import random

import pytest

from vaniloop import llm, pipeline, prompt, prove, replay
from vaniloop.models import AuditRecord, CallRecord, Finding, Ledger, Turn
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
    """Stub replay and audit: the old prompt fails `looping` on target calls, the patched prompt passes."""

    def __init__(self, fail_after=None, fail_audit_at=None):
        self.replays = self.audits = 0
        self.prompts = set()
        self.fail_after, self.fail_audit_at = fail_after, fail_audit_at

    def replay_fn(self, call, ledger, fork, bot_prompt, seed):
        self.replays += 1
        if self.fail_after is not None and self.replays > self.fail_after:
            raise RuntimeError("stop mid-run")
        self.prompts.add(bot_prompt)
        tag = "new" if "PATCHED" in bot_prompt else "old"
        turns = [*call.turns[:fork], Turn(idx=fork, speaker="bot", start_sec=1, end_sec=2, text=f"{tag} reply {seed}")]
        res = CallRecord(call_id=f"{call.call_id}__{tag}_{seed}", duration_sec=60, turns=turns)
        return ReplayResult(call=res, fork_turn=fork, ended_by="end_call")

    def audit_fn(self, call):
        self.audits += 1
        if self.audits == self.fail_audit_at:
            raise RuntimeError("ledger extraction failed")
        bad = call.call_id.startswith("t") and "__old_" in call.call_id
        return make_audit(call.call_id, "NonFatal" if bad else "Pass")


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
        tag, tail = replay.prompt_tag(bot_prompt), f"{seed}:{call.call_id}:{fork}"
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
    pipeline.main([*argv, "--dry-run"])
    assert seen == dict(cause=CAUSE, prompt="p.md", n=4, k=2, guards=6, max_turns=9, dry_run=True)
    assert f"cause {CAUSE}" in capsys.readouterr().out
