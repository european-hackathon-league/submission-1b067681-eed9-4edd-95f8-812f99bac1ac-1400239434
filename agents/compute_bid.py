# OWNER: (name)   ROLE: calculator (plain code, no model)
# NEEDS: brief, tender, quantities, requirements, materials, erp, discount, mail   WRITES: results["bid"], results["answer"]
"""Deterministic pricing, lead times and allocations per the brief's rules. Every input that is missing only removes
the fields that depend on it: a position without material/price is omitted, an unknown surcharge leaves the
surcharge field blank, and so on (a signed partial bid beats nothing).

results["bid"] = {"answer": <answer schema dict>, "omitted": [positions], "lines": [{position, display strings,
                  material attributes for the configurator, notes, "fields": {field: status}, "unresolved": [dep id]}],
                  "issues": [str], "dependencies": {dep id: {"id", "role", "need", "positions"}},
                  "unresolved": {position: [dep id]}, "coverage": {"positions", "priced", "complete", "unresolved"}}
results["answer"] = results["bid"]["answer"]
results["bid_draft"] = the same, computed during collection from what is known so far (BidDrafter)

Field status: "verified" (read and applied), "absent" (the sources show it does not apply, e.g. no packaging key in
the Q&A), "estimated: <why>" (an input is missing; the value is a best effort and the dependency is open), "missing".
A dependency id names what to collect, in the coordinator's gap vocabulary (e.g. "erp prices <material>",
"mail packaging_tariffs <key>"), so every estimated field or omitted position has a follow-up job behind it.
"""
import asyncio
import json
import math
from decimal import ROUND_FLOOR, Decimal

from agents.analyze_quantities import withdrawn_numbers
from swarm import Agent, Phase, register
from swarm.normalize import validity_end, de_number, norm_ref, round_cents, to_datetime, to_decimal

D0 = Decimal(0)


def is_set_unit(unit) -> bool:
    u = str(unit or "").strip().lower()
    return u.startswith("satz") or u.startswith("set") or u.startswith("sätze")


def dated(condition: dict) -> bool:
    return bool(condition.get("valid_from") or condition.get("valid_to"))


def valid_condition(conditions: list[dict], material: str, scenario) -> dict | None:
    """The material's price condition valid on the scenario date (both endpoints inclusive); latest start wins, and
    among equal starts a complete one (scales and price unit). A condition without any validity date counts only when
    it is the material's sole condition (its validity is then unverified: `condition_status` says so)."""
    if scenario is None:
        return None
    mine = [c for c in conditions if norm_ref(c.get("material")) == norm_ref(material)]
    if any(dated(c) for c in mine):
        mine = [c for c in mine if dated(c)]
    elif len(mine) > 1:
        return None                          # several undated records: nothing says which one applies
    valid = []
    for c in mine:
        start, end = to_datetime(c.get("valid_from")), validity_end(c.get("valid_to"))
        if (c.get("valid_from") and start is None) or (c.get("valid_to") and end is None):
            continue
        if (start is None or start <= scenario) and (end is None or scenario <= end):
            complete = bool(c.get("scales")) and to_decimal(c.get("price_unit")) is not None
            valid.append(((start or to_datetime("1970-01-01")), complete, c))
    return max(valid, key=lambda x: (x[0], x[1]))[2] if valid else None


