"""Recovery that preserves work: a hung-tab check never closes another agent's active tab (real Chromium), a hung
tab of the caller is renewed in place, and a browse that crashes resumes with what is already saved."""
import asyncio
import http.server
import json
import threading
import unittest
import urllib.request
from unittest import mock

from swarm import browser_agent as ba
from swarm.blackboard import Blackboard, Evidence
from swarm.context import Context
from tests.test_hung_tabs import Handler, free_port


class HungTabsOfOthers(unittest.TestCase):
    def test_only_own_unowned_and_idle_tabs_are_closed(self):
        from playwright.async_api import async_playwright

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base, port = f"http://127.0.0.1:{server.server_port}", free_port()

        async def run():
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True, args=[f"--remote-debugging-port={port}"])
                context = await browser.new_context()
                pages = [await context.new_page() for _ in range(3)]
                for page in pages:
                    await page.goto(base + "/")
                listed = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5))
                ids = {t["url"] + str(i): t["id"] for i, t in enumerate(t for t in listed if t["type"] == "page")}
                targets = [t["id"] for t in listed if t["type"] == "page" and t["url"].startswith(base)]
                for page in pages:                     # every page hangs (a request that never answers)
                    await page.evaluate(f"setTimeout(() => location.href = '{base}/?block', 10)")
                await asyncio.sleep(1.5)
                other, mine, idle = ba.Tab(0), ba.Tab(0), ba.Tab(0)
                other.claim(targets[0])
                mine.claim(targets[1])
                idle.claim(targets[2])
                ba._ACTIVE.update({other, mine})       # `other` is another agent's tab with a browse running
                try:
                    with mock.patch.object(ba.browsers, "cdp_urls", return_value=[f"http://127.0.0.1:{port}"]), \
                            mock.patch.object(ba, "TAB_PROBE_S", 3):
                        closed = await ba.close_hung_tabs(0, mine=mine)
                finally:
                    ba._ACTIVE.difference_update({other, mine})
                    for t in (other, mine, idle):
                        ba._OWNERS.pop(t.target_id, None)
                await browser.close()
                return closed, targets, ids

        closed, targets, _ = asyncio.run(run())
        server.shutdown()
        self.assertNotIn(targets[0], closed)          # another agent's active tab: never touched
        self.assertIn(targets[1], closed)             # the caller's own hung tab
        self.assertIn(targets[2], closed)             # an owned tab nobody is browsing in


class RenewInPlace(unittest.TestCase):
    def test_hung_own_tab_is_renewed_and_main_tab_stays_main(self):
        tab = ba.main_tab(3)
        tab.claim("dead-target")
        tab.suspect = True

        async def responds(browser, timeout=12):
            return True

        async def hung(browser, keep=None, mine=None):
            return ["dead-target"]
        with mock.patch.object(ba.browsers, "cdp_urls", return_value=["http://127.0.0.1:1"]), \
                mock.patch.object(ba, "browser_responds", responds), mock.patch.object(ba, "close_hung_tabs", hung):
            again = asyncio.run(ba.responsive_tab(tab))
        self.assertIs(again, tab)
        self.assertIs(ba.main_tab(3), tab)
        self.assertTrue(tab.renewed and tab.target_id is None and not tab.used)
        self.assertNotIn("dead-target", ba._OWNERS)
        ba._MAIN_TABS.pop(3, None)

    def test_second_hang_on_a_browser_moves_the_work_to_another(self):
        tab = ba.Tab(0)
        ba._HUNG_AT.clear()

        async def responds(browser, timeout=12):
            return True

        async def hung(browser, keep=None, mine=None):
            return [mine.target_id] if mine and mine.target_id else []
        with mock.patch.object(ba.browsers, "cdp_urls", return_value=["a", "b", "c", "d"]), \
                mock.patch.object(ba, "browser_responds", responds), mock.patch.object(ba, "close_hung_tabs", hung):
            tab.claim("first")
            tab.suspect = True
            self.assertIs(asyncio.run(ba.responsive_tab(tab)), tab)          # first hang: renewed in place
            tab.claim("second")
            tab.suspect = True
            moved = asyncio.run(ba.responsive_tab(tab))                       # second hang: another browser
        self.assertIsNot(moved, tab)
        self.assertNotEqual(moved.browser, 0)
        self.assertIs(tab.replacement, moved)
        ba._HUNG_AT.clear()
        for t in ("first", "second"):
            ba._OWNERS.pop(t, None)

    def test_healthy_recently_checked_browser_is_not_probed_again(self):
        calls = []

        async def responds(browser, timeout=12):
            calls.append(browser)
            return True

        async def none(browser, keep=None, mine=None):
            return []
        with mock.patch.object(ba.browsers, "cdp_urls", return_value=["http://127.0.0.1:1"]), \
                mock.patch.object(ba, "browser_responds", responds), mock.patch.object(ba, "close_hung_tabs", none):
            ba._CHECKED.pop(0, None)
            asyncio.run(ba.responsive_tab(ba.Tab(0)))
            asyncio.run(ba.responsive_tab(ba.Tab(0)))
            suspect = ba.Tab(0)
            suspect.suspect = True
            asyncio.run(ba.responsive_tab(suspect))
        self.assertEqual(calls, [0, 0])               # second healthy check skipped; a suspect tab is checked
        ba._CHECKED.pop(0, None)


