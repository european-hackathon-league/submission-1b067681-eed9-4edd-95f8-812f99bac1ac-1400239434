# OWNER: (name)   ROLE: analyst   NEEDS: erp.page (done.erp), results["requirements"], results["tender"], results["mail"]
# WRITES: results["erp"], results["materials"]
"""Turns the saved ERP pages into materials, price conditions and stock, then matches each position in code:
an ACTIVE material whose bore, stroke, pressure, mounting and certification all equal the requirement exactly.
A requirement given as usable travel (nutzbarer Weg) gets twice the per-end reserve of the technical release for the
current revision added (amendments were already applied by the requirement analyst).

Every saved page is extracted ONCE (cached in ctx.shared), with the shape its topic asks for (search list, material
record, price conditions, stock), so the ERP scraper can ask "which candidates did this search list show?" while it
works and the matcher and the coordinator's follow-up rounds only pay for new pages.

Attributes read on a material's own record page beat values from a search list; its status (active / blocked) is only
taken from record pages, and record pages that disagree leave it unknown (never "blocked" by one misread).
Attributes a page does not show (e.g. certification in a search list) count as unverified, not as a mismatch.

results["erp"] = {"materials": {no: {...}}, "conditions": [{...}], "stock": {no: {"free": "12", "locations": [...]}}}
results["materials"] = {position: {"material": no | None, "required": {...}, "candidates": [...], "reason": str,
                                   "unverified": [attribute, ...]}}
"""
import asyncio
import re
from decimal import Decimal

from swarm import Agent, Phase, register
from swarm.extract import extract, page_block, pages
from swarm.normalize import norm_ref, to_decimal

MATERIAL_ROW = """{"material": "material number", "description": "", "kataloggruppe": "",
  "bore": "", "stroke": "", "pressure": "", "mounting": "", "certification": "",
  "status_code": "as shown", "status_meaning": "meaning from the legend/tooltip",
  "is_active": "true/false from the status and its legend, or null if no status is shown for this material",
  "lead_time_stock_weeks": "", "lead_time_production_weeks": "", "pieces_per_carton": "",
  "allocation_mass_g": "allocation/Umlage weight per piece in g", "surcharge_mass_g": "surcharge mass per piece in g",
  "metal_index": "price index code"}"""

CONDITION_ROW = """{"material": "", "record": "condition record number as shown (null if none shown)", "condition_type": "",
  "valid_from": "as shown", "valid_to": "as shown", "currency": "", "fx_key": null,
  "price_unit": "price unit quantity, e.g. 100", "number_format": "e.g. en-US or de-DE if stated",
  "scales": [{"min_qty": "as shown incl. locale marker", "price": "as shown incl. locale marker"}]}"""

STOCK_ROW = """{"material": "", "plant": "", "location": "", "quantity": "as shown",
  "usable": "true if freely usable (unrestricted), false if blocked/quality inspection/not available"}"""

SHAPES = {
    "list": ('{"materials": [' + MATERIAL_ROW + ']}', ("materials",),
             "List every material row of this search result list with the attributes the list shows (null for "
             "attributes it does not show)."),
    "record": ('{"materials": [' + MATERIAL_ROW + ']}', ("materials",),
               "List every ERP material on this page with all attributes (merge tabs of one material). is_active "
               "true only if status legend says released/active, false only if it says blocked/locked/inactive."),
    "prices": ('{"conditions": [' + CONDITION_ROW + ']}', ("conditions",),
               "List every price condition record on this page with all quantity-scale rows. Keep number strings "
               "exactly with locale markers; note stated number format."),
    "stock": ('{"stock": [' + STOCK_ROW + ']}', ("stock",),
              "List stock quantities per material and storage location on this page. Plant total including "
              "blocked/inspection stock NOT usable; usable=true only for freely usable storage-location stock."),
}
SHAPES["any"] = ('{"materials": [' + MATERIAL_ROW + '], "conditions": [' + CONDITION_ROW + '], "stock": [' + STOCK_ROW
                 + ']}', ("materials", "conditions", "stock"),
                 "List every ERP material (all attributes), price condition record (all scale rows) and stock "
                 "quantity per storage location on this page. Keep number strings exactly with locale markers.")


def page_kind(topic: str) -> str:
    t = (topic or "").lower()
    if any(w in t for w in ("search", "such", "list", "treffer", "result")):
        return "list"
    if any(w in t for w in ("price", "preis", "condition", "kondition", "scale", "staffel")):
        return "prices"
    if any(w in t for w in ("stock", "bestand", "lager", "inventory")):
        return "stock"
    if any(w in t for w in ("material", "record", "stamm", "tab", "status", "techn")):
        return "record"
    return "any"


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


