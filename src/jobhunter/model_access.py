"""Can a model use run, and on what? (specs/020 "Credential detection").

One module answers it; the ``/models`` page, the banners and ``doctor`` only render the answer.
Every check here follows specs/019 "Rules for every check": local and free. Environment
variables, ``stat`` calls, the runner state file, ``shutil.which`` (through
``cli_runner.find_binary``) and one indexed query on a connection the caller passes. Never a
network call, a model call, a subprocess, the keyring, or a read of a credentials file. No value
is ever put in an :class:`Access`: ``detail`` is fixed wording plus variable names and dates.

Statuses:

- ``ok``: found, and for the subscription a draft has worked recently.
- ``likely``: found, but only a live check can tell (an SDK profile; a Claude login that has not
  drafted lately).
- ``missing``: not found. The only status that raises a banner (:func:`problems`).
- ``unknown``: this process cannot see it (a local server; drafting from inside a container).

The Claude Code login file. Confirmed on 2026-10-11 against the installed CLI (2.1.295, the
bundled JavaScript, not the file itself): the login is ``<dir>/.credentials.json`` where
``<dir>`` is ``CLAUDE_SECURESTORAGE_CONFIG_DIR`` when that variable is set (empty means
``~/.claude``), else ``CLAUDE_CONFIG_DIR``, else ``~/.claude``. On macOS the CLI keeps the login
in the Keychain, which a ``stat`` cannot see, so there a missing file answers ``unknown``.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from jobhunter import readiness
from jobhunter.apply import cli_runner, runner_state
from jobhunter.core.spend import PACKET_CLI_TIER
from jobhunter.scoring import scorers

# --- vocabulary ------------------------------------------------------------------------------

OK = "ok"
LIKELY = "likely"
MISSING = "missing"
UNKNOWN = "unknown"
STATUSES = (OK, LIKELY, MISSING, UNKNOWN)

ANTHROPIC = "anthropic"
OPENROUTER = "openrouter"
OPENAI_COMPAT = "openai-compat"
LOCAL = "local"
SUBSCRIPTION = "subscription"

SCREEN = "screen"
DEEP = "deep"
DRAFT = "draft"
FACTCHECK = "factcheck"
USES = (SCREEN, DEEP, DRAFT, FACTCHECK)
SCORING_USES = (SCREEN, DEEP)
DRAFTING_USES = (DRAFT, FACTCHECK)

USE_LABELS = {
    SCREEN: "Screening",
    DEEP: "The deep pass",
    DRAFT: "Drafting",
    FACTCHECK: "The fact check",
}

# Help topics (specs/019 Addendum A). Topic pages only for now: the anchors the spec names
# (``scorers#get-an-anthropic-api-key`` and the like) are added by 020 M6, and a link to a
# missing anchor would fail 019's link test.
HELP = {
    ANTHROPIC: "scorers",
    OPENROUTER: "openrouter",
    OPENAI_COMPAT: "scorers",
    LOCAL: "scorers",
    SUBSCRIPTION: "drafting",
}

# A subscription draft this recent counts as "worked" (only while a login is still found).
RECENT_DRAFT_DAYS = 30

CREDENTIALS_FILE = ".credentials.json"
OAUTH_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"

CONTAINER_DRAFTING = "drafting runs on the host with `jobhunter apply draft`; check there"


@dataclass(frozen=True)
class Access:
    provider: str  # "anthropic", "openrouter", "openai-compat", "local", "subscription"
    status: str  # "ok" | "likely" | "missing" | "unknown"
    detail: str  # fixed wording, never a value
    help: str  # help topic (and anchor once M6 adds it), e.g. "scorers"
    live_check: str | None = None  # 019 S6 check id, once S6 lands
    uses: tuple[str, ...] = ()  # which uses this answer is for (filled by access_for)


@dataclass(frozen=True)
class Choice:
    """What a use runs on: a scorer spec for screening and the deep pass; a runner and a model
    for drafting and the fact check."""

    model: str
    runner: str | None = None  # "cli" | "api" for drafting uses, None for scoring


def choice_for(use: str, settings: Any) -> Choice:
    """The effective choice for ``use`` from the loaded settings."""
    if use == SCREEN:
        return Choice(settings.scoring.screen_scorer)
    if use == DEEP:
        return Choice(settings.scoring.deep_scorer)
    apply_cfg = settings.apply
    runner = apply_cfg.runner
    if use == DRAFT:
        return Choice(apply_cfg.cli_model if runner == "cli" else apply_cfg.api_model, runner)
    if use == FACTCHECK:
        model = (
            apply_cfg.cli_entailment_model if runner == "cli" else (apply_cfg.api_entailment_model)
        )
        return Choice(model, runner)
    raise ValueError(f"unknown model use {use!r}")


# --- providers -------------------------------------------------------------------------------


def provider_of(spec: str) -> str:
    """The provider family an Access row names: Jev and decisions ride on OpenRouter's key."""
    head, sep, _ = spec.partition(":")
    if not sep:
        return ANTHROPIC
    if head in (OPENROUTER, *scorers.DECISIONS_PROVIDERS):
        return OPENROUTER
    return head


