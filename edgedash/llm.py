"""
edgedash/llm.py — Single gateway to any language model (steering rule 15).

Public API:
    complete_json(prompt, schema, *, max_retries=1) -> dict

Only this module may import an LLM SDK.
"""

from __future__ import annotations

import collections
import json
import logging
import os
import re
import time
from typing import Any, Callable

from edgedash.config import Config, load_config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class LLMError(Exception):
    """Raised when the LLM cannot return a valid response after all retries."""


class LLMProviderError(LLMError):
    """Raised for configuration / authentication problems."""


# ---------------------------------------------------------------------------
# Rate-limiter  (rule 15: ≤1 rps, ≤15 rpm rolling window)
# ---------------------------------------------------------------------------


class _RateLimiter:
    """Thread-safe-enough rate limiter for a single-threaded async cycle."""

    def __init__(self, rps: float, rpm: int, tpm: int = 0) -> None:
        self._min_interval: float = 1.0 / rps  # seconds between calls
        self._rpm = rpm
        self._tpm = tpm
        # Tracks (timestamp, estimated_tokens)
        self._calls: collections.deque[tuple[float, int]] = collections.deque()
        self._last_call: float = 0.0

    def wait(self, estimated_tokens: int = 0) -> None:
        """Block until the next call is within RPS, RPM, and TPM budgets."""
        now = time.monotonic()

        # --- per-second gate ---
        elapsed = now - self._last_call
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
            now = time.monotonic()

        # --- rolling-minute gates ---
        cutoff = now - 60.0
        while self._calls and self._calls[0][0] < cutoff:
            self._calls.popleft()

        # Check RPM
        if self._rpm > 0 and len(self._calls) >= self._rpm:
            sleep_for = 60.0 - (now - self._calls[0][0])
            if sleep_for > 0:
                logger.debug("RPM cap reached, sleeping %.1fs", sleep_for)
                time.sleep(sleep_for)
            now = time.monotonic()
            cutoff = now - 60.0
            while self._calls and self._calls[0][0] < cutoff:
                self._calls.popleft()

        # Check TPM
        if self._tpm > 0:
            current_tpm = sum(t for _, t in self._calls)
            if (current_tpm + estimated_tokens) > self._tpm:
                tokens_to_clear = (current_tpm + estimated_tokens) - self._tpm
                cleared = 0
                idx = 0
                while cleared < tokens_to_clear and idx < len(self._calls):
                    cleared += self._calls[idx][1]
                    idx += 1
                
                if idx > 0:
                    sleep_for = 60.0 - (now - self._calls[idx-1][0])
                    if sleep_for > 0:
                        logger.debug("TPM cap reached, sleeping %.1fs to clear %d tokens", sleep_for, cleared)
                        time.sleep(sleep_for)
                    now = time.monotonic()
                    cutoff = now - 60.0
                    while self._calls and self._calls[0][0] < cutoff:
                        self._calls.popleft()

        self._calls.append((now, estimated_tokens))
        self._last_call = now


# Module-level limiter; re-initialised by _get_limiter() when config changes.
_limiter: _RateLimiter | None = None
_limiter_key: tuple[float, int] | None = None


def _get_limiter(cfg: Config) -> _RateLimiter:
    global _limiter, _limiter_key
    key = (cfg.llm_rps, cfg.llm_rpm, cfg.llm_tpm)
    if _limiter is None or _limiter_key != key:
        _limiter = _RateLimiter(cfg.llm_rps, cfg.llm_rpm, cfg.llm_tpm)
        _limiter_key = key
    return _limiter


# ---------------------------------------------------------------------------
# JSON extraction helper
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _extract_json(text: str) -> Any:
    """Strip markdown fences and prose; return parsed JSON or raise ValueError."""
    # Try to find a fenced block first
    match = _FENCE_RE.search(text)
    candidate = match.group(1).strip() if match else text.strip()

    # If the candidate doesn't start with { or [, try to find the first brace
    if candidate and candidate[0] not in "{[":
        brace = min(
            (candidate.find(c) for c in "{[" if candidate.find(c) != -1),
            default=-1,
        )
        if brace != -1:
            candidate = candidate[brace:]

    return json.loads(candidate)


