"""OpenAI-compatible JSON client with a disk record/replay cache.

Default route: the IndiaMART LiteLLM proxy (Gemini 3 Flash). Configure in .env:
  LLM_BASE_URL=https://litellm.indiamart.com
  LLM_API_KEY=...
  LLM_MODEL=google/gemini-3-flash-preview
Cache: same (model, messages) -> same answer, so reruns and demos cost nothing.
Set VANILOOP_CACHE=refresh to bypass reads.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .jsonl import append_jsonl, read_jsonl

ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = ROOT / ".cache" / "llm"
COSTS_PATH = ROOT / "runs" / "costs.jsonl"

RESERVE_USD = 0.05  # default per-request reservation; override with LLM_RESERVE_USD

LOCK = threading.Lock()  # serialises ledger access between threads in this process
_ledger_broken = False  # set when a cost row could not be written; blocks every later admission
_ledger_broken_reason = ""


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
    """Return LLM_BUDGET_USD as a finite float >= 0, or None when the setting is absent or blank."""
    raw = os.environ.get("LLM_BUDGET_USD", "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"LLM_BUDGET_USD must be a finite number >= 0, got {raw!r}") from None
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"LLM_BUDGET_USD must be a finite number >= 0, got {raw!r}")
    return value


def _reserve_usd() -> float:
    """Per-request reservation amount: LLM_RESERVE_USD when set, else RESERVE_USD. Must be finite and > 0.

    Zero would admit unlimited concurrent requests at the cap, so it is refused.
    """
    raw = os.environ.get("LLM_RESERVE_USD", "").strip()
    if not raw:
        return RESERVE_USD
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"LLM_RESERVE_USD must be a finite number > 0, got {raw!r}") from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"LLM_RESERVE_USD must be a finite number > 0, got {raw!r}")
    return value


def _load_spent() -> float:
    """Sum valid cost_usd over COSTS_PATH. Missing, null, non-finite or negative costs count zero."""
    total = 0.0
    for row in read_jsonl(COSTS_PATH):
        cost = row.get("cost_usd")
        if isinstance(cost, bool) or not isinstance(cost, (int, float)):
            continue
        value = float(cost)
        if math.isfinite(value) and value >= 0:
            total += value
    return total


def _open_reservation_usd() -> float:
    """Sum usd of reserve rows not yet closed by a settle row that carries their id.

    An open reservation is budget already promised to an in-flight request in some process,
    so the cap must count it until that request settles.
    """
    rows = read_jsonl(COSTS_PATH)
    closed = {row.get("reserve_id") for row in rows if isinstance(row.get("reserve_id"), str)}
    total = 0.0
    for row in rows:
        if row.get("kind") != "reserve" or not isinstance(row.get("id"), str) or row["id"] in closed:
            continue
        usd = row.get("usd")
        if isinstance(usd, bool) or not isinstance(usd, (int, float)):
            continue
        value = float(usd)
        if math.isfinite(value) and value >= 0:
            total += value
    return total


def _marker_path() -> Path:
    """Path of the broken-ledger marker beside COSTS_PATH (costs.jsonl -> costs.jsonl.broken)."""
    return COSTS_PATH.with_name(COSTS_PATH.name + ".broken")


def _mark_broken(exc: Exception) -> None:
    """Record a ledger write failure in memory and leave a marker file other processes can see."""
    global _ledger_broken, _ledger_broken_reason
    _ledger_broken = True
    _ledger_broken_reason = f"{type(exc).__name__}: {exc}"
    try:
        _marker_path().write_text(_ledger_broken_reason, encoding="utf-8")
    except Exception:
        pass  # the in-memory flag still blocks this process


@contextmanager
def _ledger_lock():
    """Hold the thread lock and an interprocess flock for one ledger read or append.

    The flock is the cross-process guard: every process that shares one COSTS_PATH
    serialises its reads and appends on COSTS_PATH.with_suffix(".lock").
    """
    COSTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOCK:
        with open(COSTS_PATH.with_suffix(".lock"), "a") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _admit(budget: float | None) -> str | None:
    """Reserve budget in the shared ledger, or refuse when the cap is reached.

    Under the lock the check is settled spend + open reservations + this reservation. If it fits,
    a reserve row is appended and its id returned; _settle closes it with a matching reserve_id.
    The lock is released before the provider call, so requests still run concurrently. A marker
    left by an earlier failed write blocks admission in every process that shares this ledger.

    Known limit: a reservation is an estimate, not a hard bound. One request can cost more than
    RESERVE_USD (ours cost about $0.003-0.004 against $0.05), so spend past the cap is at most the
    excess of in-flight requests over their reservations. A crash between admit and settle leaves
    its reservation open; that budget stays blocked until the ledger is reset, which errs safe.
    """
    if _ledger_broken:
        raise BudgetExceeded(f"cost ledger not writable: {_ledger_broken_reason}")
    with _ledger_lock():
        marker = _marker_path()
        if marker.exists():
            raise BudgetExceeded(f"cost ledger write failed earlier; inspect and delete {marker}")
        if budget is None:
            return None
        reserve = _reserve_usd()
        spent = _load_spent()
        reserved = _open_reservation_usd()
        if spent + reserved + reserve > budget:
            raise BudgetExceeded(
                f"LLM budget reached: spent ${spent:.4f} + reserved ${reserved:.4f} of ${budget:.4f}; "
                f"delete or rename {COSTS_PATH} to reset"
            )
        reserve_id = uuid.uuid4().hex
        try:
            append_jsonl(COSTS_PATH, [{"kind": "reserve", "id": reserve_id, "usd": reserve}])
        except Exception as exc:
            _mark_broken(exc)
            raise BudgetExceeded(f"cost ledger not writable: {_ledger_broken_reason}") from exc
        return reserve_id


def _header_cost(headers) -> float | None:
    """Read the LiteLLM USD cost header. Missing, malformed, non-finite or negative values are null."""
    raw = headers.get("x-litellm-response-cost") if headers else None
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value


# OpenRouter sends no LiteLLM cost header. Its list price per token in USD (openrouter.ai/api/v1/models, 10 Oct 2026).
OPENROUTER_PRICES = {"google/gemini-3-flash-preview": (0.0000005, 0.000003)}


def _token_cost(model: str, resp) -> float | None:
    """The cost of an OpenRouter call: usage.cost when it is sent, else tokens at the list price. Null otherwise."""
    if "openrouter.ai" not in (os.environ.get("LLM_BASE_URL") or ""):
        return None
    usage = getattr(resp, "usage", None)
    sent = getattr(usage, "cost", None)
    if isinstance(sent, (int, float)) and not isinstance(sent, bool) and math.isfinite(sent) and sent >= 0:
        return float(sent)
    price = OPENROUTER_PRICES.get(model)
    if price is None or usage is None:
        return None
    tokens_in, tokens_out = getattr(usage, "prompt_tokens", 0) or 0, getattr(usage, "completion_tokens", 0) or 0
    return tokens_in * price[0] + tokens_out * price[1]


def _exception_headers(exc) -> dict:
    """Best-effort headers from an SDK exception's response (for example APIStatusError.response.headers)."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return {}
    try:
        return dict(headers)
    except (TypeError, ValueError):
        return {}


