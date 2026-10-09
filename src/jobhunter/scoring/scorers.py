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
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import httpx

from jobhunter.config import Local, OpenAICompat, OpenRouter, Scoring

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
    cost_usd: float | None = None  # reported by the provider (OpenRouter); beats price tables
    model: str = ""  # the model that served this request


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


def _reported_cost(raw_usage: dict[str, Any]) -> float | None:
    """The provider's own per-request cost (OpenRouter ``usage.cost``), if a sane number."""
    cost = raw_usage.get("cost")
    if isinstance(cost, bool) or not isinstance(cost, int | float) or cost < 0:
        return None
    return float(cost)


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
        self.extra_headers: dict[str, str] = {}
        self.extra_body: dict[str, Any] = {}
        self.config_section = "scoring.openai_compat"
        # Packed requests (specs/016); the CLI may override jobs_per_request.
        self.jobs_per_request = config.jobs_per_request
        self.max_input_tokens = config.max_input_tokens_per_request
        self.est_cost_per_request_usd = config.est_cost_per_request_usd

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
                raise ScorerError(f"environment variable {var} ({self.config_section}) is not set")
            headers["Authorization"] = f"Bearer {key}"
        return {**headers, **self.extra_headers}

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
        body.update(self.extra_body)
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
            detail = str(exc)
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 401:
                detail = f"{detail}; {self._auth_hint()}"
            return ScoreResult(request.custom_id, status="errored", detail=detail)
        return self._parse(request.custom_id, data)

    def _auth_hint(self) -> str:
        var = self.config.api_key_env
        if var:
            return (
                f"the server rejected the key: check the environment variable {var} "
                f"({self.config_section})"
            )
        return f"the server requires an API key ({self.config_section} has no api_key_env)"

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
            cost_usd=_reported_cost(raw_usage),
            model=str(data.get("model") or self.model),
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


# ─── OpenRouter ─────────────────────────────────────────────────────────────

CATALOG_MAX_AGE_S = 24 * 3600


