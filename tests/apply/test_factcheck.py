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


# ─── review round: scenarios the first checker let through ──────────────────

TENURE = {"L20": "Acme Corp - Software Engineer, Platform Architecture team - 2019-2024"}


def reasons_with(lines, text, sources, role_doc="resume"):
    extra = dict(LINES)
    extra.update(lines)
    doc = resume_doc(bullets=[(text, sources)])
    doc["context"]["lines"] = extra
    rep = run(doc)
    (b,) = [i for i in rep["items"] if i["role"] == "bullet"]
    return b["reasons"]


@pytest.mark.parametrize(
    "text",
    [
        "Platform engineer with 15+ years of experience",
        "Ran 2000+ hours of on-call",
        "Over a decade of platform experience",
        "Saved millions in hosting",
        "Cut infrastructure costs by half",
    ],
)
def test_n_plus_and_vague_quantities_need_the_same_quantity_cited(text):
    assert bullet_reasons_any(TENURE, text, ["L20"])


def bullet_reasons_any(lines, text, sources):
    return reasons_with(lines, text, sources)


def test_n_plus_passes_with_the_same_unit_and_dollar_needs_dollars():
    assert reasons_with({}, "Moved 40+ services to Kubernetes", ["L6"]) == []
    assert reasons_with({}, "Moved 30+ services to Kubernetes", ["L6"]) == []
    lines = {"L21": "- Served 2M users"}
    assert any("$2M" in r for r in reasons_with(lines, "Saved $2M for 2M users", ["L21"]))


def test_phone_digits_never_support_a_number():
    lines = {"L22": "Phone 555-867-5309 | Platform Engineer"}
    assert any("5309" in r for r in reasons_with(lines, "Handled 5309 tickets", ["L22"]))


@pytest.mark.parametrize("verb", ["Leads", "Manages", "Owns", "Drives", "Directs", "Oversees"])
def test_present_tense_leadership_is_a_stronger_claim(verb):
    lines = {"L23": "- Contributed to the database migration from MySQL to PostgreSQL"}
    reasons = reasons_with(lines, f"{verb} the database migration to PostgreSQL", ["L23"])
    assert any("(leadership)" in r for r in reasons), reasons


@pytest.mark.parametrize(
    "summary", ["Senior platform engineer.", "Principal platform engineer.", "Engineering manager."]
)
def test_seniority_in_a_resume_line_needs_a_cited_line(summary):
    rep = run(resume_doc(summary={"text": summary, "sources": ["L4"]}))
    (s,) = [i for i in rep["items"] if i["role"] == "summary"]
    assert any("(seniority)" in r for r in s["reasons"])


@pytest.mark.parametrize(
    ("text", "quotes"),
    [
        (
            "I was thrilled to hear Synthetic Widgets just raised its Series C.",
            ["Synthetic Widgets"],
        ),
        ("Your team pioneered serverless databases.", ["You"]),
        ("Your award-winning platform team has inspired me for years.", ["a"]),
        ("The company is the best place to build.", ["is hiring a Senior"]),
    ],
)
def test_employer_claims_need_a_meaningful_quote(text, quotes):
    rep = run(letter_doc([(text, ["L6"], quotes)]))
    assert any("about the employer" in r for r in rep["items"][0]["reasons"])


def test_short_employer_names_are_detected():
    rep = factcheck.check(
        letter_doc([("3M builds things I admire.", ["L6"], [])]),
        posting=POSTING,
        employer="3M",
    )
    assert any("about the employer" in r for r in rep["items"][0]["reasons"])


@pytest.mark.parametrize(
    ("text", "word"),
    [
        ("Built data pipelines in Airflow and dbt for reporting", "airflow"),
        ("Trained models with PyTorch on the fleet", "PyTorch"),
        ("Formerly at Google, moved 40 services to Kubernetes", "Google"),
    ],
)
def test_names_outside_the_lines_are_flagged_in_prose(text, word):
    reasons = reasons_with({}, text, ["L6"])
    assert any(word.lower() in r.lower() for r in reasons), reasons


