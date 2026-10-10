"""/prefs never dead-ends: skeleton form, broken-file repair, resume upload, help. Synthetic."""

from __future__ import annotations

import re
import stat
from datetime import UTC, datetime
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from jobhunter.config import Paths, Settings
from jobhunter.console import prefs_graceful as gr
from jobhunter.console.app import create_app
from jobhunter.core import db
from jobhunter.scoring.profile import load_profile

NOW = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
GOOD = """\
target_titles: [Systems Engineer]
hard:
  remote_ok: true
  salary_floor: {amount: 140000, period: year}
soft:
  weights: {skills: 0.30, seniority: 0.20, domain: 0.20, comp: 0.15, location: 0.15}
  state_ranking: [CO, WA]
narrative:
  want: Synthetic systems work.
"""


def mode(path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def dirs(tmp_path):
    return tmp_path / "profile", tmp_path / "resume"


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "t.db"
    c = db.connect(path)
    db.migrate(c)
    c.close()
    return path


@pytest.fixture
def client(dirs, db_path):
    pdir, rdir = dirs
    settings = Settings(paths=Paths(profile_dir=pdir, resume_path=rdir, db_path=db_path))
    return TestClient(create_app(settings, lambda: db.connect(db_path), clock=lambda: NOW))


def fields(client_):
    """Default form values as the page would post them, from the rendered inputs."""
    html = client_.get("/prefs").text
    form = {"mtime_ns": re.search(r'name="mtime_ns" value="(\d+)"', html).group(1)}
    for name in ("w.skills", "w.seniority", "w.domain", "w.comp", "w.location"):
        form[name] = re.search(rf'name="{re.escape(name)}"[^>]*value="(\d+)"', html).group(1)
    return form


def post_form(client_, form):
    return client_.post(
        "/prefs/save",
        content=urlencode(form, doseq=True),
        headers={"content-type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )


def write_good(pdir, text=GOOD):
    pdir.mkdir(exist_ok=True)
    (pdir / "preferences.yaml").write_text(text, encoding="utf-8")


def write_resume(rdir, name="me.md", text="Synthetic Person\n- ran synthetic hosts\n"):
    rdir.mkdir(exist_ok=True)
    (rdir / name).write_text(text, encoding="utf-8")


# ─── no preferences file ────────────────────────────────────────────────────


def test_skeleton_validates():
    assert gr.skeleton_profile().hard.remote_ok is True
    assert gr.skeleton_profile().narrative.want is None


def test_no_profile_shows_skeleton_form(client, dirs):
    r = client.get("/prefs")
    assert r.status_code == 200
    assert "No profile yet" in r.text
    assert "Save to create" in r.text
    assert 'name="hard.salary_floor.amount"' in r.text
    assert "Systems Engineer" not in r.text  # placeholder text cleared
    assert 'id="resume-warning"' in r.text
    assert not dirs[0].exists()  # viewing writes nothing


def test_save_creates_private_valid_file(client, dirs):
    pdir, _ = dirs
    form = fields(client)
    form.update({"hard.salary_floor.amount": "150000", "salary_period": "year"})
    form["target_titles"] = "Platform Engineer"
    r = post_form(client, form)
    assert r.status_code == 303 and "created=1" in r.headers["location"]
    prefs = pdir / "preferences.yaml"
    assert mode(prefs) == 0o600
    assert mode(pdir) == 0o700
    text = prefs.read_text(encoding="utf-8")
    assert text.startswith("# Preferences for jobhunter")
    profile = load_profile(pdir, resume_path=_make_resume(pdir.parent))
    assert profile.hard.salary_floor.amount == 150000
    assert profile.target_titles == ["Platform Engineer"]
    assert "Created" in client.get("/prefs?created=1").text


def _make_resume(base):
    path = base / "tmp-resume.md"
    path.write_text("Synthetic\n", encoding="utf-8")
    return path


def test_save_with_bad_value_keeps_form_and_creates_nothing(client, dirs):
    form = fields(client)
    form["hard.salary_floor.amount"] = "lots"
    r = post_form(client, form)
    assert r.status_code == 422
    assert "must be a number" in r.text
    assert not dirs[0].exists()


# ─── broken file ────────────────────────────────────────────────────────────


def test_invalid_yaml_shows_form_and_raw_editor(client, dirs):
    pdir, rdir = dirs
    write_resume(rdir)
    write_good(pdir, "hard: [unclosed\n  nonsense: : :\n")
    r = client.get("/prefs")
    assert r.status_code == 200
    assert 'name="raw_yaml"' in r.text
    assert "invalid YAML" in r.text
    assert 'name="hard.salary_floor.amount"' in r.text


def test_raw_editor_fixes_file_and_keeps_bak(client, dirs):
    pdir, rdir = dirs
    write_resume(rdir)
    broken = "hard: [unclosed\n"
    write_good(pdir, broken)
    mtime = re.search(r'name="mtime_ns" value="(\d+)"', client.get("/prefs").text).group(1)
    r = client.post("/prefs/raw", data={"raw_yaml": "hard: [unclosed\n", "mtime_ns": mtime})
    assert r.status_code == 422 and "invalid YAML" in r.text
    assert 'name="raw_yaml"' in r.text and "unclosed" in r.text  # typed text is kept
    r = client.post(
        "/prefs/raw", data={"raw_yaml": GOOD, "mtime_ns": mtime}, follow_redirects=False
    )
    assert r.status_code == 303
    baks = list(pdir.glob("preferences.yaml.*.bak"))
    assert len(baks) == 1 and baks[0].read_text(encoding="utf-8") == broken
    assert mode(pdir / "preferences.yaml") == 0o600
    assert load_profile(pdir, rdir).target_titles == ["Systems Engineer"]


def test_validation_error_prefills_other_fields_with_inline_error(client, dirs):
    pdir, rdir = dirs
    write_resume(rdir)
    write_good(pdir, GOOD + "buckets:\n  nonsense_key: 3\nqueries:\n  - bogus: 1\n")
    r = client.get("/prefs")
    assert r.status_code == 200
    assert 'value="140000"' in r.text  # salary kept
    assert "Systems Engineer" in r.text  # titles kept
    assert 'class="field-error"' in r.text
    assert "err-queries" in r.text
    assert 'name="raw_yaml"' in r.text


def test_saving_broken_profile_writes_valid_file_and_bak(client, dirs):
    pdir, rdir = dirs
    write_resume(rdir)
    bad = GOOD + "queries:\n  - bogus: 1\n"
    write_good(pdir, bad)
    form = fields(client)
    form["queries"] = "linux administrator, systems engineer"
    form["hard.salary_floor.amount"] = "140000"
    r = post_form(client, form)
    assert r.status_code == 303 and "repaired=1" in r.headers["location"]
    baks = list(pdir.glob("preferences.yaml.*.bak"))
    assert len(baks) == 1 and baks[0].read_text(encoding="utf-8") == bad
    profile = load_profile(pdir, rdir)
    assert profile.queries[0].keywords == ["linux administrator", "systems engineer"]
    assert profile.hard.salary_floor.amount == 140000


# ─── missing resume and upload ──────────────────────────────────────────────


def test_missing_resume_warns_and_offers_upload(client, dirs):
    pdir, rdir = dirs
    write_good(pdir)
    r = client.get("/prefs")
    assert r.status_code == 200
    assert 'id="resume-warning"' in r.text
    assert "Resume not found at" in r.text and str(rdir) in r.text
    assert 'type="file"' in r.text and 'action="/prefs/resume"' in r.text
    assert "No profile yet" not in r.text


def test_save_works_while_resume_missing(client, dirs):
    pdir, _ = dirs
    write_good(pdir)
    form = fields(client)
    form["hard.salary_floor.amount"] = "160000"
    r = post_form(client, form)
    assert r.status_code == 303
    assert "160000" in (pdir / "preferences.yaml").read_text(encoding="utf-8")


def upload(client, name, data):
    return client.post("/prefs/resume", files={"file": (name, data, "application/octet-stream")})


def test_upload_md_writes_private_file_and_reports_hash(client, dirs):
    pdir, rdir = dirs
    write_good(pdir)
    r = upload(client, "My Resume (v2).md", b"# Synthetic Person\n- did synthetic things\n")
    assert r.status_code == 200
    files = list(rdir.glob("*.md"))
    assert [f.name for f in files] == ["My-Resume-v2.md"]
    assert mode(files[0]) == 0o600
    sha = gr.sha256_text("# Synthetic Person\n- did synthetic things\n")
    assert sha in r.text
    assert "paid change" in r.text
    assert 'id="resume-warning"' not in r.text
    assert load_profile(pdir, rdir).resume_text.startswith("# Synthetic Person")
    assert "Synthetic Person" not in r.text  # content is never echoed


def test_upload_moves_previous_to_history_and_logs_change(client, dirs, db_path):
    pdir, rdir = dirs
    write_good(pdir)
    write_resume(rdir, "old.md", "Old Synthetic\n")
    client.get("/prefs")  # baseline snapshot
    r = upload(client, "new.txt", b"New Synthetic\n")
    assert r.status_code == 200
    assert [f.name for f in rdir.glob("*.md")] == ["new.md"]
    prev = list((rdir / ".previous").iterdir())
    assert len(prev) == 1 and prev[0].name.startswith("old.") and prev[0].suffix == ".md"
    assert prev[0].read_text(encoding="utf-8") == "Old Synthetic\n"
    assert mode(rdir / ".previous") == 0o700
    c = db.connect(db_path)
    rows = c.execute(
        "SELECT source FROM profile_change WHERE field_path = 'resume_sha256'"
    ).fetchall()
    c.close()
    assert [r["source"] for r in rows] == ["ui"]


def test_upload_replaces_same_name(client, dirs):
    pdir, rdir = dirs
    write_good(pdir)
    write_resume(rdir, "me.md", "First\n")
    upload(client, "me.md", b"Second\n")
    assert (rdir / "me.md").read_text(encoding="utf-8") == "Second\n"
    assert len(list((rdir / ".previous").iterdir())) == 1


def test_upload_creates_resume_dir_for_missing_profile(client, dirs):
    _, rdir = dirs
    r = upload(client, "me.md", b"Synthetic\n")
    assert r.status_code == 200
    assert mode(rdir) == 0o700
    assert (rdir / "me.md").exists()


def test_upload_rejects_too_large(client, dirs):
    pdir, rdir = dirs
    write_good(pdir)
    r = upload(client, "big.md", b"x" * (gr.MAX_RESUME_BYTES + 1))
    assert r.status_code == 413
    assert "1 MB" in r.text
    assert not rdir.exists()


@pytest.mark.parametrize("name", ["cv.pdf", "cv.docx"])
def test_upload_rejects_office_formats_with_advice(client, dirs, name):
    r = upload(client, name, b"%PDF-1.4 synthetic")
    assert r.status_code == 415
    assert "Markdown" in r.text and "text" in r.text
    assert not dirs[1].exists()


def test_upload_rejects_other_types_and_binary(client, dirs):
    assert upload(client, "cv.exe", b"MZ").status_code == 415
    assert upload(client, "cv.md", b"\xff\xfe\x00\x01").status_code == 422
    assert upload(client, "cv.md", b"   \n").status_code == 422
    assert not dirs[1].exists()


def test_upload_filename_cannot_escape_dir(client, dirs):
    pdir, rdir = dirs
    write_good(pdir)
    upload(client, "../../evil.md", b"Synthetic\n")
    assert [p.name for p in rdir.glob("*.md")] == ["evil.md"]
    assert not (pdir.parent / "evil.md").exists()


def test_nothing_outside_tmp_dirs_is_written(client, dirs, tmp_path, monkeypatch):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    upload(client, "me.md", b"Synthetic\n")
    post_form(client, fields(client))
    assert list(cwd.iterdir()) == []


# ─── help and buckets ───────────────────────────────────────────────────────


def buckets_section(html):
    start = html.index('id="sec-buckets"')
    return html[start : html.index("</section>", start)]


def test_help_buttons_render_for_every_section(client, dirs):
    pdir, rdir = dirs
    write_good(pdir)
    write_resume(rdir)
    html = client.get("/prefs").text
    for sec in ("pay", "where", "what", "weights", "buckets", "queries", "recency", "resume"):
        assert re.search(
            rf'<button[^>]*data-help="{sec}"[^>]*aria-expanded="false"[^>]*'
            rf'aria-controls="help-{sec}"',
            html,
        )
        assert f'id="help-{sec}"' in html and 'role="region"' in html


def test_bucket_fields_have_tooltips_and_no_spec_references(client, dirs):
    from jobhunter.scoring import buckets as bk

    pdir, rdir = dirs
    write_good(pdir)
    write_resume(rdir)
    section = buckets_section(client.get("/prefs").text)
    assert "Every scored job lands in one bucket" in section
    assert "<table" in section and "Bullseye" in section and "Bullseye (A)" not in section
    assert "Any stated requirement you don" in section
    for key in bk.DEFAULT_THRESHOLDS:
        assert re.search(rf'<input[^>]*name="buckets.{key}"[^>]*title="[^"]+"', section)
        assert f'id="bh-{key}"' in section
    assert "specs/" not in section and "006" not in section
    assert "Display only" in section
    assert "preview panel" in section


def test_no_spec_references_anywhere_on_page(client, dirs):
    pdir, rdir = dirs
    write_good(pdir)
    write_resume(rdir)
    html = client.get("/prefs").text
    assert "specs/" not in html
