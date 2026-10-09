"""Gmail application matching (specs/007 "Optional: Gmail matching"): proposals only.

Scans recent general mail (never the alerts label), classifies each candidate with
deterministic rules (ATS sender domains plus subject/body phrase tables), matches it to a known
application or job group, and writes ``mail_proposal`` rows for the user to accept or dismiss.
Nothing is ever applied here. Only a snippet of at most ``SNIPPET_MAX`` characters, the sender,
the subject and ids are stored; message bodies are held in memory for matching and dropped.
Email content is never sent anywhere.
"""

from __future__ import annotations

import base64
import json
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr

from rapidfuzz import fuzz

from jobhunter.config import Settings
from jobhunter.pipeline.ats_rules import host_of

SNIPPET_MAX = 200
BODY_MAX = 20_000  # characters kept in memory for matching, per message
DEFAULT_DAYS = 14
MAX_MESSAGES = 300
MIN_CONFIDENCE = 0.5

KIND_STATUS = {
    "confirmation": "applied",
    "acknowledgement": "acknowledged",
    "rejection": "rejected",
    "interview": "interview",
    "offer": "offer",
}
KINDS = (*KIND_STATUS, "other")

# Known ATS / applicant-tracking sender domains (suffix match on the sender's domain).
ATS_SENDER_DOMAINS = (
    "myworkday.com",
    "workday.com",
    "myworkdayjobs.com",
    "greenhouse.io",
    "greenhouse-mail.io",
    "lever.co",
    "icims.com",
    "ashbyhq.com",
    "smartrecruiters.com",
    "usastaffing.gov",
    "usajobs.gov",
    "governmentjobs.com",
    "neogov.com",
    "taleo.net",
    "jobvite.com",
    "breezy.hr",
)

# Order matters: first kind with a hit wins. A rejection often also says "thank you for
# applying", and a confirmation often says "if selected for an interview", so the strong,
# specific kinds are tested first and the loose ones last.
PHRASES: dict[str, tuple[str, ...]] = {
    "rejection": (
        "unfortunately",
        "not moving forward",
        "not be moving forward",
        "will not be proceeding",
        "decided to pursue other candidates",
        "decided to move forward with other",
        "no longer under consideration",
        "position has been filled",
        "not selected",
        "regret to inform",
        "unable to offer you",
    ),
    "offer": (
        "pleased to offer",
        "happy to offer",
        "excited to offer",
        "offer of employment",
        "offer letter",
        "extend an offer",
        "extend you an offer",
        "conditional offer",
    ),
    "interview": (
        "schedule an interview",
        "schedule your interview",
        "invite you to interview",
        "invite you for an interview",
        "like to interview you",
        "interview invitation",
        "interview request",
        "phone screen",
        "select a time",
        "your availability",
    ),
    "confirmation": (
        "thank you for applying",
        "thanks for applying",
        "application received",
        "received your application",
        "application has been received",
        "successfully submitted",
        "application was submitted",
        "your application has been submitted",
        "thank you for your application",
        "thank you for your interest",
    ),
    "acknowledgement": (
        "under review",
        "reviewing your application",
        "review your application",
        "application is being reviewed",
        "application status",
    ),
}
# Too loose to count unless the sender is a known ATS.
WEAK_PHRASES = {"thank you for your interest", "application status", "your availability"}

URL_RE = re.compile(r"https?://[^\s<>\")\]]+", re.I)
_WS = re.compile(r"\s+")
_NONWORD = re.compile(r"[^a-z0-9]+")
_CORP_WORDS = {"inc", "llc", "corp", "corporation", "co", "company", "ltd", "the", "plc", "lp"}
_GENERIC_NAMES = re.compile(
    r"\b(careers?|jobs?|recruit(?:ing|ment|er)?|talent|hr|human resources|no-?reply|"
    r"notifications?|team|via \w+|workday|greenhouse|lever|icims|ashby|smartrecruiters)\b",
    re.I,
)


@dataclass
class Message:
    """A candidate email. ``body`` lives in memory only and is never stored."""

    message_id: str
    thread_id: str
    received_at: datetime
    sender: str  # raw From header
    subject: str
    snippet: str
    body: str = ""
    label_ids: list[str] = field(default_factory=list)

    @property
    def sender_name(self) -> str:
        return parseaddr(self.sender)[0]

    @property
    def sender_domain(self) -> str:
        return parseaddr(self.sender)[1].lower().rpartition("@")[2]


