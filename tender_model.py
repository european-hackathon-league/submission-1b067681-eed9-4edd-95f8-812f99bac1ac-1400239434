"""One tender as one JSON: what was decided, and what each decision rests on.

The bid the portal wants is small - reference, decision, discount, positions, total. Getting it right is not,
because almost every number is chosen from several candidates by date, by exact reference and by authorised
sender. This module keeps the chosen value and its evidence together, so a wrong answer can be traced to the
record it came from instead of being re-derived by hand.

`build_answer` drops the evidence and returns exactly the fields the run's answer_schema asks for.
"""
from __future__ import annotations

from typing import Any

# Every value the agent picks carries one of these. `record` is the id on the site (a Q&A number, a message
# subject, a material number), `dated` the instant that made it win, `url` where to look again.
EVIDENCE_FIELDS = ("site", "url", "record", "dated", "note")

REQUIRED_POSITION = ("position", "material_number", "quantity", "unit_price", "lead_time_weeks")
OPTIONAL_POSITION = ("base_unit_price", "surcharge_per_unit", "packaging_per_unit", "rebate_total")


def evidence(site: str, url: str = "", record: str = "", dated: str = "", note: str = "") -> dict:
    """Where a value came from. `dated` is the ISO instant the 'latest record wins' rules compare."""
    return {"site": site, "url": url, "record": record, "dated": dated, "note": note}


def decided(value: Any, *why: dict) -> dict:
    """A chosen value together with the records behind it, and the ones it beat."""
    return {"value": value, "evidence": list(why)}


def position(number: str, requirement_ref: str = "") -> dict:
    """An empty position. Each field is filled with `decided(...)` as it is resolved.

    `keys` holds what the tender Q&A assigns but does not itself resolve: the packaging tariff, the delivery
    commitment, the setup pool, the bonus plan and the condition group are all keys whose values live in the
    sales mailbox, so they are kept separate from the values they later produce.
    """
    return {
        "position": number,
        "requirement_ref": requirement_ref,
        "keys": {"packaging": None, "delivery": None, "setup_pool": None,
                 "bonus_plan": None, "condition_group": None},
        "requirement": None,      # bore, stroke, pressure, mounting, certification, after amendments
        "quantity": None,         # final pieces, after clarifications, withdrawals and Satz conversion
        "material_number": None,
        "unit_price": None,
        "lead_time_weeks": None,
        "base_unit_price": None,
        "surcharge_per_unit": None,
        "packaging_per_unit": None,
        "rebate_total": None,
        "omitted": False,         # the bid form can leave a position out for partial credit
    }


def tender(reference: str, scenario_date: str = "", currency: str = "EUR") -> dict:
    return {
        "tender_reference": reference,
        "scenario_date": scenario_date,
        "currency": currency,
        "decision": None,         # "bid" or "no_bid"
        "discount_percent": None,
        "positions": [],
        "total_net": None,
        "unresolved": [],         # what could not be decided, so a partial bid can still be signed
    }


def _value(field: Any) -> Any:
    return field.get("value") if isinstance(field, dict) and "value" in field else field


def missing(state: dict) -> list[str]:
    """Every required field that is still undecided, as 'position 01.02: unit_price'."""
    gaps = []
    for field in ("decision", "discount_percent", "total_net"):
        if _value(state.get(field)) is None:
            gaps.append(field)
    for pos in state["positions"]:
        if pos.get("omitted"):
            continue
        for field in REQUIRED_POSITION:
            if _value(pos.get(field)) is None:
                gaps.append(f"position {pos['position']}: {field}")
    return gaps


def build_answer(state: dict) -> dict:
    """The answer as the run's schema wants it: values only, no evidence, omitted positions left out.

    A no_bid carries no positions and a zero total, whatever was resolved along the way.
    """
    decision = _value(state.get("decision"))
    answer: dict = {
        "tender_reference": state["tender_reference"],
        "decision": decision,
        "currency": state.get("currency") or "EUR",
        "discount_percent": _value(state.get("discount_percent")),
        "positions": [],
        "total_net": _value(state.get("total_net")),
    }
    if decision == "no_bid":
        answer["positions"], answer["total_net"] = [], 0
        return answer
    for pos in state["positions"]:
        if pos.get("omitted"):
            continue
        entry = {field: _value(pos.get(field)) for field in REQUIRED_POSITION}
        for field in OPTIONAL_POSITION:          # blank means unanswered; 0.00 means a known zero
            if _value(pos.get(field)) is not None:
                entry[field] = _value(pos.get(field))
        answer["positions"].append(entry)
    return answer


def trace(state: dict) -> list[str]:
    """One line per decided value: what it is, and which record it rests on. For a quick human check."""
    lines = []

    def add(label: str, field: Any) -> None:
        if isinstance(field, dict) and "value" in field:
            for ev in field.get("evidence") or [{}]:
                lines.append(f"{label} = {field['value']}  <- {ev.get('site','?')} "
                             f"{ev.get('record','')} {ev.get('dated','')} {ev.get('note','')}".rstrip())

    add("decision", state.get("decision"))
    add("discount_percent", state.get("discount_percent"))
    for pos in state["positions"]:
        for field in REQUIRED_POSITION + OPTIONAL_POSITION:
            add(f"{pos['position']}.{field}", pos.get(field))
    return lines
