"""Per-family alert parsers and family detection (specs/012 "Pipeline").

Detection order: the sender domain against the registry's hosts (an alert from
``employflorida.com`` is the ``fl-employflorida`` VOS row, state FL); known platform domains
(NEOGOV, NLx); body signals; the hosts the email links to; and finally the generic parser.
The matched registry row (``origin``) gives the stub its state and robots policy.

Every parser was written against synthetic fixtures (tests/fixtures/mail/). Real alert
formats must be re-validated once subscriptions arrive: ``jobhunter mail sample`` saves
scrubbed copies to add as fixtures.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from types import ModuleType
from urllib.parse import urlsplit

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from jobhunter.core.models import SourceRow
from jobhunter.mail.message import MailMessage
from jobhunter.mail.parsers import generic, joblink, neogov, nlx, vos
from jobhunter.mail.parsers.common import AlertEntry, unwrap_url

__all__ = ["PARSERS", "AlertEntry", "Detection", "detect_family", "parse_message", "row_for_host"]

PARSERS: dict[str, ModuleType] = {
    "vos": vos,
    "joblink": joblink,
    "neogov": neogov,
    "nlx": nlx,
    "generic": generic,
}
# Registry families that have an alert parser of the same name.
_REGISTRY_FAMILIES = {"vos", "joblink", "nlx"}


@dataclass
class Detection:
    family: str
    origin: SourceRow | None = None
    reason: str = ""


def _bare(host: str) -> str:
    host = host.lower().strip(".")
    return host[4:] if host.startswith("www.") else host


def _row_host(row: SourceRow) -> str:
    return _bare(urlsplit(row.entry).hostname or "")


def row_for_host(host: str, rows: Sequence[SourceRow]) -> SourceRow | None:
    """The registry row whose entry host is this host's site, if any.

    Exact host first; then the most specific row the host is a subdomain of
    (``mail.employflorida.com`` -> ``employflorida.com``); last, a single row that is a
    subdomain of the host (``illinois.gov`` -> ``illinoisjoblink.illinois.gov``), only when
    exactly one row qualifies. ``jobs.utah.gov`` is never matched by ``mail.utah.gov``.
    A host shared by more than one row (a multi-tenant platform) matches none.
    """
    host = _bare(host)
    if not host:
        return None
    candidates = [(r, _row_host(r)) for r in rows if r.family != "mailalerts"]
    # A host shared by several rows (governmentjobs.com hosts 13 states' NEOGOV boards)
    # can't identify the state, so it matches nothing rather than an arbitrary row.
    exact = [r for r, rh in candidates if rh == host]
    if exact:
        return exact[0] if len(exact) == 1 else None
    parents = [(len(rh), r) for r, rh in candidates if rh and host.endswith("." + rh)]
    if parents:
        best = max(n for n, _ in parents)
        top = [r for n, r in parents if n == best]
        return top[0] if len(top) == 1 else None
    children = [r for r, rh in candidates if rh.endswith("." + host)]
    return children[0] if len(children) == 1 else None


def _link_hosts(msg: MailMessage) -> list[str]:
    hosts: list[str] = []
    if msg.html:
        for a in HTMLParser(msg.html).css("a[href]"):
            host = urlsplit(unwrap_url(a.attributes.get("href") or "")).hostname
            if host:
                hosts.append(host.lower())
    return hosts


def _family_of(row: SourceRow | None) -> str | None:
    if row is not None and row.family in _REGISTRY_FAMILIES:
        return row.family
    return None


def detect_family(msg: MailMessage, rows: Sequence[SourceRow]) -> Detection:
    origin = row_for_host(msg.sender_domain, rows)
    if fam := _family_of(origin):
        return Detection(fam, origin, f"sender domain {msg.sender_domain}")
    if neogov.host_ok(msg.sender_domain):
        return Detection("neogov", origin, f"sender domain {msg.sender_domain}")
    if nlx.host_ok(msg.sender_domain):
        return Detection("nlx", origin, f"sender domain {msg.sender_domain}")

    if origin is None:
        counts = Counter(r.key for h in _link_hosts(msg) if (r := row_for_host(h, rows)))
        if counts:
            key = counts.most_common(1)[0][0]
            origin = next(r for r in rows if r.key == key)
            if fam := _family_of(origin):
                return Detection(fam, origin, f"links to {_row_host(origin)}")

    for name in ("vos", "neogov", "nlx", "joblink"):
        if PARSERS[name].matches(msg):
            return Detection(name, origin, "body signals")
    return Detection("generic", origin, "no family signals")


def parse_message(
    msg: MailMessage, rows: Sequence[SourceRow]
) -> tuple[Detection, list[AlertEntry]]:
    """Detect the family and parse. A family parser that finds nothing falls back to generic."""
    det = detect_family(msg, rows)
    found = PARSERS[det.family].parse(msg)
    if not found and det.family != "generic":
        found = generic.parse(msg)
    return det, found