def blank(v) -> bool:
    return v in (None, "", "null") or (isinstance(v, str) and v.strip().lower() in ("", "null", "none", "n/a", "-"))


def _key(e) -> tuple:
    return (e.data.get("url"), hash(e.data.get("text", "")))


async def extract_erp(ctx, evidence=None) -> dict:
    """Extract every saved ERP page not extracted yet (one model call per page, in parallel); returns the cache
    {page key: {"kind", "topic", "url", "materials", "conditions", "stock"}}."""
    cache = ctx.shared.setdefault("erp.extracted", {})
    todo = [e for e in (pages(ctx, "erp") if evidence is None else evidence) if _key(e) not in cache]

    async def one(e) -> dict:
        # A slow unrelated page must not block a finished search from scheduling material records.
        async with ctx.lock(f"erp.extract.{_key(e)!r}"):
            if _key(e) in cache:
                return cache[_key(e)]
            kind = page_kind(e.data.get("topic"))
            shape, keys, instruction = SHAPES[kind]
            out = await extract(ctx, instruction, shape, [page_block(e)], list_key=keys, chunk_size=25_000)
            result = {"kind": kind, "topic": e.data.get("topic"), "url": e.data.get("url"), "_incomplete": out.get("_incomplete", False),
                    **{k: out.get(k) or [] for k in ("materials", "conditions", "stock")}}
            if not result["_incomplete"]:
                cache[_key(e)] = result
                await publish(ctx, cache)
            return result
    replies = await asyncio.gather(*(one(e) for e in todo), return_exceptions=True)
    for r in replies:
        if isinstance(r, Exception):
            ctx.board.error("MaterialMatcher", f"ERP extraction: {type(r).__name__}")
    return cache


def merge_materials(extracted: list[dict], groups=()) -> dict:
    """{material no: attributes}: record pages first (they win), then search lists fill what is still missing.
    Status only from record pages; record pages that disagree on it leave it unknown. A material listed on the search
    page of a catalogue group (`groups`: the requirements' groups, named in the page topic) belongs to that group."""
    out: dict = {}
    status: dict[str, set] = {}
    order = sorted(reversed(extracted), key=lambda x: 0 if x.get("kind") == "record" else 1 if x.get("kind") == "any" else 2)
    for page in order:
        from_record = page.get("kind") in ("record", "any")
        for r in page.get("materials") or []:
            no = norm_ref(r.get("material"))
            if not no:
                continue
            cur = out.setdefault(no, {"material": str(r.get("material")).strip(), "seen_in": set()})
            cur["seen_in"].add(page.get("kind"))
            for k, v in r.items():
                if k == "is_active":
                    continue
                if not blank(v) and blank(cur.get(k)):
                    cur[k] = v
            if page.get("kind") == "list" and blank(cur.get("kataloggruppe")):
                topic = norm_ref(page.get("topic"))
                group = next((g for g in groups if norm_ref(g) and norm_ref(g) in topic), None)
                if group:
                    cur["kataloggruppe"] = group
            active = r.get("is_active")
            if active in (True, "true", False, "false"):
                status.setdefault(no, set()).add((from_record, active in (True, "true")))
    for no, cur in out.items():
        readings = status.get(no, set())
        record = {a for rec, a in readings if rec}
        listed = {a for rec, a in readings if not rec}
        values = record or listed
        cur["is_active"] = next(iter(values)) if len(values) == 1 else None
        cur["seen_in"] = sorted(cur["seen_in"])
    return out


FILLABLE = ("scales", "price_unit", "currency", "fx_key", "number_format", "condition_type", "record")
IDENTITY = ("record", "condition_type", "currency", "fx_key")      # must agree when both sides show them


def _dated(c: dict) -> bool:
    return bool(c.get("valid_from") or c.get("valid_to"))


def _value_equal(key: str, a, b) -> bool:
    if key == "price_unit":
        x, y = to_decimal(a), to_decimal(b)
        return x == y if x is not None and y is not None else norm_ref(a) == norm_ref(b)
    if key == "scales":
        def rows(v):
            return sorted((str(to_decimal(r.get("min_qty"))), str(to_decimal(r.get("price")))) for r in v or [])
        return rows(a) == rows(b)
    return norm_ref(a) == norm_ref(b)


