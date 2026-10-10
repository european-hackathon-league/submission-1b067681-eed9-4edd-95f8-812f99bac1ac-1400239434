"""Semantic contracts across branches: two implementations of the same bid rules must agree.

The runtime prices with agents/compute_bid.py (from tender_portal); swarm/bidmath.py (from main / site-recon) holds the
same rules as small tested functions. A Git merge cannot see whether they agree, so this test feeds both the same
invented case, worked out by hand, and compares every field. Invented values only.
"""
import unittest
from decimal import Decimal

from agents.compute_bid import compute
from swarm import bidmath as bm

# A hand-worked case: FX, a date-selected metal index, a base+surcharge clause, cartons, a shared setup pool, project
# bonus bands, a delivery commitment, shared stock and a conditional discount (10 % from 1000 EUR undiscounted).
EXPECTED = {
    "P1": {"quantity": 10, "unit_price": 87.31, "base_unit_price": 90.0, "surcharge_per_unit": 7.25,
           "packaging_per_unit": 0.9, "rebate_total": 14.43, "lead_time_weeks": 1},
    "P2": {"quantity": 10, "unit_price": 35.57, "base_unit_price": 36.0, "surcharge_per_unit": 3.75,
           "packaging_per_unit": 0.0, "rebate_total": 5.77, "lead_time_weeks": 3},
}
TOTAL, DISCOUNT = 1228.80, 10.0


def runtime_inputs() -> dict:
    """The case in the shapes compute() reads (results of tender_portal's analysts)."""
    return {
        "brief": {"tender_reference": "T-1", "scenario_date": "2030-03-10T09:00:00Z"},
        "tender": {"positions": [{"position": "P1", "unit": "Stück", "requirement_ref": "R1"},
                                 {"position": "P2", "unit": "Satz", "requirement_ref": "R2"}],
                   "clarifications": [
                       {"number": "1", "date": "2030-02-01", "positions": ["P1"], "setup_fee": "20",
                        "keys": {"konditionsgruppe": "K1", "packaging_tariff": "T1", "setup_pool": "POOL",
                                 "bonus_plan": "B"}},
                       {"number": "2", "date": "2030-02-01", "positions": ["P2"],
                        "keys": {"delivery_key": "C1", "setup_pool": "POOL", "bonus_plan": "B"}}]},
        "quantities": {"P1": {"quantity": 10}, "P2": {"quantity": 2}},
        "requirements": {"R1": {}, "R2": {"pieces_per_set": 5}},
        "materials": {"P1": {"material": "M1"}, "P2": {"material": "M2"}},
        "erp": {"materials": {"M1": {"pieces_per_carton": 4, "surcharge_mass_g": 2000, "metal_index": "AL",
                                     "allocation_mass_g": 1000, "lead_time_stock_weeks": 1,
                                     "lead_time_production_weeks": 6},
                              "M2": {"allocation_mass_g": 3000, "lead_time_stock_weeks": 1,
                                     "lead_time_production_weeks": 8, "surcharge_mass_g": 0, "metal_index": "AL"}},
                "conditions": [{"material": "M1", "valid_from": "2029-01-01", "valid_to": "2030-03-09", "price_unit": 1,
                                "scales": [{"min_qty": 1, "price": 999}]},
                               {"material": "M1", "valid_from": "2030-01-01", "valid_to": "2030-12-31", "price_unit": 1,
                                "scales": [{"min_qty": 1, "price": 100}, {"min_qty": 10, "price": 90}]},
                               {"material": "M2", "valid_from": "2030-01-01", "currency": "USD", "fx_key": "USD-1",
                                "price_unit": 1, "scales": [{"min_qty": 1, "price": 50}, {"min_qty": 10, "price": 40}]}],
                "stock": {"M1": {"free": 10}, "M2": {"free": 0}}},
        "discount": {"percent": "10", "min_order_value": "1000"},
        "mail": {"fx_by_key": {"USD-1": {"USD": "0.9"}}, "price_indices": {"AL": "2.0"},
                 "condition_rules": {"K1": "base_and_surcharge"}, "packaging_tariffs": {"T1": "3"},
                 "delivery_commitments": {"C1": {"material": "M2", "weeks": 3, "max_total_pieces": 10}},
                 "setup_pools": {"POOL": "50"},
                 "project_bonus": {"B": [{"from": 0, "to": 500, "percent": 1}, {"from": 500, "to": None, "percent": 2}]}},
    }


