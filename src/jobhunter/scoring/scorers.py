"""Pluggable fit scorers (specs/006 "The scorer is pluggable").

A ``FitScorer`` turns provider-neutral ``ScoreRequest``s into ``ScoreResult``s carrying the
model's JSON text and usage. Everything downstream (``Screen`` parsing, evidence verification,
the ``fit_score`` write path, buckets, eval) lives in ``screen.py`` and is the same for every
scorer.

This module is the one place outside ``jobhunter.core.fetch`` allowed to import ``httpx``: the
OpenAI-compatible scorer is an API client, not a scraper. It sends the resume-derived profile
text to the configured ``base_url`` (specs/008 "Personal data").
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import httpx

from jobhunter.config import OpenAICompat, Scoring

logger = logging.getLogger(__name__)

DEFAULT_MAX_TOKENS = 2048
# USD per million tokens, standard (non-batch) rates: (input, output).
PRICING_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-opus-5": (5.00, 25.00),
}
BATCH_DISCOUNT = 0.5
CACHE_READ_MULTIPLIER = 0.1
CACHE_WRITE_MULTIPLIER = 1.25  # 5-minute ephemeral cache writes


class ScorerError(Exception):
    """A scorer is misconfigured or unreachable."""


def split_scorer(spec: str) -> tuple[str, str]:
    """``"openai-compat:llama-3.1-70b"`` -> ``("openai-compat", "llama-3.1-70b")``.

    A bare name with no provider is treated as an Anthropic model, as before.
    """
    provider, sep, name = spec.partition(":")
    if not sep:
        return "anthropic", spec
    if not name:
        raise ScorerError(f"scorer {spec!r} has no model name")
    return provider, name


def model_id(scorer: str) -> str:
    """``"anthropic:claude-haiku-4-5"`` -> ``"claude-haiku-4-5"``. Other providers raise."""
    provider, name = split_scorer(scorer)
    if provider != "anthropic":
        raise ValueError(f"scorer {scorer!r} is not an Anthropic model")
    return name


def _usage_int(usage: Any, name: str) -> int:
    return int(getattr(usage, name, None) or 0)


def compute_cost(usage: Any, model: str, *, batch: bool) -> float:
    """USD for one Anthropic response's ``usage``: input, cache writes, cache reads, output."""
    try:
        in_rate, out_rate = PRICING_PER_MTOK[model_id(model)]
    except KeyError as exc:
        raise ValueError(f"no pricing for model {model!r}") from exc
    factor = BATCH_DISCOUNT if batch else 1.0
    cost = (
        _usage_int(usage, "input_tokens") * in_rate
        + _usage_int(usage, "cache_creation_input_tokens") * in_rate * CACHE_WRITE_MULTIPLIER
        + _usage_int(usage, "cache_read_input_tokens") * in_rate * CACHE_READ_MULTIPLIER
        + _usage_int(usage, "output_tokens") * out_rate
    )
    return cost * factor / 1_000_000


# ─── Provider-neutral request and result ────────────────────────────────────


@dataclass(frozen=True)
class ScoreRequest:
    """The pieces of one scoring call, before any provider shapes them."""

    custom_id: str
    system: str  # rubric text
    profile: str  # profile scoring text (resume-derived; never filters or weights)
    posting: str  # the single posting
    schema: dict[str, Any]  # JSON schema of the expected ``Screen`` output
    max_tokens: int = DEFAULT_MAX_TOKENS


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class ScoreResult:
    """One scorer response. ``text`` is the model's JSON (unparsed) when ``status == "succeeded"``.

    ``status`` is ``succeeded`` or one of ``errored`` / ``canceled`` / ``expired``.
    ``stop_reason`` is the provider's, normalized so ``max_tokens`` and ``refusal`` match
    Anthropic's. ``model`` is the model the provider reports having used.
    """

    custom_id: str
    status: str = "succeeded"
    text: str | None = None
    usage: Any = None
    model: str = ""
    stop_reason: str | None = None
    detail: str = ""


@runtime_checkable
class FitScorer(Protocol):
    name: str  # recorded in fit_score.model

    @property
    def supports_batching(self) -> bool: ...

    def submit(self, requests: list[ScoreRequest]) -> str | list[ScoreResult]:
        """Batching scorers return a handle for ``collect``; others return results now."""
        ...

    def ready(self, handle: str) -> bool: ...

    def collect(self, handle: str) -> list[ScoreResult]: ...

    def score_one(self, request: ScoreRequest) -> ScoreResult: ...

    def cost(self, usage: Any, *, batch: bool = False) -> float: ...


