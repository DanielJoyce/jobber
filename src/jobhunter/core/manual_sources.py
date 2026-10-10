"""Sources for job groups the user made by hand rather than ingested.

``email-manual``: a group made when accepting a mail proposal for a posting jobhunter never
ingested (specs/007). ``paste-manual``: a posting pasted on New packet (specs/017). Neither is
ever scored by a nightly run or a re-score plan; a pasted one is scored only when the user asks
(Score this group now).
"""

from __future__ import annotations

EMAIL_MANUAL = "email-manual"
PASTE_MANUAL = "paste-manual"
MANUAL_SOURCES = (EMAIL_MANUAL, PASTE_MANUAL)
# For SQL: ``j.source_key NOT IN (<MANUAL_SOURCES_SQL>)``.
MANUAL_SOURCES_SQL = ", ".join(f"'{s}'" for s in MANUAL_SOURCES)
