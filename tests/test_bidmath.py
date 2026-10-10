"""Bid arithmetic against invented numbers (never scenario values)."""
import unittest
from datetime import date
from decimal import Decimal

from swarm import bidmath as bm
from swarm.bidmath import D


class Parsing(unittest.TestCase):
    def test_german_and_english_numbers(self):
        self.assertEqual(D("1.234,56"), Decimal("1234.56"))
        self.assertEqual(D("1,234.56"), Decimal("1234.56"))
        self.assertEqual(D("12,5 %"), Decimal("12.5"))
        self.assertEqual(D("99,90 €"), Decimal("99.90"))
        self.assertEqual(D(0.1), Decimal("0.1"))

    def test_round_half_up(self):
        self.assertEqual(bm.round_cents("2.345"), Decimal("2.35"))
        self.assertEqual(bm.round_cents("2.344999"), Decimal("2.34"))
        self.assertEqual(bm.round_cents("-0.005"), Decimal("-0.01"))


class Quantities(unittest.TestCase):
    def test_sets_become_pieces(self):
        self.assertEqual(bm.final_pieces(3, "Satz", pieces_per_set=4), 12)
        self.assertEqual(bm.final_pieces("7", "Stück"), 7)
        with self.assertRaises(ValueError):
            bm.final_pieces(3, "Satz")

    def test_latest_remaining_change_wins_and_withdrawals_are_ignored(self):
        changes = [
            bm.QuantityChange("1", date(2030, 1, 2), 50),
            bm.QuantityChange("2", date(2030, 1, 5), 70),
            bm.QuantityChange("3", date(2030, 1, 9), None, withdraws="2"),
        ]
        self.assertEqual(bm.resolve_quantity(40, changes), 50)
        self.assertEqual(bm.resolve_quantity(40, changes[2:]), 40)
        self.assertEqual(bm.resolve_quantity(40, []), 40)


class Prices(unittest.TestCase):
    scales = [bm.Scale(1, "100,00"), bm.Scale(10, "90,00"), bm.Scale(50, "80,00")]

    def test_highest_scale_reached(self):
        self.assertEqual(bm.scale_price(self.scales, 9), Decimal("100.00"))
        self.assertEqual(bm.scale_price(self.scales, 10), Decimal("90.00"))
        self.assertEqual(bm.scale_price(self.scales, 500), Decimal("80.00"))
        with self.assertRaises(ValueError):
            bm.scale_price([bm.Scale(5, 1)], 4)

    def test_price_unit_and_currency_conversion_round_once(self):
        # 1234.50 per 100 pieces = 12.345 per piece; x 1.1 EUR = 13.5795 -> 13.58
        scales = [bm.Scale(1, "1234,50")]
        self.assertEqual(bm.base_unit_price_eur(scales, 5, price_unit=100, eur_per_unit="1,1"), Decimal("13.58"))

    def test_rate_window_contains_scenario_date(self):
        windows = [bm.RateWindow("1.0", date(2030, 1, 1), date(2030, 1, 31)),
                   bm.RateWindow("2.0", date(2030, 2, 1), date(2030, 2, 28)),
                   bm.RateWindow("3.0", date(2030, 3, 1), None)]
        self.assertEqual(bm.rate_on(windows, date(2030, 1, 31)), Decimal("1.0"))   # inclusive end
        self.assertEqual(bm.rate_on(windows, date(2030, 2, 1)), Decimal("2.0"))
        self.assertEqual(bm.rate_on(windows, date(2031, 1, 1)), Decimal("3.0"))
        with self.assertRaises(ValueError):
            bm.rate_on(windows[:1], date(2029, 12, 31))


class Discount(unittest.TestCase):
    def test_threshold_is_inclusive_and_has_no_fallback(self):
        net = bm.undiscounted_net([(10, D("50.00")), (3, D("33.335"))])   # 500 + 100.005 -> 600.01
        self.assertEqual(net, Decimal("600.01"))
        self.assertEqual(bm.order_discount(5, threshold="600.01", net=net), Decimal("5"))
        self.assertEqual(bm.order_discount(5, threshold="600.02", net=net), Decimal("0"))
        self.assertEqual(bm.order_discount(5), Decimal("5"))
        self.assertEqual(bm.order_discount(5, threshold=1, net=net, decision="no_bid"), Decimal("0"))


