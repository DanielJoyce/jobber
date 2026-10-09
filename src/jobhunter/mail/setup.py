"""Idempotent Gmail label + filter setup, and the manual-import filter XML."""

from __future__ import annotations

from dataclasses import dataclass, field
from xml.sax.saxutils import quoteattr

from jobhunter.config import Settings

PLACEHOLDER_ADDRESS = "<you>+jobs@gmail.com"
INBOX = "INBOX"
SPAM = "SPAM"


class MailSetupError(ValueError):
    pass


@dataclass
class SetupReport:
    created: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)
    dry_run: bool = False

    def lines(self) -> list[str]:
        verb = "would create" if self.dry_run else "created"
        return [f"{verb}: {x}" for x in self.created] + [f"exists: {x}" for x in self.existing]


def validate_address(settings: Settings) -> str:
    addr = settings.mail.alerts_address.strip()
    if not addr or addr == PLACEHOLDER_ADDRESS or "<" in addr or "@" not in addr:
        raise MailSetupError(
            "mail.alerts_address is still the placeholder. Set your real +jobs address in "
            '~/.config/jobhunter/config.toml:\n\n[mail]\nalerts_address = "<you>+jobs@gmail.com"'
            "  # your address with +jobs"
        )
    return addr


def _domain(d: str) -> str:
    return "@" + d.strip().lstrip("@")


def wanted_filters(settings: Settings) -> list[dict[str, str]]:
    """Criteria for each wanted filter: {'to': addr} then {'from': '@domain'} per fallback."""
    out: list[dict[str, str]] = [{"to": validate_address(settings)}]
    out += [{"from": _domain(d)} for d in settings.mail.fallback_sender_domains if d.strip()]
    return out


def filter_body(criteria: dict[str, str], label_id: str) -> dict:
    """Gmail users.settings.filters resource: skip inbox, never spam, apply the label."""
    return {
        "criteria": dict(criteria),
        "action": {"addLabelIds": [label_id], "removeLabelIds": [INBOX, SPAM]},
    }


def describe(criteria: dict[str, str]) -> str:
    return " ".join(f"{k}:{v}" for k, v in criteria.items())


def _find_label(service, name: str) -> str | None:
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for lab in labels:
        if lab.get("name") == name:
            return lab["id"]
    return None


def _matches(existing: dict, criteria: dict[str, str], label_id: str | None) -> bool:
    crit = existing.get("criteria", {})
    if any(crit.get(k) != v for k, v in criteria.items()):
        return False
    return label_id is None or label_id in existing.get("action", {}).get("addLabelIds", [])


def setup_mailbox(service, settings: Settings, dry_run: bool = False) -> SetupReport:
    """Ensure the label and filters exist. Safe to re-run; ``dry_run`` makes no writes."""
    wanted = wanted_filters(settings)
    report = SetupReport(dry_run=dry_run)
    name = settings.mail.label

    label_id = _find_label(service, name)
    if label_id:
        report.existing.append(f"label {name}")
    else:
        report.created.append(f"label {name}")
        if not dry_run:
            made = service.users().labels().create(userId="me", body={"name": name}).execute()
            label_id = made["id"]

    present = service.users().settings().filters().list(userId="me").execute().get("filter", [])
    for criteria in wanted:
        desc = f"filter {describe(criteria)}"
        if any(_matches(f, criteria, label_id) for f in present):
            report.existing.append(desc)
            continue
        report.created.append(desc)
        if not dry_run:
            body = filter_body(criteria, label_id or "")
            service.users().settings().filters().create(userId="me", body=body).execute()
    return report


def filters_xml(settings: Settings) -> str:
    """Atom feed for Gmail Settings -> Filters and Blocked Addresses -> Import filters."""
    entries = []
    for criteria in wanted_filters(settings):
        props = list(criteria.items())
        props += [
            ("label", settings.mail.label),
            ("shouldArchive", "true"),
            ("shouldNeverSpam", "true"),
        ]
        body = "\n".join(
            f"    <apps:property name={quoteattr(k)} value={quoteattr(v)}/>" for k, v in props
        )
        entries.append(
            "  <entry>\n    <category term='filter'></category>\n"
            "    <title>Mail Filter</title>\n    <content></content>\n" + body + "\n  </entry>"
        )
    return (
        "<?xml version='1.0' encoding='UTF-8'?>\n"
        "<feed xmlns='http://www.w3.org/2005/Atom' "
        "xmlns:apps='http://schemas.google.com/apps/2006'>\n"
        "  <title>Mail Filters</title>\n" + "\n".join(entries) + "\n</feed>\n"
    )


def mailbox_status(service, settings: Settings) -> dict:
    """Read-only: label presence, matching filters and message count under the label."""
    wanted = wanted_filters(settings)
    name = settings.mail.label
    label_id = _find_label(service, name)
    present = service.users().settings().filters().list(userId="me").execute().get("filter", [])
    filters = {describe(c): any(_matches(f, c, label_id) for f in present) for c in wanted}
    count = None
    if label_id:
        info = service.users().labels().get(userId="me", id=label_id).execute()
        count = info.get("messagesTotal", 0)
    return {"label": name, "label_present": bool(label_id), "filters": filters, "messages": count}
