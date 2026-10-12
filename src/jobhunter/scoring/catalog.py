"""The model catalog: which models each use can choose, and what each costs (specs/020 M1).

One answer for "what can I pick for this use, and what will it cost?", for ``/models``,
``jobhunter models`` and the save-time checks to come. Every function here is local and free:
**nothing in this module makes a network call or a model call.** OpenRouter's list is read
from the catalog cache file by :func:`load_openrouter_cache`, which never fetches, and the
only function that does is :func:`refresh_openrouter_cache`, for an explicit click or
``--refresh``. Pages must not call ``OpenRouterScorer.catalog()`` or ``.cost()`` to price an
option: ``catalog()`` fetches when the cache is over a day old.

Uses (``USES``):

- ``screen``: ``[scoring] screen_scorer``, a scorer spec (``anthropic:...``, ``openrouter:...``,
  ``jev:...``, ``decisions:...``, ``local:...``, ``openai-compat:...``).
- ``deep``: ``[scoring] deep_scorer``, ``anthropic:<model>`` only.
- ``draft_api`` / ``draft_cli``: ``[apply] api_model`` (an API id) and ``cli_model`` (a CLI
  alias or an API id).
- ``check_api`` / ``check_cli``: ``[apply] api_entailment_model`` and ``cli_entailment_model``.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from jobhunter.config import (
    DECISIONS_PROVIDERS,
    OpenRouter,
    Scoring,
    check_scorer_spec,
    shown_value,
)
from jobhunter.pipeline.listing import to_iso
from jobhunter.scoring import decisions, screen
from jobhunter.scoring.scorers import (
    PRICING_PER_MTOK,
    THINKING_OFF,
    OpenRouterScorer,
    ceiling_cost,
    compute_cost,
)

logger = logging.getLogger(__name__)

USES = ("screen", "deep", "draft_api", "draft_cli", "check_api", "check_cli")
USE_LABELS = {
    "screen": "Screening new jobs",
    "deep": "Deep pass",
    "draft_api": "Packet drafting (API key)",
    "draft_cli": "Packet drafting (subscription)",
    "check_api": "Fact check (API key)",
    "check_cli": "Fact check (subscription)",
}
# The date the Anthropic prices in ``scorers.PRICING_PER_MTOK`` were last checked against
# platform.claude.com/docs/en/about-claude/pricing. The page shows "prices checked <date>".
PRICES_CHECKED = "2026-10-11"

# ─── The reservation switch ─────────────────────────────────────────────────

# Per-model spend reservations are in on every screening path (git-bug 026b50c, PR 121, and
# df51dff, PR 123): the cap reserves each job at the highest of the flat
# ``screen.ESTIMATED_COST_PER_REQUEST_USD`` ($0.002), the model's measured mean and its table
# price (``screen.reserve_per_job_usd``), and an Anthropic model at no less than its price with
# no cache reuse (020 M5b review). Guaranteed: no run sends more jobs than the cap covers at
# that hold; longer than typical postings can still cost more than held. 020 M5b (git-bug
# a322711) switched this on: every screening option is within the reservation. Off, only
# options at or under the flat $0.002 are offered.
RESERVATION_PER_MODEL = True
FLAT_RESERVATION_USD = screen.ESTIMATED_COST_PER_REQUEST_USD

# ─── Typical tokens per use ─────────────────────────────────────────────────

# 006 "Cost", Stage 3 per job: 5,000 in + 3,000 cached + 1,500 out (about $0.051 on Opus 5.5).
DEEP_TYPICAL_USAGE = SimpleNamespace(
    input_tokens=5000,
    cache_creation_input_tokens=0,
    cache_read_input_tokens=3000,
    output_tokens=1500,
)
# One fact check: the cited output lines with their source lines in, a verdict per line out.
# Assumed, not yet measured (the check's own usage is not stored per call): a resume of about
# 30 cited lines with one or two sources each is about 4,000-5,000 tokens in, and its verdicts
# about 1,000-1,500 out. Bounded by ``generator.MAX_TOKENS["entailment"]`` (4,000) on output.
ENTAILMENT_TYPICAL_USAGE = SimpleNamespace(
    input_tokens=5000,
    cache_creation_input_tokens=0,
    cache_read_input_tokens=0,
    output_tokens=1500,
)
# The old flat fact-check estimate. Kept as the floor: a check is never estimated, or
# cap-checked, below what it was before 020.
ENTAILMENT_FLOOR_USD = 0.003

# ─── Anthropic models ───────────────────────────────────────────────────────

THINKING_ALWAYS_ON = (
    "its thinking cannot be turned off, and this is a bounded call (screen.MAX_TOKENS)"
)
THINKING_NOT_OFF = "jobhunter does not switch its thinking off, and this is a bounded call"
REJECTS_EFFORT = "it rejects output_config.effort, which this use sends"
CLI_SENDS_EFFORT = "the CLI runner always passes --effort, and Haiku 4.5 rejects it"
SMALL_MODEL = "a small model for bounded calls; this use is offered Opus and Sonnet"
FALLBACK_ONLY = "kept only to price server-side fallback answers, not offered for any use"
CLI_LIST = "the subscription runner is offered the aliases and current models"
NO_PRICE = "no price in the price table, so spend caps could not see its cost"


@dataclass(frozen=True)
class AnthropicModel:
    """One entry of the Anthropic table: the uses it serves, and why not the others."""

    id: str
    label: str
    older: bool  # shown under "Older"
    uses: frozenset[str]
    reasons: Mapping[str, str]  # every use not in ``uses``: why it is not offered


def _entry(
    model: str, label: str, uses: Iterable[str], reasons: Mapping[str, str], *, older=False
) -> AnthropicModel:
    return AnthropicModel(model, label, older, frozenset(uses), dict(reasons))


# Keyed exactly like ``scorers.PRICING_PER_MTOK`` (a test keeps them in step). Sources for
# the per-use rules (checked 2026-10-11, platform.claude.com/docs, "Thinking" and "Effort"):
# Opus 5.5 cannot turn thinking off at any effort; Sonnet 5.5 turns it off with
# ``between_tools``; Haiku 5.5 with ``disabled``; Haiku 4.5 does not think unless asked and
# rejects ``effort``; Opus 5 thinks by default (``disabled`` only at effort high or below,
# which ``scorers.THINKING_OFF`` does not send for it) and accepts ``effort``.
ANTHROPIC_MODELS: dict[str, AnthropicModel] = {
    m.id: m
    for m in (
        _entry(
            "claude-haiku-5-5",
            "Claude Haiku 5.5",
            ("screen", "check_api", "check_cli"),
            {"deep": SMALL_MODEL, "draft_api": SMALL_MODEL, "draft_cli": SMALL_MODEL},
        ),
        _entry("claude-sonnet-5-5", "Claude Sonnet 5.5", USES, {}),
        _entry(
            "claude-opus-5-5",
            "Claude Opus 5.5",
            ("deep", "draft_api", "draft_cli"),
            {
                "screen": THINKING_ALWAYS_ON,
                "check_api": THINKING_ALWAYS_ON,
                "check_cli": THINKING_ALWAYS_ON,
            },
        ),
        _entry(
            "claude-haiku-4-5",
            "Claude Haiku 4.5",
            ("screen", "check_api"),
            {
                "deep": REJECTS_EFFORT,
                "draft_api": REJECTS_EFFORT,
                "draft_cli": CLI_SENDS_EFFORT,
                "check_cli": CLI_LIST,
            },
            older=True,
        ),
        _entry(
            "claude-opus-5",
            "Claude Opus 5",
            ("deep", "draft_api"),
            {
                "screen": THINKING_NOT_OFF,
                "check_api": THINKING_NOT_OFF,
                "check_cli": THINKING_NOT_OFF,
                "draft_cli": CLI_LIST,
            },
            older=True,
        ),
        _entry(
            "claude-opus-4-8",
            "Claude Opus 4.8",
            (),
            {u: FALLBACK_ONLY for u in USES},
            older=True,
        ),
        _entry(
            "claude-sonnet-5",
            "Claude Sonnet 5",
            (),
            {u: FALLBACK_ONLY for u in USES},
            older=True,
        ),
    )
}

# The ``claude`` CLI's aliases resolve to whatever the plan serves. Each is offered where the
# use allows it, and priced at the dearest model it may resolve to (a cost is never quoted low).
CLI_ALIASES: dict[str, AnthropicModel] = {
    "opus": _entry(
        "opus",
        "opus (the CLI's current Opus)",
        ("draft_cli",),
        {"check_cli": THINKING_ALWAYS_ON},
    ),
    "sonnet": _entry("sonnet", "sonnet (the CLI's current Sonnet)", ("draft_cli", "check_cli"), {}),
    "haiku": _entry(
        "haiku",
        "haiku (the CLI's current Haiku)",
        ("check_cli",),
        {"draft_cli": CLI_SENDS_EFFORT},
    ),
}
CLI_ALIAS_PRICED_AS = {
    "opus": "claude-opus-5",
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-4-5",
}
CLI_USES = ("draft_cli", "check_cli")
# Each use's code default (``config.Scoring`` and ``config.Apply``), listed first.
DEFAULTS = {
    "screen": "anthropic:claude-haiku-5-5",
    "deep": "anthropic:claude-opus-5-5",
    "draft_api": "claude-opus-5-5",
    "draft_cli": "opus",
    "check_api": "claude-haiku-5-5",
    "check_cli": "haiku",
}
# Bounded calls the screen and the fact check make with thinking off: a model must be in
# ``scorers.THINKING_OFF`` or not think by default (Haiku 4.5).
_BOUNDED = ("screen", "check_api", "check_cli")


# ─── OpenRouter cache (load only) ───────────────────────────────────────────


@dataclass(frozen=True)
class OpenRouterModel:
    name: str
    context_length: int | None
    structured: bool | None  # None: not known (an old cache, or no supported_parameters)


@dataclass(frozen=True)
class OpenRouterCache:
    """The catalog cache file: ``{"fetched_at", "prices": {slug: [in, out]}, "models"?}``.

    ``models`` is None for a cache written before 020 (prices only): its priced slugs are
    offered with "structured output not known".
    """

    fetched_at: float
    prices: dict[str, tuple[float, float]]
    models: dict[str, OpenRouterModel] | None = None

    def rates(self, slug: str) -> tuple[float, float] | None:
        """``slug``'s (input, output) USD per Mtok; a ``:free``-style suffix falls back."""
        return self.prices.get(slug) or self.prices.get(slug.partition(":")[0])

    def info(self, slug: str) -> OpenRouterModel | None:
        if self.models is None:
            return None
        return self.models.get(slug) or self.models.get(slug.partition(":")[0])

    def structured(self, slug: str) -> bool | None:
        info = self.info(slug)
        return None if info is None else info.structured

    def slugs(self) -> list[str]:
        known = set(self.prices) | set(self.models or {})
        return sorted(known)


