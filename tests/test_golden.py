"""Tests for the golden set: frozen versions, stratified picks and the per-rule grader gate. Synthetic data only."""

import hashlib
import json
from collections import Counter

import pytest

from vaniloop import prove
from vaniloop.golden import (
    BAR_KAPPA,
    BAR_N,
    build_golden,
    cohen_kappa,
    golden_agreement,
    load_golden,
    run_golden_check,
    select_golden,
    sha_of,
)
from vaniloop.models import AuditRecord, CallRecord, Finding, Ledger, Turn
from vaniloop.pipeline import main
from vaniloop.replay import ReplayResult


def row(call_id, verdict, params=(), scope="bl_collection", variant="main", cause="none"):
    return {
        "call_id": call_id,
        "verdict": verdict,
        "parameters": list(params),
        "scope": scope,
        "variant": variant,
        "cause": cause,
    }


def write_inputs(tmp_path, n_pass=30, n_fail=10):
    """Write hand_grades, audits, ledgers and causes for synthetic calls across 2 scopes and 2 variants."""
    grades, audits, ledgers, members = [], [], [], []
    for i in range(n_pass + n_fail):
        cid = f"c{i:03d}"
        fail = i >= n_pass
        scope = "bl_collection" if i % 2 == 0 else "oncall_astbuy"
        variant = "main" if i % 4 < 2 else "redial"
        grades.append({"call_id": cid, "verdict": "Fatal" if fail else "Pass", "parameters": ["looping"] * fail})
        findings = [Finding(parameter="looping", status="Fatal" if fail else "Pass", reason="s", basis="observed")]
        audits.append(
            AuditRecord(call_id=cid, verdict="Fatal" if fail else "Pass", scope=scope, findings=findings).model_dump(
                mode="json"
            )
        )
        ledgers.append({"call_id": cid, "variant": variant})
        if fail:
            members.append(cid)
    causes = {"causes": [{"cause_id": "cause_looping", "fatal_calls": members, "nonfatal_calls": []}]}
    paths = {k: tmp_path / k for k in ("hand_grades.jsonl", "audits.jsonl", "ledgers.jsonl", "causes.json")}
    paths["hand_grades.jsonl"].write_text("".join(json.dumps(r) + "\n" for r in grades))
    paths["audits.jsonl"].write_text("".join(json.dumps(r) + "\n" for r in audits))
    paths["ledgers.jsonl"].write_text("".join(json.dumps(r) + "\n" for r in ledgers))
    paths["causes.json"].write_text(json.dumps(causes))
    return paths


def build(tmp_path, paths, **kw):
    return build_golden(
        grades_path=paths["hand_grades.jsonl"],
        audits_path=paths["audits.jsonl"],
        ledgers_path=paths["ledgers.jsonl"],
        causes_path=paths["causes.json"],
        out_dir=tmp_path / "golden",
        **kw,
    )


def test_second_build_writes_v2_and_leaves_v1_unchanged(tmp_path):
    """A second build must write v2 and leave v1 byte-identical, with a sha that matches its content.

    If a build could overwrite a version, a fix proven against v1 could not be compared with a later run.
    """
    paths = write_inputs(tmp_path)
    p1 = build(tmp_path, paths)
    before = p1.read_bytes()
    sha1 = load_golden(p1)["sha256"]
    p2 = build(tmp_path, paths)
    assert (p1.name, p2.name) == ("v1.json", "v2.json")
    assert p1.read_bytes() == before
    assert load_golden(p1)["sha256"] == sha1
    assert hashlib.sha256(json.dumps(load_golden(p1)["calls"], sort_keys=True).encode()).hexdigest() == sha1
    assert sha_of(load_golden(p2)["calls"]) == load_golden(p2)["sha256"]


def test_load_golden_rejects_a_tampered_file(tmp_path):
    """A golden file whose calls changed after the build must fail to load.

    A silently edited set would make every later pass or fail verdict meaningless.
    """
    p = build(tmp_path, write_inputs(tmp_path))
    doc = json.loads(p.read_text())
    doc["calls"][0]["verdict"] = "Fatal" if doc["calls"][0]["verdict"] == "Pass" else "Pass"
    p.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="sha256"):
        load_golden(p)