class Allocation(unittest.TestCase):
    def test_largest_remainder_in_cents_with_ascending_ties(self):
        out = bm.allocate_cents("1.00", {"20": 1, "10": 1, "30": 1})
        self.assertEqual(sum(out.values()), Decimal("1.00"))
        self.assertEqual(out, {"10": Decimal("0.34"), "20": Decimal("0.33"), "30": Decimal("0.33")})

    def test_proportional(self):
        out = bm.allocate_cents("10.00", {"a": 3, "b": 1})
        self.assertEqual(out, {"a": Decimal("7.50"), "b": Decimal("2.50")})

    def test_marginal_bands(self):
        bands = [bm.Band(0, 1000, 0), bm.Band(1000, 5000, 1), bm.Band(5000, None, "2,5")]
        # 0 on the first 1000, 1% of 4000 = 40, 2.5% of 1000 = 25
        self.assertEqual(bm.marginal_credit(6000, bands), Decimal("65.00"))
        self.assertEqual(bm.marginal_credit(800, bands), Decimal("0.00"))


class Delivery(unittest.TestCase):
    def test_shared_stock_all_or_nothing(self):
        self.assertEqual(bm.lead_time_weeks(stock=10, pooled_demand=10, stock_weeks=1, production_weeks=6), 1)
        self.assertEqual(bm.lead_time_weeks(stock=9, pooled_demand=10, stock_weeks=1, production_weeks=6), 6)

    def test_commitment_within_inclusive_ceiling(self):
        args = dict(stock=0, stock_weeks=1, production_weeks=6, commitment_weeks=3, commitment_ceiling=20)
        self.assertEqual(bm.lead_time_weeks(pooled_demand=20, **args), 3)
        self.assertEqual(bm.lead_time_weeks(pooled_demand=21, **args), 6)


class Pricing(unittest.TestCase):
    def test_cartons_round_up_without_changing_quantity(self):
        self.assertEqual(bm.carton_cost(21, 10, "2,50"), Decimal("7.50"))

    def test_final_unit_price_rounds_once_from_unrounded_parts(self):
        facts = bm.PositionFacts(position="10", material_number="X-1", pieces=3, base_unit_price=D("100.00"),
                                 lead_time_weeks=4, surcharge_per_piece=D("1.111"), setup_fee=D("10.00"),
                                 carton_cost=D("1.00"), project_bonus=D("0.50"), clause=bm.BASE_ONLY)
        priced = bm.price_position(facts, discount_percent=10)
        # 90 + 1.111 + (10 + 1 - 0.5) / 3 = 94.611 -> 94.61
        self.assertEqual(priced.entry["unit_price"], 94.61)
        self.assertEqual(priced.entry["surcharge_per_unit"], 4.44)   # 1.111 + 10/3, before discount
        self.assertEqual(priced.entry["packaging_per_unit"], 0.33)
        self.assertEqual(priced.entry["rebate_total"], 0.5)

    def test_clauses(self):
        base = dict(position="10", material_number="X-1", pieces=1, base_unit_price=D("100"), lead_time_weeks=1,
                    surcharge_per_piece=D("10"))
        price = lambda clause: bm.price_position(bm.PositionFacts(clause=clause, **base), 10).entry["unit_price"]
        self.assertEqual(price(bm.BASE_ONLY), 100.0)            # 90 + 10
        self.assertEqual(price(bm.BASE_AND_SURCHARGE), 99.0)    # 90 + 9
        self.assertEqual(price(bm.NO_DISCOUNT), 110.0)          # 100 + 10

    def test_answer_total_and_no_bid(self):
        positions = [bm.PositionFacts("10", "X-1", 3, D("10.005"), 2), bm.PositionFacts("20", "X-2", 2, D("1.50"), 2)]
        out = bm.answer(bm.Bid("REF-TEST", positions=positions))
        self.assertEqual([p["unit_price"] for p in out["positions"]], [10.01, 1.5])
        self.assertEqual(out["total_net"], 33.03)                   # 3 x 10.01 + 2 x 1.50
        self.assertEqual(out["currency"], "EUR")
        nb = bm.answer(bm.Bid("REF-TEST", decision="no_bid", discount_percent=D("5"), positions=positions))
        self.assertEqual((nb["positions"], nb["total_net"], nb["discount_percent"]), ([], 0, 0))


if __name__ == "__main__":
    unittest.main()
