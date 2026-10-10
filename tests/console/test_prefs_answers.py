"""/prefs Application answers (specs/017 "Application answers", "Never-store list").

The section saves with the main /prefs form into ``answers:`` of preferences.yaml. These tests
go through the routes and read the file back, so they fail if the section stops saving, if a
never-store answer is written, or if a bad hand edit can break the profile or the nightly
profile loader. Synthetic data only.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from ruamel.yaml import YAML

from jobhunter.apply import answers
from jobhunter.config import Paths, Settings
from jobhunter.console import prefs as pf
from jobhunter.console.app import create_app
from jobhunter.core import db
from jobhunter.pipeline import runner
from jobhunter.scoring.profile import load_profile, preferences_mtime_ns, profile_data

BASE = """\
# synthetic test profile
resume_path: resume.md
target_titles: [Systems Engineer]
hard:
  states_allowed: all
  remote_ok: true
narrative:
  want: Synthetic systems work.
"""
ANSWERS = """\
answers:
  # my links
  links:
    github: https://example.com/synthetic-gh
  notice_period: two weeks
  custom:
    - question: Why do you want to work here?
      answer: I like synthetic widgets.
"""
HAND_EDIT = """\
answers:
  links:
    portfolio: https://example.com/folio
  desired_salary: 123456
  custom:
    - question: Desired salary
      answer: "999999"
    - question: Why us?
      answer: Calm teams.
    - question: Missing its answer
