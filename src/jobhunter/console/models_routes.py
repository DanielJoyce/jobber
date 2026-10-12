"""Routes for /models (specs/020-model-selection.md "/models", ticket M4).

One row per thing jobhunter uses a model for, each with the value in effect always selected,
where it came from, what it costs and whether its key or subscription is there. Every write
goes to ``<data dir>/models.toml`` through ``model_choices`` (never config.toml), quotes the
digest of the file the page was built from, and then swaps only the saved fields into
``app.state.settings`` under ``app.state.settings_lock``.

Rules (specs/020 "Console rules"):

- No page render, preview or save makes a network call, a subprocess or a model call. The
  OpenRouter list is read with the load-only ``catalog.load_openrouter_cache``. Only
  **Refresh list** and **Check server** reach the network, and only on a click.
- Writes need a loopback **peer** (the TCP peer, not ``Host``), like 019 S12a's Apply: a model
  change spends nothing now, but it decides what the nightly run spends. A non-loopback peer
  sees the page read-only, with the ``jobhunter models`` commands instead of buttons. The two
  network routes are also refused under ``--allow-remote``.
- No value other than a model id or scorer spec is ever rendered (``config.shown_value``): an
  invalid value in models.toml, or something key-shaped in config.toml, shows as a placeholder.

Save + re-run is M5, the banners on other pages M7, the live S6 checks 019 S6.
"""

# No ``from __future__ import annotations``: FastAPI must resolve the local annotations.

import logging
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from jobhunter import model_access as ma
from jobhunter import model_choices as mc
from jobhunter import readiness as rd
from jobhunter.config import BLOCKED_SPEC, Settings, layers_for, relayer, shown_value
from jobhunter.console import prefs_graceful as gr
from jobhunter.scoring import catalog
from jobhunter.scoring.scorers import privacy_notice

logger = logging.getLogger(__name__)

# (title, fields) per row; the row key is also the `jobhunter models reset` name.
ROWS: dict[str, tuple[str, tuple[str, ...]]] = {
    "screen": ("Screening new jobs", ("screen",)),
    "deep": ("Deep pass", ("deep",)),
    "drafting": ("Packet drafting", ("runner", "draft_cli", "draft_api", "effort")),
    "check": ("Fact check after each draft", ("entailment_check", "check_cli", "check_api")),
}
ROW_OF = {name: row for row, (_, names) in ROWS.items() for name in names}
MODEL_FIELDS = ("screen", "deep", "draft_cli", "draft_api", "check_cli", "check_api")
ACCESS_USE = {"screen": ma.SCREEN, "deep": ma.DEEP, "drafting": ma.DRAFT, "check": ma.FACTCHECK}
FIELD_LABELS = {
    "screen": "Model",
    "deep": "Model",
    "runner": "Runner",
    "draft_cli": "Model on the subscription",
    "draft_api": "Model on the API key",
    "effort": "Effort",
    "entailment_check": "Check each draft",
    "check_cli": "Model on the subscription",
    "check_api": "Model on the API key",
}
RUNNER_LABELS = {"cli": "Subscription (claude CLI)", "api": "API key"}
EFFORTS = ("low", "medium", "high")
ALSO_USED_BY = {
    "screen": "Score now, backfill, llm bench, a hand-run score --submit, queued re-scores",
    "deep": "Deep pass on Today and the job page, jobhunter score --deep N and --deep-group GID",
}
# Typical request sizes for the drafting estimate, as the packet page uses (packet_docs).
TYPICAL_CHARS = {"resume": 32_000, "cover_letter": 36_000, "question_draft": 20_000}
KIND_WORDS = {"resume": "a resume", "cover_letter": "a letter", "question_draft": "an answer"}
LOCAL_IDS_MAX = 50
LOCAL_ID_CHARS = 200
HIDDEN = "<a value that cannot be shown>"
STATUS_ICON = {ma.OK: "✓", ma.LIKELY: "~", ma.MISSING: "✕", ma.UNKNOWN: "?", "optional": "\u2013"}
PROVIDER_LABELS = {
    ma.ANTHROPIC: "Anthropic API",
    ma.SUBSCRIPTION: "Claude subscription",
    ma.OPENROUTER: "OpenRouter and Jev",
    ma.OPENAI_COMPAT: "openai-compat endpoint",
    ma.LOCAL: "Local model server",
}
LOCAL_TEXT = {
    "reachable": "reachable",
    "down": "not reachable",
    "key": "answered but asks for a key (set LLAMA_API_KEY, or the name in [scoring.local] "
    "api_key_env)",
    "empty": "answered, listed no models",
}
FLASH = {
    "reset-file": "models.toml was emptied; the old file is kept beside it.",
    "loaded": "Loaded models.toml into the running settings.",
    "nothing": "Nothing to load: the running settings already match models.toml.",
    "stale": "models.toml changed elsewhere; this page is reloaded with what is there now.",
    "refused-stale": "Not saved: models.toml changed elsewhere since this page was loaded. "
    "Reload the page to see what is there now.",
    "refreshed": "The OpenRouter model list was refreshed.",
    "refresh-failed": "The OpenRouter list could not be fetched; the old list is kept.",
    "not-broken": "models.toml is valid TOML; reset single rows instead.",
}


def peer_is_loopback(request: Request) -> bool:
    """The TCP peer, not the Host header: a LAN browser under ``--allow-remote``, or the
    container's pasta fallback (018 D11), is not this machine."""
    from jobhunter.console.app import is_loopback  # app imports this module

    host = request.client.host if request.client else ""
    return bool(host) and is_loopback(host)


def hhmm(when: datetime) -> str:
    return when.astimezone().strftime("%H:%M")


