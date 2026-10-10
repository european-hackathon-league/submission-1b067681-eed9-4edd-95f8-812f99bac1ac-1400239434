# OWNER: (name)   ROLE: analyst   NEEDS: erp.page (done.erp), results["requirements"], results["tender"], results["mail"]
# WRITES: results["erp"], results["materials"]
"""Turns the saved ERP pages into materials, price conditions and stock, then matches each position in code:
an ACTIVE material whose bore, stroke, pressure, mounting and certification all equal the requirement exactly.
A requirement given as usable travel (nutzbarer Weg) gets twice the per-end reserve of the technical release for the
current revision added (amendments were already applied by the requirement analyst).

results["erp"] = {"materials": {no: {...}}, "conditions": [{...}], "stock": {no: {"free": "12", "locations": [...]}}}
results["materials"] = {position: {"material": no | None, "required": {...}, "candidates": [...], "reason": str}}
"""
import asyncio
import re
from decimal import Decimal

from swarm import Agent, Phase, register
from swarm.extract import extract, page_block, pages
from swarm.normalize import norm_ref, to_decimal

MATERIAL_SHAPE = """{"materials": [{"material": "material number", "description": "", "kataloggruppe": "",
  "bore": "", "stroke": "", "pressure": "", "mounting": "", "certification": "",
  "status_code": "as shown", "status_meaning": "meaning from the legend/tooltip",
  "is_active": "true/false from the status and its legend, or null if no status is shown for this material",
  "lead_time_stock_weeks": "", "lead_time_production_weeks": "", "pieces_per_carton": "",
  "allocation_mass_g": "allocation/Umlage weight per piece in g", "surcharge_mass_g": "surcharge mass per piece in g",
  "metal_index": "price index code"}]}"""

CONDITION_SHAPE = """{"conditions": [{"material": "", "record": "condition record no./page", "condition_type": "",
  "valid_from": "as shown", "valid_to": "as shown", "currency": "", "fx_key": null,
  "price_unit": "price unit quantity, e.g. 100", "number_format": "e.g. en-US or de-DE if stated",
  "scales": [{"min_qty": "as shown incl. locale marker", "price": "as shown incl. locale marker"}]}]}"""

STOCK_SHAPE = """{"stock": [{"material": "", "plant": "", "location": "", "quantity": "as shown",
  "usable": "true if freely usable (unrestricted), false if blocked/quality inspection/not available"}]}"""

MATERIAL_INSTRUCTION = ("List every ERP material on these pages with all its attributes (merge the tabs of one "
                        "material). is_active is true only if the status legend says released/active (not blocked).")
CONDITION_INSTRUCTION = ("List every price condition record on these pages with all quantity-scale rows. Keep the "
                         "number strings exactly with their locale markers; note the stated number format.")
STOCK_INSTRUCTION = ("List stock quantities per material and storage location shown on these pages. A plant total "
                     "that includes blocked/inspection stock is NOT usable; mark usable=true only for freely usable "
                     "storage-location stock.")


def num(text, locale=None) -> Decimal | None:
    return to_decimal(text, locale)


def same(a, b) -> bool:
    x, y = num(a), num(b)
    if x is not None and y is not None:
        return x == y
    return norm_ref(a) == norm_ref(b) and norm_ref(a) != ""


_NONE = re.compile(r"^(KEINE?|NONE|OHNE|NICHT|N/?A|—|-|–)")


def text_equal(a, b) -> bool:
    """Equal after normalisation; 'none required' and 'none' count as the same (no certification)."""
    x, y = norm_ref(a), norm_ref(b)
    if _NONE.match(x) and _NONE.match(y):
        return True
    return x == y and x != ""


def merge_materials(rows: list[dict]) -> dict:
    out: dict = {}
    for r in rows:
        no = norm_ref(r.get("material"))
        if not no:
            continue
        cur = out.setdefault(no, {"material": str(r.get("material")).strip()})
        for k, v in r.items():
            if v not in (None, "", "null") and (cur.get(k) in (None, "") or k == "is_active" and v is False):
                cur[k] = v
    return out


def required_stroke(req: dict, mail: dict, current_revision: str) -> Decimal | None:
    stroke = num(req.get("stroke"))
    if stroke is None or req.get("stroke_kind") != "usable":
        return stroke
    releases = (mail or {}).get("technical_release") or {}
    key = norm_ref(req.get("reserve_key"))
    match = releases.get(key) if key else None
    if match is None:
        same_rev = [r for r in releases.values() if norm_ref(r.get("revision")) == norm_ref(current_revision)]
        match = same_rev[0] if len(same_rev) == 1 else None
    reserve = num((match or {}).get("reserve_mm_per_end"))
    return stroke + 2 * reserve if reserve is not None else None


