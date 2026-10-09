"""CachedResponse: what every FetchContext call returns. Parsers read bodies from the cache."""

from __future__ import annotations

import codecs
import json as jsonlib
from dataclasses import dataclass, field
from datetime import datetime
from email.message import Message
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

if TYPE_CHECKING:
    from jobhunter.core.fetch.cache import ContentCache


def charset_from_content_type(content_type: str | None) -> str | None:
    if not content_type:
        return None
    msg = Message()
    msg["content-type"] = content_type
    charset = msg.get_param("charset")
    if not isinstance(charset, str):
        return None
    try:
        return codecs.lookup(charset.strip()).name
    except LookupError:
        return None


@dataclass
class CachedResponse:
    """A response whose body lives in the content cache.

    ``from_cache`` is True when no body crossed the wire (ttl hit or 304). On a ttl hit
    ``headers`` holds only what fetch_log keeps (etag/last-modified), so ``text`` falls back
    to utf-8 unless the body is ASCII-compatible anyway.
    """

    url: str
    final_url: str
    status: int
    headers: dict[str, str]
    content_hash: str
    from_cache: bool
    fetched_at: datetime
    _cache: ContentCache = field(repr=False)
    _content: bytes | None = field(default=None, repr=False)

    @property
    def content(self) -> bytes:
        if self._content is None:
            self._content = self._cache.get(self.content_hash)
        return self._content

    @property
    def encoding(self) -> str:
        return charset_from_content_type(self.headers.get("content-type")) or "utf-8"

    @property
    def text(self) -> str:
        return self.content.decode(self.encoding, errors="replace")

    def json(self) -> Any:
        return jsonlib.loads(self.content)

    @property
    def is_redirect(self) -> bool:
        return 300 <= self.status < 400 and "location" in self.headers

    @property
    def location(self) -> str | None:
        """Absolute redirect target, if the response carried a Location header."""
        loc = self.headers.get("location")
        return urljoin(self.final_url, loc) if loc else None