# ---------------------------------------------------------------------------
# Schema validation  (minimal jsonschema-style, stdlib only)
# ---------------------------------------------------------------------------


def _validate(data: Any, schema: dict) -> None:
    """
    Validate *data* against a simplified JSON Schema subset.

    Supported keywords: type, properties, required, items.
    Raises ValueError with a clear message on failure.
    """
    _type_map = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "array": list,
        "object": dict,
        "null": type(None),
    }

    expected_type = schema.get("type")
    if expected_type:
        py_type = _type_map.get(expected_type)
        if py_type and not isinstance(data, py_type):
            raise ValueError(
                f"Expected type '{expected_type}', got '{type(data).__name__}'"
            )

    if schema.get("type") == "object" or "properties" in schema:
        if not isinstance(data, dict):
            raise ValueError(f"Expected object, got '{type(data).__name__}'")
        for key in schema.get("required", []):
            if key not in data:
                raise ValueError(f"Missing required field: '{key}'")
        for key, sub_schema in schema.get("properties", {}).items():
            if key in data:
                _validate(data[key], sub_schema)

    if schema.get("type") == "array" or "items" in schema:
        if not isinstance(data, list):
            raise ValueError(f"Expected array, got '{type(data).__name__}'")
        item_schema = schema.get("items", {})
        for i, item in enumerate(data):
            try:
                _validate(item, item_schema)
            except ValueError as exc:
                raise ValueError(f"Item {i}: {exc}") from exc


# ---------------------------------------------------------------------------
# Provider implementations
# ---------------------------------------------------------------------------


def _call_gemini(prompt: str, model: str) -> str:
    """Send *prompt* to Gemini via google.genai and return raw text."""
    try:
        from google import genai  # type: ignore[import]
    except ImportError as exc:
        raise LLMProviderError(
            "google-genai is not installed. "
            "Run: pip install google-genai"
        ) from exc

    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise LLMProviderError(
            "GEMINI_API_KEY is not set. "
            "Add it to your .env file (uppercase key: GEMINI_API_KEY=<your-key>) "
            "and ensure python-dotenv loads it before running EdgeDash."
        )

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(model=model, contents=prompt)
    return response.text


def _call_ollama(prompt: str, model: str) -> str:
    """Send *prompt* to a local Ollama server and return raw text."""
    import urllib.error
    import urllib.request

    payload = json.dumps({"model": model, "prompt": prompt, "stream": False}).encode()
    req = urllib.request.Request(
        "http://localhost:11434/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode())
            return body.get("response", "")
    except urllib.error.URLError as exc:
        raise LLMProviderError(
            f"Could not reach Ollama at localhost:11434. "
            f"Is Ollama running? Original error: {exc}"
        ) from exc


def _call_groq(prompt: str, model: str) -> str:
    """Send *prompt* to Groq Cloud and return raw text."""
    try:
        from groq import Groq  # type: ignore[import]
    except ImportError as exc:
        raise LLMProviderError(
            "groq is not installed. Run: pip install groq"
        ) from exc

    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        raise LLMProviderError(
            "GROQ_API_KEY is not set. "
            "Add it to your .env file (uppercase key: GROQ_API_KEY=<your-key>) "
            "and ensure python-dotenv loads it before running EdgeDash."
        )

    client = Groq(api_key=api_key)
    chat = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
    )
    return chat.choices[0].message.content or ""


