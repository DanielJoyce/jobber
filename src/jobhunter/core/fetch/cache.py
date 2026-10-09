"""Content-addressed gzip body store (specs/002-architecture.md#the-raw-cache-is-load-bearing)."""

from __future__ import annotations

import gzip
import hashlib
import os
import tempfile
from pathlib import Path


def content_hash(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


class ContentCache:
    """Bodies live at ``<root>/<h[0:2]>/<h[2:4]>/<sha256>.gz``. Never auto-evicted."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def path_for(self, digest: str) -> Path:
        return self.root / digest[0:2] / digest[2:4] / f"{digest}.gz"

    def has(self, digest: str) -> bool:
        return self.path_for(digest).is_file()

    def put(self, body: bytes) -> str:
        """Store ``body`` (idempotent: identical bodies share one file). Returns its sha256."""
        digest = content_hash(body)
        path = self.path_for(digest)
        if path.is_file():
            return digest
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename so a crash never leaves a truncated file under the final name.
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
                gz.write(body)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return digest

    def get(self, digest: str) -> bytes:
        """Return the body for ``digest``. Raises FileNotFoundError if absent."""
        with gzip.open(self.path_for(digest), "rb") as fh:
            return fh.read()
