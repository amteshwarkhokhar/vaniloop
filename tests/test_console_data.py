"""Tests for the console data endpoints: submit, rubric and tune. rubric and tune are faked, so these tests
pass before those modules exist."""

from __future__ import annotations

import base64
import json
import sys
import types

import pytest
from test_console_jobs import call, data, srv  # noqa: F401 - shared helpers and fixtures

import vaniloop


def b64(raw: bytes = b"synthetic") -> str:
    return base64.b64encode(raw).decode()


def test_submit_stores_good_upload_and_lists_it(srv, data):  # noqa: F811
    """Protects the upload path: a valid mp3 and transcript land in console/submissions/<sid>/inbox, the reply
    names their kinds, and GET /api/submit lists the submission. Without it the submit job has no input."""
    files = [{"name": "callA.mp3", "data_b64": b64()}, {"name": "callB.json", "data_b64": b64(b"{}")}]
    status, out = call(srv, "POST", "/api/submit", {"files": files, "call_ids": ["c1"]})
    assert status == 201 and out["call_ids"] == ["c1"]
    assert out["files"] == [{"name": "callA.mp3", "kind": "mp3"}, {"name": "callB.json", "kind": "transcript"}]
    inbox = data / "console" / "submissions" / out["submission_id"] / "inbox"
    assert sorted(p.name for p in inbox.iterdir()) == ["callA.mp3", "callB.json"]
    row = call(srv, "GET", "/api/submit")[1]["submissions"][0]
    assert (row["submission_id"], row["calls"]) == (out["submission_id"], 3) and row["created_ist"]
    assert call(srv, "GET", f"/api/submit/{out['submission_id']}")[1]["status"] == "pending"


@pytest.mark.parametrize(
    "body",
    [
        {"files": [{"name": "../x.mp3", "data_b64": "eA=="}]},
        {"files": [{"name": "a/b.mp3", "data_b64": "eA=="}]},
        {"files": [{"name": "a.exe", "data_b64": "eA=="}]},
        {"files": [{"name": "a.mp3", "data_b64": "not base64!"}]},
        {"files": [{"name": "a.mp3", "data_b64": ""}]},
        {"files": [{"name": "a.mp3", "data_b64": "eA=="}, {"name": "a.mp3", "data_b64": "eA=="}]},
        {"files": [{"name": f"f{i}.mp3", "data_b64": "eA=="} for i in range(21)]},
        {"call_ids": ["nope"]},
        {"call_ids": [5]},
        {},
        {"files": [], "call_ids": []},
    ],
)
def test_submit_refuses_bad_input(srv, data, body):  # noqa: F811
    """Protects the data folder: path tricks, a bad extension, bad base64, repeats, more than 20 files, unknown
    call ids and an empty request must give 400 and write nothing."""
    status, out = call(srv, "POST", "/api/submit", body)
    assert status == 400 and "error" in out
    assert not (data / "console" / "submissions").exists()


def test_submit_file_size_cap(srv, monkeypatch):  # noqa: F811
    """Protects disk and memory: a decoded file over the per-file cap must be refused with 400."""
    from vaniloop import console

    monkeypatch.setattr(console, "MAX_FILE_BYTES", 4)
    status, _ = call(srv, "POST", "/api/submit", {"files": [{"name": "a.mp3", "data_b64": b64(b"12345")}]})
    assert status == 400


def test_submission_result_reads_audits_and_errors(srv, data):  # noqa: F811
    """Protects the submit result view: per-call verdict, findings, flags and errors must come from the
    submission's audits.jsonl and errors.jsonl, with source upload or existing."""
    _, made = call(
        srv, "POST", "/api/submit", {"files": [{"name": "callA.json", "data_b64": b64()}], "call_ids": ["c1"]}
    )
    base = data / "console" / "submissions" / made["submission_id"]
    audits = [
        {"call_id": "callA", "verdict": "Fatal", "scope": "bot", "flags": ["loops"],
         "findings": [{"parameter": "looping", "status": "Fatal", "reason": "asks twice", "onset_turn": 4,
                                                "basis": "x"}]},
        {"call_id": "c1", "verdict": "Pass", "scope": "none", "findings": []},
    ]  # fmt: skip
    (base / "audits.jsonl").write_text("".join(json.dumps(a) + "\n" for a in audits))
    (base / "errors.jsonl").write_text(json.dumps({"call_id": "c2", "error": "audio unreadable"}) + "\n")
    status, out = call(srv, "GET", f"/api/submit/{made['submission_id']}")
    assert status == 200 and out["status"] == "done"
    by = {c["call_id"]: c for c in out["calls"]}
    assert by["callA"]["source"] == "upload" and by["callA"]["verdict"] == "Fatal" and by["callA"]["flags"] == ["loops"]
    assert by["callA"]["findings"] == [
        {"parameter": "looping", "status": "Fatal", "reason": "asks twice", "onset_turn": 4}
    ]
    assert by["c1"]["source"] == "existing" and by["c2"]["error"] == "audio unreadable"
    assert call(srv, "GET", "/api/submit/s000000000000")[0] == 404
    assert call(srv, "GET", "/api/submit/..%2f..")[0] == 404


