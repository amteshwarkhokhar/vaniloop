"""Cost meter and budget cap for complete_json. Synthetic stubs only, no network."""

from __future__ import annotations

import hashlib
import json
import multiprocessing
import sys
import threading
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from vaniloop import llm


class FakeAPIError(Exception):
    """OpenAI SDK error stand-in that carries a response with headers, like APIStatusError."""

    def __init__(self, message: str = "provider error", headers: dict | None = None):
        super().__init__(message)
        self.response = types.SimpleNamespace(headers=dict(headers or {}))


class FakeClient:
    """OpenAI stand-in that exposes both create() and with_raw_response.create()."""

    outputs: list[str] = []
    headers: dict[str, str] = {}
    tokens: tuple[int, int] = (10, 5)
    calls: list[dict] = []
    errors: list[Exception | None] = []
    parse_errors: list[Exception | None] = []
    barrier: threading.Barrier | None = None
    instances: list = []

    def __init__(self, **kw):
        self.init_kwargs = kw
        self.max_retries = kw.get("max_retries")
        FakeClient.instances.append(self)
        self.chat = types.SimpleNamespace(completions=_Completions())


def _raise_next(bucket: list) -> None:
    if bucket:
        err = bucket.pop(0)
        if err is not None:
            raise err


def _make_response(request: dict) -> types.SimpleNamespace:
    FakeClient.calls.append(request)
    if FakeClient.barrier is not None:
        FakeClient.barrier.wait(timeout=10)
    _raise_next(FakeClient.parse_errors)
    content = FakeClient.outputs.pop(0) if FakeClient.outputs else '{"ok": true}'
    usage = types.SimpleNamespace(prompt_tokens=FakeClient.tokens[0], completion_tokens=FakeClient.tokens[1])
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content))],
        usage=usage,
    )


class _Completions:
    def create(self, **kw):
        _raise_next(FakeClient.errors)
        return _make_response(kw)

    @property
    def with_raw_response(self):
        return _RawCompletions()


class _RawCompletions:
    def create(self, **kw):
        _raise_next(FakeClient.errors)
        return types.SimpleNamespace(headers=dict(FakeClient.headers), parse=lambda: _make_response(kw))


class RetryRecordingClient:
    """OpenAI stand-in whose with_options records the options metered_create asked it for."""

    def __init__(self):
        self.options: list[dict] = []
        self.chat = types.SimpleNamespace(completions=_Completions())

    def with_options(self, **kwargs):
        self.options.append(kwargs)
        return self


@pytest.fixture
def cost_env(tmp_path, monkeypatch):
    """Point the cache and costs file at tmp, stub OpenAI and clear the module ledger state."""
    monkeypatch.setattr(llm, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(llm, "COSTS_PATH", tmp_path / "costs.jsonl")
    monkeypatch.setenv("LLM_BASE_URL", "http://x")
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.delenv("LLM_BUDGET_USD", raising=False)
    monkeypatch.delenv("VANILOOP_CACHE", raising=False)
    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=FakeClient))
    monkeypatch.setattr(llm, "_ledger_broken", False)
    monkeypatch.setattr(llm, "_ledger_broken_reason", "")
    FakeClient.outputs, FakeClient.headers, FakeClient.tokens = [], {}, (10, 5)
    FakeClient.calls, FakeClient.errors, FakeClient.parse_errors = [], [], []
    FakeClient.barrier, FakeClient.instances = None, []
    yield tmp_path
    FakeClient.barrier = None


