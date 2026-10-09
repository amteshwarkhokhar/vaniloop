"""Cost meter and budget cap for complete_json. Synthetic stubs only, no network."""

from __future__ import annotations

import hashlib
import json
import sys
import time
import types
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from vaniloop import llm


class FakeClient:
    """OpenAI stand-in that exposes both create() and with_raw_response.create()."""

    outputs: list[str] = []
    headers: dict[str, str] = {}
    tokens: tuple[int, int] = (10, 5)
    delay: float = 0.0
    calls: list[dict] = []

    def __init__(self, **kw):
        self.chat = types.SimpleNamespace(completions=_Completions())


def _make_response(request: dict) -> types.SimpleNamespace:
    FakeClient.calls.append(request)
    if FakeClient.delay:
        time.sleep(FakeClient.delay)
    content = FakeClient.outputs.pop(0) if FakeClient.outputs else '{"ok": true}'
    usage = types.SimpleNamespace(prompt_tokens=FakeClient.tokens[0], completion_tokens=FakeClient.tokens[1])
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content))],
        usage=usage,
    )


class _Completions:
    def create(self, **kw):
        return _make_response(kw)

    @property
    def with_raw_response(self):
        return _RawCompletions()


class _RawCompletions:
    def create(self, **kw):
        return types.SimpleNamespace(headers=dict(FakeClient.headers), parse=lambda: _make_response(kw))


@pytest.fixture
def cost_env(tmp_path, monkeypatch):
    """Point the cache and costs file at tmp, stub OpenAI and clear the module spend state."""
    monkeypatch.setattr(llm, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(llm, "COSTS_PATH", tmp_path / "costs.jsonl")
    monkeypatch.setenv("LLM_BASE_URL", "http://x")
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.delenv("LLM_BUDGET_USD", raising=False)
    monkeypatch.delenv("VANILOOP_CACHE", raising=False)
    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=FakeClient))
    llm._spent, llm._in_flight = None, 0
    FakeClient.outputs, FakeClient.headers, FakeClient.tokens = [], {}, (10, 5)
    FakeClient.delay, FakeClient.calls = 0.0, []
    yield tmp_path
    llm._spent, llm._in_flight = None, 0


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
    assert llm._in_flight == 0


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


def test_requests_really_overlap_across_threads(cost_env):
    """Four slow calls in parallel must finish well under the serial time.

    Holding the budget lock during the request would serialize the audit and stall runs.
    """
    FakeClient.delay = 0.2
    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(lambda i: llm.complete_json("s", f"u{i}", model="m", cache_tag=f"t{i}"), range(4)))
    assert time.monotonic() - start < 0.8


def test_default_cache_key_unchanged(cost_env):
    """The default cache key formula must equal the original so old caches stay valid.

    Changing it would invalidate every cached label and force paid reruns of the audit.
    """
    msgs = [{"role": "user", "content": "x"}]
    old = hashlib.sha256(
        json.dumps({"m": "m", "u": "b", "t": "t", "msgs": msgs}, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:32]
    assert llm._key("m", msgs, "b", "t") == old