def _from_credential(provider: str, cred: scorers.Credential) -> Access:
    names = " or ".join(cred.names)
    if cred.status == scorers.PRESENT:
        if cred.note == scorers.SDK_NOTE:
            return Access(provider, LIKELY, f"via SDK profile ({names}), not checked",
                          HELP.get(provider, "scorers"))  # fmt: skip
        detail = f"{names} set" if names else (cred.note or "no key needed")
        if names and cred.note:
            detail = f"{detail}; {cred.note}"
        return Access(provider, OK, detail, HELP.get(provider, "scorers"))
    if cred.status == scorers.MISSING:
        wording = {
            ANTHROPIC: "No Anthropic API credential found",
            OPENROUTER: "No OpenRouter key found",
            OPENAI_COMPAT: "No key found for the openai-compat endpoint",
        }.get(provider, f"No {provider} credential found")
        tail = f" ({names} unset)" if names else ""
        return Access(provider, MISSING, wording + tail, HELP.get(provider, "scorers"))
    if provider == LOCAL:
        return Access(LOCAL, UNKNOWN, "local server: not checked (press Check server)",
                      HELP[LOCAL])  # fmt: skip
    return Access(provider, UNKNOWN, cred.note or "only the server can say",
                  HELP.get(provider, "scorers"))  # fmt: skip