class FakeRubricError(Exception):
    def __init__(self, msg, field):
        super().__init__(msg)
        self.field = field


def fake_modules(monkeypatch, *, noraw=False):
    calls: list = []

    class NoRaw(Exception):
        pass

    rubric = types.ModuleType("vaniloop.rubric")
    rubric.RubricError = FakeRubricError

    def save(path, params, questions):
        calls.append(("save", path.name, params, questions))
        if "bad" in params:
            raise FakeRubricError("unknown parameter", "params.bad")

    rubric.save = save
    rubric.describe = lambda path: {"params": [], "from": path.name}
    tune = types.ModuleType("vaniloop.tune")
    tune.NoRaw = NoRaw

    def score(d, thresholds):
        calls.append(("score", thresholds))
        if thresholds.get("frustrated_p") == 9:
            raise ValueError("frustrated_p must be 0..1")
        return {"n": 3}

    tune.score = score
    for name, mod in (("rubric", rubric), ("tune", tune)):
        monkeypatch.setitem(sys.modules, f"vaniloop.{name}", mod)
        monkeypatch.setattr(vaniloop, name, mod, raising=False)
    return calls


def test_rubric_get_and_put_pass_through(srv, monkeypatch):  # noqa: F811
    """Protects the rubric editor: GET returns describe(), PUT saves to <data>/rubric.json and returns the new
    describe, and a RubricError becomes 400 with its field. Without this the UI cannot show why a save failed."""
    calls = fake_modules(monkeypatch)
    assert call(srv, "GET", "/api/rubric") == (200, {"params": [], "from": "rubric.json"})
    status, out = call(srv, "PUT", "/api/rubric", {"params": {"x": 1}, "questions": {"q": None}})
    assert status == 200 and out["from"] == "rubric.json"
    assert calls == [("save", "rubric.json", {"x": 1}, {"q": None})]
    assert call(srv, "PUT", "/api/rubric", {"params": {"bad": 1}}) == (
        400,
        {"error": "unknown parameter", "field": "params.bad"},
    )
    assert call(srv, "PUT", "/api/rubric", {"params": {}}, origin=None)[0] == 403
    assert call(srv, "PUT", "/api/other", {})[0] == 404


def test_tune_passes_through_and_maps_errors(srv, data, monkeypatch):  # noqa: F811
    """Protects the live tuning panel: with raw answers present score() runs and returns 200, a missing
    jev_raw.jsonl or a NoRaw gives 409, and a bad value gives 400."""
    fake_modules(monkeypatch)
    body = {"thresholds": {"frustrated_p": 0.5}}
    status, out = call(srv, "POST", "/api/tune", body)
    assert (status, out["error"]) == (409, "no raw Jev answers: run judge")
    (data / "jev_raw.jsonl").write_text("{}\n")
    assert call(srv, "POST", "/api/tune", body) == (200, {"n": 3})
    status, out = call(srv, "POST", "/api/tune", {"thresholds": {"frustrated_p": 9}})
    assert status == 400 and "frustrated_p" in out["error"]
    assert call(srv, "POST", "/api/tune", {"thresholds": 5})[0] == 400
    import vaniloop.tune as tune

    monkeypatch.setattr(tune, "score", lambda d, t: (_ for _ in ()).throw(tune.NoRaw()))
    assert call(srv, "POST", "/api/tune", body)[0] == 409
