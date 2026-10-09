"""rootcause: the channel counterfactual. Synthetic records, stub replay and audit functions, no network."""

import json
import threading

import pytest

from vaniloop import llm, pipeline, replay, rootcause
from vaniloop.models import AuditRecord, CallRecord, Finding, Ledger, Turn
from vaniloop.replay import ReplayResult

PARAM, CAUSE, CHANNEL_CAUSE = "looping", "cause_looping", "cause_channel_looping"
SPEAKERS = "bot user bot user bot user bot user"
FORK = 3  # the failing finding's onset; user turns 3, 5, 7 follow, and turn 5 is flagged: unclear rate 1/3


def make_call(cid):
    turns = [
        Turn(
            idx=i,
            speaker=s,
            start_sec=6.0 * i,
            end_sec=6.0 * i + 5,
            text=f"t{i}",
            audio_unclear=True if i == 5 else None,
        )
        for i, s in enumerate(SPEAKERS.split())
    ]
    return CallRecord(call_id=cid, duration_sec=60, turns=turns)


def make_audit(cid, status="NonFatal", onset=FORK):
    f = Finding(parameter=PARAM, status=status, reason="r", basis="observed", onset_turn=onset)
    return AuditRecord(call_id=cid, verdict=status, scope="bl_collection", findings=[f])


def write_inputs(tmp_path, causes=None):
    """Five calls: a1 a2 a3 are root calls of the prompt cause, c1 is its symptom, ch1 belongs to the channel cause,
    e1 is another variant. Returns the keyword paths for run_rootcause."""
    ids = ["a1", "a2", "a3", "c1", "ch1", "e1"]
    paths = {f"{n}_path": tmp_path / f"{n}.jsonl" for n in ("calls", "ledgers", "audits")}
    paths["calls_path"].write_text("".join(make_call(c).model_dump_json() + "\n" for c in ids))
    variants = {"e1": "enrich"}
    paths["ledgers_path"].write_text(
        "".join(
            Ledger(call_id=c, scope="bl_collection", variant=variants.get(c, "main")).model_dump_json() + "\n"
            for c in ids
        )  # fmt: skip
    )
    paths["audits_path"].write_text("".join(make_audit(c).model_dump_json() + "\n" for c in ids))
    causes = causes or [
        dict(cause_id=CAUSE, parameter=PARAM, type="prompt", fatal_calls=["a1"], nonfatal_calls=["a2", "a3", "e1"],
             symptom_calls=["c1"]),
        dict(cause_id=CHANNEL_CAUSE, parameter=PARAM, type="channel", fatal_calls=[], nonfatal_calls=["ch1"]),
    ]  # fmt: skip
    paths["causes_path"] = tmp_path / "causes.json"
    paths["causes_path"].write_text(json.dumps({"causes": causes}))
    paths["out_path"] = tmp_path / "out" / "rootcause.jsonl"
    pf = tmp_path / "prompt.md"
    pf.write_text("\n".join(f"line {i}: rule text {i}" for i in range(1, 31)))
    return pf, paths


class Stubs:
    """Stub replay and audit. `mode` decides when the replayed call fails the cause parameter after the fork:
    noisy_only (only a replay with unclear_rate > 0), always, never, pre_fork (a Fatal finding before the fork).
    `log` keeps (call id, seed, unclear_rate, prompt) per replay call."""

    def __init__(self, mode="noisy_only", noisy_fail_seeds=None):
        self.mode, self.noisy_fail_seeds = mode, noisy_fail_seeds
        self.log = []
        self.lock = threading.Lock()  # --workers calls the stubs from a thread pool

    def replay_fn(self, call, ledger, fork, bot_prompt, seed, unclear_rate):
        with self.lock:
            self.log.append((call.call_id, seed, unclear_rate, bot_prompt))
        arm = "noisy" if unclear_rate > 0 else "clean"
        turns = [*call.turns[:fork], Turn(idx=fork, speaker="bot", start_sec=1, end_sec=2, text=f"reply {seed}")]
        rec = CallRecord(call_id=f"{call.call_id}__{arm}_{seed}", duration_sec=60, turns=turns)
        return ReplayResult(call=rec, fork_turn=fork, ended_by="end_call")

    def audit_fn(self, call):
        arm, seed = call.call_id.rsplit("__", 1)[1].split("_")
        if self.mode == "pre_fork":
            return make_audit(call.call_id, "Fatal", onset=0)
        bad = self.mode == "always" or (
            self.mode == "noisy_only"
            and arm == "noisy"
            and (self.noisy_fail_seeds is None or int(seed) in self.noisy_fail_seeds)
        )
        return make_audit(call.call_id, "NonFatal" if bad else "Pass", onset=call.turns[-1].idx)