def bidmath_answer() -> dict:
    """The same case through bidmath's functions (the calculator in main's / tanreader-debug's pipeline)."""
    on = bm.date(2030, 3, 10)
    p1_base = bm.base_unit_price_eur([bm.Scale(1, 100), bm.Scale(10, 90)], 10)
    p2_base = bm.base_unit_price_eur([bm.Scale(1, 50), bm.Scale(10, 40)], 10, 1, Decimal("0.9"))
    rate = bm.rate_on([bm.RateWindow(Decimal("2.0"), bm.date(2030, 3, 1))], on)
    shares = bm.allocate_cents(50, {"P1": 10 * 1, "P2": 10 * 3})
    values = {"P1": 10 * p1_base, "P2": 10 * p2_base}
    credit = bm.marginal_credit(sum(values.values()), [bm.Band(0, 500, 1), bm.Band(500, None, 2)])
    bonus = bm.allocate_cents(credit, values)
    net = bm.undiscounted_net([(10, p1_base), (10, p2_base)])
    pct = bm.order_discount(10, 1000, net)
    p1 = bm.PositionFacts("P1", "M1", 10, p1_base, bm.lead_time_weeks(10, 10, 1, 6),
                          surcharge_per_piece=2 * rate, setup_fee=Decimal(20), shared_setup=shares["P1"],
                          carton_cost=bm.carton_cost(10, 4, 3), project_bonus=bonus["P1"], clause=bm.BASE_AND_SURCHARGE)
    p2 = bm.PositionFacts("P2", "M2", bm.final_pieces(2, "Satz", 5), p2_base, bm.lead_time_weeks(0, 10, 1, 8, 3, 10),
                          shared_setup=shares["P2"], project_bonus=bonus["P2"])
    return bm.answer(bm.Bid("T-1", discount_percent=pct, positions=[p1, p2]))


class PriceRulesAgree(unittest.TestCase):
    def check(self, answer: dict, who: str):
        self.assertEqual(answer["discount_percent"], DISCOUNT, who)
        self.assertEqual(answer["total_net"], TOTAL, who)
        got = {p["position"]: p for p in answer["positions"]}
        for pos, fields in EXPECTED.items():
            for name, value in fields.items():
                self.assertEqual(got[pos].get(name), value, f"{who}: {pos}.{name}")

    def test_runtime_calculator_matches_the_hand_worked_case(self):
        self.check(compute(runtime_inputs())["answer"], "agents/compute_bid.compute")

    def test_bidmath_matches_the_hand_worked_case(self):
        self.check(bidmath_answer(), "swarm.bidmath")

    def test_missing_price_unit_is_an_open_contract_question(self):
        """OPEN (docs/INTEGRATION.md): bidmath reads a missing price unit as 1; compute() deliberately prices nothing
        (tender_portal: tests/test_rules.py 'does_not_get_guessed'). This pins today's runtime behaviour."""
        inputs = runtime_inputs()
        for c in inputs["erp"]["conditions"]:
            c.pop("price_unit")
        self.assertEqual(compute(inputs)["answer"]["positions"], [])

    def test_explicit_price_unit_is_applied(self):
        inputs = runtime_inputs()
        inputs["erp"]["conditions"][1]["price_unit"] = 100
        p1 = {p["position"]: p for p in compute(inputs)["answer"]["positions"]}["P1"]
        self.assertEqual(p1["base_unit_price"], 0.9)                     # 90 per 100 pieces

if __name__ == "__main__":
    unittest.main()
