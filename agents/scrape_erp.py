# OWNER: (name)   ROLE: scraper   SITE: our ERP (role "erp")
"""Looks up ERP candidates for the required cylinders, saved as page evidence ("erp.page").

The ERP only lists records for narrow searches, so the work starts once the requirements are known
(results["requirements"], from agents/analyze_requirements.py). While waiting, the main tab of browser 2 logs in and
passes the ERP's gates. Then the work runs as small jobs on up to ERP_TABS tabs of browser 2 (they share the login):

1. search: one job per catalogue group (Kataloggruppe): search it, save the result list. Nothing else.
2. As soon as a list is saved, the materials on it that fit a requirement (agents/analyze_materials.search_candidates:
   bore, pressure, mounting, certification equal where shown; stroke equal or covering the usable travel) become three
   jobs each, run in parallel: record (all tabs incl. status and lead times), prices (condition records with the
   quantity-scale register open) and stock (storage-location level).
   A material whose record turns out blocked gets no price/stock job (they wait behind the record jobs).

Small jobs finish inside their step budget (one big job per group used to run out of steps before prices and stock),
and no step is spent on materials the list already rules out. results["done.erp"] is set when the queue is empty.
"""
import asyncio
import itertools

from agents.analyze_materials import extract_erp, merge_materials, search_candidates
from swarm import Phase, register  # noqa: F401
from swarm.browser_agent import Tab
from swarm.blackboard import Evidence
from swarm.context import SUBMIT_RESERVE_S
from swarm.normalize import norm_ref
from swarm.scraper import SiteScraper
from swarm.sites import site_for

ANALYSIS_TIME_S = 60    # what the material matcher needs after the last ERP page
ERP_TABS = 8             # parallel ERP jobs at most (main tab included)

HINTS = ("Adapt to the observed software; these are possible patterns, not requirements. ERP habits: SAP-style transaction codes (start page / menu lists transactions for material display, stock "
         "overview, price conditions); system notice or access gate after login (copy displayed number exactly - "
         "read_page shows numbers drawn on canvas -, confirm, continue; slider/robot gate passes with ONE fill_form: "
         "slider = its maximum (e.g. 100), robot checkbox = true, then the number and confirmation checkbox, then_click "
         "continue button); material search help (F4) lists results only for narrow search term; records with several "
         "tabs/registers (basic data, technical data, planning/status with lead times; condition header and quantity "
         "scales; stock per plant and per storage location); status codes explained in legend or tooltip; short session "
         "timeout - log in again when login page appears. Save steps: once you see the links of a record's "
         "tabs/registers, read all with ONE read_page(urls=[...]); transactions often take the material number "
         "directly in their entry field.")

SEARCH, RECORD, PRICES, STOCK = 0, 1, 2, 2       # job priorities: searches first, then records, then prices/stock


def work_items(requirements: dict) -> list[dict]:
    """One item per catalogue group (or per requirement when no group is given), with the attributes to check."""
    items: dict[str, dict] = {}
    for ref, req in (requirements or {}).items():
        group = (req.get("kataloggruppe") or "").strip()
        key = group or f"req:{ref}"
        item = items.setdefault(key, {"group": group, "requirements": []})
        item["requirements"].append({k: req.get(k) for k in ("bore", "stroke", "stroke_kind", "pressure", "mounting",
                                                              "certification")} | {"ref": ref})
    return list(items.values())


def describe(item: dict) -> str:
    return "\n".join(f"- requirement {r['ref']}: bore {r['bore']} mm, stroke {r['stroke']} mm"
                     f"{' (usable travel; nominal stroke will be longer)' if r.get('stroke_kind') == 'usable' else ''}, "
                     f"pressure {r['pressure']} bar, mounting {r['mounting']}, certification {r['certification']}"
                     for r in item["requirements"])


def search_goal(item: dict) -> str:
    what = f"catalogue group (Kataloggruppe) {item['group']}" if item["group"] else "these technical attributes"
    term = item["group"] or "the attributes"
    return (f"Find our ERP materials for {what}. Required:\n{describe(item)}\n"
            "1. Log in if needed, open material search (search help of material display transaction).\n"
            f"2. Search {term}; read_page the result list with topic 'erp search {item['group']}' (all result pages "
            "via next_page_link).\n"
            "3. done, listing the materials of the list with their attributes. Do NOT open records (other agents do).")


def material_goal(job: str, material: str, item: dict) -> str:
    group = f" (catalogue group {item['group']})" if item.get("group") else ""
    find = (f"Open material {material}{group}: type the number into the transaction's material field, or search "
            f"{item.get('group') or material} in its search help and click the row of {material}.")
    if job == "record":
        return (f"{find}\nread_page EVERY tab/register of its record (basic data, technical data, planning/status with "
                f"lead times and status; status legend or tooltip too) with topic 'erp material {material}', in one "
                "read_page(urls=[...]) when the tabs are links. done with status and lead times.")
    if job == "prices":
        return (f"Price conditions of material {material}{group}: open the price condition display transaction for "
                f"it, open its quantity-scale register so every scale row shows, read_page with topic 'erp prices "
                f"{material}'; several condition records (validity periods) -> read each one (next_page_link = exact "
                "text of link to next record). done with validity periods and scale prices.")
    return (f"Stock of material {material}{group}: open the stock overview transaction for it, open storage-location "
            f"level so quantity per location and stock type shows, read_page with topic 'erp stock {material}'. done "
            "with freely usable quantity per location.")