class OpenRouterScorer(OpenAICompatScorer):
    """The openai-compat scorer preset for OpenRouter (specs/016).

    ``model`` is the slug sent to OpenRouter (``openai/gpt-oss-120b``, ``...:free``,
    ``typesafe/jev-router``). The reply's top-level ``model`` is what actually served the
    request and lands in ``ScoreResult.model``. Cost: ``usage.cost`` from the response when
    present, else the cached public ``/models`` catalog, else $0 with a one-time warning.
    """

    def __init__(
        self,
        model: str,
        config: OpenRouter,
        *,
        client: httpx.Client | None = None,
        env: dict[str, str] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        compat = OpenAICompat(
            base_url=config.base_url,
            api_key_env=config.api_key_env,
            max_concurrency=config.max_concurrency,
            json_schema=config.json_schema,
            jobs_per_request=config.jobs_per_request,
            max_input_tokens_per_request=config.max_input_tokens_per_request,
            est_cost_per_request_usd=config.est_cost_per_request_usd,
        )
        super().__init__(model, compat, client=client, env=env)
        self.name = f"openrouter:{model}"
        self.openrouter = config
        self.config_section = "scoring.openrouter"
        self._clock = clock
        self._prices: dict[str, tuple[float, float]] | None = None
        self.extra_headers = {"X-Title": config.title}
        if config.referer:
            self.extra_headers["HTTP-Referer"] = config.referer
        self.extra_body = {"provider": dict(config.provider), "usage": {"include": True}}

    # price catalog (USD per million tokens), cached on disk, refreshed at most daily

    def _load_cache(self) -> tuple[float, dict[str, tuple[float, float]]] | None:
        try:
            raw = json.loads(Path(self.openrouter.catalog_cache).read_text())
            prices = {k: (float(v[0]), float(v[1])) for k, v in raw["prices"].items()}
            return float(raw["fetched_at"]), prices
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            return None

    def _fetch_catalog(self) -> dict[str, tuple[float, float]]:
        url = self.openrouter.base_url.rstrip("/") + "/models"
        resp = self.client.get(url)
        resp.raise_for_status()
        prices: dict[str, tuple[float, float]] = {}
        for entry in resp.json()["data"]:
            try:
                p_in = float(entry["pricing"]["prompt"]) * 1_000_000
                p_out = float(entry["pricing"]["completion"]) * 1_000_000
            except (KeyError, TypeError, ValueError):
                continue
            if p_in < 0 or p_out < 0:  # routers report -1: variable pricing
                continue
            prices[str(entry["id"])] = (p_in, p_out)
        return prices

    def catalog(self) -> dict[str, tuple[float, float]]:
        with self._lock:
            if self._prices is not None:
                return self._prices
            cached = self._load_cache()
            now = self._clock()
            if cached is not None and now - cached[0] < CATALOG_MAX_AGE_S:
                self._prices = cached[1]
                return self._prices
            try:
                self._prices = self._fetch_catalog()
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                logger.warning("openrouter: could not fetch model catalog (%s)", exc)
                self._prices = cached[1] if cached else {}
                return self._prices
            path = Path(self.openrouter.catalog_cache)
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps({"fetched_at": now, "prices": self._prices}, sort_keys=True)
                )
            except OSError as exc:
                logger.warning("openrouter: could not cache model catalog (%s)", exc)
            return self._prices

    def _lookup(self, slug: str) -> tuple[float, float] | None:
        prices = self.catalog()
        return prices.get(slug) or prices.get(slug.partition(":")[0])

    def cost(self, usage: Any, *, batch: bool = False) -> float:
        reported = getattr(usage, "cost_usd", None)
        if reported is not None:
            return float(reported)
        served = getattr(usage, "model", "") or ""
        rates = (self._lookup(served) if served else None) or self._lookup(self.model)
        if rates is None:
            with self._lock:
                if not self._warned_cost:
                    self._warned_cost = True
                    logger.warning(
                        "%s: cost is unknown (not in response or model catalog); "
                        "spend caps will not see this scorer's cost",
                        self.name,
                    )
            return 0.0
        return (
            _usage_int(usage, "input_tokens") * rates[0]
            + _usage_int(usage, "output_tokens") * rates[1]
        ) / 1_000_000


# ─── Local model (llama.cpp / Ollama) ───────────────────────────────────────

LOCAL_DEFAULT_URLS = {
    "llama.cpp": "http://127.0.0.1:8080",
    "ollama": "http://127.0.0.1:11434/v1",
}
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def local_base_url(config: Local) -> str:
    """The configured URL, else the runtime's loopback default."""
    return config.base_url or LOCAL_DEFAULT_URLS[config.runtime]


def is_loopback(url: str) -> bool:
    return (httpx.URL(url).host or "") in _LOOPBACK_HOSTS


class LocalScorer(OpenAICompatScorer):
    """The openai-compat preset for a model served on this machine (specs/016).

    Zero cost, no API key, one request at a time (the CPU is the bottleneck) and a long
    per-request timeout. For llama.cpp, ``cache_prompt`` asks the server to reuse the KV cache of
    the rubric + profile prefix that every request shares.
    """

    def __init__(self, model: str, config: Local, *, client: httpx.Client | None = None) -> None:
        compat = OpenAICompat(
            base_url=local_base_url(config),
            api_key_env="",
            max_concurrency=config.max_concurrency,
            json_schema=config.json_schema,
            jobs_per_request=config.jobs_per_request,
            max_input_tokens_per_request=config.max_input_tokens_per_request,
        )
        super().__init__(
            model,
            compat,
            client=client if client is not None else httpx.Client(timeout=config.timeout_s),
        )
        self.name = f"local:{model}"
        self.local = config
        self.config_section = "scoring.local"
        if config.runtime == "llama.cpp":
            self.extra_body = {"cache_prompt": True}

    def cost(self, usage: Any, *, batch: bool = False) -> float:
        return 0.0

    def _headers(self) -> dict[str, str]:
        return local_headers(self.local)

    def _auth_hint(self) -> str:
        var = self.local.api_key_env or "api_key_env"
        return (
            f"llama-server requires an API key: set {var} in ~/.env or the environment "
            "to the key the server was started with"
        )


