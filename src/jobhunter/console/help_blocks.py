"""Generated blocks of the help pages (specs/019, Addendum A, S11c).

Kept outside ``jobhunter.console.help`` on purpose: that package renders before anything is
set up and must not import settings or the database. These functions read only constants of
the code (no settings values, no environment, no secrets), so what they return is the same on
every machine. Importing this module registers them in ``help.BLOCKS``; regenerate the pages
with ``uv run python -m jobhunter.console.help``.
"""

from __future__ import annotations

from collections.abc import Callable

from jobhunter import config, secrets
from jobhunter.apply import cli_runner
from jobhunter.config import SCORER_PROVIDERS, OpenAICompat, OpenRouter
from jobhunter.console import help as helpdocs
from jobhunter.mail import auth
from jobhunter.ops import ctr
from jobhunter.scoring.scorers import BATCH_DISCOUNT, PRICING_PER_MTOK
from jobhunter.xdg import CACHE_HOME_VAR, CONFIG_HOME_VAR, DATA_HOME_VAR


def cloud_steps() -> str:
    """``mail/auth.py`` ``CLOUD_STEPS`` as a code block, with the path and variable filled in
    by name rather than by value."""
    text = auth.CLOUD_STEPS.format(
        path="~/.config/jobhunter/google_client_secret.json",
        env=auth.SECRETS_ENV_VAR,
    )
    return f"```text\n{text}\n```"


# What reads each secret, for the table. Keyed by the name in ``secrets.DEFAULT_NAMES``.
SECRET_USE = {
    "ANTHROPIC_API_KEY": "the Anthropic scorers and the `api` drafting runner",
    "ANTHROPIC_AUTH_TOKEN": "the Anthropic SDK's bearer-token alternative to the key",
    "OPENROUTER_API_KEY": "`openrouter:` and `jev:` scorers",
    "OPENAI_COMPAT_API_KEY": "`openai_compat:` scorers",
    "LLAMA_API_KEY": "`local:` scorers (usually unset)",
    "USAJOBS_API_KEY": "the USAJOBS source",
    "USAJOBS_EMAIL": "the USAJOBS source (the email you registered the key with)",
}

# Variables that are not secrets: (name, read by, on the host, in containers, needs restart).
# Anything from the environment is read once at start, so it needs a console restart.
UNITS = "set by the units, do not set"
OTHER_ROWS = (
    (
        config.ENV_FILE_VAR,
        "which `.env` is read first",
        "your choice",
        "not used (secrets are podman secrets)",
        "yes",
    ),
    (
        "JOBHUNTER_CONFIG",
        "which `config.toml` is read",
        "your choice",
        "not used (the config is `/config/config.toml`)",
        "yes",
    ),
    *(
        (var, f"overrides `[paths] {field}`", "optional", UNITS, "yes")
        for field, var in config.PATH_ENV_VARS.items()
    ),
    (
        auth.SECRETS_ENV_VAR,
        "the Google OAuth client JSON (overrides `[mail] client_secrets_path`)",
        "optional",
        "podman secret `jobhunter_google_client`",
        "yes",
    ),
    (
        "JOBHUNTER_GMAIL_TOKEN_FILE",
        "the Gmail token, read from a file",
        "not used (OS keyring, else a 0600 file)",
        "podman secret `jobhunter_gmail_token`",
        "yes",
    ),
    (CONFIG_HOME_VAR, "base of `~/.config/jobhunter`", "optional", "ignored", "yes"),
    (DATA_HOME_VAR, "base of `~/.local/share/jobhunter`", "optional", "ignored", "yes"),
    (CACHE_HOME_VAR, "base of `~/.cache/jobhunter`", "optional", "ignored", "yes"),
    (
        "XDG_RUNTIME_DIR",
        "podman's socket directory, read by `container preflight`",
        "set by your login session",
        "not used",
        "no",
    ),
    (
        "CLAUDE_CONFIG_DIR",
        "where the Claude Code login lives; passed to the drafting runner",
        "optional",
        "not used (drafting runs on the host)",
        "yes",
    ),
    (
        "CLAUDE_CODE_OAUTH_TOKEN",
        "a subscription token from `claude setup-token`; passed to the drafting runner. "
        "A secret with no `_FILE` form",
        "`~/.env`",
        "not used",
        "yes",
    ),
    *(
        (name, "set by jobhunter on the Claude Code child: do not set", "-", "-", "-")
        for name in cli_runner.FORCED_ENV
    ),
    *(
        (name, "never passed to the Claude Code child", "-", "-", "-")
        for name in cli_runner.NEVER_PASSED
        if name not in secrets.DEFAULT_NAMES and name != "CLAUDECODE"
    ),
    ("JOBHUNTER_CONTAINER", "container mode switch", "-", "set by the image, do not set", "-"),
    (
        "JOBHUNTER_TRIGGER",
        "marks timer-started runs (`timer`); anything else is a manual run",
        "set by the run and collect units, do not set",
        "set by the run, collect and rescore units (and preflight's trigger drop-in), do not set",
        "no",
    ),
    (
        "JOBHUNTER_PUBLISHED_LOOPBACK_ONLY",
        "the console's published-port check",
        "-",
        "set by you only for the 0.0.0.0 fallback (see Containers, Troubleshooting)",
        "-",
    ),
    (
        "JOBHUNTER_BUILD_COMMIT",
        "the commit `jobhunter version` shows",
        "-",
        "set by the image, do not set",
        "-",
    ),
)

