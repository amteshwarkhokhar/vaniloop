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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = ROOT / ".cache" / "llm"


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


def _key(model: str, messages: list[dict], base_url: str = "", cache_tag: str = "") -> str:
    raw = json.dumps({"m": model, "u": base_url, "t": cache_tag, "msgs": messages}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _is_dict(x) -> bool:
    return isinstance(x, dict)


def _valid(data, validate) -> bool:
    try:
        return bool(_is_dict(data) and (validate is None or validate(data)))
    except Exception:
        return False


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


def complete_json(system: str, user: str, model: str | None = None, retries: int = 2,
                  validate=None, cache_tag: str = "") -> dict:
    """Return a parsed JSON object. Retries on unparseable or invalid output; only valid output is cached."""
    model = model or model_name()
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    base = os.environ.get("LLM_BASE_URL")
    key = _key(model, messages, base or "", cache_tag)
    hit = cached_json(key, validate)
    if hit is not None:
        return hit

    from openai import OpenAI

    api_key = os.environ.get("LLM_API_KEY")
    if not base or not api_key:
        raise RuntimeError("LLM_BASE_URL and LLM_API_KEY must be set in .env (no cached answer for this request)")
    client = OpenAI(base_url=base, api_key=api_key)
    last_err: Exception | None = None
    for _ in range(retries + 1):
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=0,
            response_format={"type": "json_object"},
        )
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