def test_strata_are_balanced_and_mostly_pass():
    """Selection must be mostly Pass and spread across strata, capped at n, with equal-size picks per stratum.

    A skewed set would let one scope or cause dominate the agreement numbers the gate relies on.
    """
    rows = []
    for scope in ("bl_collection", "oncall_astbuy"):
        for variant in ("main", "redial"):
            rows += [row(f"p-{scope}-{variant}-{i}", "Pass", scope=scope, variant=variant) for i in range(20)]
    for cause in ("cause_a", "cause_b"):
        rows += [row(f"f-{cause}-{i}", "Fatal", ["x"], cause=cause) for i in range(20)]
    picked = select_golden(rows, n=40, pass_share=0.75, seed=0)
    assert len(picked) == 40
    verdicts = Counter(r["verdict"] for r in picked)
    assert verdicts == {"Pass": 30, "Fatal": 10}
    pass_strata = Counter((r["scope"], r["variant"]) for r in picked if r["verdict"] == "Pass")
    assert sorted(pass_strata.values()) == [7, 7, 8, 8]  # 30 over 4 strata
    fail_strata = Counter(r["cause"] for r in picked if r["verdict"] != "Pass")
    assert fail_strata == {"cause_a": 5, "cause_b": 5}
    assert select_golden(rows, n=40, pass_share=0.75, seed=0) == picked  # deterministic
    assert len({r["call_id"] for r in picked}) == 40


def test_select_fills_from_the_other_pool_when_one_runs_short():
    """When Fail calls run out, the quota must fill from Pass, and a pool never repeats a call.

    A short Fail pool must not shrink the set below the calls available.
    """
    rows = [row(f"p{i}", "Pass") for i in range(20)] + [row(f"f{i}", "Fatal", ["x"], cause="c") for i in range(2)]
    picked = select_golden(rows, n=10, pass_share=0.6, seed=0)
    assert len(picked) == 10
    assert Counter(r["verdict"] for r in picked) == {"Pass": 8, "Fatal": 2}


def test_cohen_kappa_known_values():
    """Cohen's kappa must give 1.0 for full agreement, 0.0 for chance level and None when undefined.

    A wrong kappa would mark a good rule advisory or let a bad rule block fixes.
    """
    assert cohen_kappa([True, False, True, False], [True, False, True, False]) == 1.0
    assert cohen_kappa([True, True, False, False], [True, False, True, False]) == 0.0
    assert cohen_kappa([False, False], [False, False]) is None  # one class only: chance agreement is 1


def make_audit(call_id, statuses):
    findings = [Finding(parameter=p, status=s, reason="s", basis="observed") for p, s in statuses.items()]
    return AuditRecord(call_id=call_id, verdict="Pass", scope="bl_collection", findings=findings)


def golden_doc(calls):
    return {"version": 1, "calls": calls}


def test_rule_with_kappa_0_4_is_advisory():
    """A rule whose kappa is 0.4 must be advisory even with n of 20, above the minimum count.

    A weak grader rule must not block or pass a fix.
    """
    # 20 calls: hand fails 10, audit fails 10, they agree on 14 (7 both fail, 7 both pass): kappa 0.4
    pairs = [(True, True)] * 7 + [(True, False)] * 3 + [(False, True)] * 3 + [(False, False)] * 7
    calls = [row(f"c{i}", "Fatal" if h else "Pass", ["looping"] if h else []) for i, (h, _) in enumerate(pairs)]
    audits = [make_audit(f"c{i}", {"looping": "Fatal" if a else "Pass"}) for i, (_, a) in enumerate(pairs)]
    out = golden_agreement(golden_doc(calls), audits)["looping"]
    assert out["n"] == 20 and out["agree"] == 14
    assert out["kappa"] == pytest.approx(0.4)
    assert out["status"] == "advisory"


