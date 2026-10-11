"""The swarm framework against fakes: blackboard, JSON parsing, phase orchestration, failure isolation."""
import asyncio
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from swarm import Agent, Blackboard, Evidence, Phase  # noqa: E402
from swarm import orchestrator  # noqa: E402
from swarm.llm import parse_json  # noqa: E402

TASK = {"brief": "b", "access": [{"siteId": "erp", "url": "http://x", "credentials": {}}],
        "submission": {"kind": "api"}, "expires_at": None}


class BoardTests(unittest.IsolatedAsyncioTestCase):
    async def test_post_query_and_wait(self):
        board = Blackboard()
        waiter = asyncio.create_task(board.wait_for(kind="a.b", timeout=2))
        await asyncio.sleep(0)
        await board.post(Evidence("a.b", "erp", {"x": 1}, source="u"))
        self.assertEqual((await waiter)[0].data, {"x": 1})
        self.assertEqual(len(board.query("a", prefix=True)), 1)
        self.assertEqual(board.query("a.b", site="mail"), [])

    async def test_dump_and_load_round_trip(self):
        board = Blackboard()
        await board.post(Evidence("k", "erp", {"x": 1}))
        await board.set("r", [1])
        path = Path(__file__).parent / "_board.json"
        try:
            board.dump(path)
            again = Blackboard.load(path)
        finally:
            path.unlink(missing_ok=True)
        self.assertEqual(again.evidence[0].data, {"x": 1})
        self.assertEqual(again.get("r"), [1])


class ParseJson(unittest.TestCase):
    def test_fenced_and_chatty(self):
        self.assertEqual(parse_json('Sure!\n```json\n{"a": [1, 2]}\n```'), {"a": [1, 2]})

    def test_garbage_raises(self):
        with self.assertRaises(ValueError):
            parse_json("no json here")


def make_agents(order):
    class Boom(Agent):
        phase = Phase.COLLECT
        async def run(self): raise RuntimeError("boom")

    class Collect(Agent):
        phase = Phase.COLLECT
        async def run(self):
            order.append("collect")
            await self.ctx.board.post(Evidence("k", "erp", {}))

    class Analyze(Agent):
        phase = Phase.ANALYZE
        async def run(self):
            order.append(f"analyze sees {len(self.ctx.board.query('k'))}")

    class Submit(Agent):
        phase = Phase.SUBMIT
        async def run(self):
            order.append("submit")
            await self.ctx.board.set("answer", {"ok": True})

    return [Submit, Analyze, Boom, Collect]


class Orchestration(unittest.IsolatedAsyncioTestCase):
    async def run_with(self, classes, task=TASK, env=None):
        with patch.object(orchestrator, "discover", lambda: classes):
            return await orchestrator.run_swarm(task, None, scored=True, env=env or {})

    async def test_phases_run_in_order_and_a_failure_does_not_stop_the_rest(self):
        order = []
        answer = await self.run_with(make_agents(order))
        self.assertEqual(order, ["collect", "analyze sees 1", "submit"])
        self.assertEqual(answer, {"ok": True})

    async def test_site_submission_requires_receipt(self):
        task = {**TASK, "submission": {"kind": "site"}}
        with self.assertRaisesRegex(RuntimeError, "No confirmed portal receipt"):
            await self.run_with(make_agents([]), task=task)
        class Confirmed(Agent):
            phase = Phase.SUBMIT
            async def run(self):
                await self.ctx.board.set("submission", {"accepted": True, "reference": "fixture"})
        self.assertIsNone(await self.run_with([Confirmed], task=task))

    async def test_scored_run_ignores_local_filter(self):
        order = []
        await self.run_with(make_agents(order), env={"SWARM_ONLY": "Collect,Submit"})
        self.assertEqual(order, ["collect", "analyze sees 1", "submit"])

    async def test_plain_code_phases_run_even_out_of_time(self):
        order = []

        class Compute(Agent):
            phase = Phase.COMPUTE
            async def run(self): order.append("compute")

        late = {**TASK, "expires_at": (time.time() + 100) * 1000}
        await self.run_with(make_agents(order) + [Compute], task=late)
        self.assertEqual(order, ["compute", "submit"])

    async def test_out_of_time_skips_work_but_still_submits(self):
        order = []
        late = {**TASK, "expires_at": (time.time() + 100) * 1000}   # inside the submit margin
        answer = await self.run_with(make_agents(order), task=late)
        self.assertEqual(order, ["submit"])
        self.assertEqual(answer, {"ok": True})


