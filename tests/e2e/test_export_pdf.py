"""The real PDF renderer (headless Chromium, the browser extra): a tiny local render."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from jobhunter.apply import export
from jobhunter.core import pdf

pytestmark = pytest.mark.e2e


def test_render_pdf_makes_a_pdf_and_never_touches_the_network(tmp_path):
    hits: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        md = "Synthetic Person\nx@example.com\n\n## Skills\n\nGo\n"
        html = export.to_html(md, "resume", title="t")
        beacon = f'<img src="http://127.0.0.1:{srv.server_port}/beacon.png">'
        out = tmp_path / "sub" / "r.pdf"
        pdf.render_pdf(html.replace("</body>", beacon + "</body>"), out)
    finally:
        srv.shutdown()
    data = out.read_bytes()
    assert data.startswith(b"%PDF-") and len(data) > 1000
    assert hits == []