def test_rule_above_the_bar_is_gating_and_a_small_n_is_advisory():
    """Kappa 1.0 with n at the bar is gating, and the same kappa with n below the bar is advisory.

    The bar needs both kappa and enough calls, so a lucky 3-call match cannot let a rule gate fixes.
    """
    assert (BAR_KAPPA, BAR_N) == (0.6, 10)
    for n, expected in ((10, "gating"), (9, "advisory")):
        hand = [i % 2 == 0 for i in range(n)]
        calls = [row(f"c{i}", "Fatal" if h else "Pass", ["looping"] if h else []) for i, h in enumerate(hand)]
        audits = [make_audit(f"c{i}", {"looping": "Fatal" if h else "Pass"}) for i, h in enumerate(hand)]
        out = golden_agreement(golden_doc(calls), audits)["looping"]
        assert out["kappa"] == 1.0 and out["n"] == n
        assert out["status"] == expected


def test_agreement_skips_calls_the_audit_did_not_decide():
    """A call whose finding is Unknown or missing must not count toward a rule's n or agreement.

    Missing evidence counted as agreement would inflate kappa for rules that rarely run.
    """
    calls = [row("a", "Pass"), row("b", "Pass"), row("c", "Pass")]
    audits = [make_audit("a", {"looping": "Unknown"}), make_audit("b", {"looping": "Pass"}), make_audit("c", {})]
    out = golden_agreement(golden_doc(calls), audits)["looping"]
    assert out["n"] == 1 and out["status"] == "advisory"


def test_pipeline_golden_build_cli(tmp_path, monkeypatch, capsys):
    """`pipeline golden build` must write the next version and print its path, count and sha.

    A CLI that is not wired would leave the owner with tooling nobody can run.
    """
    paths = write_inputs(tmp_path)
    monkeypatch.setattr("vaniloop.pipeline.GOLDEN_DIR", tmp_path / "golden")
    monkeypatch.setattr("vaniloop.pipeline.GOLDEN_INPUTS", {k: v for k, v in paths.items()})
    main(["golden", "build"])
    out = capsys.readouterr().out
    assert "v1.json" in out and "sha256=" in out
    assert (tmp_path / "golden" / "v1.json").exists()


def test_stale_builder_cannot_truncate_a_frozen_version(tmp_path, monkeypatch):
    """A builder that picked version 1 before another builder linked it must not touch v1's bytes.

    A shared PID-based temp name let the late builder truncate the inode that v1.json already pointed to.
    """
    import os

    import vaniloop.golden as g

    paths = write_inputs(tmp_path)
    real_link, real_next = os.link, g._next_version
    state = {"frozen": None, "stale": False, "inner": False}

    def link_then_race(src, dst, *a, **k):
        real_link(src, dst, *a, **k)
        if not state["inner"]:
            state["inner"] = True
            state["frozen"] = (tmp_path / "golden" / "v1.json").read_bytes()
            state["stale"] = True  # a second builder that read the version before this link
            build(tmp_path, paths, seed=1)

    def next_version(out_dir):
        if state["stale"]:
            state["stale"] = False
            return 1
        return real_next(out_dir)

    monkeypatch.setattr(os, "link", link_then_race)
    monkeypatch.setattr(g, "_next_version", next_version)
    build(tmp_path, paths, seed=0)
    assert (tmp_path / "golden" / "v1.json").read_bytes() == state["frozen"]
    assert (tmp_path / "golden" / "v2.json").exists()
    assert not list((tmp_path / "golden").glob(".v*.tmp"))


