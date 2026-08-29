import time
from typing import Any

import requests

USER_AGENT = "EdgeDash/0.1 (career intelligence agent; contact via GitHub)"
DEFAULT_TIMEOUT = 10
MAX_RETRIES = 2


class SourceError(Exception):
    """Raised when an HTTP request fails after all retries."""


def get_json(
    url: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    """Make a GET request and return parsed JSON.

    Applies timeout, 2 retries with exponential backoff, and a real
    User-Agent.  This is the ONLY place in the project that calls requests.
    Raises SourceError on unrecoverable failure.
    """
    merged_headers = {"User-Agent": USER_AGENT}
    if headers:
        merged_headers.update(headers)

    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        if attempt > 0:
            backoff = 2 ** (attempt - 1)   # 1s, 2s
            time.sleep(backoff)
        try:
            response = requests.get(
                url,
                params=params,
                headers=merged_headers,
                timeout=DEFAULT_TIMEOUT,
            )
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            last_exc = exc

    raise SourceError(
        f"GET {url} failed after {MAX_RETRIES + 1} attempts: {last_exc}"
    ) from last_exc