def load_openrouter_cache(path: Path | str) -> OpenRouterCache | None:
    """Read the OpenRouter catalog cache from disk, at any age. **Never fetches.**

    The same file ``OpenRouterScorer.catalog()`` keeps. None when it is missing or unreadable.
    A malformed ``models`` map is treated as absent (prices still load). ``prices`` and
    ``fetched_at`` are parsed as ``scorers.load_catalog_cache`` parses them; this reader also
    keeps the ``models`` map, which that one drops, so the file is read once here.
    """
    try:
        raw = json.loads(Path(path).read_text())
        prices = {str(k): (float(v[0]), float(v[1])) for k, v in raw["prices"].items()}
        fetched_at = float(raw["fetched_at"])
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError):
        return None
    models: dict[str, OpenRouterModel] | None = None
    raw_models = raw.get("models")
    if isinstance(raw_models, dict):
        models = {}
        for slug, entry in raw_models.items():
            if not isinstance(entry, dict):
                continue
            structured = entry.get("structured")
            context = entry.get("context_length")
            models[str(slug)] = OpenRouterModel(
                name=str(entry.get("name") or slug),
                context_length=int(context) if isinstance(context, int | float) else None,
                structured=structured if isinstance(structured, bool) else None,
            )
    return OpenRouterCache(fetched_at, prices, models)