def _conflicts(a: dict, b: dict, keys) -> list[str]:
    return [k for k in keys if not blank(a.get(k)) and not blank(b.get(k)) and a.get(k) not in ([], {})
            and b.get(k) not in ([], {}) and not _value_equal(k, a.get(k), b.get(k))]


def _url_linked(a: dict, b: dict) -> bool:
    """Two pages of one record: the same page, or one URL path extending the other (header -> its scale register)."""
    def path(u):
        return str(u or "").split("#")[0].split("?")[0].rstrip("/")
    x, y = path(a.get("_url")), path(b.get("_url"))
    return bool(x and y) and (x == y or x.startswith(y + "/") or y.startswith(x + "/"))


def _fill(target: dict, frag: dict) -> None:
    clash = _conflicts(target, frag, ("scales", "price_unit", "number_format"))
    if clash:
        target["_conflict"] = (target.get("_conflict", "") + f" {', '.join(clash)} differ between pages "
                               f"({target.get('_url')} vs {frag.get('_url')})").strip()
        return
    for key in FILLABLE:
        if blank(target.get(key)) or target.get(key) in ([], {}):
            if not blank(frag.get(key)) and frag.get(key) not in ([], {}):
                target[key] = frag[key]
    target["_merged"] = target.get("_merged", 1) + 1


def reconcile_conditions(conditions: list[dict]) -> list[dict]:
    """Price conditions of one material read on several pages (a header with validity and price unit, a separate
    scales register, a re-read) become one condition only when they belong together:
    - copies of one dated record (same record id and validity) are merged; differing scales or price units mark the
      record as conflicting (`_conflict`) instead of picking one;
    - an undated fragment joins a dated condition when its record id names that condition, or (no ids to compare)
      when its page belongs to that condition's page, or the material has a single dated condition; identity fields
      shown on both sides (record, type, currency, FX key) must agree;
    - anything else stays separate: an unrelated or ambiguous fragment never completes another record's price
      (it is not selectable by date either; compute_bid reports the missing part as a dependency).
    Late pages are fine: the view is rebuilt from every extracted page each time."""
    out: list[dict] = []
    for c in conditions:                         # 1. copies of one dated record
        c = dict(c)
        twin = next((o for o in out if _dated(c) and _dated(o)
                     and norm_ref(o.get("material")) == norm_ref(c.get("material"))
                     and norm_ref(o.get("record")) == norm_ref(c.get("record"))
                     and norm_ref(o.get("valid_from")) == norm_ref(c.get("valid_from"))
                     and norm_ref(o.get("valid_to")) == norm_ref(c.get("valid_to"))
                     and not _conflicts(o, c, IDENTITY)), None)
        if twin is not None:
            _fill(twin, c)
        else:
            out.append(c)
    for material in {norm_ref(c.get("material")) for c in out}:   # 2. undated fragments
        mine = [c for c in out if norm_ref(c.get("material")) == material]
        targets = [c for c in mine if _dated(c)]
        for frag in [c for c in mine if not _dated(c)]:
            fitting = [t for t in targets if not _conflicts(t, frag, IDENTITY)]
            if not blank(frag.get("record")):
                same = [t for t in fitting if norm_ref(t.get("record")) == norm_ref(frag.get("record"))]
                fitting = same or [t for t in fitting if blank(t.get("record"))]
            linked = [t for t in fitting if _url_linked(t, frag)]
            if len(fitting) == 1 and (len(targets) == 1 or linked or not blank(frag.get("record"))):
                chosen = fitting[0]
            elif len(linked) == 1:
                chosen = linked[0]
            else:
                if targets:
                    frag["_unmatched"] = (f"fragment of {len(targets)} dated conditions"
                                          if fitting else "fragment of no dated condition read")
                continue
            _fill(chosen, frag)
            out = [c for c in out if c is not frag]
        undated = [c for c in out if norm_ref(c.get("material")) == material and not _dated(c)]
        if not targets and len(undated) > 1 and all(not _conflicts(a, b, IDENTITY) for a in undated for b in undated):
            # pages of one record whose dates were not read at all: one condition, its validity still open
            for frag in undated[1:]:
                _fill(undated[0], frag)
                out = [c for c in out if c is not frag]
    return out


def merge_condition_fragments(conditions: list[dict]) -> list[dict]:
    """Kept name: see reconcile_conditions."""
    return reconcile_conditions(conditions)


