"""Shared test setup."""

import pytest

from vaniloop import llm


@pytest.fixture(autouse=True)
def _isolated_cost_log(tmp_path, monkeypatch):
    """Point the LLM cost log at a temp file and reset the budget state for every test.

    Without this, any test that stubs the LLM client appends rows to the real runs/costs.jsonl.
    """
    monkeypatch.setattr(llm, "COSTS_PATH", tmp_path / "costs.jsonl")
    monkeypatch.setattr(llm, "_ledger_broken", False)
    monkeypatch.setattr(llm, "_ledger_broken_reason", "")
    monkeypatch.delenv("LLM_RESERVE_USD", raising=False)
