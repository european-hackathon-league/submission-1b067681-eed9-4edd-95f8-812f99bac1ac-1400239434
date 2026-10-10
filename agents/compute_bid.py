# OWNER: (name)   ROLE: calculator (plain code, no model)
# NEEDS: brief, tender, quantities, requirements, materials, erp, discount, mail   WRITES: results["bid"], results["answer"]
"""Deterministic pricing, lead times and allocations per the brief's rules. Every input that is missing only removes
the fields that depend on it: a position without material/price is omitted, an unknown surcharge leaves the
surcharge field blank, and so on (a signed partial bid beats nothing).

results["bid"] = {"answer": <answer schema dict>, "omitted": [positions], "lines": [{position, display strings,
                  material attributes for the configurator, notes}], "issues": [str]}
results["answer"] = results["bid"]["answer"]
"""
import math
from decimal import ROUND_FLOOR, Decimal

from agents.analyze_quantities import withdrawn_numbers
from swarm import Agent, Phase, register
from swarm.normalize import de_number, norm_ref, round_cents, to_datetime, to_decimal

D0 = Decimal(0)


def is_set_unit(unit) -> bool:
    u = str(unit or "").strip().lower()
    return u.startswith("satz") or u.startswith("set") or u.startswith("sätze")


def valid_condition(conditions: list[dict], material: str, scenario) -> dict | None:
    """The material's price condition valid on the scenario date (both endpoints inclusive); latest start wins."""
    valid = []
    for c in conditions:
        if norm_ref(c.get("material")) != norm_ref(material):
            continue
        start, end = to_datetime(c.get("valid_from")), to_datetime(c.get("valid_to"))
        if end and end.hour == 0 and end.minute == 0 and end.second == 0:
            end = end.replace(hour=23, minute=59, second=59)
        if scenario is None or ((start is None or start <= scenario) and (end is None or scenario <= end)):
            valid.append((start or to_datetime("1970-01-01"), c))
    return max(valid, key=lambda x: x[0])[1] if valid else None


def scale_price(condition: dict, pieces: int) -> Decimal | None:
    """Per-piece price at the highest scale whose minimum quantity <= pieces, divided by the price unit."""
    fmt = condition.get("number_format")
    rows = []
    for s in condition.get("scales") or []:
        q, p = to_decimal(s.get("min_qty"), fmt), to_decimal(s.get("price"), fmt)
        if q is not None and p is not None and q <= pieces:
            rows.append((q, p))
    if not rows:
        return None
    unit = to_decimal(condition.get("price_unit")) or Decimal(1)
    return max(rows)[1] / (unit if unit > 0 else Decimal(1))


def allocate_cents(total: Decimal, weights: dict[str, Decimal]) -> dict[str, Decimal]:
    """Split `total` (EUR) proportionally in cents: floor, then remaining cents to the largest remainders,
    ties by ascending position identifier."""
    cents = int((total * 100).to_integral_value())
    wsum = sum(weights.values(), D0)
    if wsum <= 0 or not weights:
        return {k: D0 for k in weights}
    exact = {k: Decimal(cents) * w / wsum for k, w in weights.items()}
    floors = {k: int(v.to_integral_value(rounding=ROUND_FLOOR)) for k, v in exact.items()}
    rest = cents - sum(floors.values())
    order = sorted(weights, key=lambda k: (-(exact[k] - floors[k]), k))
    for k in order[:rest]:
        floors[k] += 1
    return {k: Decimal(v) / 100 for k, v in floors.items()}


def marginal_bonus(base_total: Decimal, bands: list[dict]) -> Decimal:
    credit = D0
    for b in bands:
        lo = to_decimal(b.get("from")) or D0
        hi = to_decimal(b.get("to")) if b.get("to") not in (None, "", "null") else None
        pct = to_decimal(b.get("percent")) or D0
        top = base_total if hi is None else min(base_total, hi)
        if top > lo:
            credit += (top - lo) * pct / 100
    return round_cents(credit)


def position_keys(clarifications: list[dict], position: str) -> dict:
    """Commercial keys and setup fee the latest Q&A answers assign to a position (withdrawn answers ignored)."""
    withdrawn = withdrawn_numbers(clarifications)
    keys, fee = {}, None
    ordered = sorted(clarifications, key=lambda c: to_datetime(c.get("date")) or to_datetime("1970-01-01"))
    for c in ordered:
        if norm_ref(c.get("number")) in withdrawn or position not in [str(x).strip() for x in c.get("positions") or []]:
            continue
        for k, v in (c.get("keys") or {}).items():
            if v not in (None, "", "null"):
                keys[k] = v
        if c.get("setup_fee") not in (None, "", "null"):
            fee = to_decimal(c["setup_fee"], "de-DE" if "," in str(c["setup_fee"]) else None)
    return {"keys": keys, "setup_fee": fee}


