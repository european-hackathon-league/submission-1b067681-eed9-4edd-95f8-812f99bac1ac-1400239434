"""Deterministic bid arithmetic for the RFQ bid rules (plain code, no model, no scenario values).

The model-driven agents find the facts on the websites; these pure functions turn those facts into the numbers the
configurator wants. Every rounding is HALF_UP to cents and happens exactly where the rules say, never earlier.
All money is Decimal. Callers pass numbers as found (str/int/float/Decimal); `D()` converts them safely.

Rule numbers in the comments refer to the bid rules in the run's brief.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
ZERO = Decimal("0")

# The websites pad identifiers and numbers with invisible characters, so text copied from a page does not compare
# equal to what it looks like. Strip them before matching, computing or entering anything.
INVISIBLE = dict.fromkeys(map(ord, "​‌‍⁠﻿­"), None)


def plain(text) -> str:
    """Text without invisible padding, whitespace collapsed (non-breaking spaces count as spaces)."""
    return " ".join(str(text or "").translate(INVISIBLE).replace(" ", " ").split())


def D(value) -> Decimal:
    """A Decimal from what a page shows: accepts German formats like '1.234,56' or '12,5 %'."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int,)):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(repr(value))
    text = plain(value).replace(" ", "").replace("%", "").replace("€", "")
    text = text.replace("EUR", "")
    if "," in text and "." in text:
        # the later separator is the decimal one: 1.234,56 (German) or 1,234.56 (English)
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    return Decimal(text)


def round_cents(value) -> Decimal:
    """HALF_UP to cents (rules 4, 7, 16, 22, 23, 26, 27)."""
    return D(value).quantize(CENT, rounding=ROUND_HALF_UP)


# --- quantities (rules 1, 13, 17) ------------------------------------------------------------------------------------

def final_pieces(quantity, unit: str = "", pieces_per_set: int | None = None) -> int:
    """A tender/Q&A quantity in its stated unit, as pieces: a 'Satz' (set) is converted with the governing
    specification's pieces per set; anything else is already pieces."""
    qty = D(quantity)
    if unit.strip().lower().startswith("satz"):
        if not pieces_per_set:
            raise ValueError("quantity is in sets but pieces_per_set is unknown")
        qty *= pieces_per_set
    if qty != qty.to_integral_value():
        raise ValueError(f"non-integral piece quantity {qty}")
    return int(qty)


@dataclass
class QuantityChange:
    number: str            # the Q&A answer number
    when: date
    quantity: object       # in the position's stated unit
    withdraws: str | None = None   # number of an earlier answer this one withdraws (rule 13)


def resolve_quantity(tender_quantity, changes: list[QuantityChange]):
    """Rule 1 + 13: the latest remaining change wins; withdrawn answers are ignored; else the tender quantity.
    `changes` are the Q&A answers for ONE position (withdrawals may be listed among them without a quantity)."""
    withdrawn = {c.withdraws for c in changes if c.withdraws}
    remaining = [c for c in changes if c.number not in withdrawn and c.quantity is not None and not c.withdraws]
    if not remaining:
        return tender_quantity
    return max(remaining, key=lambda c: (c.when, c.number)).quantity


# --- prices (rules 4, 22, 28) ----------------------------------------------------------------------------------------

@dataclass
class Scale:
    min_quantity: int      # the scale applies from this many pieces
    price: object          # in the condition's currency, per `price_unit` pieces


def scale_price(scales: list[Scale], pieces: int) -> Decimal:
    """Rule 4: the price of the highest quantity scale reached by `pieces` (scales are per position, rule 15)."""
    reached = [s for s in scales if pieces >= s.min_quantity]
    if not reached:
        raise ValueError(f"no price scale reached by {pieces} pieces")
    return D(max(reached, key=lambda s: s.min_quantity).price)


def base_unit_price_eur(scales: list[Scale], pieces: int, price_unit: int = 1, eur_per_unit=1) -> Decimal:
    """Rule 22: normalise the price unit, pick the scale, convert with EUR per currency unit, HALF_UP to cents.
    This rounded value is THE base unit price for every later calculation and threshold."""
    per_piece = scale_price(scales, pieces) / D(price_unit)
    return round_cents(per_piece * D(eur_per_unit))


