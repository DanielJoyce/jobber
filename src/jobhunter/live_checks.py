"""Free live checks: the "Test (free)" buttons on /setup and ``jobhunter doctor --live``
(specs/019 "Live checks", ticket S6).

Each check contacts one service on an explicit action and answers with **booleans mapped to
fixed text**. None calls a model, and none can spend a credit:

| kind        | request                                   | answers                               |
|-------------|-------------------------------------------|---------------------------------------|
| anthropic   | ``models.list`` through the ``anthropic`` SDK | accepted                          |
| openrouter  | ``GET <base_url>/key``, openrouter.ai only | accepted; credit remaining on the key |
| local       | ``/v1/models`` then ``/health`` on a loopback URL | reachable; model loaded        |
| gmail       | token refresh, labels list, filters list  | token accepted; label; filters        |
| runner      | ``claude auth status --json``, uncached   | logged in; claude.ai login            |

Rules kept here (019 rule 3, "Live checks"):

- **Never a value.** No key, masked key (OpenRouter's ``label``), email address, account or
  organization, filter criteria, or raw response or exception text reaches a result. Failures
  map to fixed wording, or to an exception type at most.
- **Never stored or logged.** Results live only in the response that shows them; this module
  logs nothing.
- **Network** only through the existing clients: the ``anthropic`` SDK, ``scorers``'
  ``httpx`` transport (OpenRouter, local server), Google's Gmail client, and the ``claude``
  CLI through ``cli_runner``'s one process launch. Tests inject all four.
- The runner check never adds to ``cli_runner._AUTH_OK`` and never turns the runner off.
- In host-managing mode the containers' podman secrets decide, not this shell's environment,
  so secret-backed checks point at ``jobhunter-ctr doctor --live`` instead of running.

A future check that could spend credits must be labelled "Test (may spend credits, about $X)",
must confirm, and must never join ``doctor --live``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from jobhunter import readiness as rd
from jobhunter.config import Settings, resolve_path
from jobhunter.scoring import scorers

ANTHROPIC = "anthropic"
OPENROUTER = "openrouter"
LOCAL = "local"
GMAIL = "gmail"
RUNNER = "runner"
NO_FREE = "none"  # a provider with no free live check (openai-compat, a non-openrouter.ai URL)
KINDS = (ANTHROPIC, OPENROUTER, LOCAL, GMAIL, RUNNER, NO_FREE)

# The /setup rows that carry a live check (019 "Live checks" table), in doctor's order.
LIVE_ROWS = ("scorer.screen", "scorer.deep", "mail.token", "mail.label", "apply.runner")

OK = "ok"
FAILED = "failed"  # the service answered no
ERROR = "error"  # no answer we can read: not reachable, an unexpected reply
NOT_RUN = "not run"  # nothing was contacted

BUTTON_TEXT = "Test (free)"
CTR_LIVE = "jobhunter-ctr doctor --live"
NO_FREE_TEXT = "no free live check for this endpoint"

TITLES = {
    ANTHROPIC: "Anthropic credential",
    OPENROUTER: "OpenRouter or Jev key",
    LOCAL: "Local model",
    GMAIL: "Gmail",
    RUNNER: "Drafting runner (cli)",
    NO_FREE: "Scorer endpoint",
}

_SECRET_BACKED = {ANTHROPIC, OPENROUTER, GMAIL}


@dataclass(frozen=True)
class Fact:
    label: str  # fixed wording
    value: bool | None  # None: could not tell


@dataclass(frozen=True)
class LiveResult:
    kind: str
    title: str
    outcome: str  # ok | failed | error | not run
    facts: tuple[Fact, ...] = ()
    notes: tuple[str, ...] = ()  # fixed wording only
    at: datetime | None = None
    instead: str = ""  # a command to run instead (nothing was contacted)
    rows: tuple[str, ...] = ()  # the /setup rows this answers for

    @property
    def failing(self) -> bool:
        return self.outcome in (FAILED, ERROR)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "title": self.title,
            "outcome": self.outcome,
            "facts": {f.label: f.value for f in self.facts},
            "notes": list(self.notes),
            "at": self.at.isoformat() if self.at else None,
            "instead": self.instead,
            "rows": list(self.rows),
        }


def _result(kind: str, facts: list[Fact], notes: list[str], now: datetime) -> LiveResult:
    if any(f.value is False for f in facts):
        outcome = FAILED
    elif facts and all(f.value is True for f in facts):
        outcome = OK
    else:
        outcome = ERROR
    return LiveResult(kind, TITLES[kind], outcome, tuple(facts), tuple(notes), now)


def _not_run(kind: str, note: str, now: datetime, instead: str = "") -> LiveResult:
    return LiveResult(kind, TITLES[kind], NOT_RUN, (), (note,) if note else (), now, instead)


# --- injectable clients (tests replace these; nothing else does) ----------------------------


def _anthropic_client() -> Any:
    """The SDK client on whatever credential it resolves. No retries: one click, one request."""
    import anthropic

    return anthropic.Anthropic(max_retries=0, timeout=15.0)


def _http_client() -> Any:
    """``None``: ``scorers`` builds and closes its own ``httpx`` client."""
    return None


def _gmail_service(settings: Settings) -> Any:
    from jobhunter.mail import auth

    return auth.build_service(settings)


@dataclass
class Deps:
    """What the checks talk through. ``None`` means the module default."""

    anthropic_client: Callable[[], Any] | None = None
    http_client: Callable[[], Any] | None = None
    gmail_service: Callable[[Settings], Any] | None = None
    environ: Mapping[str, str] | None = None
    home: Path | None = None


# --- which check a row gets ------------------------------------------------------------------


def _scorer_kind(spec: str, settings: Settings) -> str:
    provider = rd._provider(spec)
    if provider == "anthropic":
        return ANTHROPIC
    if provider in ("openrouter", "jev", "decisions"):
        if not scorers.is_openrouter_host(settings.scoring.openrouter.base_url):
            return NO_FREE
        return OPENROUTER
    if provider == "local":
        return LOCAL
    return NO_FREE


def kind_for(row_id: str, settings: Settings, mode: str) -> str | None:
    """The live check behind a /setup row, or None when the row has none."""
    if row_id == "scorer.screen":
        return _scorer_kind(settings.scoring.screen_scorer, settings)
    if row_id == "scorer.deep":
        return _scorer_kind(settings.scoring.deep_scorer, settings)
    if row_id in ("mail.token", "mail.label"):
        return GMAIL
    if row_id == "apply.runner":
        if mode == rd.CONTAINER:
            return None  # drafting runs on the host
        return ANTHROPIC if settings.apply.runner == "api" else RUNNER
    return None


def gmail_token_visible(environ: Mapping[str, str] | None = None) -> bool:
    """In a container the token is a mounted secret; the console's container has none."""
    import os

    from jobhunter.mail import auth

    env = os.environ if environ is None else environ
    if not env.get(auth.TOKEN_FILE_VAR):
        return False
    try:
        return Path(env[auth.TOKEN_FILE_VAR]).is_file()
    except OSError:
        return False


