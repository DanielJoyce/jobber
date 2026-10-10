"""Decisions-model scorer: TypeSafe Jev through OpenRouter's ``/api/alpha/decisions`` (specs/016).

A decisions model does not write JSON or rationale. It answers typed questions about a
``state``: ``choice`` (one option, with probabilities and confidence), ``score`` (a position
along ordered levels) and ``noul`` (the probability of yes). Following TypeSafe's composite
scoring pattern, each 006 factor is one focused question, and the answers are combined in
Python into the same ``dimensions`` JSON the Haiku screen writes, so ``buckets.compute_row``,
the inbox and ``eval`` read decisions rows unchanged.

Several jobs share one request: ``state = {"candidate": {...}, "jobs": [...]}`` and every
question id is namespaced ``"<custom_id>.<question>"`` with instructions that point at
``jobs[i]`` by a backtick path. Questions are evaluated independently, so batching changes
cost, not answers.

Never asked: anything about pay or location fit. Those are computed in Python (specs/014).
The candidate state carries the same fields as ``profile.scoring_inputs``: resume, current
focus, done-with and narrative; no salary, states, weights or thresholds.

There is no evidence quote to verify. Rows are written with ``evidence = []``,
``evidence_unverified = 0`` and ``evidence_mode = 'none'``; the raw answers (probabilities and
confidence per question) go to ``fit_score.decisions`` for the detail page, and ``eval``
leaves these rows out of the hallucinated-quote rate.

Like ``scorers.py``, this module is an API client, not a scraper, so it may use ``httpx``.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sqlite3
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

from jobhunter.config import OpenRouter
from jobhunter.core import rejections
from jobhunter.core.models import LocationScope, Verdict
from jobhunter.pipeline.listing import _txn, to_iso
from jobhunter.scoring.profile import Profile
from jobhunter.scoring.scorers import ScoreRequest, ScorerError, ScoreResult, Usage, _usage_int

logger = logging.getLogger(__name__)

TIER = "screen"
DECISIONS_PROMPT_VERSION = "decisions-v1"
PROVIDERS = ("jev", "decisions")
JEV_DEFAULT_MODEL = "typesafe/jev-1.13"
DEFAULT_JOBS_PER_REQUEST = 8
MAX_JOBS_PER_REQUEST = 25
DEFAULT_MAX_INPUT_TOKENS = 60_000
# Observed 2026-10-09: 451 input tokens cost $0.0000189, about $0.042 per million input
# tokens, and output is free. Used only when a response omits usage.cost, and for the
# pre-request spend-cap estimate.
FALLBACK_USD_PER_MTOK_INPUT = 0.042
CHARS_PER_TOKEN = 4  # rough estimate for the per-request input cap

NOUL_YES = 0.5  # done_with hit
FLAG_YES = 0.6  # shape flag (narrative avoid / dealbreakers) and blocker
STATED_NO = 0.4  # missing_info: the posting does not state it
LOW_CONFIDENCE = 0.4

# ─── Levels (low -> high), from the 006 / rubric anchors ───────────────────

SKILLS_LEVELS = [
    "Unrelated work: none of the duties or named tools appear in the candidate material",
    "Same broad field, different discipline (for example infrastructure versus front-end "
    "development, or network engineering versus data science)",
    "Transferable: the candidate would be learning a meaningful part of the job on the job",
    "Clearly capable: does most of this work, but one or two of the named tools are adjacent "
    "rather than held (a different cloud, a sibling configuration tool, a related language)",
    "Does the work described daily: the named tools are the candidate's primary stack",
]
LEVELS = [
    "Entry level or junior: trainee, apprentice, junior or level I roles with close supervision",
    "Mid level: works independently on defined tasks, a few years of experience",
    "Senior individual contributor: owns systems or projects end to end and mentors others",
    "Lead or staff: technical lead across a team or a large system, sets technical direction",
    "Principal, architect, manager of managers or executive: organisation-wide scope",
]
DOMAIN_LEVELS = [
    "A domain the candidate has no connection to, or one their own narrative rules out",
    "A distant domain with little shared context",
    "A different domain where the craft transfers: the candidate would learn the domain, "
    "not the job",
    "A neighbouring domain: the vocabulary and constraints mostly carry over",
    "The same domain and kind of organisation the candidate works in now",
]
FOCUS_LEVELS = [
    "Little or none of the job is current-focus work",
    "Some overlap: current-focus work is a minority of the job",
    "A large part of the job is current-focus work",
    "The job is substantially the candidate's current focus",
]
VERDICT_OPTIONS = {
    "strong": "A strong fit: the candidate could do this job now and it is the kind of work "
    "they want",
    "possible": "A plausible fit with one or two real gaps",
    "weak": "A weak fit: large gaps in skills, level or domain",
    "mismatch": "Not the candidate's field",
    "other": "None of these fits, or the posting is too thin to judge",
}
DIRECTION_OPTIONS = {
    "below": "The job is more junior than the candidate's current level",
    "match": "The job is at about the candidate's current level",
    "above": "The job is more senior than the candidate's current level",
    "unclear": "The posting does not show its level",
}
# Posting facts that matter to the decision. Asked as "does the posting state ...", never as
# fit: pay and location fit stay in Python (specs/014).
STATED = {
    "salary": ("a salary or pay range", "salary or pay range not stated"),
    "location": ("where the work is located", "work location not stated"),
    "remote_policy": (
        "whether the job is remote, hybrid or on-site",
        "remote or on-site policy not stated",
    ),
}
_DIMENSION_SCORES = ("skills_raw", "skills_recent", "job_level", "domain", "focus_overlap")


class DecisionsError(ScorerError):
    """One decisions request failed. ``status`` is the HTTP status, 0 for transport errors."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