@dataclass
class Classification:
    kind: str
    ats_sender: bool
    in_subject: bool
    phrase: str | None = None
    source: str = "rules"


# Hook for an optional LLM classifier later (a separate bug). Called only when the rules say
# "other"; it may return a Classification or None. No LLM is used or required today.
Classifier = Callable[[Message], Classification | None]


@dataclass
class Proposal:
    gmail_message_id: str
    thread_id: str
    received_at: str
    kind: str
    proposed_action: str  # create_application | add_event
    proposed_status: str
    confidence: float
    evidence: dict
    application_id: int | None = None
    job_group_id: int | None = None


@dataclass
class ScanResult:
    proposals: list[Proposal] = field(default_factory=list)
    scanned: int = 0
    already_seen: int = 0
    stored: int = 0


# --- classification ------------------------------------------------------------------------


def is_ats_sender(domain: str) -> bool:
    domain = domain.lower()
    return any(domain == d or domain.endswith("." + d) for d in ATS_SENDER_DOMAINS)


def _norm_text(s: str) -> str:
    return _WS.sub(" ", s.lower().replace(chr(0x2019), "'")).strip()


def classify(msg: Message, fallback: Classifier | None = None) -> Classification:
    """Deterministic classification; ``fallback`` (optional LLM hook) only sees 'other'."""
    subject = _norm_text(msg.subject)
    body = _norm_text(msg.body or msg.snippet)
    ats = is_ats_sender(msg.sender_domain)
    for kind, phrases in PHRASES.items():
        for p in phrases:
            if p in subject and not (p in WEAK_PHRASES and not ats):
                return Classification(kind, ats, True, p)
        for p in phrases:
            if p in body and not (p in WEAK_PHRASES and not ats):
                return Classification(kind, ats, False, p)
    if fallback is not None:
        out = fallback(msg)
        if out is not None and out.kind in KINDS:
            return out
    return Classification("other", ats, False)


# --- Gmail reading -------------------------------------------------------------------------


def build_query(settings: Settings, days: int) -> str:
    domains = " OR ".join(ATS_SENDER_DOMAINS)
    words = "application OR applying OR applied OR interview OR candidacy OR position OR offer"
    label = settings.mail.label.strip().replace("/", "-").replace(" ", "-")
    return (
        f"newer_than:{days}d -label:{label} -in:spam -in:trash "
        f"(from:({domains}) OR subject:({words}))"
    )


def _header(payload: dict, name: str) -> str:
    for h in payload.get("headers", []):
        if h.get("name", "").lower() == name:
            return h.get("value", "")
    return ""