# Dispatch table: provider name -> callable(prompt, model) -> str
# Adding a new provider = one new entry here; complete_json never changes.
_PROVIDERS: dict[str, Callable[[str, str], str]] = {
    "gemini": _call_gemini,
    "ollama": _call_ollama,
    "groq": _call_groq,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def complete_json(
    prompt: str,
    schema: dict,
    *,
    max_retries: int = 1,
    _cfg: Config | None = None,
) -> dict:
    """
    Send *prompt* to the configured LLM; parse, validate, and return JSON.

    Parameters
    ----------
    prompt:      The user/system prompt. Do NOT include JSON-format instructions;
                 this function appends them automatically.
    schema:      A simplified JSON Schema dict used to validate the response.
    max_retries: How many times to retry on parse/validation failure (default 1).
    _cfg:        Override config (used by tests / the CLI check).

    Raises
    ------
    LLMError           – after all retries fail.
    LLMProviderError   – for auth / config problems (subclass of LLMError).
    """
    cfg = _cfg or load_config()
    limiter = _get_limiter(cfg)

    provider_fn = _PROVIDERS.get(cfg.llm_provider)
    if provider_fn is None:
        raise LLMProviderError(
            f"Unknown llm_provider '{cfg.llm_provider}'. "
            f"Supported values: {list(_PROVIDERS)}"
        )

    json_instruction = (
        "\n\nReply with ONLY a JSON object — no prose, no markdown fences, "
        "no explanation. The JSON must satisfy this schema:\n"
        + json.dumps(schema, indent=2)
    )
    active_prompt = prompt + json_instruction

    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        # roughly 1 token per 4 chars + 200 overhead for completion
        estimated_tokens = len(active_prompt) // 4 + 200
        limiter.wait(estimated_tokens)

        # --- exponential back-off on 429 / quota errors ---
        for backoff_attempt in range(3):
            try:
                raw = provider_fn(active_prompt, cfg.llm_model)
                break
            except Exception as exc:
                msg = str(exc).lower()
                is_quota = any(k in msg for k in ("429", "quota", "rate limit"))
                if is_quota and backoff_attempt < 2:
                    wait = 2 ** backoff_attempt * 5  # 5s, 10s
                    logger.warning(
                        "Rate-limit / quota error (attempt %d), "
                        "backing off %ds: %s",
                        backoff_attempt + 1,
                        wait,
                        exc,
                    )
                    time.sleep(wait)
                else:
                    raise
        else:
            raise LLMError("Exhausted quota back-off retries (3 attempts).")

        # --- parse ---
        try:
            data = _extract_json(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            last_error = exc
            logger.warning(
                "JSON parse failed on attempt %d: %s | raw=%.200s",
                attempt + 1,
                exc,
                raw,
            )
            if attempt < max_retries:
                active_prompt = (
                    prompt
                    + json_instruction
                    + f"\n\nYour previous reply could not be parsed as JSON. "
                    f"Error: {exc}. "
                    f"Reply with ONLY raw JSON — no prose, no markdown fence."
                )
            continue

        # --- validate ---
        try:
            _validate(data, schema)
            return data  # type: ignore[return-value]
        except ValueError as exc:
            last_error = exc
            logger.warning(
                "Schema validation failed on attempt %d: %s",
                attempt + 1,
                exc,
            )
            if attempt < max_retries:
                active_prompt = (
                    prompt
                    + json_instruction
                    + f"\n\nYour previous reply failed schema validation. "
                    f"Error: {exc}. "
                    f"Fix the JSON and reply with ONLY the corrected JSON — "
                    f"no prose, no markdown fence."
                )

    raise LLMError(
        f"LLM returned an invalid response after {max_retries + 1} attempt(s). "
        f"Last error: {last_error}"
    )


# ---------------------------------------------------------------------------
# CLI: python -m edgedash.llm --check
# ---------------------------------------------------------------------------


def _cli_check() -> None:
    """Send one trivial prompt and report whether it worked."""
    from dotenv import load_dotenv  # type: ignore[import]

    load_dotenv()
    cfg = load_config()

    print(f"Provider : {cfg.llm_provider}")
    print(f"Model    : {cfg.llm_model}")
    print("Sending test prompt … ", end="", flush=True)

    test_schema = {
        "type": "object",
        "required": ["status"],
        "properties": {"status": {"type": "string"}},
    }
    try:
        result = complete_json(
            "Reply with a JSON object containing the key 'status' set to 'ok'.",
            test_schema,
            _cfg=cfg,
        )
        print("OK")
        print(f"Response : {result}")
    except LLMError as exc:
        print("FAILED")
        print(f"Error    : {exc}")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    import sys

    if "--check" in sys.argv:
        _cli_check()
    else:
        print("Usage: python -m edgedash.llm --check")
        raise SystemExit(1)
