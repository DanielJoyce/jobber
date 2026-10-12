"""Fakes for the free live checks (specs/019 S6): every service they contact, recorded.

Nothing here reaches a network: the Anthropic SDK and the scorers' httpx client run on mock
transports, Gmail is an in-memory object, and ``claude`` is conftest's ``fake_claude``.
Each fake records what was asked, so tests can prove that no model endpoint was hit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import anthropic
import httpx
import httpx2

from jobhunter import live_checks as lc

LEAK = "SENTINEL-c0ffee5eed"  # put in every key, address, label and reply field
MODEL_PATHS = ("/messages", "/chat/completions", "/completions", "/alpha/decisions", "/batches")


@dataclass
class Recorder:
    """Requests seen by every fake, as ``host path`` strings."""

    seen: list[str] = field(default_factory=list)

    def model_calls(self) -> list[str]:
        return [s for s in self.seen if any(p in s for p in MODEL_PATHS)]


def anthropic_factory(rec: Recorder, status: int = 200):
    def handler(request: httpx2.Request) -> httpx2.Response:
        rec.seen.append(f"{request.url.host} {request.url.path}")
        if status != 200:
            body = {"type": "error", "error": {"type": "authentication_error", "message": LEAK}}
            return httpx2.Response(status, json=body)
        model = {
            "id": "claude-x",
            "type": "model",
            "display_name": LEAK,
            "created_at": "2025-01-01T00:00:00Z",
        }
        return httpx2.Response(
            200, json={"data": [model], "has_more": False, "first_id": "x", "last_id": "x"}
        )

    def make() -> Any:
        return anthropic.Anthropic(
            api_key=f"sk-ant-{LEAK}",
            http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
            max_retries=0,
        )

    return make


def http_factory(
    rec: Recorder,
    *,
    key_status: int = 200,
    remaining: float | None = 12.5,
    models: list[str] | None = None,
    health: int = 200,
):
    """OpenRouter ``/key`` and a local server's ``/v1/models`` and ``/health``."""
    models = ["local-model"] if models is None else models

    def handler(request: httpx.Request) -> httpx.Response:
        rec.seen.append(f"{request.url.host} {request.url.path}")
        path = request.url.path
        if path.endswith("/key"):
            if key_status != 200:
                return httpx.Response(key_status, json={"error": {"message": LEAK}})
            data = {
                "label": f"sk-or-v1-{LEAK}",
                "usage": 1.0,
                "limit": 20,
                "limit_remaining": remaining,
                "is_free_tier": False,
            }
            return httpx.Response(200, json={"data": data})
        if path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": m} for m in models]})
        if path == "/health":
            return httpx.Response(health, json={"status": "ok"})
        return httpx.Response(500, json={"error": LEAK})

    return lambda: httpx.Client(transport=httpx.MockTransport(handler))


class _Call:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class FakeGmail:
    """``service.users().labels().list(...)`` and ``.settings().filters().list(...)``."""

    def __init__(
        self,
        rec: Recorder,
        *,
        labels: list[dict] | None = None,
        filters: list[dict] | None = None,
        fail: Exception | None = None,
    ) -> None:
        self.rec = rec
        self.labels_ = labels or []
        self.filters_ = filters or []
        self.fail = fail

    def _run(self, what: str, payload: dict):
        def go():
            self.rec.seen.append(f"gmail.googleapis.com {what}")
            if self.fail is not None:
                raise self.fail
            return payload

        return _Call(go)

    def users(self):
        return self

    def labels(self):
        outer = self

        class Labels:
            def list(self, userId):
                return outer._run("labels.list", {"labels": outer.labels_})

            def create(self, userId, body):
                outer.rec.seen.append("gmail.googleapis.com labels.create")
                raise AssertionError("a live check must never create a label")

        return Labels()

    def settings(self):
        outer = self

        class Settings:
            def filters(self):
                class Filters:
                    def list(self, userId):
                        return outer._run("filters.list", {"filter": outer.filters_})

                    def create(self, userId, body):
                        outer.rec.seen.append("gmail.googleapis.com filters.create")
                        raise AssertionError("a live check must never create a filter")

                return Filters()

        return Settings()


def gmail_ok(rec: Recorder, settings, *, to_filter: bool = True) -> FakeGmail:
    label_id = "Label_1"
    filters = [
        {
            "id": "f1",
            "criteria": {"from": "@alerts.example.org"},
            "action": {"addLabelIds": [label_id]},
        }
    ]
    if to_filter:
        filters.append(
            {
                "id": "f2",
                "criteria": {"to": settings.mail.alerts_address},
                "action": {"addLabelIds": [label_id], "removeLabelIds": ["INBOX", "SPAM"]},
            }
        )
    return FakeGmail(
        rec,
        labels=[{"id": label_id, "name": settings.mail.label}, {"id": "L2", "name": LEAK}],
        filters=filters,
    )


AUTH_WITH_IDENTITY = {
    "loggedIn": True,
    "authMethod": "claude.ai",
    "apiProvider": "firstParty",
    "email": f"{LEAK}@example.org",
    "orgId": LEAK,
    "orgName": f"{LEAK} Org",
    "subscriptionType": "max",
}


def deps(rec: Recorder, settings, **kw) -> lc.Deps:
    gmail = kw.pop("gmail", None) or gmail_ok(rec, settings)
    return lc.Deps(
        anthropic_client=kw.pop("anthropic_client", None) or anthropic_factory(rec),
        http_client=kw.pop("http_client", None) or http_factory(rec),
        gmail_service=lambda _s: gmail,
        **kw,
    )
