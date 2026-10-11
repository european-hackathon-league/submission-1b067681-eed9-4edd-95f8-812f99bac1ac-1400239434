"""read_page loads a same-origin frame that had not loaded yet (sandbox: a mail's reading pane was saved as "frame not
loaded or not readable", so the analyst never saw the rates). Invented page, real Chromium."""
import asyncio
import http.server
import threading
import unittest

from swarm.page_text import EXTRACT_JS, FRAME_MISSING, LOAD_FRAMES_JS


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = (b"<html><body><p>Message body: rate 4,20 per kg</p></body></html>" if self.path.startswith("/msg") else
                b"<html><body><p>Inbox</p><div style='height:6000px'></div>"
                b"<iframe loading='lazy' title='Nachricht' src='/msg'></iframe></body></html>")
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)


class FrameReload(unittest.TestCase):
    def test_lazy_frame_is_loaded_and_read(self):
        from playwright.async_api import async_playwright
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()

        async def run():
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.goto(f"http://127.0.0.1:{server.server_port}/")
                before = await page.evaluate(EXTRACT_JS, {})
                loaded = await page.evaluate(LOAD_FRAMES_JS)
                after = await page.evaluate(EXTRACT_JS, {})
                await browser.close()
                return before, loaded, after

        before, loaded, after = asyncio.run(run())
        server.shutdown()
        self.assertIn(FRAME_MISSING, before)
        self.assertEqual(loaded, 1)
        self.assertIn("rate 4,20 per kg", after)


if __name__ == "__main__":
    unittest.main()
