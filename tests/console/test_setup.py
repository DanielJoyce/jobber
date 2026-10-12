"""/setup (specs/019 S3a): rows, writes, mode-aware fixes, email guide, no value leaks.
Synthetic data only; no network."""

from __future__ import annotations

import json
import re
import stat
from datetime import UTC, datetime
from html import unescape
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from jobhunter import readiness, secrets
from jobhunter.config import Settings, load_settings
from jobhunter.console import prefs_graceful as gr
from jobhunter.console import setup_routes as sr
from jobhunter.console.app import create_app
from jobhunter.core import db
from jobhunter.mail import auth
from jobhunter.scoring.profile import Profile, profile_data, validate_profile_data

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
SENTINEL = "SENTINEL-4d2b8e6a91"
GOOD_PREFS = Path(__file__).resolve().parent.parent.parent / "examples" / "preferences.example.yaml"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch):
    # The page's secret rows read os.environ; drop conftest's placeholder scorer keys (S7) so
    # these tests see a machine with no keys unless a test sets one.
    for name in ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "OPENAI_COMPAT_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    readiness.clear_caches()
    yield
    readiness.clear_caches()


def make_settings(tmp_path: Path, **sections: dict) -> Settings:
    data = tmp_path / "data"
    overrides: dict = {
        "paths": {
            "data_dir": str(data),
            "db_path": str(data / "jobhunter.db"),
            "profile_dir": str(data / "profile"),
            "resume_path": str(data / "resume"),
        }
    }
    for name, values in sections.items():
        overrides.setdefault(name, {}).update(values)
    return load_settings(tmp_path / "no-config.toml", overrides=overrides)


def make_client(settings: Settings, *, mode: str = readiness.HOST, home: Path | None = None):
    data = Path(settings.paths.data_dir)
    data.mkdir(parents=True, exist_ok=True)
    conn = db.connect(settings.paths.db_path)
    db.migrate(conn)
    conn.close()
    app = create_app(settings, lambda: db.connect(settings.paths.db_path), clock=lambda: NOW)
    app.state.setup_mode = mode
    if home is not None:
        app.state.setup_home = home
    return TestClient(app)


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
def client(settings):
    return make_client(settings)


def row(html: str, check_id: str) -> str:
    ident = "row-" + check_id.replace(".", "-")
    match = re.search(rf'<article class="su-row[^"]*" id="{ident}".*?</article>', html, re.S)
    assert match, f"no row {check_id}"
    return match.group(0)


def text_of(fragment: str) -> str:
    return unescape(re.sub(r"<[^>]+>", " ", fragment))


def test_page_renders_groups_in_order_with_nav_and_no_store(client):
    r = client.get("/setup")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    assert ">Setup</a>" in r.text
    heads = re.findall(r"<h2>([^<]+)</h2>", r.text)
    expected = [g for g in readiness.GROUPS if g in heads]
    assert heads == expected
    assert "Basics" in heads and "Email alerts" in heads
    assert 'name="viewport"' in r.text
    assert "setup.css" in r.text


def test_row_shows_status_word_icon_unlocks_and_fix(client):
    html = client.get("/setup").text
    r = row(html, "profile.preferences")
    assert "missing" in r.lower()
    assert "su-icon" in r
    assert "/setup/starter-prefs" in r  # the inline form
    assert "Create a starter preferences file" in r


def test_non_loopback_host_refused(client):
    r = client.get("/setup", headers={"host": "evil.example"})
    assert r.status_code == 403


def test_cross_origin_writes_refused(client):
    for path in ("/setup/starter-prefs", "/setup/dismiss", "/setup/resume"):
        r = client.post(path, headers={"origin": "http://evil.example"})
        assert r.status_code == 403, path


# --- starter prefs ---------------------------------------------------------------------------


