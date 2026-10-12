"""Application-form data per ATS (specs/022 "Fill on click", "Signals"; specs/017 "Field
classification").

Data, grown like ``pipeline/ats_rules.py``: for each supported ATS and each **form variant**
(Greenhouse classic, Greenhouse ``job-boards``, Greenhouse embed, ...), the form, the named fields
and the answer each one takes, the vetted helper that may set it, the Submit control, the step and
other controls that are never a submit, confirmation and error rules with a **rule version**,
challenge markers, ``parse_done`` and ``attach_ok``. A **generic** entry for Fill this page holds
label synonyms only, no selectors.

The data only names targets and which vetted helper to use. It never names event types or
sequences (017 "Fill mechanics"), so no data can create a click path. Selectors and rules were
written against hand-written fixtures in each ATS's shape (``tests/fixtures/ats_forms/``) and
are checked at your first live submits; every variant starts in ask mode, so a wrong
confirmation rule asks instead of recording.

Row 0 of the classification (specs/022 "Classification") is decided here, from the visibility
facts the content script reports: :func:`row0`. Never-store is decided in ``apply/labels.py``
first, always: :func:`field_for` refuses a never-store field before any mapping.

Workday's step data comes with its own ticket (A11); its hosts and posting key are known already
(``pipeline/ats_rules.py``, ``apply/page_match.py``).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

from jobhunter.apply import labels
from jobhunter.pipeline.ats_rules import host_matches, match_ats

FORMS_VERSION = "1"  # bump on any change below; the worker re-fetches on a new version

Helper = Literal["setValue", "setChecked", "pickOption", "attach", "none"]
Role = Literal["resume", "cover_letter_file", "cover_letter_text", "parse_control"]
SignalKind = Literal["request", "element_present", "element_gone", "field_populated"]
AttachMethod = Literal["datatransfer", "drop", "manual"]

# Answer keys, as the answer bank names them (specs/022 "The answer bank"). ``contact.full_name``
# is derived (first plus last) for a single "Full name" field.
CONTACT_KEYS = (
    "contact.first_name",
    "contact.last_name",
    "contact.full_name",
    "contact.preferred_name",
    "contact.email",
    "contact.phone",
    "contact.city",
    "contact.region",
    "contact.country",
)
LINK_KEYS = ("links.linkedin", "links.github", "links.portfolio", "links.website")
OTHER_KEYS = ("notice_period", "relocation", "work_authorization")
ANSWER_KEYS = frozenset((*CONTACT_KEYS, *LINK_KEYS, *OTHER_KEYS))


@dataclass(frozen=True)
class FieldSpec:
    """One named field of a form. ``key`` is the stable field key (017: the ``ats_forms.py``
    field name), independent of generated ids."""

    key: str
    selector: str
    helper: Helper
    answer: str | None = None  # an ANSWER_KEYS entry, or None (listed, drafted or yours)
    role: Role | None = None
    names: tuple[str, ...] = ()  # the field's name or id attributes, matched exactly


@dataclass(frozen=True)
class Signal:
    """``request``: a resource entry whose URL matches ``pattern`` (a regular expression);
    ``element_present`` / ``element_gone``: a selector appearing or going; ``field_populated``:
    the named field's selector getting a value."""

    kind: SignalKind
    pattern: str


@dataclass(frozen=True)
class ConfirmationRule:
    """``url``: the next document in the same frame has a path matching ``pattern``.
    ``element``: an element matching ``pattern`` appears while the form disappears. Never a
    bare text match."""

    kind: Literal["url", "element"]
    pattern: str


@dataclass(frozen=True)
class Variant:
    name: str
    hosts: tuple[str, ...]  # host patterns (ats_rules.host_matches)
    path: str  # regular expression over the URL path: this variant's form pages
    form: str  # the form (or its container)
    fields: tuple[FieldSpec, ...]
    submit: str  # the Submit control
    not_submit: tuple[str, ...]  # step and other controls: never an attempt
    rule_version: str
    confirmation: tuple[ConfirmationRule, ...]
    errors: tuple[str, ...]  # validation failure markers after an attempt
    submit_request: str | None = None  # regex over a resource URL: request finished (supporting)
    parse_done: Signal | None = None  # None: nothing to wait for (no parse)
    attach_ok: Signal | None = None
    attach: AttachMethod = "datatransfer"
    steps: bool = False  # multi-step (Workday): Next and Back are step controls


@dataclass(frozen=True)
class AtsForm:
    ats: str
    variants: tuple[Variant, ...]