def test_a_name_from_the_posting_says_so():
    posting = POSTING + " Experience with Snowflakeish ETL is a plus."
    doc = resume_doc(bullets=[("Moved 40 services to Kubernetes and Snowflakeish", ["L6"])])
    rep = factcheck.check(doc, posting=posting, employer=EMPLOYER)
    (b,) = [i for i in rep["items"] if i["role"] == "bullet"]
    assert any("comes from the posting" in r for r in b["reasons"])


def test_the_job_title_and_employer_may_be_named_in_a_letter():
    quote = "build our Kubernetes platform"
    text = f"As a Platform Engineer at Synthetic Widgets you want someone to {quote}."
    rep = factcheck.check(
        letter_doc([(text, ["L6"], [quote])]),
        posting=POSTING,
        employer=EMPLOYER,
        title="Senior Platform Engineer",
    )
    assert rep["items"][0]["reasons"] == []


def test_a_heading_cannot_carry_a_claim():
    doc = resume_doc()
    doc["resume"]["sections"][0]["heading"] = (
        "Certifications: AWS Certified Solutions Architect, CISSP, Active TS/SCI clearance"
    )
    rep = run(doc)
    (h,) = [i for i in rep["items"] if i["role"] == "heading"]
    assert h["status"] == "unsupported" and rep["ok"] is False
    doc["resume"]["sections"][0]["heading"] = "Work Experience"
    assert next(i for i in run(doc)["items"] if i["role"] == "heading")["reasons"] == []


def test_entry_fields_never_come_from_the_next_job_or_inside_a_word():
    lines = dict(LINES)
    lines.update(
        {
            "L30": "Engineer, Foo Corp, 2010 - 2012",
            "L31": "Senior Staff Engineer, Bar Corp, 2012 - 2015",
            "L32": "Software Engineer, Platform Architecture team",
        }
    )

    def entry_reasons(e):
        doc = resume_doc(entry={"employer": "", "dates": "", **e})
        doc["resume"]["sections"][0]["entries"].append(
            {"source_line": "L31", "employer": "Bar Corp", "title": "", "dates": "", "bullets": []}
        )
        doc["context"]["lines"] = lines
        rep = run(doc)
        return next(i for i in rep["items"] if i["role"] == "entry")["reasons"]

    assert entry_reasons({"source_line": "L30", "title": "Senior Staff Engineer"})
    assert entry_reasons({"source_line": "L30", "title": "Engineer", "employer": "Bar Corp"})
    assert entry_reasons({"source_line": "L32", "title": "Architect"})
    assert entry_reasons({"source_line": "L30", "title": "Engineer", "employer": "Foo Corp"}) == []


@pytest.mark.parametrize(
    "text",
    [
        "Engineer with an M.S. in Computer Science",
        "CompTIA Security+ holder",
        "Holds a Public Trust",
    ],
)
def test_credential_notations(text):
    lines = {"L40": "B.S. Computer Science, State University, 2010"}
    assert any("credential" in r for r in reasons_with(lines, text, ["L40"]))


def test_go_as_a_skill_needs_the_language_not_go_live():
    lines = dict(LINES)
    lines["L9"] = "- handled go-live for the billing service"
    doc = resume_doc(skills=[("Go", ["L9"])])
    doc["context"]["lines"] = lines
    (sk,) = [i for i in run(doc)["items"] if i["role"] == "skill"]
    assert sk["reasons"] == ["'Go' is not anywhere in your resume"]


def test_confirming_one_line_leaves_the_others_unsupported():
    a, b = "Led the migration of 40 services to Kubernetes", "Expert in Terraform modules"
    rep = run(resume_doc(bullets=[(a, ["L6"]), (b, ["L7"])]), confirmed=[confirm_key(a)])
    statuses = {i["text"]: i["status"] for i in rep["items"] if i["role"] == "bullet"}
    assert statuses == {a: "confirmed", b: "unsupported"}
    assert rep["ok"] is False and rep["confirmed"] == [confirm_key(a)]


