"""Gmail API calls with backoff on rate limits.

A 60-day ``mail match`` scan issues one ``messages.get`` per candidate (5 quota units each)
and can trip Gmail's per-minute quota: HTTP 429, or 403 with reason ``rateLimitExceeded`` /
``userRateLimitExceeded``. ``execute`` retries those with exponential backoff, honouring a
``Retry-After`` header when Gmail sends one, and raises ``GmailRateLimited`` (a clean,
user-facing message) once retries run out. Every other error propagates unchanged.

``format="metadata"`` costs the same 5 units as ``format="full"`` and batch requests are
charged per inner call, so neither reduces quota; backoff is what fixes the crash.
``sleep`` is injectable so tests never wait.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

MAX_RETRIES = 6  # 1 + 2 + 4 + 8 + 16 + 32 s: just over a minute, one per-minute quota window
BASE_DELAY_S = 1.0
MAX_DELAY_S = 64.0
MAX_RETRY_AFTER_S = 120.0
RATE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}

Sleep = Callable[[float], None]


class GmailRateLimited(RuntimeError):
    """Gmail kept refusing with a rate limit after every retry."""


def _status(exc: BaseException) -> int | None:
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _reasons(exc: BaseException) -> set[str]:
    out: set[str] = set()
    details = getattr(exc, "error_details", None)
    if isinstance(details, list):
        out |= {str(d.get("reason")) for d in details if isinstance(d, dict) and d.get("reason")}
    content = getattr(exc, "content", None)
    if content:
        try:
            data = json.loads(content.decode() if isinstance(content, bytes) else content)
            errors = (data.get("error") or {}).get("errors") or []
            out |= {str(e.get("reason")) for e in errors if isinstance(e, dict) and e.get("reason")}
        except (ValueError, AttributeError, UnicodeDecodeError):
            pass
    return out


def is_rate_limited(exc: BaseException) -> bool:
    status = _status(exc)
    return status == 429 or (status == 403 and bool(_reasons(exc) & RATE_REASONS))


def retry_after(exc: BaseException) -> float | None:
    resp = getattr(exc, "resp", None)
    getter = getattr(resp, "get", None)
    value = getter("retry-after") if callable(getter) else None
    try:
        secs = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return None if secs is None or secs < 0 else min(secs, MAX_RETRY_AFTER_S)


def execute(
    request: Any,
    *,
    sleep: Sleep = time.sleep,
    retries: int = MAX_RETRIES,
    base_delay: float = BASE_DELAY_S,
) -> Any:
    """``request.execute()``, retrying Gmail rate limits; other errors propagate."""
    attempt = 0
    while True:
        try:
            return request.execute()
        except Exception as exc:
            if not is_rate_limited(exc):
                raise
            if attempt >= retries:
                raise GmailRateLimited(
                    f"Gmail rate limit: still refused after {retries} retries "
                    f"(HTTP {_status(exc)}). Wait a minute and run again, or scan fewer days "
                    "with --days."
                ) from exc
            delay = retry_after(exc)
            if delay is None:
                delay = min(MAX_DELAY_S, base_delay * 2**attempt)
            log.warning("Gmail rate limit (HTTP %s); retrying in %.0fs", _status(exc), delay)
            sleep(delay)
            attempt += 1