def condition_status(conditions: list[dict], material: str, scenario, pieces: int | None) -> tuple[dict | None, str]:
    """(valid condition, what is missing to price from it: '' when it prices). Used to name the dependency."""
    mine = [c for c in conditions if norm_ref(c.get("material")) == norm_ref(material)]
    if not mine:
        return None, f"price condition records of material {material} with quantity scales and price unit"
    if scenario is None:
        return None, "scenario date (from the brief)"
    cond = valid_condition(conditions, material, scenario)
    record = f" (record {cond.get('record')})" if cond and cond.get("record") else ""
    if cond is None:
        periods = [(c.get("record"), c.get("valid_from"), c.get("valid_to")) for c in mine]
        return None, (f"price condition of material {material} valid on the scenario date (read: {periods}; "
                      "other condition records or validity periods)")
    if cond.get("_conflict"):
        return cond, f"conflicting evidence for the valid condition{record} of {material}: {cond['_conflict']}"
    start = to_datetime(cond.get("valid_from"))
    rivals = [c for c in mine if c is not cond and dated(c) and to_datetime(c.get("valid_from")) == start
              and scale_price(c, pieces or 1) is not None and scale_price(cond, pieces or 1) is not None
              and scale_price(c, pieces or 1) != scale_price(cond, pieces or 1)
              and (c.get("valid_to") in (None, "") or (validity_end(c.get("valid_to")) or scenario) >= scenario)]
    if rivals:
        return dict(cond, _conflict="another condition valid from the same date prices differently"), (
            f"conflicting evidence for the valid condition{record} of {material}: "
            f"{len(rivals) + 1} conditions valid from the same date price differently")
    if not cond.get("scales"):
        return cond, f"quantity scales of the valid price condition{record} of {material} (open its scale register)"
    if to_decimal(cond.get("price_unit")) is None:
        return cond, f"price unit (price per quantity, e.g. per 100 pieces) of the valid condition{record} of {material}"
    if pieces is not None and scale_price(cond, pieces) is None:
        return cond, f"scale rows of the valid condition{record} of {material} readable for {pieces} pieces"
    return cond, ""


def scale_price(condition: dict, pieces: int) -> Decimal | None:
    """Per-piece price at the highest scale whose minimum quantity <= pieces, divided by the price unit."""
    fmt = condition.get("number_format")
    rows = []
    for s in condition.get("scales") or []:
        q, p = to_decimal(s.get("min_qty"), fmt), to_decimal(s.get("price"), fmt)
        if q is not None and p is not None and 0 <= q <= pieces and p >= 0:
            rows.append((q, p))
    if not rows:
        return None
    unit = to_decimal(condition.get("price_unit"))
    return max(rows)[1] / unit if unit is not None and unit > 0 else None


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


class Deps:
    """The open dependencies of a bid: what to collect, for which positions."""

    def __init__(self):
        self.items: dict[str, dict] = {}
        self.by_pos: dict[str, list[str]] = {}

    def need(self, pos: str | None, gid: str, role: str, need: str, kind: str = "") -> str:
        item = self.items.setdefault(gid, {"id": gid, "role": role, "need": need, "positions": [], "kind": kind})
        if need not in item["need"]:
            item["need"] += "; " + need
        if pos is not None:
            if pos not in item["positions"]:
                item["positions"].append(pos)
            ids = self.by_pos.setdefault(pos, [])
            if gid not in ids:
                ids.append(gid)
        return gid