# ─── Anthropic ──────────────────────────────────────────────────────────────


def anthropic_params(request: ScoreRequest, model: str) -> dict[str, Any]:
    """Messages API params for one request. No ``thinking`` and no ``output_config.effort``.

    Haiku 4.5 rejects ``effort``, and a bounded classification runs with thinking off.
    """
    return {
        "model": model,
        "max_tokens": request.max_tokens,
        "system": [
            {"type": "text", "text": request.system},
            {"type": "text", "text": request.profile, "cache_control": {"type": "ephemeral"}},
        ],
        "messages": [{"role": "user", "content": request.posting}],
        "output_config": {"format": {"type": "json_schema", "schema": request.schema}},
    }


def _message_result(custom_id: str, message: Any) -> ScoreResult:
    text = None
    for block in getattr(message, "content", None) or []:
        if getattr(block, "type", None) == "text":
            text = block.text
            break
    return ScoreResult(
        custom_id=custom_id,
        text=text,
        usage=getattr(message, "usage", None),
        model=getattr(message, "model", "") or "",
        stop_reason=getattr(message, "stop_reason", None),
    )


class AnthropicScorer:
    """Anthropic Messages + Message Batches. ``client`` is an ``anthropic.Anthropic``."""

    supports_batching = True

    def __init__(self, client: Any, spec: str = "anthropic:claude-haiku-4-5") -> None:
        self.client = client
        self.name = spec
        self.model = model_id(spec)

    def params(self, request: ScoreRequest) -> dict[str, Any]:
        return anthropic_params(request, self.model)

    def submit(self, requests: list[ScoreRequest]) -> str:
        batch = self.client.messages.batches.create(
            requests=[{"custom_id": r.custom_id, "params": self.params(r)} for r in requests]
        )
        return batch.id

    def ready(self, handle: str) -> bool:
        return self.client.messages.batches.retrieve(handle).processing_status == "ended"

    def collect(self, handle: str) -> list[ScoreResult]:
        out: list[ScoreResult] = []
        for entry in self.client.messages.batches.results(handle):
            kind = entry.result.type
            if kind != "succeeded":
                out.append(ScoreResult(entry.custom_id, status=kind, detail=str(entry.result)))
            else:
                out.append(_message_result(entry.custom_id, entry.result.message))
        return out

    def score_one(self, request: ScoreRequest) -> ScoreResult:
        message = self.client.messages.parse(**self.params(request))
        return _message_result(request.custom_id, message)

    def cost(self, usage: Any, *, batch: bool = False) -> float:
        return compute_cost(usage, self.name, batch=batch)


# ─── OpenAI-compatible ──────────────────────────────────────────────────────

_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)
_JSON_ONLY = (
    "\n\nRespond with a single JSON object and nothing else (no prose, no code fences) that "
    "validates against this JSON schema:\n"
)


def extract_json(text: str) -> str:
    """Strip code fences and surrounding prose from a model reply that should be JSON."""
    text = text.strip()
    if m := _FENCE.match(text):
        text = m.group(1).strip()
    if not text.startswith("{") and "{" in text and "}" in text:
        text = text[text.index("{") : text.rindex("}") + 1]
    return text


