"""No-model-access banners on the pages that need a model (specs/020 "Banners elsewhere").

``model_banner(request, uses, scorer=None)`` is a template global. It renders
``model_access.problems`` (``missing`` only; ``likely`` and ``unknown`` never raise a banner) plus
uses that models.toml blocked. Local and free: environment variables, ``stat`` and the runner
state file, no network, no subprocess, no SDK import, no database. A broken check shows nothing.
No value is rendered: ``Access.detail`` is fixed wording plus variable names.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates

from jobhunter import model_access as ma
from jobhunter import readiness
from jobhunter.config import BLOCKED_SPEC

# provider -> (help link, label). Anchors that exist today; 020 M6 may add friendlier ones.
HELP_LINKS = {
    ma.ANTHROPIC: ("/help/scorers#get-an-anthropic-api-key", "Help: get an Anthropic API key"),
    ma.OPENROUTER: ("/help/openrouter#get-an-account-credits-and-a-key", "Help: OpenRouter"),
    ma.OPENAI_COMPAT: ("/help/scorers#other-choices", "Help: other choices"),
    ma.LOCAL: ("/help/scorers#other-choices", "Help: other choices"),
    ma.SUBSCRIPTION: (
        "/help/drafting#the-cli-runner-log-in-to-claude-code",
        "Help: Claude Code login",
    ),
}
BLOCKED_HELP = ("/help/troubleshooting#a-model-use-is-blocked-by-the-saved-choice", "Help: troubleshooting")
FIELD_OF_USE = {ma.SCREEN: "screen_scorer", ma.DEEP: "deep_scorer"}
COMMAND = "jobhunter models show"


def _list(labels: list[str]) -> str:
    labels = labels[:1] + [label.lower() for label in labels[1:]]
    return labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " and " + labels[-1]


def register(app: FastAPI, templates: Jinja2Templates, now: Callable[[], datetime]) -> None:
    def fix_of(request: Request) -> dict[str, str | None]:
        # /models is another ticket (020 M4); link it only when this app serves it.
        has_models = any(getattr(r, "path", None) == "/models" for r in request.app.routes)
        return {
            "href": "/models" if has_models else None,
            "text": "Models" if has_models else COMMAND,
        }

    def banners(request: Request, uses: list[str], scorer: str | None = None) -> list[dict]:
        try:
            settings = request.app.state.settings
            mode = getattr(request.app.state, "setup_mode", None) or readiness.detect_mode(
                settings=settings
            )
            environ = getattr(request.app.state, "setup_environ", None)
            home = getattr(request.app.state, "setup_home", None)
            layers = settings.models_layers
            blocked = layers.blocked if layers is not None else {}
            out: list[dict] = []
            live: list[str] = []
            blocked_uses: list[str] = []
            for use in uses:
                field = FIELD_OF_USE.get(use)
                override = scorer if use == ma.SCREEN else None
                spec = override or ma.choice_for(use, settings).model
                if field and (spec == BLOCKED_SPEC or (not override and field in blocked)):
                    blocked_uses.append(use)
                else:
                    live.append(use)
            fix = fix_of(request)
            choices = {ma.SCREEN: ma.Choice(scorer)} if scorer and ma.SCREEN in live else None
            items = ma.problems(
                live, settings, mode=mode, now=now(), environ=environ, home=home, choices=choices
            )
            for access in items:
                href, label = HELP_LINKS.get(access.provider, HELP_LINKS[ma.ANTHROPIC])
                out.append(
                    {
                        "key": f"{access.provider}|{access.detail}|{','.join(access.uses)}",
                        "text": ma.summary(access),
                        "help_href": href,
                        "help_label": label,
                        "fix": fix,
                    }
                )
            if blocked_uses:
                names = _list([ma.USE_LABELS[u] for u in blocked_uses])
                out.append(
                    {
                        "key": "blocked|" + ",".join(blocked_uses),
                        "text": f"models.toml holds an invalid value, so {names.lower()} is "
                        "blocked and cannot run.",
                        "help_href": BLOCKED_HELP[0],
                        "help_label": BLOCKED_HELP[1],
                        "fix": fix,
                    }
                )
            return out
        except Exception:  # a broken check must never take down the page it sits on
            return []

    templates.env.globals["model_banner"] = banners
