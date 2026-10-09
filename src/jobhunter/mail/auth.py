"""Installed-app OAuth for Gmail; the refresh token lives in the OS keyring only.

Scopes are the minimum in specs/012: read one label, create the label, create filters.
There is deliberately no send scope and no modify scope.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import keyring
from keyring.errors import KeyringError

from jobhunter.config import Settings, resolve_path

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.settings.basic",
]
KEYRING_SERVICE = "jobhunter"
KEYRING_KEY = "gmail"
SECRETS_ENV_VAR = "JOBHUNTER_GOOGLE_CLIENT_SECRETS"
TOKEN_URI = "https://oauth2.googleapis.com/token"

CLOUD_STEPS = """\
To create a Google OAuth client (one time, free, no billing needed):
  1. Open https://console.cloud.google.com/ and create a project (e.g. "jobhunter").
  2. APIs & Services -> Library -> search "Gmail API" -> Enable.
  3. APIs & Services -> OAuth consent screen: user type External, fill the required fields,
     and add your own Google account under "Test users" (leave the app in Testing).
  4. APIs & Services -> Credentials -> Create credentials -> OAuth client ID ->
     Application type "Desktop app". Download the JSON.
  5. Save it as {path}
     (or point {env} at it, or set mail.client_secrets_path in config.toml).
Then run: jobhunter mail auth"""


class MailAuthError(RuntimeError):
    """A user-actionable Gmail auth problem."""


def client_secrets_path(settings: Settings) -> Path:
    env = os.environ.get(SECRETS_ENV_VAR)
    return resolve_path(env if env else settings.mail.client_secrets_path)


def missing_secrets_message(path: Path) -> str:
    return f"Google client secrets file not found: {path}\n\n" + CLOUD_STEPS.format(
        path=path, env=SECRETS_ENV_VAR
    )


def require_client_secrets(settings: Settings) -> Path:
    path = client_secrets_path(settings)
    if not path.is_file():
        raise MailAuthError(missing_secrets_message(path))
    return path


def run_oauth_flow(settings: Settings) -> str:
    """Run the browser consent flow on 127.0.0.1 (random port); return the refresh token."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    path = require_client_secrets(settings)
    flow = InstalledAppFlow.from_client_secrets_file(str(path), SCOPES)
    creds = flow.run_local_server(host="127.0.0.1", port=0, open_browser=True)
    if not creds.refresh_token:
        raise MailAuthError(
            "Google did not return a refresh token. Revoke the app at "
            "https://myaccount.google.com/permissions and run `jobhunter mail auth` again."
        )
    return creds.refresh_token


def store_token(refresh_token: str) -> None:
    try:
        keyring.set_password(KEYRING_SERVICE, KEYRING_KEY, refresh_token)
    except KeyringError as exc:
        raise MailAuthError(f"could not store the token in the OS keyring: {exc}") from exc


def load_token() -> str | None:
    try:
        return keyring.get_password(KEYRING_SERVICE, KEYRING_KEY)
    except KeyringError:
        return None


def authenticate(settings: Settings) -> None:
    store_token(run_oauth_flow(settings))


def build_service(settings: Settings):
    """A Gmail API service built from the keyring refresh token and the client secrets."""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    token = load_token()
    if not token:
        raise MailAuthError("No Gmail token in the keyring. Run: jobhunter mail auth")
    path = require_client_secrets(settings)
    info = json.loads(path.read_text(encoding="utf-8"))
    client = info.get("installed") or info.get("web") or {}
    creds = Credentials(
        token=None,
        refresh_token=token,
        token_uri=client.get("token_uri", TOKEN_URI),
        client_id=client.get("client_id"),
        client_secret=client.get("client_secret"),
        scopes=SCOPES,
    )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)