def run(tmp_path, stubs=None, cause=CAUSE, **kw):
    pf, paths = write_inputs(tmp_path)
    stubs = stubs or Stubs()
    args = dict(k=3, replay_fn=stubs.replay_fn, audit_fn=stubs.audit_fn, **paths)
    return rootcause.run_rootcause(cause, pf, **{**args, **kw}), paths, stubs


def labels(out):
    return {r["call_id"]: r["label"] for r in out["rows"]}


def test_fault_only_under_noise_is_labelled_channel(tmp_path):
    """A call that fails every noisy replay and passes every clean one must be labelled channel. Without this the
    classifier would send audio faults to prompt fixes, which cannot cure them."""
    out, _, _ = run(tmp_path)
    assert labels(out) == {"a1": "channel", "a2": "channel", "a3": "channel"}
    row = out["rows"][0]
    assert (row["noisy_fails"], row["clean_fails"], row["k"]) == (3, 0, 3)


def test_fault_in_both_arms_is_not_channel(tmp_path):
    """A call that fails with clean audio too must be labelled not_channel, a candidate prompt fault. Calling it
    channel would hide a real prompt problem behind bad audio."""
    out, _, _ = run(tmp_path, Stubs("always"))
    assert set(labels(out).values()) == {"not_channel"}
    assert (out["rows"][0]["noisy_fails"], out["rows"][0]["clean_fails"]) == (3, 3)


def test_no_fault_in_either_arm_is_not_reproduced(tmp_path):
    """A call whose replays never fail, noisy or clean, must be labelled not_reproduced. Calling it channel or
    not_channel would claim a cause the replay cannot show."""
    out, _, _ = run(tmp_path, Stubs("never"))
    assert set(labels(out).values()) == {"not_reproduced"}


def test_both_arms_use_the_same_seeds_and_only_the_noisy_arm_is_noisy(tmp_path):
    """Per call, the noisy and the clean arm must replay the same seeds 0..K-1 with the OLD prompt, the clean arm
    with unclear_rate 0 and the noisy arm with the call's own rate. Different seeds would compare two draws of the
    buyer, not the channel, and a nonzero clean rate would not be a clean arm."""
    out, _, stubs = run(tmp_path, k=4)
    old = rootcause.prompt_mod.load(tmp_path / "prompt.md")
    for cid in ("a1", "a2", "a3"):
        calls = [(s, r) for c, s, r, _ in stubs.log if c == cid]
        noisy = sorted(s for s, r in calls if r > 0)
        clean = sorted(s for s, r in calls if r == 0)
        assert noisy == clean == [0, 1, 2, 3]
        assert [r for _, r in calls if r > 0] == [pytest.approx(1 / 3)] * 4  # the rate from the call's audio flags
    assert {p for *_, p in stubs.log} == {old}
    assert out["rows"][0]["noisy_rate"] == pytest.approx(1 / 3)


@pytest.mark.parametrize(
    ("noisy", "clean", "k", "nu", "cu", "expected"),
    [
        (2, 0, 3, 0, 0, "channel"),  # most noisy seeds (2 of 3), none clean
        (2, 1, 3, 0, 0, "channel"),  # one clean fail of 3 is "few"
        (3, 2, 3, 0, 0, "not_channel"),  # clean fails on most too
        (1, 0, 3, 0, 0, "unresolved"),  # noisy fails on only 1 of 3: not most
        (3, 1, 3, 0, 1, "unresolved"),  # a clean fail plus an unresolved clean trial: 2 of 3 is not few
        (2, 0, 4, 0, 0, "unresolved"),  # exactly half is not most
        (3, 1, 4, 0, 0, "channel"),  # 1 of 4 clean fails is under a third (4/3): few
        (3, 2, 4, 0, 0, "unresolved"),  # 2 of 4 clean fails is not few and not most
        (0, 0, 3, 1, 0, "unresolved"),  # an unresolved trial is not a pass, so not_reproduced is refused
        (0, 3, 3, 0, 0, "unresolved"),  # clean worse than noisy: neither channel nor a both-fail
        (0, 0, 3, 0, 0, "not_reproduced"),
    ],
)
def test_label_thresholds(noisy, clean, k, nu, cu, expected):
    """The label thresholds must be exactly: noisy fails on more than half of K, clean fails plus unresolved at most
    a third of K. A loose threshold would label a flaky call channel and send a prompt fault away unfixed."""
    assert rootcause.label_call(noisy, clean, k, nu, cu) == expected