# Every ATS: a CAPTCHA frame or a "verify you are human" view means the tab is yours.
CHALLENGE = (
    'iframe[src*="recaptcha"]',
    'iframe[src*="hcaptcha"]',
    'iframe[src*="challenges.cloudflare.com"]',
    'iframe[title*="challenge"]',
)
# Words on a control that make it a step or other control, never Submit (the content script
# compares a control's own text, value or aria-label, normalized, whole).
NOT_SUBMIT_WORDS = (
    "next",
    "back",
    "previous",
    "continue",
    "save and continue",
    "save draft",
    "save for later",
    "apply",
    "apply now",
    "apply for this job",
    "search",
    "attach",
    "upload",
    "upload file",
    "enter manually",
    "dropbox",
    "google drive",
)

# ─── Greenhouse ────────────────────────────────────────────────────────────

_GH_HOSTS = ("boards.greenhouse.io", "job-boards.greenhouse.io", "job-boards.eu.greenhouse.io")
_GH_CLASSIC_FIELDS = (
    FieldSpec("first_name", "#first_name", "setValue", "contact.first_name",
              names=("job_application[first_name]", "first_name")),
    FieldSpec("last_name", "#last_name", "setValue", "contact.last_name",
              names=("job_application[last_name]", "last_name")),
    FieldSpec("email", "#email", "setValue", "contact.email",
              names=("job_application[email]", "email")),
    FieldSpec("phone", "#phone", "setValue", "contact.phone",
              names=("job_application[phone]", "phone")),
    FieldSpec("location", "#job_application_location", "setValue", "contact.city",
              names=("job_application[location]", "job_application_location")),
    FieldSpec("resume", '#s3_upload_for_resume input[type="file"]', "attach", role="resume"),
    FieldSpec("cover_letter", '#s3_upload_for_cover_letter input[type="file"]', "attach",
              role="cover_letter_file"),
    FieldSpec("cover_letter_text", "#cover_letter_text", "setValue", role="cover_letter_text",
              names=("job_application[cover_letter_text]", "cover_letter_text")),
)  # fmt: skip
_GH_CLASSIC_NOT_SUBMIT = (
    "#apply_button",
    "#resume_fieldset button",
    "#resume_fieldset a",
    "#cover_letter_fieldset button",
    "#cover_letter_fieldset a",
)
_GH_ERRORS = ("#error_message", ".field-error-msg")

GREENHOUSE = AtsForm(
    "greenhouse",
    (
        Variant(
            name="greenhouse_embed",
            hosts=_GH_HOSTS,
            path=r"^/embed/job_app",
            form="#application_form",
            fields=_GH_CLASSIC_FIELDS,
            submit="#submit_app",
            not_submit=_GH_CLASSIC_NOT_SUBMIT,
            rule_version="1",
            confirmation=(ConfirmationRule("url", r"^/embed/job_app/confirmation"),),
            errors=_GH_ERRORS,
            submit_request=r"greenhouse\.io/embed/job_app",
        ),
        Variant(
            name="greenhouse_job_boards",
            hosts=("job-boards.greenhouse.io", "job-boards.eu.greenhouse.io"),
            path=r"^/[^/]+/jobs/\d+",
            form="form#application-form",
            fields=(
                FieldSpec("first_name", "#first_name", "setValue", "contact.first_name",
                          names=("first_name",)),
                FieldSpec("last_name", "#last_name", "setValue", "contact.last_name",
                          names=("last_name",)),
                FieldSpec("preferred_name", "#preferred_name", "setValue",
                          "contact.preferred_name", names=("preferred_name",)),
                FieldSpec("email", "#email", "setValue", "contact.email", names=("email",)),
                FieldSpec("phone", "#phone", "setValue", "contact.phone", names=("phone",)),
                FieldSpec("country", "#country", "pickOption", "contact.country",
                          names=("country",)),
                FieldSpec("location", "#candidate-location", "pickOption", "contact.city",
                          names=("candidate-location",)),
                FieldSpec("resume", '#resume[type="file"]', "attach", role="resume"),
                FieldSpec("cover_letter", '#cover_letter[type="file"]', "attach",
                          role="cover_letter_file"),
            ),
            submit='form#application-form button[type="submit"]',
            not_submit=(
                ".job__header button",
                'a[href="#app"]',
                ".file-upload button",
            ),
            rule_version="1",
            confirmation=(ConfirmationRule("url", r"^/[^/]+/jobs/\d+/confirmation"),),
            errors=(".helper-text--error", ".input-wrapper--error"),
            submit_request=r"greenhouse\.io/.+/jobs/\d+",
        ),
        Variant(
            name="greenhouse_classic",
            hosts=("boards.greenhouse.io",),
            path=r"^/[^/]+/jobs/\d+",
            form="#application_form",
            fields=_GH_CLASSIC_FIELDS,
            submit="#submit_app",
            not_submit=_GH_CLASSIC_NOT_SUBMIT,
            rule_version="1",
            confirmation=(ConfirmationRule("url", r"^/[^/]+/jobs/\d+/confirmation"),),
            errors=_GH_ERRORS,
            submit_request=r"boards\.greenhouse\.io/.+/jobs/\d+",
        ),
    ),
)  # fmt: skip

