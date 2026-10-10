"""Browsers for agents. In a run: CDP browsers 1-4 from the sandbox (never launch your own). Locally: one Chromium."""
import browsers


class BrowserPool:
    def __init__(self, playwright):
        self._pw = playwright
        self._contexts: dict[int, object] = {}   # slot -> browser context

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
