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
        "submission": {"kind": "site"}, "expires_at": None}


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

    async def test_only_filter(self):
        order = []
        await self.run_with(make_agents(order), env={"SWARM_ONLY": "Collect,Submit"})
        self.assertEqual(order, ["collect", "submit"])

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


class RealAgentsImport(unittest.TestCase):
    def test_every_agent_module_imports_and_registers(self):
        names = {c.__name__ for c in orchestrator.discover()}
        self.assertTrue({"TenderScraper", "SpecScraper", "ErpScraper", "MailScraper", "BidSubmitter"} <= names)


if __name__ == "__main__":
    unittest.main()