class CrashResume(unittest.IsolatedAsyncioTestCase):
    async def test_crashed_browse_is_retried_with_what_is_saved(self):
        from swarm import scraper as sc

        class Fixture(sc.SiteScraper):
            role, browser, site_id = "spec", 1, "s"

            def goal(self):
                return "collect"
        ctx = Context(task={"brief": "", "access": [{"siteId": "s", "url": "https://s.example", "credentials": {}}]},
                      board=Blackboard(), llm=None, pool=None)
        jobs = []

        async def fake_browse(ctx_, prompt, **kwargs):
            jobs.append(prompt)
            if len(jobs) == 1:
                await kwargs["on_record"]({"kind": "page", "data": {"topic": "spec revisions", "text": "x"},
                                           "source": "u", "note": ""})
                raise RuntimeError("CDP connection lost")
            return {"summary": "requirements saved", "done": True, "records": [], "steps": 3}
        with mock.patch.object(sc, "browse", fake_browse):
            out = await Fixture(ctx).browse_until_done("read the requirement table", attempts=3)
        self.assertTrue(out["done"])
        self.assertEqual(len(jobs), 2)
        self.assertIn("Already saved: ['spec revisions']", jobs[1])      # resumes; does not start over
        self.assertEqual(len(ctx.board.query("spec.page")), 1)            # the first attempt's page survived


class RunClosedBySubmission(unittest.IsolatedAsyncioTestCase):
    async def test_finished_token_after_signing_counts_as_accepted_and_stops_retries(self):
        from agents import submit_bid
        from swarm import timeline
        timeline.reset()
        ctx = Context(task={"brief": "Submit.", "submission": {"kind": "site"}, "access": [
            {"siteId": "t", "url": "https://t.example"}, {"siteId": "m", "url": "https://m.example"}]},
            board=Blackboard(), llm=None, pool=None)
        ctx.board.results.update({"sites": {"tender": "t", "spec": "t", "erp": "t", "mail": "m"},
                                  "brief": {"tender_reference": "FIXTURE"}})
        attempts = []

        async def fake_browse(ctx_, goal, **kwargs):
            if kwargs["author"].endswith(".mailbox"):
                return {"summary": "", "done": True, "steps": 1}
            attempts.append(1)
            timeline.mark("TAN requested")
            ba.note_run_closed(ctx, RuntimeError("Error code: 401 - unknown, finished or expired run token"))
            return {"summary": "", "done": False, "steps": 4}
        with mock.patch.object(submit_bid, "browse", fake_browse):
            await submit_bid.BidSubmitter(ctx).run()
        self.assertEqual(len(attempts), 1)
        self.assertTrue(ctx.board.get("submission")["accepted"])
        self.assertIn("accepted submission", timeline.current().milestones)


if __name__ == "__main__":
    unittest.main()