def refresh_openrouter_cache(config: OpenRouter, *, client: Any = None) -> OpenRouterCache | None:
    """Fetch OpenRouter's public ``/models`` list now and rewrite the cache. **Network.**

    Only for an explicit **Refresh list** click or ``models catalog --refresh``; no key is
    sent. Returns the new cache, or None when the fetch failed (the old cache is kept).
    """
    scorer = OpenRouterScorer("catalog", config, client=client, env={})
    try:
        if not scorer.refresh_catalog():
            return None
    finally:
        if client is None:
            scorer.client.close()
    return load_openrouter_cache(config.catalog_cache)


# ─── Prices ─────────────────────────────────────────────────────────────────


def _priced_id(model: str) -> str:
    """An API id, or the id a CLI alias is priced as."""
    return CLI_ALIAS_PRICED_AS.get(model, model)


def entailment_estimate_usd(model: str) -> float:
    """What one fact check on ``model`` (an API id or a CLI alias) is estimated and capped at.

    The price table at ``ENTAILMENT_TYPICAL_USAGE``, never below the old flat
    ``ENTAILMENT_FLOOR_USD``. A model missing from the table is priced at its dearest rates.
    ``generator.estimate`` and every fact-check cap check use this one figure.
    """
    priced = _priced_id(model)
    if priced in PRICING_PER_MTOK:
        cost = compute_cost(ENTAILMENT_TYPICAL_USAGE, f"anthropic:{priced}", batch=False)
    else:
        # A model missing from the table: the table's dearest rates, never below.
        cost = ceiling_cost(ENTAILMENT_TYPICAL_USAGE, batch=False)
    return max(cost, ENTAILMENT_FLOOR_USD)


