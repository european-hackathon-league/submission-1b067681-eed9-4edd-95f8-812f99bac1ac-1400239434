"""Rule code against invented data: number/date parsing, quantities, amendments, mail authority, matching, pricing."""
import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.analyze_discount import summarise, valid_messages  # noqa: E402
from agents.analyze_materials import match_positions, required_stroke  # noqa: E402
from agents.analyze_quantities import final_quantities  # noqa: E402
from agents.analyze_requirements import apply_amendments  # noqa: E402
from agents.compute_bid import allocate_cents, compute, marginal_bonus, scale_price, valid_condition  # noqa: E402
from agents.scrape_erp import work_items  # noqa: E402
from swarm.extract import brief_section, chunks  # noqa: E402
from swarm.normalize import de_number, norm_ref, to_datetime, to_decimal  # noqa: E402
from swarm.page_text import clean  # noqa: E402


class Normalize(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual(to_decimal("1.234,56"), Decimal("1234.56"))
        self.assertEqual(to_decimal("1,234.56"), Decimal("1234.56"))
        self.assertEqual(to_decimal("511,250 (en-US)"), Decimal("511250"))
        self.assertEqual(to_decimal("17,5 (de-DE)"), Decimal("17.5"))
        self.assertEqual(to_decimal("42,00 EUR"), Decimal("42.00"))
        self.assertEqual(to_decimal("7​3"), Decimal("73"))
        self.assertIsNone(to_decimal("keine"))

    def test_german_display(self):
        self.assertEqual(de_number(Decimal("12345.5")), "12.345,50")
        self.assertEqual(de_number(Decimal("0.005")), "0,01")

    def test_dates(self):
        self.assertEqual(to_datetime("2031-03-02T08:00:00Z").day, 2)
        self.assertEqual(to_datetime("03.04.2031, 10:00").month, 4)
        self.assertEqual(to_datetime("02/07 (MM/TT) 2031").month, 2)
        self.assertEqual(to_datetime("02/07 (TT/MM) 2031").month, 7)
        self.assertEqual(to_datetime('2031-02-01 13:00 Europe/Athens'), to_datetime('2031-02-01T11:00:00Z'))
        self.assertEqual(to_datetime('01.07.2031 13:00 Europe/Berlin'), to_datetime('2031-07-01T11:00:00Z'))
        self.assertEqual(to_datetime('01.07.2031 13:00 UTC+03:00'), to_datetime('2031-07-01T10:00:00Z'))
        self.assertIsNone(to_datetime('2031-01-01 10:00 Invalid/Timezone'))

    def test_refs_and_text(self):
        self.assertEqual(norm_ref("x​y-1 "), "XY-1")
        self.assertEqual(clean("a​b\n\n\n  c  "), "ab\n\nc")
        self.assertIn("## Sub", brief_section("# T\n## Rules\nr\n## Sub\ns", "sub"))
        self.assertEqual(len(chunks(["a" * 30, "b" * 30], size=40)), 2)


class Quantities(unittest.TestCase):
    def test_latest_change_wins_and_withdrawn_answers_are_ignored(self):
        positions = [{"position": "9.1", "quantity": "10", "unit": "Stk"}, {"position": "9.2", "quantity": "4", "unit": "Satz"}]
        qa = [{"number": "11", "date": "2031-01-02T10:00:00Z", "positions": ["9.1"], "new_quantity": "12"},
              {"number": "12", "date": "2031-01-05T10:00:00Z", "positions": ["9.1"], "new_quantity": "14"},
              {"number": "13", "date": "2031-01-03T10:00:00Z", "positions": ["9.2"], "new_quantity": "6"},
              {"number": "14", "date": "2031-01-06T10:00:00Z", "positions": ["9.2"], "new_quantity": "9"},
              {"number": "15", "date": "2031-01-07T10:00:00Z", "positions": ["9.2"], "withdraws": ["14"], "kind": "withdrawal"},
              {"number": "16", "date": "2031-01-08T10:00:00Z", "positions": ["9.1"], "withdraws": ["12"],
               "kind": "quantity_change", "new_quantity": None}]     # a superseding value is not a withdrawal
        q = final_quantities(positions, qa)
        self.assertEqual(q["9.1"]["quantity"], 14)
        self.assertEqual(q["9.2"]["quantity"], 6)


class Requirements(unittest.TestCase):
    def test_only_amendments_for_the_current_revision_latest_per_attribute(self):
        reqs = {"Z-1": {"stroke": "300", "bore": "50"}}
        qa = [{"number": "1", "date": "2031-01-01", "amendment": {"revision": "Q-2", "requirement": "Z-1", "attribute": "Hub", "value": "310"}},
              {"number": "2", "date": "2031-01-03", "amendment": {"revision": "Q-2", "requirement": "Z-1", "attribute": "Hub", "value": "305"}},
              {"number": "3", "date": "2031-01-04", "amendment": {"revision": "Q-1", "requirement": "Z-1", "attribute": "Bohrung", "value": "60"}}]
        out = apply_amendments(reqs, "Q-2", qa)
        self.assertEqual(out["Z-1"]["stroke"], "305")
        self.assertEqual(out["Z-1"]["bore"], "50")

    def test_usable_stroke_gets_two_end_reserves(self):
        mail = {"technical_release": {"T-1": {"revision": "Q-2", "reserve_mm_per_end": "7"}}}
        self.assertEqual(required_stroke({"stroke": "300", "stroke_kind": "usable"}, mail, "Q-2"), Decimal("314"))
        self.assertEqual(required_stroke({"stroke": "300", "stroke_kind": "nominal"}, mail, "Q-2"), Decimal("300"))

    def test_work_items_group_by_catalogue_group(self):
        items = work_items({"Z-1": {"kataloggruppe": "G-1", "bore": "50"}, "Z-2": {"kataloggruppe": "G-1"},
                            "Z-3": {"kataloggruppe": ""}})
        self.assertEqual(sorted(len(i["requirements"]) for i in items), [1, 2])


class Mail(unittest.TestCase):
    def test_authority_reference_and_date(self):
        senders = {"discount_approval": "boss"}
        msgs = [{"topic": "discount_approval", "sender": "boss@us.test", "reference": "T-9", "date": "2031-01-01T00:00:00Z", "discount_percent": 3},
                {"topic": "discount_approval", "sender": "boss@us.test", "reference": "T-9-B", "date": "2031-01-02T00:00:00Z", "discount_percent": 8},
                {"topic": "discount_approval", "sender": "intern@us.test", "reference": "T-9", "date": "2031-01-02T00:00:00Z", "discount_percent": 9},
                {"topic": "discount_approval", "sender": "boss@other.test", "reference": "T-9", "date": "2031-01-02T00:00:00Z", "discount_percent": 7},
                {"topic": "discount_approval", "sender": "boss@us.test", "reference": "T-9", "date": "2031-03-01T00:00:00Z", "discount_percent": 6},
                {"topic": "discount_approval", "sender": "boss@us.test", "reference": "T-9", "date": "2031-01-03T00:00:00Z", "discount_percent": 4, "is_draft": True}]
        valid = valid_messages(msgs, "T-9", to_datetime("2031-02-01T00:00:00Z"), senders, "us.test")
        self.assertEqual([m["discount_percent"] for m in valid], [3])

    def test_index_window_and_newest_key_wins(self):
        msgs = [{"topic": "price_indices", "date": "2031-01-01", "entries": [
                    {"index": "IX", "eur_per_kg": 1.5, "valid_from": "01.12.2030", "valid_to": "31.01.2031"},
                    {"index": "IX", "eur_per_kg": 2.5, "valid_from": "01.02.2031", "valid_to": None}]},
                {"topic": "packaging_tariffs", "date": "2031-01-01", "entries": [{"key": "P-1", "eur_per_carton": 9}]},
                {"topic": "packaging_tariffs", "date": "2031-01-02", "entries": [{"key": "P-1", "eur_per_carton": 11}]}]
        mail = summarise(msgs, to_datetime("2031-01-31T12:00:00Z"))
        self.assertEqual(mail["price_indices"]["IX"], "1.5")
        self.assertEqual(mail["packaging_tariffs"]["P-1"], "11")


class Pricing(unittest.TestCase):
    def test_unpriced_member_keeps_its_pool_share_and_makes_bonus_unknown(self):
        inputs = {
            'brief':{'tender_reference':'fixture','scenario_date':'2031-02-01'},
            'tender':{'positions':[{'position':p,'unit':'Stk'} for p in ('1','2')],
                      'clarifications':[{'positions':['1','2'],'keys':{'setup_pool':'S','bonus_plan':'B'}}]},
            'quantities':{'1':{'quantity':1},'2':{'quantity':1}},
            'materials':{'1':{'material':'M1'},'2':{'material':'M2'}},
            'erp':{'materials':{m:{'allocation_mass_g':'100','lead_time_production_weeks':'2'} for m in ('M1','M2')},
                   'conditions':[{'material':'M1','price_unit':'1','scales':[{'min_qty':'1','price':'100'}]}]},
            'mail':{'setup_pools':{'S':'20'},'project_bonus':{'B':[{'from':'0','percent':'10'}]}},
            'discount':{'percent':'5','min_order_value':'150'},
        }
        bid = compute(inputs)
        self.assertEqual(bid['answer']['positions'][0]['unit_price'], 110)
        self.assertNotIn('rebate_total', bid['answer']['positions'][0])
        self.assertFalse(bid['order_value_complete'])
        self.assertFalse(bid['discount_resolved'])

    def test_unconditional_discount_survives_no_bid(self):
        bid = compute({'brief':{'tender_reference':'fixture'}, 'tender':{'positions':[{'position':'1'}]},
                       'materials':{'1':{'reason':'only blocked matches'}}, 'discount':{'percent':'5'}})
        self.assertEqual(bid['answer']['decision'], 'no_bid')
        self.assertEqual(bid['answer']['discount_percent'], 5)

    def test_missing_price_unit_or_scenario_does_not_get_guessed(self):
        self.assertIsNone(scale_price({'scales':[{'min_qty':1,'price':10}]}, 1))
        self.assertIsNone(valid_condition([{'material':'M'}], 'M', None))

    def test_scale_validity_and_allocation(self):
        cond = [{"material": "M-1", "valid_from": "01.01.2031", "valid_to": "31.03.2031, 23:59:59 UTC",
                 "price_unit": "10", "number_format": "en-US",
                 "scales": [{"min_qty": "1", "price": "1,000.00"}, {"min_qty": "20", "price": "900.00"}]}]
        c = valid_condition(cond, "M-1", to_datetime("2031-03-31T12:00:00Z"))
        self.assertEqual(scale_price(c, 19), Decimal("100"))
        self.assertEqual(scale_price(c, 20), Decimal("90"))
        self.assertIsNone(valid_condition(cond, "M-1", to_datetime("2031-04-01T00:00:01Z")))
        self.assertEqual(allocate_cents(Decimal("1.00"), {"a": Decimal(1), "b": Decimal(1), "c": Decimal(1)}),
                         {"a": Decimal("0.34"), "b": Decimal("0.33"), "c": Decimal("0.33")})
        self.assertEqual(marginal_bonus(Decimal("150"), [{"from": "0", "to": "100", "percent": "0"},
                                                         {"from": "100", "to": None, "percent": "10"}]), Decimal("5.00"))

    def test_full_bid(self):
        inputs = {
            "brief": {"tender_reference": "T-9", "scenario_date": "2031-02-01T09:00:00Z"},
            "tender": {"positions": [{"position": "1", "quantity": "4", "unit": "Stk", "requirement_ref": "Z-1"},
                                     {"position": "2", "quantity": "2", "unit": "Satz", "requirement_ref": "Z-2"}],
                       "clarifications": [{"number": "5", "date": "2031-01-01", "positions": ["1"], "setup_fee": "10,00 EUR",
                                           "keys": {"konditionsgruppe": "K-1", "packaging_tariff": "P-1"}}]},
            "quantities": {"1": {"quantity": 4}, "2": {"quantity": 2}},
            "requirements": {"Z-1": {}, "Z-2": {"pieces_per_set": "3"}},
            "materials": {"1": {"material": "M-1"}, "2": {"material": "M-1"}},
            "erp": {"materials": {"M-1": {"pieces_per_carton": "3", "surcharge_mass_g": "500", "metal_index": "IX",
                                          "lead_time_stock_weeks": "2", "lead_time_production_weeks": "8"}},
                    "conditions": [{"material": "M-1", "valid_from": "2031-01-01", "valid_to": "2031-12-31",
                                    "price_unit": "1", "currency": "EUR", "scales": [{"min_qty": "1", "price": "100.00"}]}],
                    "stock": {"M-1": {"free": "10"}}},
            "discount": {"percent": "10", "min_order_value": "500"},
            "mail": {"condition_rules": {"K-1": "base_and_surcharge"}, "packaging_tariffs": {"P-1": "6"},
                     "price_indices": {"IX": "4"}},
        }
        bid = compute(inputs)
        answer = bid["answer"]
        p1, p2 = answer["positions"]
        # pos 1: base 100*0.9 + surcharge 2*0.9 + (setup 10 + 2 cartons*6)/4 = 90 + 1.8 + 5.5
        self.assertEqual(p1["unit_price"], 97.30)
        self.assertEqual(p1["surcharge_per_unit"], 4.50)
        self.assertEqual(p1["packaging_per_unit"], 3.00)
        # pos 2: 6 pieces, default base-only: 90 + 2
        self.assertEqual((p2["quantity"], p2["unit_price"]), (6, 92.00))
        self.assertEqual(p1["lead_time_weeks"], 2)          # pooled demand 4 + 6 = 10 <= 10 free: from stock
        self.assertEqual(answer["total_net"], round(4 * 97.30 + 6 * 92.00, 2))
        self.assertEqual(answer["discount_percent"], 10.0)
        inputs["erp"]["stock"]["M-1"]["free"] = "9"           # not enough for both: all-or-nothing -> production
        self.assertEqual({p["lead_time_weeks"] for p in compute(inputs)["answer"]["positions"]}, {8})

    def test_submitter_table_lists_values_and_omissions(self):
        from agents.submit_bid import bid_table
        inputs = {"brief": {"tender_reference": "T", "scenario_date": "2031-02-01"},
                  "tender": {"positions": [{"position": "1", "quantity": "2", "unit": "Stk", "requirement_ref": "Z"},
                                           {"position": "2", "quantity": "1", "unit": "Stk", "requirement_ref": "Z"}]},
                  "quantities": {"1": {"quantity": 2}, "2": {"quantity": 1}}, "requirements": {"Z": {}},
                  "materials": {"1": {"material": "M"}, "2": {"material": None}},
                  "erp": {"materials": {"M": {"bore": "50", "lead_time_production_weeks": "5"}},
                          "conditions": [{"material": "M", "price_unit": "1", "scales": [{"min_qty": "1", "price": "1234.5"}]}]}}
        bid = compute(inputs)
        table = bid_table(bid, inputs["tender"])
        self.assertIn("1.234,50 EUR", table)
        self.assertIn("bore 50", table)
        self.assertIn("Position 2: OMIT", table)
        self.assertEqual(bid["total_display"], "2.469,00")

    def test_threshold_failure_means_no_discount_and_missing_material_omits(self):
        inputs = {"brief": {"tender_reference": "T", "scenario_date": "2031-02-01"},
                  "tender": {"positions": [{"position": "1", "quantity": "1", "unit": "Stk", "requirement_ref": "Z"},
                                           {"position": "2", "quantity": "1", "unit": "Stk", "requirement_ref": "Z"}]},
                  "quantities": {"1": {"quantity": 1}, "2": {"quantity": 1}}, "requirements": {"Z": {}},
                  "materials": {"1": {"material": "M"}, "2": {"material": None, "reason": "no exact match"}},
                  "erp": {"materials": {"M": {"lead_time_production_weeks": "5"}},
                          "conditions": [{"material": "M", "price_unit": "1", "scales": [{"min_qty": "1", "price": "10"}]}]},
                  "discount": {"percent": "5", "min_order_value": "1000"}}
        bid = compute(inputs)
        self.assertEqual(bid["answer"]["discount_percent"], 0.0)
        self.assertEqual(bid["omitted"], ["2"])
        self.assertEqual(bid["answer"]["positions"][0]["unit_price"], 10.0)


class Matching(unittest.TestCase):
    def test_exact_active_match_only(self):
        tender = {"positions": [{"position": "1", "requirement_ref": "Z"}]}
        reqs = {"Z": {"bore": "50", "stroke": "300", "pressure": "160", "mounting": "Flansch", "certification": "C-1",
                      "kataloggruppe": "G"}}
        mats = {"A": {"material": "A", "bore": "50", "stroke": "300", "pressure": "160", "mounting": "Flansch",
                      "certification": "C-1", "kataloggruppe": "G", "is_active": False},
                "B": {"material": "B", "bore": "50", "stroke": "300", "pressure": "160 bar", "mounting": "flansch",
                      "certification": "C-1", "kataloggruppe": "G", "is_active": True},
                "C": {"material": "C", "bore": "50", "stroke": "310", "pressure": "160", "mounting": "Flansch",
                      "certification": "C-1", "kataloggruppe": "G", "is_active": True}}
        self.assertEqual(match_positions(tender, reqs, mats, {}, "")["1"]["material"], "B")

    def test_attribute_not_shown_is_unverified_not_a_mismatch(self):
        tender = {"positions": [{"position": "1", "requirement_ref": "Z"}]}
        reqs = {"Z": {"bore": "50", "stroke": "300", "pressure": "160", "mounting": "Flansch", "certification": "C-1",
                      "kataloggruppe": "G"}}
        mats = {"A": {"material": "A", "bore": "50", "stroke": "300", "pressure": "160", "mounting": "Flansch",
                      "kataloggruppe": "G", "is_active": True},                       # certification not shown
                "B": {"material": "B", "bore": "50", "stroke": "300", "pressure": "160", "mounting": "Flansch",
                      "certification": "C-2", "kataloggruppe": "G", "is_active": True}}  # contradicts
        out = match_positions(tender, reqs, mats, {}, "")["1"]
        self.assertIsNone(out["material"])
        self.assertEqual(out["candidates"][0]["unverified"], ["certification"])

    def test_usable_travel_without_reserve_remains_unverified(self):
        tender = {"positions": [{"position": "1", "requirement_ref": "Z"}]}
        reqs = {"Z": {"bore": "50", "stroke": "300", "stroke_kind": "usable", "pressure": "160", "mounting": "F",
                      "certification": "keine", "kataloggruppe": "G"}}
        base = {"bore": "50", "pressure": "160", "mounting": "F", "certification": "keine", "kataloggruppe": "G",
                "is_active": True}
        mats = {"S": {"material": "S", "stroke": "290", **base}, "M": {"material": "M", "stroke": "320", **base},
                "L": {"material": "L", "stroke": "360", **base}}
        self.assertIsNone(match_positions(tender, reqs, mats, {}, "")["1"]["material"])
        mail = {"technical_release": {"K": {"revision": "R", "reserve_mm_per_end": "30"}}}
        reqs["Z"]["reserve_key"] = "K"
        self.assertEqual(match_positions(tender, reqs, mats, mail, "R")["1"]["material"], "L")

    def test_usable_travel_without_reserve_takes_the_only_stroke_above_it(self):
        tender = {"positions": [{"position": "1", "requirement_ref": "Z"}]}
        reqs = {"Z": {"bore": "50", "stroke": "300", "stroke_kind": "usable", "pressure": "160", "mounting": "F",
                      "certification": "keine", "kataloggruppe": "G"}}
        base = {"bore": "50", "pressure": "160", "mounting": "F", "certification": "keine", "kataloggruppe": "G",
                "is_active": True}
        mats = {"E": {"material": "E", "stroke": "300", **base}, "L": {"material": "L", "stroke": "360", **base},
                "B": {"material": "B", "stroke": "340", **base, "is_active": False}}
        out = match_positions(tender, reqs, mats, {}, "")["1"]
        self.assertEqual((out["material"], out["unverified"]), ("L", ["end reserve"]))
        mats["M"] = {"material": "M", "stroke": "320", **base}       # two fit: the reserve decides, no guess
        self.assertIsNone(match_positions(tender, reqs, mats, {}, "")["1"]["material"])

    def test_record_status_beats_list_and_disagreement_is_unknown(self):
        from agents.analyze_materials import merge_materials
        rows = lambda *r: [dict(material="A", **x) for x in r]  # noqa: E731
        merged = merge_materials([{"kind": "list", "materials": rows({"bore": "50", "is_active": False})},
                                  {"kind": "record", "materials": rows({"stroke": "300", "is_active": True})}])
        self.assertEqual((merged["A"]["is_active"], merged["A"]["bore"], merged["A"]["stroke"]), (True, "50", "300"))
        merged = merge_materials([{"kind": "record", "materials": rows({"is_active": True})},
                                  {"kind": "record", "materials": rows({"is_active": False})}])
        self.assertIsNone(merged["A"]["is_active"])

    def test_search_candidates_drop_what_the_list_rules_out(self):
        from agents.analyze_materials import search_candidates
        item = {"group": "G", "requirements": [{"bore": "50", "stroke": "300", "stroke_kind": "nominal",
                                                 "pressure": "160", "mounting": "F", "certification": "keine"}]}
        page = {"kind": "list", "materials": [
            {"material": "A", "kataloggruppe": "G", "bore": "50", "stroke": "300", "pressure": "160", "mounting": "F"},
            {"material": "B", "kataloggruppe": "G", "bore": "50", "stroke": "300", "pressure": "250", "mounting": "F"},
            {"material": "C", "kataloggruppe": "H", "bore": "50", "stroke": "300", "pressure": "160", "mounting": "F"}]}
        self.assertEqual(search_candidates({"p": page}, item), ["A"])

    def test_no_certification_spellings_match(self):
        from agents.analyze_materials import text_equal
        self.assertTrue(text_equal("Keine gefordert", "keine"))
        self.assertTrue(text_equal("—", "Keine"))
        self.assertFalse(text_equal("C-1", "keine"))


if __name__ == "__main__":
    unittest.main()


class GuardThenClick(unittest.TestCase):
    def test_a_gate_fill_never_follows_a_link_away(self):
        from swarm.guard import safe_then_click
        item = {"buttons": [{"label": "Original öffnen", "away": True}, {"label": "Weiter", "away": False}]}
        self.assertEqual(safe_then_click({"then_click": "Original öffnen"}, item), "")
        self.assertEqual(safe_then_click({"then_click": "Weiter"}, item), "Weiter")
        self.assertEqual(safe_then_click({}, item), "")


class ConditionFragments(unittest.TestCase):
    def test_header_and_scales_pages_become_one_condition(self):
        from agents.analyze_materials import merge_condition_fragments
        from agents.compute_bid import valid_condition, scale_price
        from swarm.normalize import to_datetime
        header = {"material": "M-9", "valid_from": "01.01.2030", "valid_to": "31.12.2030", "price_unit": "100 ST",
                  "currency": "EUR", "scales": []}
        scales = {"material": "M-9", "number_format": "en-US",
                  "scales": [{"min_qty": "1", "price": "212,063"}, {"min_qty": "25", "price": "186,615"}]}
        merged = merge_condition_fragments([header, scales])
        self.assertEqual(len(merged), 1)
        cond = valid_condition(merged, "M-9", to_datetime("2030-03-10"))
        self.assertEqual(str(scale_price(cond, 130)), "1866.15")

    def test_ambiguous_fragments_are_left_alone(self):
        from agents.analyze_materials import merge_condition_fragments
        a = {"material": "M-9", "valid_from": "01.01.2030", "scales": []}
        b = {"material": "M-9", "valid_from": "01.07.2030", "scales": []}
        frag = {"material": "M-9", "scales": [{"min_qty": "1", "price": "1"}]}
        self.assertEqual(len(merge_condition_fragments([a, b, frag])), 3)


class SameValidityDuplicates(unittest.TestCase):
    def test_a_header_copy_without_scales_does_not_hide_the_priced_condition(self):
        from agents.analyze_materials import merge_condition_fragments
        from agents.compute_bid import valid_condition, scale_price
        from swarm.normalize import to_datetime
        header = {"material": "M-2", "valid_from": "01.01.2030", "valid_to": "31.12.2030", "price_unit": "100 ST",
                  "currency": "USD", "scales": []}
        full = {"material": "M-2", "valid_from": "01.01.2030", "valid_to": "31.12.2030", "price_unit": "100 ST",
                "currency": "USD", "number_format": "en-US", "scales": [{"min_qty": "1", "price": "50,000"}]}
        old = {"material": "M-2", "valid_from": "01.01.2029", "valid_to": "31.12.2029", "price_unit": "1 ST",
               "scales": [{"min_qty": "1", "price": "1"}]}
        on = to_datetime("2030-03-10")
        merged = merge_condition_fragments([header, full, old])
        self.assertEqual(len(merged), 2)                                   # the two 2030 copies became one
        self.assertEqual(str(scale_price(valid_condition(merged, "M-2", on), 10)), "500")
        # and without the merge, the choice itself prefers the copy with scales
        self.assertTrue(valid_condition([header, full], "M-2", on).get("scales"))
