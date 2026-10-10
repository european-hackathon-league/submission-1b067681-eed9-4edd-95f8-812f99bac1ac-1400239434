"""The submitter's data preparation, with invented values and no browser or model."""
import asyncio
import unittest

from agents.submit_bid import BidSubmitter, TanWaiter, de, de_percent
from swarm import Blackboard, Context
from swarm import bidmath as bm


def make_ctx(bid=None, bidder=None):
    task = {"brief": "dummy", "access": [{"siteId": "tender-portal", "url": "https://portal.invalid",
                                          "credentials": {"username": "u", "password": "p"}}],
            "submission": {"kind": "site", "siteId": "tender-portal", "url": "https://portal.invalid"},
            "expires_at": None}
    board = Blackboard()
    if bid is not None:
        board.results["bid"] = bid
    if bidder is not None:
        board.results["bidder"] = bidder
    return Context(task=task, board=board, llm=None, pool=None)


class Formats(unittest.TestCase):
    def test_german_numbers(self):
        self.assertEqual(de(1234.5), "1234,50")
        self.assertEqual(de("0"), "0,00")
        self.assertEqual(de(bm.D("12.345")), "12,35")
        self.assertEqual(de_percent(5), "5")
        self.assertEqual(de_percent("2.5"), "2,5")


class FormData(unittest.TestCase):
    facts = {"tender_reference": "REF-1", "bidder_company": "Dummy GmbH", "bidder_vat_id": "XX1", "bidder_email": None}

    def test_without_a_bid_everything_is_omitted(self):
        data = BidSubmitter(make_ctx()).data_for_form(self.facts)
        self.assertEqual((data["tender_reference"], data["decision"], data["positions_to_enter"]), ("REF-1", "bid", []))
        self.assertEqual((data["discount_percent"], data["net_total_eur"]), ("0", "0,00"))
        self.assertEqual(data["bidder"], {"company": "Dummy GmbH", "vat_id": "XX1", "contact_email": None})
        self.assertEqual(data["login"]["username"], "u")

    def test_bid_from_bidmath_with_attributes(self):
        answer = bm.answer(bm.Bid("REF​-2", discount_percent=bm.D("2.5"), positions=[
            bm.PositionFacts("01.01", "M-1", 4, bm.D("10.00"), 3)]))
        answer["positions"][0]["attributes"] = {"bore": "50", "stroke": "200"}
        data = BidSubmitter(make_ctx(bid=answer, bidder={"company": "Board GmbH"})).data_for_form(self.facts)
        self.assertEqual(data["tender_reference"], "REF-2")
        self.assertEqual(data["bidder"]["company"], "Board GmbH")
        pos = data["positions_to_enter"][0]
        self.assertEqual((pos["position"], pos["quantity_pieces"], pos["net_unit_price_after_discount"]),
                         ("01.01", 4, "9,75"))
        self.assertEqual(pos["material_attributes"], {"bore": "50", "stroke": "200"})
        self.assertEqual((data["discount_percent"], data["net_total_eur"]), ("2,5", "39,00"))

    def test_no_bid_enters_no_positions(self):
        answer = bm.answer(bm.Bid("REF-3", decision="no_bid"))
        data = BidSubmitter(make_ctx(bid=answer)).data_for_form(self.facts)
        self.assertEqual((data["decision"], data["positions_to_enter"]), ("no_bid", []))


class Tan(unittest.TestCase):
    def test_waits_for_a_tan_posted_after_the_request(self):
        async def scenario():
            ctx = make_ctx()
            ctx.board.results["tan"] = {"tan": "old"}           # a stale TAN must not be used
            waiter = TanWaiter(ctx, timeout_cap=2)

            async def mailbox():
                await ctx.board.wait_for(key="tan_request", timeout=2)
                await ctx.board.set("tan", {"tan": "12​34", "at": 1})
            asyncio.ensure_future(mailbox())
            return await waiter(since=0)
        self.assertEqual(asyncio.run(scenario()), "1234")

    def test_no_tan_returns_none(self):
        self.assertIsNone(asyncio.run(TanWaiter(make_ctx(), timeout_cap=0.1)(since=0)))


if __name__ == "__main__":
    unittest.main()