async def _items(ctx) -> list[dict]:
    requirements = await ctx.board.wait_for(key="requirements", timeout=max(1.0, ctx.time_left()))
    used = {str(p.get("requirement_ref") or "").strip().upper()
            for p in ((ctx.board.get("tender") or {}).get("positions") or [])}
    if used - {""}:                       # only requirements some position refers to
        requirements = {k: v for k, v in requirements.items() if k.strip().upper() in used} or requirements
    items = work_items(requirements)
    await ctx.board.set("erp.work_items", items)
    return items


@register
class ErpScraper(SiteScraper):
    role = "erp"
    browser = 2
    max_steps = 16
    hints = HINTS

    def __init__(self, ctx):
        super().__init__(ctx)
        self.queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self.pending = 0
        self.producer_done = False
        self.seen: set = set()
        self._order = itertools.count()

    def add(self, priority: int, job: tuple) -> None:
        key = job[:2] if job[0] != "search" else ("search", job[1]["group"], repr(job[1]["requirements"]))
        if key in self.seen:
            return
        self.seen.add(key)
        self.pending += 1
        self.queue.put_nowait((priority, next(self._order), job))

    def blocked(self, material: str) -> bool:
        """True if the material's record page (read meanwhile) says it is blocked: its prices and stock are not needed."""
        info = merge_materials(list(self.ctx.shared.get("erp.extracted", {}).values())).get(norm_ref(material)) or {}
        return info.get("is_active") is False

    async def extract_job(self, out: dict) -> dict:
        evidence = []
        for record in out.get("records") or []:
            data = record.get("data") or {}
            text = data.get("text") or data.get("_observed_text")
            if text:
                evidence.append(Evidence("erp.page", self.site_id,
                    {**data, "text": text, "url": data.get("url") or record.get("source"),
                     "topic": data.get("topic") or record.get("kind", "")}))
        return await extract_erp(self.ctx, evidence=evidence)

    async def do(self, job: tuple, tab: Tab | None) -> None:
        stop = SUBMIT_RESERVE_S + ANALYSIS_TIME_S
        if job[0] == "search":
            item = job[1]
            out = await self.browse_until_done(search_goal(item), attempts=2, tab=tab, max_steps=14, stop_reserve=stop)
            found = search_candidates(await self.extract_job(out), item)
            self.log.info("search %s: candidates %s", item["group"], found)
            for material in found:
                self.add(RECORD, ("record", material, item))
            return
        kind, material, item = job
        if kind != "record" and self.blocked(material):
            self.log.info("%s %s skipped: material blocked", kind, material)
            return
        out = await self.browse_until_done(material_goal(kind, material, item), attempts=2, tab=tab, max_steps=16,
                                     stop_reserve=stop)
        await self.extract_job(out)
        if kind == "record" and not self.blocked(material):
            self.add(PRICES, ("prices", material, item))
            self.add(STOCK, ("stock", material, item))

    async def worker(self, tab: Tab | None) -> None:
        """Takes jobs until the queue is empty and no running job can add more (or time is up)."""
        try:
            while self.ctx.time_left() > ANALYSIS_TIME_S:
                try:
                    _, _, job = self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    if self.pending <= 0 and self.producer_done:
                        return
                    await asyncio.sleep(1.0)
                    continue
                try:
                    await self.do(job, tab)
                except Exception as error:
                    self.ctx.board.error(self.name, f"{job[0]} {job[1] if job[0] != 'search' else job[1]['group']}: "
                                                    f"{error}")
                finally:
                    self.pending -= 1
        finally:
            if tab is not None:
                await tab.close()

    async def run(self) -> None:
        try:
            self.site_id = self.site_id or await site_for(self.ctx, self.role)
            if self.ctx.board.get("requirements") is None:      # use the wait: log in and pass the ERP's gates now
                await self.browse("Log in, accept cookies, pass any system notice or access gate, open material search "
                                  "help of material display transaction. Do NOT search yet. done once search form open.",
                                  max_steps=12)
            async def produce():
                try:
                    while self.ctx.time_left() > ANALYSIS_TIME_S:
                        for item in await _items(self.ctx):
                            self.add(SEARCH, ("search", item))
                        if self.ctx.board.get("requirements_final"):
                            return
                        await asyncio.sleep(2)
                finally:
                    self.producer_done = True
            async with asyncio.TaskGroup() as group:
                group.create_task(produce())
                group.create_task(self.worker(None))
                for _ in range(ERP_TABS - 1):
                    group.create_task(self.worker(Tab(self.browser)))
        finally:
            await self.ctx.board.set("done.erp", True)