# ─── Lever ─────────────────────────────────────────────────────────────────

LEVER = AtsForm(
    "lever",
    (
        Variant(
            name="lever",
            hosts=("jobs.lever.co", "jobs.eu.lever.co"),
            path=r"^/[^/]+/[0-9a-f-]{20,}",
            form="form#application-form",
            fields=(
                FieldSpec("name", 'input[name="name"]', "setValue", "contact.full_name",
                          names=("name",)),
                FieldSpec("email", 'input[name="email"]', "setValue", "contact.email",
                          names=("email",)),
                FieldSpec("phone", 'input[name="phone"]', "setValue", "contact.phone",
                          names=("phone",)),
                FieldSpec("location", 'input[name="location"]', "setValue", "contact.city",
                          names=("location",)),
                FieldSpec("org", 'input[name="org"]', "setValue", names=("org",)),
                FieldSpec("linkedin", 'input[name="urls[LinkedIn]"]', "setValue",
                          "links.linkedin", names=("urls[LinkedIn]",)),
                FieldSpec("github", 'input[name="urls[GitHub]"]', "setValue", "links.github",
                          names=("urls[GitHub]",)),
                FieldSpec("portfolio", 'input[name="urls[Portfolio]"]', "setValue",
                          "links.portfolio", names=("urls[Portfolio]",)),
                FieldSpec("other_website", 'input[name="urls[Other]"]', "setValue",
                          "links.website", names=("urls[Other]",)),
                FieldSpec("resume", "#resume-upload-input", "attach", role="resume"),
                FieldSpec("comments", 'textarea[name="comments"]', "setValue",
                          role="cover_letter_text", names=("comments",)),
            ),
            submit="#btn-submit",
            not_submit=("a.postings-btn", "button.template-btn-utility"),
            rule_version="1",
            confirmation=(ConfirmationRule("url", r"^/[^/]+/[0-9a-f-]{20,}/thanks"),),
            errors=(".error-message", ".application-error"),
            submit_request=r"lever\.co/[^/]+/[0-9a-f-]{20,}/apply",
            parse_done=Signal("element_present", ".resume-upload-success"),
            attach_ok=Signal("element_present", ".resume-upload-filename"),
        ),
    ),
)  # fmt: skip

# ─── Ashby ─────────────────────────────────────────────────────────────────

_ASHBY_FIELDS = (
    FieldSpec("name", "#_systemfield_name", "setValue", "contact.full_name",
              names=("_systemfield_name",)),
    FieldSpec("email", "#_systemfield_email", "setValue", "contact.email",
              names=("_systemfield_email",)),
    FieldSpec("location", "#_systemfield_location", "pickOption", "contact.city",
              names=("_systemfield_location",)),
    FieldSpec("autofill", '.ashby-application-form-autofill-input input[type="file"]', "attach",
              role="parse_control"),
    FieldSpec("resume", "#_systemfield_resume", "attach", role="resume",
              names=("_systemfield_resume",)),
)  # fmt: skip
_ASHBY_COMMON: dict[str, Any] = {
    "hosts": ("jobs.ashbyhq.com",),
    "form": ".ashby-application-form-container",
    "fields": _ASHBY_FIELDS,
    "submit": "button.ashby-application-form-submit-button",
    "not_submit": (
        ".ashby-application-form-autofill-input button",
        'a[href$="/application"]',
    ),
    "rule_version": "1",
    "confirmation": (ConfirmationRule("element", ".ashby-application-form-success-container"),),
    "errors": (".ashby-application-form-field-entry-error",),
    "submit_request": r"ashbyhq\.com/api/non-user-graphql\?op=ApiSubmit",
    # Only the Autofill box triggers the parse (017 "Resume attach"); the name field filling
    # itself is the parse finishing.
    "parse_done": Signal("field_populated", "#_systemfield_name"),
    "attach_ok": Signal("element_present", ".ashby-application-form-autofill-input-file"),
}