def _chat_price_per_job(rates: tuple[float, float], jobs_per_request: int = 1) -> float:
    """A chat model's price for one typical screen, prompt prefix shared across a pack."""
    n = max(int(jobs_per_request), 1)
    return screen.chat_price_usd(rates, n) / n


@dataclass(frozen=True)
class ListPrice:
    usd: float | None  # per job; None: no known price
    source: str


def screen_list_price(
    spec: str,
    scoring: Scoring | None = None,
    *,
    cache: OpenRouterCache | None = None,
    batch: bool = True,
) -> ListPrice:
    """``spec``'s per-job screen price at ``screen.TYPICAL_USAGE``, without measured history.

    Anthropic: the price table (batch-priced unless ``batch=False``). Jev's default model:
    ``rescore.DECISIONS_FALLBACK_COST``. OpenRouter and other decisions slugs: the cached
    catalog (``cache``, else read from ``scoring.openrouter.catalog_cache``; never fetched).
    openai-compat: its configured prices (0 means none). ``local:``: free.

    Deliberately not ``screen.list_price_per_job_usd`` (the spend cap's figure): that one
    prices OpenRouter at one job a request and any decisions slug at $0 (decisions requests
    check the cap themselves), returns a priced $0 for openai-compat with no prices, and never
    reads a ``models`` map. This one is what the catalog offers and shows; keep them separate.
    """
    from jobhunter.scoring.rescore import DECISIONS_FALLBACK_COST

    scoring = scoring or Scoring()
    provider, _, name = spec.partition(":")
    if provider == "anthropic":
        if name not in PRICING_PER_MTOK:
            return ListPrice(None, "no price")
        return ListPrice(compute_cost(screen.TYPICAL_USAGE, spec, batch=batch), "price table")
    if provider == "local":
        return ListPrice(0.0, "free: runs on your local server")
    if provider == "openai-compat":
        compat = scoring.openai_compat
        rates = (compat.input_usd_per_mtok, compat.output_usd_per_mtok)
        if rates == (0.0, 0.0):
            return ListPrice(None, "no price (scoring.openai_compat prices are 0)")
        return ListPrice(_chat_price_per_job(rates, compat.jobs_per_request), "configured price")
    if provider in DECISIONS_PROVIDERS and name == decisions.JEV_DEFAULT_MODEL:
        return ListPrice(DECISIONS_FALLBACK_COST, "Jev default price")
    if provider in ("openrouter", *DECISIONS_PROVIDERS):
        if cache is None:
            cache = load_openrouter_cache(scoring.openrouter.catalog_cache)
        rates = cache.rates(name) if cache else None
        if rates is None:
            return ListPrice(None, "no price (not in the cached OpenRouter list)")
        pack = (
            decisions.DEFAULT_JOBS_PER_REQUEST
            if provider in DECISIONS_PROVIDERS
            else scoring.openrouter.jobs_per_request
        )
        return ListPrice(_chat_price_per_job(rates, pack), "cached OpenRouter price")
    return ListPrice(None, "no price")


@dataclass(frozen=True)
class Estimate:
    """A per-unit cost and where it came from, for the page to say."""

    usd: float | None  # None: no known price
    unit: str  # "job", "resume", "cover_letter", "question_draft", "check"
    source: str  # "measured over 412 jobs", "price table", "no price", ...
    measured: int = 0  # how many measured rows the figure is from


