"""Installed-app OAuth for Gmail; the refresh token lives in the OS keyring (or a 0600 file).

Scopes are the minimum in specs/012: read one label, create the label, create filters.
There is deliberately no send scope and no modify scope.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import webbrowser
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import keyring
from keyring.backends import null

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

OPEN_HINT = "If no browser opened, open this URL in any browser on this machine."
DEFAULT_PORT = 8765
OPENER_TIMEOUT_SECONDS = 8
FLOW_TIMEOUT_SECONDS = 600
PORTAL_ARGS = [
    "gdbus",
    "call",
    "--session",
    "--dest",
    "org.freedesktop.portal.Desktop",
    "--object-path",
    "/org/freedesktop/portal/desktop",
    "--method",
    "org.freedesktop.portal.OpenURI.OpenURI",
    "",
]
TEXT_BROWSERS = {"www-browser", "links", "elinks", "lynx", "w3m", "browsh", "links2"}
TOKEN_FILE_NAME = "gmail_token.json"
FILE_WARNING = (
    "warning: no OS keyring is available in this environment; storing the Gmail token in "
    "{path} (mode 0600) instead."
)
MANUAL_PROMPT = (
    "After approving, your browser will try to load a 127.0.0.1 page that may fail - "
    "copy the full URL from the address bar and paste it here: "
)


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


# --- opening a browser ------------------------------------------------------------------


def _python_browser(url: str) -> bool:
    """Python's webbrowser, but only when a real graphical browser is registered."""
    try:
        controller = webbrowser.get()
    except webbrowser.Error:
        return False
    name = os.path.basename(str(getattr(controller, "name", "") or "")).lower()
    if name in TEXT_BROWSERS:
        return False
    return bool(controller.open(url, new=1))


def _run_opener(argv: list[str]) -> bool:
    if shutil.which(argv[0]) is None:
        return False
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            timeout=OPENER_TIMEOUT_SECONDS,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def opener_chain(url: str) -> list[tuple[str, Callable[[], bool]]]:
    """Ordered ways to open `url`, each returning True on success."""
    return [
        ("python webbrowser", lambda: _python_browser(url)),
        ("xdg-open", lambda: _run_opener(["xdg-open", url])),
        ("flatpak-spawn", lambda: _run_opener(["flatpak-spawn", "--host", "xdg-open", url])),
        ("distrobox-host-exec", lambda: _run_opener(["distrobox-host-exec", "xdg-open", url])),
        ("desktop portal", lambda: _run_opener([*PORTAL_ARGS, url, "{}"])),
    ]


def open_url(url: str) -> str | None:
    """Try each opener in order; return the name of the first that worked, else None."""
    for name, attempt in opener_chain(url):
        try:
            if attempt():
                return name
        except Exception:  # an opener must never break the auth flow
            continue
    return None


class _ChainBrowser:
    """webbrowser-style controller so the library's own `open_browser` path uses our chain."""

    def open(self, url: str, new: int = 0, autoraise: bool = True) -> bool:
        return open_url(url) is not None


def _register_chain_browser() -> str:
    name = "jobhunter-chain"
    webbrowser.register(name, None, _ChainBrowser())
    return name


# --- OAuth flows ------------------------------------------------------------------------


def _new_flow(settings: Settings):
    from google_auth_oauthlib.flow import InstalledAppFlow

    path = require_client_secrets(settings)
    return InstalledAppFlow.from_client_secrets_file(str(path), SCOPES)


def _refresh_token_of(creds) -> str:
    if not creds.refresh_token:
        raise MailAuthError(
            "Google did not return a refresh token. Revoke the app at "
            "https://myaccount.google.com/permissions and run `jobhunter mail auth` again."
        )
    return creds.refresh_token


def run_oauth_flow(settings: Settings, *, port: int = 0, open_browser: bool = True) -> str:
    """Run the consent flow with a local server on 127.0.0.1; return the refresh token.

    The URL is always printed. With `open_browser`, the opener chain is tried as well.
    """
    flow = _new_flow(settings)
    kwargs: dict = {}
    if open_browser:
        kwargs["browser"] = _register_chain_browser()
    message = f"Authorization URL:\n\n{{url}}\n\n{OPEN_HINT}\n"
    creds = flow.run_local_server(
        host="127.0.0.1",
        port=port,
        open_browser=open_browser,
        authorization_prompt_message=message,
        timeout_seconds=FLOW_TIMEOUT_SECONDS,
        **kwargs,
    )
    return _refresh_token_of(creds)