# ─── Answers ────────────────────────────────────────────────────────────────


def position(answer: Mapping[str, Any], n_levels: int) -> float:
    """A score answer as a 0-1 position along ``n_levels`` ordered levels.

    Computed from ``probabilities`` (sum of level x probability over the top level), which is
    unambiguous. The docs describe ``score`` as 0..top-level while the live API returned 0-1,
    so ``score`` is only a fallback: values above 1 are divided by the top level.
    """
    top = max(n_levels - 1, 1)
    probs = answer.get("probabilities")
    if isinstance(probs, Mapping) and probs:
        total = 0.0
        acc = 0.0
        for key, p in probs.items():
            try:
                acc += int(key) * float(p)
                total += float(p)
            except (TypeError, ValueError):
                continue
        if total > 0:
            return max(0.0, min(1.0, acc / total / top))
    raw = answer.get("score")
    if isinstance(raw, int | float):
        value = float(raw) / top if raw > 1 else float(raw)
        return max(0.0, min(1.0, value))
    raise KeyError("score answer has neither probabilities nor score")


def noul(answer: Mapping[str, Any]) -> float:
    value = answer.get("noul")
    if not isinstance(value, int | float):
        raise KeyError("noul answer has no value")
    return float(value)


def confidence(answer: Mapping[str, Any]) -> float | None:
    """Reported confidence; for a noul, ``|2p - 1|`` (TypeSafe's suggested equivalent)."""
    value = answer.get("confidence")
    if isinstance(value, int | float):
        return float(value)
    if answer.get("type") == "noul" and isinstance(answer.get("noul"), int | float):
        return abs(2 * float(answer["noul"]) - 1)
    return None


def _top_level(answer: Mapping[str, Any], levels: Sequence[str]) -> tuple[str, float]:
    probs = answer.get("probabilities") or {}
    best, best_p = 0, -1.0
    for key, p in probs.items():
        try:
            if float(p) > best_p:
                best, best_p = int(key), float(p)
        except (TypeError, ValueError):
            continue
    best = max(0, min(best, len(levels) - 1))
    return levels[best], max(best_p, 0.0)


# ─── Questions ──────────────────────────────────────────────────────────────

_SLUG = re.compile(r"[^a-z0-9]+")
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*")


def slug(text: str, *, words: int = 6) -> str:
    """Stable short id for a profile line: lowercase words joined by underscores."""
    parts = [p for p in _SLUG.split(text.casefold()) if p][:words]
    return "_".join(parts)[:48].strip("_") or "item"


def split_items(text: str | None) -> list[str]:
    """Lines (or bullets) of a narrative block, one item each.

    Blank lines, ``#`` comment or heading lines (a profile template's placeholder prompt) and
    lead-in lines ending in ``:`` are not items and are dropped.
    """
    out: list[str] = []
    for raw in (text or "").splitlines():
        if raw.lstrip().startswith("#"):
            continue
        line = _BULLET.sub("", raw).strip()
        if line and not line.endswith(":"):
            out.append(line)
    return out


def _unique(items: Sequence[str]) -> list[tuple[str, str]]:
    """(slug, text) pairs with slugs made unique by a numeric suffix, order kept."""
    seen: dict[str, int] = {}
    out: list[tuple[str, str]] = []
    for text in items:
        base = slug(text)
        n = seen.get(base, 0)
        seen[base] = n + 1
        out.append((base if n == 0 else f"{base}_{n + 1}", text))
    return out


@dataclass(frozen=True)
class ProfileItems:
    """The per-profile fan-out lists, with stable ids."""

    done_with: list[tuple[str, str]]
    avoid: list[tuple[str, str]]  # narrative.avoid + dealbreakers_soft lines
    requires: list[tuple[str, str]]  # hard.requires_i_lack credential keys -> readable text


def profile_items(profile: Profile) -> ProfileItems:
    focus = profile.current_focus
    done = list(focus.done_with) if focus is not None else []
    avoid = split_items(profile.narrative.avoid) + split_items(profile.narrative.dealbreakers_soft)
    requires = [(slug(k), k.replace("_", " ")) for k in profile.hard.requires_i_lack]
    return ProfileItems(_unique(done), _unique(avoid), requires)