def compute(inputs: dict) -> dict:
    brief, tender = inputs.get("brief") or {}, inputs.get("tender") or {}
    quantities, requirements = inputs.get("quantities") or {}, inputs.get("requirements") or {}
    matched, erp = inputs.get("materials") or {}, inputs.get("erp") or {}
    discount, mail = inputs.get("discount") or {}, inputs.get("mail") or {}
    scenario = to_datetime(brief.get("scenario_date"))
    erp_materials = {norm_ref(k): v for k, v in (erp.get("materials") or {}).items()}
    issues, omitted, lines = [], [], {}
    clarifications = tender.get("clarifications") or []
    deps = Deps()
    qa_read = bool(clarifications)
    keyed = "absent" if qa_read else "absent (no Q&A read)"

    # 1. per position: pieces, material, EUR base price
    for p in tender.get("positions") or []:
        pos = str(p.get("position")).strip()
        q = (quantities.get(pos) or {}).get("quantity")
        req = next((v for k, v in requirements.items() if norm_ref(k) == norm_ref(p.get("requirement_ref"))), {}) or {}
        mat = (matched.get(pos) or {}).get("material")
        line = {"position": pos, "material": mat, "unit": p.get("unit"), "required": (matched.get(pos) or {}).get("required"),
                "short_text": p.get("short_text"), **position_keys(clarifications, pos), "fields": {}}
        fields = line["fields"]
        lines[pos] = line
        if q is None:
            issues.append(f"{pos}: no quantity")
            fields["quantity"] = "missing"
            # no quantity analysis yet is pending work, not something to collect
            deps.need(pos, f"tender quantity {pos}", "tender" if quantities else "calc",
                      f"quantity of position {pos} (bill of quantities, Q&A)")
            continue
        if is_set_unit(p.get("unit")):
            per_set = to_decimal(req.get("pieces_per_set"))
            if not per_set:
                issues.append(f"{pos}: quantity in sets but pieces per set unknown")
                fields["quantity"] = "missing"
                ref = p.get("requirement_ref")
                deps.need(pos, f"spec pieces per set {norm_ref(ref)}", "spec",
                          f"pieces per set (Satz) of requirement {ref} (set definition / data sheet)")
                continue
            q = (to_decimal(q) * per_set) if to_decimal(q) is not None else None
        if to_decimal(q) is None or to_decimal(q) <= 0 or to_decimal(q) != int(to_decimal(q)):
            issues.append(f"{pos}: quantity must be a positive integer")
            fields["quantity"] = "missing"
            deps.need(pos, f"tender quantity {pos}", "tender", f"quantity of position {pos} as a whole number")
            continue
        line["pieces"] = int(q)
        fields["quantity"] = "verified"
        if not mat:
            reason = (matched.get(pos) or {}).get("reason") or "not matched yet"
            issues.append(f"{pos}: no material ({reason})")
            fields["material"] = "missing"
            deps.need(pos, f"erp material {pos}", "erp", f"an active ERP material matching position {pos} exactly "
                                                         f"({reason})", kind="material")
            continue
        unverified = (matched.get(pos) or {}).get("unverified") or []
        fields["material"] = "verified" if not unverified else f"estimated: unverified {', '.join(unverified)}"
        if unverified:
            deps.need(pos, f"erp record {norm_ref(mat)}", "erp", f"{', '.join(unverified)} of material {mat}")
        line["erp"] = erp_materials.get(norm_ref(mat), {})
        line["configuration"] = {k: line["erp"].get(k) for k in ("bore", "stroke", "pressure", "mounting", "certification")}
        conditions = erp.get("conditions") or []
        cond, missing_price = condition_status(conditions, mat, scenario, int(q))
        price = scale_price(cond, int(q)) if cond and not cond.get("_conflict") else None
        if price is None:
            # say why: a missing condition, one outside the scenario date, or unreadable scales/price unit
            seen = [{k: c.get(k) for k in ("record", "valid_from", "valid_to", "price_unit", "number_format", "currency")}
                    | {"scales": len(c.get("scales") or [])}
                    for c in conditions if norm_ref(c.get("material")) == norm_ref(mat)]
            issues.append(f"{pos}: no valid price condition/scale for {mat}: {missing_price or 'unreadable'}; "
                          f"conditions seen: {seen or 'none'}")
            fields["base"] = "missing"
            deps.need(pos, f"erp prices {norm_ref(mat)}", "erp", missing_price or f"price condition of {mat}")
            continue
        currency = norm_ref((cond or {}).get("currency") or "EUR")
        if currency != "EUR":
            rate = to_decimal((mail.get("fx_by_key") or {}).get(norm_ref(cond.get("fx_key")), {}).get(currency)) if cond.get("fx_key") else to_decimal((mail.get("fx_fixing") or {}).get(currency))
            if rate is None:
                issues.append(f"{pos}: price in {currency}, no FX rate")
                fields["base"] = "missing"
                deps.need(pos, f"mail fx {currency}", "mail", f"FX fixing mail for {currency}"
                                                             + (f" (key {cond.get('fx_key')})" if cond.get("fx_key") else ""))
                continue
            price = price * rate
        line["base"] = round_cents(price)
        fields["base"] = "verified" if dated(cond) else "estimated: condition validity not read"
        if not dated(cond):
            deps.need(pos, f"erp prices {norm_ref(mat)}", "erp", f"validity period of the price condition of {mat}")

    priced = {pos: l for pos, l in lines.items() if "base" in l}
    omitted = [pos for pos in lines if pos not in priced]

    # 2. order-level discount (threshold on undiscounted goods value of all priced positions)
    pct = to_decimal(discount.get("percent")) or D0
    threshold = to_decimal(discount.get("min_order_value"))
    order_value = round_cents(sum((l["pieces"] * l["base"] for l in priced.values()), D0))
    order_value_complete = len(priced) == len(lines)
    discount_unknown = threshold is not None and order_value < threshold and not order_value_complete
    if discount_unknown:
        issues.append("conditional discount unresolved: incomplete order value; 0% is a partial estimate")
    if threshold is not None and order_value < threshold:
        pct = D0
    d = pct / 100
    approval = (discount.get("source") or "")
    discount_status = ("estimated: conditional, order value incomplete" if discount_unknown else
                       "absent (no valid approval found)" if approval in ("no valid approval found", "") and not pct
                       else "estimated: latest approval unreadable" if approval == "latest approval invalid"
                       else "verified")

    # 3. shared setup pools, project bonus (allocations in cents)
    pools: dict[str, dict] = {}
    bonus: dict[str, dict] = {}
    for pos, l in lines.items():
        if l["keys"].get("setup_pool"):
            pools.setdefault(norm_ref(l["keys"]["setup_pool"]), {})[pos] = l
        if l["keys"].get("bonus_plan"):
            bonus.setdefault(norm_ref(l["keys"]["bonus_plan"]), {})[pos] = l
    for key, members in pools.items():
        fee = to_decimal((mail.get("setup_pools") or {}).get(key))
        masses = {pos: to_decimal(l.get("erp", {}).get("allocation_mass_g")) for pos, l in members.items()}
        weights = {pos: Decimal(l["pieces"]) * masses[pos] for pos, l in members.items()
                   if l.get("pieces") and masses[pos] is not None and masses[pos] >= 0}
        if fee is None or fee < 0 or len(weights) != len(members) or not any(weights.values()):
            for pos, l in members.items():
                l["pool_unknown"] = True
                if fee is None or fee < 0:
                    deps.need(pos, f"mail setup_pools {key}", "mail", f"setup pool fee mail for key {key}")
                for other, ol in members.items():
                    if masses[other] is None and ol.get("material"):
                        deps.need(pos, f"erp record {norm_ref(ol['material'])}", "erp",
                                  f"allocation mass (Umlagegewicht) of material {ol['material']}")
                    elif not ol.get("pieces") or not ol.get("material"):
                        deps.need(pos, f"position {other}", "calc", f"quantity/material of pool member {other}")
            continue
        for pos, amount in allocate_cents(fee, weights).items():
            members[pos]["pool"] = amount
    for key, members in bonus.items():
        bands = (mail.get("project_bonus") or {}).get(key)
        if not bands or any("base" not in l for l in members.values()):
            for pos, l in members.items():
                l["bonus_unknown"] = True
                if not bands:
                    deps.need(pos, f"mail project_bonus {key}", "mail", f"project bonus bands mail for key {key}")
                for other, ol in members.items():
                    if "base" not in ol and other != pos:
                        deps.need(pos, f"position {other}", "calc", f"price of bonus plan member {other}")
            continue
        values = {pos: l["pieces"] * l["base"] for pos, l in members.items()}
        credit = marginal_bonus(sum(values.values(), D0), bands)
        for pos, amount in allocate_cents(credit, values).items():
            members[pos]["bonus"] = amount

    # 4. per-position surcharge, packaging, final price
    for pos, l in priced.items():
        n = Decimal(l["pieces"])
        fields, mat = l["fields"], l["material"]
        index = l["erp"].get("metal_index")
        rate = to_decimal((mail.get("price_indices") or {}).get(norm_ref(index))) if index else None
        mass = to_decimal(l["erp"].get("surcharge_mass_g"))
        l["mass_surcharge"] = mass / 1000 * rate if (rate is not None and mass is not None) else None
        if l["mass_surcharge"] is None and mass is not None and mass == 0:
            l["mass_surcharge"] = D0          # no surcharge mass: nothing to index
        if l["mass_surcharge"] is not None:
            fields["surcharge"] = "verified"
        else:
            fields["surcharge"] = "estimated: " + ("price index rate missing" if index and mass is not None
                                                   else "surcharge mass / metal index not read")
            if index and mass is not None:
                deps.need(pos, f"mail price index {norm_ref(index)}", "mail",
                          f"metal price index mail for {index} valid on {brief.get('scenario_date')}")
            else:
                deps.need(pos, f"erp record {norm_ref(mat)}", "erp", f"surcharge mass and metal index of material {mat}")
        tariff_key = l["keys"].get("packaging_tariff")
        tariff = to_decimal((mail.get("packaging_tariffs") or {}).get(norm_ref(tariff_key))) if tariff_key else D0
        carton = to_decimal(l["erp"].get("pieces_per_carton"))
        if tariff_key and (tariff is None or not carton):
            l["packaging_total"] = None
            fields["packaging"] = "estimated: " + ("tariff missing" if tariff is None else "pieces per carton missing")
            if tariff is None:
                deps.need(pos, f"mail packaging_tariffs {norm_ref(tariff_key)}", "mail",
                          f"packaging tariff mail for key {tariff_key}")
            if not carton:
                deps.need(pos, f"erp record {norm_ref(mat)}", "erp", f"pieces per carton of material {mat}")
        else:
            l["packaging_total"] = (tariff * math.ceil(n / carton)) if tariff_key else D0
            fields["packaging"] = "verified" if tariff_key else keyed
        scope_key = l["keys"].get("konditionsgruppe")
        scope = (mail.get("condition_rules") or {}).get(norm_ref(scope_key)) if scope_key else "base_only"
        missing = []
        if l["mass_surcharge"] is None:
            missing.append("mass surcharge")
        if l["packaging_total"] is None:
            missing.append("packaging")
        if l.get("pool_unknown"):
            missing.append("shared setup")
        if l.get("bonus_unknown"):
            missing.append("project bonus")
        if scope not in ("base_only", "base_and_surcharge", "none"):
            missing.append("discount scope")
            deps.need(pos, f"mail condition_rules {norm_ref(scope_key)}", "mail",
                      f"condition rules mail for Konditionsgruppe {scope_key}")
            scope = None
        fields["discount_scope"] = ("verified" if scope_key and scope else "estimated: rule missing" if scope_key
                                    else keyed)
        fields["setup"] = ("estimated: pool share unknown" if l.get("pool_unknown") else
                           "verified" if (l.get("setup_fee") is not None or "pool" in l) else keyed)
        fields["bonus"] = ("estimated: bonus unknown" if l.get("bonus_unknown") else
                           "verified" if "bonus" in l else keyed)
        if discount_unknown:
            missing.append("conditional discount")
        fields["discount"] = discount_status
        l["pricing_complete"] = not missing
        if missing:
            issues.append(f"{pos}: estimated final price; unresolved: {', '.join(missing)}")
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
    for l in lines.values():
        if not l.get("material") or not l.get("pieces"):
            continue
        demand[norm_ref(l["material"])] = demand.get(norm_ref(l["material"]), 0) + l["pieces"]
    stock = {norm_ref(k): v for k, v in (erp.get("stock") or {}).items()}
    for pos, l in priced.items():
        m, fields = norm_ref(l["material"]), l["fields"]
        delivery_key = l["keys"].get("delivery_key")
        commit = (mail.get("delivery_commitments") or {}).get(norm_ref(delivery_key)) if delivery_key else None
        if commit and norm_ref(commit.get("material")) == m and to_decimal(commit.get("max_total_pieces")) is not None \
                and demand[m] <= to_decimal(commit["max_total_pieces"]) and to_decimal(commit.get("weeks")) is not None:
            l["lead"] = int(to_decimal(commit["weeks"]))
            fields["lead"] = "verified: delivery commitment"
            continue
        why = ""
        if delivery_key and not commit:
            why = "delivery commitment missing"
            deps.need(pos, f"mail delivery_commitments {norm_ref(delivery_key)}", "mail",
                      f"delivery commitment mail for key {delivery_key}")
        elif commit:
            why = ("commitment is for another material" if norm_ref(commit.get("material")) != m else
                   "commitment does not cover the demand" if to_decimal(commit.get("max_total_pieces")) is not None
                   else "commitment unreadable")
        free = to_decimal((stock.get(m) or {}).get("free"))
        stock_weeks = to_decimal(l["erp"].get("lead_time_stock_weeks"))
        prod_weeks = to_decimal(l["erp"].get("lead_time_production_weeks"))
        if free is None:
            deps.need(pos, f"erp stock {m}", "erp", f"stock of material {l['material']} at storage-location level")
        chosen = stock_weeks if (free is not None and free >= demand[m]) else prod_weeks
        l["lead"] = int(chosen) if chosen is not None else None
        if l["lead"] is None:
            issues.append(f"{pos}: lead time unknown")
            fields["lead"] = "missing"
            deps.need(pos, f"erp record {norm_ref(l['material'])}", "erp", f"lead times (stock / production) of material {l['material']}")
        else:
            source = "stock" if (free is not None and free >= demand[m]) else "production"
            open_why = [why] if why in ("delivery commitment missing", "commitment unreadable") else []
            if free is None:
                open_why.append("stock not read")
            fields["lead"] = (f"estimated: {source} lead time, {', '.join(open_why)}" if open_why
                              else f"verified: {source}" + (f" ({why})" if why else ""))
        if why == "commitment unreadable":
            deps.need(pos, f"mail delivery_commitments {norm_ref(delivery_key)}", "mail",
                      f"material, weeks and maximum pieces of the delivery commitment {delivery_key}")

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
    for pos, l in lines.items():
        l["unresolved"] = deps.by_pos.get(pos, [])
    total = round_cents(sum((Decimal(e["quantity"]) * Decimal(str(e["unit_price"])) for e in positions), D0))
    answer = {"tender_reference": brief.get("tender_reference") or (tender.get("meta") or {}).get("reference") or "",
              "decision": decision, "currency": "EUR", "discount_percent": float(pct if decision == "bid" or threshold is None else D0),
              "positions": positions if decision == "bid" else [], "total_net": float(total) if decision == "bid" else 0.0}
    if decision == "no_bid":
        issues.append(f"no active material for {blocked}: no_bid")
    entered = {e["position"] for e in positions}
    complete = [pos for pos in entered if not deps.by_pos.get(pos) and lines[pos].get("pricing_complete")]
    return {"answer": answer, "omitted": sorted(set(omitted)) if decision == "bid" else [],
            "total_display": de_number(total if decision == "bid" else D0), "order_value": str(order_value),
            "order_value_complete": order_value_complete, "discount_resolved": not discount_unknown,
            "lines": [{k: (str(v) if isinstance(v, Decimal) else v) for k, v in l.items() if k != "erp"}
                      for l in lines.values()],
            "issues": issues, "dependencies": deps.items,
            "unresolved": {pos: ids for pos, ids in deps.by_pos.items() if ids},
            "coverage": {"positions": len(lines), "priced": len(entered), "complete": len(complete),
                         "complete_positions": sorted(complete),
                         "unresolved": len([d for d in deps.items.values() if d["role"] != "calc"])}}