ASHBY = AtsForm(
    "ashby",
    (
        Variant(name="ashby_embed", path=r"^/[^/]+(?:/[0-9a-f-]{20,})?(?:/application)?/embed",
                **_ASHBY_COMMON),
        Variant(name="ashby", path=r"^/[^/]+/[0-9a-f-]{20,}", **_ASHBY_COMMON),
    ),
)  # fmt: skip

# ─── Workable ──────────────────────────────────────────────────────────────

WORKABLE = AtsForm(
    "workable",
    (
        Variant(
            name="workable",
            hosts=("apply.workable.com",),
            path=r"^/[^/]+/j/[0-9A-Za-z]+",
            form='form[data-ui="application-form"]',
            fields=(
                FieldSpec("firstname", 'input[name="firstname"]', "setValue",
                          "contact.first_name", names=("firstname",)),
                FieldSpec("lastname", 'input[name="lastname"]', "setValue", "contact.last_name",
                          names=("lastname",)),
                FieldSpec("email", 'input[name="email"]', "setValue", "contact.email",
                          names=("email",)),
                FieldSpec("phone", 'input[name="phone"]', "setValue", "contact.phone",
                          names=("phone",)),
                FieldSpec("resume", 'input[data-ui="resume"]', "attach", role="resume"),
                FieldSpec("cover_letter", 'textarea[name="cover_letter"]', "setValue",
                          role="cover_letter_text", names=("cover_letter",)),
            ),
            submit='button[data-ui="apply-button"]',
            not_submit=('a[data-ui="overview-apply-now"]', 'button[data-ui="upload-button"]'),
            rule_version="1",
            confirmation=(ConfirmationRule("element", '[data-ui="application-success"]'),),
            errors=('[data-ui="error-message"]',),
            submit_request=r"apply\.workable\.com/api/v\d+/accounts/[^/]+/jobs/[^/]+/apply",
            attach_ok=Signal("element_present", '[data-ui="resume-file-name"]'),
        ),
    ),
)  # fmt: skip

FORMS: dict[str, AtsForm] = {f.ats: f for f in (GREENHOUSE, LEVER, ASHBY, WORKABLE)}
# The ATSs this spec supports, each with its posting key (``apply/page_match.py``). Workday's
# form data is ticket A11; until then it matches but has no named fields.
SUPPORTED_ATS = ("greenhouse", "lever", "ashby", "workable", "workday")

# ─── generic entry: Fill this page ─────────────────────────────────────────

# Whole normalized labels (``normalize_label``) that name one answer. Exact matches only: a
# label that merely contains "name" or "city" is not mapped, so it is listed for you.
SYNONYMS: dict[str, tuple[str, ...]] = {
    "contact.first_name": (
        "first name", "given name", "legal first name", "first name (legal)", "forename",
    ),
    "contact.last_name": (
        "last name", "surname", "family name", "legal last name", "last name (legal)",
    ),
    "contact.full_name": ("full name", "name", "your name", "legal name", "full legal name"),
    "contact.preferred_name": (
        "preferred name", "preferred first name", "nickname", "what name do you go by",
    ),
    "contact.email": ("email", "email address", "e-mail", "e-mail address", "your email"),
    "contact.phone": (
        "phone", "phone number", "mobile", "mobile number", "mobile phone", "cell phone",
        "cell phone number", "telephone", "telephone number", "contact number",
    ),
    "contact.city": (
        "city", "current city", "location", "location (city)", "current location",
        "city of residence",
    ),
    "contact.region": (
        "state", "state/province", "state / province", "state or province", "province",
        "region",
    ),
    "contact.country": ("country", "country of residence", "current country"),
    "links.linkedin": (
        "linkedin", "linkedin profile", "linkedin url", "linkedin profile url", "linkedin link",
    ),
    "links.github": ("github", "github profile", "github url", "github link"),
    "links.portfolio": ("portfolio", "portfolio url", "portfolio link", "portfolio website"),
    "links.website": (
        "website", "personal website", "website url", "other website", "personal site", "blog",
    ),
    "notice_period": ("notice period", "what is your notice period"),
    "relocation": (
        "are you willing to relocate", "willing to relocate", "open to relocation",
        "would you be willing to relocate",
    ),
    "work_authorization": (
        "are you legally authorized to work in the united states",
        "are you authorized to work in the united states",
        "are you legally authorized to work in the us",
        "are you authorized to work in the us",
        "are you legally eligible to work in the united states",
        "work authorization",
    ),
}  # fmt: skip
_BY_LABEL: dict[str, str] = {s: key for key, syns in SYNONYMS.items() for s in syns}
_QUALIFIER = re.compile(r"\s*\((?:optional|required)\)\s*$")