def rows(path: Path) -> list[dict]:
    """Parse the JSONL cost file into a list of dicts for assertions."""
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_real_call_writes_one_cost_row_with_header_cost(cost_env):
    """A real provider call must append exactly one cost row carrying the header cost.

    A missing row or wrong fields would let the meter undercount spend and miss the cap.
    """
    FakeClient.headers = {"x-litellm-response-cost": "0.0123"}
    FakeClient.outputs = ['{"ok": true}']
    llm.complete_json("s", "u", model="m", cache_tag="t")
    r = rows(cost_env / "costs.jsonl")
    assert len(r) == 1
    assert r[0]["cost_usd"] == pytest.approx(0.0123)
    assert r[0]["model"] == "m" and r[0]["cache_tag"] == "t"
    assert r[0]["prompt_tokens"] == 10 and r[0]["completion_tokens"] == 5
    assert r[0]["status"] == "ok" and r[0]["error"] is None
    assert r[0]["ts"].endswith("+00:00")


def test_cache_hit_writes_no_cost_row(cost_env):
    """A cache hit must serve the answer without a provider call and without a cost row.

    Counting cache hits as spend would make reruns and demos look expensive.
    """
    FakeClient.headers = {"x-litellm-response-cost": "0.01"}
    FakeClient.outputs = ['{"ok": true}']
    llm.complete_json("s", "u", model="m", cache_tag="t")
    assert llm.complete_json("s", "u", model="m", cache_tag="t") == {"ok": True}
    assert len(FakeClient.calls) == 1
    assert len(rows(cost_env / "costs.jsonl")) == 1


def test_missing_cost_header_records_null_cost_row(cost_env):
    """A response without the LiteLLM cost header must still record a row with a null cost.

    Dropping the row would hide that a call happened and break token accounting.
    """
    FakeClient.headers = {}
    FakeClient.outputs = ['{"ok": true}']
    llm.complete_json("s", "u", model="m", cache_tag="t")
    r = rows(cost_env / "costs.jsonl")
    assert len(r) == 1 and r[0]["cost_usd"] is None and r[0]["prompt_tokens"] == 10


def test_budget_reached_raises_before_provider_call(cost_env, monkeypatch):
    """Spend at the cap must raise BudgetExceeded and never reach the provider stub.

    Without the pre-flight check a run could spend far past the configured cap.
    """
    (cost_env / "costs.jsonl").write_text(json.dumps({"cost_usd": 5.0}) + "\n")
    monkeypatch.setenv("LLM_BUDGET_USD", "5")
    with pytest.raises(llm.BudgetExceeded) as err:
        llm.complete_json("s", "u", model="m", cache_tag="t")
    assert FakeClient.calls == []
    assert "5.0000" in str(err.value) and str(cost_env / "costs.jsonl") in str(err.value)


def test_eight_threads_write_eight_valid_json_rows(cost_env):
    """Eight parallel real calls must leave eight valid JSON rows and no interleaved line.

    A torn or missing line would corrupt the cost meter and later budget loads.
    """

    def work(i: int):
        return llm.complete_json("s", f"u{i}", model="m", cache_tag=f"t{i}")

    FakeClient.headers = {"x-litellm-response-cost": "0.001"}
    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(work, range(8)))
    r = rows(cost_env / "costs.jsonl")
    assert len(r) == 8
    assert {x["cache_tag"] for x in r} == {f"t{i}" for i in range(8)}
    assert all(x["cost_usd"] == pytest.approx(0.001) for x in r)


def _mp_settle_worker(costs_path: str, budget: str, queue) -> None:
    """Child process: settle a cap-sized cost, then prove the shared ledger refuses the next admission."""
    from vaniloop import llm as child_llm

    child_llm.COSTS_PATH = Path(costs_path)
    child_llm._ledger_broken = False
    child_llm._ledger_broken_reason = ""
    outcomes = []
    reserve_id = None
    try:
        reserve_id = child_llm._admit(float(budget))
        outcomes.append("admitted")
    except child_llm.BudgetExceeded:
        outcomes.append("refused")
    child_llm._settle("m", "t", None, float(budget), "ok", None, reserve_id=reserve_id)
    try:
        child_llm._admit(float(budget))
        outcomes.append("admitted")
    except child_llm.BudgetExceeded:
        outcomes.append("refused")
    queue.put(outcomes)


