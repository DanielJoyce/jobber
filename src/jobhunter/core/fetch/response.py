"""CachedResponse: what every FetchContext call returns. Parsers read bodies from the cache."""

from __future__ import annotations

import codecs
import json as jsonlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from email.message import Message
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin

if TYPE_CHECKING:
    from jobhunter.core.fetch.cache import ContentCache

# <meta charset="x"> and <meta http-equiv="Content-Type" content="...; charset=x"> both match.
_META_CHARSET = re.compile(rb"""<meta[^>]+?charset\s*=\s*["']?\s*([A-Za-z0-9_.:-]+)""", re.I)
META_SNIFF_BYTES = 2048


def _known_codec(name: str) -> str | None:
    try:
        return codecs.lookup(name.strip()).name
    except LookupError:
        return None


def charset_from_content_type(content_type: str | None) -> str | None:
    if not content_type:
        return None
    msg = Message()
    msg["content-type"] = content_type
    charset = msg.get_param("charset")
    if not isinstance(charset, str):
        return None
    return _known_codec(charset)


def charset_from_meta(body: bytes) -> str | None:
    """The charset declared by a meta tag in the first 2 KB of an HTML body, if any."""
    match = _META_CHARSET.search(body[:META_SNIFF_BYTES])
    if match is None:
        return None
    return _known_codec(match.group(1).decode("ascii"))


@dataclass
class CachedResponse:
    """A response whose body lives in the content cache.

    ``from_cache`` is True when no body crossed the wire (ttl hit or 304). On those, ``headers``
    holds what fetch_log kept (etag, last-modified, content-type), so the charset survives.

    ``encoding``: the Content-Type charset, else a ``<meta>`` charset sniffed from the body,
    else utf-8 (decoded with replacement).
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
        declared = charset_from_content_type(self.headers.get("content-type"))
        return declared or charset_from_meta(self.content) or "utf-8"

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
        """Redirect target, if the response carried a Location header.

        Relative targets are made absolute. Non-http targets (mailto:, javascript:) and
        malformed ones come back as the raw header value.
        """
        loc = self.headers.get("location")
        if not loc:
            return None
        try:
            return urljoin(self.final_url, loc)
        except ValueError:  # e.g. "http://[" fails urlsplit
            return loc