"""


@pytest.fixture
def pdir(tmp_path):
    d = tmp_path / "profile"
    d.mkdir()
    (d / "preferences.yaml").write_text(BASE + ANSWERS, encoding="utf-8")
    (d / "resume.md").write_text("Synthetic Person\n- ran synthetic hosts\n", encoding="utf-8")
    return d


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "t.db"
    c = db.connect(path)
    db.migrate(c)
    c.close()
    return path


@pytest.fixture
def client(pdir, db_path):
    settings = Settings(paths=Paths(profile_dir=pdir, db_path=db_path))
    return TestClient(create_app(settings, lambda: db.connect(db_path)), follow_redirects=False)


def text_of(pdir):
    return (pdir / "preferences.yaml").read_text(encoding="utf-8")


def answers_in_file(pdir):
    return YAML(typ="safe").load(text_of(pdir)).get("answers")


def page_form(pdir, **answer_fields):
    """What the page posts: every profile field as rendered, plus the answers section."""
    view = pf.view_from_data(profile_data(load_profile(pdir)))
    form: list[tuple[str, str]] = []
    for key, value in view.items():
        if isinstance(value, bool):
            if value:
                form.append((key, "1"))
        elif isinstance(value, list):
            form.extend((key, v) for v in value)
        else:
            form.append((key, value))
    form.append(("mtime_ns", str(preferences_mtime_ns(pdir))))
    form.append(("ans.present", "1"))
    current = answers.load_answers(pdir).answers
    fields = {f"ans.links.{k}": getattr(current.links, k) or "" for k in answers.LINK_KEYS}
    fields |= {f"ans.{k}": getattr(current, k) or "" for k, _ in answers.TEXT_FIELDS}
    fields["ans.work_authorization"] = ""
    fields |= answer_fields
    custom = fields.pop("custom", [(c.question, c.answer) for c in current.custom])
    form.extend(fields.items())
    for q, a in [*custom, ("", "")]:
        form.append(("ans.custom.question", q))
        form.append(("ans.custom.answer", a))
    return form


def post(client, path, form):
    body = "&".join(f"{_q(k)}={_q(v)}" for k, v in form)
    return client.post(
        path, content=body, headers={"content-type": "application/x-www-form-urlencoded"}
    )


def _q(s):
    from urllib.parse import quote_plus

    return quote_plus(s)


# ─── page ───────────────────────────────────────────────────────────────────


def test_section_shows_the_answers_with_a_help_button_on_each_field(client):
    r = client.get("/prefs")
    assert r.status_code == 200
    html = r.text
    assert 'id="sec-answers"' in html and 'href="#sec-answers"' in html
    assert 'value="https://example.com/synthetic-gh"' in html
    assert 'value="two weeks"' in html
    assert "I like synthetic widgets." in html
    # inside the main form, so the same Save (and live preview) handles it
    start = html.index('id="prefs-form"')
    assert start < html.index('id="sec-answers"') < html.index("</form>", start)
    for help_id in ("links-portfolio", "links-github", "notice_period", "work-auth", "custom"):
        assert f'data-help="ans-{help_id}"' in html and f'id="help-ans-{help_id}"' in html


# ─── save ───────────────────────────────────────────────────────────────────


def test_save_writes_answers_keeps_comments_and_logs_no_values(client, pdir, db_path):
    form = page_form(
        pdir,
        **{"ans.links.linkedin": "https://example.com/in/synthetic", "ans.remote": "Remote first"},
        custom=[
            ("Why do you want to work here?", "I like synthetic widgets."),
            ("What makes you a good fit?", "Synthetic reasons."),
        ],
    )
    r = post(client, "/prefs/save", form)
    assert r.status_code == 303, r.text
    assert r.headers["location"] == "/prefs?saved=3"
    a = answers_in_file(pdir)
    assert a["links"] == {
        "github": "https://example.com/synthetic-gh",
        "linkedin": "https://example.com/in/synthetic",
    }
    assert a["remote"] == "Remote first"
    assert a["custom"][1] == {
        "question": "What makes you a good fit?",
        "answer": "Synthetic reasons.",
    }
    assert "# my links" in text_of(pdir) and "# synthetic test profile" in text_of(pdir)
    # answers are not profile history: no value reaches the database
    c = db.connect(db_path)
    rows = c.execute("SELECT field_path, old_value, new_value FROM profile_change").fetchall()
    c.close()
    assert not [r for r in rows if "answers" in r[0] or "Synthetic reasons" in (r[2] or "")]


def test_clearing_a_custom_row_removes_it_and_empty_answers_disappear(client, pdir):
    form = page_form(pdir, **{"ans.links.github": "", "ans.notice_period": ""}, custom=[("", "")])
    assert post(client, "/prefs/save", form).status_code == 303
    assert "answers" not in YAML(typ="safe").load(text_of(pdir))


def test_never_store_question_is_refused_and_nothing_is_written(client, pdir):
    before = text_of(pdir)
    form = page_form(
        pdir,
        **{"target_titles": "Changed Title"},
        custom=[("Why do you want to work here?", "x"), ("Desired salary", "a lot")],
    )
    r = post(client, "/prefs/save", form)
    assert r.status_code == 422
    assert "&#39;Desired salary&#39; is on the never-store list" in r.text
    assert 'id="err-ans-custom-1-question"' in r.text
    # the whole save is refused: profile changes too
    assert text_of(pdir) == before


def test_form_without_the_section_leaves_answers_alone(client, pdir):
    form = [(k, v) for k, v in page_form(pdir) if not k.startswith("ans.")]
    form = [(k, "New Title" if k == "target_titles" else v) for k, v in form]
    assert post(client, "/prefs/save", form).status_code == 303
    assert answers_in_file(pdir)["notice_period"] == "two weeks"
    assert load_profile(pdir).target_titles == ["New Title"]


def test_preview_counts_answer_changes_and_flags_a_never_store_one(client, pdir):
    r = post(client, "/prefs/preview", page_form(pdir, **{"ans.relocation": "Open to it"}))
    assert "Application answers: 1 unsaved change" in r.text
    r = post(client, "/prefs/preview", page_form(pdir, custom=[("Your gender", "x")]))
    assert "never-store list" in r.text


def test_raw_yaml_save_refuses_a_never_store_answer(client, pdir):
    (pdir / "preferences.yaml").write_text("hard: [broken\n", encoding="utf-8")
    raw = BASE + "answers:\n  custom:\n    - question: Expected CTC\n      answer: x\n"
    r = post(
        client,
        "/prefs/raw",
        [("raw_yaml", raw), ("mtime_ns", str(preferences_mtime_ns(pdir)))],
    )
    assert r.status_code == 422
    assert "never-store list" in r.text
    assert text_of(pdir) == "hard: [broken\n"
    assert "Expected CTC" in r.text  # the raw editor keeps what was typed


def test_creating_the_file_from_the_form_writes_the_answers_too(tmp_path, db_path):
    pdir = tmp_path / "fresh"
    settings = Settings(paths=Paths(profile_dir=pdir, db_path=db_path))
    c = TestClient(create_app(settings, lambda: db.connect(db_path)), follow_redirects=False)
    page = c.get("/prefs").text
    assert 'id="sec-answers"' in page
    form = [
        ("mtime_ns", "0"),
        ("ans.present", "1"),
        ("ans.notice_period", "one month"),
        ("ans.custom.question", "Why us?"),
        ("ans.custom.answer", "Because."),
        *[(f"w.{k}", "20") for k in pf.WEIGHT_KEYS],
    ]
    r = post(c, "/prefs/save", form)
    assert r.status_code == 303, r.text
    data = YAML(typ="safe").load((pdir / "preferences.yaml").read_text("utf-8"))
    assert data["answers"] == {
        "notice_period": "one month",
        "custom": [{"question": "Why us?", "answer": "Because."}],
    }


# ─── hand edits: dropped with a warning, never breaking the profile ─────────


def test_hand_edited_never_store_answer_is_dropped_with_a_warning(client, pdir):
    (pdir / "preferences.yaml").write_text(BASE + HAND_EDIT, encoding="utf-8")
    loaded = answers.load_answers(pdir)
    assert [c.question for c in loaded.answers.custom] == ["Why us?"]
    assert loaded.answers.links.portfolio == "https://example.com/folio"
    assert len(loaded.refused) == 2  # the custom entry and the desired_salary key
    assert len(loaded.warnings) == 3  # plus the entry missing its answer
    # warnings name labels, never values
    assert not [w for w in loaded.warnings if "999999" in w or "123456" in w]
    page = client.get("/prefs").text
    assert 'id="answers-warnings"' in page
    assert "&#39;Desired salary&#39; is on the never-store list" in page
    assert "999999" not in page and "123456" not in page


@pytest.mark.parametrize(
    "block",
    [
        HAND_EDIT,
        "answers: just a string\n",
        "answers:\n  custom: 5\n  links: [a, b]\n  work_authorization: {x: 1}\n",
        "answers:\n  - one\n  - two\n",
    ],
)
def test_bad_answers_never_affect_the_profile_or_the_nightly_loader(tmp_path, block):
    clean, edited = tmp_path / "clean", tmp_path / "edited"
    for d, text in ((clean, BASE), (edited, BASE + block)):
        d.mkdir()
        (d / "preferences.yaml").write_text(text, encoding="utf-8")
        (d / "resume.md").write_text("Synthetic Person\n", encoding="utf-8")
    a, b = load_profile(clean), load_profile(edited)
    assert b.load_warnings == ()  # Profile never sees answers:, so it has nothing to warn about
    assert (a.filter_version, a.scoring_version) == (b.filter_version, b.scoring_version)
    assert "answers" not in b.model_dump()
    nightly = runner.default_profile_loader(
        Settings(paths=Paths(profile_dir=edited, resume_path=edited / "resume.md"))
    )()
    assert nightly is not None and nightly.filter_version == a.filter_version
    answers.load_answers(edited)  # never raises


def test_the_fixed_answer_labels_are_not_on_the_never_store_list():
    from jobhunter.apply import labels

    for label in answers.FIELD_LABELS.values():
        assert labels.never_store(label) is None, label