def candidate_state(profile: Profile) -> dict[str, Any]:
    """The candidate half of ``state``: the ``scoring_inputs`` fields, nothing filter-related."""
    focus = profile.current_focus
    out: dict[str, Any] = {"resume": profile.resume_text.strip()}
    if focus is not None:
        cf: dict[str, Any] = {}
        if focus.since:
            cf["since"] = focus.since
        if focus.doing:
            cf["doing"] = focus.doing.strip()
        if focus.want_more_of:
            cf["want_more_of"] = list(focus.want_more_of)
        if cf:
            out["current_focus"] = cf
        if focus.done_with:
            out["done_with"] = list(focus.done_with)
    n = profile.narrative
    for key, text in (
        ("want", n.want),
        ("avoid", n.avoid),
        ("dealbreakers", n.dealbreakers_soft),
        ("context", n.context),
    ):
        if text and text.strip():
            out[key] = text.strip()
    return out


def _employment(value: str | None) -> str:
    return "not stated" if not value or value == "unknown" else value.replace("_", " ")


def job_state(job: Mapping[str, Any], custom_id: str, locations_summary: str) -> dict[str, Any]:
    """One entry of ``state.jobs``. No salary: pay is not the model's question."""
    scope = job.get("location_scope") or LocationScope.unknown.value
    entry: dict[str, Any] = {
        "id": custom_id,
        "title": job.get("title") or "not stated",
        "employer": job.get("employer") or job.get("agency_raw") or "not stated",
        "location": f"{locations_summary} (scope: {scope})",
        "employment_type": _employment(job.get("employment_type")),
        "remote": job.get("remote") or "unknown",
        "description": (job.get("description_text") or "").strip() or "(no description provided)",
    }
    if job.get("description_completeness") == "partial":
        entry["description_note"] = (
            "partial: a summary or truncated listing; the full posting may say more"
        )
    if job.get("prior_rejection"):
        # A neutral fact about a different role at this employer (specs/006 "Employer
        # rejections"). No question asks about it; it is context, never pay or location.
        entry["employer_history"] = str(job["prior_rejection"])
    return entry


def _score_q(instructions: str, levels: Sequence[str]) -> dict[str, Any]:
    return {"type": "score", "instructions": instructions, "criteria": list(levels)}


def _noul_q(instructions: str, yes: str | None = None, no: str | None = None) -> dict[str, Any]:
    q: dict[str, Any] = {"type": "noul", "instructions": instructions}
    if yes and no:
        q["criteria"] = {"true": yes, "false": no}
    return q


def candidate_questions(profile: Profile) -> dict[str, dict[str, Any]]:
    """Asked once per request: the candidate's own level, the reference for seniority."""
    return {
        "candidate.level": _score_q(
            "What is the candidate's current professional level, judged from "
            "`candidate.resume` and `candidate.context`?",
            LEVELS,
        )
    }


def job_questions(
    custom_id: str, index: int, profile: Profile, items: ProfileItems
) -> dict[str, dict[str, Any]]:
    """Every question about ``jobs[index]``, ids namespaced ``"<custom_id>.<question>"``."""
    j = f"`jobs[{index}]`"
    has_focus = bool(profile.current_focus and profile.current_focus.doing)
    recent_basis = (
        "judged ONLY against the candidate's recent work in `candidate.current_focus` "
        "(roughly the last three years), ignoring older experience in the resume"
        if has_focus
        else "judged against the candidate's roles in `candidate.resume` from the last three "
        "years only, ignoring older experience"
    )
    qs: dict[str, dict[str, Any]] = {
        "skills_raw": _score_q(
            f"How well can the candidate do the work described in {j}, judged against "
            "everything in `candidate.resume` at any time, ignoring dates?",
            SKILLS_LEVELS,
        ),
        "skills_recent": _score_q(
            f"How well can the candidate do the work described in {j}, {recent_basis}?",
            SKILLS_LEVELS,
        ),
        "job_level": _score_q(
            f"What professional level does {j} require, from its title, duties, scope and "
            "required years of experience?",
            LEVELS,
        ),
        "seniority_direction": {
            "type": "choice",
            "instructions": f"Is {j} below, at or above the level of the candidate described "
            "in `candidate.resume` and `candidate.context`?",
            "criteria": dict(DIRECTION_OPTIONS),
        },
        "domain": _score_q(
            f"How familiar is the domain and kind of organisation of {j} (industry, sector, "
            "mission) to the candidate, given `candidate.resume` and `candidate.want`?",
            DOMAIN_LEVELS,
        ),
        "focus_overlap": _score_q(
            f"How much of the work in {j} is the work in `candidate.current_focus`?"
            if has_focus
            else f"How much of the work in {j} is the candidate's most recent role in "
            "`candidate.resume`?",
            FOCUS_LEVELS,
        ),
        "verdict": {
            "type": "choice",
            "instructions": f"Overall, how well does {j} fit the candidate in `candidate`, "
            "considering skills, level and domain only (not pay or location)?",
            "criteria": dict(VERDICT_OPTIONS),
        },
    }
    for key, text in items.done_with:
        qs[f"done_with.{key}"] = _noul_q(
            f"Is {text} a substantial part of the work in {j} (a duty or requirement, not a "
            "passing mention)?"
        )
    for key, text in items.avoid:
        qs[f"avoid.{key}"] = _noul_q(
            f"Does {j} itself show this shape the candidate wants to avoid: {text}",
            "The posting's duties, schedule or requirements show it",
            "Not shown, or only hinted at",
        )
    for key, text in items.requires:
        qs[f"requires.{key}"] = _noul_q(
            f"Does {j} state {text} as a hard requirement?",
            "Required: a minimum qualification or condition of employment",
            "Not mentioned, or only preferred or nice to have",
        )
    for key, (what, _) in STATED.items():
        qs[f"states.{key}"] = _noul_q(f"Does {j} state {what}?")
    return {f"{custom_id}.{qid}": q for qid, q in qs.items()}