def parse_pasted_redirect(pasted: str, expected_state: str) -> str:
    """Validate a pasted redirect URL and return it cleaned; raise MailAuthError if wrong."""
    text = pasted.strip().strip("<>\"'")
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.query:
        raise MailAuthError(
            "That does not look like the redirected URL (expected http://127.0.0.1:<port>/"
            "?state=...&code=...). Copy the whole address-bar URL after approving."
        )
    query = parse_qs(parsed.query)
    if "error" in query:
        raise MailAuthError(f"Google reported an error: {query['error'][0]}")
    if query.get("state", [None])[0] != expected_state:
        raise MailAuthError(
            "The pasted URL belongs to a different or expired authorization attempt "
            "(state mismatch). Run `jobhunter mail auth --manual` again and use the new URL."
        )
    if not query.get("code", [""])[0]:
        raise MailAuthError("The pasted URL has no authorization code. Paste the full URL.")
    return text


def run_manual_flow(
    settings: Settings,
    *,
    port: int = DEFAULT_PORT,
    echo: Callable[[str], None] = print,
    ask: Callable[[str], str] = input,
) -> str:
    """No local server: print the URL, then take the pasted redirect URL; return the token."""
    flow = _new_flow(settings)
    flow.redirect_uri = f"http://127.0.0.1:{port}/"
    url, state = flow.authorization_url()
    echo(f"Authorization URL:\n\n{url}\n\n{OPEN_HINT}\n")
    try:
        pasted = ask(MANUAL_PROMPT)
    except (EOFError, KeyboardInterrupt) as exc:
        raise MailAuthError("No redirect URL was pasted; authorization cancelled.") from exc
    response = parse_pasted_redirect(pasted, state)
    # oauthlib refuses http redirect URLs unless told otherwise. The redirect here is the
    # loopback address (127.0.0.1), which Google's installed-app guidance allows over http;
    # the code is exchanged with Google over https. Scoped to this call only.
    previous = os.environ.get("OAUTHLIB_INSECURE_TRANSPORT")
    os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = "1"
    try:
        flow.fetch_token(authorization_response=response)
    except Exception as exc:
        raise MailAuthError(
            f"Google rejected the pasted code ({type(exc).__name__}); it may have expired or "
            "been used already. Run `jobhunter mail auth --manual` again."
        ) from exc
    finally:
        if previous is None:
            os.environ.pop("OAUTHLIB_INSECURE_TRANSPORT", None)
        else:
            os.environ["OAUTHLIB_INSECURE_TRANSPORT"] = previous
    return _refresh_token_of(flow.credentials)


# --- token storage ----------------------------------------------------------------------


def token_file_path() -> Path:
    return Path.home() / ".config" / "jobhunter" / TOKEN_FILE_NAME


def _keyring_usable() -> bool:
    try:
        backend = keyring.get_keyring()
    except Exception:
        return False
    return not isinstance(backend, null.Keyring)


def _write_token_file(refresh_token: str) -> Path:
    path = token_file_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"refresh_token": refresh_token}))
    os.chmod(path, 0o600)
    return path


def _read_token_file() -> str | None:
    try:
        data = json.loads(token_file_path().read_text(encoding="utf-8"))
        token = data.get("refresh_token")
    except (OSError, ValueError, AttributeError):
        return None
    return token or None


def store_token(refresh_token: str, echo: Callable[[str], None] = print) -> str:
    """Store in the OS keyring, else a 0600 file. Returns "keyring" or "file"."""
    if _keyring_usable():
        try:
            keyring.set_password(KEYRING_SERVICE, KEYRING_KEY, refresh_token)
            return "keyring"
        except Exception:  # the fail backend raises NoKeyringError; D-Bus errors vary
            pass
    path = _write_token_file(refresh_token)
    echo(FILE_WARNING.format(path=path))
    return "file"


def _keyring_token() -> str | None:
    if not _keyring_usable():
        return None
    try:
        return keyring.get_password(KEYRING_SERVICE, KEYRING_KEY)
    except Exception:
        return None


def load_token() -> str | None:
    return _keyring_token() or _read_token_file()


def token_location() -> str | None:
    """Human-readable place the token lives (never the token itself), or None."""
    if _keyring_token():
        return f"OS keyring (service {KEYRING_SERVICE}, key {KEYRING_KEY})"
    if _read_token_file():
        return f"file {token_file_path()}"
    return None


def authenticate(
    settings: Settings,
    *,
    manual: bool = False,
    port: int | None = None,
    open_browser: bool = True,
) -> str:
    """Run the chosen flow and store the token; returns where it was stored."""
    if manual:
        token = run_manual_flow(settings, port=port or DEFAULT_PORT)
    else:
        token = run_oauth_flow(settings, port=port or 0, open_browser=open_browser)
    return store_token(token)


def build_service(settings: Settings):
    """A Gmail API service built from the stored refresh token and the client secrets."""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    token = load_token()
    if not token:
        raise MailAuthError("No Gmail token found. Run: jobhunter mail auth")
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
