"""What the bid needs, and how evidence turns into it. Invented values only.

- every omitted position / estimated field names its open dependency, and gap detection sees it (missing price unit)
- price condition fragments merge only when they belong together; conflicts and unrelated records stay apart
- delivery commitments, packaging, bonus: verified vs. justified absence vs. estimated
- ERP scheduling finishes positions first and never waits for extraction; draft bid; live submission reserve
"""
import asyncio
import unittest
from unittest import mock

from agents.analyze_materials import erp_view, reconcile_conditions
from agents.compute_bid import compute
from swarm import timeline
from swarm.blackboard import Blackboard
from swarm.context import Context


def inputs(**over) -> dict:
    base = {
        "brief": {"tender_reference": "T-1", "scenario_date": "2031-02-01T09:00:00Z"},
        "tender": {"positions": [{"position": "1", "unit": "Stk", "requirement_ref": "Z"}],
                   "clarifications": [{"number": "1", "date": "2031-01-01", "positions": ["1"], "keys": {}}]},
        "quantities": {"1": {"quantity": 10}}, "requirements": {"Z": {}},
        "materials": {"1": {"material": "M1"}},
        "erp": {"materials": {"M1": {"lead_time_stock_weeks": "1", "lead_time_production_weeks": "6",
                                     "surcharge_mass_g": "0"}},
                "conditions": [{"material": "M1", "valid_from": "2031-01-01", "valid_to": "2031-12-31",
                                "price_unit": "1", "scales": [{"min_qty": "1", "price": "100"}]}],
                "stock": {"M1": {"free": "50"}}},
        "discount": {"percent": "0", "source": "no valid approval found"}, "mail": {},
    }
    base.update(over)
    return base


class Dependencies(unittest.TestCase):
    def test_missing_price_unit_names_its_dependency_and_is_a_gap(self):
        data = inputs()
        del data["erp"]["conditions"][0]["price_unit"]
        bid = compute(data)
        self.assertEqual(bid["answer"]["positions"], [])
        self.assertEqual(bid["unresolved"], {"1": ["erp prices M1"]})
        self.assertIn("price unit", bid["dependencies"]["erp prices M1"]["need"])
        # the coordinator's checks saw a valid condition with scales and reported nothing before
        from agents.coordinate import find_gaps
        board = Blackboard()
        board.results.update(data)
        gaps = {g["id"]: g for g in find_gaps(board, {"tender", "spec", "erp", "mail"}, {"tender": ["bidder q&a"]})}
        self.assertIn("price unit", gaps["erp prices M1"]["need"])

    def test_complete_position_has_no_open_dependency(self):
        bid = compute(inputs())
        self.assertEqual(bid["coverage"]["complete"], 1)
        self.assertEqual(bid["unresolved"], {})
        fields = bid["lines"][0]["fields"]
        self.assertEqual(fields["base"], "verified")
        self.assertEqual(fields["packaging"], "absent")          # the Q&A gives the position no packaging key
        self.assertEqual(fields["lead"], "verified: stock")

    def test_missing_delivery_commitment_is_an_estimate_with_its_dependency(self):
        data = inputs()
        data["tender"]["clarifications"][0]["keys"] = {"delivery_key": "D-1"}
        bid = compute(data)
        line = bid["lines"][0]
        self.assertTrue(line["fields"]["lead"].startswith("estimated"))
        self.assertIn("mail delivery_commitments D-1", line["unresolved"])
        data["mail"] = {"delivery_commitments": {"D-1": {"material": "OTHER", "weeks": "2", "max_total_pieces": "99"}}}
        line = compute(data)["lines"][0]                          # a commitment for another material does not apply
        self.assertEqual(line["fields"]["lead"], "verified: stock (commitment is for another material)")
        self.assertEqual(line["unresolved"], [])
        data["mail"]["delivery_commitments"]["D-1"]["material"] = "M1"
        self.assertEqual(compute(data)["answer"]["positions"][0]["lead_time_weeks"], 2)

    def test_bonus_and_packaging_without_their_mails_are_open_not_zero(self):
        data = inputs()
        data["tender"]["clarifications"][0]["keys"] = {"bonus_plan": "B", "packaging_tariff": "P"}
        data["erp"]["materials"]["M1"]["pieces_per_carton"] = "4"
        bid = compute(data)
        self.assertEqual(sorted(bid["unresolved"]["1"]), ["mail packaging_tariffs P", "mail project_bonus B"])
        self.assertNotIn("rebate_total", bid["answer"]["positions"][0])
        self.assertTrue(bid["lines"][0]["fields"]["bonus"].startswith("estimated"))