def _settle(
    model: str,
    cache_tag: str,
    resp,
    cost: float | None,
    status: str,
    error: str | None,
    reserve_id: str | None = None,
) -> None:
    """Append one cost row under the lock, closing reserve_id. A write failure marks the ledger broken, never raises."""
    usage = getattr(resp, "usage", None)
    row = {
        "ts": datetime.now(UTC).isoformat(),
        "model": model,
        "cache_tag": cache_tag,
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "cost_usd": cost,
        "status": status,
        "error": error,
        "reserve_id": reserve_id,
    }
    try:
        with _ledger_lock():
            try:
                append_jsonl(COSTS_PATH, [row])
            except Exception as exc:
                _mark_broken(exc)  # still under the lock: no process can admit before the marker exists
    except Exception as exc:
        # Never mask the request result or its exception: record the break and refuse later requests.
        _mark_broken(exc)


def _without_retries(client):
    """Return the client with SDK transport retries disabled when it supports with_options."""
    with_options = getattr(client, "with_options", None)
    if not callable(with_options):
        return client
    return with_options(max_retries=0)


def metered_create(client, request: dict, cache_tag: str):
    """One metered provider request: reserve, call, settle exactly once, return the parsed response.

    SDK transport retries are turned off first, so every attempt is its own admitted and settled
    request even for callers that built the client themselves. Raw headers are read before
    parse(), so a parse failure still records the header cost. The request body is passed through
    unchanged, so audio content parts and other shapes work too.
    """
    model = request.get("model") or model_name()
    client = _without_retries(client)  # before admission: a setup failure must not leak a reservation
    reserve_id = _admit(_budget())
    resp = None
    headers: dict = {}
    status = "ok"
    error: str | None = None
    try:
        completions = client.chat.completions
        raw_api = getattr(completions, "with_raw_response", None)
        if raw_api is None:  # test stand-ins that expose only create()
            resp = completions.create(**request)
        else:
            raw = raw_api.create(**request)
            headers = dict(getattr(raw, "headers", None) or {})
            resp = raw.parse()
        return resp
    except Exception as exc:
        status = "error"
        error = f"{type(exc).__name__}: {exc}"
        if not headers:
            headers = _exception_headers(exc)
        raise
    finally:
        cost = _header_cost(headers)
        cost = _token_cost(model, resp) if cost is None else cost
        _settle(model, cache_tag, resp, cost, status, error, reserve_id=reserve_id)