class BrowserUseWiring(unittest.TestCase):
    def test_only_task_sites_are_allowed_and_telemetry_is_off(self):
        import os
        from swarm import browser_agent
        task = {"access": [{"url": "https://a.example/x?y=1"}, {"url": "http://b.example:8080/"}, {"url": ""}]}
        self.assertEqual(browser_agent.allowed_domains(task), ["https://a.example", "http://b.example:8080"])
        for flag in ("ANONYMIZED_TELEMETRY", "BROWSER_USE_CLOUD_SYNC", "BROWSER_USE_VERSION_CHECK"):
            self.assertEqual(os.environ[flag], "false")


class NoExternalHosts(unittest.TestCase):
    def test_about_blank_animation_loads_nothing(self):
        from browser_use.browser.watchdogs.aboutblank_watchdog import AboutBlankWatchdog
        from swarm import browser_agent
        self.assertIs(AboutBlankWatchdog._show_dvd_screensaver_loading_animation_cdp,
                      browser_agent._no_loading_animation)
        self.assertTrue(issubclass(browser_agent.TextSession, browser_agent.RobustSession))


class BrowseTools(unittest.TestCase):
    def test_our_actions_are_registered_and_helpers_are_not(self):
        from swarm import browser_agent
        tools = browser_agent.build_tools(None, "erp", "t", None)
        names = set(tools.registry.registry.actions)
        self.assertTrue({"record", "read_page", "fill_form", "share_tip"} <= names)
        self.assertNotIn("read_one", names)

    def test_unused_browser_use_actions_are_excluded(self):
        from swarm import browser_agent
        names = set(browser_agent.build_tools(None, "erp", "t", None).registry.registry.actions)
        self.assertFalse(set(browser_agent.EXCLUDED_ACTIONS) & names)
        self.assertTrue({"navigate", "click", "input", "done"} <= names)

    def test_flash_prompt_formats(self):
        from swarm import browser_agent
        text = browser_agent.FLASH_SYSTEM.format(max_actions=4)
        self.assertIn("Max 4 actions", text)
        self.assertIn('{"memory"', text)


class Tabs(unittest.TestCase):
    def tearDown(self):
        from swarm import browser_agent
        browser_agent._OWNERS.clear()
        browser_agent._OPENERS.clear()

    def test_agent_sees_own_tab_and_its_popups_never_other_agents_tabs(self):
        from types import SimpleNamespace as NS
        from swarm import browser_agent
        main, mine, other = browser_agent.Tab(0, main=True), browser_agent.Tab(0), browser_agent.Tab(0)
        main.claim("t-main")
        mine.claim("t-mine")
        other.claim("t-other")
        browser_agent._OPENERS.update({"t-my-popup": "t-mine", "t-its-popup": "t-other"})
        ids = ("t-stray", "t-its-popup", "t-other", "t-my-popup", "t-main", "t-mine")
        manager = NS(_targets={t: NS(target_id=t, target_type="page") for t in ids}, browser_session=NS(_tab=mine))
        seen = lambda tab: [t.target_id for t in browser_agent._visible_page_targets(  # noqa: E731
            NS(_targets=manager._targets, browser_session=NS(_tab=tab)))]
        self.assertEqual(seen(mine), ["t-mine", "t-my-popup"])
        self.assertEqual(seen(other), ["t-other", "t-its-popup"])
        self.assertEqual(seen(main), ["t-main", "t-stray"])      # a tab nobody opened: only the main tab sees it
        self.assertEqual(len(seen(None)), 6)                     # sessions without a tab see everything

    def test_focused_popup_is_adopted_and_closed_with_the_tab(self):
        from swarm import browser_agent
        mine, other = browser_agent.Tab(0), browser_agent.Tab(0)
        mine.claim("t-mine")
        other.claim("t-other")
        mine.adopt("t-new")
        mine.adopt("t-other")                                    # never another agent's tab
        self.assertIs(browser_agent.owner_of("t-new"), mine)
        self.assertIs(browser_agent.owner_of("t-other"), other)
        self.assertEqual(mine.popups, {"t-new"})


