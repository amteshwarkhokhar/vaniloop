"""Tests for the golden set: frozen versions, stratified picks and the per-rule grader gate. Synthetic data only."""

import hashlib
import json
from collections import Counter

import pytest

from vaniloop.golden import (
    BAR_KAPPA,
    BAR_N,
    build_golden,
    cohen_kappa,
    golden_agreement,
    load_golden,
    select_golden,
    sha_of,
)
from vaniloop.models import AuditRecord, Finding
from vaniloop.pipeline import main


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
