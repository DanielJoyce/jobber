"""The no-fabrication checker (specs/017 "No-fabrication rule"): table tests.

Each case starts from a resume line set and one generated item, and asserts the item passes
or fails with the expected reason. A faithful restatement must pass, so a checker that flagged
everything would fail these tests too.
"""

from __future__ import annotations

import pytest

from jobhunter.apply import factcheck
from jobhunter.apply.documents import confirm_key

LINES = {
    "L1": "Synthetic Person",
    "L2": "<you>@example.com | Denver, CO",
    "L3": "Experience",
    "L4": "Platform Engineer, Acme Synthetic Corp",
    "L5": "2019 - 2023",
    "L6": "- Contributed to the migration of 40 services to Kubernetes",
    "L7": "- Wrote Terraform modules used by 3 teams",
    "L8": "- Cut deploy time by 35%",
    "L9": "Skills: Python, Go, Terraform, Kubernetes, PostgreSQL",
    "L10": "- Managed the on-call rotation for a team of 6 engineers",
}
POSTING = (
    "Synthetic Widgets is hiring a Senior Platform Engineer.\n"
    "You will build our Kubernetes platform and own the deploy pipeline.\n"
    "We value calm operators who write things down."
)
EMPLOYER = "Synthetic Widgets Inc"


def resume_doc(*, summary=None, bullets=(), skills=(), entry=None, header=("L1", "L2")):
    e = entry or {
        "source_line": "L4",
        "employer": "Acme Synthetic Corp",
        "title": "Platform Engineer",
        "dates": "2019 - 2023",
    }
    return {
        "kind": "resume",
        "resume": {
            "header": list(header),
            "summary": summary or {"text": "", "sources": []},
            "sections": [
                {
                    "heading": "Experience",
                    "entries": [{**e, "bullets": [{"text": t, "sources": s} for t, s in bullets]}],
                }
            ],
            "skills": [{"name": n, "sources": s} for n, s in skills],
            "omitted": [],
            "change_notes": [],
        },
        "context": {"lines": LINES},
    }


def letter_doc(paragraphs, notes=None):
    lines = dict(LINES)
    lines.update(notes or {})
    return {
        "kind": "cover_letter",
        "cover_letter": {
            "paragraphs": [
                {"text": t, "resume_sources": s, "posting_quotes": q} for t, s, q in paragraphs
            ]
        },
        "context": {"lines": lines},
    }


def draft_doc(sentences, stories=None):
    lines = dict(LINES)
    lines.update(stories or {})
    return {
        "kind": "question_draft",
        "draft": {
            "sentences": [{"text": t, "sources": s, "posting_quotes": q} for t, s, q in sentences]
        },
        "context": {"lines": lines},
    }


def run(doc, confirmed=()):
    return factcheck.check(doc, posting=POSTING, employer=EMPLOYER, confirmed=confirmed)


def bullet_reasons(text, sources):
    rep = run(resume_doc(bullets=[(text, sources)]))
    (item,) = [i for i in rep["items"] if i["role"] == "bullet"]
    return item["reasons"]


def test_faithful_resume_passes():
    rep = run(
        resume_doc(
            summary={
                "text": "Platform engineer who moves services to Kubernetes.",
                "sources": ["L4", "L6"],
            },
            bullets=[
                ("Contributed to moving 40 services onto Kubernetes", ["L6"]),
                ("Cut deploy time by 35%", ["L8"]),
                ("Wrote Terraform modules adopted by 3 teams", ["L7"]),
            ],
            skills=[("Python", ["L9"]), ("k8s", ["L9"]), ("Postgres", ["L9"])],
        )
    )
    assert [i["reasons"] for i in rep["items"] if i["reasons"]] == []
    assert rep["ok"] is True


@pytest.mark.parametrize(
    ("text", "sources", "reason"),
    [
        ("Moved 40 services to Kubernetes", [], "cites no source line"),
        ("Moved 40 services to Kubernetes", ["L99"], "L99, which does not exist"),
        ("Moved 40 services to Kubernetes", ["N1"], "does not exist"),
        ("Migrated 45 services to Kubernetes", ["L6"], "number 45"),
        ("Cut deploy time by 50%", ["L8"], "number 50%"),
        ("Saved $2M in hosting costs", ["L8"], "number $2M"),
        (
            "Led the migration of 40 services to Kubernetes",
            ["L6"],
            "'led' claims more (leadership)",
        ),
        ("Architected the migration of 40 services", ["L6"], "(scope)"),
        ("Expert in moving 40 services to Kubernetes", ["L6"], "(superlative)"),
        ("Wrote Terraform modules with a team of 5", ["L7"], "team size"),
        ("Wrote Terraform modules as a certified AWS architect", ["L7"], "credential"),
        ("Wrote Terraform and Ansible modules", ["L7"], "'ansible' is not anywhere"),
        ("Ran 40 services on Kubernetes and OpenShift", ["L6"], "'openshift'"),
    ],
)
def test_invented_or_inflated_bullets_fail(text, sources, reason):
    reasons = bullet_reasons(text, sources)
    assert any(reason in r for r in reasons), reasons


