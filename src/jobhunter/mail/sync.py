"""Read new alert messages under the alerts label, with a Gmail historyId watermark.

First run (no watermark): ``users.messages.list(labelIds=[label])``, newest first. Later runs:
``users.history.list(startHistoryId=..., labelId=label)`` for messages added to the label since.
If Gmail no longer has that history (404, roughly a week), fall back to a full listing.
Messages already in ``mail_message`` are skipped either way, so a replay costs nothing.

Read-only: the gmail.readonly scope; nothing here modifies the mailbox. Every call backs off
on Gmail rate limits (``gmail_api.execute``).
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from email.utils import getaddresses
from pathlib import Path
from typing import Any

from jobhunter.mail.gmail_api import execute as gmail_execute
from jobhunter.mail.message import MailMessage, from_eml, from_gmail, gmail_raw_to_eml, scrub
from jobhunter.mail.setup import _find_label
from jobhunter.pipeline.listing import to_iso

log = logging.getLogger(__name__)

PAGE_SIZE = 100
DEFAULT_LIMIT = 200


class MailSyncError(RuntimeError):
    """A user-actionable problem reading the alerts label."""


@dataclass
class Batch:
    """Messages to process this run and the watermark to store once they are done."""

    label_id: str
    message_ids: list[str] = field(default_factory=list)
    history_id: str | None = None  # None: leave the watermark as it is
    truncated: bool = False


def _http_status(exc: BaseException) -> int | None:
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def label_id_for(service: Any, label: str) -> str:
    found = _find_label(service, label)
    if not found:
        raise MailSyncError(f"Gmail label {label!r} not found. Run: jobhunter mail setup")
    return found


def stored_watermark(conn: sqlite3.Connection, label: str) -> str | None:
    row = conn.execute(
        "SELECT history_id FROM mail_sync_state WHERE label = ?", (label,)
    ).fetchone()
    return row[0] if row else None


def store_watermark(
    conn: sqlite3.Connection, label: str, label_id: str, history_id: str | None, now: datetime
) -> None:
    conn.execute(
        "INSERT INTO mail_sync_state (label, label_id, history_id, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(label) DO UPDATE SET label_id = excluded.label_id, "
        "history_id = COALESCE(excluded.history_id, mail_sync_state.history_id), "
        "updated_at = excluded.updated_at",
        (label, label_id, history_id, to_iso(now)),
    )


def _processed(conn: sqlite3.Connection, ids: list[str]) -> set[str]:
    done: set[str] = set()
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        marks = ",".join("?" * len(chunk))
        q = f"SELECT message_id FROM mail_message WHERE message_id IN ({marks})"
        done.update(r[0] for r in conn.execute(q, chunk))
    return done


def _list_all(
    service: Any, label_id: str, limit: int, conn: sqlite3.Connection | None = None
) -> tuple[list[str], bool]:
    """Newest-first ids under the label, skipping processed ones, up to ``limit`` fresh ids.

    Returns (ids, truncated): truncated when more unprocessed messages remain.
    """
    ids: list[str] = []
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {
            "userId": "me",
            "labelIds": [label_id],
            "maxResults": min(PAGE_SIZE, max(limit, 1)),
        }
        if token:
            kwargs["pageToken"] = token
        resp = gmail_execute(service.users().messages().list(**kwargs))
        page = [m["id"] for m in resp.get("messages") or [] if m.get("id")]
        if conn is not None:
            done = _processed(conn, page)
            page = [i for i in page if i not in done]
        ids += page
        token = resp.get("nextPageToken")
        if len(ids) > limit:
            return ids[:limit], True
        if not token:
            return ids, False
        if len(ids) == limit:
            return ids, True


def _history_since(service: Any, label_id: str, start: str) -> tuple[list[str], str | None]:
    ids: list[str] = []
    latest: str | None = None
    token: str | None = None
    while True:
        kwargs: dict[str, Any] = {
            "userId": "me",
            "startHistoryId": start,
            "labelId": label_id,
            "historyTypes": ["messageAdded", "labelAdded"],
        }
        if token:
            kwargs["pageToken"] = token
        resp = gmail_execute(service.users().history().list(**kwargs))
        latest = str(resp.get("historyId") or latest or start)
        for rec in resp.get("history") or []:
            for item in (rec.get("messagesAdded") or []) + (rec.get("labelsAdded") or []):
                msg = item.get("message") or {}
                labels = msg.get("labelIds")
                if msg.get("id") and (labels is None or label_id in labels):
                    ids.append(msg["id"])
        token = resp.get("nextPageToken")
        if not token:
            return ids, latest


def plan_batch(
    service: Any, conn: sqlite3.Connection, label: str, limit: int = DEFAULT_LIMIT
) -> Batch:
    """Which message ids to fetch now, and the watermark to record after processing them."""
    label_id = label_id_for(service, label)
    batch = Batch(label_id)
    start = stored_watermark(conn, label)
    ids: list[str] | None = None
    if start:
        try:
            ids, batch.history_id = _history_since(service, label_id, start)
        except Exception as exc:
            if _http_status(exc) != 404:
                raise
            log.warning("Gmail history %s expired; listing the label in full", start)
            ids = None
    if ids is None:
        # Read the mailbox's history id first, so mail arriving mid-listing is caught next run.
        profile = gmail_execute(service.users().getProfile(userId="me"))
        batch.history_id = str(profile["historyId"]) if profile.get("historyId") else None
        ids, batch.truncated = _list_all(service, label_id, limit, conn)
    seen: set[str] = set()
    ordered = [i for i in ids if not (i in seen or seen.add(i))]
    done = _processed(conn, ordered)
    fresh = [i for i in ordered if i not in done]
    if len(fresh) > limit:
        fresh, batch.truncated = fresh[:limit], True
    batch.message_ids = fresh
    if batch.truncated:
        batch.history_id = None  # some messages wait for the next run: keep the old mark
    return batch


def fetch_message(service: Any, message_id: str) -> MailMessage:
    raw = gmail_execute(service.users().messages().get(userId="me", id=message_id, format="full"))
    return from_gmail(raw)


def iter_messages(service: Any, batch: Batch) -> Iterator[MailMessage]:
    for mid in batch.message_ids:
        yield fetch_message(service, mid)


# ─── jobhunter mail sample ──────────────────────────────────────────────────

_KEEP_HEADERS = ("From", "To", "Subject", "Date", "Reply-To")


def scrubbed_eml(raw: bytes, addresses: list[str]) -> bytes:
    """A minimal fixture copy: kept headers + decoded text/html parts, user address removed.

    Bodies are decoded before scrubbing (a base64 part would hide the address) and routing
    headers (Received, Delivered-To, DKIM, ...) are dropped entirely.
    """
    src = from_eml(raw)
    found = [a for _, a in getaddresses([src.headers.get("to", ""), src.headers.get("cc", "")])]
    addrs = [a for a in addresses + found if a and "@" in a]
    out = EmailMessage()
    for name in _KEEP_HEADERS:
        value = src.headers.get(name.lower())
        if value:
            out[name] = scrub(value, addrs)
    out.set_content(scrub(src.text, addrs) or "(no plain-text part)")
    if src.html:
        out.add_alternative(scrub(src.html, addrs), subtype="html")
    return bytes(out)


def save_samples(
    service: Any, label: str, out_dir: Path, n: int, addresses: list[str]
) -> list[Path]:
    """Save the newest ``n`` labelled messages as scrubbed ``.eml`` files for parser fixtures."""
    label_id = label_id_for(service, label)
    ids, _ = _list_all(service, label_id, n)
    out_dir.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for mid in ids[:n]:
        res = service.users().messages().get(userId="me", id=mid, format="raw").execute()
        data = scrubbed_eml(gmail_raw_to_eml(res["raw"]), addresses)
        path = out_dir / f"{mid}.eml"
        path.write_bytes(data)
        saved.append(path)
    return saved