def test_starter_prefs_writes_skeleton_0600_and_redirects(client, settings):
    r = client.post("/setup/starter-prefs", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/prefs?created=1"
    path = Path(settings.paths.profile_dir) / "preferences.yaml"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    written = yaml.safe_load(path.read_text())
    loaded = validate_profile_data(written, base=Profile())
    assert profile_data(loaded) == profile_data(gr.skeleton_profile())
    assert written["hard"]["title_exclusions"] == ["intern", "volunteer", "student trainee"]
    # and the row now stops offering the form
    assert "Create a starter preferences file" not in row(
        client.get("/setup?all=1").text, "profile.preferences"
    )


def test_starter_prefs_409_when_file_exists_and_leaves_it(client, settings):
    pdir = Path(settings.paths.profile_dir)
    pdir.mkdir(parents=True)
    (pdir / "preferences.yaml").write_text("target_titles: [Keep Me]\n")
    r = client.post("/setup/starter-prefs", follow_redirects=False)
    assert r.status_code == 409
    assert "Keep Me" in (pdir / "preferences.yaml").read_text()


# --- resume ----------------------------------------------------------------------------------


def test_resume_upload_stores_file_and_row_clears(client, settings):
    client.post("/setup/starter-prefs")
    before = row(client.get("/setup").text, "profile.resume")
    assert "missing" in before.lower()
    r = client.post(
        "/setup/resume",
        files={"file": ("my resume.md", b"# Synthetic Person\nSystems work.\n", "text/markdown")},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    stored = list(Path(settings.paths.resume_path).glob("*.md"))
    assert len(stored) == 1
    assert stat.S_IMODE(stored[0].stat().st_mode) == 0o600
    after = row(client.get("/setup?all=1").text, "profile.resume")
    assert "ok" in text_of(after).lower().split()


def test_resume_upload_htmx_returns_refreshed_row(client):
    client.post("/setup/starter-prefs")
    r = client.post(
        "/setup/resume",
        files={"file": ("r.md", b"# Synthetic\n", "text/markdown")},
        headers={"hx-request": "true"},
    )
    assert r.status_code == 200
    assert r.text.lstrip().startswith("<article")
    assert 'id="row-profile-resume"' in r.text
    assert "<html" not in r.text


@pytest.mark.parametrize(
    ("name", "data", "status"),
    [
        ("r.pdf", b"%PDF-1.4", 415),
        ("r.exe", b"MZ", 415),
        ("big.md", b"x" * (1024 * 1024 + 1), 413),
        ("bin.md", b"\xff\xfe\x00\x01", 422),
        ("empty.md", b"   \n", 422),
    ],
)
def test_resume_upload_refusals(client, settings, name, data, status):
    r = client.post("/setup/resume", files={"file": (name, data, "application/octet-stream")})
    assert r.status_code == status
    assert 'role="alert"' in r.text
    assert (
        not list(Path(settings.paths.resume_path).glob("*"))
        if Path(settings.paths.resume_path).exists()
        else True
    )


def test_resume_upload_no_file_is_422(client):
    r = client.post("/setup/resume")
    assert r.status_code == 422
    assert "Choose a .md or .txt file" in r.text


# --- dismiss ---------------------------------------------------------------------------------


def recommended_failing(client) -> str:
    checks = readiness.run_checks(
        client.app.state.settings,
        secrets_report=None,
        now=NOW,
        mode=readiness.HOST,
    )
    for c in checks:
        if c.level == readiness.RECOMMENDED and c.status in readiness.FAILING:
            return c.id
    raise AssertionError("no recommended failing row in the fresh state")


def test_dismiss_recommended_writes_private_file(client, settings):
    cid = recommended_failing(client)
    r = client.post("/setup/dismiss", data={"check_id": cid}, follow_redirects=False)
    assert r.status_code == 303
    path = Path(settings.paths.data_dir) / "setup-dismissed.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    data = json.loads(path.read_text())["dismissed"]
    assert data[cid]["status"] in readiness.FAILING
    assert data[cid]["at"].startswith("2026-10-11")


def test_dismiss_merges_with_earlier_entries(client, settings):
    ids = [
        c.id
        for c in readiness.run_checks(settings, secrets_report=None, now=NOW, mode=readiness.HOST)
        if c.level == readiness.RECOMMENDED and c.status in readiness.FAILING
    ]
    assert len(ids) >= 2
    client.post("/setup/dismiss", data={"check_id": ids[0]})
    client.post("/setup/dismiss", data={"check_id": ids[1]})
    path = Path(settings.paths.data_dir) / "setup-dismissed.json"
    assert set(json.loads(path.read_text())["dismissed"]) == {ids[0], ids[1]}


def test_dismiss_refuses_essentials_unknown_and_empty(client, settings):
    essential = next(
        c.id
        for c in readiness.run_checks(settings, secrets_report=None, now=NOW, mode=readiness.HOST)
        if c.level == readiness.ESSENTIAL
    )
    path = Path(settings.paths.data_dir) / "setup-dismissed.json"
    for body in ({"check_id": essential}, {"check_id": "nope.nothing"}, {}):
        assert client.post("/setup/dismiss", data=body).status_code == 422
    # all-or-nothing: one bad id among good ones writes nothing
    good = recommended_failing(client)
    assert client.post("/setup/dismiss", data={"check_id": [good, essential]}).status_code == 422
    assert not path.exists()


def test_page_offers_hide_only_on_recommended_rows(client):
    html = client.get("/setup").text
    for m in re.finditer(r'name="check_id" value="([^"]+)"', html):
        level = {
            c.id: c.level
            for c in readiness.run_checks(
                client.app.state.settings, secrets_report=None, now=NOW, mode=readiness.HOST
            )
        }[m.group(1)]
        assert level != readiness.ESSENTIAL


# --- mode-aware fixes, restart notes ----------------------------------------------------------


def test_host_mode_secret_fix_is_env_file_with_restart(client, monkeypatch):
    r = row(client.get("/setup").text, "scorer.screen")
    t = text_of(r)
    assert "ANTHROPIC_API_KEY" in t
    assert "install -m 600 /dev/null ~/.env" in t
    assert "podman" not in t
    assert "restart" in t.lower()
    assert "Ctrl-C" in t


def test_container_mode_uses_read_rs_and_unset(tmp_path):
    c = make_client(make_settings(tmp_path), mode=readiness.HOST_MANAGING)
    t = text_of(row(c.get("/setup").text, "scorer.screen"))
    assert "read -rs KEY" in t and "unset KEY" in t
    assert "podman secret create jobhunter_anthropic -" in t
    assert "systemctl --user restart jobhunter-console" in t
    assert "~/.env" not in t


def test_container_mode_shows_host_path_with_container_path_in_brackets():
    shown = readiness.display_path("/config/google_client_secret.json", readiness.CONTAINER)
    assert shown.startswith("~/") and shown.endswith("[/config/google_client_secret.json]")
    assert readiness.display_path("/config/x", readiness.HOST) == "/config/x"


def test_container_page_says_paths_are_host_paths(tmp_path):
    c = make_client(make_settings(tmp_path), mode=readiness.CONTAINER)
    assert "Paths show the host path" in c.get("/setup").text


def test_rows_read_from_config_toml_carry_restart_note(client):
    r = row(client.get("/setup").text, "fetch.contact")
    assert "Read when the console started" in r
    assert "(2026-10-1" in r  # local time, from the localtime filter
    assert "Ctrl-C" in r


def test_replace_flag_only_when_the_secret_row_is_broken(tmp_path, monkeypatch):
    s = make_settings(tmp_path)
    c = make_client(s, mode=readiness.CONTAINER)
    missing = text_of(row(c.get("/setup").text, "scorer.screen"))
    assert "podman secret create jobhunter_anthropic -" in missing
    assert "--replace" not in missing
    environ = {"ANTHROPIC_API_KEY": "has space"}
    names = secrets.known_names(s)
    states = secrets.resolve(names, set(environ), {}, lambda line: None, environ=environ)
    c.app.state.secrets_report = secrets.Report(states, [])
    c.app.state.setup_environ = environ
    broken = text_of(row(c.get("/setup").text, "scorer.screen"))
    assert "podman secret create --replace jobhunter_anthropic -" in broken
    assert "has space" not in broken


# --- email alerts guide -----------------------------------------------------------------------


def test_email_guide_is_numbered_and_waits_on_earlier_steps(client):
    html = client.get("/setup").text
    section = re.search(
        r'<section class="su-group" id="group-email-alerts">.*?</section>', html, re.S
    )
    assert section
    t = text_of(section.group(0))
    assert "Step 1:" in t and "Step 2:" in t and "Step 3:" in t
    token = text_of(row(html, "mail.token"))
    assert "Waiting on step 1" in token
    assert "jobhunter mail auth" not in token  # its own fix is hidden while it waits
    label = text_of(row(html, "mail.label"))
    assert "Waiting on step 2" in label


def test_waiting_follows_the_unmet_dependency(tmp_path):
    s = make_settings(tmp_path, mail={"alerts_address": "x+jobs@example.test"})
    html = make_client(s).get("/setup").text
    assert "Waiting on step 2" not in text_of(row(html, "mail.label"))  # step 2 is done
    assert "Waiting on step 1" in text_of(row(html, "mail.token"))


def test_cloud_steps_shown_inline_from_cloud_steps_with_target_path(client, tmp_path):
    html = client.get("/setup").text
    client_row = row(html, "mail.client")
    t = text_of(client_row)
    head = auth.CLOUD_STEPS.partition("  5. Save it")[0]
    for line in head.splitlines()[1:]:
        if line.strip()[:2] in {"1.", "2.", "3.", "4."}:
            first = line.strip()[3:].split("  ")[0]
            assert " ".join(first.split()) in " ".join(t.split()), line
    target = readiness.display_path(auth.client_secrets_path(client.app.state.settings), "host")
    assert target in t
    assert "console.cloud.google.com" in client_row
    assert 'href="/help/email-alerts#step-1-google-client-json"' in client_row


def test_cloud_steps_in_container_mode_name_the_podman_secret(tmp_path):
    c = make_client(make_settings(tmp_path), mode=readiness.HOST_MANAGING)
    t = text_of(row(c.get("/setup").text, "mail.client"))
    assert "podman secret create jobhunter_google_client <file>" in t
    assert "jobhunter-ctr mail auth" in t


def test_cloud_steps_helper_covers_all_five_steps(settings):
    steps = sr.cloud_steps(settings, readiness.HOST, None)
    assert [s[:2] for s in steps[:5]] == ["1.", "2.", "3.", "4.", "5."]
    assert steps[-1] == "Then run: jobhunter mail auth"


# --- links, leaks -----------------------------------------------------------------------------


def internal_links(html: str) -> set[str]:
    hrefs = set(re.findall(r'href="([^"#]+)', html))
    return {h for h in hrefs if h.startswith("/") and not h.startswith("/static/")}


def test_every_internal_link_resolves_including_help(tmp_path):
    s = make_settings(tmp_path, mail={"alerts_address": "x+jobs@example.test"})
    c = make_client(s, mode=readiness.HOST)
    html = c.get("/setup?all=1").text
    links = internal_links(html.split("<main>", 1)[1])  # not the nav bar
    assert "/prefs" in links or "/alerts" in links or links
    assert "/help/getting-started" in links
    for href in links:
        assert c.get(href).status_code == 200, href
    # external links are present but not fetched
    assert 'href="https://console.cloud.google.com/"' in html


def _help_page_ids(client: TestClient, topic: str) -> set[str]:
    return set(re.findall(r'id="([^"]+)"', client.get(f"/help/{topic}").text))


@pytest.mark.parametrize("mode", [readiness.HOST, readiness.CONTAINER, readiness.HOST_MANAGING])
def test_every_setup_row_links_a_help_anchor_that_exists(tmp_path, mode):
    s = make_settings(tmp_path)
    c = make_client(s, mode=mode)
    html = c.get("/setup?all=1").text
    checks = set(readiness.CHECK_IDS) - sr.DOCTOR_ONLY
    for check_id in checks:
        row_html = row(html, check_id)
        assert row_html, check_id
        href = re.search(r'<p class="su-help"><a href="([^"]+)"', row_html)
        assert href, check_id
        topic, _, anchor = href.group(1)[len("/help/") :].partition("#")
        assert c.get(f"/help/{topic}").status_code == 200, check_id
        if anchor:
            assert anchor in _help_page_ids(c, topic), (check_id, href.group(1))


def test_gmail_setup_redirects_to_step_one(tmp_path):
    c = make_client(make_settings(tmp_path))
    r = c.get("/setup/gmail", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/help/email-alerts#step-1-google-client-json"
    assert c.get(r.headers["location"]).status_code == 200
    assert "step-1-google-client-json" in _help_page_ids(c, "email-alerts")


def test_container_secret_row_links_the_containers_secrets_section(tmp_path):
    c = make_client(make_settings(tmp_path), mode=readiness.CONTAINER)
    assert 'href="/help/containers#secrets"' in row(c.get("/setup").text, "secrets.valid")


def test_setup_page_links_getting_started(client):
    assert 'href="/help/getting-started"' in client.get("/setup").text


def test_no_value_ever_rendered(tmp_path, monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "USAJOBS_API_KEY", "USAJOBS_EMAIL"):
        monkeypatch.setenv(name, SENTINEL + name)
    s = make_settings(
        tmp_path,
        fetch={"user_agent_contact": f"{SENTINEL}@example.test"},
        mail={"alerts_address": f"{SENTINEL}+jobs@example.test"},
    )
    c = make_client(s)
    for url in ("/setup", "/setup?all=1"):
        r = c.get(url)
        assert r.status_code == 200
        assert SENTINEL not in r.text
    r = c.post("/setup/dismiss", data={"check_id": "bogus"})
    assert SENTINEL not in r.text


def test_dark_mode_and_phone_width_css():
    css = (
        Path(__file__).resolve().parents[2] / "src/jobhunter/console/static/setup.css"
    ).read_text()
    assert "@media (max-width: 600px)" in css
    assert "#" not in re.sub(r"/\*.*?\*/", "", css)  # colours come from the app.css tokens
    assert "var(--surface-1)" in css


def test_all_ok_group_collapses_and_show_everything_expands(tmp_path):
    s = make_settings(tmp_path)
    c = make_client(s)
    default = c.get("/setup").text
    everything = c.get("/setup?all=1").text
    assert "Collapse finished items" in everything
    assert "Show everything" in default
    assert default.count("<details") > everything.count("<details")


# --- review fixes (B2, S1, minors) -------------------------------------------------------------


def test_container_resume_upload_repoints_an_absolute_yaml_path(tmp_path):
    s = make_settings(tmp_path)
    pdir = Path(s.paths.profile_dir)
    pdir.mkdir(parents=True)
    text = GOOD_PREFS.read_text().replace("resume_path: null", "resume_path: /home/x/resume.md")
    (pdir / "preferences.yaml").write_text(text)
    c = make_client(s, mode=readiness.CONTAINER)
    before = row(c.get("/setup").text, "profile.resume")
    assert "su-broken" in before
    r = c.post(
        "/setup/resume",
        files={"file": ("cv.md", b"# Synthetic\n", "text/markdown")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    saved = yaml.safe_load((pdir / "preferences.yaml").read_text())
    assert not Path(saved["resume_path"]).is_absolute()
    assert (pdir / saved["resume_path"]).resolve().is_file()
    after = row(c.get("/setup?all=1").text, "profile.resume")
    assert "su-ok" in after


def test_htmx_swap_carries_the_refreshed_summary(client):
    client.post("/setup/starter-prefs")
    r = client.post(
        "/setup/resume",
        files={"file": ("r.md", b"# Synthetic\n", "text/markdown")},
        headers={"hx-request": "true"},
    )
    assert 'id="su-summary" hx-swap-oob="true"' in r.text


def test_dismissed_row_collapses_and_returns_when_status_changes(client, settings):
    cid = recommended_failing(client)
    client.post("/setup/dismiss", data={"check_id": cid})
    html = client.get("/setup").text
    assert "hidden (" in row(html, cid)
    assert "Hide this for now" not in row(html, cid)
    path = Path(settings.paths.data_dir) / "setup-dismissed.json"
    data = json.loads(path.read_text())
    data["dismissed"][cid]["status"] = "ok"  # it was hidden as something else
    path.write_text(json.dumps(data))
    assert "hidden (" not in row(client.get("/setup").text, cid)


def test_doctor_only_rows_are_not_listed(client):
    html = client.get("/setup?all=1").text
    assert 'id="row-config-file"' not in html
    assert 'id="row-db-schema"' not in html


def test_no_literal_backticks_or_double_or_or_test_button_text(tmp_path):
    c = make_client(make_settings(tmp_path, mail={"alerts_address": "x+jobs@example.test"}))
    main = c.get("/setup?all=1").text.split("<main>", 1)[1]
    t = text_of(main)
    assert "`" not in t
    assert "or or" not in " ".join(t.split())
    assert "Test (free)" not in t