def text_of(value: Any) -> str:
    """A field's value as the form's text; "" when it may not be shown (or is blocked)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value == BLOCKED_SPEC:
        return ""
    return shown_value(value) or ""


def clean_local_ids(ids: list[str]) -> list[str]:
    """At most 50 ids, each cut to 200 characters (escaped when rendered)."""
    out: list[str] = []
    for raw in ids:
        text = str(raw).strip()[:LOCAL_ID_CHARS]
        if text and text not in out:
            out.append(text)
        if len(out) >= LOCAL_IDS_MAX:
            break
    return out


# ─── View models ────────────────────────────────────────────────────────────


@dataclass
class Opt:
    value: str
    label: str
    group: str = ""


@dataclass
class FieldView:
    name: str
    label: str
    kind: str  # "select" | "radio" | "check"
    value: str  # what the control has selected now (the effective value, or a candidate)
    shown: str  # the effective value the page showed: Save writes only what differs
    options: list[Opt] = field(default_factory=list)
    source: str = ""
    note: str = ""  # why the effective value would not be offered
    active: bool = True  # the model field of the runner in use

    @property
    def groups(self) -> list[tuple[str, list[Opt]]]:
        """The options as (optgroup label, options), in order ("" for no optgroup)."""
        out: list[tuple[str, list[Opt]]] = []
        for o in self.options:
            if out and out[-1][0] == o.group:
                out[-1][1].append(o)
            else:
                out.append((o.group, [o]))
        return out


@dataclass
class RowState:
    """What a write or preview left on a row: candidate values and a message."""

    values: dict[str, str] = field(default_factory=dict)
    shown: dict[str, str] = field(default_factory=dict)
    message: str = ""
    kind: str = ""  # "ok" | "error" | "warn"
    reasons: list[str] = field(default_factory=list)  # "save anyway" reasons
    queued_line: str = ""
    queued_old: str = ""  # the model "keep" would keep queued re-scores on
    action: str = ""  # the URL the confirm buttons post to (save, reset or revert)
    # {name: value text} that "save anyway" approves. Sent back as force_for_<name> and
    # honoured only for exactly that value: a changed value is judged again.
    approve: dict[str, str] = field(default_factory=dict)
    effect: list[str] = field(default_factory=list)


@dataclass
class RowView:
    key: str
    title: str
    fields: list[FieldView]
    source: str
    lines: list[str]
    access: ma.Access | None
    privacy: str | None
    can_reset: bool
    blocked: str
    state: RowState
    commands: list[str]
    also: str = ""


@dataclass
class Ctx:
    """Everything one request reads once: settings, layers, the cached OpenRouter list."""

    settings: Settings
    layers: Any
    disk: Any
    conn: sqlite3.Connection
    mode: str
    now: datetime
    environ: Mapping[str, str] | None
    home: Path | None
    cache: Any
    local_ids: list[str] | None
    history_times: dict[str, tuple[str, str]]
    peer_ok: bool
    allow_remote: bool
    provider_access: dict[str, ma.Access] = field(default_factory=dict)

    @property
    def prefix(self) -> str:
        return "jobhunter" if self.mode == rd.HOST else "jobhunter-ctr"

    def access_of(self, provider: str) -> ma.Access:
        if provider not in self.provider_access:
            self.provider_access[provider] = _provider_access(self, provider)
        return self.provider_access[provider]


def _provider_access(ctx: Ctx, provider: str) -> ma.Access:
    kw = {"environ": ctx.environ, "home": ctx.home}
    if provider == ma.SUBSCRIPTION:
        if ctx.mode == rd.CONTAINER:
            return ma.Access(
                ma.SUBSCRIPTION, ma.UNKNOWN, ma.CONTAINER_DRAFTING, ma.HELP[ma.SUBSCRIPTION]
            )
        return ma.subscription_access(ctx.settings, now=ctx.now, conn=ctx.conn, **kw)
    spec = {
        ma.ANTHROPIC: "anthropic:claude-haiku-5-5",
        ma.OPENROUTER: "openrouter:any/model",
        ma.OPENAI_COMPAT: "openai-compat:any",
        ma.LOCAL: "local:any",
    }[provider]
    return ma.scorer_access(spec, ctx.settings, mode=ctx.mode, **kw)


# ─── Building the page ──────────────────────────────────────────────────────


def effective(settings: Settings, name: str) -> Any:
    section, key = mc.NAMES[name]
    return getattr(getattr(settings, section), key)


def source_label(ctx: Ctx, name: str) -> str:
    section, key = mc.NAMES[name]
    layers = ctx.layers
    if layers is None:
        return ""
    src = layers.source(section, key)
    if src == "models.toml":
        when, how = ctx.history_times.get(f"{section}.{key}", ("", ""))
        if not when:
            label = "set in models.toml"
        elif how == "cli":
            label = f"set with jobhunter models at {when}"
        else:
            label = f"saved on /models at {when}"
        if (section, key) in layers.config_keys:
            base = text_of(getattr(getattr(layers.base, section), key)) or HIDDEN
            label += f"; config.toml says {base}, /models overrides it"
        return label
    return {
        "models.toml (invalid)": "models.toml (invalid value)",
        "override": "set when the console started",
    }.get(src, src)


def _select_options(ctx: Ctx, name: str, value: str, shown: str, blocked: bool) -> list[Opt]:
    scoring = ctx.settings.scoring
    offered = catalog.options(name, scoring, cache=ctx.cache, local_ids=ctx.local_ids)
    out: list[Opt] = []
    values = {o.value for o in offered.options}
    if blocked:
        out.append(Opt("", "Blocked: the value in models.toml is invalid", "In effect"))
    elif shown == "" and not blocked:
        out.append(Opt("", HIDDEN, "In effect"))
    elif shown not in values:
        out.append(Opt(shown, shown, "In effect"))
    if value and value != shown and value not in values:
        out.append(Opt(value, f"{value} (typed)", "Typed"))
    for o in offered.options:
        label = o.label if o.label == o.value else f"{o.label} ({o.value})"
        if o.note:
            label += f" [{o.note}]"
        group = "Older" if o.older else "Available"
        provider = ma.provider_of(o.value)
        if name == "screen" and not o.older and ctx.access_of(provider).status == ma.MISSING:
            group = "Needs a key"
        out.append(Opt(o.value, label, group))
    order = {"In effect": 0, "Typed": 1, "Available": 2, "Needs a key": 3, "Older": 4}
    out.sort(key=lambda o: order.get(o.group, 5))
    return out


def _not_offered(ctx: Ctx, name: str, value: str) -> str:
    if not value:
        return ""
    verdict = catalog.judge(name, value, ctx.settings.scoring, cache=ctx.cache,
                            local_ids=ctx.local_ids)  # fmt: skip
    if verdict.offered:
        return verdict.note
    return f"not offered for {catalog.USE_LABELS[name].lower()}: {verdict.reason}"


def candidate_settings(settings: Settings, values: Mapping[str, Any]) -> Settings:
    """``settings`` with candidate values, for prices and access only (never assigned)."""
    updates: dict[str, dict[str, Any]] = {}
    for name, value in values.items():
        section, key = mc.NAMES[name]
        updates.setdefault(section, {})[key] = value
    new = {s: getattr(settings, s).model_copy(update=u) for s, u in updates.items()}
    return settings.model_copy(update=new)


def _money(usd: float) -> str:
    return f"${usd:.2f}" if usd >= 0.1 else f"${usd:.4f}".rstrip("0").rstrip(".")


def price_lines(ctx: Ctx, row: str, values: Mapping[str, Any]) -> list[str]:
    """What the row's choice costs, with the source of each number. Local and free."""
    conn, scoring = ctx.conn, ctx.settings.scoring
    if row == "screen":
        spec = values["screen"]
        if not spec:
            return []
        est = catalog.screen_estimate(conn, spec, scoring, cache=ctx.cache)
        if est.usd is None:
            return [f"No known price ({est.source})."]
        lines = [f"About {_money(est.usd * 1000)} per 1,000 jobs ({est.source})."]
        n = catalog.jobs_screened_last_week(conn, ctx.now)
        if n:
            lines.append(
                f"About {_money(est.usd * n)} a week at your volume ({n} jobs screened in "
                "the last 7 days)."
            )
        return lines
    if row == "deep":
        spec = values["deep"]
        if not spec:
            return []
        est = catalog.deep_estimate(conn, spec)
        if est.usd is None:
            return [f"No known price ({est.source})."]
        return [f"About {_money(est.usd)} a job ({est.source}). It runs only when you ask."]
    settings = candidate_settings(ctx.settings, values)
    if row == "drafting":
        from jobhunter.apply import generator

        if values["runner"] == "cli":
            line = "$0 on your subscription"
            api = catalog.cli_api_equivalent(conn, "resume", values["draft_cli"])
            if api.usd is not None:
                line += f"; about {_money(api.usd)} a resume at API prices ({api.source})"
            return [line + "."]
        parts, sources = [], []
        for kind in TYPICAL_CHARS:
            est = generator.estimate(conn, settings, kind, TYPICAL_CHARS[kind],
                                     model=values["draft_api"], runner="api")  # fmt: skip
            parts.append(f"{_money(est.usd)} {KIND_WORDS[kind]}")
            sources.append(est.source)
        src = ", ".join(dict.fromkeys(sources))
        tail = " (fact check included)" if settings.apply.entailment_check else ""
        return [f"About {', '.join(parts)}{tail} ({src})."]
    if row == "check":
        if not values["entailment_check"]:
            return ["Off: drafts are not fact-checked."]
        runner = ctx.settings.apply.runner
        model = values["check_cli"] if runner == "cli" else values["check_api"]
        est = catalog.check_estimate(model)
        line = f"About {_money(est.usd or 0)} a check ({est.source})"
        if runner == "cli":
            line += "; $0 on your subscription"
        return [line + "."]
    return []


