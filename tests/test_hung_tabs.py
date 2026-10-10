"""close_hung_tabs against a real Chromium: a tab blocked by a request that never answers is closed, a working tab is
kept (sandbox: one hung tab made every browser-use connect to that browser time out)."""
import asyncio
import http.server
import json
import socket
import threading
import unittest
import urllib.request
from unittest import mock

from swarm import browser_agent as ba


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.startswith("/hang"):
            threading.Event().wait(120)      # never answers
            return
        body = (b"<html><body>ok<script>if (location.search === '?block') { const x = new XMLHttpRequest(); "
                b"x.open('GET', '/hang', false); x.send(); }</script></body></html>")
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class HungTabs(unittest.TestCase):
    def test_hung_tab_is_closed_and_working_tab_kept(self):
        from playwright.async_api import async_playwright

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base, port = f"http://127.0.0.1:{server.server_port}", free_port()

        async def run():
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True, args=[f"--remote-debugging-port={port}"])
                context = await browser.new_context()
                good, bad = await context.new_page(), await context.new_page()
                await good.goto(base + "/")
                await bad.goto(base + "/")
                await bad.evaluate(f"setTimeout(() => location.href = '{base}/?block', 10)")
                await asyncio.sleep(1.5)
                with mock.patch.object(ba.browsers, "cdp_urls", return_value=[f"http://127.0.0.1:{port}"]), \
                        mock.patch.object(ba, "TAB_PROBE_S", 3):
                    closed = await ba.close_hung_tabs(0)
                await asyncio.sleep(1)
                listed = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5))
                urls = sorted(t["url"] for t in listed if t["type"] == "page" and t["url"].startswith(base))
                await browser.close()
                return closed, urls

        closed, urls = asyncio.run(run())
        server.shutdown()
        self.assertEqual(len(closed), 1)
        self.assertEqual(urls, [base + "/"])             # the working tab is still there, the hung one gone


if __name__ == "__main__":
    unittest.main()
