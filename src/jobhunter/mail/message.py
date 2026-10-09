"""One alert email, decoded from a Gmail API message (format=full) or a saved ``.eml`` file.

Both paths produce the same ``MailMessage`` so the parsers are tested on saved files and run
on live Gmail messages unchanged.
"""

from __future__ import annotations

import base64
import email
import email.policy
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Any


@dataclass
class MailMessage:
    message_id: str
    sender: str = ""  # bare address, lowercased
    subject: str = ""
    to: str = ""
    date: datetime | None = None
    html: str = ""
    text: str = ""
    history_id: str | None = None
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def sender_domain(self) -> str:
        return self.sender.rpartition("@")[2].lower()

    @property
    def body(self) -> str:
        """HTML when present, else plain text: what the parsers search for signals."""
        return self.html or self.text


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _b64(data: str) -> str:
    raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    return raw.decode("utf-8", errors="replace")


def _walk_parts(part: dict[str, Any], html: list[str], text: list[str]) -> None:
    mime = (part.get("mimeType") or "").lower()
    data = (part.get("body") or {}).get("data")
    if data and mime == "text/html":
        html.append(_b64(data))
    elif data and mime == "text/plain":
        text.append(_b64(data))
    for child in part.get("parts") or []:
        _walk_parts(child, html, text)


def from_gmail(msg: dict[str, Any]) -> MailMessage:
    """Decode a ``users.messages.get(format='full')`` resource."""
    payload = msg.get("payload") or {}
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers") or [] if "name" in h}
    html: list[str] = []
    text: list[str] = []
    _walk_parts(payload, html, text)
    date = _parse_date(headers.get("date"))
    if date is None and msg.get("internalDate"):
        date = datetime.fromtimestamp(int(msg["internalDate"]) / 1000, tz=UTC)
    return MailMessage(
        message_id=str(msg.get("id") or ""),
        sender=parseaddr(headers.get("from", ""))[1].lower(),
        subject=headers.get("subject", ""),
        to=headers.get("to", ""),
        date=date,
        html="\n".join(html),
        text="\n".join(text),
        history_id=str(msg["historyId"]) if msg.get("historyId") else None,
        headers=headers,
    )


def _eml_bodies(msg: EmailMessage) -> tuple[str, str]:
    html: list[str] = []
    text: list[str] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/html", "text/plain"):
            continue
        try:
            content = part.get_content()
        except (LookupError, ValueError):
            payload = part.get_payload(decode=True) or b""
            content = payload.decode("utf-8", errors="replace")
        (html if ctype == "text/html" else text).append(str(content))
    return "\n".join(html), "\n".join(text)


def from_eml(data: bytes, message_id: str = "") -> MailMessage:
    """Decode an RFC 822 message (the fixture format, and what ``mail sample`` saves)."""
    msg = email.message_from_bytes(data, policy=email.policy.default)
    assert isinstance(msg, EmailMessage)
    html, text = _eml_bodies(msg)
    headers = {k.lower(): str(v) for k, v in msg.items()}
    return MailMessage(
        message_id=message_id or headers.get("message-id", "").strip("<>"),
        sender=parseaddr(headers.get("from", ""))[1].lower(),
        subject=headers.get("subject", ""),
        to=headers.get("to", ""),
        date=_parse_date(headers.get("date")),
        html=html,
        text=text,
        headers=headers,
    )


def load_eml(path: Path | str) -> MailMessage:
    p = Path(path)
    return from_eml(p.read_bytes(), message_id=p.stem)


# ─── scrubbing (``jobhunter mail sample``) ──────────────────────────────────

_PLACEHOLDER = "you+jobs@example.com"


def scrub(text: str, addresses: list[str]) -> str:
    """Replace the user's own address(es) and their plain/+suffix variants with a placeholder.

    For ``<you>+jobs@example.com`` this also replaces the plain ``<you>@...`` address, any
    other ``+suffix`` variant, and the URL-encoded forms some alert links carry.
    """
    out = text
    for addr in addresses:
        addr = addr.strip()
        if "@" not in addr:
            continue
        local, _, domain = addr.partition("@")
        base = local.split("+", 1)[0]
        pattern = re.compile(
            rf"{re.escape(base)}(?:(?:\+|%2B)[\w.-]*)?(?:@|%40){re.escape(domain)}", re.IGNORECASE
        )
        out = pattern.sub(_PLACEHOLDER, out)
    return out


def gmail_raw_to_eml(raw_b64: str) -> bytes:
    """Bytes of a ``users.messages.get(format='raw')`` resource's ``raw`` field."""
    return base64.urlsafe_b64decode(raw_b64 + "=" * (-len(raw_b64) % 4))