def _measured(conn: sqlite3.Connection, tier: str, spec: str) -> tuple[float, int]:
    avg, n = conn.execute(
        "SELECT avg(cost_usd), count(*) FROM fit_score WHERE tier = ? AND model = ? "
        "AND cost_usd > 0",
        (tier, spec),
    ).fetchone()
    return float(avg or 0.0), int(n or 0)


def screen_estimate(
    conn: sqlite3.Connection,
    spec: str,
    scoring: Scoring | None = None,
    *,
    cache: OpenRouterCache | None = None,
) -> Estimate:
    """Per-job screen cost: measured history for that model first, then the list price."""
    avg, n = _measured(conn, "screen", spec)
    if n:
        return Estimate(avg, "job", f"measured over {n} jobs", n)
    price = screen_list_price(spec, scoring, cache=cache)
    return Estimate(price.usd, "job", price.source)


def jobs_screened_last_week(conn: sqlite3.Connection, now: datetime) -> int:
    """Groups screened in the 7 days before ``now`` (any model): "at your volume"."""
    since = to_iso(now - timedelta(days=7))
    return int(
        conn.execute(
            "SELECT count(DISTINCT job_group_id) FROM fit_score WHERE tier = 'screen' "
            "AND created_at >= ?",
            (since,),
        ).fetchone()[0]
    )


def deep_estimate(conn: sqlite3.Connection, spec: str) -> Estimate:
    """Per-job deep-pass cost: measured ``tier = 'deep'`` for that model, else the table at
    006's typical deep tokens (``DEEP_TYPICAL_USAGE``), unbatched."""
    avg, n = _measured(conn, "deep", spec)
    if n:
        return Estimate(avg, "job", f"measured over {n} jobs", n)
    provider, _, name = spec.partition(":")
    if provider == "anthropic" and name in PRICING_PER_MTOK:
        return Estimate(compute_cost(DEEP_TYPICAL_USAGE, spec, batch=False), "job", "price table")
    return Estimate(None, "job", "no price")


def check_estimate(model: str) -> Estimate:
    """Per-draft fact-check cost for ``model`` (``entailment_estimate_usd``)."""
    priced = _priced_id(model)
    source = "price table" if priced in PRICING_PER_MTOK else "no price: the dearest rates"
    if entailment_estimate_usd(model) == ENTAILMENT_FLOOR_USD:
        source = f"{source}, at least ${ENTAILMENT_FLOOR_USD:.3f}"
    return Estimate(entailment_estimate_usd(model), "check", source)


def cli_api_equivalent(conn: sqlite3.Connection, kind: str, cli_model: str) -> Estimate:
    """ "About $X at API prices" for a subscription draft: the median ``api_equiv_usd`` of
    earlier subscription drafts of ``kind`` with that model (an alias matches the ids of its
    family: ``sonnet`` matches ``claude-sonnet-*``)."""
    pattern = f"claude-{cli_model}-%" if cli_model in CLI_ALIASES else cli_model
    costs = [
        float(r[0])
        for r in conn.execute(
            "SELECT api_equiv_usd FROM packet_document WHERE kind = ? AND runner = 'cli' "
            "AND origin = 'generated' AND api_equiv_usd > 0 AND model LIKE ?",
            (kind, pattern),
        )
    ]
    if not costs:
        return Estimate(None, kind, "no subscription drafts yet")
    return Estimate(statistics.median(costs), kind, f"median of {len(costs)}", len(costs))


# ─── The reservation rule ───────────────────────────────────────────────────


def within_reservation(
    spec: str, scoring: Scoring | None = None, *, cache: OpenRouterCache | None = None
) -> bool:
    """May ``spec`` be offered for screening under today's spend reservation?

    True for everything while ``RESERVATION_PER_MODEL`` is on (since M5b): every path reserves
    the model's own price. Off, only a spec whose per-job list price at typical tokens is known
    and at most the flat $0.002 the cap used to reserve. The unbatched price is used: Score
    now and the sync path pay it.
    """
    if RESERVATION_PER_MODEL:
        return True
    price = screen_list_price(spec, scoring, cache=cache, batch=False).usd
    return price is not None and price <= FLAT_RESERVATION_USD + 1e-12


