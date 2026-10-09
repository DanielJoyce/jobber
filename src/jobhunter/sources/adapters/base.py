"""SourceAdapter protocol (specs/003-sources-and-adapters.md#adapter-contract)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from jobhunter.core.models import JobDetail, JobStub, Query, SourceRow

if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import datetime

    from jobhunter.core.fetch import FetchContext


class SourceAdapter(Protocol):
    family: str

    def search(
        self, src: SourceRow, query: Query, since: datetime, ctx: FetchContext
    ) -> Iterator[JobStub]:
        """Run one query against one source. Yield stubs newest-first.

        Must be a generator: the pipeline stops consuming at the watermark, so
        a well-written adapter never fetches page 25 of a result set whose
        page 2 already predates `since`.
        """
        ...

    def resolve(self, stub: JobStub, ctx: FetchContext) -> JobDetail:
        """Fetch and parse the full posting. Called only when needed."""
        ...
