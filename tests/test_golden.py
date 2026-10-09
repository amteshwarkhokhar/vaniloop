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