def row_choice(ctx: Ctx, row: str, values: Mapping[str, Any]) -> ma.Choice | None:
    runner = values.get("runner", ctx.settings.apply.runner)
    if row in ("screen", "deep"):
        return ma.Choice(values[row]) if values[row] else None
    if row == "drafting":
        return ma.Choice(values["draft_cli"] if runner == "cli" else values["draft_api"], runner)
    if not values["entailment_check"]:
        return None
    return ma.Choice(values["check_cli"] if runner == "cli" else values["check_api"], runner)


def build_row(ctx: Ctx, row: str, state: RowState | None = None) -> RowView:
    title, names = ROWS[row]
    state = state or RowState()
    settings = ctx.settings
    blocked_map = settings.scoring.scorer_problems()
    fields: list[FieldView] = []
    values: dict[str, Any] = {}
    for name in names:
        eff = effective(settings, name)
        shown = state.shown.get(name, text_of(eff))
        value = state.values.get(name, shown)
        # The placeholder only for the blocked marker: a malformed config.toml value is
        # still the value in effect, shown with its source and the problem.
        blocked = eff == BLOCKED_SPEC and name not in state.values
        if name == "runner":
            opts = [Opt(r, RUNNER_LABELS[r]) for r in ("cli", "api")]
            fv = FieldView(name, FIELD_LABELS[name], "radio", value, shown, opts)
        elif name == "effort":
            opts = [Opt(e, e) for e in EFFORTS]
            fv = FieldView(name, FIELD_LABELS[name], "select", value, shown, opts)
        elif name == "entailment_check":
            fv = FieldView(name, FIELD_LABELS[name], "check", value, shown)
        else:
            opts = _select_options(ctx, name, value, shown, blocked)
            fv = FieldView(name, FIELD_LABELS[name], "select", value, shown, opts)
            fv.note = _not_offered(ctx, name, value) if value else ""
        fv.source = source_label(ctx, name)
        fields.append(fv)
        values[name] = value == "true" if name == "entailment_check" else value
    runner = values.get("runner", settings.apply.runner)
    for fv in fields:
        if fv.name in ("draft_cli", "check_cli"):
            fv.active = runner == "cli"
        elif fv.name in ("draft_api", "check_api"):
            fv.active = runner == "api"
    choice = row_choice(ctx, row, values)
    access = None
    if choice is not None:
        try:
            access = ma.access_for(ACCESS_USE[row], choice, settings, mode=ctx.mode, now=ctx.now,
                                   conn=ctx.conn, environ=ctx.environ, home=ctx.home)  # fmt: skip
        except ValueError:
            access = None
    privacy = None
    if row == "screen" and values["screen"]:
        try:
            privacy = privacy_notice(values["screen"], settings.scoring)
        except ValueError:
            privacy = None
    blocked = ""
    if row in ("screen", "deep"):
        blocked = blocked_map.get(mc.NAMES[row][1], "")
    try:
        lines = price_lines(ctx, row, values)
    except Exception as exc:  # a price must never take the page down
        logger.warning("models: price for %s failed (%s)", row, type(exc).__name__)
        lines = ["No estimate available."]
    layers = ctx.disk
    can_reset = layers is not None and any(
        mc.NAMES[n][1] in layers.layer.get(mc.NAMES[n][0], {})
        or mc.NAMES[n][1] in layers.invalid.get(mc.NAMES[n][0], {})
        for n in names
    )
    sources = list(dict.fromkeys(f.source for f in fields if f.source))
    commands = [f"{ctx.prefix} models set {f.name} {f.shown or 'VALUE'}" for f in fields]
    commands.append(f"{ctx.prefix} models reset {row}")
    return RowView(
        key=row,
        title=title,
        fields=fields,
        source="; ".join(sources) if len(fields) == 1 else "",
        lines=lines,
        access=access,
        privacy=privacy,
        can_reset=can_reset,
        blocked=blocked,
        state=state,
        commands=commands,
        also=ALSO_USED_BY.get(row, ""),
    )


