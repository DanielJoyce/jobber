"""GET /help and /help/{topic} (specs/019, Addendum A).

No ``Conn``, no profile, no settings values: help renders before anything is set up and while
the database answers 503. The Host check is the app-wide middleware's, as for every route.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from jobhunter.console import help as helpdocs


def register(app: FastAPI, templates: Jinja2Templates, nav: list[tuple[str, str]]) -> None:
    def respond(request: Request, name: str, title: str, **ctx: object) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            name,
            {
                "title": title,
                "active": "/help",
                "nav": nav,
                "topics": helpdocs.TOPICS,
                **ctx,
            },
        )

    @app.get("/help", response_class=HTMLResponse)
    def help_index(request: Request) -> HTMLResponse:
        entries = [
            (t, [h for h in helpdocs.render(t.slug).headings if h.level in (2, 3)])
            for t in helpdocs.TOPICS
        ]
        return respond(request, "help_index.html", "Help", entries=entries, current=None)

    @app.get("/help/{topic}", response_class=HTMLResponse)
    def help_topic(request: Request, topic: str) -> HTMLResponse:
        entry = helpdocs.TOPIC_BY_SLUG.get(topic)  # a fixed list, never a path built from input
        if entry is None:
            raise HTTPException(status_code=404, detail="no such help topic")
        page = helpdocs.render(topic)
        return respond(request, "help_topic.html", entry.title, page=page, current=topic)