def compute(inputs: dict) -> dict:
    brief, tender = inputs.get("brief") or {}, inputs.get("tender") or {}
    quantities, requirements = inputs.get("quantities") or {}, inputs.get("requirements") or {}
    matched, erp = inputs.get("materials") or {}, inputs.get("erp") or {}
    discount, mail = inputs.get("discount") or {}, inputs.get("mail") or {}
    scenario = to_datetime(brief.get("scenario_date"))
    erp_materials = {norm_ref(k): v for k, v in (erp.get("materials") or {}).items()}
    issues, omitted, lines = [], [], {}
    clarifications = tender.get("clarifications") or []

    # 1. per position: pieces, material, EUR base price
    for p in tender.get("positions") or []:
        pos = str(p.get("position")).strip()
        q = (quantities.get(pos) or {}).get("quantity")
        req = next((v for k, v in requirements.items() if norm_ref(k) == norm_ref(p.get("requirement_ref"))), {}) or {}
        mat = (matched.get(pos) or {}).get("material")
        line = {"position": pos, "material": mat, "unit": p.get("unit"), "required": (matched.get(pos) or {}).get("required"),
                "short_text": p.get("short_text"), **position_keys(clarifications, pos)}
        lines[pos] = line
        if q is None:
            issues.append(f"{pos}: no quantity")
            continue
        if is_set_unit(p.get("unit")):
            per_set = to_decimal(req.get("pieces_per_set"))
            if not per_set:
                issues.append(f"{pos}: quantity in sets but pieces per set unknown")
                continue
            q = int(Decimal(q) * per_set)
        line["pieces"] = int(q)
        if not mat:
            issues.append(f"{pos}: no material ({(matched.get(pos) or {}).get('reason')})")
            continue
        cond = valid_condition(erp.get("conditions") or [], mat, scenario)
        price = scale_price(cond, int(q)) if cond else None
        if price is None:
            issues.append(f"{pos}: no valid price condition/scale for {mat}")
            continue
        currency = norm_ref((cond or {}).get("currency") or "EUR")
        if currency != "EUR":
            rate = to_decimal((mail.get("fx_fixing") or {}).get(currency))
            if rate is None:
                issues.append(f"{pos}: price in {currency}, no FX rate")
                continue
            price = price * rate
        line["base"] = round_cents(price)
        line["erp"] = erp_materials.get(norm_ref(mat), {})
        # the configurator lists the catalogue's own attribute values, so the submitter selects these
        line["configuration"] = {k: line["erp"].get(k) for k in ("bore", "stroke", "pressure", "mounting", "certification")}

    priced = {pos: l for pos, l in lines.items() if "base" in l}
    omitted = [pos for pos in lines if pos not in priced]

    # 2. order-level discount (threshold on undiscounted goods value of all priced positions)
    pct = to_decimal(discount.get("percent")) or D0
    threshold = to_decimal(discount.get("min_order_value"))
    order_value = round_cents(sum((l["pieces"] * l["base"] for l in priced.values()), D0))
    if threshold is not None and order_value < threshold:
        pct = D0
    d = pct / 100

    # 3. shared setup pools, project bonus (allocations in cents)
    pools: dict[str, dict] = {}
    bonus: dict[str, dict] = {}
    for pos, l in priced.items():
        if l["keys"].get("setup_pool"):
            pools.setdefault(norm_ref(l["keys"]["setup_pool"]), {})[pos] = l
        if l["keys"].get("bonus_plan"):
            bonus.setdefault(norm_ref(l["keys"]["bonus_plan"]), {})[pos] = l
    for key, members in pools.items():
        fee = to_decimal((mail.get("setup_pools") or {}).get(key))
        weights = {pos: Decimal(l["pieces"]) * (to_decimal(l["erp"].get("allocation_mass_g")) or D0) for pos, l in members.items()}
        if fee is None or not any(weights.values()):
            for l in members.values():
                l["pool_unknown"] = True
            continue
        for pos, amount in allocate_cents(fee, weights).items():
            members[pos]["pool"] = amount
    for key, members in bonus.items():
        bands = (mail.get("project_bonus") or {}).get(key)
        if not bands:
            for l in members.values():
                l["bonus_unknown"] = True
            continue
        values = {pos: l["pieces"] * l["base"] for pos, l in members.items()}
        credit = marginal_bonus(sum(values.values(), D0), bands)
        for pos, amount in allocate_cents(credit, values).items():
            members[pos]["bonus"] = amount

    # 4. per-position surcharge, packaging, final price
    for pos, l in priced.items():
        n = Decimal(l["pieces"])
        rate = to_decimal((mail.get("price_indices") or {}).get(norm_ref(l["erp"].get("metal_index"))))
        mass = to_decimal(l["erp"].get("surcharge_mass_g"))
        l["mass_surcharge"] = mass / 1000 * rate if (rate is not None and mass is not None) else None
        tariff_key = l["keys"].get("packaging_tariff")
        tariff = to_decimal((mail.get("packaging_tariffs") or {}).get(norm_ref(tariff_key))) if tariff_key else D0
        carton = to_decimal(l["erp"].get("pieces_per_carton"))
        if tariff_key and (tariff is None or not carton):
            l["packaging_total"] = None
        else:
            l["packaging_total"] = (tariff * math.ceil(n / carton)) if tariff_key else D0
        scope = (mail.get("condition_rules") or {}).get(norm_ref(l["keys"].get("konditionsgruppe"))) \
            if l["keys"].get("konditionsgruppe") else "base_only"
        scope = scope or "base_only"
        base_factor = (1 - d) if scope in ("base_only", "base_and_surcharge") else Decimal(1)
        sur_factor = (1 - d) if scope == "base_and_surcharge" else Decimal(1)
        setup = (l.get("setup_fee") or D0) + l.get("pool", D0)
        extras = setup + (l["packaging_total"] or D0) - l.get("bonus", D0)
        final = l["base"] * base_factor + (l["mass_surcharge"] or D0) * sur_factor + extras / n
        l["unit_price"] = round_cents(final)
        l["surcharge_report"] = (round_cents(l["mass_surcharge"] + setup / n)
                                 if l["mass_surcharge"] is not None and not l.get("pool_unknown") else None)
        l["packaging_report"] = round_cents(l["packaging_total"] / n) if l["packaging_total"] is not None else None
        l["rebate_report"] = None if l.get("bonus_unknown") else round_cents(l.get("bonus", D0))

    # 5. lead times: delivery commitment, else shared stock all-or-nothing per material
    demand: dict[str, int] = {}
    for l in priced.values():
        demand[norm_ref(l["material"])] = demand.get(norm_ref(l["material"]), 0) + l["pieces"]
    stock = {norm_ref(k): v for k, v in (erp.get("stock") or {}).items()}
    for pos, l in priced.items():
        m = norm_ref(l["material"])
        commit = (mail.get("delivery_commitments") or {}).get(norm_ref(l["keys"].get("delivery_key"))) \
            if l["keys"].get("delivery_key") else None
        if commit and norm_ref(commit.get("material")) == m and to_decimal(commit.get("max_total_pieces")) is not None \
                and demand[m] <= to_decimal(commit["max_total_pieces"]) and to_decimal(commit.get("weeks")) is not None:
            l["lead"] = int(to_decimal(commit["weeks"]))
            continue
        free = to_decimal((stock.get(m) or {}).get("free"))
        stock_weeks = to_decimal(l["erp"].get("lead_time_stock_weeks"))
        prod_weeks = to_decimal(l["erp"].get("lead_time_production_weeks"))
        chosen = stock_weeks if (free is not None and free >= demand[m]) else prod_weeks
        l["lead"] = int(chosen) if chosen is not None else None
        if l["lead"] is None:
            issues.append(f"{pos}: lead time unknown")

    # 6. answer
    blocked = [pos for pos in lines if (matched.get(pos) or {}).get("reason") == "only blocked matches"]
    decision = "no_bid" if blocked else "bid"
    positions = []
    for pos, l in priced.items():
        if l.get("lead") is None:
            omitted.append(pos)
            continue
        entry = {"position": pos, "material_number": l["material"], "quantity": l["pieces"],
                 "unit_price": float(l["unit_price"]), "lead_time_weeks": l["lead"],
                 "base_unit_price": float(l["base"])}
        for field, key in (("surcharge_per_unit", "surcharge_report"), ("packaging_per_unit", "packaging_report"),
                           ("rebate_total", "rebate_report")):
            if l.get(key) is not None:
                entry[field] = float(l[key])
        positions.append(entry)
        l["display"] = {k: de_number(Decimal(str(v))) for k, v in entry.items()
                        if k in ("unit_price", "base_unit_price", "surcharge_per_unit", "packaging_per_unit", "rebate_total")}
    total = round_cents(sum((Decimal(e["quantity"]) * Decimal(str(e["unit_price"])) for e in positions), D0))
    answer = {"tender_reference": brief.get("tender_reference") or (tender.get("meta") or {}).get("reference") or "",
              "decision": decision, "currency": "EUR", "discount_percent": float(pct if decision == "bid" else D0),
              "positions": positions if decision == "bid" else [], "total_net": float(total) if decision == "bid" else 0.0}
    if decision == "no_bid":
        issues.append(f"no active material for {blocked}: no_bid")
    return {"answer": answer, "omitted": sorted(set(omitted)) if decision == "bid" else [],
            "total_display": de_number(total if decision == "bid" else D0), "order_value": str(order_value),
            "lines": [{k: (str(v) if isinstance(v, Decimal) else v) for k, v in l.items() if k != "erp"}
                      for l in lines.values()],
            "issues": issues}


@register
class BidCalculator(Agent):
    phase = Phase.COMPUTE

    async def run(self) -> None:
        names = ("brief", "tender", "quantities", "requirements", "materials", "erp", "discount", "mail")
        bid = compute({n: self.ctx.board.get(n) for n in names})
        await self.ctx.board.set("bid", bid)
        await self.ctx.board.set("answer", bid["answer"])
        self.log.info("bid: %s omitted=%s issues=%s", bid["answer"], bid["omitted"], bid["issues"])