def build_body(
    model: str,
    profile: Profile,
    jobs: Sequence[tuple[str, Mapping[str, Any], str]],
    *,
    items: ProfileItems | None = None,
) -> dict[str, Any]:
    """One decisions request for ``jobs`` = [(custom_id, job row, locations summary), ...]."""
    items = items or profile_items(profile)
    questions = candidate_questions(profile)
    state_jobs = []
    for i, (cid, job, summary) in enumerate(jobs):
        state_jobs.append(job_state(job, cid, summary))
        questions.update(job_questions(cid, i, profile, items))
    return {
        "model": model,
        "state": {"candidate": candidate_state(profile), "jobs": state_jobs},
        "questions": questions,
    }


def estimate_tokens(obj: Any) -> int:
    return math.ceil(len(json.dumps(obj, ensure_ascii=False)) / CHARS_PER_TOKEN)


def pack(
    profile: Profile,
    jobs: Sequence[tuple[str, Mapping[str, Any], str]],
    *,
    jobs_per_request: int,
    max_input_tokens: int,
) -> list[list[tuple[str, Mapping[str, Any], str]]]:
    """Split jobs into requests of at most ``jobs_per_request`` and about ``max_input_tokens``.

    The candidate block and per-request questions count once per request; each job adds its
    state entry and its questions. A job over the cap on its own still gets a request.
    """
    n = max(1, min(jobs_per_request, MAX_JOBS_PER_REQUEST))
    items = profile_items(profile)
    base = estimate_tokens(candidate_state(profile)) + estimate_tokens(candidate_questions(profile))
    out: list[list[tuple[str, Mapping[str, Any], str]]] = []
    cur: list[tuple[str, Mapping[str, Any], str]] = []
    used = base
    for cid, job, summary in jobs:
        cost = estimate_tokens(job_state(job, cid, summary)) + estimate_tokens(
            job_questions(cid, len(cur), profile, items)
        )
        if cur and (len(cur) >= n or used + cost > max_input_tokens):
            out.append(cur)
            cur, used = [], base
        cur.append((cid, job, summary))
        used += cost
    if cur:
        out.append(cur)
    return out


# ─── Client ─────────────────────────────────────────────────────────────────


@dataclass
class DecisionsResponse:
    answers: dict[str, dict[str, Any]]
    usage: Usage
    model: str
    id: str = ""
    provider: str = ""


def format_api_error(status: int, text: str) -> str:
    """An error body as one line. Zod validation issues (a JSON list in ``error.message``)
    become ``path: message`` pairs."""
    message: Any = text
    try:
        data = json.loads(text)
        err = data.get("error", data) if isinstance(data, dict) else data
        message = err.get("message", err) if isinstance(err, dict) else err
    except ValueError:
        pass
    issues: Any = message
    if isinstance(message, str):
        try:
            issues = json.loads(message)
        except ValueError:
            issues = message
    if isinstance(issues, list) and issues and all(isinstance(i, dict) for i in issues):
        parts = []
        for issue in issues:
            path = ".".join(str(p) for p in issue.get("path") or []) or "(body)"
            parts.append(f"{path}: {issue.get('message') or issue.get('code') or issue}")
        return f"HTTP {status} invalid request: " + "; ".join(parts)
    return f"HTTP {status}: {str(message)[:500]}"