class FakeLLM:
    def __init__(self, reply):
        self.reply, self.calls = reply, 0

    async def ask_json(self, system, user, **kwargs):
        self.calls += 1
        await asyncio.sleep(0.01)
        return self.reply


class Guard(unittest.IsolatedAsyncioTestCase):
    def tearDown(self):
        from swarm import guard
        guard._DECISIONS.clear()
        guard._PENDING.clear()

    def test_signature_ignores_numbers(self):
        from swarm.guard import signature
        a = {"kind": "overlay", "text": "Wartung am 12.03. Code 4711", "buttons": [{"label": "OK"}], "fields": []}
        b = dict(a, text="Wartung am 14.05. Code 0815")
        self.assertEqual(signature(a), signature(b))

    def test_careful_mode_only_clicks_noise_without_inputs(self):
        from swarm.guard import allowed
        item = {"buttons": [{"i": 0, "label": "x", "away": False}, {"i": 1, "label": "go", "away": True}], "fields": []}
        self.assertTrue(allowed({"action": "click", "button": 0, "kind": "survey"}, item, careful=True))
        from swarm.guard import pick_button
        # the model picked the link away ("accept and continue" decoy): the item's one staying button (x) instead
        self.assertEqual(pick_button({"action": "click", "button": 1, "kind": "notice"}, item), 0)
        two = dict(item, buttons=item["buttons"] + [{"i": 2, "label": "ok", "away": False}])
        self.assertIsNone(pick_button({"action": "click", "button": 1, "kind": "notice"}, two))
        self.assertFalse(allowed({"action": "click", "button": 1, "kind": "notice"}, two, careful=False))
        self.assertEqual(pick_button({"action": "click", "button": 1, "kind": "error"}, two), 1)   # retry link
        self.assertFalse(allowed({"action": "click", "button": 0, "kind": "content"}, item, careful=True))
        self.assertFalse(allowed({"action": "fill", "fields": [{"field": "a", "value": "1"}]}, item, careful=True))
        self.assertFalse(allowed({"action": "click", "button": 0, "kind": "notice"},
                                 dict(item, fields=[{"type": "text"}]), careful=True))

    async def test_one_model_call_per_new_item_across_agents_then_cached_clicks(self):
        from types import SimpleNamespace as NS
        from swarm.guard import PopupGuard
        llm = FakeLLM({"items": [{"id": 0, "action": "click", "button": 0, "kind": "notice"}]})
        ctx = NS(llm=llm)
        scan = {"url": "https://erp.example/x", "items": [
            {"id": 0, "kind": "overlay", "text": "Wartungshinweis", "buttons": [{"i": 0, "label": "OK", "away": False}],
             "fields": []}]}
        clicks = []

        async def evaluate(js, *args):
            if args:
                clicks.append(args[0])
                return "clicked"
            return scan
        from swarm import guard
        a, b = PopupGuard(ctx, "a"), PopupGuard(ctx, "b")
        await asyncio.gather(a.sweep(evaluate), b.sweep(evaluate))   # never waits for the model
        self.assertEqual(clicks, [])
        await asyncio.gather(*guard._TASKS)
        await asyncio.gather(a.sweep(evaluate), b.sweep(evaluate), PopupGuard(ctx, "c").sweep(evaluate))
        self.assertEqual(llm.calls, 1)
        self.assertEqual(len(clicks), 3)


class Coordinator(unittest.TestCase):
    def board(self):
        board = Blackboard()
        board.results.update({
            "brief": {"tender_reference": "T-1", "scenario_date": "2027-01-15T00:00:00Z"},
            "tender": {"positions": [{"position": "1", "requirement_ref": "A"}, {"position": "2", "requirement_ref": "B"}],
                       "clarifications": [{"number": "1", "positions": ["1"], "keys": {"packaging_tariff": "P-1"}}]},
            "requirements": {"A": {"bore": "50", "kataloggruppe": "G1"}, "B": {"bore": "60", "kataloggruppe": "G2"}},
            "materials": {"1": {"material": "M1", "unverified": [], "reason": "exact active match"},
                          "2": {"material": None, "reason": "no exact match among the ERP records read"}},
            "erp": {"materials": {"M1": {"material": "M1", "lead_time_production_weeks": "5"}},
                    "conditions": [{"material": "M1", "valid_from": "2027-01-01", "valid_to": "2027-12-31",
                                    "scales": []}], "stock": {}},
            "mail": {}, "discount": {"percent": "0", "source": "no valid approval found"}})
        return board

    def test_gaps_name_exactly_what_is_missing_on_ready_sites(self):
        from agents.coordinate import find_gaps
        board = self.board()
        ids = {g["id"] for g in find_gaps(board, {"tender", "spec", "erp", "mail"}, {"tender": ["bidder q&a"]})}
        self.assertEqual(ids, {"erp record M1", "erp prices M1", "erp stock M1", "erp search G2",
                               "mail packaging_tariffs P-1", "mail discount"})
        only_erp = {g["role"] for g in find_gaps(board, {"erp"}, {})}
        self.assertEqual(only_erp, {"erp"})