# String literals in src/ that look like environment variables but are not. Each says why.
NOT_ENV = {
    "ANTHROPIC_HAIKU": "a constant holding a scorer spec",
    "ANTHROPIC_MODELS": "a constant holding a model list",
    "ANTHROPIC_KEY_PREFIX": "a constant holding the key prefix",
    "OPENROUTER_KEY_PREFIX": "a constant holding the key prefix",
    "USAJOBS_MAX_DAYS": "a request-parameter limit",
    "XDG_ENV": "a placeholder name inside the host unit templates",
}

# The Anthropic SDK's own credential variables (``scoring/scorers.py`` only checks that they
# are set, as the SDK would): one row for the group rather than a row each.
SDK_VARS = (
    "ANTHROPIC_PROFILE",
    "ANTHROPIC_CONFIG_DIR",
    "ANTHROPIC_FEDERATION_RULE_ID",
    "ANTHROPIC_ORGANIZATION_ID",
    "ANTHROPIC_IDENTITY_TOKEN",
    "ANTHROPIC_IDENTITY_TOKEN_FILE",
)


def env_var_table() -> str:
    mounts = {m.env: m for m in ctr.ctr_secret_mounts()}
    lines = [
        "| Name | Read by | On the host | In containers | Needs restart |",
        "|---|---|---|---|---|",
    ]
    for name in secrets.DEFAULT_NAMES:
        mount = mounts.get(f"{name}_FILE")
        place = f"podman secret `{mount.secret}` as `{name}_FILE`" if mount else "not mounted"
        lines.append(
            f"| `{name}` (secret; or `{name}_FILE`) | {SECRET_USE.get(name, '')} "
            f"| `~/.env` or the environment | {place} | yes |"
        )
    sdk = ", ".join(f"`{n}`" for n in SDK_VARS)
    lines.append(
        f"| {sdk} | the Anthropic SDK's other sign-in routes; jobhunter only checks that they "
        "are set | the environment | not mounted | yes |"
    )
    for name, use, host, in_ctr, restart in OTHER_ROWS:
        lines.append(f"| `{name}` | {use} | {host} | {in_ctr} | {restart} |")
    return "\n".join(lines)


# Scorer provider table (scorers.md). A provider added to SCORER_PROVIDERS without a row
# here fails the generator, which fails the drift test.
_ENDPOINT = {
    "anthropic": "Anthropic batch API",
    "openrouter": "OpenRouter chat completions (`[scoring.openrouter]`)",
    "jev": "OpenRouter decisions endpoint (`[scoring.openrouter]`)",
    "decisions": "OpenRouter decisions endpoint (`[scoring.openrouter]`)",
    "openai-compat": "any OpenAI-compatible endpoint (`[scoring.openai_compat]`)",
    "local": "a model on this machine (`[scoring.local]`)",
}
_OPENROUTER_KEY = f"`{OpenRouter().api_key_env}`"
_KEY = {
    "anthropic": "`ANTHROPIC_API_KEY`",
    "openrouter": _OPENROUTER_KEY,
    "jev": _OPENROUTER_KEY,
    "decisions": _OPENROUTER_KEY,
    "openai-compat": f"`{OpenAICompat().api_key_env}` (the name is `api_key_env`)",
    "local": "optional",
}


def scorer_providers() -> str:
    rows = ["| Spec | Endpoint | Key |", "|---|---|---|"]
    for provider in SCORER_PROVIDERS:
        rows.append(f"| `{provider}:<model>` | {_ENDPOINT[provider]} | {_KEY[provider]} |")
    return "\n".join(rows)


def _usd(value: float) -> str:
    return f"${value:.2f}"


def model_prices() -> str:
    rows = [
        "| Anthropic model | Input per 1M tokens | Output per 1M tokens | In a batch (in / out) |",
        "|---|---|---|---|",
    ]
    for model, (in_rate, out_rate) in sorted(PRICING_PER_MTOK.items()):
        batch = f"{_usd(in_rate * BATCH_DISCOUNT)} / {_usd(out_rate * BATCH_DISCOUNT)}"
        rows.append(f"| `{model}` | {_usd(in_rate)} | {_usd(out_rate)} | {batch} |")
    return "\n".join(rows)


def config_reload() -> str:
    """Which settings reload in place (019 Addendum B, S12a), from ``config.RELOADABLE``."""
    from jobhunter import config_watch

    return config_watch.reload_table()


GENERATORS: dict[str, Callable[[], str]] = {
    "config-reload": config_reload,
    "cloud-steps": cloud_steps,
    "env-vars": env_var_table,
    "scorer-providers": scorer_providers,
    "model-prices": model_prices,
}
helpdocs.BLOCKS.update(GENERATORS)