def test_judges_the_cause_parameter_after_the_fork(tmp_path):
    """A cause finding that starts before the fork sits in the copied prefix and fails both arms alike. It must not
    count as a failure, so such a call is unresolved, never not_channel."""
    out, _, _ = run(tmp_path, Stubs("pre_fork"))
    assert set(labels(out).values()) == {"unresolved"}
    assert out["rows"][0]["noisy_fails"] == 0 and out["rows"][0]["noisy_unresolved"] == 3


def test_a_mixed_result_is_unresolved(tmp_path):
    """A noisy arm that fails on only one of three seeds is too weak to call a channel fault. It must come back
    unresolved so nobody acts on a single lucky seed."""
    out, _, _ = run(tmp_path, Stubs("noisy_only", noisy_fail_seeds={0}))
    assert set(labels(out).values()) == {"unresolved"}


def test_prompt_cause_candidates_are_its_root_calls_only(tmp_path):
    """A prompt cause must replay its fatal and nonfatal root calls, and not its symptom calls, another variant or
    another cause's calls. Replaying symptoms would spend money on calls whose fault belongs to another cause."""
    out, _, stubs = run(tmp_path)
    assert {c for c, *_ in stubs.log} == {"a1", "a2", "a3"}
    assert out["skipped"] == {"e1": "variant enrich, the prompt is the main variant"}


def test_channel_cause_candidates_are_its_own_calls(tmp_path):
    """A cause_channel_<parameter> cause must replay its own calls and judge its parameter. The prompt-cause rule
    would find no calls for it, so the counterfactual could not test a cause already suspected of bad audio."""
    out, _, stubs = run(tmp_path, cause=CHANNEL_CAUSE)
    assert {c for c, *_ in stubs.log} == {"ch1"}
    assert labels(out) == {"ch1": "channel"}


def test_dry_run_spends_nothing_and_writes_nothing(tmp_path):
    """A dry run must call no replay, no audit and no LLM, and write no file. It exists so the owner can see the plan
    and the replay count before any spend."""

    def raising(*_a, **_k):
        raise AssertionError("a replay or audit function was called")

    pf, paths = write_inputs(tmp_path)
    out = rootcause.run_rootcause(CAUSE, pf, k=3, dry_run=True, replay_fn=raising, audit_fn=raising, **paths)
    assert out["rows"] == [] and len(out["plan"].targets) == 3
    assert not paths["out_path"].exists() and not llm.COSTS_PATH.exists()
    assert "at most 18 replays" in "\n".join(rootcause.describe_rootcause(CAUSE, out))


def test_rows_are_written_and_a_rerun_replaces_only_that_cause(tmp_path):
    """The run must write one row per call with cause_id, call_id, both fail counts and the label to rootcause.jsonl.
    A rerun of one cause must replace its rows and keep another cause's, or a rerun would erase earlier results."""
    out, paths, _ = run(tmp_path)
    rows = [json.loads(x) for x in paths["out_path"].read_text().splitlines()]
    assert [(r["cause_id"], r["call_id"], r["noisy_fails"], r["clean_fails"], r["label"]) for r in rows] == [
        (CAUSE, c, 3, 0, "channel") for c in ("a1", "a2", "a3")
    ]
    run(tmp_path, Stubs("never"), cause=CHANNEL_CAUSE)
    run(tmp_path, Stubs("always"))  # the prompt cause again
    rows = [json.loads(x) for x in paths["out_path"].read_text().splitlines()]
    assert [(r["cause_id"], r["label"]) for r in rows] == [
        (CHANNEL_CAUSE, "not_reproduced"),
        *[(CAUSE, "not_channel")] * 3,
    ]


def test_a_failing_call_gets_an_unresolved_row_and_the_others_finish(tmp_path):
    """A call whose replay raises must get an unresolved row with the error, and the other calls must still be
    labelled. One bad call must not lose a paid run."""
    stubs = Stubs()
    inner = stubs.replay_fn

    def flaky(call, ledger, fork, bot_prompt, seed, rate):
        if call.call_id == "a2":
            raise RuntimeError("replay blew up")
        return inner(call, ledger, fork, bot_prompt, seed, rate)

    out, _, _ = run(tmp_path, stubs=stubs, replay_fn=flaky)
    assert labels(out) == {"a1": "channel", "a2": "unresolved", "a3": "channel"}
    assert "replay blew up" in next(r["error"] for r in out["rows"] if r["call_id"] == "a2")