def _mp_reserve_worker(costs_path: str, budget: str, queue) -> None:
    """Child process: attempt one reservation and admission against the shared ledger, then report."""
    from vaniloop import llm as child_llm

    child_llm.COSTS_PATH = Path(costs_path)
    child_llm._ledger_broken = False
    child_llm._ledger_broken_reason = ""
    try:
        queue.put(("admitted", child_llm._admit(float(budget))))
    except child_llm.BudgetExceeded:
        queue.put(("refused", None))


def test_cross_process_ledger_lock_refuses_after_cap(cost_env):
    """Regression for review round 1 finding 1: two processes sharing one ledger must refuse after the cap.

    In-memory spend state would let each process pass the cap and overspend the budget.
    """
    ledger = cost_env / "costs.jsonl"
    ledger.write_text("")
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    procs = [ctx.Process(target=_mp_settle_worker, args=(str(ledger), "1.0", queue)) for _ in range(2)]
    for p in procs:
        p.start()
    outcomes = [queue.get(timeout=60) for _ in procs]
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0
    assert all(second == "refused" for _, second in outcomes), outcomes
    # both settle rows survived and no line was torn; the two reserve rows closed them
    assert len([x for x in rows(ledger) if x.get("kind") != "reserve"]) == 2
    with pytest.raises(llm.BudgetExceeded):
        llm._admit(1.0)


def test_concurrent_processes_admit_only_one_reservation(cost_env):
    """Regression for review round 2 finding 1: a budget that fits one reservation admits exactly one process.

    Reading spend without reserving lets every process pass the cap before any settles, so the run overspends.
    """
    ledger = cost_env / "costs.jsonl"
    ledger.write_text("")
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    procs = [ctx.Process(target=_mp_reserve_worker, args=(str(ledger), "0.08", queue)) for _ in range(2)]
    for p in procs:
        p.start()
    outcomes = [queue.get(timeout=60) for _ in procs]
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0
    admitted = [o for o in outcomes if o[0] == "admitted"]
    assert len(admitted) == 1, outcomes
    r = rows(ledger)
    assert len(r) == 1 and r[0]["kind"] == "reserve" and r[0]["id"] == admitted[0][1]


def test_broken_marker_blocks_admission_after_globals_reset(cost_env, monkeypatch):
    """Regression for review round 2 finding 2: the broken-ledger marker must block a fresh process state.

    A process-local flag alone would let another process keep spending after a cost row was lost.
    """

    def boom(path, items):
        raise OSError("disk full")

    monkeypatch.setattr(llm, "append_jsonl", boom)
    FakeClient.outputs = ['{"ok": true}']
    assert llm.complete_json("s", "u", model="m", cache_tag="t") == {"ok": True}
    marker = llm._marker_path()
    assert marker.exists() and marker.name == "costs.jsonl.broken"
    # Simulate a process that never saw the failed append: only the marker file can refuse it.
    llm._ledger_broken = False
    llm._ledger_broken_reason = ""
    with pytest.raises(llm.BudgetExceeded, match="cost ledger write failed earlier"):
        llm._admit(None)


def test_sdk_retries_are_metered_per_attempt(cost_env):
    """Regression for review round 1 finding 2: each retry attempt must admit and settle its own row.

    Internal SDK retries would spend real money without a row, so the meter would undercount.
    """
    FakeClient.errors = [FakeAPIError("boom1"), FakeAPIError("boom2"), None]
    FakeClient.outputs = ['{"ok": true}']
    assert llm.complete_json("s", "u", model="m", cache_tag="t") == {"ok": True}
    r = rows(cost_env / "costs.jsonl")
    assert [x["status"] for x in r] == ["error", "error", "ok"]
    assert [x["cost_usd"] for x in r] == [None, None, None]
    assert [x["error"] for x in r][:2] == ["FakeAPIError: boom1", "FakeAPIError: boom2"]
    assert r[2]["error"] is None
    assert len(FakeClient.calls) == 1  # only the successful attempt reached the stub
    assert FakeClient.instances[-1].max_retries == 0  # SDK transport retries are off