def normalize_label(label: str) -> str:
    """``labels.normalize``, with a trailing "(optional)" or "(required)" dropped."""
    text = labels.normalize(label)
    while True:
        stripped = labels.normalize(_QUALIFIER.sub("", text))
        if stripped == text:
            return text
        text = stripped


# ─── matching a URL to a variant ───────────────────────────────────────────


def variant_for(url: str) -> Variant | None:
    """The form variant a frame URL is on, or None (not a supported ATS form page)."""
    rule = match_ats(url)
    form = FORMS.get(rule.name) if rule else None
    if form is None:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    for v in form.variants:
        if any(host_matches(host, h) for h in v.hosts) and re.search(v.path, parts.path):
            return v
    return None


def ats_of(url: str) -> str | None:
    """The supported ATS a URL is on (Workday included), or None: anywhere else is generic."""
    rule = match_ats(url)
    return rule.name if rule and rule.name in SUPPORTED_ATS else None


# ─── fields ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FieldMatch:
    answer: str | None  # the answer key, or None
    via: Literal["never_store", "field", "synonym", "none"]
    field_key: str | None = None
    never_store: str | None = None  # the never-store category, when via == "never_store"


def field_for(
    label: str,
    names: tuple[str, ...] = (),
    options: tuple[str, ...] = (),
    variant: Variant | None = None,
) -> FieldMatch:
    """Which answer a field takes. Never-store first (``labels.never_store`` on the label, the
    name and id, and a choice group's options); then the variant's named fields by exact name or
    id; then the label synonyms. Anything else maps to nothing."""
    hit = labels.never_store(label, options=options, names=names)
    if hit is not None:
        return FieldMatch(None, "never_store", never_store=hit.category)
    if variant is not None:
        wanted = set(names)
        for f in variant.fields:
            if wanted & set(f.names):
                return FieldMatch(f.answer, "field", field_key=f.key)
    answer = _BY_LABEL.get(normalize_label(label))
    if answer is not None:
        return FieldMatch(answer, "synonym")
    return FieldMatch(None, "none")


# ─── row 0: not visible or not editable ────────────────────────────────────

MIN_BOX_PX = 4


class Visibility(BaseModel):
    """The visibility facts the content script reports in a field descriptor. Any fact left
    out counts against the field: it is not fillable unless every fact says it is."""

    model_config = ConfigDict(extra="forbid")

    check_visibility: bool | None = None  # checkVisibility({checkOpacity, checkVisibilityCSS})
    width: float | None = None
    height: float | None = None
    in_scroll_area: bool | None = None  # its box is inside the document's scroll area
    aria_hidden: bool | None = None  # it or an ancestor has aria-hidden="true"
    inert: bool | None = None  # it or an ancestor is inert
    disabled: bool | None = None
    read_only: bool | None = None


def row0(v: Visibility | None) -> str | None:
    """Why a field is ``listed`` under row 0 (never ticked, never written), or None when it is
    visible and editable. Honeypots fall here."""
    if v is None:
        return "no visibility facts"
    if v.check_visibility is not True:
        return "not visible"
    if v.width is None or v.height is None or v.width < MIN_BOX_PX or v.height < MIN_BOX_PX:
        return "smaller than 4 by 4 px"
    if v.in_scroll_area is not True:
        return "outside the page"
    if v.aria_hidden is not False:
        return "hidden from assistive technology (aria-hidden)"
    if v.inert is not False:
        return "inert"
    if v.disabled is not False:
        return "disabled"
    if v.read_only is not False:
        return "read-only"
    return None


# ─── what the worker caches ────────────────────────────────────────────────


@dataclass(frozen=True)
class _Payload:
    version: str
    ats: dict[str, AtsForm]
    challenge: tuple[str, ...]
    not_submit_words: tuple[str, ...]
    generic: dict[str, Any] = field(default_factory=dict)


def forms_payload() -> dict[str, Any]:
    """The form data for every supported ATS and the generic entry, as JSON-ready data (the
    ``forms`` route, ticket A2). Selectors and patterns only: no values, no event types."""
    payload = _Payload(
        version=FORMS_VERSION,
        ats=FORMS,
        challenge=CHALLENGE,
        not_submit_words=NOT_SUBMIT_WORDS,
        generic={"synonyms": SYNONYMS},
    )
    return asdict(payload)
