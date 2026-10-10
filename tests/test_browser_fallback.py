"""An unreachable browser hands its work to a responsive one (sandbox: browser 0 never reconnected after a hung page,
so the tender scraper and the submitter both failed). No real browser: the CDP call is faked."""
import asyncio
import unittest
from unittest import mock

from swarm import browser_agent as ba

URLS = ["http://127.0.0.1:9222", "http://127.0.0.1:9223", "http://127.0.0.1:9224"]


class Fallback(unittest.TestCase):
    def setUp(self):
        ba._UNRESPONSIVE.clear()
        self.calls = []

    def test_responsive_browser_keeps_its_tab(self):
        tab = ba.Tab(0)
        with mock.patch.object(ba.browsers, "cdp_urls", return_value=URLS), \
                mock.patch.object(ba, "_with_cdp", self._ok):
            self.assertIs(asyncio.run(ba.responsive_tab(tab)), tab)

    def test_dead_browser_moves_to_the_next_responsive_one_and_is_then_skipped(self):
        async def fake(browser, call):
            self.calls.append(browser)
            if browser == 0:
                await asyncio.sleep(5)
            return {}

        async def scenario():
            original = ba.browser_responds

            async def quick(browser, timeout=0.05):
                return await original(browser, timeout)
            with mock.patch.object(ba, "browser_responds", quick):
                first = await ba.responsive_tab(ba.Tab(0))
                second = await ba.responsive_tab(ba.main_tab(0))
            return first, second

        with mock.patch.object(ba.browsers, "cdp_urls", return_value=URLS), mock.patch.object(ba, "_with_cdp", fake):
            first, second = asyncio.run(scenario())
        self.assertEqual((first.browser, second.browser), (1, 1))
        self.assertFalse(first.main)                                 # a new tab, not browser 1's main tab
        self.assertEqual(self.calls.count(0), 1)                     # the dead browser is not probed again at once

    async def _ok(self, browser, call):
        return {}

    def test_locally_without_cdp_nothing_changes(self):
        tab = ba.Tab(0)
        with mock.patch.object(ba.browsers, "cdp_urls", return_value=[]):
            self.assertIs(asyncio.run(ba.responsive_tab(tab)), tab)


if __name__ == "__main__":
    unittest.main()