class DecisionsScorer:
    """``jev:<slug>`` / ``decisions:<slug>``: synchronous, many jobs per request.

    It satisfies the ``FitScorer`` protocol for the factory and the CLI, but it does not take
    a rubric prompt: ``screen.score_sync`` hands it to ``score_decisions`` (``evidence_mode``
    is ``"none"``), and ``score_one`` / ``submit`` refuse a prompt-shaped request.
    """

    supports_batching = False
    evidence_mode = "none"

    def __init__(
        self,
        model: str,
        config: OpenRouter,
        *,
        name: str | None = None,
        client: httpx.Client | None = None,
        env: Mapping[str, str] | None = None,
        jobs_per_request: int = DEFAULT_JOBS_PER_REQUEST,
        max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS,
    ) -> None:
        self.model = model
        self.name = name or f"decisions:{model}"
        self.config = config
        self.client = client if client is not None else httpx.Client(timeout=180.0)
        self._env = env
        self.jobs_per_request = jobs_per_request
        self.max_input_tokens = max_input_tokens

    @property
    def url(self) -> str:
        base = self.config.base_url.rstrip("/").removesuffix("/v1")
        return f"{base}/alpha/decisions"

    def _headers(self) -> dict[str, str]:
        env = self._env if self._env is not None else os.environ
        key = env.get(self.config.api_key_env)
        if not key:
            raise ScorerError(
                f"environment variable {self.config.api_key_env} (scoring.openrouter) is not set"
            )
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "X-Title": self.config.title,
        }
        if self.config.referer:
            headers["HTTP-Referer"] = self.config.referer
        return headers

    def provider_prefs(self) -> dict[str, Any]:
        """Only ``data_collection`` carries over: ``require_parameters`` is a chat concept."""
        dc = self.config.provider.get("data_collection")
        return {"data_collection": dc} if dc else {}

    def decide(self, body: Mapping[str, Any]) -> DecisionsResponse:
        """POST one request. Raises ``DecisionsError`` with a readable message on failure."""
        payload = dict(body)
        if prefs := self.provider_prefs():
            payload.setdefault("provider", prefs)
        try:
            resp = self.client.post(self.url, json=payload, headers=self._headers())
        except httpx.HTTPError as exc:
            raise DecisionsError(f"transport error: {exc}") from exc
        if resp.status_code != 200:
            raise DecisionsError(format_api_error(resp.status_code, resp.text), resp.status_code)
        try:
            data = resp.json()
            answers = data["answers"]
            if not isinstance(answers, dict):
                raise TypeError("answers is not an object")
        except (ValueError, KeyError, TypeError) as exc:
            raise DecisionsError(f"malformed response: {exc}", resp.status_code) from exc
        raw_usage = data.get("usage") or {}
        cost = raw_usage.get("cost")
        served = str(data.get("model") or self.model)
        usage = Usage(
            input_tokens=int(raw_usage.get("input_tokens") or 0),
            output_tokens=int(raw_usage.get("output_tokens") or 0),
            cost_usd=float(cost)
            if isinstance(cost, int | float) and not isinstance(cost, bool) and cost >= 0
            else None,
            model=served,
        )
        return DecisionsResponse(
            answers={str(k): v for k, v in answers.items() if isinstance(v, dict)},
            usage=usage,
            model=served,
            id=str(data.get("id") or ""),
            provider=str(data.get("provider") or ""),
        )

    # FitScorer protocol ------------------------------------------------------

    def submit(self, requests: list[ScoreRequest]) -> list[ScoreResult]:
        raise ScorerError(f"{self.name} answers questions, not prompts: use score_decisions")

    def score_one(self, request: ScoreRequest) -> ScoreResult:
        raise ScorerError(f"{self.name} answers questions, not prompts: use score_decisions")

    def ready(self, handle: str) -> bool:
        return True

    def collect(self, handle: str) -> list[ScoreResult]:
        raise ScorerError(f"{self.name} does not batch; there is nothing to collect")

    def cost(self, usage: Any, *, batch: bool = False) -> float:
        reported = getattr(usage, "cost_usd", None)
        if reported is not None:
            return float(reported)
        return _usage_int(usage, "input_tokens") * FALLBACK_USD_PER_MTOK_INPUT / 1_000_000

    def estimate_cost(self, body: Mapping[str, Any]) -> float:
        return estimate_tokens(body) * FALLBACK_USD_PER_MTOK_INPUT / 1_000_000


def decisions_scorer(
    spec: str,
    config: OpenRouter,
    *,
    client: httpx.Client | None = None,
    env: Mapping[str, str] | None = None,
) -> DecisionsScorer:
    """``"jev:typesafe/jev-1.13"`` or ``"decisions:<slug>"``; ``"jev:"`` alone is not allowed."""
    provider, _, slug_ = spec.partition(":")
    if provider not in PROVIDERS or not slug_:
        raise ScorerError(f"not a decisions scorer: {spec!r}")
    return DecisionsScorer(slug_, config, name=spec, client=client, env=env)


def privacy_notice(spec: str) -> str:
    return (
        f"notice: scorer {spec} sends your resume, current focus, done-with list and narrative, "
        "and the job postings, to openrouter.ai and TypeSafe (the decisions model provider); "
        "no salary or location preferences are sent (specs/008 personal data, specs/016)"
    )


# ─── Mapping answers to the dimensions JSON ────────────────────────────────


class MissingAnswers(Exception):
    """A job's questions are not all answered; it stays eligible."""


def _pct(x: float) -> int:
    return max(0, min(100, round(100 * x)))