@dataclass
class AccessRow:
    access: ma.Access
    label: str
    word: str
    icon: str
    needed_by: list[str]
    check: str = ""  # "local" for the Check server button


def access_rows(ctx: Ctx) -> list[AccessRow]:
    settings = ctx.settings
    needs: dict[str, list[str]] = {}
    for use in ma.USES:
        if use == ma.FACTCHECK and not settings.apply.entailment_check:
            continue
        choice = ma.choice_for(use, settings)
        if use in ma.SCORING_USES:
            if choice.model == BLOCKED_SPEC:
                continue
            provider = ma.provider_of(choice.model)
        else:
            provider = ma.SUBSCRIPTION if choice.runner == "cli" else ma.ANTHROPIC
        needs.setdefault(provider, []).append(ma.USE_LABELS[use].lower())
    providers = [ma.ANTHROPIC, ma.SUBSCRIPTION, ma.OPENROUTER]
    if ma.OPENAI_COMPAT in needs:
        providers.append(ma.OPENAI_COMPAT)
    providers.append(ma.LOCAL)
    rows = []
    for provider in providers:
        access = ctx.access_of(provider)
        word = access.status
        if access.status == ma.MISSING and provider not in needs:
            word = "optional"
        rows.append(
            AccessRow(
                access,
                PROVIDER_LABELS[provider],
                word,
                STATUS_ICON[word],
                needs.get(provider, []),
                check="local" if provider == ma.LOCAL else "",
            )
        )
    return rows


def banner(ctx: Ctx, *, watching: bool = False) -> dict[str, Any]:
    """The /models banner: blocked uses, a broken file, fallbacks, missing access, and an
    outside change to models.toml. ``watching``: the config watch runs, so an outside change
    is applied there (Config changes, Apply now), and this banner links to it instead of
    offering Load (specs/020 "Config watch": one flow, no double apply)."""
    settings = ctx.settings
    disk = ctx.disk
    lines: list[str] = list(settings.scoring.scorer_problems().values())
    lines += settings.models_notices
    file_problems = []
    if disk is not None:
        file_problems = [p.message for p in disk.problems if p.key is None or p.kind == "unknown"]
    try:
        access = ma.problems(ma.USES, settings, mode=ctx.mode, now=ctx.now, conn=ctx.conn,
                             environ=ctx.environ, home=ctx.home)  # fmt: skip
    except ValueError:
        access = []
    nothing = (
        ctx.access_of(ma.ANTHROPIC).status == ma.MISSING
        and ctx.access_of(ma.OPENROUTER).status == ma.MISSING
        and ctx.access_of(ma.SUBSCRIPTION).status == ma.MISSING
        and not settings.scoring.local.base_url
        and not settings.scoring.screen_scorer.startswith("local:")
    )
    pending = []
    if disk is not None and ctx.layers is not None:
        pending = mc.pending_load(settings, disk)
    return {
        "lines": lines,
        "file_problems": file_problems,
        "broken": disk is not None and disk.broken,
        "access": [] if nothing else [(ma.summary(a), a.help) for a in access],
        "nothing": nothing,
        "pending": pending,
        "watching": watching,
    }


def history_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    try:
        return mc.history(conn)
    except sqlite3.Error:
        return []


def reset_file_changes(before: Any, after: Any) -> dict[str, tuple[Any, Any]]:
    """{name: (old, new)} for each value **Reset file** changes, for the history. A blocked
    value is recorded as not set (None): the blocked marker is not a value to revert to."""
    out: dict[str, tuple[Any, Any]] = {}
    for name, (section, key) in mc.NAMES.items():
        old = getattr(getattr(before.merged, section), key)
        new = getattr(getattr(after.merged, section), key)
        if old == new:
            continue
        out[name] = (None if old == BLOCKED_SPEC else old, None if new == BLOCKED_SPEC else new)
    return out