def erp_view(cache: dict, groups=()) -> dict:
    extracted = list(cache.values())
    materials = merge_materials(extracted, groups)
    conditions = reconcile_conditions([dict(c, _url=page.get("url")) for page in extracted
                                       for c in page.get("conditions") or [] if isinstance(c, dict)])
    stock: dict = {}
    for page in extracted:                 # one row per (material, location); a re-read replaces
        for s in page.get("stock") or []:
            stock.setdefault(norm_ref(s.get("material")), {})[(norm_ref(s.get("plant")), norm_ref(s.get("location")), str(s.get("usable")))] = s
    out_stock = {}
    for no, rows in stock.items():
        free = sum((num(s.get("quantity")) or Decimal(0) for s in rows.values()
                    if str(s.get("usable")).lower() in ("true", "1", "yes")), Decimal(0))
        out_stock[no] = {"free": format(free, "f"), "locations": list(rows.values())}
    return {"materials": materials, "conditions": conditions, "stock": out_stock}


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
    if match and norm_ref(match.get("revision")) != norm_ref(current_revision):
        return None
    reserve = num((match or {}).get("reserve_mm_per_end"))
    return stroke + 2 * reserve if reserve is not None else None


MIN_KNOWN = 3      # of bore, stroke, pressure, mounting, certification: fewer read -> no evidence it fits


def check(m: dict, req: dict, stroke: Decimal | None, min_known: int = MIN_KNOWN) -> list[str] | None:
    """None if `m` contradicts the requirement or too little of it was read; else the attributes that could not be
    verified (not shown, or the usable-travel reserve unknown)."""
    if sum(not blank(m.get(a)) for a in ("bore", "stroke", "pressure", "mounting", "certification")) < min_known:
        return None
    unverified = []
    for attr, equal in (("bore", same), ("pressure", same), ("mounting", text_equal), ("certification", text_equal)):
        if blank(m.get(attr)):
            unverified.append(attr)
        elif not equal(m.get(attr), req.get(attr)):
            return None
    have = num(m.get("stroke"))
    if have is None:
        unverified.append("stroke")
    elif stroke is not None:
        if have != stroke:
            return None
    else:                                  # usable travel, reserve unknown: nominal stroke must at least cover it
        usable = num(req.get("stroke"))
        if usable is None or have < usable:
            return None
        unverified.append("stroke")
    return unverified


def reserve_fallback(cands: list[dict], req: dict, stroke: Decimal | None) -> dict | None:
    """For a usable-travel requirement whose reserve is unknown: the ONLY active candidate, otherwise verified, whose
    nominal stroke lies above the usable travel (equal = no reserve). Several such candidates: no guess, since the
    reserve decides between them."""
    usable = num(req.get("stroke"))
    if stroke is not None or req.get("stroke_kind") != "usable" or usable is None:
        return None
    fits = [c for c in cands if c["active"] is True and c["unverified"] == ["stroke"]
            and num(c["stroke"]) is not None and num(c["stroke"]) > usable]
    return fits[0] if len(fits) == 1 else None


def match_positions(tender: dict, requirements: dict, materials: dict, mail: dict, current: str,
                    priced=frozenset()) -> dict:
    """`priced`: normalised numbers of materials with a price condition read (preferred among equal candidates)."""
    out = {}
    for p in (tender or {}).get("positions") or []:
        pos = str(p.get("position")).strip()
        ref = next((r for r in requirements if norm_ref(r) == norm_ref(p.get("requirement_ref"))), None)
        req = requirements.get(ref) if ref else None
        if not req:
            out[pos] = {"material": None, "reason": f"requirement {p.get('requirement_ref')} not found",
                        "candidates": [], "unverified": []}
            continue
        stroke = required_stroke(req, mail, current)
        group = norm_ref(req.get("kataloggruppe"))
        cands = []
        for m in materials.values():
            if group and norm_ref(m.get("kataloggruppe")) and norm_ref(m.get("kataloggruppe")) != group:
                continue
            unverified = check(m, req, stroke)
            if unverified is None:
                continue
            active = m.get("is_active")
            cands.append({"material": m["material"], "status": m.get("status_code"), "unverified": unverified,
                          "stroke": m.get("stroke"),
                          "active": True if active in (True, "true") else False if active in (False, "false") else None})
        # best first: active before unknown status, fully verified before unverified, then the shortest fitting stroke
        usable = [c for c in cands if c["active"] is True and not c["unverified"]]
        usable.sort(key=lambda c: (c["active"] is not True, len(c["unverified"]),
                                   norm_ref(c["material"]) not in priced, num(c["stroke"]) or Decimal(0)))
        required = {k: req.get(k) for k in ("bore", "pressure", "mounting", "certification", "kataloggruppe")}
        required["stroke"] = None if stroke is None else format(stroke.normalize(), "f")
        if usable:
            best = usable[0]
            reason = ("exact active match" if best["active"] and not best["unverified"] else
                      "exact match, status not read" if not best["unverified"] else
                      f"match, unverified: {', '.join(best['unverified'])}"
                      + ("" if best["active"] else ", status not read"))
            out[pos] = {"material": best["material"], "required": required, "candidates": cands, "reason": reason,
                        "unverified": best["unverified"] + ([] if best["active"] else ["status"])}
        elif fallback := reserve_fallback(cands, req, stroke):
            # Usable travel whose end reserve is unread (no technical release found), one candidate fits anyway.
            out[pos] = {"material": fallback["material"], "required": required, "candidates": cands,
                        "reason": "usable travel, end reserve not read: the only active stroke above it",
                        "unverified": ["end reserve"]}
        else:
            out[pos] = {"material": None, "required": required, "candidates": cands, "unverified": [],
                        "reason": "only blocked matches" if cands and all(c["active"] is False and not c["unverified"] for c in cands) else "no verified active exact match among the ERP records read"}
    return out