@pytest.mark.parametrize(
    "text",
    [
        "Contributed to the migration of forty services to Kubernetes",  # forty is not mapped
        "Migrated 40+ services to Kubernetes",
        "Helped move 40 services to Kubernetes so teams could go faster",  # "go" in prose
        "Managed on-call for a team of 6 engineers",
    ],
)
def test_fair_restatements_pass(text):
    sources = ["L10"] if "on-call" in text else ["L6"]
    reasons = bullet_reasons(text, sources)
    if "forty" in text:
        # A spelled-out number the table does not know is simply not compared.
        assert not any("number" in r for r in reasons)
    else:
        assert reasons == []


def test_leadership_claim_supported_by_cited_line_passes():
    assert bullet_reasons("Managed the on-call rotation for 6 engineers", ["L10"]) == []


def test_changed_entry_fields_fail_the_structure_check():
    rep = run(
        resume_doc(
            entry={
                "source_line": "L4",
                "employer": "Globex",
                "title": "Senior Platform Engineer",
                "dates": "2018 - 2023",
            }
        )
    )
    (entry,) = [i for i in rep["items"] if i["role"] == "entry"]
    text = " ".join(entry["reasons"])
    assert "employer 'Globex'" in text and "title" in text and "dates" in text


def test_entry_fields_on_the_next_lines_pass():
    rep = run(resume_doc())
    (entry,) = [i for i in rep["items"] if i["role"] == "entry"]
    assert entry["reasons"] == []


def test_skill_not_in_resume_fails_and_synonyms_pass():
    rep = run(resume_doc(skills=[("Golang", ["L9"]), ("Rust", ["L9"]), ("Python and Go", ["L9"])]))
    by = {i["text"]: i["reasons"] for i in rep["items"] if i["role"] == "skill"}
    assert by["Golang"] == []
    assert by["Python and Go"] == []
    assert by["Rust"] == ["'Rust' is not anywhere in your resume"]


def test_resume_may_not_cite_notes_and_header_must_exist():
    doc = resume_doc(header=("L1", "L77"), bullets=[("Wrote Terraform modules", ["L7", "N1"])])
    doc["context"]["lines"] = {**LINES, "N1": "I like their docs"}
    rep = run(doc)
    reasons = {i["role"]: i["reasons"] for i in rep["items"]}
    assert any("L77" in r for r in reasons["header"])
    assert any("may cite only L" in r for r in reasons["bullet"])


# ─── letters and drafts: employer claims and quotes ─────────────────────────


def test_employer_sentence_without_a_quote_fails_even_with_notes():
    notes = {"N1": "Synthetic Widgets' docs taught me a lot."}
    rep = run(
        letter_doc([("Synthetic Widgets has the best docs in the industry.", ["N1"], [])], notes)
    )
    (p,) = rep["items"]
    assert any("about the employer" in r for r in p["reasons"])
    assert any("(superlative)" in r for r in p["reasons"])


def test_employer_sentence_with_a_verified_quote_passes():
    quote = "build our Kubernetes platform"
    rep = run(
        letter_doc(
            [
                (
                    f"You want someone to {quote}, and I moved 40 services to Kubernetes.",
                    ["L6"],
                    [quote],
                )
            ]
        )
    )
    assert rep["items"][0]["reasons"] == []


def test_you_sentence_with_quote_not_in_posting_fails_both_ways():
    quote = "build our quantum platform"
    rep = run(letter_doc([(f"You want someone to {quote}.", ["L6"], [quote])]))
    reasons = rep["items"][0]["reasons"]
    assert any("is not in the posting" in r for r in reasons)
    assert any("about the employer" in r for r in reasons)


def test_quote_not_used_in_the_sentence_does_not_cover_it():
    rep = run(
        letter_doc(
            [("Your team is famous.", ["L6"], ["We value calm operators who write things down."])]
        )
    )
    assert any("about the employer" in r for r in rep["items"][0]["reasons"])


def test_behavioral_sentence_without_story_or_resume_source_fails():
    stories = {"S1": "The cluster upgrade failed at 2am and I rolled it back."}
    rep = run(
        draft_doc(
            [
                ("When an upgrade failed at 2am, I rolled it back.", ["S1"], []),
                ("I then rewrote the whole runbook.", [], []),
            ],
            stories,
        )
    )
    a, b = rep["items"]
    assert a["reasons"] == []
    assert b["reasons"] == ["cites no source line"]
    assert rep["ok"] is False


# ─── confirmations ──────────────────────────────────────────────────────────


def test_confirmed_item_counts_and_unrelated_confirmations_are_dropped():
    text = "Led the migration of 40 services to Kubernetes"
    doc = resume_doc(bullets=[(text, ["L6"])])
    assert run(doc)["ok"] is False
    rep = run(doc, confirmed=[confirm_key(text), confirm_key("something else")])
    (b,) = [i for i in rep["items"] if i["role"] == "bullet"]
    assert b["status"] == "confirmed" and b["reasons"]
    assert rep["ok"] is True
    assert rep["confirmed"] == [confirm_key(text)]


def test_an_edit_to_a_confirmed_line_is_rechecked():
    old = "Led the migration of 40 services to Kubernetes"
    new = "Led the migration of 40 services to Kubernetes, fast"
    rep = run(resume_doc(bullets=[(new, ["L6"])]), confirmed=[confirm_key(old)])
    (b,) = [i for i in rep["items"] if i["role"] == "bullet"]
    assert b["status"] == "unsupported"


def test_base_resume_report_is_ok():
    rep = factcheck.check(
        {"kind": "resume", "base": True, "context": {"lines": LINES}},
        posting=POSTING,
        employer=EMPLOYER,
    )
    assert rep["ok"] is True and rep["items"] == []