def instead_for(row_id: str, kind: str, mode: str, environ: Mapping[str, str] | None = None) -> str:
    """The command to run instead of this check here, or "" when it can run here."""
    if mode == rd.HOST_MANAGING and kind in _SECRET_BACKED and row_id != "apply.runner":
        return CTR_LIVE  # the containers' podman secrets decide, not this shell
    if mode == rd.CONTAINER and kind == GMAIL and not gmail_token_visible(environ):
        return "jobhunter-ctr mail status"
    return ""


def doctor_live_command(mode: str) -> str:
    return CTR_LIVE if mode in (rd.CONTAINER, rd.HOST_MANAGING) else "jobhunter doctor --live"


def origin(url: str) -> str:
    """``scheme://host[:port]`` of a URL: no userinfo, path or query (they can hold a key)."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return "the configured server"
    if not parts.scheme or not host:
        return "the configured server"
    if ":" in host:
        host = f"[{host}]"
    return f"{parts.scheme}://{host}" + (f":{port}" if port else "")


def anthropic_base_url(environ: Mapping[str, str] | None = None) -> str:
    """Where the SDK client sends requests: ``ANTHROPIC_BASE_URL``, else the public API."""
    import os

    env = os.environ if environ is None else environ
    return env.get("ANTHROPIC_BASE_URL", "").strip() or "https://api.anthropic.com"


def gate(kind: str, row_id: str, settings: Settings, mode: str, deps: Deps | None = None) -> str:
    """Why a check would contact nothing (fixed text), or "" when it will. Local only: the
    checks and ``doctor --live``'s contact list both ask this, so the list never names a
    service a gate then skips."""
    import os

    deps = deps or Deps()
    env = os.environ if deps.environ is None else deps.environ
    if kind == ANTHROPIC:
        if row_id == "apply.runner":
            spec = "anthropic:" + settings.apply.api_model
            cred = scorers.credential_for(spec, settings.scoring, environ=env, home=deps.home)
        else:
            scoring = settings.scoring
            spec = scoring.deep_scorer if row_id == "scorer.deep" else scoring.screen_scorer
            cred = rd.scorer_credential(
                spec, settings.scoring, mode=mode, environ=env, home=deps.home
            )
        if cred.status == scorers.MISSING:
            return "no Anthropic credential is set; nothing was sent"
        return ""
    if kind == OPENROUTER:
        config = settings.scoring.openrouter
        if not scorers.is_openrouter_host(config.base_url):
            return NO_FREE_TEXT
        if not (config.api_key_env and env.get(config.api_key_env, "").strip()):
            return "no key set; nothing was sent"
        return ""
    if kind == LOCAL:
        if not scorers.is_loopback(scorers.local_base_url(settings.scoring.local)):
            return "the local base_url is not a loopback address; not checked"
        return ""
    if kind == GMAIL:
        from jobhunter.mail import auth

        try:
            found = auth.client_secrets_path(settings).is_file()
        except OSError:
            found = False
        return "" if found else "the Google client JSON is missing; nothing was sent"
    if kind == RUNNER:
        from jobhunter.apply import cli_runner

        return "" if cli_runner.find_binary() else "`claude` is not on PATH"
    return NO_FREE_TEXT


def contact_line(
    kind: str, row_id: str, settings: Settings, mode: str, deps: Deps | None = None
) -> str:
    """What a check will contact, for ``doctor --live``'s preamble: "" when its gate skips it.
    Origins only (``scheme://host:port``), never a path, userinfo or other value."""
    if gate(kind, row_id, settings, mode, deps):
        return ""
    env = (deps or Deps()).environ
    if kind == ANTHROPIC:
        return f"the Anthropic API ({origin(anthropic_base_url(env))}): list models"
    if kind == OPENROUTER:
        return f"{origin(settings.scoring.openrouter.base_url)}: GET /key"
    if kind == LOCAL:
        return f"{origin(scorers.local_base_url(settings.scoring.local))}: /v1/models and /health"
    if kind == GMAIL:
        return "Google (oauth2.googleapis.com, gmail.googleapis.com): token, labels, filters"
    if kind == RUNNER:
        return "`claude auth status` (may contact claude.ai)"
    return ""


# --- the checks ----------------------------------------------------------------------------------


def _anthropic(row_id: str, settings: Settings, mode: str, deps: Deps, now: datetime) -> LiveResult:
    import anthropic

    if skip := gate(ANTHROPIC, row_id, settings, mode, deps):
        return _not_run(ANTHROPIC, skip, now)
    try:
        client = (deps.anthropic_client or _anthropic_client)()
        client.models.list(limit=1)
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
        return _result(ANTHROPIC, [Fact("accepted", False)], ["the credential was refused"], now)
    except anthropic.APIConnectionError:
        return _result(ANTHROPIC, [Fact("accepted", None)], ["could not reach the Anthropic API"],
                       now)  # fmt: skip
    except anthropic.APIStatusError as exc:
        note = f"the Anthropic API answered HTTP {int(exc.status_code)}"
        return _result(ANTHROPIC, [Fact("accepted", None)], [note], now)
    except Exception as exc:  # never the message: it can carry a credential detail
        return _result(ANTHROPIC, [Fact("accepted", None)], [f"error ({type(exc).__name__})"], now)
    return _result(ANTHROPIC, [Fact("accepted", True)], [], now)


def _openrouter(settings: Settings, deps: Deps, now: datetime) -> LiveResult:
    config = settings.scoring.openrouter
    if skip := gate(OPENROUTER, "", settings, rd.HOST, deps):
        return _not_run(OPENROUTER, skip, now)
    try:
        status = scorers.openrouter_key_status(
            config, client=(deps.http_client or _http_client)(), environ=deps.environ
        )
    except Exception as exc:  # never the message
        return _result(OPENROUTER, [Fact("accepted", None)], [f"error ({type(exc).__name__})"], now)
    if status.accepted is None and status.problem in (NO_FREE_TEXT, "no key set; nothing was sent"):
        return _not_run(OPENROUTER, status.problem, now)
    notes = [status.problem] if status.problem else []
    if status.accepted:
        if status.limit_remaining is None:
            notes.append("credit remaining: no limit set on this key")
        else:
            notes.append(f"credit remaining on this key: ${status.limit_remaining:,.2f}")
    return _result(OPENROUTER, [Fact("accepted", status.accepted)], notes, now)


def _local(settings: Settings, deps: Deps, now: datetime) -> LiveResult:
    config = settings.scoring.local
    if skip := gate(LOCAL, "", settings, rd.HOST, deps):
        return _not_run(LOCAL, skip, now)
    try:
        status = scorers.local_server_status(config, client=(deps.http_client or _http_client)())
    except Exception as exc:  # never the message
        return _result(LOCAL, [Fact("reachable", None)], [f"error ({type(exc).__name__})"], now)
    notes: list[str] = []
    if status.unauthorized:
        notes.append("the server wants an API key: set scoring.local.api_key_env")
        loaded: bool | None = None
    else:
        # /v1/models listing a model, or llama.cpp's /health answering 200 (503 while loading)
        loaded = bool(status.models) or status.detail.endswith("/health ok")
    facts = [Fact("reachable", status.reachable)]
    facts.append(Fact("model loaded", loaded if status.reachable else False))
    return _result(LOCAL, facts, notes, now)


def _gmail(settings: Settings, deps: Deps, now: datetime) -> LiveResult:
    from jobhunter.mail import auth, setup

    labels = (
        "token accepted",
        "label present",
        "filter for your alerts address present",
    )
    if skip := gate(GMAIL, "", settings, rd.HOST, deps):
        return _not_run(GMAIL, skip, now)
    try:
        service = (deps.gmail_service or _gmail_service)(settings)
    except auth.MailAuthError as exc:
        missing_token = str(exc).startswith("No Gmail token")
        note = "no Gmail token found" if missing_token else "the Google client JSON is missing"
        return _not_run(GMAIL, note + "; nothing was sent", now)
    except (OSError, ValueError):
        return _not_run(GMAIL, "the Google client JSON could not be read; nothing was sent", now)
    except Exception as exc:  # never the message
        return _not_run(GMAIL, f"could not build the Gmail client ({type(exc).__name__})", now)

    def failed(note: str, token: bool | None = None) -> LiveResult:
        facts = [Fact(labels[0], token), Fact(labels[1], None), Fact(labels[2], None)]
        return _result(GMAIL, facts, [note], now)

    try:
        with auth.translate_refresh_error():
            label_id = setup._find_label(service, settings.mail.label)
            present = (
                service.users().settings().filters().list(userId="me").execute().get("filter") or []
            )
    except auth.GmailTokenExpired:
        return failed(auth.TOKEN_EXPIRED_MESSAGE, token=False)
    except Exception as exc:  # HttpError, transport errors: status or type only
        status = getattr(getattr(exc, "resp", None), "status", None)
        if isinstance(status, int):
            return failed(f"Gmail answered HTTP {status}")
        return failed(f"could not reach Gmail ({type(exc).__name__})")
    if not isinstance(present, list):
        present = []
    notes: list[str] = []
    try:
        address = setup.validate_address(settings)
    except setup.MailSetupError:
        address = ""
        notes.append("mail.alerts_address is still the placeholder")
    to_filter: bool | None = (
        any(isinstance(f, dict) and setup._matches(f, {"to": address}, label_id) for f in present)
        if address
        else None
    )
    fallback = [{"from": setup._domain(d)} for d in settings.mail.fallback_sender_domains
                if d.strip()]  # fmt: skip
    try:
        fallback += [{"from": a} for a in setup.clean_senders(settings)]
    except setup.MailSetupError:
        notes.append("mail.fallback_senders has an entry that is not a full sender address")
    if fallback:
        have = sum(
            1
            for c in fallback
            if any(isinstance(f, dict) and setup._matches(f, c, label_id) for f in present)
        )
        notes.append(f"{have} of {len(fallback)} fallback sender filters present")
    facts = [
        Fact(labels[0], True),
        Fact(labels[1], label_id is not None),
        Fact(labels[2], to_filter),
    ]
    return _result(GMAIL, facts, notes, now)


def _runner(settings: Settings, deps: Deps, now: datetime) -> LiveResult:
    from jobhunter.apply import cli_runner

    if skip := gate(RUNNER, "", settings, rd.HOST, deps):
        return _not_run(RUNNER, skip, now)
    binary = cli_runner.find_binary()
    if binary is None:
        return _not_run(RUNNER, "`claude` is not on PATH", now)
    try:
        cwd = cli_runner.work_dir(resolve_path(settings.paths.cache_dir))
        status = cli_runner.auth_status(binary, cwd, deps.environ)
    except cli_runner.CliFailure as exc:
        known = {"the claude command is not installed", "claude auth status timed out"}
        note = str(exc) if str(exc) in known else "claude auth status failed"
        return _result(RUNNER, [Fact("logged in", None)], [note], now)
    except Exception as exc:  # never the message
        return _result(RUNNER, [Fact("logged in", None)], [f"error ({type(exc).__name__})"], now)
    facts = [
        Fact("logged in", status.logged_in),
        Fact("claude.ai login (not an API key)", status.subscription),
    ]
    return _result(RUNNER, facts, [], now)


def run_kind(
    kind: str,
    row_id: str,
    settings: Settings,
    mode: str,
    *,
    now: datetime,
    deps: Deps | None = None,
) -> LiveResult:
    deps = deps or Deps()
    if kind == ANTHROPIC:
        return _anthropic(row_id, settings, mode, deps, now)
    if kind == OPENROUTER:
        return _openrouter(settings, deps, now)
    if kind == LOCAL:
        return _local(settings, deps, now)
    if kind == GMAIL:
        return _gmail(settings, deps, now)
    if kind == RUNNER:
        return _runner(settings, deps, now)
    return _not_run(NO_FREE, NO_FREE_TEXT, now)


def run_row(
    row_id: str, settings: Settings, mode: str, *, now: datetime, deps: Deps | None = None
) -> LiveResult | None:
    """The live check for one /setup row (one click). None when the row has none."""
    kind = kind_for(row_id, settings, mode)
    if kind is None:
        return None
    instead = instead_for(row_id, kind, mode, (deps or Deps()).environ)
    if instead:
        why = (
            "the containers use podman secrets"
            if instead == CTR_LIVE
            else "the console container never sees the Gmail token"
        )
        result = _not_run(kind, f"not run here: {why}", now, instead)
    else:
        result = run_kind(kind, row_id, settings, mode, now=now, deps=deps)
    return _with_rows(result, (row_id,))


def _with_rows(result: LiveResult, rows: tuple[str, ...]) -> LiveResult:
    return replace(result, rows=rows)


@dataclass(frozen=True)
class Planned:
    kind: str
    rows: tuple[str, ...]
    instead: str = ""


def plan(settings: Settings, mode: str, environ: Mapping[str, str] | None = None) -> list[Planned]:
    """``doctor --live``'s checks: one per kind (rows sharing a kind share it), and one line
    per command that runs the rest elsewhere."""
    runnable: dict[str, list[str]] = {}
    elsewhere: dict[tuple[str, str], list[str]] = {}
    for row_id in LIVE_ROWS:
        kind = kind_for(row_id, settings, mode)
        if kind is None:
            continue
        instead = instead_for(row_id, kind, mode, environ)
        if instead:
            elsewhere.setdefault((kind, instead), []).append(row_id)
        else:
            runnable.setdefault(kind, []).append(row_id)
    out = [Planned(k, tuple(rows)) for k, rows in runnable.items()]
    out += [Planned(k, tuple(rows), instead) for (k, instead), rows in elsewhere.items()]
    return out


def run_plan(
    planned: list[Planned],
    settings: Settings,
    mode: str,
    *,
    now: Callable[[], datetime],
    deps: Deps | None = None,
) -> list[LiveResult]:
    out: list[LiveResult] = []
    for p in planned:
        if p.instead:
            why = (
                "the containers use podman secrets"
                if p.instead == CTR_LIVE
                else "this container never sees the Gmail token"
            )
            skipped = _not_run(p.kind, f"not run here: {why}", now(), p.instead)
            out.append(_with_rows(skipped, p.rows))
            continue
        result = run_kind(p.kind, p.rows[0], settings, mode, now=now(), deps=deps)
        out.append(_with_rows(result, p.rows))
    return out


def fact_text(fact: Fact) -> str:
    value = {True: "yes", False: "no", None: "unknown"}[fact.value]
    return f"{fact.label}: {value}"
