"""Browsers for agents. In a run: CDP browsers 1-4 from the sandbox (never launch your own). Locally: one Chromium."""
import browsers
import os
import json
import socket


class BrowserPool:
    def __init__(self, playwright):
        self._pw = playwright
        self._contexts: dict[int, object] = {}   # slot -> browser context
        self._local = []

    async def prepare(self):
        """Give local runs the same persistent CDP/tab lifecycle as sandbox runs."""
        if self._pw is None or browsers.cdp_urls():
            return
        urls = []
        for _ in range(4):
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            browser = await self._pw.chromium.launch(headless=os.environ.get("HEADLESS") == "1",
                                                    args=[f"--remote-debugging-port={port}"])
            self._local.append(browser)
            urls.append(f"http://127.0.0.1:{port}")
        os.environ["CDP_URLS"] = json.dumps(urls)

    async def close(self):
        if self._local:
            for browser in self._local:
                await browser.close()
            os.environ.pop("CDP_URLS", None)
            self._local.clear()

    async def context(self, index: int = 0):
        n = len(browsers.cdp_urls())
        slot = index % n if n else 0             # locally everything shares one Chromium
        if slot not in self._contexts:
            _, page = await browsers.connect(self._pw, slot)
            self._contexts[slot] = page.context
        return self._contexts[slot]

    async def new_page(self, index: int = 0):
        """A fresh tab in browser `index`; tabs are unlimited, browsers are not (max 4)."""
        return await (await self.context(index)).new_page()