def match_positions(tender: dict, requirements: dict, materials: dict, mail: dict, current: str) -> dict:
    out = {}
    for p in (tender or {}).get("positions") or []:
        pos = str(p.get("position")).strip()
        ref = next((r for r in requirements if norm_ref(r) == norm_ref(p.get("requirement_ref"))), None)
        req = requirements.get(ref) if ref else None
        if not req:
            out[pos] = {"material": None, "reason": f"requirement {p.get('requirement_ref')} not found", "candidates": []}
            continue
        stroke = required_stroke(req, mail, current)
        group = norm_ref(req.get("kataloggruppe"))
        cands = []
        for m in materials.values():
            if group and norm_ref(m.get("kataloggruppe")) and norm_ref(m.get("kataloggruppe")) != group:
                continue
            ok = (same(m.get("bore"), req.get("bore")) and stroke is not None and num(m.get("stroke")) == stroke
                  and same(m.get("pressure"), req.get("pressure")) and text_equal(m.get("mounting"), req.get("mounting"))
                  and text_equal(m.get("certification"), req.get("certification")))
            if ok:
                active = m.get("is_active")
                cands.append({"material": m["material"], "status": m.get("status_code"),
                              "active": True if active in (True, "true") else False if active in (False, "false") else None})
        active = [c for c in cands if c["active"] is True]
        unknown = [c for c in cands if c["active"] is None]       # status page not read: better than nothing
        required = {k: req.get(k) for k in ("bore", "pressure", "mounting", "certification", "kataloggruppe")}
        required["stroke"] = None if stroke is None else format(stroke.normalize(), "f")
        chosen = active or unknown
        out[pos] = {"material": chosen[0]["material"] if chosen else None, "required": required, "candidates": cands,
                    "reason": "exact active match" if active else "exact match, status not read" if unknown else
                    ("only blocked matches" if cands else "no exact match among the ERP records read")}
    return out


@register
class MaterialMatcher(Agent):
    phase = Phase.COLLECT

    async def run(self) -> None:
        await self.ctx.board.wait_for(key="done.erp", timeout=max(1.0, self.ctx.time_left()))
        blocks = [page_block(e) for e in pages(self.ctx, "erp")]
        if not blocks:
            raise RuntimeError("no ERP pages were saved")
        mats, conds, stock_out = await asyncio.gather(
            extract(self.ctx, MATERIAL_INSTRUCTION, MATERIAL_SHAPE, blocks, list_key="materials", chunk_size=25_000),
            extract(self.ctx, CONDITION_INSTRUCTION, CONDITION_SHAPE, blocks, list_key="conditions", chunk_size=25_000),
            extract(self.ctx, STOCK_INSTRUCTION, STOCK_SHAPE, blocks, list_key="stock", chunk_size=25_000))
        materials = merge_materials(mats.get("materials") or [])
        stock: dict = {}
        for s in stock_out.get("stock") or []:           # one row per (material, location); a re-read replaces
            stock.setdefault(norm_ref(s.get("material")), {})[norm_ref(s.get("location")) or norm_ref(s.get("plant"))] = s
        for no, rows in list(stock.items()):
            free = sum((num(s.get("quantity")) or Decimal(0) for s in rows.values()
                        if str(s.get("usable")).lower() in ("true", "1", "yes")), Decimal(0))
            stock[no] = {"free": free, "locations": list(rows.values())}
        erp = {"materials": materials, "conditions": conds.get("conditions") or [],
               "stock": {k: {"free": format(v["free"], "f"), "locations": v["locations"]} for k, v in stock.items()}}
        await self.ctx.board.set("erp", erp)
        try:
            await self.ctx.board.wait_for(key="requirements_final", timeout=max(1.0, min(90, self.ctx.time_left())))
            await self.ctx.board.wait_for(key="mail", timeout=max(1.0, min(90, self.ctx.time_left())))  # end reserves
        except TimeoutError:
            self.log.warning("matching without final requirements or mail")
        mail =self.ctx.board.get("mail") or {}
        spec = self.ctx.board.get("spec") or {}
        result = match_positions(self.ctx.board.get("tender") or {}, self.ctx.board.get("requirements") or {},
                                 materials, mail, spec.get("current", ""))
        await self.ctx.board.set("materials", result)
        self.log.info("materials: %s", {p: (r["material"], r["reason"]) for p, r in result.items()})