def _above_reservation(spec: str, scoring: Scoring | None, cache: OpenRouterCache | None) -> str:
    price = screen_list_price(spec, scoring, cache=cache, batch=False).usd
    flat = f"${FLAT_RESERVATION_USD:.3f}"
    if price is None:
        return (
            f"it has no known price, and the spend cap reserves a flat {flat} a job for "
            "screening until per-model reservations land (git-bug 026b50c, df51dff)"
        )
    return (
        f"about ${price:.4f} a job, above the flat {flat} the spend cap reserves for screening "
        "until per-model reservations land (git-bug 026b50c, df51dff)"
    )


# ─── Judging one value ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class Verdict:
    """Is ``value`` offered for a use? ``reason`` says why not; ``note`` adds a caveat."""

    offered: bool
    reason: str | None = None
    note: str = ""
    older: bool = False


def _anthropic_verdict(model: str, use: str) -> Verdict:
    entry = ANTHROPIC_MODELS.get(model)
    if entry is None or model not in PRICING_PER_MTOK:
        return Verdict(False, NO_PRICE)
    if use not in entry.uses:
        return Verdict(False, entry.reasons.get(use, "not offered for this use"), older=entry.older)
    if use in _BOUNDED and model not in THINKING_OFF and model != "claude-haiku-4-5":
        return Verdict(False, THINKING_NOT_OFF, older=entry.older)
    return Verdict(True, older=entry.older)


def judge(
    use: str,
    value: str,
    scoring: Scoring | None = None,
    *,
    cache: OpenRouterCache | None = None,
    local_ids: Sequence[str] | None = None,
) -> Verdict:
    """Would the catalog offer ``value`` for ``use``, and if not, why? Local and free.

    The same rules the dropdowns use, for the save-time checks (020 M2) and for showing an
    effective value the catalog would not offer. ``local_ids`` is what the last **Check
    server** returned in this page view; None means not checked.
    """
    if use not in USES:
        raise ValueError(f"unknown use {use!r} (known: {', '.join(USES)})")
    value = value.strip()
    if not value:
        return Verdict(False, "no model given")
    if use in CLI_USES:
        alias = CLI_ALIASES.get(value)
        if alias is not None:
            if use in alias.uses:
                return Verdict(True)
            return Verdict(False, alias.reasons.get(use, "not offered for this use"))
        verdict = _anthropic_verdict(value, use)
        return verdict
    if use in ("draft_api", "check_api"):
        if ":" in value:
            return Verdict(False, f'write the model id alone, e.g. "{value.partition(":")[2]}"')
        return _anthropic_verdict(value, use)
    try:
        check_scorer_spec(value, use, anthropic_only=use == "deep")
    except ValueError as exc:
        return Verdict(False, str(exc))
    provider, _, name = value.partition(":")
    if provider == "anthropic":
        verdict = _anthropic_verdict(name, use)
        if not verdict.offered or use != "screen":
            return verdict
        if not within_reservation(value, scoring):
            return Verdict(False, _above_reservation(value, scoring, None), older=verdict.older)
        return verdict
    # Only screening reaches here: the deep pass is Anthropic only.
    note = ""
    if provider in ("openrouter", *DECISIONS_PROVIDERS):
        if cache is None:
            cache = load_openrouter_cache((scoring or Scoring()).openrouter.catalog_cache)
        if provider == "openrouter":
            structured = cache.structured(name) if cache else None
            if structured is False:
                return Verdict(
                    False,
                    "it does not support structured output, and the screen requires it "
                    "(provider.require_parameters)",
                )
            if cache is None or name not in cache.slugs():
                note = "not in the cached OpenRouter list"
            elif structured is None:
                note = "structured output not known"
    if provider == "local" and local_ids is not None and name not in local_ids:
        note = "not in the server's model list"
    if not within_reservation(value, scoring, cache=cache):
        return Verdict(False, _above_reservation(value, scoring, cache), note=note)
    return Verdict(True, note=note)


