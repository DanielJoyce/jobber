"""Console resume upload and profile writes land in the resolved (XDG) paths. Synthetic."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from urllib.parse import urlencode

from fastapi.testclient import TestClient

from jobhunter.config import load_settings
from jobhunter.console.app import create_app
from jobhunter.xdg import data_home

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def make_client() -> TestClient:
    # No paths configured at all: every location is a default.
    return TestClient(create_app(load_settings(), clock=lambda: NOW))


def test_resume_upload_goes_to_the_xdg_data_dir():
    client = make_client()
    r = client.post(
        "/prefs/resume",
        files={"file": ("me.md", b"# Synthetic Person\n- did synthetic things\n", "text/plain")},
    )
    assert r.status_code == 200
    stored = list((data_home() / "resume").glob("*.md"))
    assert [p.name for p in stored] == ["me.md"]


def test_profile_created_below_the_xdg_data_dir():
    client = make_client()
    html = client.get("/prefs").text
    form = {"mtime_ns": re.search(r'name="mtime_ns" value="(\d+)"', html).group(1)}
    for name in ("w.skills", "w.seniority", "w.domain", "w.comp", "w.location"):
        form[name] = re.search(rf'name="{re.escape(name)}"[^>]*value="(\d+)"', html).group(1)
    form.update({"hard.salary_floor.amount": "150000", "salary_period": "year"})
    form["target_titles"] = "Platform Engineer"
    r = client.post(
        "/prefs/save",
        content=urlencode(form, doseq=True),
        headers={"content-type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    prefs = data_home() / "profile" / "preferences.yaml"
    assert "150000" in prefs.read_text(encoding="utf-8")