def _why(answer: Mapping[str, Any], levels: Sequence[str]) -> str:
    text, p = _top_level(answer, levels)
    conf = confidence(answer)
    tail = f", confidence {conf:.2f}" if conf is not None else ""
    return f"Jev: {text} (p {p:.2f}{tail})"[:300]


@dataclass
class JobDecision:
    verdict: str  # strong / possible / weak / mismatch (fit_score.verdict)
    dimensions: dict[str, Any]
    blockers: list[str]
    missing_info: list[str]
    shape_flags: list[str]
    report: dict[str, Any]


def seniority_score(job_level: float, candidate_level: float, n_levels: int = len(LEVELS)) -> int:
    """``100 - 25 x |level gap|`` on the five-level scale, clamped to 0..100.

    Both inputs are 0-1 positions. One level off scores 75 ("one step off", 70-89 in the
    rubric), two levels 50 (40-69), three 25 (10-39), four 0.
    """
    gap = abs(job_level - candidate_level) * (n_levels - 1)
    return _pct(1 - gap / (n_levels - 1))


def map_job(
    custom_id: str,
    answers: Mapping[str, Mapping[str, Any]],
    candidate: Mapping[str, Mapping[str, Any]],
    profile: Profile,
    items: ProfileItems,
) -> JobDecision:
    """Turn one job's answers into the dimensions JSON ``buckets.compute_row`` reads.

    Raises ``MissingAnswers`` when any of the job's questions (or the candidate level) is
    absent, so a partly answered job is written not at all rather than half-scored.
    """
    prefix = f"{custom_id}."
    mine = {k[len(prefix) :]: v for k, v in answers.items() if k.startswith(prefix)}
    expected = set(job_questions(custom_id, 0, profile, items))
    missing = sorted(k[len(prefix) :] for k in expected if k[len(prefix) :] not in mine)
    if missing or "candidate.level" not in candidate:
        raise MissingAnswers(", ".join(missing or ["candidate.level"]))
    try:
        raw = position(mine["skills_raw"], len(SKILLS_LEVELS))
        recent = position(mine["skills_recent"], len(SKILLS_LEVELS))
        job_lvl = position(mine["job_level"], len(LEVELS))
        cand_lvl = position(candidate["candidate.level"], len(LEVELS))
        domain = position(mine["domain"], len(DOMAIN_LEVELS))
        focus = position(mine["focus_overlap"], len(FOCUS_LEVELS))
        nouls = {k: noul(v) for k, v in mine.items() if v.get("type") == "noul" or "noul" in v}
    except KeyError as exc:
        raise MissingAnswers(str(exc)) from exc

    direction = str(mine["seniority_direction"].get("choice") or "unclear")
    if direction not in ("below", "match", "above"):
        gap = (job_lvl - cand_lvl) * (len(LEVELS) - 1)
        direction = "above" if gap > 0.5 else "below" if gap < -0.5 else "match"

    seniority = seniority_score(job_lvl, cand_lvl)
    raw_i, recent_i = _pct(raw), _pct(recent)
    done_hits = [text for key, text in items.done_with if nouls[f"done_with.{key}"] >= NOUL_YES]
    flags = [key for key, _ in items.avoid if nouls[f"avoid.{key}"] >= FLAG_YES]
    if done_hits:
        flags.append("done_with")
    if raw_i >= 70 and recent_i < 55:
        flags.append("stale_match")
    blockers = [text for key, text in items.requires if nouls[f"requires.{key}"] >= FLAG_YES]
    missing_info = [msg for key, (_, msg) in STATED.items() if nouls[f"states.{key}"] < STATED_NO]

    verdict_ans = mine["verdict"]
    probs = verdict_ans.get("probabilities") or {}
    ranked = sorted(
        (v.value for v in Verdict), key=lambda v: float(probs.get(v, 0.0) or 0.0), reverse=True
    )
    verdict = ranked[0] if probs else "mismatch"
    if verdict_ans.get("choice") in {v.value for v in Verdict}:
        verdict = str(verdict_ans["choice"])

    confs = [confidence(mine[q]) for q in _DIMENSION_SCORES]
    low = [c for c in confs if c is not None and c < LOW_CONFIDENCE]
    v_conf = confidence(verdict_ans)
    if (v_conf is not None and v_conf < LOW_CONFIDENCE) or len(low) * 2 > len(confs):
        flags.append("low_confidence")

    dims: dict[str, Any] = {
        "skills": {"score": recent_i, "why": _why(mine["skills_recent"], SKILLS_LEVELS)},
        "seniority": {
            "score": seniority,
            "why": f"{direction}: " + _why(mine["job_level"], LEVELS),
            "direction": direction,
        },
        "domain": {"score": _pct(domain), "why": _why(mine["domain"], DOMAIN_LEVELS)},
        "raw_skills": raw_i,
        "recency_weighted_skills": recent_i,
        "current_focus_overlap": _pct(focus),
        "stale_skills": [],
        "done_with_hits": done_hits,
        "seniority_direction": direction,
        "evidence_unverified": False,
        "evidence_mode": "none",
    }
    labels: dict[str, str] = {}
    labels.update({f"done_with.{k}": t for k, t in items.done_with})
    labels.update({f"avoid.{k}": t for k, t in items.avoid})
    labels.update({f"requires.{k}": t for k, t in items.requires})
    labels.update({f"states.{k}": f"states {w}" for k, (w, _) in STATED.items()})
    report = {
        "verdict": {
            "choice": verdict_ans.get("choice"),
            "probabilities": probs,
            "confidence": v_conf,
        },
        "answers": {"candidate.level": dict(candidate["candidate.level"]), **mine},
        "labels": labels,
        "positions": {
            "skills_raw": round(raw, 4),
            "skills_recent": round(recent, 4),
            "job_level": round(job_lvl, 4),
            "candidate_level": round(cand_lvl, 4),
            "domain": round(domain, 4),
            "focus_overlap": round(focus, 4),
        },
    }
    return JobDecision(verdict, dims, blockers, missing_info, flags, report)


