"""Export (specs/017 phase 1c): files, names, formats, the PDF-less path, and the page.

PDF rendering is injected (a fake that writes a tiny PDF and records the HTML it was given);
the real Chromium render is checked in ``tests/e2e``. The CLI runner is the conftest fake.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import test_packet_documents as base

from jobhunter.apply import export
from jobhunter.core import pdf

# The shared fixture, re-exported under the same name so pytest finds it here.
env = base.env
ENTAIL, LETTER_OUT, RESUME_OUT = base.ENTAIL, base.LETTER_OUT, base.RESUME_OUT
docs, generate, parse = base.docs, base.generate, base.parse

FAKE_PDF = b"%PDF-1.4\n% synthetic\n"


class FakeRenderer:
    def __init__(self):
        self.html: list[str] = []

    def __call__(self, html: str, out: Path) -> None:
        self.html.append(html)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(FAKE_PDF)


@pytest.fixture
def renderer(env):
    r = FakeRenderer()
    env.app.state.packet_pdf_renderer = r
    return r


def pdir(env) -> Path:
    return env.settings.paths.data_dir / "packets" / str(env.pid)


def export_kind(env, kind="resume"):
    return env.client.post(f"/packet/{env.pid}/export", data={"kind": kind})


def generated_and_confirmed(env, fake_claude, claude_stream):
    fake_claude.set([claude_stream(RESUME_OUT), claude_stream(ENTAIL)])
    generate(env)
    page = env.client.get(f"/packet/{env.pid}").text
    (btn,) = [b for b in parse(page).buttons if b.get("name") == "ckey"]
    doc_id = docs(env)[-1]["id"]
    env.client.post(f"/packet/{env.pid}/doc/{doc_id}/confirm", data={"ckey": btn["value"]})


# ─── names, text, html ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("line", "want"),
    [
        ("Synthetic Person", "Synthetic-Person"),
        ("Jane Q. Public", "Jane-Public"),
        ("Mary-Ann O'Neil", "Mary-Ann-ONeil"),
        ("José Núñez", "José-Núñez"),
        ("Cher", "Cher"),
        ("<you>@example.com | Denver, CO", ""),
        ("Platform Engineer, Acme", ""),
        ("Resume 2026", ""),
        ("../../etc passwd", ""),
        ("", ""),
    ],
)
def test_person_name(line, want):
    assert export.person_name(line) == want


def test_plain_text_drops_markup_and_keeps_every_line():
    md = "Synthetic Person\n\n## Summary\n\nHello there.\n\n### Eng, Acme (2019)\n\n- one\n- two\n"
    text = export.to_text(md)
    assert "SUMMARY" in text and "## " not in text and "### " not in text
    assert "Eng, Acme (2019)" in text and "- one\n- two" in text


def test_html_escapes_and_has_no_external_references():
    html = export.to_html(
        "Name <b>\ncontact | x\n\n## Skills\n\n<script>alert(1)</script>, Go\n",
        "resume",
        title="t",
    )
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "<h1>Name &lt;b&gt;</h1>" in html
    assert "http://" not in html and "https://" not in html and "src=" not in html


# ─── export of a generated resume ───────────────────────────────────────────


def test_unconfirmed_lines_block_export_and_nothing_is_written(
    env,
    fake_claude,
    claude_stream,
    renderer,
):
    fake_claude.set([claude_stream(RESUME_OUT), claude_stream(ENTAIL)])
    generate(env)
    page = env.client.get(f"/packet/{env.pid}").text
    assert 'id="export-refusal-resume"' in page and 'id="export-btn-resume"' not in page
    r = export_kind(env)
    assert r.status_code == 409 and "unsupported" in r.text
    assert not pdir(env).exists() and renderer.html == []


def test_export_writes_every_format_named_for_the_employer(
    env,
    fake_claude,
    claude_stream,
    renderer,
):
    generated_and_confirmed(env, fake_claude, claude_stream)
    r = export_kind(env)
    assert r.status_code == 200 and 'id="export-msg"' in r.text and "as PDF, text" in r.text
    d = pdir(env)
    assert d == env.settings.paths.data_dir / "packets" / str(env.pid)  # data dir, not the repo
    named = {p.name for p in d.iterdir()}
    assert {
        "Synthetic-Person-Resume.pdf",
        "Synthetic-Person-Resume.txt",
        "Synthetic-Person-Resume.md",
        "Synthetic-Person-Resume.html",
        "resume-v1.pdf",
        "resume-v1.txt",
        "resume-v1.md",
        "resume-v1.html",
    } == named
    body_md = docs(env)[0]["body_md"]
    assert (d / "Synthetic-Person-Resume.md").read_text(encoding="utf-8") == body_md
    assert (d / "Synthetic-Person-Resume.pdf").read_bytes() == FAKE_PDF
    # The renderer was handed the same page the .html file holds.
    assert renderer.html == [(d / "Synthetic-Person-Resume.html").read_text(encoding="utf-8")]
    # Recorded relative to the data dir, so moving it does not strand the pointer.
    assert docs(env)[0]["rendered_path"] == f"packets/{env.pid}/resume-v1.pdf"


def test_exported_text_has_the_versions_lines_and_the_header_as_is(
    env,
    fake_claude,
    claude_stream,
    renderer,
):
    generated_and_confirmed(env, fake_claude, claude_stream)
    export_kind(env)
    text = (pdir(env) / "Synthetic-Person-Resume.txt").read_text(encoding="utf-8")
    lines = text.splitlines()
    # Header lines come from the resume exactly as it has them.
    assert lines[:2] == ["Synthetic Person", "<you>@example.com | Denver, CO"]
    for bullet in (
        "- Contributed to moving 40 services onto Kubernetes",
        "- Led the Terraform work for 3 teams",
    ):
        assert bullet in lines
    assert "EXPERIENCE" in lines and "Platform Engineer, Acme Synthetic Corp (2019 - 2023)" in lines
    assert "Terraform" in text.split("SKILLS")[1]
    # And the Markdown file is the stored body, so an edit shows up in both.
    md = (pdir(env) / "Synthetic-Person-Resume.md").read_text(encoding="utf-8")
    assert "- Led the Terraform work for 3 teams" in md and "## Experience" in md


def test_html_is_one_page_with_the_header_and_each_bullet(
    env,
    fake_claude,
    claude_stream,
    renderer,
):
    generated_and_confirmed(env, fake_claude, claude_stream)
    export_kind(env)
    html = (pdir(env) / "Synthetic-Person-Resume.html").read_text(encoding="utf-8")
    assert "<h1>Synthetic Person</h1>" in html
    assert "&lt;you&gt;@example.com | Denver, CO" in html
    assert html.count("<li>") == 2 and "<h2>Experience</h2>" in html


def test_nothing_but_the_document_is_exported(
    env,
    fake_claude,
    claude_stream,
    renderer,
):
    """Saved answers, drafts, notes and the check report never reach a file."""
    env.client.post(f"/packet/{env.pid}/base")
    env.client.post(f"/packet/{env.pid}/notes", data={"notes": "Their docs taught me Terraform."})
    env.conn.execute(
        "INSERT INTO packet_answer (packet_id, field_key, label, value, source) "
        "VALUES (?, 'q:why us', 'Why us', 'SECRET-ANSWER-TEXT', 'user')",
        (env.pid,),
    )
    env.conn.commit()
    export_kind(env)
    for f in pdir(env).iterdir():
        if f.suffix in (".txt", ".md", ".html"):
            body = f.read_text(encoding="utf-8")
            assert "SECRET-ANSWER-TEXT" not in body and "taught me Terraform" not in body
            assert "check_report" not in body and "unsupported" not in body


def test_base_resume_exports_as_its_own_text(env, renderer):
    env.client.post(f"/packet/{env.pid}/base")
    assert export_kind(env).status_code == 200
    d = pdir(env)
    md = (d / "Synthetic-Person-Resume.md").read_text(encoding="utf-8")
    assert md.startswith("Synthetic Person\n<you>@example.com | Denver, CO\n")
    assert "- Wrote Terraform modules used by 3 teams" in md
    html = (d / "Synthetic-Person-Resume.html").read_text(encoding="utf-8")
    assert "<h1>Synthetic Person</h1>" in html and "Wrote Terraform modules" in html


# ─── without a PDF renderer ─────────────────────────────────────────────────


def test_without_a_pdf_renderer_text_markdown_and_html_still_export_with_a_clear_message(
    env,
    monkeypatch,
):
    env.client.post(f"/packet/{env.pid}/base")

    def unavailable(html, out):
        raise pdf.PdfUnavailable(pdf.INSTALL_HINT)

    env.app.state.packet_pdf_renderer = unavailable
    # A PDF left by an earlier export must not survive as if it were current.
    d = pdir(env)
    d.mkdir(parents=True)
    (d / "Synthetic-Person-Resume.pdf").write_bytes(b"old")
    r = export_kind(env)
    assert r.status_code == 200
    assert "No PDF: PDF export needs the browser extra" in r.text.replace("&#39;", "'")
    assert "uv sync --extra browser" in r.text and "print it to PDF" in r.text
    assert (d / "Synthetic-Person-Resume.txt").is_file()
    assert (d / "Synthetic-Person-Resume.md").is_file()
    assert (d / "Synthetic-Person-Resume.html").is_file()
    assert not (d / "Synthetic-Person-Resume.pdf").exists()
    assert docs(env)[0]["rendered_path"] is None
    assert 'id="dl-resume-pdf"' not in r.text and 'id="dl-resume-html"' in r.text


def test_a_crashing_renderer_does_not_lose_the_other_formats(env):
    env.client.post(f"/packet/{env.pid}/base")

    def boom(html, out):
        raise OSError("disk on fire")

    env.app.state.packet_pdf_renderer = boom
    r = export_kind(env)
    assert r.status_code == 200 and "disk on fire" in r.text
    assert (pdir(env) / "Synthetic-Person-Resume.txt").is_file()


def test_render_pdf_reports_a_missing_playwright_cleanly(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)  # import raises ImportError
    with pytest.raises(pdf.PdfUnavailable, match="browser extra"):
        pdf.render_pdf("<p>x</p>", tmp_path / "x.pdf")
    assert not (tmp_path / "x.pdf").exists()


# ─── cover letter, versions, downloads ──────────────────────────────────────


def test_cover_letter_exports_under_the_resumes_name_without_a_header(
    env,
    fake_claude,
    claude_stream,
    renderer,
):
    env.client.post(f"/packet/{env.pid}/base")
    fake_claude.set([claude_stream(LETTER_OUT), claude_stream({"lines": []})])
    generate(env, kind="cover_letter")
    assert export_kind(env, "cover_letter").status_code == 200
    d = pdir(env)
    assert (d / "Synthetic-Person-Cover-Letter.pdf").is_file()
    assert (d / "cover-letter-v1.md").is_file()
    text = (d / "Synthetic-Person-Cover-Letter.txt").read_text(encoding="utf-8")
    assert (
        text
        == "You want someone to build our Kubernetes platform; I moved 40 services to Kubernetes.\n"
    )
    html = (d / "Synthetic-Person-Cover-Letter.html").read_text(encoding="utf-8")
    assert "<h1>" not in html
    assert not (d / "Synthetic-Person-Resume.txt").exists()  # the resume was not asked for


def test_a_new_version_is_not_shown_as_exported_until_it_is(
    env,
    fake_claude,
    claude_stream,
    renderer,
):
    env.client.post(f"/packet/{env.pid}/base")
    export_kind(env)
    page = env.client.get(f"/packet/{env.pid}").text
    assert 'id="dl-resume-pdf"' in page
    generated_and_confirmed(env, fake_claude, claude_stream)  # v2 is now current
    page = env.client.get(f"/packet/{env.pid}").text
    assert 'id="dl-resume-pdf"' not in page and "PDF: not exported yet" in page
    assert env.client.get(f"/packet/{env.pid}/export/resume/pdf").status_code == 404
    export_kind(env)
    assert (pdir(env) / "resume-v1.pdf").is_file()  # the older version's files are kept
    assert (pdir(env) / "resume-v2.pdf").is_file()
    body = env.client.get(f"/packet/{env.pid}/export/resume/md").text
    assert "Led the Terraform work" in body


def test_downloads_have_the_employer_name_no_store_and_a_locked_down_html(
    env,
    renderer,
):
    env.client.post(f"/packet/{env.pid}/base")
    export_kind(env)
    base = f"/packet/{env.pid}/export/resume"
    want = {
        "pdf": ("application/pdf", "attachment"),
        "txt": ("text/plain", "attachment"),
        "md": ("text/markdown", "attachment"),
        "html": ("text/html", "inline"),
    }
    for fmt, (mime, disp) in want.items():
        r = env.client.get(f"{base}/{fmt}")
        assert r.status_code == 200, fmt
        assert r.headers["content-type"].startswith(mime)
        assert r.headers["content-disposition"].startswith(disp)
        assert f"Synthetic-Person-Resume.{fmt}" in r.headers["content-disposition"]
        assert r.headers["cache-control"] == "no-store"
    html = env.client.get(f"{base}/html")
    assert "default-src 'none'" in html.headers["content-security-policy"]
    assert env.client.get(f"{base}/pdf").content == FAKE_PDF


def test_unknown_or_missing_downloads_are_404(env, renderer):
    base = f"/packet/{env.pid}/export"
    assert env.client.get(f"{base}/resume/pdf").status_code == 404  # no resume yet
    env.client.post(f"/packet/{env.pid}/base")
    assert env.client.get(f"{base}/resume/pdf").status_code == 404  # not exported yet
    export_kind(env)
    assert env.client.get(f"{base}/resume/docx").status_code == 404  # no .docx
    assert env.client.get(f"{base}/letter/pdf").status_code == 404
    assert env.client.get(f"{base}/cover-letter/pdf").status_code == 404  # no letter
    assert env.client.get(f"{base}/..%2f..%2fx/pdf").status_code == 404
    assert env.client.post("/packet/9999/export", data={"kind": "resume"}).status_code == 404
    assert export_kind(env, "../../x").status_code == 409


def test_export_with_no_document_says_so(env, renderer):
    r = export_kind(env)
    assert r.status_code == 409 and "no such document" in r.text.lower()
    page = env.client.get(f"/packet/{env.pid}").text
    assert 'id="export-none"' in page


# ─── page hygiene ───────────────────────────────────────────────────────────


def test_cross_site_export_is_refused(env, renderer):
    env.client.post(f"/packet/{env.pid}/base")
    r = env.client.post(
        f"/packet/{env.pid}/export",
        data={"kind": "resume"},
        headers={
            "Origin": "https://evil.example",
            "Sec-Fetch-Site": "cross-site",
            "Host": "127.0.0.1:8808",
        },
    )
    assert r.status_code == 403
    assert renderer.html == [] and not pdir(env).exists()


def test_export_section_copy_button_links_and_forms_resolve(env, renderer):
    env.client.post(f"/packet/{env.pid}/base")
    export_kind(env)
    page = env.client.get(f"/packet/{env.pid}")
    assert page.headers["cache-control"] == "no-store"
    parsed = parse(page.text)
    assert 'id="copy-resume"' in page.text and 'class="act copy-btn"' in page.text
    assert "Wrote Terraform modules used by 3 teams" in page.text
    links = {h for h in parsed.links if h.startswith("/")}
    downloads = {h for h in links if "/export/" in h}
    assert len(downloads) == 4
    for href in sorted(links):
        assert env.client.get(href.split("#")[0]).status_code == 200, href
    for f in parsed.forms.values():
        assert f["__action"].startswith("/"), f  # same-origin posts only
    assert f"/packet/{env.pid}/export" in {f["__action"] for f in parsed.forms.values()}