def _make_client(base_url: str, api_key: str, max_retries: int | None):
    """Build the OpenAI client with transport retries off, so complete_json owns every attempt.

    The caller's max_retries is accepted for compatibility but never enables SDK retries:
    setting client.max_retries to 0 disables the SDK's internal retry loop.
    """
    from openai import OpenAI

    kw = {} if max_retries is None else {"max_retries": 0}
    client = OpenAI(base_url=base_url, api_key=api_key, **kw)
    client.max_retries = 0
    return client


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

    api_key = os.environ.get("LLM_API_KEY")
    if not base or not api_key:
        raise RuntimeError("LLM_BASE_URL and LLM_API_KEY must be set in .env (no cached answer for this request)")
    _budget()  # validate the setting before the first real attempt, so a bad value raises ValueError
    client = _make_client(base, api_key, max_retries)
    extra = {} if seed is None else {"seed": seed}
    request = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "response_format": {"type": "json_object"},
        **extra,
    }
    last_err: Exception | None = None
    for _ in range(retries + 1):
        try:
            resp = metered_create(client, request, cache_tag)
        except BudgetExceeded:
            raise
        except Exception as exc:
            last_err = exc
            continue
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


# --- Jev (TypeSafe System One): yes/no judgement questions. Same disk cache, budget guard and cost rows. ---

JEV_URL = "https://api.typesafe.ai/v1/systemone"
JEV_RETRIES = 3  # attempts on 429 / 529 / network errors, with exponential backoff
# USD per token. The Jev price is unknown, so cost rows carry 0.0 and do not count against LLM_BUDGET_USD.
JEV_PRICE_IN_USD = 0.0
JEV_PRICE_OUT_USD = 0.0


def jev_model() -> str:
    return os.environ.get("TYPESAFE_MODEL", "jev-latest")


def _jev_post(url: str, api_key: str, body: dict) -> dict:
    """The one network call to Jev. Tests replace this function. Raises on HTTP errors."""
    import urllib.request

    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def _jev_valid(ids: list[str]):
    """Accept an answers dict only when every id holds a finite number in [0, 1]."""

    def ok(v) -> bool:
        return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and 0 <= v <= 1

    def check(d: dict) -> bool:
        a = d.get("answers")
        return isinstance(a, dict) and all(ok(a.get(i)) for i in ids)

    return check


def _jev_err(exc: Exception, api_key: str) -> str:
    """Error text with the exception type and HTTP status only, never the raw message. The key is redacted."""
    code = getattr(exc, "code", None)
    text = type(exc).__name__ + (f" HTTP {code}" if isinstance(code, int) else "")
    return text.replace(api_key, "[redacted]")


def complete_jev(state, questions: dict[str, dict], cache_tag: str = "") -> dict[str, float]:
    """Ask Jev yes/no questions in one request. Return {question id: probability of yes, 0..1}.

    One request carries every question. A cache hit makes no request and needs no key.
    """
    from types import SimpleNamespace

    model = jev_model()
    base = os.environ.get("TYPESAFE_BASE_URL", "").strip() or JEV_URL
    ids = list(questions)
    key = _key(model, [{"state": state, "questions": questions}], base, cache_tag)
    hit = cached_json(key, _jev_valid(ids))
    if hit is not None:
        return {i: float(hit["answers"][i]) for i in ids}
    api_key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("TYPESAFE_API_KEY must be set in .env (no cached Jev answer for this request)")
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in api_key):
        raise SystemExit("TYPESAFE_API_KEY is malformed (whitespace inside)")
    _budget()
    body = {"state": state, "model": model, "questions": questions}
    last_err: Exception | None = None
    for attempt in range(JEV_RETRIES):
        reserve_id = _admit(_budget())
        resp, status, error = None, "ok", None
        try:
            out = _jev_post(base, api_key, body)
            answers = {i: (out.get("answers") or {}).get(i, {}).get("noul") for i in ids}
            u = out.get("usage") or {}
            resp = SimpleNamespace(
                usage=SimpleNamespace(prompt_tokens=u.get("input_tokens"), completion_tokens=u.get("output_tokens"))
            )
            data = {"answers": answers, "usage": u}
            if not _jev_valid(ids)(data):
                raise ValueError("Jev response lacks a numeric noul for every question")
        except Exception as exc:
            status, error = "error", _jev_err(exc, api_key)
            last_err = exc
        finally:
            usage = getattr(resp, "usage", None)
            cost = None if status == "error" else (
                (getattr(usage, "prompt_tokens", 0) or 0) * JEV_PRICE_IN_USD
                + (getattr(usage, "completion_tokens", 0) or 0) * JEV_PRICE_OUT_USD
            )  # fmt: skip
            _settle(model, cache_tag, resp, cost, status, error, reserve_id=reserve_id)
        if status == "ok":
            _write_cache(key, data)
            return {i: float(answers[i]) for i in ids}
        code = getattr(last_err, "code", None)
        if isinstance(code, int) and code < 500 and code != 429:
            break  # a 401 or 422 will not change on retry
        if attempt + 1 < JEV_RETRIES:
            time.sleep(2**attempt)
    raise RuntimeError(f"Jev request failed: {_jev_err(last_err, api_key)}")