# ─── Driver ─────────────────────────────────────────────────────────────────


@dataclass
class DecisionsResult:
    """``score_sync``'s counters plus what a packed decisions run adds."""

    submitted: int = 0
    written: int = 0
    duplicate: int = 0
    invalid: int = 0
    errored: int = 0
    unverified: int = 0
    cost_usd: float = 0.0
    requests: int = 0
    capped: bool = False
    served_models: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _write(
    conn: sqlite3.Connection,
    group_id: int,
    input_rev: int,
    decision: JobDecision,
    *,
    scorer: str,
    scoring_version: str,
    served_model: str,
    cost: float,
    input_tokens: int,
    output_tokens: int,
    now: datetime,
    request_meta: Mapping[str, Any],
) -> int | None:
    report = {**decision.report, **request_meta}
    cur = conn.execute(
        "INSERT INTO fit_score (job_group_id, tier, model, prompt_version, scoring_version, "
        "input_rev, verdict, overall, dimensions, evidence, blockers, missing_info, "
        "tailoring_hints, shape_flags, evidence_unverified, input_tokens, output_tokens, "
        "cache_read_tokens, cost_usd, batch_id, created_at, served_model, evidence_mode, "
        "decisions) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, '[]', ?, ?, '[]', ?, 0, ?, ?, 0, ?, "
        "NULL, ?, ?, 'none', ?) "
        "ON CONFLICT (job_group_id, tier, prompt_version, scoring_version, model, input_rev) "
        "DO NOTHING",
        (
            group_id,
            TIER,
            scorer,
            DECISIONS_PROMPT_VERSION,
            scoring_version,
            input_rev,
            decision.verdict,
            json.dumps(decision.dimensions),
            json.dumps(decision.blockers),
            json.dumps(decision.missing_info),
            json.dumps(decision.shape_flags),
            input_tokens,
            output_tokens,
            cost,
            to_iso(now),
            served_model or None,
            json.dumps(report),
        ),
    )
    if cur.rowcount == 0:
        return None
    conn.execute(
        "UPDATE job SET stage = 'scored' WHERE job_group_id = ? AND stage = 'prefiltered'",
        (group_id,),
    )
    return cur.lastrowid


def eligible(
    conn: sqlite3.Connection,
    profile: Profile,
    scorer: str,
    limit: int,
    *,
    group_ids: Sequence[int] | None = None,
    new_only: bool = False,
) -> list[Any]:
    """``screen.eligible_groups`` under the decisions prompt version (rejected postings are
    never eligible; ``new_only`` as there)."""
    from jobhunter.scoring import screen

    return conn.execute(
        screen._ELIGIBLE,
        {
            "filter_version": profile.filter_version,
            "tier": TIER,
            "model": scorer,
            "prompt_version": DECISIONS_PROMPT_VERSION,
            "scoring_version": profile.scoring_version,
            "limit": limit,
            "rejected": rejections.rejected_json(conn),
            "only": screen.only_json(group_ids),
            "new_only": int(new_only),
        },
    ).fetchall()


def _jobs(
    conn: sqlite3.Connection,
    groups: Sequence[Any],
    now: datetime | None = None,
    rejection_days: int = rejections.DEFAULT_WINDOW_DAYS,
) -> list[tuple[str, dict[str, Any], str]]:
    from jobhunter.scoring import screen

    rows = screen._context_rows(conn, groups, now, rejection_days)
    return [(f"g{r['group_id']}", r, screen._summary_for(conn, r)) for r in rows]


