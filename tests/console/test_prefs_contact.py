"""/prefs contact block, sponsorship and Copy from my resume header (specs/022 A4).

Through the routes, reading the file back. Canary values only; nothing is a real address.
"""

# ruff: noqa: F811  (fixtures imported from test_prefs_answers are used as parameters)
from __future__ import annotations

import logging

import pytest
from test_prefs_answers import (  # noqa: F401
    BASE,
    answers_in_file,
    client,
    db_path,
    pdir,
    post,
    text_of,
)
from test_prefs_answers import page_form as base_form

from jobhunter.apply import answers, labels
from jobhunter.apply.resume_header import propose_contact
from jobhunter.core import db
from jobhunter.scoring.profile import load_profile, preferences_mtime_ns

CONTACT = {
    "first_name": "canaryfirst",
    "last_name": "canarylast",
    "email": "canary.contact@invalid.test",
    "phone": "555-0100",
    "city": "canarycity",
}


def form_with(pdir, contact=None, sponsorship="", **more):
    form = base_form(pdir, **more)
    contact = contact or {}
    for k in answers.CONTACT_KEYS:
        form.append((f"ans.contact.{k}", contact.get(k, "")))
    form.append(("ans.requires_sponsorship", sponsorship))
    return form


def test_contact_and_sponsorship_round_trip_and_stay_out_of_history(client, pdir, db_path, caplog):
    caplog.set_level(logging.DEBUG)
    r = post(client, "/prefs/save", form_with(pdir, CONTACT, "yes"))
    assert r.status_code == 303, r.text
    a = answers_in_file(pdir)
    assert a["contact"] == CONTACT
    assert a["requires_sponsorship"] is True
    assert "# synthetic test profile" in text_of(pdir)
    loaded = answers.load_answers(pdir)
    assert loaded.answers.contact.city == "canarycity" and not loaded.warnings
    # the form shows them back
    page = client.get("/prefs").text
    assert "canary.contact@invalid.test" in page and "Copy from my resume header" in page
    # not profile history, not a log line, not on the Profile
    c = db.connect(db_path)
    rows = c.execute("SELECT field_path, old_value, new_value FROM profile_change").fetchall()
    c.close()
    assert not any("canary" in " ".join(str(x) for x in row) or "contact" in row[0] for row in rows)
    assert "canary" not in caplog.text
    assert "contact" not in load_profile(pdir).model_dump()
    # clearing a box removes just that key
    cleared = {k: v for k, v in CONTACT.items() if k != "phone"}
    assert post(client, "/prefs/save", form_with(pdir, cleared, "yes")).status_code == 303
    assert "phone" not in answers_in_file(pdir)["contact"]


def test_a_bad_email_is_refused_with_nothing_written(client, pdir):
    before = text_of(pdir)
    r = post(client, "/prefs/save", form_with(pdir, {"email": "not an email"}))
    assert r.status_code == 422
    assert "does not look like an email" in r.text
    assert text_of(pdir) == before


def test_a_multi_line_contact_value_is_refused(client, pdir):
    before = text_of(pdir)
    r = post(client, "/prefs/save", form_with(pdir, {"city": "line one\nline two"}))
    assert r.status_code == 422 and text_of(pdir) == before


@pytest.mark.parametrize(
    "block",
    [
        "answers:\n  contact: just a string\n",
        "answers:\n  contact: [a, b]\n",
        "answers:\n  contact:\n    email: 5\n    phone: {x: 1}\n    city: [a]\n",
        "answers:\n  contact:\n    street_address: 1 Canary Way\n    postal_code: 00000\n",
        "answers:\n  requires_sponsorship: maybe\n  contact:\n    email: no-at-sign\n",
        "answers:\n  contact:\n    email: a@b\n    email: c@d\n",
    ],
)
def test_a_broken_contact_block_never_stops_scoring_or_loading(tmp_path, block):
    d = tmp_path / "p"
    d.mkdir()
    (d / "preferences.yaml").write_text(BASE + block, encoding="utf-8")
    (d / "resume.md").write_text("Synthetic Person\n", encoding="utf-8")
    profile = load_profile(d)  # the profile never reads answers:
    assert profile.load_warnings == ()
    loaded = answers.load_answers(d)  # never raises
    assert loaded.answers.contact.email in (None, "a@b")
    assert "canary" not in " ".join(loaded.warnings).casefold()


def test_a_bad_entry_drops_alone_and_a_never_store_key_is_refused(tmp_path):
    out = answers.answers_from_data(
        {
            "contact": {
                "first_name": "canaryfirst",
                "email": "nope",
                "street_address": "1 Canary Way",
            }
        }
    )
    assert out.answers.contact.first_name == "canaryfirst"
    assert out.answers.contact.email is None
    assert len(out.warnings) == 2
    assert out.refused and "street address" in out.refused[0]
    assert all("1 Canary Way" not in w and "nope" not in w for w in out.warnings)


def test_raw_yaml_save_refuses_a_never_store_key_in_the_contact_block(client, pdir):
    (pdir / "preferences.yaml").write_text("hard: [broken\n", encoding="utf-8")
    raw = BASE + "answers:\n  contact:\n    street_address: 1 Canary Way\n"
    r = post(
        client,
        "/prefs/raw",
        [("raw_yaml", raw), ("mtime_ns", str(preferences_mtime_ns(pdir)))],
    )
    assert r.status_code == 422 and "never-store list" in r.text
    assert text_of(pdir) == "hard: [broken\n"


