"""Request and response models of the extension API, ``/ext/v1/`` (specs/017 phase 1e).

Every model forbids unknown fields and carries its size limits as field constraints, so a
hostile page's JSON-LD cannot grow a request past what the console accepts (the body as a whole
is also capped at ``MAX_BODY_BYTES``, answered with 413). ``schema_document`` is what
``extension/api/v1.schema.json`` holds; a test fails when the checked-in file differs, and
``python -m jobhunter.apply.capture_models > extension/api/v1.schema.json`` regenerates it.
"""

from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

API_VERSION = "v1"
MAX_BODY_BYTES = 1_000_000
MAX_TEXT = 100_000  # page text and selection; detail.MAX_PASTE_CHARS
MAX_STR = 1_000  # other strings
MAX_URL = 2_048  # URLs are never truncated by the extension; a longer one is dropped
MAX_JSONLD = 400_000  # one JobPosting object, re-serialized
MAX_POSTINGS = 5

Text = Annotated[str, StringConstraints(max_length=MAX_TEXT)]
Str = Annotated[str, StringConstraints(max_length=MAX_STR)]
Short = Annotated[str, StringConstraints(max_length=100)]
Url = Annotated[str, StringConstraints(max_length=MAX_URL)]
JsonLd = Annotated[str, StringConstraints(max_length=MAX_JSONLD)]
# crypto.randomUUID(): a lowercase version-4 UUID, minted per user action by the worker.
ActionId = Annotated[
    str,
    StringConstraints(
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
    ),
]
GroupId = Annotated[int, Field(ge=1, le=2**53)]
Trigger = Literal["menu-selection", "menu-page", "popup"]
ApplyMode = Literal["easy_apply", "offsite", "unknown"]
Outcome = Literal["added", "existing", "possible", "previewed", "linked", "same_job"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ─── requests ───────────────────────────────────────────────────────────────


class MicroProp(Strict):
    name: Short
    value: Text


class SiteFacts(Strict):
    """A site-table row's fields (``capture/extract.js``; empty table at launch)."""

    title: Str | None = None
    employer: Str | None = None
    location: Str | None = None
    description: Text | None = None


class ApplyControl(Strict):
    """LinkedIn and Indeed only: the Apply control, read, never clicked."""

    kind: ApplyMode
    label: Str = ""
    href: Url | None = None


class Facts(Strict):
    """What ``capture/extract.js`` read from the one page: raw facts, no decisions."""

    url: Url
    canonical: Url | None = None
    iframes: list[Url] = Field(default_factory=list, max_length=10)
    jsonld: list[JsonLd] = Field(default_factory=list, max_length=MAX_POSTINGS)
    jsonld_dropped: int = Field(default=0, ge=0, le=1000)
    microdata: list[MicroProp] = Field(default_factory=list, max_length=100)
    site: SiteFacts | None = None
    page_text: Text = ""
    og_title: Str | None = None
    og_site_name: Str | None = None
    document_title: Str | None = None
    selection: Text = ""
    apply_control: ApplyControl | None = None
    trigger: Trigger = "popup"
    truncated: list[Short] = Field(default_factory=list, max_length=20)


class BoardCapture(Strict):
    """Click-through: the most recent off-site LinkedIn / Indeed capture the worker holds for
    this window or opener tab (at most 30 minutes old)."""

    group_id: GroupId
    job_id: GroupId | None = None
    board_key: Short
    title: Str = ""
    employer: Str = ""
    minutes_ago: int = Field(default=0, ge=0, le=30)


class CaptureRequest(Strict):
    action_id: ActionId
    facts: Facts
    board_capture: BoardCapture | None = None


class AddRequest(Strict):
    """**Add** after a preview (edited fields), or **Add as new** (``force_new``)."""

    action_id: ActionId
    facts: Facts
    posting_index: int | None = Field(default=None, ge=0, lt=MAX_POSTINGS)
    title: Str
    employer: Str
    description: Text
    source: Literal["structured", "page", "selection", "fetch", "edited"] = "edited"
    force_new: bool = False


class FetchRequest(Strict):
    action_id: ActionId
    url: Url


class DescribeRequest(Strict):
    """**Add this description** to an empty manual group (a capture's mapped fields)."""

    action_id: ActionId
    facts: Facts
    posting_index: int | None = Field(default=None, ge=0, lt=MAX_POSTINGS)
    description: Text = ""
    job_id: GroupId | None = None


class LinkRequest(Strict):
    """**Link them** (``b`` may be the same group, or none for a board-id-only link) or
    **Not the same** (``not_same``)."""

    action_id: ActionId
    a: GroupId
    b: GroupId | None = None
    board_key: Short | None = None
    apply_url: Url | None = None  # the employer's posting the board's Apply led to
    not_same: bool = False


class GroupRequest(Strict):
    """Group routes: the job the popup holds, so a merged-away group can be followed."""

    job_id: GroupId | None = None


class ScoreRequest(Strict):
    action_id: ActionId
    token: Short
    job_id: GroupId | None = None


class PairRequest(Strict):
    code: Short


class VersionRequest(Strict):
    pass


# ─── responses ──────────────────────────────────────────────────────────────


class GroupCard(Strict):
    group_id: int
    job_id: int | None = None
    title: str
    employer: str
    location: str = ""
    salary: str = ""
    posted_at: str | None = None
    host: str = ""
    scored: bool = False
    bucket: str | None = None
    bucket_name: str | None = None
    verdict: str | None = None
    packet_id: int | None = None
    partial: bool = False
    has_description: bool = False
    description_chars: int = 0
    first_lines: str = ""
    score_on_request: bool = False
    manual: bool = False
    why: str | None = None
    path: str  # console path: /job/{id}


class PostingChoice(Strict):
    index: int
    title: str
    employer: str
    url: str | None = None
    first_line: str = ""
    description: str = ""


class Preview(Strict):
    title: str
    employer: str
    location: str = ""
    salary: str = ""
    description: str
    page_description: str | None = None
    selection: str | None = None
    postings: list[PostingChoice] = Field(default_factory=list)
    posting_index: int | None = None
    method: str
    url: str
    reasons: list[str] = Field(default_factory=list)


class FetchOffer(Strict):
    url: str
    host: str
    kind: Literal["fetch", "select"]


class LinkOffer(Strict):
    group_id: int
    board_key: str
    title: str
    employer: str
    minutes_ago: int
    message: str


class BoardInfo(Strict):
    key: str
    board: str
    apply_mode: ApplyMode
    apply_url: str | None = None
    destination_host: str | None = None


class CaptureResponse(Strict):
    outcome: Outcome
    message: str
    method: str | None = None
    group: GroupCard | None = None
    candidates: list[GroupCard] = Field(default_factory=list)
    preview: Preview | None = None
    describe: list[int] = Field(default_factory=list)
    fetch: FetchOffer | None = None
    offer: LinkOffer | None = None
    board: BoardInfo | None = None
    job_id: int | None = None
    url: str | None = None  # the URL chosen for this page's posting
    # same_job only: "pair" links the two candidates (the board posting and the employer's);
    # "pick" asks which one candidate this posting is (never links candidates to each other).
    link_mode: Literal["pair", "pick"] | None = None
    replayed: bool = False


class FetchResponse(Strict):
    url: str
    text: str
    title: str | None = None
    employer: str | None = None


class DescribeResponse(Strict):
    outcome: Literal["description_added"]
    group: GroupCard
    replayed: bool = False


class LinkResponse(Strict):
    outcome: Literal["linked_groups", "not_same"]
    group_id: int | None = None
    message: str
    replayed: bool = False


class EstimateResponse(Strict):
    group_id: int
    scorer: str
    cost_per_job: float
    cost_source: str
    estimated_usd: float
    remaining_usd: float
    batch: bool
    refusal: str | None = None
    token: str
    notice: str | None = None
    prefilter_line: str
    confirm_label: str


class ScoreAccepted(Strict):
    status: Literal["in_progress"]
    group_id: int


class StatusResponse(Strict):
    group_id: int
    state: Literal["scored", "submitted", "in_progress", "stopped", "error", "not_scored"]
    message: str
    bucket: str | None = None
    bucket_name: str | None = None
    verdict: str | None = None
    batch_id: str | None = None
    started_at: str | None = None


class PrepareResponse(Strict):
    packet_id: int
    path: str
    needs_text: bool


class VersionResponse(Strict):
    api: str
    console: str
    min_extension: str
    tracking_params: list[str]
    tracking_prefixes: list[str]
    disabled_hosts: list[str]
    notice_hosts: list[str]  # regular expressions over the host, JS-compatible


class PairResponse(Strict):
    token: str
    extension_id: str


class GoneResponse(Strict):
    gone: Literal[True]
    current_group: int | None = None


class ErrorResponse(Strict):
    error: str
    estimate: EstimateResponse | None = None


ROUTES: dict[str, tuple[type[BaseModel], tuple[type[BaseModel], ...]]] = {
    "pair": (PairRequest, (PairResponse,)),
    "version": (VersionRequest, (VersionResponse,)),
    "capture": (CaptureRequest, (CaptureResponse,)),
    "capture/add": (AddRequest, (CaptureResponse,)),
    "capture/fetch": (FetchRequest, (FetchResponse,)),
    "groups/{id}/describe": (DescribeRequest, (DescribeResponse, GoneResponse)),
    "link": (LinkRequest, (LinkResponse, GoneResponse)),
    "groups/{id}/estimate": (GroupRequest, (EstimateResponse, GoneResponse)),
    "groups/{id}/score": (ScoreRequest, (ScoreAccepted, ErrorResponse, GoneResponse)),
    "groups/{id}/status": (GroupRequest, (StatusResponse, GoneResponse)),
    "groups/{id}/prepare": (GroupRequest, (PrepareResponse, GoneResponse)),
}


def schema_document() -> dict[str, object]:
    """The JSON Schema of every route's request and responses (``extension/api/``)."""
    routes: dict[str, object] = {}
    for path, (req, resps) in ROUTES.items():
        routes[f"/ext/{API_VERSION}/{path}"] = {
            "method": "POST",
            "request": req.model_json_schema(),
            "responses": {r.__name__: r.model_json_schema() for r in resps},
        }
    return {
        "title": "jobhunter extension API",
        "version": API_VERSION,
        "max_body_bytes": MAX_BODY_BYTES,
        "routes": routes,
    }


def schema_json() -> str:
    return json.dumps(schema_document(), indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":  # pragma: no cover
    print(schema_json(), end="")