@dataclass
class RateWindow:
    rate: object
    valid_from: date | None = None
    valid_to: date | None = None    # inclusive


def rate_on(windows: list[RateWindow], on: date) -> Decimal:
    """Rule 28: the rate whose inclusive validity window contains the scenario date (not the last one listed)."""
    hits = [w for w in windows
            if (w.valid_from is None or w.valid_from <= on) and (w.valid_to is None or on <= w.valid_to)]
    if not hits:
        raise ValueError(f"no rate valid on {on}")
    # several open-ended windows: the one that started latest is the current one
    return D(max(hits, key=lambda w: w.valid_from or date.min).rate)


# --- discount (rules 5, 12, 16) --------------------------------------------------------------------------------------

def undiscounted_net(lines: list[tuple[int, Decimal]]) -> Decimal:
    """Rule 16: sum of final pieces x base unit price, before any discount, without surcharges/setup, HALF_UP."""
    return round_cents(sum((D(q) * D(p) for q, p in lines), ZERO))


def order_discount(percent, threshold=None, net=None, decision: str = "bid") -> Decimal:
    """Rule 5 + 16: the approved percentage, or 0 when its minimum net order value (inclusive) is not reached.
    No fallback to older approvals; a conditional approval on a no_bid yields 0."""
    pct = D(percent or 0)
    if threshold is None:
        return pct
    if decision != "bid" or net is None:
        return ZERO
    return pct if D(net) >= D(threshold) else ZERO


BASE_ONLY = "base_only"
BASE_AND_SURCHARGE = "base_and_surcharge"
NO_DISCOUNT = "none"


# --- allocations (rules 25, 26) --------------------------------------------------------------------------------------

def allocate_cents(total, weights: dict[str, object]) -> dict[str, Decimal]:
    """Split `total` (EUR, already in cents) proportionally to `weights` with the largest-remainder method in cents;
    ties go to the ascending key. Rule 26 names this method; rule 25 points to the Rüstpools mail: check that the
    mail's cent rule is the same before relying on it for the setup pool."""
    cents = int((round_cents(total) * 100).to_integral_value())
    weight = {k: D(v) for k, v in weights.items()}
    whole = sum(weight.values(), ZERO)
    if not weight or whole == 0:
        return {k: ZERO for k in weights}
    exact = {k: D(cents) * w / whole for k, w in weight.items()}
    floor = {k: int(math.floor(v)) for k, v in exact.items()}
    left = cents - sum(floor.values())
    order = sorted(weight, key=lambda k: (-(exact[k] - floor[k]), k))
    for k in order[:left]:
        floor[k] += 1
    return {k: (D(v) / 100).quantize(CENT) for k, v in floor.items()}


@dataclass
class Band:
    lower: object                  # EUR, the band starts above this
    upper: object | None           # EUR, None = no upper end
    percent: object


def marginal_credit(total, bands: list[Band]) -> Decimal:
    """Rule 26: apply each band's percentage only to the part of `total` inside it; HALF_UP the aggregate."""
    total = D(total)
    credit = ZERO
    for band in bands:
        lo = D(band.lower)
        hi = total if band.upper is None else min(total, D(band.upper))
        if hi > lo:
            credit += (hi - lo) * D(band.percent) / 100
    return round_cents(credit)


# --- delivery (rules 6, 15, 24) --------------------------------------------------------------------------------------

def stock_covers(stock: int, pooled_demand: int) -> bool:
    """Rule 15: stock must cover the WHOLE pooled demand of a material, else every position uses production."""
    return stock >= pooled_demand


def lead_time_weeks(stock: int, pooled_demand: int, stock_weeks: int, production_weeks: int,
                    commitment_weeks: int | None = None, commitment_ceiling: int | None = None) -> int:
    """Rule 24 then 15/6: a matching commitment within its inclusive ceiling wins, else shared-stock delivery."""
    if commitment_weeks is not None and commitment_ceiling is not None and pooled_demand <= commitment_ceiling:
        return commitment_weeks
    return stock_weeks if stock_covers(stock, pooled_demand) else production_weeks


# --- packaging (rule 23) ---------------------------------------------------------------------------------------------