# ─── re-review: verbatim lines must pass, ordinary nouns are not claims ─────


def test_a_verbatim_line_with_dotted_names_passes():
    line = "- Built Node.js and Vue.js services on ASP.NET and AWS for U.S. customers (e.g. banks)"
    lines = {"L50": line, "L51": "Skills: AWS"}
    assert reasons_with(lines, line[2:], ["L50"]) == []


@pytest.mark.parametrize(
    ("text", "resume"),
    [
        ("Ran Postgres and K8s on AWS", "- Ran PostgreSQL and Kubernetes on Amazon Web Services"),
        ("Wrote services in Golang", "- Wrote services in Go"),
        ("Ran 40 services on Amazon EKS", "- Ran 40 services on EKS"),
    ],
)
def test_synonyms_and_vendor_prefixes_of_resume_names_pass(text, resume):
    assert reasons_with({"L52": resume}, text, ["L52"]) == []


@pytest.mark.parametrize(
    ("text", "cited"),
    [
        ("Administered 200+ Linux servers", "- Administered 200 RHEL servers and VMware clusters"),
        ("Managed Terraform for 30+ accounts", "- Managed Terraform modules for 30+ AWS accounts"),
        ("Ran 24/7 on-call for the APIs", "- Ran on-call for the APIs"),
    ],
)
def test_n_plus_with_words_between_and_24_7_pass(text, cited):
    lines = {"L53": cited, "L54": "Skills: Linux, RHEL, Terraform, AWS"}
    assert not any("number" in r for r in reasons_with(lines, text, ["L53"]))


@pytest.mark.parametrize(
    "text",
    [
        "Cut p99 latency to 200 ms on MS SQL",
        "Ran staff meetings for the platform group",
        "Wrote an executive summary of the migration",
        "Packaged tools for the apt package manager",
    ],
)
def test_ordinary_nouns_are_not_credentials_or_seniority(text):
    lines = {"L55": f"- {text}"}
    reasons = reasons_with(lines, text, ["L55"])
    assert not any("credential" in r or "seniority" in r for r in reasons), reasons


def test_seniority_still_needs_a_title_context_to_be_cited():
    lines = {"L56": "- Platform engineer on the payments team"}
    reasons = reasons_with(lines, "Staff platform engineer on the payments team", ["L56"])
    assert any("(seniority)" in r for r in reasons)


def test_at_and_t_is_a_name_not_the_word_at():
    rep = factcheck.check(
        letter_doc([("I moved 40 services at night to Kubernetes.", ["L6"], [])]),
        posting=POSTING,
        employer="AT&T",
    )
    assert rep["items"][0]["reasons"] == []
    rep = factcheck.check(
        letter_doc([("AT&T is a great place to work.", ["L6"], [])]),
        posting=POSTING,
        employer="AT&T",
    )
    assert any("about the employer" in r for r in rep["items"][0]["reasons"])


@pytest.mark.parametrize(
    "heading", ["Experience Highlights", "Career History", "Core Competencies"]
)
def test_title_case_headings_are_not_names(heading):
    doc = resume_doc()
    doc["resume"]["sections"][0]["heading"] = heading
    (h,) = [i for i in run(doc)["items"] if i["role"] == "heading"]
    assert h["reasons"] == []


def test_an_empty_section_is_neither_rendered_nor_checked():
    from jobhunter.apply.documents import render_md

    doc = resume_doc()
    doc["resume"]["sections"].append({"heading": "Certifications: CISSP", "entries": []})
    rep = run(doc)
    assert all("CISSP" not in i["text"] for i in rep["items"])
    assert "CISSP" not in render_md(doc)