def score_decisions(
    conn: sqlite3.Connection,
    scorer: DecisionsScorer,
    profile: Profile,
    *,
    limit: int,
    now: datetime,
    remaining_usd: Callable[[], float] | None = None,
    rejection_days: int = rejections.DEFAULT_WINDOW_DAYS,
    group_ids: Sequence[int] | None = None,
    new_only: bool = False,
) -> DecisionsResult:
    """Score up to ``limit`` eligible groups, ``scorer.jobs_per_request`` per request.

    Before each request the spend cap is checked against an estimate of that request's cost.
    A failed request (HTTP error, zod 400, transport) writes nothing, so its jobs stay
    eligible; a job whose answers are incomplete is likewise skipped. The request's reported
    cost is split evenly over its jobs, and the whole request lands in ``llm_spend``.
    """
    from jobhunter.scoring.screen import _record_spend

    out = DecisionsResult()
    groups = eligible(conn, profile, scorer.name, limit, group_ids=group_ids, new_only=new_only)
    if not groups:
        return out
    by_cid = {f"g{g['group_id']}": g for g in groups}
    items = profile_items(profile)
    batches = pack(
        profile,
        _jobs(conn, groups, now, rejection_days),
        jobs_per_request=scorer.jobs_per_request,
        max_input_tokens=scorer.max_input_tokens,
    )
    for batch in batches:
        body = build_body(scorer.model, profile, batch, items=items)
        if remaining_usd is not None and remaining_usd() < scorer.estimate_cost(body):
            logger.warning("spend cap reached: %d requests not sent", len(batches) - out.requests)
            out.capped = True
            break
        out.requests += 1
        out.submitted += len(batch)
        try:
            resp = scorer.decide(body)
        except DecisionsError as exc:
            if exc.status in (401, 402, 403):
                raise
            out.errored += len(batch)
            out.errors.append(str(exc))
            logger.warning("%s: request for %d jobs failed: %s", scorer.name, len(batch), exc)
            continue
        cost = scorer.cost(resp.usage)
        share = cost / len(batch)
        in_share = resp.usage.input_tokens // len(batch)
        out_share = resp.usage.output_tokens // len(batch)
        out.cost_usd += cost
        out.served_models[resp.model] = out.served_models.get(resp.model, 0) + len(batch)
        cand = {k: v for k, v in resp.answers.items() if k.startswith("candidate.")}
        meta = {"request_id": resp.id, "provider": resp.provider, "jobs_in_request": len(batch)}
        with _txn(conn):
            for cid, _job, _summary in batch:
                g = by_cid[cid]
                try:
                    decision = map_job(cid, resp.answers, cand, profile, items)
                except MissingAnswers as exc:
                    out.invalid += 1
                    logger.warning("%s %s: unanswered questions: %s", scorer.name, cid, exc)
                    continue
                row = _write(
                    conn,
                    g["group_id"],
                    g["input_rev"],
                    decision,
                    scorer=scorer.name,
                    scoring_version=profile.scoring_version,
                    served_model=resp.model,
                    cost=share,
                    input_tokens=in_share,
                    output_tokens=out_share,
                    now=now,
                    request_meta=meta,
                )
                if row is None:
                    out.duplicate += 1
                else:
                    out.written += 1
            _record_spend(
                conn,
                scorer.name,
                now,
                1,
                resp.usage.input_tokens,
                resp.usage.output_tokens,
                cost,
            )
    return out


# ─── Bench (writes nothing) ─────────────────────────────────────────────────


def run_bench(
    conn: sqlite3.Connection,
    scorer: DecisionsScorer,
    profile: Profile,
    *,
    n: int,
    night_hours: float = 8.0,
    clock: Callable[[], float] = time.monotonic,
) -> Any:
    """``jobhunter llm bench`` for a decisions scorer: packed requests, nothing stored.

    ``seconds`` holds per-job time (request time over its jobs). "Schema-valid" counts jobs
    whose questions were all answered. There are no evidence quotes to verify.
    """
    from jobhunter.scoring.bench import _GROUPS, BenchReport

    conn.execute("PRAGMA query_only = ON")
    try:
        groups = conn.execute(
            _GROUPS, (profile.filter_version, rejections.rejected_json(conn), n)
        ).fetchall()
        report = BenchReport(scorer=scorer.name, requested=n, night_hours=night_hours)
        items = profile_items(profile)
        batches = pack(
            profile,
            _jobs(conn, groups),
            jobs_per_request=scorer.jobs_per_request,
            max_input_tokens=scorer.max_input_tokens,
        )
        for batch in batches:
            body = build_body(scorer.model, profile, batch, items=items)
            start = clock()
            try:
                resp = scorer.decide(body)
            except DecisionsError as exc:
                elapsed = clock() - start
                report.seconds += [elapsed / len(batch)] * len(batch)
                report.errored += len(batch)
                logger.warning("%s: %s", scorer.name, exc)
                continue
            elapsed = clock() - start
            report.seconds += [elapsed / len(batch)] * len(batch)
            report.cost_usd += scorer.cost(resp.usage)
            cand = {k: v for k, v in resp.answers.items() if k.startswith("candidate.")}
            for cid, _job, _summary in batch:
                try:
                    map_job(cid, resp.answers, cand, profile, items)
                except MissingAnswers:
                    continue
                report.schema_valid += 1
        return report
    finally:
        conn.execute("PRAGMA query_only = OFF")