def search_candidates(cache: dict, item: dict) -> list[str]:
    """Material numbers of the catalogue group `item` (a scraper work item) that fit one of its requirements on the
    pages read so far: the materials worth opening. Same tolerant check as the matcher (stroke may still differ by
    the reserve, so it only has to cover the usable travel / be unknown)."""
    group = norm_ref(item.get("group"))
    materials = merge_materials(list(cache.values()), [item.get("group")] if item.get("group") else [])
    out = []
    for m in materials.values():
        if group and norm_ref(m.get("kataloggruppe")) and norm_ref(m.get("kataloggruppe")) != group:
            continue
        if group and not norm_ref(m.get("kataloggruppe")) and "list" not in m.get("seen_in", []):
            continue
        for req in item.get("requirements") or []:
            loose = dict(req, stroke_kind="usable") if req.get("stroke_kind") == "usable" else req
            stroke = None if loose.get("stroke_kind") == "usable" else num(req.get("stroke"))
            if check(m, loose, stroke, min_known=0) is not None:      # unread attributes: open it and see
                out.append(m["material"])
                break
    return out


async def refresh(ctx) -> dict:
    """Extract new ERP pages, publish results["erp"] and match every position again; returns the match."""
    requirements = ctx.board.get("requirements") or {}
    groups = sorted({str(r.get("kataloggruppe")) for r in requirements.values() if r.get("kataloggruppe")})
    return await publish(ctx, await extract_erp(ctx))


async def publish(ctx, cache=None) -> dict:
    """Reconcile completed extractions without spending another model call."""
    requirements = ctx.board.get("requirements") or {}
    groups = sorted({str(r.get("kataloggruppe")) for r in requirements.values() if r.get("kataloggruppe")})
    erp = erp_view(ctx.shared.get("erp.extracted", {}) if cache is None else cache, groups)
    await ctx.board.set("erp", erp)
    mail = ctx.board.get("mail") or {}
    spec = ctx.board.get("spec") or {}
    priced = {norm_ref(c.get("material")) for c in erp["conditions"] if c.get("scales")}
    result = match_positions(ctx.board.get("tender") or {}, requirements, erp["materials"], mail,
                             spec.get("current", ""), priced)
    await ctx.board.set("materials", result)
    return result


@register
class MaterialMatcher(Agent):
    phase = Phase.COLLECT

    async def run(self) -> None:
        await self.ctx.board.wait_for(key="done.erp", timeout=max(1.0, self.ctx.time_left()))
        if not pages(self.ctx, "erp"):
            raise RuntimeError("no ERP pages were saved")
        try:
            await self.ctx.board.wait_for(key="requirements_final", timeout=max(1.0, min(90, self.ctx.time_left())))
            await self.ctx.board.wait_for(key="mail", timeout=max(1.0, min(90, self.ctx.time_left())))  # end reserves
        except TimeoutError:
            self.log.warning("matching without final requirements or mail")
        result = await refresh(self.ctx)
        self.log.info("materials: %s", {p: (r["material"], r["reason"]) for p, r in result.items()})