def carton_cost(pieces: int, pieces_per_carton: int, price_per_carton) -> Decimal:
    """Rule 23: ceil(final pieces / carton size) cartons per position; the bid quantity never changes."""
    cartons = math.ceil(pieces / pieces_per_carton)
    return D(cartons) * D(price_per_carton)


# --- one position, the whole bid (rules 7, 12, 18, 25, 27) -----------------------------------------------------------

@dataclass
class PositionFacts:
    position: str
    material_number: str
    pieces: int
    base_unit_price: Decimal                 # from base_unit_price_eur(): EUR per piece, already rounded (rule 22)
    lead_time_weeks: int
    surcharge_per_piece: Decimal = ZERO      # mass surcharge in EUR per piece, unrounded (rule 12: before discount)
    setup_fee: Decimal = ZERO                # ordinary setup fee for the position, EUR
    shared_setup: Decimal = ZERO             # this position's allocation of the shared setup pool, EUR (rule 25)
    carton_cost: Decimal = ZERO              # carton_cost(...) for the position, EUR (rule 23)
    project_bonus: Decimal = ZERO            # allocated project bonus credit for the WHOLE position, EUR (rule 26)
    clause: str = BASE_ONLY                  # rule 12: BASE_ONLY | BASE_AND_SURCHARGE | NO_DISCOUNT


@dataclass
class PricedPosition:
    entry: dict                              # the configurator entry, matching the answer schema's position item
    line_total: Decimal


def price_position(facts: PositionFacts, discount_percent) -> PricedPosition:
    """Rule 27: clause-discounted base + clause-discounted surcharge + (setup + shared setup + cartons - bonus) / pieces,
    from unrounded per-piece parts, rounded HALF_UP once. Breakdown fields are reports only (rule 18)."""
    if facts.pieces <= 0:
        raise ValueError(f"position {facts.position}: no pieces")
    keep = 1 - D(discount_percent) / 100
    base = D(facts.base_unit_price) * (keep if facts.clause in (BASE_ONLY, BASE_AND_SURCHARGE) else 1)
    surcharge = D(facts.surcharge_per_piece) * (keep if facts.clause == BASE_AND_SURCHARGE else 1)
    fees = D(facts.setup_fee) + D(facts.shared_setup) + D(facts.carton_cost) - D(facts.project_bonus)
    unit_price = round_cents(base + surcharge + fees / facts.pieces)
    entry = {
        "position": plain(facts.position),
        "material_number": plain(facts.material_number),
        "quantity": facts.pieces,
        "unit_price": float(unit_price),
        "lead_time_weeks": facts.lead_time_weeks,
        "base_unit_price": float(round_cents(facts.base_unit_price)),
        # rule 25: surcharge reported before discount, setup allocations included, packaging and bonus excluded
        "surcharge_per_unit": float(round_cents(
            D(facts.surcharge_per_piece) + (D(facts.setup_fee) + D(facts.shared_setup)) / facts.pieces)),
        "packaging_per_unit": float(round_cents(D(facts.carton_cost) / facts.pieces)),
        "rebate_total": float(round_cents(facts.project_bonus)),
    }
    return PricedPosition(entry=entry, line_total=D(facts.pieces) * unit_price)


@dataclass
class Bid:
    tender_reference: str
    decision: str = "bid"                    # "bid" | "no_bid"
    discount_percent: Decimal = ZERO
    positions: list[PositionFacts] = field(default_factory=list)
    notes: str = ""


def answer(bid: Bid) -> dict:
    """The answer object for the configurator / answer_schema. no_bid: no positions, total 0 (rule 8).
    Partially known bids still go out: leave unknown positions out and say so in `notes` (rule 9).
    The tender reference decides eligibility, so it goes out without invisible padding."""
    reference = plain(bid.tender_reference)
    if bid.decision == "no_bid":
        return {"tender_reference": reference, "decision": "no_bid", "currency": "EUR",
                "discount_percent": 0, "positions": [], "total_net": 0, "notes": bid.notes}
    priced = [price_position(p, bid.discount_percent) for p in bid.positions]
    total = round_cents(sum((p.line_total for p in priced), ZERO))   # rule 7: sum of qty x rounded unit price
    return {"tender_reference": reference, "decision": "bid", "currency": "EUR",
            "discount_percent": float(D(bid.discount_percent)), "positions": [p.entry for p in priced],
            "total_net": float(total), "notes": bid.notes}