class ConditionEvidence(unittest.TestCase):
    SCALES = [{"min_qty": "1", "price": "200"}, {"min_qty": "10", "price": "150"}]

    def price(self, conditions, pieces=10):
        data = inputs()
        data["erp"]["conditions"] = reconcile_conditions(conditions)      # as erp_view does in a run
        bid = compute(data)
        return (bid["answer"]["positions"][0]["base_unit_price"] if bid["answer"]["positions"] else None), bid

    def test_fragment_of_another_record_does_not_complete_the_valid_one(self):
        header = {"material": "M1", "record": "C-1", "valid_from": "2031-01-01", "valid_to": "2031-12-31",
                  "price_unit": "1", "scales": []}
        foreign = {"material": "M1", "record": "C-9", "scales": self.SCALES}
        merged = reconcile_conditions([header, foreign])
        self.assertEqual(len(merged), 2)
        price, bid = self.price([header, foreign])
        self.assertIsNone(price)
        self.assertIn("quantity scales of the valid price condition (record C-1)",
                      bid["dependencies"]["erp prices M1"]["need"])

    def test_record_id_picks_the_right_one_of_several_periods(self):
        old = {"material": "M1", "record": "C-1", "valid_from": "2030-01-01", "valid_to": "2030-12-31",
               "price_unit": "1", "scales": []}
        new = {"material": "M1", "record": "C-2", "valid_from": "2031-01-01", "valid_to": "2031-12-31",
               "price_unit": "1", "scales": []}
        scales_of_old = {"material": "M1", "record": "C-1", "scales": self.SCALES}
        price, bid = self.price([old, new, scales_of_old])
        self.assertIsNone(price)                     # the expired record's scales never price the valid one
        scales_of_new = {"material": "M1", "record": "C-2", "scales": [{"min_qty": "1", "price": "120"}]}
        price, _ = self.price([old, new, scales_of_old, scales_of_new])
        self.assertEqual(price, 120.0)

    def test_two_reads_that_disagree_are_a_conflict_not_a_choice(self):
        a = {"material": "M1", "record": "C-1", "valid_from": "2031-01-01", "valid_to": "2031-12-31",
             "price_unit": "1", "scales": self.SCALES}
        b = dict(a, scales=[{"min_qty": "1", "price": "999"}])
        price, bid = self.price([a, b])
        self.assertIsNone(price)
        self.assertIn("conflicting evidence", bid["dependencies"]["erp prices M1"]["need"])

    def test_late_scale_page_of_the_same_record_completes_it(self):
        header = {"material": "M1", "valid_from": "2031-01-01", "valid_to": "2031-12-31", "price_unit": "1",
                  "scales": []}
        cache = {"h": {"kind": "prices", "url": "https://erp.example/cond/77", "conditions": [header]}}
        other = {"material": "M1", "valid_from": "2030-01-01", "valid_to": "2030-06-30", "price_unit": "1",
                 "scales": [{"min_qty": "1", "price": "5"}]}
        cache["o"] = {"kind": "prices", "url": "https://erp.example/cond/12", "conditions": [other]}
        self.assertIsNone(self.price(erp_view(cache)["conditions"])[0])
        cache["s"] = {"kind": "prices", "url": "https://erp.example/cond/77/scales",
                      "conditions": [{"material": "M1", "scales": self.SCALES}]}       # arrives later
        self.assertEqual(self.price(erp_view(cache)["conditions"])[0], 150.0)

    def test_undated_sole_condition_prices_but_stays_open(self):
        price, bid = self.price([{"material": "M1", "price_unit": "1", "scales": self.SCALES}])
        self.assertEqual(price, 150.0)
        self.assertTrue(bid["lines"][0]["fields"]["base"].startswith("estimated"))
        self.assertIn("validity period", bid["dependencies"]["erp prices M1"]["need"])


class ErpScheduling(unittest.IsolatedAsyncioTestCase):
    def scraper(self):
        from agents.scrape_erp import ErpScraper
        ctx = Context(task={"brief": "", "access": []}, board=Blackboard(), llm=None, pool=None)
        ctx.board.results["tender"] = {"positions": [{"position": "1", "requirement_ref": "A"},
                                                     {"position": "2", "requirement_ref": "B"}]}
        ctx.board.results["materials"] = {"1": {"material": "M1", "unverified": []}, "2": {"material": None}}
        return ErpScraper(ctx)

    async def test_finishing_a_matched_position_comes_first_and_covered_records_last(self):
        erp = self.scraper()
        item_a = {"group": "GA", "requirements": [{"ref": "A"}]}
        item_b = {"group": "GB", "requirements": [{"ref": "B"}]}
        erp.rank.update({"M5": 1, "M7": 0, "M8": 1})
        for job in [("record", "M5", item_a), ("search", item_b), ("record", "M8", item_b), ("record", "M7", item_b),
                    ("prices", "M9", item_b), ("prices", "M1", item_a), ("stock", "M1", item_a),
                    ("prices", "M1", item_a, "price unit of the valid condition")]:
            erp.add(job, force=len(job) > 3)
        order = []
        while (job := erp.take()) is not None:
            order.append(job[0] + " " + (job[1] if job[0] != "search" else job[1]["group"]) + (" focus" if len(job) > 3 else ""))
        self.assertEqual(order, ["prices M1 focus", "prices M1", "stock M1", "search GB", "record M7", "record M8",
                                 "prices M9", "record M5"])

    async def test_worker_takes_the_next_job_while_extraction_runs(self):
        erp = self.scraper()
        erp.site_id = "erp"
        browsed, release = [], asyncio.Event()

        async def fake_browse_until_done(goal, **kwargs):
            browsed.append(goal)
            return {"records": [], "done": True}

        async def slow_extract(out):
            await release.wait()
            return {}
        erp.browse_until_done = fake_browse_until_done
        erp.extract_job = slow_extract
        item = {"group": "GB", "requirements": [{"ref": "B", "bore": "50", "stroke": "100", "pressure": "160",
                                                 "mounting": "F", "certification": "keine"}]}
        erp.add(("search", item))
        erp.add(("record", "M7", item))
        erp.producer_done = True
        with mock.patch("agents.scrape_erp.search_candidates", return_value=[]):
            worker = asyncio.create_task(erp.worker(None))
            for _ in range(50):
                await asyncio.sleep(0.01)
                if len(browsed) == 2:
                    break
            self.assertEqual(len(browsed), 2)        # the record browse ran before the search was extracted
            self.assertFalse(worker.done())          # pending extraction keeps the worker alive
            release.set()
            await asyncio.wait_for(worker, 3)


