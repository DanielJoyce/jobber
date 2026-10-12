"""A page on another origin cannot frame the console."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

pytestmark = pytest.mark.e2e


def test_cross_origin_iframe_of_the_console_is_blocked(page, server):
    # The framing page is a real second origin (another port) on loopback: Chromium refuses
    # public-to-loopback frames by itself, which would hide a missing header.
    body = f'<!doctype html><iframe id="f" src="{server.url}/"></iframe>'.encode()

    class Attacker(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Attacker)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        page.goto(f"http://127.0.0.1:{httpd.server_port}/")
        page.wait_for_timeout(1500)
    finally:
        httpd.shutdown()
    frames = [f for f in page.frames if f != page.main_frame]
    assert frames, "the iframe should exist"
    for f in frames:
        assert f.url.startswith("chrome-error:"), f.url
        assert f.locator("nav").count() == 0
