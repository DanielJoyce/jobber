"""The model catalog (specs/020 M1): per-use rules, prices and the load-only cache reader.

No network: the cache reader is checked with a recording transport that must see zero
requests, and the one refresh test runs on ``httpx.MockTransport``.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from jobhunter.config import OpenAICompat, OpenRouter, Scoring
from jobhunter.core import db
from jobhunter.pipeline.listing import to_iso
from jobhunter.scoring import catalog
from jobhunter.scoring.scorers import PRICING_PER_MTOK, OpenRouterScorer

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
# Per Mtok: a cheap slug (about $0.0004 a job), a dear one (about $0.03 a job).
CHEAP = "openai/gpt-oss-120b"
DEAR = "anthropic/claude-sonnet-x"
UNSTRUCTURED = "some/chat-only"
OLD_PRICES = {CHEAP: [0.1, 0.5], DEAR: [3.0, 15.0], UNSTRUCTURED: [0.05, 0.1]}


def write_cache(path, *, models=True, fetched_at=0.0):
    body = {"fetched_at": fetched_at, "prices": OLD_PRICES}
    if models:
        body["models"] = {
            CHEAP: {"name": "GPT OSS 120B", "context_length": 131072, "structured": True},
            DEAR: {"name": "Dear model", "context_length": 200000, "structured": True},
            UNSTRUCTURED: {"name": "Chat only", "context_length": 8192, "structured": False},
            "unpriced/model": {"name": "No price", "context_length": None, "structured": True},
        }
    path.write_text(json.dumps(body))
    return path


@pytest.fixture
def scoring(tmp_path):
    return Scoring(openrouter=OpenRouter(catalog_cache=write_cache(tmp_path / "cat.json")))


def values(use, scoring=None, **kw):
    return [o.value for o in catalog.options(use, scoring, **kw).options]


# ─── The Anthropic table ────────────────────────────────────────────────────


def test_every_priced_model_has_a_catalog_entry_and_the_reverse():
    assert set(catalog.ANTHROPIC_MODELS) == set(PRICING_PER_MTOK)


def test_every_use_a_model_does_not_serve_says_why():
    for entry in [*catalog.ANTHROPIC_MODELS.values(), *catalog.CLI_ALIASES.values()]:
        for use in catalog.USES:
            if entry.id in catalog.CLI_ALIASES and use not in catalog.CLI_USES:
                continue
            if use not in entry.uses:
                assert entry.reasons.get(use), (entry.id, use)


def test_defaults_are_the_config_defaults_and_listed_first():
    from jobhunter.config import Apply

    s, a = Scoring(), Apply()
    assert {
        "screen": s.screen_scorer,
        "deep": s.deep_scorer,
        "draft_api": a.api_model,
        "draft_cli": a.cli_model,
        "check_api": a.api_entailment_model,
        "check_cli": a.cli_entailment_model,
    } == catalog.DEFAULTS
    for use in catalog.USES:
        assert values(use)[0] == catalog.DEFAULTS[use]
        assert catalog.judge(use, catalog.DEFAULTS[use]).offered


@pytest.mark.parametrize(
    ("use", "value"),
    [
        ("screen", "anthropic:claude-opus-5-5"),
        ("check_api", "claude-opus-5-5"),
        ("check_cli", "claude-opus-5-5"),
        ("check_cli", "opus"),
    ],
)
def test_opus_5_5_is_not_offered_for_bounded_calls(use, value):
    verdict = catalog.judge(use, value)
    assert not verdict.offered and "thinking cannot be turned off" in verdict.reason
    assert value not in values(use)


@pytest.mark.parametrize("value", ["haiku", "claude-haiku-4-5", "claude-haiku-5-5"])
def test_haiku_is_not_offered_for_cli_drafting(value):
    assert not catalog.judge("draft_cli", value).offered
    assert value not in values("draft_cli")


@pytest.mark.parametrize("use", ["deep", "draft_api"])
def test_haiku_4_5_is_not_offered_where_effort_is_sent(use):
    value = "anthropic:claude-haiku-4-5" if use == "deep" else "claude-haiku-4-5"
    verdict = catalog.judge(use, value)
    assert not verdict.offered and "effort" in verdict.reason


def test_the_use_table_matches_the_spec():
    assert values("deep") == [
        "anthropic:claude-opus-5-5",
        "anthropic:claude-sonnet-5-5",
        "anthropic:claude-opus-5",  # Older
    ]
    assert values("draft_api") == ["claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-5"]
    assert values("draft_cli") == ["opus", "sonnet", "claude-sonnet-5-5", "claude-opus-5-5"]
    assert values("check_api") == ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-haiku-4-5"]
    assert values("check_cli") == ["haiku", "sonnet", "claude-haiku-5-5", "claude-sonnet-5-5"]
    older = {o.value for o in catalog.options("check_api").options if o.older}
    assert older == {"claude-haiku-4-5"}


@pytest.mark.parametrize("model", ["claude-opus-4-8", "claude-sonnet-5"])
def test_fallback_only_models_are_offered_nowhere(model):
    for use in catalog.USES:
        bare = use not in ("screen", "deep")
        assert not catalog.judge(use, model if bare else f"anthropic:{model}").offered


@pytest.mark.parametrize(
    ("use", "value"),
    [
        ("screen", "anthropic:claude-haiku-9"),
        ("deep", "anthropic:claude-opus-9"),
        ("draft_api", "claude-opus-9"),
        ("draft_cli", "claude-opus-9"),
        ("check_api", "claude-haiku-9"),
        ("check_cli", "claude-haiku-9"),
    ],
)
def test_an_unpriced_anthropic_model_is_refused(use, value):
    verdict = catalog.judge(use, value)
    assert not verdict.offered and "no price" in verdict.reason


@pytest.mark.parametrize(
    ("use", "value"),
    [
        ("screen", "claude-haiku-5-5"),  # bare name
        ("screen", "openrouter:jev:typesafe/jev-1.13"),
        ("deep", "openrouter:openai/gpt-oss-120b"),  # the deep pass is Anthropic only
        ("draft_api", "anthropic:claude-opus-5-5"),
    ],
)
def test_malformed_values_are_refused_with_a_reason(use, value):
    verdict = catalog.judge(use, value)
    assert not verdict.offered and verdict.reason


# ─── The reservation rule ───────────────────────────────────────────────────


OVER_THE_FLAT = [
    "anthropic:claude-sonnet-5-5",  # about $0.010 a job unbatched
    "anthropic:claude-haiku-4-5",  # about $0.005 a job unbatched
    f"openrouter:{DEAR}",
    "openrouter:unpriced/model",
    "openrouter:not/in-cache",
    "decisions:not/in-cache",
    "openai-compat:my-model",  # prices 0: unknown
]


def test_the_switch_is_on():
    """020 M5b (a322711): per-model reservations are on every screening path."""
    assert catalog.RESERVATION_PER_MODEL is True


@pytest.mark.parametrize("spec", OVER_THE_FLAT)
def test_screening_options_over_the_flat_reservation_are_within_it_now(scoring, spec):
    assert catalog.within_reservation(spec, scoring)
    verdict = catalog.judge("screen", spec, scoring)
    assert verdict.offered, verdict.reason


@pytest.mark.parametrize("spec", OVER_THE_FLAT)
def test_with_the_switch_off_they_wait_for_the_fix(scoring, spec, monkeypatch):
    """The pre-M5 rule, kept behind the switch."""
    monkeypatch.setattr(catalog, "RESERVATION_PER_MODEL", False)
    assert not catalog.within_reservation(spec, scoring)
    verdict = catalog.judge("screen", spec, scoring)
    assert not verdict.offered and "026b50c" in verdict.reason
    assert spec not in values("screen", scoring)


@pytest.mark.parametrize(
    "spec",
    [
        "anthropic:claude-haiku-5-5",
        "jev:typesafe/jev-1.13",
        f"openrouter:{CHEAP}",
        f"decisions:{CHEAP}",
        "local:qwen3-8b",
    ],
)
def test_screening_options_within_the_reservation_are_offered(scoring, spec):
    assert catalog.within_reservation(spec, scoring)
    assert catalog.judge("screen", spec, scoring).offered


def test_the_switch_offers_dearer_screening_options(scoring):
    offered = values("screen", scoring)
    assert "anthropic:claude-sonnet-5-5" in offered
    assert f"openrouter:{DEAR}" in offered
    assert catalog.judge("screen", "anthropic:claude-haiku-4-5", scoring).older
    # Rules other than price still apply.
    assert "anthropic:claude-opus-5-5" not in offered
    assert f"openrouter:{UNSTRUCTURED}" not in offered
    assert not catalog.judge("screen", "anthropic:claude-haiku-9", scoring).offered


def test_configured_openai_compat_prices_are_used(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog, "RESERVATION_PER_MODEL", False)
    cheap = Scoring(openai_compat=OpenAICompat(input_usd_per_mtok=0.1, output_usd_per_mtok=0.2))
    assert catalog.within_reservation("openai-compat:m", cheap)
    dear = Scoring(openai_compat=OpenAICompat(input_usd_per_mtok=5.0, output_usd_per_mtok=20.0))
    assert not catalog.within_reservation("openai-compat:m", dear)


# ─── OpenRouter cache ───────────────────────────────────────────────────────


def test_screening_lists_cached_slugs_that_do_structured_output(scoring):
    opts = {o.value: o for o in catalog.options("screen", scoring).options}
    assert f"openrouter:{CHEAP}" in opts
    assert opts[f"openrouter:{CHEAP}"].label == "GPT OSS 120B"
    assert f"openrouter:{UNSTRUCTURED}" not in opts  # structured: false hides it
    verdict = catalog.judge("screen", f"openrouter:{UNSTRUCTURED}", scoring)
    assert not verdict.offered and "structured output" in verdict.reason
    assert catalog.options("screen", scoring).openrouter_fetched_at == 0.0


def test_an_old_format_cache_still_loads_and_offers_its_priced_slugs(tmp_path):
    path = write_cache(tmp_path / "old.json", models=False)
    cache = catalog.load_openrouter_cache(path)
    assert cache is not None and cache.models is None
    assert cache.rates(CHEAP) == (0.1, 0.5)
    assert cache.rates(f"{CHEAP}:free") == (0.1, 0.5)
    scoring = Scoring(openrouter=OpenRouter(catalog_cache=path))
    opts = {o.value: o for o in catalog.options("screen", scoring).options}
    assert opts[f"openrouter:{CHEAP}"].note == "structured output not known"
    # Without a models map, structured output is unknown, not refused.
    assert f"openrouter:{UNSTRUCTURED}" in opts
    # The scorer's own reader (prices only) reads the new format too.
    scorer = OpenRouterScorer(CHEAP, OpenRouter(catalog_cache=write_cache(tmp_path / "n.json")))
    loaded = scorer._load_cache()
    assert loaded is not None and loaded[1][CHEAP] == (0.1, 0.5)


@pytest.mark.parametrize("body", ["", "{", "[]", '{"prices": {}}', '{"fetched_at": "x"}'])
def test_an_unreadable_cache_is_none(tmp_path, body):
    path = tmp_path / "bad.json"
    path.write_text(body)
    assert catalog.load_openrouter_cache(path) is None
    assert catalog.load_openrouter_cache(tmp_path / "missing.json") is None


def test_a_malformed_models_map_keeps_the_prices(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"fetched_at": 1, "prices": OLD_PRICES, "models": [1, 2]}))
    cache = catalog.load_openrouter_cache(path)
    assert cache is not None and cache.models is None and cache.rates(CHEAP)


class Recorder:
    """A transport that records every request and answers 500: it must see none."""

    def __init__(self):
        self.requests: list[httpx.Request] = []

    def __call__(self, request):
        self.requests.append(request)
        return httpx.Response(500)


def test_nothing_in_the_catalog_fetches_even_with_a_stale_cache(scoring, tmp_path, monkeypatch):
    # The cache is from 1970: a stray OpenRouterScorer.catalog() would try to refresh it, and
    # swallow the error, so a recording transport (not a raising one) is what catches it.
    recorder = Recorder()
    real_init = httpx.Client.__init__

    def recording_init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(recorder)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", recording_init)
    monkeypatch.setattr(
        OpenRouterScorer, "catalog", lambda self: pytest.fail("catalog() must not be called")
    )
    conn = db.connect(tmp_path / "t.db")
    db.migrate(conn)
    local = ["qwen3-8b"]
    for use in catalog.USES:
        listed = catalog.options(use, scoring, local_ids=local)
        for opt in listed.options:
            catalog.judge(use, opt.value, scoring, local_ids=local)
    for spec in (
        f"openrouter:{CHEAP}",
        "openrouter:not/in-cache",
        "jev:typesafe/jev-1.13",
        "decisions:x/y",
        "local:qwen3-8b",
        "anthropic:claude-sonnet-5-5",
    ):
        catalog.judge("screen", spec, scoring)
        catalog.screen_estimate(conn, spec, scoring)
        catalog.within_reservation(spec, scoring)
    catalog.deep_estimate(conn, "anthropic:claude-opus-5-5")
    catalog.cli_api_equivalent(conn, "resume", "opus")
    catalog.check_estimate("claude-sonnet-5-5")
    assert recorder.requests == []
    conn.close()


def test_refresh_fetches_the_public_list_without_a_key_and_writes_the_models_map(tmp_path):
    seen: list[httpx.Request] = []
    listing = {
        "data": [
            {
                "id": CHEAP,
                "name": "GPT OSS 120B",
                "context_length": 131072,
                "pricing": {"prompt": "0.0000001", "completion": "0.0000005"},
                "supported_parameters": ["response_format", "structured_outputs", "tools"],
            },
            {
                "id": UNSTRUCTURED,
                "name": "Chat only",
                "pricing": {"prompt": "0.00000005", "completion": "0.0000001"},
                "supported_parameters": ["temperature"],
            },
            {"id": "router/auto", "pricing": {"prompt": "-1", "completion": "-1"}},
        ]
    }

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=listing)

    path = tmp_path / "cache" / "cat.json"
    config = OpenRouter(catalog_cache=path)
    before = time.time()
    cache = catalog.refresh_openrouter_cache(
        config, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert [str(r.url) for r in seen] == ["https://openrouter.ai/api/v1/models"]
    assert "authorization" not in seen[0].headers
    assert cache is not None and cache.fetched_at >= before
    assert cache.structured(CHEAP) is True and cache.structured(UNSTRUCTURED) is False
    assert cache.info("router/auto") is not None and cache.rates("router/auto") is None
    raw = json.loads(path.read_text())
    assert set(raw) == {"fetched_at", "prices", "models"}
    assert raw["models"][CHEAP] == {
        "name": "GPT OSS 120B",
        "context_length": 131072,
        "structured": True,
    }


def test_a_failed_refresh_keeps_the_old_cache(tmp_path):
    path = write_cache(tmp_path / "cat.json")
    before = path.read_text()
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    assert catalog.refresh_openrouter_cache(OpenRouter(catalog_cache=path), client=client) is None
    assert path.read_text() == before


def test_scoring_s_own_daily_refresh_writes_the_models_map_too(tmp_path):
    listing = {
        "data": [
            {
                "id": CHEAP,
                "pricing": {"prompt": "0.0000001", "completion": "0.0000005"},
                "supported_parameters": ["structured_outputs"],
            }
        ]
    }
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=listing))
    )
    path = tmp_path / "cat.json"
    scorer = OpenRouterScorer(CHEAP, OpenRouter(catalog_cache=path), client=client, env={})
    assert scorer.catalog()[CHEAP] == pytest.approx((0.1, 0.5))
    cache = catalog.load_openrouter_cache(path)
    assert cache is not None and cache.structured(CHEAP) is True


# ─── Local models ───────────────────────────────────────────────────────────


def test_local_options_are_the_configured_model_and_the_last_server_check():
    scoring = Scoring(screen_scorer="local:configured-model")
    assert [v for v in values("screen", scoring) if v.startswith("local:")] == [
        "local:configured-model"
    ]
    checked = values("screen", scoring, local_ids=["a", "configured-model", "b"])
    assert [v for v in checked if v.startswith("local:")] == [
        "local:configured-model",
        "local:a",
        "local:b",
    ]
    verdict = catalog.judge("screen", "local:c", scoring, local_ids=["a"])
    assert verdict.offered and verdict.note == "not in the server's model list"


# ─── Prices per use ─────────────────────────────────────────────────────────


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.migrate(c)
    c.execute(
        "INSERT INTO source (key, class, name, family, tier, entry, policy) "
        "VALUES ('wa', 'B', 'WA', 'neogov', 'http', 'https://example.com', 'enabled')"
    )
    yield c
    c.close()


def add_score(conn, tier, model, cost, *, at=NOW):
    n = conn.execute("SELECT coalesce(max(id), 0) + 1 FROM job").fetchone()[0]
    stamp = to_iso(at)
    conn.execute(
        "INSERT INTO job (id, source_key, external_id, url, title, first_seen_at, last_seen_at) "
        "VALUES (?, 'wa', ?, 'u', 't', ?, ?)",
        (n, str(n), stamp, stamp),
    )
    conn.execute(
        "INSERT INTO job_group (id, canonical_job_id, method, created_at) "
        "VALUES (?, ?, 'exact_hash', ?)",
        (n, n, stamp),
    )
    group = n
    conn.execute(
        "INSERT INTO fit_score (job_group_id, tier, model, prompt_version, scoring_version, "
        "verdict, overall, dimensions, evidence, cost_usd, created_at) "
        "VALUES (?, ?, ?, 'p', 's', 'possible', 50, '{}', '[]', ?, ?)",
        (group, tier, model, cost, to_iso(at)),
    )


def test_screen_estimate_prefers_measured_history_for_that_model(conn, scoring):
    haiku = "anthropic:claude-haiku-5-5"
    first = catalog.screen_estimate(conn, haiku, scoring)
    assert first.source == "price table" and first.measured == 0
    # Batch-priced: 2,600 in, 3,900 cache reads, 455 out at $0.10/$0.50, halved.
    assert first.usd == pytest.approx((2600 * 0.1 + 3900 * 0.01 + 455 * 0.5) / 1e6 / 2)
    add_score(conn, "screen", haiku, 0.0004)
    add_score(conn, "screen", haiku, 0.0006)
    add_score(conn, "screen", "anthropic:claude-haiku-4-5", 0.05)  # another model: ignored
    est = catalog.screen_estimate(conn, haiku, scoring)
    assert (est.usd, est.source, est.measured) == (
        pytest.approx(0.0005),
        "measured over 2 jobs",
        2,
    )


@pytest.mark.parametrize(
    ("spec", "usd", "source"),
    [
        ("jev:typesafe/jev-1.13", 0.00013, "Jev default price"),
        ("local:m", 0.0, "free: runs on your local server"),
        ("openrouter:not/in-cache", None, "no price (not in the cached OpenRouter list)"),
        ("openai-compat:m", None, "no price (scoring.openai_compat prices are 0)"),
        ("anthropic:claude-haiku-9", None, "no price"),
    ],
)
def test_screen_estimate_sources(conn, scoring, spec, usd, source):
    est = catalog.screen_estimate(conn, spec, scoring)
    assert est.source == source
    assert est.usd == (pytest.approx(usd) if usd is not None else None)


def test_screen_estimate_prices_a_cached_openrouter_slug(conn, scoring):
    est = catalog.screen_estimate(conn, f"openrouter:{CHEAP}", scoring)
    assert est.source == "cached OpenRouter price"
    assert est.usd == pytest.approx(((2600 + 3900) * 0.1 + 455 * 0.5) / 1e6)


def test_deep_estimate_measured_then_the_table(conn):
    opus = "anthropic:claude-opus-5-5"
    est = catalog.deep_estimate(conn, opus)
    # 006: 5,000 in + 3,000 cached + 1,500 out on Opus 5.5 (cache reads at 0.05x): ~$0.051.
    assert est.source == "price table"
    assert est.usd == pytest.approx((5000 * 4 + 3000 * 0.2 + 1500 * 20) / 1e6)
    add_score(conn, "deep", opus, 0.07)
    add_score(conn, "screen", opus, 1.0)  # a screen row never counts for the deep pass
    assert catalog.deep_estimate(conn, opus).usd == pytest.approx(0.07)
    assert catalog.deep_estimate(conn, "anthropic:claude-opus-9").usd is None


def test_jobs_screened_last_week(conn):
    add_score(conn, "screen", "anthropic:claude-haiku-5-5", 0.001, at=NOW - timedelta(days=1))
    add_score(conn, "screen", "anthropic:claude-haiku-4-5", 0.001, at=NOW - timedelta(days=6))
    add_score(conn, "screen", "anthropic:claude-haiku-5-5", 0.001, at=NOW - timedelta(days=8))
    add_score(conn, "deep", "anthropic:claude-opus-5-5", 0.05, at=NOW)
    assert catalog.jobs_screened_last_week(conn, NOW) == 2


@pytest.mark.parametrize(
    ("model", "usd"),
    [
        ("claude-haiku-5-5", 0.003),  # $0.00125 at the table: the old flat figure is the floor
        ("claude-sonnet-5-5", (5000 * 2 + 1500 * 10) / 1e6),  # $0.025
        ("claude-haiku-4-5", (5000 * 1 + 1500 * 5) / 1e6),  # $0.0125
        ("haiku", (5000 * 1 + 1500 * 5) / 1e6),  # the alias at the dearest Haiku
        ("sonnet", (5000 * 2 + 1500 * 10) / 1e6),
        ("claude-unknown", None),  # unpriced: the dearest rates in the table
    ],
)
def test_entailment_estimate_is_priced_for_its_model(model, usd):
    if usd is None:
        top_in = max(r[0] for r in PRICING_PER_MTOK.values())
        top_out = max(r[1] for r in PRICING_PER_MTOK.values())
        usd = (5000 * top_in + 1500 * top_out) / 1e6
    assert catalog.entailment_estimate_usd(model) == pytest.approx(usd)
    assert catalog.check_estimate(model).usd == pytest.approx(usd)


def test_cli_api_equivalent_is_per_model_family(conn):
    from jobhunter.apply import paste

    _gid, pid = paste.create_pasted_packet(
        conn, url=None, text="posting", employer="E", title="T", now=NOW
    )
    for version, (model, equiv) in enumerate(
        [("claude-opus-5-5", 0.2), ("claude-opus-5-5", 0.4), ("claude-sonnet-5-5", 0.05)], 1
    ):
        conn.execute(
            "INSERT INTO packet_document (packet_id, kind, version, origin, doc_json, body_md, "
            "check_report, model, created_at, runner, api_equiv_usd) "
            "VALUES (?, 'resume', ?, 'generated', '{}', '', '{}', ?, ?, 'cli', ?)",
            (pid, version, model, to_iso(NOW), equiv),
        )
    opus = catalog.cli_api_equivalent(conn, "resume", "opus")
    assert (opus.usd, opus.source) == (pytest.approx(0.3), "median of 2")
    assert catalog.cli_api_equivalent(conn, "resume", "claude-sonnet-5-5").usd == 0.05
    assert catalog.cli_api_equivalent(conn, "cover_letter", "opus").usd is None