class MailTopics(unittest.TestCase):
    def test_only_topics_without_a_valid_message_are_searched(self):
        from types import SimpleNamespace as NS
        from agents.scrape_mail import MailScraper
        scraper = MailScraper(NS(board=None))
        facts = {"authorized_senders": {"discount_approval": "boss", "packaging_tariffs": "log"},
                 "topic_words": {"packaging_tariffs": "Verpackung", "fx_fixing": "Devisen"}}
        jobs = scraper.topic_jobs(facts, {"discount_approval"})
        self.assertEqual(len(jobs), 2)
        self.assertEqual(sum("Verpackung" in j for j in jobs), 1)
        self.assertEqual(sum("Devisen" in j for j in jobs), 1)

    def test_discount_first_and_delivery_last_with_its_identifiers(self):
        from types import SimpleNamespace as NS
        from agents.scrape_mail import MailScraper
        scraper = MailScraper(NS(board=None))
        facts = {"director_sender": "boss@", "authorized_senders": {"delivery_commitments": "dispo",
                                                                   "price_indices": "einkauf"}}
        topics, senders, words = scraper.topics(facts)
        self.assertEqual(topics, ["discount_approval", "price_indices", "delivery_commitments"])
        topics, _, _ = scraper.topics({"authorized_senders": {"x_unknown": "a", "delivery_commitments": "b",
                                                              "fx_fixing": "c", "technical_release": "d"}})
        self.assertEqual(topics, ["discount_approval", "technical_release", "fx_fixing", "x_unknown",
                                  "delivery_commitments"])
        job = scraper.topic_job("delivery_commitments", words, senders, ["D-1"], ["M-9"])
        self.assertIn("D-1", job)
        self.assertIn("M-9", job)

    def test_delivery_counts_as_covered_only_when_every_key_has_its_mail(self):
        from types import SimpleNamespace as NS
        from agents.scrape_mail import MailScraper
        board = {"tender": {"positions": [{"position": "1"}, {"position": "2"}],
                            "clarifications": [{"number": "1", "positions": ["1"], "keys": {"delivery_key": "D-1"}},
                                               {"number": "2", "positions": ["2"], "keys": {"delivery_key": "D-2"}}]},
                 "mail": {"delivery_commitments": {"D-1": {}}}}
        scraper = MailScraper(NS(board=NS(get=board.get)))
        valid = [{"topic": "delivery_commitments"}]
        self.assertNotIn("delivery_commitments", scraper.covered(valid))
        board["mail"]["delivery_commitments"]["D-2"] = {}
        self.assertIn("delivery_commitments", scraper.covered(valid))


class CoordinatorFallback(unittest.TestCase):
    def test_direct_jobs_group_three_gaps_per_job_and_role(self):
        from agents.coordinate import direct_jobs
        gaps = [{"id": f"g{i}", "role": "mail" if i < 4 else "erp", "need": f"need {i}"} for i in range(5)]
        jobs = direct_jobs(gaps)
        self.assertEqual([(j["role"], j["gaps"]) for j in jobs],
                         [("mail", ["g0", "g1", "g2"]), ("mail", ["g3"]), ("erp", ["g4"])])
        self.assertIn("need 0", jobs[0]["job"])


class RealAgentsImport(unittest.TestCase):
    def test_every_agent_module_imports_and_registers(self):
        names = {c.__name__ for c in orchestrator.discover()}
        self.assertTrue({"TenderScraper", "SpecScraper", "ErpScraper", "MailScraper", "BidSubmitter"} <= names)


if __name__ == "__main__":
    unittest.main()
