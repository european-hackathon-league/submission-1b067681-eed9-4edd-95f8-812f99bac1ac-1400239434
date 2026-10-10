"""The submitter's data preparation, with invented values and no browser or model.

Carried over from main's formpilot submitter tests: the same intents (German number format, every position omitted
without a bid, no positions on no_bid) checked against the browser-use submitter that replaced it.
"""
import unittest
from decimal import Decimal

from agents.submit_bid import bid_table
from swarm.normalize import de_number


class Formats(unittest.TestCase):
    def test_german_numbers(self):
        # OPEN (docs/INTEGRATION.md): main entered '1234,50', tender_portal enters '1.234,50'. Both are "German number
        # format"; which the bid form accepts above 999 EUR is unverified. This pins the integrated behaviour.
        self.assertEqual(de_number(Decimal("1234.5")), "1.234,50")
        self.assertEqual(de_number(Decimal("0")), "0,00")
        self.assertEqual(de_number(Decimal("12.345")), "12,35")      # half-up to cents


class Table(unittest.TestCase):
    def test_without_a_bid_every_tender_position_is_omitted(self):
        table = bid_table({}, {"positions": [{"position": "01.01"}, {"position": "01.02"}]})
        self.assertIn("Position 01.01: OMIT", table)
        self.assertIn("Position 01.02: OMIT", table)

    def test_no_positions_at_all(self):
        self.assertIn("omit every position", bid_table({}, {}))

    def test_priced_position_is_entered_and_unpriced_one_omitted(self):
        bid = {"answer": {"positions": [{"position": "01.01", "material_number": "M-1", "quantity": 4,
                                         "lead_time_weeks": 3}]},
               "lines": [{"position": "01.01", "display": {"unit_price": "9,75"}, "required": {"bore": "50"}},
                         {"position": "01.02"}]}
        table = bid_table(bid, {})
        self.assertIn("material M-1; quantity 4 pieces; net unit price 9,75 EUR; lead time 3 weeks", table)
        self.assertIn("Position 01.02: OMIT", table)


if __name__ == "__main__":
    unittest.main()