def test_parse_failure_still_records_header_cost(cost_env):
    """Regression for review round 1 finding 3: a parse failure must still settle the header cost.

    Losing headers when parse() raises would drop the cost of a paid provider response.
    """
    FakeClient.headers = {"x-litellm-response-cost": "0.004"}
    FakeClient.parse_errors = [ValueError("bad payload"), None]
    FakeClient.outputs = ['{"ok": true}']
    assert llm.complete_json("s", "u", model="m", cache_tag="t", retries=1) == {"ok": True}
    r = rows(cost_env / "costs.jsonl")
    assert len(r) == 2
    assert r[0]["cost_usd"] == pytest.approx(0.004) and r[0]["status"] == "error"
    assert "bad payload" in r[0]["error"]
    assert r[1]["status"] == "ok" and r[1]["cost_usd"] == pytest.approx(0.004)


def test_sdk_exception_response_headers_recorded(cost_env):
    """Regression for review round 1 finding 3: headers on an SDK exception response must record its cost.

    A raised APIStatusError still paid for the call, so ignoring its headers would lose that cost.
    """
    FakeClient.errors = [FakeAPIError("status", {"x-litellm-response-cost": "0.006"}), None]
    FakeClient.outputs = ['{"ok": true}']
    assert llm.complete_json("s", "u", model="m", cache_tag="t", retries=1) == {"ok": True}
    r = rows(cost_env / "costs.jsonl")
    assert r[0]["cost_usd"] == pytest.approx(0.006)
    assert r[0]["status"] == "error" and "FakeAPIError" in r[0]["error"]


def test_unwritable_ledger_blocks_later_admission(cost_env, monkeypatch):
    """Regression for review round 1 finding 4: a failed append must not mask the result but block later calls.

    Silently losing rows would let spend run unbounded with no record of it.
    """

    def boom(path, items):
        raise OSError("disk full")

    monkeypatch.setattr(llm, "append_jsonl", boom)
    FakeClient.outputs = ['{"ok": true}']
    assert llm.complete_json("s", "u", model="m", cache_tag="t") == {"ok": True}
    assert llm._ledger_broken is True
    with pytest.raises(llm.BudgetExceeded, match="cost ledger not writable"):
        llm.complete_json("s", "u2", model="m", cache_tag="t2")


def test_unwritable_ledger_does_not_mask_provider_error(cost_env, monkeypatch):
    """Regression for review round 1 finding 4: a failed append must not replace the provider error.

    Masking the provider error with the disk error would hide the real cause of the failed call.
    """

    def boom(path, items):
        raise OSError("disk full")

    monkeypatch.setattr(llm, "append_jsonl", boom)
    FakeClient.errors = [RuntimeError("provider down")]
    with pytest.raises(RuntimeError) as err:
        llm.complete_json("s", "u", model="m", cache_tag="t", retries=0)
    assert "disk full" not in str(err.value)
    assert "provider down" in str(err.value)
    assert llm._ledger_broken is True


@pytest.mark.parametrize("value", ["NaN", "inf", "-1", "abc"])
def test_invalid_budget_raises_value_error(cost_env, monkeypatch, value):
    """Regression for review round 1 finding 5: a budget that is not finite and >= 0 must raise ValueError.

    A NaN or negative cap would silently disable the budget and let spend run unbounded.
    """
    monkeypatch.setenv("LLM_BUDGET_USD", value)
    with pytest.raises(ValueError, match="LLM_BUDGET_USD"):
        llm.complete_json("s", "u", model="m", cache_tag="t")
    assert FakeClient.calls == []


