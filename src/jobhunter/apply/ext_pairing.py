"""Pairing the jobhunter browser extension with this console (specs/017 "Local API security").

**Pair browser extension** on /prefs, or ``jobhunter ext pair``, makes a one-time code: 10
base32 characters (50 bits), valid 10 minutes, shown once. Its hash and expiry go to
``<data dir>/extension.json`` (mode 0600), because the CLI and the console are separate
processes. The user types the code on the extension's options page; the service worker sends
it to ``POST /ext/v1/pair`` from the pinned extension origin; five wrong codes void it. The
console answers with a random 256-bit token and keeps only its SHA-256. Pairing again revokes
the old token; ``jobhunter ext unpair`` revokes it.

The token is the one secret: never in a URL, cookie, page or log.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jobhunter.xdg import mkdir_private

# The ID Chrome derives from the public ``key`` in extension/manifest.json (the private half
# was discarded; nothing is packed or signed). A test ties the two together.
EXTENSION_ID = "jcchedhflefalogkellhfkjnifobnlde"
EXTENSION_ORIGIN = f"chrome-extension://{EXTENSION_ID}"
FILE_NAME = "extension.json"
CODE_TTL = timedelta(minutes=10)
CODE_CHARS = 10
MAX_FAILURES = 5


class PairError(Exception):
    """Pairing refused; the message is safe to show (it never holds a code or token)."""


def state_path(data_dir: Path) -> Path:
    return Path(data_dir) / FILE_NAME


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _norm_code(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


def _read(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(path: Path, data: dict[str, Any]) -> None:
    mkdir_private(path.parent)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    os.chmod(path, 0o600)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def new_code(path: Path, now: datetime) -> tuple[str, datetime]:
    """Make the one-time code (shown once, as ``ABCDE-FGHIJ``) and its expiry.

    A new code replaces any pending one; an existing pairing keeps working until a code is
    redeemed.
    """
    raw = base64.b32encode(secrets.token_bytes(7)).decode("ascii")[:CODE_CHARS]
    expires = now + CODE_TTL
    data = _read(path)
    data["pending"] = {"code_sha256": _sha(raw), "expires_at": _iso(expires), "failures": 0}
    _write(path, data)
    return f"{raw[:5]}-{raw[5:]}", expires


def redeem(path: Path, code: str, extension_id: str, now: datetime) -> str:
    """Exchange a pending code for a new token (the old token stops working)."""
    if not hmac.compare_digest(extension_id, EXTENSION_ID):
        raise PairError("this is not the jobhunter extension")
    data = _read(path)
    pending = data.get("pending")
    if not isinstance(pending, dict):
        raise PairError("no pairing code is waiting; make one on /prefs or with jobhunter ext pair")
    try:
        expires = datetime.fromisoformat(pending["expires_at"])
    except (KeyError, TypeError, ValueError):
        expires = now
    if now >= expires:
        data.pop("pending", None)
        _write(path, data)
        raise PairError("that pairing code has expired; make a new one")
    if not hmac.compare_digest(_sha(_norm_code(code)), str(pending.get("code_sha256", ""))):
        pending["failures"] = int(pending.get("failures", 0)) + 1
        if pending["failures"] >= MAX_FAILURES:
            data.pop("pending", None)
            _write(path, data)
            raise PairError("too many wrong codes; that code is void, make a new one")
        data["pending"] = pending
        _write(path, data)
        raise PairError("wrong pairing code")
    token = secrets.token_urlsafe(32)  # 256 bits
    _write(
        path,
        {"token_sha256": _sha(token), "extension_id": EXTENSION_ID, "paired_at": _iso(now)},
    )
    return token


def verify(path: Path, token: str | None) -> bool:
    """Constant-time check of a bearer token against the stored hash."""
    if not token:
        return False
    stored = _read(path).get("token_sha256")
    if not isinstance(stored, str) or not stored:
        return False
    return hmac.compare_digest(_sha(token), stored)


def unpair(path: Path) -> bool:
    """Revoke the token (and any pending code). True when something was revoked."""
    data = _read(path)
    had = bool(data.get("token_sha256") or data.get("pending"))
    if path.exists():
        _write(path, {})
    return had


def status(path: Path, now: datetime) -> dict[str, Any]:
    data = _read(path)
    pending = data.get("pending") if isinstance(data.get("pending"), dict) else None
    waiting = None
    if pending:
        try:
            exp = datetime.fromisoformat(pending["expires_at"])
            waiting = _iso(exp) if exp > now else None
        except (KeyError, TypeError, ValueError):
            waiting = None
    return {
        "paired": bool(data.get("token_sha256")),
        "paired_at": data.get("paired_at"),
        "code_expires_at": waiting,
    }
