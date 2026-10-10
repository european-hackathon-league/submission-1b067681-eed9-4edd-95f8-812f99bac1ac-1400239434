"""The answer the portal gets, and the trail behind it."""
import tender_model as tm


def _position(number, **fields):
    pos = tm.position(number)
    for key, value in fields.items():
        pos[key] = tm.decided(value, tm.evidence("erp", record=str(value)))
    return pos


def _bid():
    state = tm.tender("AB-0000-0001", "2030-01-15T09:00:00Z")
    state["decision"] = tm.decided("bid", tm.evidence("tender-portal"))
    state["discount_percent"] = tm.decided(5.0, tm.evidence(
        "mail", record="Rabattfreigabe AB-0000-0001", dated="2030-01-14T17:00:00Z"))
    state["total_net"] = tm.decided(1234.56)
    state["positions"] = [_position("01.01", material_number="M-1", quantity=25,
                                    unit_price=10.0, lead_time_weeks=4)]
    return state


def test_answer_carries_only_the_schema_fields():
    answer = tm.build_answer(_bid())
    assert set(answer) == {"tender_reference", "decision", "currency",
                           "discount_percent", "positions", "total_net"}
    assert answer["positions"][0] == {"position": "01.01", "material_number": "M-1",
                                      "quantity": 25, "unit_price": 10.0, "lead_time_weeks": 4}
    assert "evidence" not in str(answer)


def test_optional_fields_are_left_out_when_undecided_and_kept_when_zero():
    state = _bid()
    state["positions"][0]["packaging_per_unit"] = tm.decided(0.0)
    answer = tm.build_answer(state)
    assert answer["positions"][0]["packaging_per_unit"] == 0.0   # a known zero is an answer
    assert "rebate_total" not in answer["positions"][0]          # blank means unanswered


def test_omitted_positions_are_dropped_but_the_rest_keep_their_credit():
    state = _bid()
    state["positions"].append(_position("01.02", material_number="M-2", quantity=29,
                                        unit_price=11.0, lead_time_weeks=6))
    state["positions"][1]["omitted"] = True
    answer = tm.build_answer(state)
    assert [p["position"] for p in answer["positions"]] == ["01.01"]


def test_no_bid_empties_the_positions_and_zeroes_the_total():
    state = _bid()
    state["decision"] = tm.decided("no_bid", tm.evidence("erp", note="no active material for 01.03"))
    answer = tm.build_answer(state)
    assert answer["positions"] == [] and answer["total_net"] == 0


def test_missing_names_the_gaps_and_ignores_omitted_positions():
    state = _bid()
    state["positions"][0]["unit_price"] = None
    state["positions"].append(_position("01.02"))
    state["positions"][1]["omitted"] = True
    gaps = tm.missing(state)
    assert "position 01.01: unit_price" in gaps
    assert not any("01.02" in gap for gap in gaps)


def test_trace_points_at_the_record_that_won():
    lines = "\n".join(tm.trace(_bid()))
    assert "discount_percent = 5.0" in lines
    assert "Rabattfreigabe AB-0000-0001" in lines and "2030-01-14T17:00:00Z" in lines