def history_times(conn: sqlite3.Connection) -> dict[str, tuple[str, str]]:
    """{field: (HH:MM local, source)} of the latest write of each field."""
    out: dict[str, tuple[str, str]] = {}
    for r in history_rows(conn):
        if r["field"] in out:
            continue
        try:
            out[r["field"]] = (hhmm(datetime.fromisoformat(r["at"])), r["source"])
        except ValueError:
            continue
    return out


# ─── Registration ───────────────────────────────────────────────────────────


def register(
    app: FastAPI,
    templates: Jinja2Templates,
    get_conn: Callable[..., Any],
    nav: list[tuple[str, str]],
    now: Callable[[], datetime],
) -> None:
    Conn = Annotated[sqlite3.Connection, Depends(get_conn)]
    if getattr(app.state, "settings_lock", None) is None:  # create_app makes it
        import threading

        app.state.settings_lock = threading.Lock()

    def watching(request: Request) -> bool:
        return getattr(request.app.state, "config_watch", None) is not None

    def mode_of(request: Request) -> str:
        override = getattr(request.app.state, "setup_mode", None)
        return override or rd.detect_mode(settings=request.app.state.settings)

    def make_ctx(
        request: Request, conn: sqlite3.Connection, local_ids: list[str] | None = None
    ) -> Ctx:
        settings: Settings = request.app.state.settings
        layers = layers_for(settings)
        disk = relayer(layers)  # one small read of models.toml: what a write is based on
        return Ctx(
            settings=settings,
            layers=layers,
            disk=disk,
            conn=conn,
            mode=mode_of(request),
            now=now(),
            environ=getattr(request.app.state, "setup_environ", None),
            home=getattr(request.app.state, "setup_home", None),
            cache=catalog.load_openrouter_cache(settings.scoring.openrouter.catalog_cache),
            local_ids=local_ids,
            history_times=history_times(conn),
            peer_ok=peer_is_loopback(request),
            allow_remote=bool(request.app.state.allow_remote),
        )

    def page_context(
        request: Request,
        ctx: Ctx,
        states: Mapping[str, RowState] | None = None,
        flash: str = "",
        history_msg: RowState | None = None,
        local: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        states = states or {}
        fetched = None
        if ctx.cache is not None and ctx.cache.fetched_at:
            fetched = datetime.fromtimestamp(ctx.cache.fetched_at, UTC)
        return {
            "title": "Models",
            "active": "/models",
            "nav": nav,
            "ctx": ctx,
            "digest": ctx.disk.digest if ctx.disk is not None else "",
            "rows": [build_row(ctx, r, states.get(r)) for r in ROWS],
            "access_rows": access_rows(ctx),
            "banner": banner(ctx, watching=watching(request)),
            "history": history_rows(ctx.conn),
            "history_msg": history_msg,
            "flash": flash,
            "prices_checked": catalog.PRICES_CHECKED,
            "openrouter_fetched": fetched,
            "local": local,
            "file": str(ctx.disk.path) if ctx.disk is not None else "",
        }

    def render(name: str, request: Request, context: dict[str, Any], status: int = 200):
        return templates.TemplateResponse(request, name, context, status_code=status)

    @app.get("/models", response_class=HTMLResponse)
    def models_page(request: Request, conn: Conn, done: str = "") -> HTMLResponse:
        ctx = make_ctx(request, conn)
        return render("models.html", request, page_context(request, ctx, flash=FLASH.get(done, "")))

    def candidate_from(
        form: Mapping[str, Any], getlist, row: str
    ) -> tuple[dict[str, str], dict[str, str], list[str]]:
        """(values, shown, refused) the row's form posted, as text. A model value that is not
        a usable model name (key-shaped, or characters no model id has) is dropped, never
        echoed: ``refused`` names its field."""
        values: dict[str, str] = {}
        shown: dict[str, str] = {}
        refused: list[str] = []
        for name in ROWS[row][1]:
            if name == "entailment_check":
                values[name] = "true" if "true" in getlist(name) else "false"
            else:
                values[name] = str(form.get(name, "") or "").strip()
            if name == "screen" and (other := str(form.get("screen_other", "") or "").strip()):
                values[name] = other
            if name in MODEL_FIELDS and values[name] and shown_value(values[name]) is None:
                values.pop(name)
                refused.append(name)
            if f"shown_{name}" in form:
                shown[name] = shown_value(str(form.get(f"shown_{name}", ""))) or ""
                if name in ("runner", "effort", "entailment_check"):
                    shown[name] = str(form.get(f"shown_{name}", ""))[:16]
        return values, shown, refused

    REFUSED_TEXT = "That is not a usable model name, so it was not kept."

    def approved_from(form: Mapping[str, Any]) -> dict[str, str]:
        return {
            name: str(form.get(f"force_for_{name}"))
            for name in mc.NAMES
            if form.get(f"force_for_{name}") is not None
        }

    @app.post("/models/{row}/preview", response_class=HTMLResponse)
    async def models_preview(request: Request, row: str) -> Response:
        """Price, access and queued lines for a candidate. A POST (the middleware's same-origin
        rule, no peer gate), so a typed spec never lands in a URL or an access log. Writes
        nothing but the prefilter catch-up a queued line plans with."""
        if row not in ROWS:
            return PlainTextResponse("unknown row", status_code=404)
        form, getlist = await read_form(request)
        values, shown, refused = candidate_from(form, getlist, row)
        local_ids = clean_local_ids(getlist("local_id")) or None

        def work() -> Response:
            conn = request.app.state.conn_factory()
            try:
                ctx = make_ctx(request, conn, local_ids)
                state = RowState(values=values, shown=shown)
                if refused:
                    state.message, state.kind = REFUSED_TEXT, "error"
                if row == "screen" and ctx.peer_ok:
                    _queued_preview(ctx, state, values.get("screen", ""), shown.get("screen"))
                view = build_row(ctx, row, state)
                return render("_models_preview.html", request, {"r": view, "ctx": ctx})
            finally:
                conn.close()

        return await run_in_threadpool(work)

    def disk_screen(layers: Any) -> str | None:
        """The screening spec models.toml on disk gives (what tonight's drain will plan
        with), or None when it is blocked."""
        merged = layers.merged.scoring
        return None if "screen_scorer" in merged.scorer_problems() else merged.screen_scorer

    def _queued_preview(ctx: Ctx, state: RowState, new_spec: str, shown: str | None) -> None:
        """The queued re-score line for a screening change (specs/020 "Queued re-scores")."""
        old = ctx.settings.scoring.screen_scorer
        old_spec = disk_screen(ctx.disk)
        if not new_spec or new_spec == (shown if shown is not None else text_of(old)):
            return
        line = queued_line(ctx.conn, ctx.settings, ctx.now, old_spec, new_spec)
        if line:
            state.queued_line = line
            state.queued_old = old_spec or ""

    def queued_line(
        conn: sqlite3.Connection,
        settings: Settings,
        when: datetime,
        old_spec: str | None,
        new_spec: str,
    ) -> str:
        """Planned the way the drain runs (``model_choices.queued_impact``); its only write
        is the free prefilter catch-up."""
        pending = mc.unplanned_requests(conn)
        if not pending:
            return ""
        st = gr.lenient_state(settings)
        impact = None
        if st.kind == "ok":
            impact = mc.queued_impact(conn, st.profile, settings.scoring,
                                      old_spec or "(blocked)", new_spec, when)  # fmt: skip
        if impact is not None:
            return impact.line
        return (
            f"{len(pending)} queued re-score(s) follow the screening model (could not price them)"
        )

    # ── writes ──

    def refuse_peer() -> Response:
        return PlainTextResponse(
            "refused: model choices can be changed only from the machine running the console; "
            "use `jobhunter models set` there",
            status_code=403,
        )

    async def read_form(request: Request):
        form = await request.form()
        return form, form.getlist

    def digest_of(form: Mapping[str, Any]) -> str:
        return str(form.get("digest") or form.get("page_digest") or "")

    @dataclass
    class Outcome:
        status: int = 200
        state: RowState = field(default_factory=RowState)
        saved: bool = False
        stale: bool = False
        wrote: bool = False  # models.toml was written: only then is a new digest handed out

    def apply_write(
        request: Request,
        conn: sqlite3.Connection,
        changes: dict[str, Any],
        *,
        digest: str,
        source: str,
        queued: str,
        approved: Mapping[str, str],
        local_ids: list[str] | None,
        state: RowState,
        action: str,
    ) -> Outcome:
        """Validate, settle the queued question, write, swap. Holds ``settings_lock`` from the
        first read of the running settings to the assignment, so two writes serialize."""
        with request.app.state.settings_lock:
            running: Settings = request.app.state.settings
            layers = layers_for(running)
            disk = relayer(layers)
            if disk.digest != digest:  # moved under us: refused before anything is written
                state.message = (
                    "Not saved: models.toml changed elsewhere since this page was loaded. "
                    "Reload the page to see what is there now."
                )
                state.kind = "error"
                return Outcome(409, state, stale=True)
            reasons: list[str] = []
            needs: dict[str, str] = {}
            ok_approved: dict[str, str] = {}
            for name, value in changes.items():
                if value is None:
                    continue
                check = mc.check_save(
                    name,
                    value,
                    running.scoring,
                    local_ids=local_ids,
                    settings=running if name in ("screen", "deep") else None,
                )
                if check.error:
                    state.message, state.kind = f"Not saved: {check.error}", "error"
                    return Outcome(422, state)
                if not check.needs_force:
                    continue
                text = mc.to_text(value) or ""
                if approved.get(name) == text:
                    ok_approved[name] = text  # "save anyway" was pressed for this value
                else:
                    reasons += list(check.needs_force)
                    needs[name] = text
            if reasons:
                state.reasons = reasons
                state.approve = {**ok_approved, **needs}
                state.action = action
                state.message, state.kind = "Not saved yet: check the reasons below.", "warn"
                return Outcome(200, state)
            keep_on: str | None = None
            if "screen" in changes:
                # What queued re-scores follow tonight is models.toml on disk, not this
                # console's running value (they differ while a Load is pending).
                old_spec = disk_screen(disk)
                new_spec = changes["screen"]
                if new_spec is None:
                    new_spec = layers.base.scoring.screen_scorer
                if new_spec != old_spec and mc.unplanned_requests(conn):
                    if queued not in ("new", "keep"):
                        state.queued_line = queued_line(conn, running, now(), old_spec, new_spec)
                        state.queued_old = old_spec or ""
                        state.action = action
                        state.approve = ok_approved
                        state.message, state.kind = (
                            "Not saved yet: choose what queued re-scores use.",
                            "warn",
                        )
                        return Outcome(200, state)
                    if queued == "keep":
                        if old_spec is None:
                            state.message = (
                                "Not saved: the current screening model is blocked, so there "
                                "is none to keep queued re-scores on. Choose the new model."
                            )
                            state.kind = "error"
                            return Outcome(422, state)
                        keep_on = old_spec
            try:
                result = mc.write_changes(layers.path, changes, digest, conn=conn,
                                          source=source, now=now())  # fmt: skip
                if keep_on is not None and "screen" in result.changed:
                    # Only once the write succeeded: a refused write pins nothing.
                    mc.keep_queued(conn, keep_on)
            except mc.StaleDigest:
                state.message = (
                    "Not saved: models.toml changed elsewhere since this page was loaded. "
                    "Reload the page to see what is there now."
                )
                state.kind = "error"
                return Outcome(409, state, stale=True)
            except mc.ModelsFileError as exc:
                state.message, state.kind = f"Not saved: {exc}", "error"
                return Outcome(422, state)
            if result.changed:
                fresh = relayer(layers)
                request.app.state.settings = mc.swap_fields(running, fresh, result.changed)
        saved_at = hhmm(now())
        if not result.changed:
            return Outcome(200, RowState(message="No change: nothing was written.", kind="ok"),
                           saved=True)  # fmt: skip
        verb = {"reset": "Reset", "revert": "Reverted"}.get(source, "Saved")
        done = RowState(message=f"{verb} at {saved_at}.", kind="ok")
        done.effect = effect_lines(result.changed, request.app.state.settings)
        return Outcome(200, done, saved=True, wrote=True)

    def effect_lines(changed: Mapping[str, Any], settings: Settings) -> list[str]:
        out = []
        if "screen" in changed:
            spec = text_of(settings.scoring.screen_scorer) or "the new model"
            out.append(
                f"New jobs from the next run use {spec}; jobs already screened keep their "
                "scores. A hand-run `jobhunter score --submit` scores new jobs only; to "
                "re-screen scored jobs, run a confirmed re-score (Prefs, Re-score). A run "
                "already waiting for the run lock read its settings before it waited and "
                "uses the old model."
            )
        if "deep" in changed:
            spec = text_of(settings.scoring.deep_scorer) or "the new model"
            out.append(
                f"New deep runs use {spec}. Groups already deep-scored keep their reports, "
                "and Deep-score top N (or `jobhunter score --deep N`) moves on to the next N. "
                "To re-run your top groups with it, use Deep-score below and pick Re-run; "
                "one group: its job page, or `jobhunter score --deep-group GID`."
            )
        if any(mc.USE_OF[n] in ("drafting", "check") for n in changed):
            out.append("The next Generate uses it.")
        return out

    def write_response(
        request: Request,
        conn: sqlite3.Connection,
        outcome: Outcome,
        *,
        row: str | None,
        history_msg: RowState | None = None,
        local_ids: list[str] | None = None,
        posted_digest: str = "",
    ) -> Response:
        """htmx: the row (or the history section) plus out-of-band swaps of the digest, the
        banner, the access list and the history. Otherwise the whole page.

        The new digest goes out only when this request wrote models.toml. Any other answer
        (no change, a refusal, a 409) echoes the digest it was posted: handing out the file's
        current digest would let the next save overwrite another writer's change unseen.
        Only a reload, or this page's own write, moves it."""
        ctx = make_ctx(request, conn, local_ids)
        states = {row: outcome.state} if row else {}
        context = page_context(request, ctx, states, history_msg=history_msg)
        context["wrote"] = outcome.wrote
        if not outcome.wrote:
            context["digest"] = posted_digest
        if not request.headers.get("hx-request"):
            if outcome.stale:
                context["flash"] = FLASH["refused-stale"]
            return render("models.html", request, context, outcome.status)
        context["primary"] = row or "history"
        return render("_models_write.html", request, context, outcome.status)

    # Registered last (below): "/models/{row}" would otherwise match /models/load and friends.
    async def models_save(request: Request, row: str) -> Response:
        if row not in ROWS:
            return PlainTextResponse("unknown row", status_code=404)
        if not peer_is_loopback(request):
            return refuse_peer()
        form, getlist = await read_form(request)
        values, shown, refused = candidate_from(form, getlist, row)
        local_ids = clean_local_ids(getlist("local_id")) or None

        def work() -> Response:
            conn = request.app.state.conn_factory()
            try:
                if refused:
                    state = RowState(values=values, shown=shown, message=REFUSED_TEXT,
                                     kind="error")  # fmt: skip
                    return write_response(request, conn, Outcome(422, state), row=row,
                                          posted_digest=digest_of(form))  # fmt: skip
                changes: dict[str, Any] = {}
                for name, text in values.items():
                    if name in shown and text == shown[name]:
                        continue
                    if text == "" and name in MODEL_FIELDS:
                        continue  # the blocked or hidden placeholder: nothing chosen
                    changes[name] = mc.parse_value(name, text)
                state = RowState(values=values, shown=shown)
                if not changes:
                    outcome = Outcome(200, RowState(message="No change: nothing was written.",
                                                    kind="ok"), saved=True)  # fmt: skip
                else:
                    outcome = apply_write(
                        request, conn, changes, digest=digest_of(form), source="ui",
                        queued=str(form.get("queued", "")), approved=approved_from(form),
                        local_ids=local_ids, state=state, action=f"/models/{row}",
                    )  # fmt: skip
                return write_response(request, conn, outcome, row=row, local_ids=local_ids,
                                      posted_digest=digest_of(form))  # fmt: skip
            except mc.ModelsFileError as exc:
                state = RowState(values=values, shown=shown, message=f"Not saved: {exc}",
                                 kind="error")  # fmt: skip
                return write_response(request, conn, Outcome(422, state), row=row,
                                      posted_digest=digest_of(form))  # fmt: skip
            finally:
                conn.close()

        return await run_in_threadpool(work)

    @app.post("/models/{row}/reset")
    async def models_reset(request: Request, row: str) -> Response:
        if row not in ROWS:
            return PlainTextResponse("unknown row", status_code=404)
        if not peer_is_loopback(request):
            return refuse_peer()
        form, _getlist = await read_form(request)

        def work() -> Response:
            conn = request.app.state.conn_factory()
            try:
                changes = dict.fromkeys(ROWS[row][1])
                outcome = apply_write(
                    request, conn, changes, digest=digest_of(form), source="reset",
                    queued=str(form.get("queued", "")), approved={}, local_ids=None,
                    state=RowState(), action=f"/models/{row}/reset",
                )  # fmt: skip
                return write_response(request, conn, outcome, row=row,
                                      posted_digest=digest_of(form))  # fmt: skip
            finally:
                conn.close()

        return await run_in_threadpool(work)

    @app.post("/models/history/{change_id}/revert")
    async def models_revert(request: Request, change_id: int) -> Response:
        if not peer_is_loopback(request):
            return refuse_peer()
        form, getlist = await read_form(request)
        local_ids = clean_local_ids(getlist("local_id")) or None

        def work() -> Response:
            conn = request.app.state.conn_factory()
            try:
                cur = conn.execute("SELECT * FROM model_change WHERE id = ?", (change_id,))
                found = cur.fetchone()
                if found is None:
                    return PlainTextResponse("no such change", status_code=404)
                entry = dict(zip([d[0] for d in cur.description], found, strict=True))
                state = RowState()
                try:
                    name, value = mc.revert_change(entry)
                except mc.ModelsFileError as exc:
                    state.message, state.kind = f"Not reverted: {exc}", "error"
                    return write_response(request, conn, Outcome(422, state), row=None,
                                          history_msg=state,
                                          posted_digest=digest_of(form))  # fmt: skip
                outcome = apply_write(
                    request, conn, {name: value}, digest=digest_of(form), source="revert",
                    queued=str(form.get("queued", "")), approved=approved_from(form),
                    local_ids=local_ids, state=state,
                    action=f"/models/history/{change_id}/revert",
                )  # fmt: skip
                return write_response(request, conn, outcome, row=None,
                                      history_msg=outcome.state,
                                      posted_digest=digest_of(form))  # fmt: skip
            finally:
                conn.close()

        return await run_in_threadpool(work)

    @app.post("/models/reset-file")
    async def models_reset_file(request: Request) -> Response:
        if not peer_is_loopback(request):
            return refuse_peer()
        form, _getlist = await read_form(request)

        def work() -> Response:
            with request.app.state.settings_lock:
                running: Settings = request.app.state.settings
                layers = layers_for(running)
                before = relayer(layers)
                try:
                    mc.reset_file(layers.path, digest_of(form), now=now())
                except mc.StaleDigest:
                    return RedirectResponse("/models?done=stale", status_code=303)
                except mc.Refused:
                    return RedirectResponse("/models?done=not-broken", status_code=303)
                fresh = relayer(layers)
                request.app.state.settings = mc.swap_fields(running, fresh, mc.NAMES)
                changed = reset_file_changes(before, fresh)
                if changed:
                    conn = request.app.state.conn_factory()
                    try:
                        mc.record_changes(conn, changed, source="reset", now=now())
                    finally:
                        conn.close()
            return RedirectResponse("/models?done=reset-file", status_code=303)

        return await run_in_threadpool(work)

    @app.post("/models/load")
    async def models_load(request: Request) -> Response:
        """Take the on-disk models.toml into the running settings, for a console that does
        not run the config watch. When the watch runs, an outside change is the watch's to
        apply (Config changes, Apply now, which reloads config.toml too): Load then writes
        nothing and sends you there, so the two never apply the same change twice."""
        if not peer_is_loopback(request):
            return refuse_peer()
        if watching(request):
            return RedirectResponse("/config/changes?flash=models", status_code=303)
        form, _getlist = await read_form(request)

        def work() -> Response:
            with request.app.state.settings_lock:
                running: Settings = request.app.state.settings
                layers = layers_for(running)
                fresh = relayer(layers)
                if fresh.digest != digest_of(form):
                    return RedirectResponse("/models?done=stale", status_code=303)
                names = mc.pending_load(running, fresh)
                if not names:
                    return RedirectResponse("/models?done=nothing", status_code=303)
                request.app.state.settings = mc.swap_fields(running, fresh, names)
            return RedirectResponse("/models?done=loaded", status_code=303)

        return await run_in_threadpool(work)

    def network_refusal(request: Request) -> Response | None:
        if not peer_is_loopback(request):
            return refuse_peer()
        if request.app.state.allow_remote:
            return PlainTextResponse(
                "refused: this check contacts the network and is off under --allow-remote",
                status_code=403,
            )
        return None

    @app.post("/models/openrouter/refresh")
    def models_openrouter_refresh(request: Request) -> Response:
        """Fetch OpenRouter's public model list (free, no key sent) and rewrite the cache."""
        if (refusal := network_refusal(request)) is not None:
            return refusal
        config = request.app.state.settings.scoring.openrouter
        cache = catalog.refresh_openrouter_cache(config)
        done = "refreshed" if cache is not None else "refresh-failed"
        return RedirectResponse(f"/models?done={done}", status_code=303)

    @app.post("/models/local/check")
    def models_local_check(request: Request, conn: Conn) -> Response:
        """Is the local model server answering, and which models does it list? Fixed text and
        the ids only: ``ServerStatus.detail`` can hold an exception's text and is never shown
        or logged."""
        if (refusal := network_refusal(request)) is not None:
            return refusal
        from jobhunter.scoring.scorers import local_server_status

        factory = getattr(request.app.state, "local_check_client", None)
        client = factory() if factory is not None else None
        try:
            status = local_server_status(request.app.state.settings.scoring.local, client=client)
        except Exception as exc:  # never the text: it can carry a URL or a key echo
            logger.warning("models: local server check failed (%s)", type(exc).__name__)
            status = None
        finally:
            if client is not None:
                client.close()
        if status is None or not status.reachable:
            outcome, ids = "down", []
        elif status.unauthorized:
            outcome, ids = "key", []
        elif not status.models:
            outcome, ids = "empty", []
        else:
            outcome, ids = "reachable", clean_local_ids(status.models)
        local = {"text": LOCAL_TEXT[outcome], "ids": ids, "ok": outcome == "reachable"}
        ctx = make_ctx(request, conn, ids or None)
        context = page_context(request, ctx, local=local)
        name = "_models_local.html" if request.headers.get("hx-request") else "models.html"
        return render(name, request, context)

    app.add_api_route("/models/{row}", models_save, methods=["POST"])