INPUTS = ("brief", "tender", "quantities", "requirements", "materials", "erp", "discount", "mail")


def build_bid(ctx) -> dict:
    """compute() on what the board holds now, made schema-valid (malformed lines omitted, total recomputed)."""
    bid = compute({n: ctx.board.get(n) for n in INPUTS})
    from swarm.validation import answer_issues, prepare_answer
    original_positions = {p["position"] for p in bid["answer"]["positions"]}
    bid["answer"], repairs = prepare_answer(bid["answer"], ctx.task.get("answer_schema"))
    bid["issues"].extend(repairs)
    included = {p["position"] for p in bid["answer"]["positions"]}
    bid["omitted"] = sorted(set(bid["omitted"]) | (original_positions - included))
    bid["total_display"] = de_number(Decimal(str(bid["answer"]["total_net"])))
    bid["validation_errors"] = answer_issues(bid["answer"], ctx.task.get("answer_schema"))
    bid["issues"].extend(bid["validation_errors"])
    bid["coverage"]["priced"] = len(included)
    return bid


@register
class BidCalculator(Agent):
    phase = Phase.COMPUTE

    async def run(self) -> None:
        bid = build_bid(self.ctx)
        errors = bid["validation_errors"]
        if errors:
            self.ctx.board.error(self.name, "; ".join(errors))
        await self.ctx.board.set("bid", bid)
        await self.ctx.board.set("answer", None if errors else bid["answer"])
        self.log.info("bid: %s omitted=%s issues=%s", bid["answer"], bid["omitted"], bid["issues"])
        self.log.info("bid fields: %s", {l["position"]: {k: v for k, v in (l.get("fields") or {}).items()
                                                         if v != "verified"} for l in bid["lines"]})


