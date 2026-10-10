"""Local HTML to PDF through headless Chromium (the optional ``browser`` extra).

Used only to print a page jobhunter built itself (a packet's resume or cover letter), never to
fetch anything: every request the page makes is aborted, so the render cannot touch the
network. Callers in ``jobhunter.apply`` take this as an injected function, because the
architecture test keeps ``playwright`` out of that package.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


class PdfUnavailable(RuntimeError):
    """PDF rendering is not possible here; ``str()`` says what to do, for the page."""


INSTALL_HINT = (
    "PDF export needs the browser extra: run `uv sync --extra browser` and "
    "`uv run playwright install chromium`, then restart the console. "
    "Meanwhile, open the HTML file and print it to PDF from your browser."
)


def render_pdf(html: str, out: Path) -> None:
    """Print ``html`` to ``out`` (US Letter). Raises :class:`PdfUnavailable`.

    Runs on a thread of its own: Playwright's sync API refuses a thread that has an asyncio
    loop running, and the caller may be one.
    """
    with ThreadPoolExecutor(max_workers=1) as one:
        one.submit(_render, html, out).result()


def _render(html: str, out: Path) -> None:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise PdfUnavailable(INSTALL_HINT) from exc
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                context = browser.new_context(java_script_enabled=False)
                context.route("**/*", lambda route: route.abort())
                page = context.new_page()
                page.set_content(html, wait_until="load")
                out.parent.mkdir(parents=True, exist_ok=True)
                page.pdf(
                    path=str(out),
                    format="Letter",
                    print_background=True,
                    margin={"top": "0.7in", "bottom": "0.7in", "left": "0.8in", "right": "0.8in"},
                )
            finally:
                browser.close()
    except PlaywrightError as exc:
        text = str(exc).strip()
        first = text.splitlines()[0] if text else "browser error"
        raise PdfUnavailable(f"PDF rendering failed ({first}). {INSTALL_HINT}") from exc