def scorer_access(
    spec: str,
    settings: Any,
    *,
    mode: str,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Access:
    """Screening or the deep pass on ``spec``, judged where it runs (per mode)."""
    cred = readiness.scorer_credential(
        spec, settings.scoring, mode=mode, environ=environ, home=home
    )
    return _from_credential(provider_of(spec), cred)


def anthropic_api_access(
    model: str,
    settings: Any,
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Access:
    """The ``api`` drafting runner: ``credential_for`` over this (the host's) environment."""
    spec = model if model.startswith("anthropic:") else f"anthropic:{model}"
    cred = scorers.credential_for(spec, settings.scoring, environ=environ, home=home)
    return _from_credential(ANTHROPIC, cred)


# --- the subscription ------------------------------------------------------------------------


def claude_login_dir(environ: Mapping[str, str], home: Path | None = None) -> Path:
    """Where the Claude Code CLI keeps ``.credentials.json`` (see the module docstring)."""
    base = home if home is not None else Path.home()
    if "CLAUDE_SECURESTORAGE_CONFIG_DIR" in environ:
        value = environ["CLAUDE_SECURESTORAGE_CONFIG_DIR"].strip()
        return Path(value) if value else base / ".claude"
    if value := environ.get("CLAUDE_CONFIG_DIR", "").strip():
        return Path(value)
    return base / ".claude"


def claude_login_file(environ: Mapping[str, str], home: Path | None = None) -> Path:
    return claude_login_dir(environ, home) / CREDENTIALS_FILE


def _login_found(environ: Mapping[str, str], home: Path | None) -> str | None:
    """How a login was found, or None. Presence and ``stat`` only: never opened."""
    if environ.get(OAUTH_TOKEN_ENV, "").strip():
        return f"{OAUTH_TOKEN_ENV} set"
    path = claude_login_file(environ, home)
    try:
        found = path.is_file()
    except OSError:
        found = False
    return "a login file exists" if found else None


def _recent_cli_draft(conn: sqlite3.Connection | None, now: datetime) -> str | None:
    """The latest ``llm_spend`` day of a subscription draft in the last 30 days, or None."""
    if conn is None:
        return None
    cutoff = (now.astimezone(UTC) - timedelta(days=RECENT_DRAFT_DAYS)).date().isoformat()
    try:
        row = conn.execute(
            "SELECT max(day) FROM llm_spend WHERE tier = ? AND day >= ?",
            (PACKET_CLI_TIER, cutoff),
        ).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row and row[0] else None


def subscription_access(
    settings: Any,
    *,
    now: datetime,
    conn: sqlite3.Connection | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    platform: str | None = None,
) -> Access:
    """The ``cli`` runner on the host, in the order of specs/020 "The subscription"."""
    env = os.environ if environ is None else environ
    help_topic = HELP[SUBSCRIPTION]
    binary = cli_runner.find_binary()
    if binary is None:
        return Access(SUBSCRIPTION, MISSING, "the claude command is not installed", help_topic)
    state = runner_state.load(settings.paths.data_dir)
    if state.off:
        return Access(SUBSCRIPTION, MISSING, f"the CLI runner is off: {state.off_reason}",
                      help_topic)  # fmt: skip
    login = _login_found(env, home)
    if login is None:
        if (platform or sys.platform) == "darwin":
            # The CLI keeps a macOS login in the Keychain, which is never read here.
            return Access(SUBSCRIPTION, UNKNOWN,
                          "claude is installed; its login is in the Keychain, not checked",
                          help_topic)  # fmt: skip
        return Access(SUBSCRIPTION, MISSING, "claude is installed but no login was found",
                      help_topic)  # fmt: skip
    if binary in cli_runner._AUTH_OK:
        return Access(SUBSCRIPTION, OK, "a subscription draft worked in this console session",
                      help_topic)  # fmt: skip
    if day := _recent_cli_draft(conn, now):
        return Access(SUBSCRIPTION, OK, f"a subscription draft worked on {day}", help_topic)
    return Access(SUBSCRIPTION, LIKELY,
                  f"claude is installed and a login exists ({login}); not checked",
                  help_topic)  # fmt: skip


# --- per use, per mode -----------------------------------------------------------------------


def access_for(
    use: str,
    choice: Choice | None,
    settings: Any,
    *,
    mode: str,
    now: datetime,
    conn: sqlite3.Connection | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Access:
    """Access for one use, judged in the environment that will run it (specs/020 "Modes").

    - Screening and the deep pass: this process's environment in host and container mode; the
      podman drop-ins in host-managing mode (019's rule, through ``readiness``).
    - Drafting and the fact check: ``unknown`` in a container, which never drafts; otherwise the
      host's environment and the subscription order, since ``apply draft`` runs on the host.
    """
    if use not in USES:
        raise ValueError(f"unknown model use {use!r}")
    if mode not in readiness.MODES:
        raise ValueError(f"unknown mode {mode!r}")
    choice = choice or choice_for(use, settings)
    if use in SCORING_USES:
        access = scorer_access(choice.model, settings, mode=mode, environ=environ, home=home)
    elif mode == readiness.CONTAINER:
        provider = SUBSCRIPTION if choice.runner == "cli" else ANTHROPIC
        access = Access(provider, UNKNOWN, CONTAINER_DRAFTING, HELP[provider])
    elif choice.runner == "cli":
        access = subscription_access(settings, now=now, conn=conn, environ=environ, home=home)
    elif choice.runner == "api":
        access = anthropic_api_access(choice.model, settings, environ=environ, home=home)
    else:
        raise ValueError(f"unknown drafting runner {choice.runner!r}")
    return replace(access, uses=(use,))


def problems(
    uses: Iterable[str],
    settings: Any,
    *,
    mode: str,
    now: datetime,
    conn: sqlite3.Connection | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    choices: Mapping[str, Choice] | None = None,
) -> list[Access]:
    """What the banners show: ``missing`` only (never ``likely`` or ``unknown``). Uses that
    fail for the same reason are merged into one entry whose ``uses`` names them all."""
    merged: dict[tuple[str, str], Access] = {}
    for use in dict.fromkeys(uses):
        choice = (choices or {}).get(use)
        access = access_for(
            use, choice, settings, mode=mode, now=now, conn=conn, environ=environ, home=home
        )
        if access.status != MISSING:
            continue
        key = (access.provider, access.detail)
        if key in merged:
            merged[key] = replace(merged[key], uses=merged[key].uses + access.uses)
        else:
            merged[key] = access
    return list(merged.values())


def summary(access: Access) -> str:
    """One banner line: "<detail>. Screening and the deep pass cannot run." """
    labels = [USE_LABELS[u] for u in access.uses]
    if not labels:
        return access.detail
    labels = labels[:1] + [label.lower() for label in labels[1:]]
    who = labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " and " + labels[-1]
    return f"{access.detail}. {who} cannot run."