DRAFT_EVERY_S = 3.0


@register
class BidDrafter(Agent):
    """The bid as it would be submitted now, recomputed during collection (plain code, no model): a usable partial
    bid exists as soon as one position is priced, and its open dependencies tell the collectors what is still missing
    for each position (results["bid_draft"]). Milestones mark the first usable bid and complete coverage."""
    phase = Phase.COLLECT
    background = True          # ends when the collecting agents end

    async def run(self) -> None:
        from swarm import timeline
        last = None
        while True:
            try:
                bid = build_bid(self.ctx)
            except Exception as error:  # noqa: BLE001 - a half-collected board must never stop the drafts
                self.log.debug("draft failed: %s", error)
                await asyncio.sleep(DRAFT_EVERY_S)
                continue
            cov = bid["coverage"]
            signature = (json.dumps(bid["answer"], sort_keys=True), json.dumps(bid["unresolved"], sort_keys=True))
            if signature != last:
                last = signature
                await self.ctx.board.set("bid_draft", bid)
                self.log.info("draft: %d/%d positions priced, %d complete, total %s; open: %s", cov["priced"],
                              cov["positions"], cov["complete"], bid["total_display"],
                              {p: ids for p, ids in bid["unresolved"].items()})
                if cov["priced"]:
                    timeline.mark("first usable bid", positions=cov["priced"])
                if cov["positions"] and cov["priced"] == cov["positions"]:
                    timeline.mark("all positions priced")
                if cov["positions"] and cov["complete"] == cov["positions"]:
                    timeline.mark("all positions complete")
                for pos in cov.get("complete_positions") or []:
                    timeline.mark(f"position {pos} complete")
            await asyncio.sleep(DRAFT_EVERY_S)
