"""Sites whose alert emails have no parser yet and whose hosts are never fetched.

LinkedIn (bug 430a0e7) and Craigslist (b0ac328). Their messages are recorded in
``mail_message`` under a distinct family (``pending-<key>``) with no entries and yield no
stubs, so a later parser can re-read exactly those messages (specs/019 "Alerts that arrive
before their parser"). One list, shared by ingest and the /alerts rows.
"""

from __future__ import annotations

PENDING_PREFIX = "pending-"
# (key, sender domain)
PENDING_SITES = (("linkedin", "linkedin.com"), ("craigslist", "craigslist.org"))


def pending_key(sender_domain: str) -> str | None:
    """The site key for a sender domain (or subdomain), else None."""
    host = sender_domain.strip().lower().lstrip("@").rstrip(".")
    for key, dom in PENDING_SITES:
        if host == dom or host.endswith("." + dom):
            return key
    return None


def pending_family(sender_domain: str) -> str | None:
    key = pending_key(sender_domain)
    return PENDING_PREFIX + key if key else None
