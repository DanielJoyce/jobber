"""FetchContext: cache, rate limit, robots, retry (specs/002, specs/003, specs/008).

The only package in jobhunter allowed to import ``httpx`` or ``playwright``.
"""

from jobhunter.core.fetch.cache import ContentCache, content_hash
from jobhunter.core.fetch.context import (
    FetchContext,
    fetch_context_for_url,
    request_hash,
    reset_shared_state,
)
from jobhunter.core.fetch.errors import (
    AccessDenied,
    FetchError,
    RobotsDisallowed,
    SourceBlocked,
    TierError,
    TransientFetchError,
)
from jobhunter.core.fetch.ratelimit import HostRateLimiter
from jobhunter.core.fetch.response import CachedResponse
from jobhunter.core.fetch.robots import ROBOTS, RobotsPolicy, RobotsRules

__all__ = [
    "ROBOTS",
    "AccessDenied",
    "CachedResponse",
    "ContentCache",
    "FetchContext",
    "FetchError",
    "HostRateLimiter",
    "RobotsDisallowed",
    "RobotsPolicy",
    "RobotsRules",
    "SourceBlocked",
    "TierError",
    "TransientFetchError",
    "content_hash",
    "fetch_context_for_url",
    "request_hash",
    "reset_shared_state",
]
