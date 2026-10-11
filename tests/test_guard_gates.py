"""The guard's access-check handling on a page with two identical checks (mail list + reading pane): every check is
unlocked by page order, unrelated checkboxes stay untouched, and a gate fill never presses a link. Invented page."""
import asyncio
import unittest

from swarm import guard as guard_module
from swarm.guard import SCAN_JS, UNLOCK_ALL_JS, PopupGuard, safe_then_click

GATE = """<section id="{id}"><label>Zum Entsperren ziehen <input type="range" min="0" max="100" value="0"></label>
<label><input type="checkbox"> Ich bin kein Roboter</label><p role="status">Gesperrt</p></section>
<script>(() => {{ const r = document.getElementById("{id}"), s = r.querySelector('[type=range]'),
  c = r.querySelector('[type=checkbox]'), st = r.querySelector('[role=status]');
  const u = () => {{ st.textContent = Number(s.value) === 100 && c.checked ? 'Entsperrt' : 'Gesperrt'; }};
  s.addEventListener('input', u); c.addEventListener('change', u); }})();</script>"""
PAGE = ("<html><body>" + GATE.format(id="list") + GATE.format(id="reader")
        + "<label><input type=checkbox id=terms> Bedingungen</label></body></html>")


class GuardGates(unittest.TestCase):
    def test_every_identical_check_is_unlocked_and_other_boxes_untouched(self):
        from playwright.async_api import async_playwright

        async def run():
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.set_content(PAGE)
                handled = await page.evaluate(UNLOCK_ALL_JS)
                statuses = await page.locator("[role=status]").all_inner_texts()
                terms = await page.locator("#terms").is_checked()
                again = await page.evaluate(UNLOCK_ALL_JS)            # idempotent: nothing toggled back
                after = await page.locator("[role=status]").all_inner_texts()
                await browser.close()
                return handled, statuses, terms, after, again

        handled, statuses, terms, after, again = asyncio.run(run())
        self.assertEqual(handled, 2)
        self.assertEqual(statuses, ["Entsperrt", "Entsperrt"])
        self.assertFalse(terms)
        self.assertEqual(after, ["Entsperrt", "Entsperrt"])

    def test_a_gate_fill_never_presses_a_link(self):
        item = {"buttons": [{"label": "Signatur-TAN · Anforderung 1", "away": False, "link": True},
                            {"label": "Weiter", "away": False, "link": False}]}
        self.assertEqual(safe_then_click({"then_click": "Signatur-TAN · Anforderung 1"}, item), "")
        self.assertEqual(safe_then_click({"then_click": "Weiter"}, item), "Weiter")


class LearnedUnlockedStatus(unittest.TestCase):
    """A gate that keeps its unlocked status but resets the slider (sandbox mail after each search) is not filled again
    once the guard has seen that status right after its own fill."""

    def test_reset_slider_with_unlocked_status_is_left_alone(self):
        from playwright.async_api import async_playwright

        async def run():
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                page = await browser.new_page()
                await page.set_content("<html><body>" + GATE.format(id="list") + "</body></html>")
                evaluate = lambda js, *a: page.evaluate(js, *a)  # noqa: E731
                guard = PopupGuard(None, "test")
                first = await evaluate(SCAN_JS, {"unlocked": guard._unlocked_all()})
                gate = [i for i in first["items"] if i["kind"] == "gate"][0]
                await evaluate(UNLOCK_ALL_JS)
                await guard._learn_unlocked(evaluate, "mail.example", gate)
                await page.evaluate("document.querySelector('[type=range]').value = 0")   # the site resets it
                later = await evaluate(SCAN_JS, {"unlocked": guard._unlocked_all()})
                unknown = await evaluate(SCAN_JS, {"unlocked": []})
                await browser.close()
                return gate, later, unknown

        guard_module._UNLOCKED.clear()
        gate, later, unknown = asyncio.run(run())
        guard_module._UNLOCKED.clear()
        self.assertEqual(gate["status"], "Gesperrt")
        self.assertEqual([i for i in later["items"] if i["kind"] == "gate"], [])
        self.assertEqual(len([i for i in unknown["items"] if i["kind"] == "gate"]), 1)   # not learned: still a gate


if __name__ == "__main__":
    unittest.main()