# ─── Copy from my resume header ─────────────────────────────────────────────


def test_copy_from_header_proposes_and_never_saves(client, pdir):
    (pdir / "resume.md").write_text(
        "# Alex Q. Example\nDenver, CO 80202 | alex@example.com | (555) 010 0199\n\n"
        "## Experience\nSynthetic Corp\n",
        encoding="utf-8",
    )
    before = text_of(pdir)
    r = post(client, "/prefs/contact/from-resume", form_with(pdir))
    assert r.status_code == 200 and r.headers["HX-Retarget"] == "#sec-answers"
    body = r.text
    assert 'value="Alex"' in body and 'value="Example"' in body
    assert 'value="alex@example.com"' in body and 'value="(555) 010 0199"' in body
    assert 'value="Denver"' in body and 'value="CO"' in body
    assert "80202" not in body  # a postal code is never proposed
    assert "nothing is saved yet" in body
    assert text_of(pdir) == before  # proposes only
    assert "contact" not in (answers_in_file(pdir) or {})


def test_copy_from_header_keeps_what_you_typed_in_other_boxes(client, pdir):
    (pdir / "resume.md").write_text("Alex Example\nalex@example.com\n", encoding="utf-8")
    form = form_with(pdir, {"preferred_name": "canarypreferred"}, "yes")
    body = post(client, "/prefs/contact/from-resume", form).text
    assert 'value="canarypreferred"' in body and 'value="alex@example.com"' in body
    assert '<option value="yes" selected>' in body


def test_copy_from_header_with_nothing_recognizable_says_so(client, pdir):
    (pdir / "resume.md").write_text("## Experience\nSynthetic Corp\n", encoding="utf-8")
    body = post(client, "/prefs/contact/from-resume", form_with(pdir)).text
    assert "nothing recognizable" in body


# ─── The header parser ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Jane Public\nSpringfield, IL | jane@example.com | 555-010-0123\n\nSummary\nx\n",
            {
                "first_name": "Jane",
                "last_name": "Public",
                "city": "Springfield",
                "region": "IL",
                "email": "jane@example.com",
                "phone": "555-010-0123",
            },
        ),
        (
            "# Jane Q. Public, PhD\n\n**Email:** [jane@example.com](mailto:jane@example.com)  \n"
            "**Phone:** +1 555 010 0123\nPortland, OR 97201\n## Experience\nOther, TX\n",
            {
                "first_name": "Jane",
                "last_name": "Public",
                "email": "jane@example.com",
                "phone": "+1 555 010 0123",
                "city": "Portland",
                "region": "OR",
            },
        ),
        (
            "JANE PUBLIC JR.\njane@example.com · (555) 010 0123 · Berlin, BE, Germany\n",
            {
                "first_name": "JANE",
                "last_name": "PUBLIC",
                "email": "jane@example.com",
                "phone": "(555) 010 0123",
                "city": "Berlin",
                "region": "BE",
                "country": "Germany",
            },
        ),
        ("Jane Public\nhttps://example.com/jane\n", {"first_name": "Jane", "last_name": "Public"}),
        ("", {}),
        ("Summary\nSystems engineer with 10 years of experience.\n", {}),
    ],
)
def test_the_header_parser_handles_common_shapes(text, expected):
    assert propose_contact(text) == expected


def test_the_header_parser_reads_only_the_header():
    text = "Jane Public\n\n## Experience\nContact me at boss@example.com or 555-010-0123\n"
    assert propose_contact(text) == {"first_name": "Jane", "last_name": "Public"}


# ─── Sponsorship polarity ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("question", "polarity"),
    [
        ("Will you require visa sponsorship?", "require"),
        ("Do you now or in the future require sponsorship for employment visa status?", "require"),
        ("Will you need sponsorship to work for us?*", "require"),
        ("Is sponsorship required?", "require"),
        ("Can you work without sponsorship?", "without"),
        ("Are you authorized to work in the United States without visa sponsorship?", "without"),
        ("Able to work without requiring sponsorship", "without"),
        # ambiguous or not about sponsorship: no polarity
        ("Do you not require sponsorship?", None),
        ("Are you legally authorized to work in the US and not require sponsorship?", None),
        ("Are you authorized to work in the US and will you require sponsorship?", None),
        ("Do you currently hold a visa sponsored by another employer?", None),
        ("Visa sponsorship", None),
        ("Are you authorized to work in the United States?", None),
        ("", None),
    ],
)
def test_sponsorship_polarity_vectors(question, polarity):
    assert labels.sponsorship_polarity(question) == polarity


def test_sponsorship_offer_applies_the_polarity():
    yes = answers.Answers(requires_sponsorship=True)
    no = answers.Answers(requires_sponsorship=False)
    assert answers.sponsorship_offer(yes, "Will you require sponsorship?") == (
        answers.SponsorshipOffer(True, True)
    )
    assert answers.sponsorship_offer(yes, "Can you work without sponsorship?") == (
        answers.SponsorshipOffer(False, True)
    )
    assert answers.sponsorship_offer(no, "Can you work without sponsorship?") == (
        answers.SponsorshipOffer(True, True)
    )
    # ambiguous wording: suggested with the stored value, never ticked
    assert answers.sponsorship_offer(yes, "Visa sponsorship") == answers.SponsorshipOffer(
        True, False
    )
    assert answers.sponsorship_offer(answers.Answers(), "Will you require sponsorship?") is None
    assert answers.sponsorship_offer(yes, "Are you authorized to work here?") is None