def local_headers(config: Local) -> dict[str, str]:
    """JSON headers, plus ``Authorization: Bearer`` when ``api_key_env`` is set in the env."""
    headers = {"Content-Type": "application/json"}
    var = config.api_key_env
    if var and (key := os.environ.get(var)):
        headers["Authorization"] = f"Bearer {key}"
    return headers


@dataclass
class ServerStatus:
    url: str
    reachable: bool
    models: list[str]
    detail: str = ""
    unauthorized: bool = False  # the server answered 401: it wants a key this client lacks


def local_server_status(config: Local, *, client: httpx.Client | None = None) -> ServerStatus:
    """Is a local server answering? Lists models from ``/v1/models``, else tries ``/health``."""
    base = local_base_url(config).rstrip("/")
    root = base.removesuffix("/v1")
    http = client if client is not None else httpx.Client(timeout=5.0)
    headers = local_headers(config)
    try:
        try:
            resp = http.get(f"{root}/v1/models", headers=headers)
            if resp.status_code == 200:
                data = resp.json().get("data") or []
                return ServerStatus(base, True, [str(m.get("id")) for m in data if m.get("id")])
            if resp.status_code == 401:
                return ServerStatus(base, True, [], "HTTP 401 Unauthorized", unauthorized=True)
            detail = f"/v1/models returned HTTP {resp.status_code}"
        except (httpx.TransportError, ValueError, AttributeError) as exc:
            if isinstance(exc, httpx.TransportError):
                return ServerStatus(base, False, [], f"not reachable: {exc}")
            detail = f"/v1/models gave an unreadable reply ({exc})"
        try:
            health = http.get(f"{root}/health", headers=headers)
        except httpx.TransportError as exc:
            return ServerStatus(base, False, [], f"not reachable: {exc}")
        if health.status_code == 200:
            return ServerStatus(base, True, [], f"{detail}; /health ok")
        return ServerStatus(base, False, [], f"{detail}; /health HTTP {health.status_code}")
    finally:
        if client is None:
            http.close()


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
    if provider == "openrouter":
        return OpenRouterScorer(name, (scoring or Scoring()).openrouter, client=http_client)
    if provider == "local":
        return LocalScorer(name, (scoring or Scoring()).local, client=http_client)
    raise ScorerError(f"unknown scorer provider {provider!r} in {spec!r}")


def privacy_notice(spec: str, scoring: Scoring) -> str | None:
    """One line for non-Anthropic scorers: the resume and postings leave for ``base_url``."""
    provider, _ = split_scorer(spec)
    if provider == "anthropic":
        return None
    if provider == "openrouter":
        collection = scoring.openrouter.provider.get(
            "data_collection", "unset (OpenRouter default)"
        )
        return (
            f"notice: scorer {spec} sends your resume-derived profile and the job postings to "
            f"openrouter.ai and the model provider it routes to; provider data_collection is "
            f"{collection} (specs/008 personal data, specs/016)"
        )
    if provider == "local":
        url = local_base_url(scoring.local)
        if is_loopback(url):
            return (
                f"notice: scorer {spec} runs on this machine: your resume-derived profile and "
                f"the job postings stay here (sent only to {url}, nothing leaves the computer)"
            )
        return (
            f"notice: scorer {spec} is configured for {url}, which is NOT this machine; your "
            f"resume-derived profile and the job postings go there (specs/008 personal data)"
        )
    target = scoring.openai_compat.base_url if provider == "openai-compat" else provider
    return (
        f"notice: scorer {spec} sends your resume-derived profile and the job postings to "
        f"{target} (specs/008 personal data)"
    )
