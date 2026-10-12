"""Achievements (specs/021): a catalog of habits that make a job search work, computed from
tables jobhunter already keeps.

Pure SQL and Python over the local database: nothing here imports a scorer, the packet
generator, an API client or the fetch layer, and no rule reads scoring or spending tables
(tests enforce both).

- ``catalog``: ids, names, categories, tiers, copy.
- ``facts``: the shared definitions (user events, sent dates, local days, active days).
- ``rules``: one rule per catalog entry.
- ``engine``: ``evaluate`` (read first, write only on change), revocation, settings, and the
  in-process fingerprint gate.
"""

from jobhunter.achievements.catalog import CATALOG, CATALOG_VERSION, CATEGORIES, Achievement
from jobhunter.achievements.engine import (
    Gate,
    Settings,
    Unlock,
    compute,
    evaluate,
    fingerprint,
    local_tz,
    mark_seen,
    read_settings,
    stored,
)

__all__ = [
    "CATALOG",
    "CATALOG_VERSION",
    "CATEGORIES",
    "Achievement",
    "Gate",
    "Settings",
    "Unlock",
    "compute",
    "evaluate",
    "fingerprint",
    "local_tz",
    "mark_seen",
    "read_settings",
    "stored",
]