@pytest.mark.parametrize("raw", ["NaN", "inf", "-1", "abc"])
def test_invalid_header_cost_records_null(cost_env, raw):
    """Regression for review round 1 finding 5: a non-finite or negative header cost must be recorded as null.

    Counting NaN or a negative value as spend would corrupt the budget sum and the ledger forever.
    """
    FakeClient.headers = {"x-litellm-response-cost": raw}
    FakeClient.outputs = ['{"ok": true}']
    llm.complete_json("s", "u", model="m", cache_tag="t")
    r = rows(cost_env / "costs.jsonl")
    assert len(r) == 1 and r[0]["cost_usd"] is None


def test_invalid_ledger_costs_count_as_zero(cost_env):
    """Regression for review round 1 finding 5: invalid ledger costs must count zero in the budget sum.

    A NaN row would poison the sum and a negative row would cancel out real spend.
    """
    (cost_env / "costs.jsonl").write_text(
        json.dumps({"cost_usd": float("nan")})
        + "\n"
        + json.dumps({"cost_usd": float("inf")})
        + "\n"
        + json.dumps({"cost_usd": -3.0})
        + "\n"
        + json.dumps({"cost_usd": "x"})
        + "\n"
        + json.dumps({"cost_usd": 2.0})
        + "\n"
    )
    assert llm._load_spent() == pytest.approx(2.0)


def test_metered_create_accepts_audio_messages(cost_env):
    """Regression for review round 1 finding 6: metered_create must accept arbitrary message shapes.

    A JSON-only request builder would stop the transcribe stage from using the same cost meter.
    """
    FakeClient.headers = {"x-litellm-response-cost": "0.002"}
    FakeClient.outputs = ['{"ok": true}']
    request = {
        "model": "m",
        "messages": [
            {
                "role": "user",
                "content": [{"type": "input_audio", "input_audio": {"data": "AAAA", "format": "wav"}}],
            }
        ],
    }
    resp = llm.metered_create(FakeClient(), request, cache_tag="audio")
    assert resp.choices[0].message.content == '{"ok": true}'
    r = rows(cost_env / "costs.jsonl")
    assert len(r) == 1 and r[0]["cache_tag"] == "audio" and r[0]["cost_usd"] == pytest.approx(0.002)


def test_metered_create_disables_sdk_transport_retries(cost_env):
    """Regression for review round 2 finding 3: metered_create must ask the client for max_retries=0.

    SDK transport retries would pay for provider attempts the meter never admitted or recorded.
    """
    FakeClient.headers = {"x-litellm-response-cost": "0.001"}
    FakeClient.outputs = ['{"ok": true}']
    client = RetryRecordingClient()
    resp = llm.metered_create(client, {"model": "m", "messages": []}, cache_tag="t")
    assert resp.choices[0].message.content == '{"ok": true}'
    assert client.options == [{"max_retries": 0}]
    r = rows(cost_env / "costs.jsonl")
    assert len(r) == 1 and r[0]["cost_usd"] == pytest.approx(0.001)


def test_threads_reach_provider_concurrently(cost_env, monkeypatch):
    """Regression for review round 1 finding 7: four threads with a budget must reach the stub together.

    Holding the budget lock through the request would serialise calls and stall the audit.
    """
    monkeypatch.setenv("LLM_BUDGET_USD", "100")
    FakeClient.barrier = threading.Barrier(4, timeout=10)
    FakeClient.outputs = ['{"ok": true}'] * 4
    FakeClient.headers = {"x-litellm-response-cost": "0.001"}

    def work(i: int):
        return llm.complete_json("s", f"u{i}", model="m", cache_tag=f"t{i}")

    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(work, range(4)))
    assert len([x for x in rows(cost_env / "costs.jsonl") if x.get("kind") != "reserve"]) == 4


def test_default_cache_key_unchanged(cost_env):
    """The default cache key formula must equal the original so old caches stay valid.

    Changing it would invalidate every cached label and force paid reruns of the audit.
    """
    msgs = [{"role": "user", "content": "x"}]
    old = hashlib.sha256(
        json.dumps({"m": "m", "u": "b", "t": "t", "msgs": msgs}, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:32]
    assert llm._key("m", msgs, "b", "t") == old