class OpenAICompatScorer:
    """A generic ``/v1/chat/completions`` endpoint. No batching: bounded concurrency.

    Sends ``response_format: json_schema`` first. A server that rejects it (HTTP 400/422) is
    retried without it, with the schema spelled out in the prompt, and remembered as not
    supporting it. Either way the text is validated against ``Screen`` by the caller.
    """

    supports_batching = False

    def __init__(
        self,
        model: str,
        config: OpenAICompat,
        *,
        client: httpx.Client | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.model = model
        self.name = f"openai-compat:{model}"
        self.config = config
        self.client = client if client is not None else httpx.Client(timeout=120.0)
        self._env = env
        self._use_schema = config.json_schema
        self._warned_cost = False
        self._lock = threading.Lock()

    @property
    def url(self) -> str:
        base = self.config.base_url.rstrip("/").removesuffix("/v1")
        return f"{base}/v1/chat/completions"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        var = self.config.api_key_env
        if var:
            env = self._env if self._env is not None else os.environ
            key = env.get(var)
            if not key:
                raise ScorerError(f"environment variable {var} (scoring.openai_compat) is not set")
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _body(self, request: ScoreRequest, *, schema: bool) -> dict[str, Any]:
        system = f"{request.system}\n\n{request.profile}"
        if not schema:
            system += _JSON_ONLY + json.dumps(request.schema)
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": request.max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": request.posting},
            ],
        }
        if schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "screen", "schema": request.schema},
            }
        return body

    def _post(self, body: dict[str, Any]) -> httpx.Response:
        return self.client.post(self.url, json=body, headers=self._headers())

    def score_one(self, request: ScoreRequest) -> ScoreResult:
        try:
            use_schema = self._use_schema
            resp = self._post(self._body(request, schema=use_schema))
            if use_schema and resp.status_code in (400, 422):
                logger.warning(
                    "%s rejected response_format json_schema (HTTP %d); "
                    "falling back to instructed JSON",
                    self.url,
                    resp.status_code,
                )
                self._use_schema = False
                resp = self._post(self._body(request, schema=False))
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            return ScoreResult(request.custom_id, status="errored", detail=str(exc))
        return self._parse(request.custom_id, data)

    def _parse(self, custom_id: str, data: dict[str, Any]) -> ScoreResult:
        try:
            choice = data["choices"][0]
            message = choice["message"]
            content = message.get("content")
        except (KeyError, IndexError, TypeError, AttributeError):
            return ScoreResult(custom_id, status="errored", detail="malformed response")
        raw_usage = data.get("usage") or {}
        usage = Usage(
            input_tokens=int(raw_usage.get("prompt_tokens") or 0),
            output_tokens=int(raw_usage.get("completion_tokens") or 0),
        )
        finish = choice.get("finish_reason")
        stop = {"length": "max_tokens", "content_filter": "refusal"}.get(finish, finish)
        if message.get("refusal"):
            stop = "refusal"
        return ScoreResult(
            custom_id,
            text=extract_json(content) if isinstance(content, str) else None,
            usage=usage,
            model=str(data.get("model") or self.model),
            stop_reason=stop,
        )

    def submit(self, requests: list[ScoreRequest]) -> list[ScoreResult]:
        """Score every request, at most ``max_concurrency`` in flight. Results keep order."""
        workers = max(1, self.config.max_concurrency)
        if workers == 1 or len(requests) <= 1:
            return [self.score_one(r) for r in requests]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(self.score_one, requests))

    def ready(self, handle: str) -> bool:
        return True

    def collect(self, handle: str) -> list[ScoreResult]:
        raise ScorerError("openai-compat scorers do not batch; there is nothing to collect")

    def cost(self, usage: Any, *, batch: bool = False) -> float:
        in_price = self.config.input_usd_per_mtok
        out_price = self.config.output_usd_per_mtok
        if in_price == 0 and out_price == 0:
            with self._lock:
                if not self._warned_cost:
                    self._warned_cost = True
                    logger.warning(
                        "%s: cost is unknown (scoring.openai_compat prices are 0); "
                        "spend caps will not see this scorer's cost",
                        self.name,
                    )
            return 0.0
        return (
            _usage_int(usage, "input_tokens") * in_price
            + _usage_int(usage, "output_tokens") * out_price
        ) / 1_000_000


# ─── Factory ────────────────────────────────────────────────────────────────


def scorer_from_string(
    spec: str,
    *,
    scoring: Scoring | None = None,
    client: Any = None,
    http_client: httpx.Client | None = None,
    anthropic_factory: Callable[[], Any] | None = None,
) -> FitScorer:
    """``"anthropic:claude-haiku-4-5"`` or ``"openai-compat:llama-3.1-70b"`` -> a scorer."""
    provider, name = split_scorer(spec)
    if provider == "anthropic":
        if client is None:
            if anthropic_factory is not None:
                client = anthropic_factory()
            else:
                import anthropic

                client = anthropic.Anthropic()
        return AnthropicScorer(client, f"anthropic:{name}")
    if provider == "openai-compat":
        config = (scoring or Scoring()).openai_compat
        return OpenAICompatScorer(name, config, client=http_client)
    raise ScorerError(f"unknown scorer provider {provider!r} in {spec!r}")


def privacy_notice(spec: str, scoring: Scoring) -> str | None:
    """One line for non-Anthropic scorers: the resume and postings leave for ``base_url``."""
    provider, _ = split_scorer(spec)
    if provider == "anthropic":
        return None
    target = scoring.openai_compat.base_url if provider == "openai-compat" else provider
    return (
        f"notice: scorer {spec} sends your resume-derived profile and the job postings to "
        f"{target} (specs/008 personal data)"
    )