class ErpPause(unittest.IsolatedAsyncioTestCase):
    async def test_worker_pauses_near_cutoff_and_resumes_when_the_reserve_shrinks(self):
        from agents.scrape_erp import ErpScraper
        ctx = Context(task={"brief": "", "access": []}, board=Blackboard(), llm=None, pool=None)
        left = [30.0]                                 # below the ERP's analysis margin: no new job yet
        ctx.time_left = lambda: left[0]
        erp = ErpScraper(ctx)
        ran = []

        async def do(job, tab):
            ran.append(job[0])
        erp.do = do
        erp.add(("search", {"group": "G", "requirements": []}))
        erp.producer_done = True
        worker = asyncio.create_task(erp.worker(None))
        await asyncio.sleep(0.1)
        self.assertEqual(ran, [])
        self.assertFalse(worker.done())              # waiting, not gone
        left[0] = 200.0                               # the wizard warm-up finished: the reserve shrank
        await asyncio.wait_for(worker, 5)
        self.assertEqual(ran, ["search"])


class DraftAndReserve(unittest.IsolatedAsyncioTestCase):
    async def test_draft_bid_is_published_during_collection_with_milestones(self):
        from agents.compute_bid import BidDrafter
        tl = timeline.reset()
        ctx = Context(task={"brief": "", "access": []}, board=Blackboard(), llm=None, pool=None)
        ctx.board.results.update(inputs())
        task = asyncio.create_task(BidDrafter(ctx).run())
        draft = await ctx.board.wait_for(key="bid_draft", timeout=2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(draft["coverage"]["priced"], 1)
        self.assertIn("first usable bid", tl.milestones)
        self.assertIn("all positions complete", tl.milestones)

    def test_reserve_follows_positions_pace_and_wizard_prep(self):
        from swarm import context as c
        tl = timeline.reset()
        ctx = Context(task={"brief": "", "access": []}, board=Blackboard(), llm=None, pool=None)
        ctx.board.results["tender"] = {"positions": [{"position": str(i)} for i in range(5)]}
        with mock.patch.object(c, "FIXED_RESERVE", False), mock.patch.object(c, "MIN_RESERVE_S", 300):
            for i in range(10):
                tl.add("browse.step", "step", i * 10.0, i * 10.0 + 10.0)        # 10 s per step measured
            slow_free = ctx.submit_reserve()
            ctx.shared["wizard_ready"] = "wizard open"
            prepared = ctx.submit_reserve()
            for i in range(40):
                tl.add("browse.step", "step", 0.0, 40.0)                        # pace drops to 40 s per step
            slow = ctx.submit_reserve()
        self.assertLess(prepared, slow_free)
        self.assertLessEqual(300, prepared)
        self.assertEqual(slow, c.MAX_RESERVE_S)

    async def test_collection_cutoff_follows_the_live_reserve(self):
        from swarm.orchestrator import _until_collect_ends
        ctx = Context(task={"brief": "", "access": []}, board=Blackboard(), llm=None, pool=None)
        left = [3.0]
        ctx.time_left = lambda: left[0]
        stopped = asyncio.Event()

        async def work():
            try:
                await asyncio.sleep(60)
            finally:
                stopped.set()
        runner = asyncio.create_task(_until_collect_ends(ctx, work()))
        await asyncio.sleep(0.05)
        left[0] = 0                                  # the reserve grew: collection must end now
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(runner, 3)
        self.assertTrue(stopped.is_set())


class SummaryCheck(unittest.TestCase):
    def test_check_reports_values_missing_from_the_summary(self):
        from agents.submit_bid import check_page, expected_values
        bid = compute(inputs())
        expected = expected_values(bid)
        self.assertEqual(expected["1"], ["M1", "10", "100,00"])
        self.assertEqual(check_page("Pos 1  M1  10 Stk  100,00 EUR  Summe 1.000,00", expected), [])
        self.assertEqual(check_page("Pos 1  M1  10 Stk  10,00 EUR", expected), ["1: 100,00", "total: 1.000,00"])


if __name__ == "__main__":
    unittest.main()