def use_rule_problem(use: str, value: str, field: str | None = None) -> str | None:
    """Why ``value`` can never serve ``use``, or None. Local and free; reads no file.

    The load-time half of :func:`judge`, for a value set in ``models.toml`` (020 M2): the spec
    syntax, an Anthropic model's price-table entry and its per-use rules. It leaves out what a
    save checks and ``--force`` may override (the screening reservation, the OpenRouter cache
    and the local server list), so a value saved on purpose still applies when read back.
    """
    if use not in USES:
        raise ValueError(f"unknown use {use!r} (known: {', '.join(USES)})")
    value = value.strip()
    if not value:
        return "no model given"
    if use in CLI_USES or use in ("draft_api", "check_api"):
        # These branches of judge never reach the reservation or the cache.
        verdict = judge(use, value)
        return None if verdict.offered else verdict.reason
    try:
        check_scorer_spec(value, field or use, anthropic_only=use == "deep")
    except ValueError as exc:
        return str(exc)
    provider, _, name = value.partition(":")
    if provider == "anthropic":
        verdict = _anthropic_verdict(name, use)
        return None if verdict.offered else verdict.reason
    return None


# ─── Options per use ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Option:
    """One dropdown entry: what is saved, its label, and where it sits."""

    value: str
    label: str
    provider: str  # "anthropic", "cli", "openrouter", "jev", "local"
    older: bool = False
    note: str = ""


@dataclass(frozen=True)
class FreeText:
    """A provider whose model is typed in, with an example of the form."""

    provider: str
    example: str


@dataclass
class UseOptions:
    use: str
    options: list[Option] = field(default_factory=list)
    free_text: list[FreeText] = field(default_factory=list)
    openrouter_fetched_at: float | None = None  # the cache's fetched_at, when it was read


def _order(use: str, option: Option) -> tuple[bool, bool]:
    # Stable sort: the default first, then current options in table order, then Older.
    return option.older, option.value != DEFAULTS[use]


def options(
    use: str,
    scoring: Scoring | None = None,
    *,
    cache: OpenRouterCache | None = None,
    local_ids: Sequence[str] | None = None,
) -> UseOptions:
    """The options the catalog offers for ``use``: current first, then Older.

    Not every model that works is listed for screening: OpenRouter slugs come from the cache
    (structured output not refused; within the reservation rule), local ids from the last
    server check. The effective value is the caller's to add when it is not here (``judge``
    says why).
    """
    if use not in USES:
        raise ValueError(f"unknown use {use!r} (known: {', '.join(USES)})")
    scoring = scoring or Scoring()
    out = UseOptions(use)
    if use in CLI_USES:
        for alias in CLI_ALIASES.values():
            if use in alias.uses:
                out.options.append(Option(alias.id, alias.label, "cli"))
    for model in ANTHROPIC_MODELS.values():
        bare = use in ("draft_api", "check_api", *CLI_USES)
        value = model.id if bare else f"anthropic:{model.id}"
        if judge(use, value, scoring).offered:
            out.options.append(Option(value, model.label, "anthropic", older=model.older))
    if use != "screen":
        out.options.sort(key=lambda o: _order(use, o))
        return out
    if cache is None:
        cache = load_openrouter_cache(scoring.openrouter.catalog_cache)
    if cache is not None:
        out.openrouter_fetched_at = cache.fetched_at
    jev = f"jev:{decisions.JEV_DEFAULT_MODEL}"
    if judge(use, jev, scoring, cache=cache).offered:
        out.options.append(Option(jev, f"Jev ({decisions.JEV_DEFAULT_MODEL})", "jev"))
    if cache is not None:
        for slug in cache.slugs():
            spec = f"openrouter:{slug}"
            verdict = judge(use, spec, scoring, cache=cache)
            if verdict.offered:
                info = cache.info(slug)
                label = info.name if info and info.name else slug
                out.options.append(Option(spec, label, "openrouter", note=verdict.note))
    local_values: list[str] = []
    if scoring.screen_scorer.startswith("local:") and shown_value(scoring.screen_scorer):
        local_values.append(scoring.screen_scorer.partition(":")[2])
    for model_id in local_ids or ():
        if model_id not in local_values:
            local_values.append(model_id)
    for model_id in local_values:
        out.options.append(Option(f"local:{model_id}", model_id, "local"))
    out.options.sort(key=lambda o: _order(use, o))
    out.free_text = [
        FreeText("jev", f"jev:{decisions.JEV_DEFAULT_MODEL}"),
        FreeText("decisions", "decisions:<openrouter slug>"),
        FreeText("openrouter", "openrouter:<author>/<model>"),
        FreeText("local", "local:<model id>"),
        FreeText("openai-compat", "openai-compat:<model>"),
    ]
    return out