def _decode(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
    except ValueError:
        return ""


def _plain_text(part: dict) -> str:
    if part.get("mimeType", "").startswith("text/plain") and part.get("body", {}).get("data"):
        return _decode(part["body"]["data"])
    return "\n".join(_plain_text(p) for p in part.get("parts", []))


def parse_message(raw: dict) -> Message:
    payload = raw.get("payload", {})
    ms = int(raw.get("internalDate") or 0)
    return Message(
        message_id=raw["id"],
        thread_id=raw.get("threadId", ""),
        received_at=datetime.fromtimestamp(ms / 1000, UTC),
        sender=_header(payload, "from"),
        subject=_header(payload, "subject"),
        snippet=raw.get("snippet", ""),
        body=_plain_text(payload)[:BODY_MAX],
        label_ids=list(raw.get("labelIds", [])),
    )


def _alerts_label_id(service, settings: Settings) -> str | None:
    try:
        labels = service.users().labels().list(userId="me").execute().get("labels", [])
    except Exception:
        return None
    for lab in labels:
        if lab.get("name") == settings.mail.label:
            return lab["id"]
    return None


def fetch_candidates(
    service, settings: Settings, days: int, skip_ids: set[str], limit: int = MAX_MESSAGES
) -> tuple[list[Message], int]:
    """Messages from the last ``days`` days not in ``skip_ids``, minus the alerts label.

    Returns (messages, count of ids listed). Read-only: list + get with gmail.readonly.
    """
    q = build_query(settings, days)
    alerts_id = _alerts_label_id(service, settings)
    out: list[Message] = []
    listed = 0
    token: str | None = None
    while True:
        kwargs = {"userId": "me", "q": q, "maxResults": 100}
        if token:
            kwargs["pageToken"] = token
        page = service.users().messages().list(**kwargs).execute()
        for ref in page.get("messages", []):
            listed += 1
            if ref["id"] in skip_ids or listed > limit:
                continue
            raw = service.users().messages().get(userId="me", id=ref["id"], format="full").execute()
            msg = parse_message(raw)
            if alerts_id and alerts_id in msg.label_ids:
                continue
            out.append(msg)
        token = page.get("nextPageToken")
        if not token or listed >= limit:
            return out, listed


# --- matching ------------------------------------------------------------------------------


@dataclass
class Group:
    group_id: int
    title: str
    employer: str
    first_seen_at: str | None
    link_hosts: set[str]
    app_id: int | None
    app_status: str | None
    app_since: str | None


def load_groups(conn: sqlite3.Connection) -> list[Group]:
    rows = conn.execute(
        "SELECT g.id AS gid, j.title, COALESCE(j.employer, j.agency_raw, '') AS employer, "
        "j.first_seen_at, l.final_url, l.employer_host, a.id AS aid, a.status AS astatus, "
        "COALESCE(a.applied_at, a.created_at) AS asince "
        "FROM job_group g JOIN job j ON j.id = g.canonical_job_id "
        "LEFT JOIN apply_link l ON l.job_group_id = g.id "
        "LEFT JOIN application a ON a.job_group_id = g.id"
    ).fetchall()
    out = []
    for r in rows:
        hosts = {h.lower() for h in (r["employer_host"], host_of(r["final_url"] or "")) if h}
        out.append(
            Group(
                r["gid"],
                r["title"],
                r["employer"],
                r["first_seen_at"],
                hosts,
                r["aid"],
                r["astatus"],
                r["asince"],
            )
        )
    return out


def _tokens(s: str) -> list[str]:
    return [t for t in _NONWORD.split(s.lower()) if t]


def _employer_tokens(name: str) -> list[str]:
    toks = _tokens(name)
    return [t for t in toks if t not in _CORP_WORDS] or toks


def employer_signal(employer: str, haystack_tokens: list[str]) -> float:
    """1.0 for an exact token-run match, 0.6 for a fuzzy one (ratio >= 88), else 0."""
    emp = _employer_tokens(employer)
    want = " ".join(emp)
    if len(want) < 4:
        return 0.0
    n = len(emp)
    best = 0.0
    for i in range(max(0, len(haystack_tokens) - n + 1)):
        window = " ".join(haystack_tokens[i : i + n])
        if window == want:
            return 1.0
        if fuzz.ratio(want, window) >= 88:
            best = 0.6
    return best


def title_signal(title: str, haystack: str) -> float:
    t = " ".join(_tokens(title))
    if len(t) < 8:
        return 0.0
    if t in haystack:
        return 1.0
    return 0.8 if fuzz.partial_ratio(t, haystack) >= 92 else 0.0


def _registrable(host: str) -> str:
    return ".".join(host.split(".")[-2:])


def host_signal(link_hosts: set[str], seen_hosts: set[str]) -> float:
    """1.0 if an apply-link host appears verbatim in the email, 0.7 for the same domain."""
    if not link_hosts or not seen_hosts:
        return 0.0
    if link_hosts & seen_hosts:
        return 1.0
    for lh in link_hosts:
        reg = _registrable(lh)
        if is_ats_sender(reg):
            continue  # a shared ATS domain says nothing about which employer
        if any(_registrable(eh) == reg for eh in seen_hosts):
            return 0.7
    return 0.0


def email_hosts(msg: Message) -> set[str]:
    hosts = {host_of(u) for u in URL_RE.findall(msg.body or "")}
    if msg.sender_domain:
        hosts.add(msg.sender_domain)
    return {h for h in hosts if h}


@dataclass
class Match:
    group: Group
    confidence: float
    signals: dict[str, float]
    matched: list[str]


def _recent_enough(g: Group, msg: Message) -> bool:
    """The email is not older than the application (or, for a bare job, its first sighting)."""
    ref = g.app_since or g.first_seen_at
    if not ref:
        return True
    try:
        dt = datetime.fromisoformat(ref)
    except ValueError:
        return True
    dt = dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return msg.received_at >= dt - timedelta(days=1)


def match_groups(msg: Message, groups: list[Group]) -> Match | None:
    """Best group for the email, or None (no match, or an ambiguous tie)."""
    hay_text = _norm_text(f"{msg.sender_name} {msg.subject} {(msg.body or msg.snippet)[:4000]}")
    hay_tokens = _tokens(hay_text)[:800]
    hay_flat = " ".join(hay_tokens)
    hosts = email_hosts(msg)
    emp_cache: dict[str, float] = {}
    scored: list[Match] = []
    for g in groups:
        if g.employer not in emp_cache:
            emp_cache[g.employer] = employer_signal(g.employer, hay_tokens)
        emp = emp_cache[g.employer]
        host = host_signal(g.link_hosts, hosts)
        title = title_signal(g.title, hay_flat) if (emp or host) else 0.0
        if not (emp or host or title):
            continue
        recency = 1.0 if _recent_enough(g, msg) else 0.0
        conf = min(1.0, 0.5 * host + 0.3 * emp + 0.3 * title + 0.05 * recency)
        if conf < MIN_CONFIDENCE:
            continue
        matched = [n for n, v in (("ats_host", host), ("employer", emp), ("title", title)) if v]
        signals = {"host": host, "employer": emp, "title": title}
        scored.append(Match(g, round(conf, 2), signals, matched))
    if not scored:
        return None
    scored.sort(key=lambda m: (-m.confidence, m.group.group_id))
    if len(scored) > 1 and scored[1].confidence >= scored[0].confidence - 0.05:
        a, b = scored[0], scored[1]
        # Prefer the group the user is already tracking; otherwise it is ambiguous.
        if (a.group.app_id is None) != (b.group.app_id is None):
            return a if a.group.app_id is not None else b
        return None
    return scored[0]


# --- extracting employer/title for unmatched confirmations ---------------------------------

_TITLE_PATTERNS = (
    re.compile(
        r"applying (?:for|to) (?:the )?(?P<t>.+?) (?:position |role )?at (?P<e>[^.,!|]+)", re.I
    ),
    re.compile(
        r"application (?:for|to) (?:the )?(?P<t>.+?) (?:position |role )?at (?P<e>[^.,!|]+)", re.I
    ),
    re.compile(r"your application (?:to|with) (?P<e>[^.,!|-]+?)(?: - |: )(?P<t>[^.,!|]+)", re.I),
    re.compile(r"for the (?P<t>[^.,!|]+?) (?:position|role)\b", re.I),
)


def parse_employer_title(msg: Message) -> tuple[str | None, str | None]:
    employer = title = None
    for text in (msg.subject, (msg.body or msg.snippet)[:1500]):
        for pat in _TITLE_PATTERNS:
            m = pat.search(text)
            if m:
                gd = m.groupdict()
                title = title or (gd.get("t") or "").strip(" -:") or None
                employer = employer or (gd.get("e") or "").strip(" -:") or None
        if employer and title:
            break
    if not employer:
        employer = _GENERIC_NAMES.sub("", msg.sender_name).strip(" -:|,") or None
    return employer, title


# --- proposals -----------------------------------------------------------------------------

_ORDER = ("interested", "preparing", "applied", "acknowledged", "screening", "interview", "offer")


def make_snippet(msg: Message, phrase: str | None) -> str:
    """At most SNIPPET_MAX chars, starting near the matched phrase when it is in the body."""
    text = _WS.sub(" ", msg.snippet or msg.body or "").strip()
    if phrase and msg.body:
        flat = _WS.sub(" ", msg.body)
        i = flat.lower().find(phrase)
        if i >= 0:
            text = flat[max(0, i - 40) :].strip()
    return text[:SNIPPET_MAX]


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def _worth_event(g: Group, kind: str, msg: Message) -> bool:
    """Skip emails that tell us nothing new about an already-tracked application."""
    if not _recent_enough(g, msg):
        return False
    status, want = g.app_status or "", KIND_STATUS[kind]
    if status == want:
        return False
    if status in ("rejected", "withdrawn", "closed"):
        return False
    if kind == "confirmation":
        return status in ("interested", "preparing")
    if kind == "acknowledgement":
        return status in ("interested", "preparing", "applied", "no_response")
    if kind == "rejection":
        return True
    if status in _ORDER:
        return _ORDER.index(want) > _ORDER.index(status)
    return True


def build_proposal(msg: Message, cls: Classification, groups: list[Group]) -> Proposal | None:
    if cls.kind == "other":
        return None
    status = KIND_STATUS[cls.kind]
    kind_factor = 1.0 if (cls.ats_sender or cls.in_subject) else 0.85
    evidence: dict = {
        "sender": msg.sender[:200],
        "subject": msg.subject[:200],
        "snippet": make_snippet(msg, cls.phrase),
        "phrase": cls.phrase,
        "classifier": cls.source,
        "ats_sender": cls.ats_sender,
    }
    base = {
        "gmail_message_id": msg.message_id,
        "thread_id": msg.thread_id,
        "received_at": _iso(msg.received_at),
        "kind": cls.kind,
        "proposed_status": status,
    }
    match = match_groups(msg, groups)
    if match is not None:
        g = match.group
        evidence["matched"] = match.matched
        evidence["signals"] = match.signals
        evidence["job"] = {"title": g.title, "employer": g.employer}
        conf = round(match.confidence * kind_factor, 2)
        if g.app_id is not None:
            if not _worth_event(g, cls.kind, msg):
                return None
            return Proposal(
                **base,
                proposed_action="add_event",
                confidence=conf,
                evidence=evidence,
                application_id=g.app_id,
                job_group_id=g.group_id,
            )
        if cls.kind != "confirmation":
            return None
        return Proposal(
            **base,
            proposed_action="create_application",
            confidence=conf,
            evidence=evidence,
            job_group_id=g.group_id,
        )
    if cls.kind != "confirmation":
        return None
    employer, title = parse_employer_title(msg)
    evidence["matched"] = []
    evidence["parsed"] = {"employer": employer, "title": title}
    return Proposal(
        **base,
        proposed_action="create_application",
        confidence=round((0.4 if cls.ats_sender else 0.3) * kind_factor, 2),
        evidence=evidence,
    )


def existing_message_ids(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT gmail_message_id FROM mail_proposal")}


def store(conn: sqlite3.Connection, proposals: list[Proposal], now: datetime) -> int:
    """Insert new proposals; a message that already has a row is left untouched."""
    n = 0
    for p in proposals:
        if p.proposed_action == "create_application" and p.job_group_id is not None:
            dup = conn.execute(
                "SELECT 1 FROM mail_proposal WHERE job_group_id = ? AND state = 'pending' "
                "AND proposed_action = 'create_application'",
                (p.job_group_id,),
            ).fetchone()
            if dup:
                continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO mail_proposal (gmail_message_id, thread_id, received_at, "
            "kind, proposed_action, application_id, job_group_id, proposed_status, confidence, "
            "evidence, state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (
                p.gmail_message_id,
                p.thread_id,
                p.received_at,
                p.kind,
                p.proposed_action,
                p.application_id,
                p.job_group_id,
                p.proposed_status,
                p.confidence,
                json.dumps(p.evidence),
                _iso(now),
            ),
        )
        n += cur.rowcount
    conn.commit()
    return n


def scan(
    conn: sqlite3.Connection,
    service,
    settings: Settings,
    days: int = DEFAULT_DAYS,
    dry_run: bool = False,
    now: datetime | None = None,
    fallback: Classifier | None = None,
) -> ScanResult:
    """Scan, classify, match and (unless ``dry_run``) store proposals. Idempotent."""
    now = now or datetime.now(UTC)
    seen = existing_message_ids(conn)
    messages, listed = fetch_candidates(service, settings, days, seen)
    groups = load_groups(conn)
    result = ScanResult(scanned=len(messages), already_seen=listed - len(messages))
    for msg in messages:
        prop = build_proposal(msg, classify(msg, fallback), groups)
        if prop is not None:
            result.proposals.append(prop)
    if not dry_run:
        result.stored = store(conn, result.proposals, now)
    return result
