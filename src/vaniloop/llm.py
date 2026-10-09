"""OpenAI-compatible JSON client with a disk record/replay cache.

Default route: the IndiaMART LiteLLM proxy (Gemini 3 Flash). Configure in .env:
  LLM_BASE_URL=https://litellm.indiamart.com
  LLM_API_KEY=...
  LLM_MODEL=google/gemini-3-flash-preview
Cache: same (model, messages) -> same answer, so reruns and demos cost nothing.
Set VANILOOP_CACHE=refresh to bypass reads.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path

from .jsonl import append_jsonl, read_jsonl

ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = ROOT / ".cache" / "llm"
COSTS_PATH = ROOT / "runs" / "costs.jsonl"

LOCK = threading.Lock()
_spent: float | None = None  # USD recorded so far, loaded once from COSTS_PATH before a budget check
_in_flight = 0  # real provider requests started but not yet settled


class BudgetExceeded(RuntimeError):
    """Recorded spend reached LLM_BUDGET_USD, so the next provider request is refused."""


def _load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


_load_env()


def model_name() -> str:
    return os.environ.get("LLM_MODEL", "google/gemini-3-flash-preview")


def _key(
    model: str,
    messages: list[dict],
    base_url: str = "",
    cache_tag: str = "",
    temperature: float = 0,
    seed: int | None = None,
) -> str:
    body = {"m": model, "u": base_url, "t": cache_tag, "msgs": messages}
    if temperature != 0:  # added only when non-default, so keys of older calls stay identical
        body["temp"] = temperature
    if seed is not None:
        body["seed"] = seed
    raw = json.dumps(body, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _is_dict(x) -> bool:
    return isinstance(x, dict)


def _valid(data, validate) -> bool:
    try:
        return bool(_is_dict(data) and (validate is None or validate(data)))
    except Exception:
        return False


def _budget() -> float | None:
    """Return LLM_BUDGET_USD as a float, or None when the setting is absent or blank."""
    raw = os.environ.get("LLM_BUDGET_USD", "").strip()
    return float(raw) if raw else None


def _load_spent() -> float:
    """Sum cost_usd over COSTS_PATH. Missing, null or non-numeric costs count as zero."""
    total = 0.0
    for row in read_jsonl(COSTS_PATH):
        cost = row.get("cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            total += float(cost)
    return total


def _claim(budget: float | None) -> None:
    """Reserve one slot for a real provider request, raising BudgetExceeded at the cap.

    The lock is held only for the check. Overspend is bounded by the calls already in
    flight when the cap is reached, because those calls settled after it and their cost
    is charged once they return.
    """
    global _spent, _in_flight
    with LOCK:
        if budget is not None:
            if _spent is None:
                _spent = _load_spent()
            if _spent >= budget:
                raise BudgetExceeded(
                    f"LLM budget reached: spent ${_spent:.4f} of ${budget:.4f}; delete or rename {COSTS_PATH} to reset"
                )
        _in_flight += 1


def _header_cost(headers) -> float | None:
    """Read the LiteLLM USD cost header. A missing or malformed value is null, never an error."""
    raw = headers.get("x-litellm-response-cost") if headers else None
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _create(client, request: dict):
    """One provider call, returning (parsed response, headers). Uses the raw SDK path when present."""
    completions = client.chat.completions
    raw_api = getattr(completions, "with_raw_response", None)
    if raw_api is None:  # test stand-ins that only expose create()
        return completions.create(**request), {}
    raw = raw_api.create(**request)
    return raw.parse(), raw.headers


def _settle(model: str, cache_tag: str, resp, cost: float | None) -> None:
    """Append one cost row for a finished provider request and add its cost to the in-memory spend."""
    global _spent, _in_flight
    usage = getattr(resp, "usage", None)
    row = {
        "ts": datetime.now(UTC).isoformat(),
        "model": model,
        "cache_tag": cache_tag,
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "cost_usd": cost,
    }
    with LOCK:
        COSTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        append_jsonl(COSTS_PATH, [row])
        if _spent is not None and cost:
            _spent += cost
        _in_flight -= 1


def cached_json(key: str, validate=None) -> dict | None:
    """Return the cached object, or None on miss, corrupt file or failed validation."""
    p = CACHE_DIR / f"{key}.json"
    if os.environ.get("VANILOOP_CACHE") == "refresh":
        return None
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    return data if _valid(data, validate) else None


def _write_cache(key: str, data: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    final = CACHE_DIR / f"{key}.json"
    tmp = CACHE_DIR / f"{key}.{os.getpid()}.{threading.get_ident()}.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    os.replace(tmp, final)


def complete_json(
    system: str,
    user: str,
    model: str | None = None,
    retries: int = 2,
    validate=None,
    cache_tag: str = "",
    temperature: float = 0,
    seed: int | None = None,
    max_retries: int | None = None,
) -> dict:
    """Return a parsed JSON object. Retries on unparseable or invalid output; only valid output is cached."""
    model = model or model_name()
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    base = os.environ.get("LLM_BASE_URL")
    key = _key(model, messages, base or "", cache_tag, temperature, seed)
    hit = cached_json(key, validate)
    if hit is not None:
        return hit

    from openai import OpenAI

    api_key = os.environ.get("LLM_API_KEY")
    if not base or not api_key:
        raise RuntimeError("LLM_BASE_URL and LLM_API_KEY must be set in .env (no cached answer for this request)")
    client_kw = {} if max_retries is None else {"max_retries": max_retries}
    client = OpenAI(base_url=base, api_key=api_key, **client_kw)
    extra = {} if seed is None else {"seed": seed}
    request = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "response_format": {"type": "json_object"},
        **extra,
    }
    budget = _budget()
    last_err: Exception | None = None
    for _ in range(retries + 1):
        _claim(budget)
        resp = None
        headers: dict = {}
        try:
            resp, headers = _create(client, request)
        finally:
            _settle(model, cache_tag, resp, _header_cost(headers))
        text = resp.choices[0].message.content or ""
        try:
            data = json.loads(text.strip().removeprefix("```json").removesuffix("```").strip())
        except json.JSONDecodeError as e:
            last_err = e
            continue
        if not _valid(data, validate):
            last_err = ValueError("model output failed validation")
            continue
        _write_cache(key, data)
        return data
    raise RuntimeError(f"model returned unparseable or invalid JSON after {retries + 1} tries: {last_err}")