def test_a_budget_stop_ends_the_run_and_keeps_finished_rows(tmp_path):
    """A budget stop must end the run, not become an unresolved row, and the calls finished before it must still be
    written. Hiding it would spend past the cap, and dropping the rows would waste what was paid."""

    def broke(call, ledger, fork, bot_prompt, seed, rate):
        if call.call_id == "a3":
            raise llm.BudgetExceeded("cap")
        return Stubs().replay_fn(call, ledger, fork, bot_prompt, seed, rate)

    pf, paths = write_inputs(tmp_path)
    with pytest.raises(llm.BudgetExceeded):
        rootcause.run_rootcause(CAUSE, pf, k=3, replay_fn=broke, audit_fn=Stubs().audit_fn, **paths)
    rows = [json.loads(x) for x in paths["out_path"].read_text().splitlines()]
    assert [r["call_id"] for r in rows] == ["a1", "a2"]


def test_workers_give_the_same_rows(tmp_path):
    """Running on several workers must give the same labelled rows as one worker. Threads that mix up calls or
    seeds would make the paid run differ from the plan."""
    (tmp_path / "one").mkdir()
    (tmp_path / "many").mkdir()
    one, _, _ = run(tmp_path / "one", k=3)
    many, _, _ = run(tmp_path / "many", k=3, workers=3)
    assert one["rows"] == many["rows"]


def test_unknown_cause_and_audio_cause_stop_before_any_replay(tmp_path):
    """A cause that is not in causes.json, or a dead_air cause that a text replay cannot judge, must stop with
    SystemExit before any replay. A silent run would label nothing and look like success."""
    pf, paths = write_inputs(tmp_path)
    with pytest.raises(SystemExit, match="not in"):
        rootcause.run_rootcause("cause_nothing", pf, dry_run=True, **paths)
    paths["causes_path"].write_text(json.dumps({"causes": [dict(cause_id="cause_dead_air", parameter="dead_air")]}))
    with pytest.raises(SystemExit, match="audio timing"):
        rootcause.run_rootcause("cause_dead_air", pf, dry_run=True, **paths)


def test_real_replay_fn_forces_a_clean_channel_without_touching_replay(tmp_path, monkeypatch):
    """The real replay function must pass the unclear_rate it is given to the buyer brief: 0.0 for the clean arm,
    the call's rate for the noisy arm. If the clean arm kept the noise, the counterfactual would compare noise
    with noise and label every call not_channel."""
    seen = []

    def fake_replay_result(call, fork, sent, brief, max_turns, seed):
        seen.append((brief.unclear_rate, seed, max_turns))
        return ReplayResult(call=call, fork_turn=fork, ended_by="end_call")

    monkeypatch.setattr(replay, "replay_result", fake_replay_result)
    pf, paths = write_inputs(tmp_path)
    call, ledger = make_call("a1"), Ledger(call_id="a1", scope="bl_collection", variant="main")
    assert replay.buyer_brief(call, ledger, FORK).unclear_rate == pytest.approx(1 / 3)
    fn = rootcause.real_channel_replay_fn(7)
    fn(call, ledger, FORK, "bot prompt", 2, 1 / 3)
    fn(call, ledger, FORK, "bot prompt", 2, 0.0)
    assert seen == [(pytest.approx(1 / 3), 2, 7), (0.0, 2, 7)]


def test_pipeline_rootcause_subcommand_passes_its_options_and_prints(monkeypatch, capsys, tmp_path):
    """`pipeline rootcause` must pass --cause, --prompt, --k, --max-turns, --workers and --dry-run to run_rootcause
    and print its lines. A dropped option would run a different, paid experiment than the one asked for."""
    pf, paths = write_inputs(tmp_path)
    seen = {}

    def fake(cause_id, prompt_path, **kw):
        seen.update(cause=cause_id, prompt=str(prompt_path), **kw)
        return rootcause.run_rootcause(cause_id, prompt_path, **{**kw, **paths})

    monkeypatch.setattr(pipeline, "run_rootcause", fake)
    pipeline.main(["rootcause", "--cause", CAUSE, "--prompt", str(pf), "--k", "2", "--max-turns", "9",
                   "--workers", "3", "--dry-run"])  # fmt: skip
    assert seen == dict(cause=CAUSE, prompt=str(pf), k=2, max_turns=9, workers=3, dry_run=True)
    assert f"cause {CAUSE}" in capsys.readouterr().out