def test_concurrent_builds_write_distinct_valid_versions(tmp_path):
    """Eight builds racing in threads must write eight distinct versions that all load and match their sha.

    A version clash or a shared temp file would lose a build or leave a corrupt frozen set.
    """
    from concurrent.futures import ThreadPoolExecutor

    paths = write_inputs(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as ex:
        outs = list(ex.map(lambda i: build(tmp_path, paths, seed=i), range(8)))
    assert sorted(p.name for p in outs) == [f"v{i}.json" for i in range(1, 9)]
    for p in outs:
        assert load_golden(p)["version"] == int(p.stem[1:])


def test_abstaining_on_human_faults_does_not_make_a_rule_gating():
    """A rule the audit marks Unknown on most calls humans failed must stay advisory despite a perfect kappa.

    Selective abstention would otherwise hide every miss: only the easy calls would be scored.
    """
    calls, audits = [], []
    for i in range(10):  # human faults; the audit decides 4 and abstains on 6
        calls.append(row(f"f{i}", "Fatal", ["looping"]))
        status = "Fatal" if i < 4 else "Unknown"
        audits.append(make_audit(f"f{i}", {"looping": status}))
    for i in range(30):  # no fault, decided and agreed
        calls.append(row(f"p{i}", "Pass"))
        audits.append(make_audit(f"p{i}", {"looping": "Pass"}))
    out = golden_agreement(golden_doc(calls), audits)["looping"]
    assert out["kappa"] == 1.0 and out["n"] == 34
    assert out["coverage"] == pytest.approx(0.85)
    assert out["fault_coverage"] == pytest.approx(0.4)
    assert out["skipped"]["unknown"] == 6
    assert out["status"] == "advisory"


def test_low_overall_coverage_and_missing_audits_are_reported_and_advisory():
    """Calls with no audit row or no finding must be counted per rule, and low coverage must block gating.

    Silently dropping them would let a rule that ran on a few calls look as sure as one that ran on all.
    """
    calls = [row(f"c{i}", "Fatal" if i % 2 == 0 else "Pass", ["looping"] if i % 2 == 0 else []) for i in range(20)]
    audits = [make_audit(f"c{i}", {"looping": "Fatal" if i % 2 == 0 else "Pass"}) for i in range(12)]  # c12.. no audit
    audits[0] = make_audit("c0", {})  # audit ran, no looping finding
    out = golden_agreement(golden_doc(calls), audits)["looping"]
    assert out["skipped"] == {"no_audit": 8, "no_finding": 1, "unknown": 0}
    assert out["n"] == 11 and out["coverage"] == pytest.approx(0.55)
    assert out["status"] == "advisory"


def test_short_pass_pool_keeps_a_pass_majority_and_says_so(tmp_path, capsys, monkeypatch):
    """With too few Pass calls the sample must stay Pass-majority and smaller, and the build must say so.

    Filling the quota from failures would give a mostly failing set that does not match real call mix.
    """
    rows = [row(f"p{i}", "Pass") for i in range(4)] + [row(f"f{i}", "Fatal", ["x"], cause="c") for i in range(30)]
    picked = select_golden(rows, n=10, pass_share=0.6, seed=0)
    verdicts = Counter(r["verdict"] for r in picked)
    assert verdicts["Pass"] > verdicts["Fatal"] and verdicts["Pass"] == 4
    assert len(picked) < 10
    paths = write_inputs(tmp_path, n_pass=4, n_fail=30)
    monkeypatch.setattr("vaniloop.pipeline.GOLDEN_DIR", tmp_path / "golden")
    monkeypatch.setattr("vaniloop.pipeline.GOLDEN_INPUTS", dict(paths))
    main(["golden", "build", "--n", "10"])
    assert "Pass majority could not be met at full size" in capsys.readouterr().out
    assert load_golden(tmp_path / "golden" / "v1.json")["short_of"] == 10


# ---- golden check (#76): stubs only, no LLM call anywhere ----

LOOP, QTY = "cause_looping", "cause_qty_probing"
TURNS = "bot user bot user bot user bot user"


def _anchor(n):
    return f"line {n}: rule text {n}"


def _patch(cause, n, tag, attempt=1):
    return dict(
        cause_id=cause, target_start_line=n, target_end_line=n, old_text=_anchor(n),
        new_text=f"{_anchor(n)} PATCHED_{tag}", rationale="r", risk="low", attempt=attempt,
    )  # fmt: skip


def _gate_row(cause, decision, attempt=1):
    return {"cause_id": cause, "decision": decision, "attempt": attempt}


def _status_audit(cid, loop="Pass", qty="Pass"):
    findings = [
        Finding(parameter=p, status=s, reason="s", basis="observed")
        for p, s in (("looping", loop), ("qty_probing", qty))
    ]
    return AuditRecord(call_id=cid, verdict="Pass", scope="bl_collection", findings=findings)


def write_check_inputs(tmp_path, patches=None, gates=None):
    """Write 12 looping, 12 qty and 12 Pass golden calls, their saved audits, a prompt, patches and gate rows.

    The saved audit matches the human label for looping (a gating rule) and misses every qty fault (an advisory rule).
    """

    def call(cid):
        turns = [
            Turn(idx=i, speaker=s, start_sec=6.0 * i, end_sec=6.0 * i + 5, text=f"t{i}")
            for i, s in enumerate(TURNS.split())
        ]
        return CallRecord(call_id=cid, duration_sec=60, turns=turns)

    ids = [(f"lf{i:02d}", "Fatal", ["looping"]) for i in range(12)]
    ids += [(f"qf{i:02d}", "NonFatal", ["qty_probing"]) for i in range(12)]
    ids += [(f"gp{i:02d}", "Pass", []) for i in range(12)]
    golden = [
        {"call_id": c, "scope": "bl_collection", "variant": "main", "cause": "none", "verdict": v, "parameters": p,
         "first_fault_turn": None if v == "Pass" else 3}
        for c, v, p in ids
    ]  # fmt: skip
    gdir = tmp_path / "golden"
    gdir.mkdir(parents=True)
    (gdir / "v1.json").write_text(
        json.dumps({"version": 1, "n": len(golden), "sha256": sha_of(golden), "calls": golden})
    )
    audits = [_status_audit(c, loop="Fatal" if p == ["looping"] else "Pass") for c, _, p in ids]
    files = {
        "calls_path": [call(c) for c, _, _ in ids],
        "ledgers_path": [Ledger(call_id=c, scope="bl_collection", variant="main") for c, _, _ in ids],
        "audits_path": audits,
    }
    paths = {}
    for key, rows in files.items():
        paths[key] = tmp_path / f"{key}.jsonl"
        paths[key].write_text("".join(r.model_dump_json() + "\n" for r in rows))
    patches = patches if patches is not None else [_patch(LOOP, 10, "LOOP"), _patch(QTY, 20, "QTY")]
    gates = gates if gates is not None else [_gate_row(LOOP, "ship"), _gate_row(QTY, "ship")]
    paths["patches_path"] = tmp_path / "patches.json"
    paths["patches_path"].write_text(json.dumps({"patches": patches}))
    paths["gates_path"] = tmp_path / "gates.jsonl"
    paths["gates_path"].write_text("".join(json.dumps(g) + "\n" for g in gates))
    paths["runs_path"] = tmp_path / "out" / "golden_runs.jsonl"
    paths["golden_dir"] = gdir
    pf = tmp_path / "prompt.md"
    pf.write_text("\n".join(_anchor(i) for i in range(1, 31)))
    return pf, paths


class CheckStubs:
    """Stub replay and audit. OLD fails looping on lf calls and qty_probing on qf calls. NEW fixes them unless told not.

    `pass_fatal` is the parameter NEW makes Fatal on the Pass calls, or None. `log` keeps (call id, prompt label, fork).
    """

    def __init__(self, loop_fixed=True, qty_fixed=True, pass_fatal=None, audit_error_on=None):
        self.loop_fixed, self.qty_fixed = loop_fixed, qty_fixed
        self.pass_fatal, self.audit_error_on = pass_fatal, audit_error_on
        self.log, self.prompts = [], set()

    def replay_fn(self, call, ledger, fork, bot_prompt, seed):
        tag = "new" if "PATCHED" in bot_prompt else "old"
        self.log.append((call.call_id, tag, fork))
        self.prompts.add(bot_prompt)
        turns = [*call.turns[:fork], Turn(idx=fork, speaker="bot", start_sec=1, end_sec=2, text="x")]
        res = CallRecord(call_id=f"{call.call_id}__{tag}", duration_sec=60, turns=turns)
        return ReplayResult(call=res, fork_turn=fork, ended_by="end_call")

    def audit_fn(self, call):
        base, tag = call.call_id.split("__")
        if self.audit_error_on == (base, tag):
            raise RuntimeError("ledger extraction failed")
        old = tag == "old"
        status = {"looping": "Pass", "qty_probing": "Pass"}
        if base.startswith("lf") and (old or not self.loop_fixed):
            status["looping"] = "Fatal"
        if base.startswith("qf") and (old or not self.qty_fixed):
            status["qty_probing"] = "Fatal"
        if base.startswith("gp") and not old and self.pass_fatal:
            status[self.pass_fatal] = "Fatal"
        onset = call.turns[-1].idx  # the fault starts at the generated turn, at the fork
        findings = [
            Finding(parameter=p, status=s, reason="s", basis="observed", onset_turn=onset) for p, s in status.items()
        ]
        verdict = "Fatal" if "Fatal" in status.values() else "Pass"
        return AuditRecord(call_id=call.call_id, verdict=verdict, scope="bl_collection", findings=findings)


def raising(*_a, **_k):
    raise AssertionError("an LLM-backed function was called")


def check(tmp_path, stubs=None, patches=None, gates=None, **kw):
    pf, paths = write_check_inputs(tmp_path, patches, gates)
    stubs = stubs or CheckStubs()
    out = run_golden_check("v1", pf, replay_fn=stubs.replay_fn, audit_fn=stubs.audit_fn, **{**paths, **kw})
    return out, paths, stubs


def test_all_shipped_patches_apply_together_and_only_shipped_ones(tmp_path):
    """Every patch whose latest gate row says ship must be in the NEW prompt, and no other patch.

    Testing one patch at a time hides how patches interact, and a rejected patch must never ride along.
    """
    patches = [_patch(LOOP, 10, "LOOP"), _patch(QTY, 20, "QTY"), _patch("cause_x", 25, "X"), _patch("cause_y", 5, "Y")]
    gates = [_gate_row(c, d) for c, d in ((LOOP, "ship"), (QTY, "ship"), ("cause_x", "ship"), ("cause_x", "reject"))]
    _, _, stubs = check(tmp_path, patches=patches, gates=gates)  # cause_y has no gate row at all
    old, new = (next(p for p in stubs.prompts if ("PATCHED" in p) == flag) for flag in (False, True))
    assert "PATCHED" not in old
    assert "PATCHED_LOOP" in new and "PATCHED_QTY" in new
    assert "PATCHED_X" not in new and "PATCHED_Y" not in new  # latest row reject, and no row


def test_a_rerun_of_the_same_candidate_is_refused_before_any_llm_call(tmp_path, monkeypatch):
    """A second real run of the same patch set must exit before any replay or audit, and a changed set must run.

    Each extra look at the golden set inflates false passes, so one candidate gets one look.
    """
    out, paths, _ = check(tmp_path)
    assert out.row["passed"] is True and len(read_rows(paths["runs_path"])) == 1
    monkeypatch.setattr(prove, "real_replay_fn", raising)
    monkeypatch.setattr(prove, "real_audit_fn", raising)
    with pytest.raises(SystemExit, match="already checked"):
        run_golden_check("v1", tmp_path / "prompt.md", **paths)  # default (real) functions would call the LLM
    with pytest.raises(SystemExit, match="already checked"):
        run_golden_check("v1", tmp_path / "prompt.md", replay_fn=raising, audit_fn=raising, **paths)
    assert len(read_rows(paths["runs_path"])) == 1
    changed = json.loads(paths["patches_path"].read_text())
    changed["patches"][0]["new_text"] += " AGAIN"
    paths["patches_path"].write_text(json.dumps(changed))
    again = CheckStubs()
    run_golden_check("v1", tmp_path / "prompt.md", replay_fn=again.replay_fn, audit_fn=again.audit_fn, **paths)
    assert len(read_rows(paths["runs_path"])) == 2 and again.log


def read_rows(path):
    return [json.loads(x) for x in path.read_text().splitlines()]


def test_a_fatal_rise_on_golden_pass_calls_fails_the_check(tmp_path):
    """When NEW makes a non-advisory rule Fatal on golden Pass calls, the check must fail and name the rise.

    A patch that fixes one cause by breaking calls that were fine must never be let through.
    """
    out, paths, _ = check(tmp_path, CheckStubs(pass_fatal="looping"))
    row = out.row
    assert row["passed"] is False and row["fatal"] == {"old": 0, "new": 12}
    assert row["causes"]["cause_looping"]["status"] == "dropped"  # the fault dropped, and the check still fails
    assert "Fatal" in row["reason"]
    assert read_rows(paths["runs_path"])[0]["passed"] is False


def test_a_clean_run_passes_and_a_cause_that_does_not_drop_blocks(tmp_path):
    """A gating cause that drops with no Fatal rise must pass, and one that does not drop must block.

    The pass rule is the whole point of the check: a wrong sign here ships a patch that does nothing.
    """
    ok, _, _ = check(tmp_path)
    assert ok.row["passed"] is True and ok.row["causes"]["cause_looping"] == {
        "status": "dropped", "old_fail": 12, "new_fail": 0, "calls": 12
    }  # fmt: skip
    blocked, _, _ = check(tmp_path / "b", CheckStubs(loop_fixed=False))
    assert blocked.row["passed"] is False and blocked.row["causes"]["cause_looping"]["status"] == "not_dropped"


def test_advisory_rules_neither_block_nor_pass(tmp_path):
    """A cause on an advisory rule must not block when it does not drop, nor count as a pass when it does.

    The audit grades such a rule unlike the owner, so its replays say nothing about the patch.
    """
    # qty_probing is advisory in the fixture. Not fixed for it, fixed for looping: the check still passes.
    stubs = CheckStubs(qty_fixed=False, pass_fatal="qty_probing")  # an advisory Fatal on Pass calls is no rise
    out, _, _ = check(tmp_path / "a", stubs)
    assert out.row["passed"] is True and out.row["causes"]["cause_qty_probing"]["status"] == "advisory"
    assert not [c for c, _, _ in stubs.log if c.startswith("qf")]  # no replay is spent on an advisory fault
    # Only the advisory cause patched: nothing can pass, and the run stops before any LLM call.
    pf, paths = write_check_inputs(tmp_path / "c", [_patch(QTY, 20, "QTY")], [_gate_row(QTY, "ship")])
    with pytest.raises(SystemExit, match="gating"):
        run_golden_check("v1", pf, replay_fn=raising, audit_fn=raising, **paths)
    assert not paths["runs_path"].exists()


def test_failing_calls_fork_at_the_first_fault_and_pass_calls_after_the_greeting(tmp_path):
    """A failing golden call must fork at its human first-fault turn, a Pass call at the bot turn after the first user.

    A fork before the fault would replay behaviour that was fine, and one after it would miss the fault.
    """
    _, _, stubs = check(tmp_path)
    forks = {c.split("__")[0]: f for c, _, f in stubs.log}
    assert {forks[f"lf{i:02d}"] for i in range(12)} == {3}
    assert {forks[f"gp{i:02d}"] for i in range(12)} == {2}
    assert len(stubs.log) == 2 * (12 + 12)  # each replayed call once under OLD and once under NEW


def test_an_unresolved_replay_cannot_pass(tmp_path):
    """A replay whose audit raised must be counted as unresolved, and the check must not pass on the rest.

    Missing evidence is never a pass: a silent skip would let a patch through on fewer calls than claimed.
    """
    out, _, _ = check(tmp_path, CheckStubs(audit_error_on=("lf03", "new")))
    assert out.row["passed"] is False and out.row["unresolved"] == 1


def test_dry_run_prints_the_plan_and_spends_nothing(tmp_path, monkeypatch, capsys):
    """`pipeline golden check --dry-run` must print the call and replay counts and make no call or file.

    The owner needs the cost before the one allowed look at the golden set, and a dry run must never use it up.
    """
    pf, paths = write_check_inputs(tmp_path)
    monkeypatch.setattr(prove, "real_replay_fn", raising)
    monkeypatch.setattr(prove, "real_audit_fn", raising)
    monkeypatch.setattr("vaniloop.pipeline.GOLDEN_CHECK_INPUTS", paths)
    main(["golden", "check", "--version", "v1", "--prompt", str(pf), "--dry-run"])
    out = capsys.readouterr().out
    assert "48 replays" in out and "2 shipped patch" in out and "dry run" in out
    assert not paths["runs_path"].exists()
    main(["golden", "check", "--version", "v1", "--prompt", str(pf), "--dry-run"])  # a dry run is repeatable
