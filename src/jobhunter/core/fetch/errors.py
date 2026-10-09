"""Fetch exceptions, all in one place (specs/004-ingest-pipeline.md#failure-handling)."""

from __future__ import annotations


class FetchError(Exception):
    """Base class for everything FetchContext raises on purpose."""

    def __init__(self, message: str, *, url: str | None = None) -> None:
        super().__init__(message)
        self.url = url


class RobotsDisallowed(FetchError):
    """The host's robots.txt disallows this URL for our user agent."""

    def __init__(self, url: str, reason: str = "robots.txt disallows") -> None:
        super().__init__(f"{reason}: {url}", url=url)
        self.reason = reason


class SourceBlocked(FetchError):
    """The source's registry policy is blocked/disabled; nothing may be fetched."""

    def __init__(self, source_key: str, policy: str) -> None:
        super().__init__(f"source {source_key!r} has policy {policy!r}; refusing to fetch")
        self.source_key = source_key
        self.policy = policy


class AccessDenied(FetchError):
    """401/403: a block is a decision, not a blip. Never retried; caller marks source broken."""

    def __init__(self, url: str, status: int) -> None:
        super().__init__(f"HTTP {status} for {url}", url=url)
        self.status = status


class TransientFetchError(FetchError):
    """429/5xx/connection errors that outlasted the retry ladder; caller marks source suspect."""

    def __init__(self, url: str, detail: str, *, status: int | None = None) -> None:
        super().__init__(f"giving up on {url}: {detail}", url=url)
        self.status = status


class TierError(FetchError):
    """Operation not available for this source's access tier (e.g. page() on an http source)."""
