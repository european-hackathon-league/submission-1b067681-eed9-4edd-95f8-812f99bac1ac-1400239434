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

Scheduling: the next job is chosen when a tab gets free, by what it adds to a submit-ready answer (`priority`): first
focused re-reads of something the bid calculation still misses, then prices and stock of materials that already match
a position (they finish that position), then searches and the best candidate record per requirement, then further
candidates; records of candidates for positions that already have a verified active match come last. A tab never
waits for its page's extraction: that runs in the background and queues the follow-up jobs when it is done. After a
material's prices / stock / record were read, the calculator's open dependencies for it (a condition without its price
unit, scales, stock, lead times, carton size, ...) queue one focused re-read naming exactly what is missing.
"""
import os
import asyncio
import itertools

from agents.analyze_materials import extract_erp, merge_materials, search_candidates
from swarm import Phase, register, timeline  # noqa: F401
from swarm.browser_agent import Tab
from swarm.blackboard import Evidence
from swarm.normalize import norm_ref
from swarm.scraper import SiteScraper
from swarm.sites import site_for

ANALYSIS_TIME_S = 60    # what the material matcher needs after the last ERP page
# parallel ERP jobs (main tab included). 8 once stalled every sandbox browser at once; A/B on 2026-10-11 (same code):
# 6 tabs 17 ERP jobs / 4 positions priced / 10 failed browses (run_1efbfc7e6d2895e0) vs 4 tabs 12 / 1 / 16
# (run_db181692d8232b97); 4 tabs in run_7a11d3a90d55e1ce: 15 / 3. ERP throughput is the collection bottleneck.
ERP_TABS = int(os.environ.get("SWARM_ERP_TABS", "6"))

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

FOLLOWUP, FINISH, SEARCH, BEST_RECORD, RECORD, SPECULATIVE, COVERED = 0, 1, 2, 2, 3, 4, 6
MAX_FOCUSED = 1          # focused re-reads per (job kind, material) after the first read


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


def material_goal(job: str, material: str, item: dict, missing: str = "") -> str:
    group = f" (catalogue group {item['group']})" if item.get("group") else ""
    find = (f"Open material {material}{group}: type the number into the transaction's material field, or search "
            f"{item.get('group') or material} in its search help and click the row of {material}.")
    focus = (f"\nA previous read of it did NOT show: {missing}. Find exactly that (other tab/register/record, "
             "header vs. detail view, tooltip) and read_page the page that shows it.") if missing else ""
    if job == "record":
        return (f"{find}\nread_page EVERY tab/register of its record (basic data, technical data, planning/status with "
                f"lead times and status; status legend or tooltip too) with topic 'erp material {material}', in one "
                f"read_page(urls=[...]) when the tabs are links. done with status and lead times.{focus}")
    if job == "prices":
        return (f"Price conditions of material {material}{group}: open the price condition display transaction for "
                f"it, open its quantity-scale register so every scale row shows, read_page with topic 'erp prices "
                f"{material}' (the header with validity, price unit and condition record number must be in the read "
                "too); several condition records (validity periods) -> read each one (next_page_link = exact "
                f"text of link to next record). done with validity periods, price unit and scale prices.{focus}")
    return (f"Stock of material {material}{group}: open the stock overview transaction for it, open storage-location "
            f"level so quantity per location and stock type shows, read_page with topic 'erp stock {material}'. done "
            f"with freely usable quantity per location.{focus}")


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
        self.jobs: list[tuple[int, tuple]] = []      # (insertion order, job); the next job is chosen when taken
        self.pending = 0                               # queued + running jobs + background extractions
        self.producer_done = False
        self.seen: set = set()
        self.focused: dict[tuple, int] = {}
        self.rank: dict[str, int] = {}                 # material -> its place among its search's candidates
        self._order = itertools.count()
        self.background: set[asyncio.Task] = set()

    # ---- queue ----------------------------------------------------------------------------------------------------
    def add(self, job: tuple, force: bool = False) -> bool:
        key = job[:2] if job[0] != "search" else ("search", job[1]["group"], repr(job[1]["requirements"]))
        if key in self.seen and not force:
            return False
        self.seen.add(key)
        self.pending += 1
        self.jobs.append((next(self._order), job))
        return True

    def matched(self) -> dict[str, list[str]]:
        """material -> positions it currently matches (verified active exact match)."""
        out: dict[str, list[str]] = {}
        for pos, m in (self.ctx.board.get("materials") or {}).items():
            if m.get("material") and not m.get("unverified"):
                out.setdefault(norm_ref(m["material"]), []).append(pos)
        return out

    def positions_of(self, item: dict) -> list[str]:
        refs = {norm_ref(r.get("ref")) for r in item.get("requirements") or []}
        return [str(p.get("position")) for p in (self.ctx.board.get("tender") or {}).get("positions") or []
                if norm_ref(p.get("requirement_ref")) in refs]

    def priority(self, job: tuple) -> int:
        """What the job adds to a submit-ready answer (lower first)."""
        kind = job[0]
        if kind == "search":
            return SEARCH
        material, item = job[1], job[2]
        matched = self.matched()
        if len(job) > 3 and job[3]:                    # focused re-read of something the calculation misses
            return FOLLOWUP
        if kind in ("prices", "stock"):
            return FINISH if norm_ref(material) in matched else SPECULATIVE
        positions = self.positions_of(item)
        covered = {p for ps in matched.values() for p in ps}
        if positions and all(p in covered for p in positions):
            return COVERED
        return BEST_RECORD if self.rank.get(norm_ref(material), 0) == 0 else RECORD

    def take(self) -> tuple | None:
        if not self.jobs:
            return None
        best = min(self.jobs, key=lambda j: (self.priority(j[1]), j[0]))
        self.jobs.remove(best)
        return best[1]

    def blocked(self, material: str) -> bool:
        """True if the material's record page (read meanwhile) says it is blocked: its prices and stock are not needed."""
        info = merge_materials(list(self.ctx.shared.get("erp.extracted", {}).values())).get(norm_ref(material)) or {}
        return info.get("is_active") is False

    # ---- jobs -----------------------------------------------------------------------------------------------------
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

    def spawn(self, coro) -> None:
        """Background work that may queue jobs: counted as pending so no worker quits while it runs."""
        self.pending += 1
        task = asyncio.ensure_future(coro)
        self.background.add(task)
        self.ctx.shared.setdefault("background", set()).add(task)

        def finished(t):
            self.pending -= 1
            self.background.discard(t)
            if not t.cancelled() and t.exception() is not None:
                self.ctx.board.error(self.name, f"extraction: {type(t.exception()).__name__}: {t.exception()}")
        task.add_done_callback(finished)

    async def after_search(self, item: dict, out: dict) -> None:
        found = search_candidates(await self.extract_job(out), item)
        self.log.info("search %s: candidates %s", item["group"], found)
        for i, material in enumerate(found):
            self.rank.setdefault(norm_ref(material), i)
            self.add(("record", material, item))

    async def after_material(self, kind: str, material: str, item: dict, out: dict) -> None:
        await self.extract_job(out)
        from agents import analyze_materials
        await analyze_materials.publish(self.ctx)            # the match (and so the priorities) follows at once
        if kind == "record" and not self.blocked(material):
            self.add(("prices", material, item))
            self.add(("stock", material, item))
        self.focus(kind, material, item)

    def missing_for(self, kind: str, material: str) -> str:
        """What the bid calculation still misses from this material's `kind` pages ('' if nothing / not matched)."""
        from agents.compute_bid import build_bid
        try:
            bid = build_bid(self.ctx)
        except Exception as error:  # noqa: BLE001
            self.log.debug("draft for focus failed: %s", error)
            return ""
        dep = (bid.get("dependencies") or {}).get(f"erp {kind} {norm_ref(material)}")
        return dep["need"] if dep else ""

    def focus(self, kind: str, material: str, item: dict) -> None:
        """One focused re-read when the calculation still misses something this kind of page should show."""
        if norm_ref(material) not in self.matched() or self.blocked(material):
            return
        missing = self.missing_for(kind, material)
        key = (kind, norm_ref(material))
        if missing and self.focused.get(key, 0) < MAX_FOCUSED:
            self.focused[key] = self.focused.get(key, 0) + 1
            self.log.info("%s %s still misses: %s -> focused re-read", kind, material, missing[:160])
            timeline.count("erp.focused")
            self.add((kind, material, item, missing), force=True)

    async def do(self, job: tuple, tab: Tab | None) -> None:
        extra = ANALYSIS_TIME_S
        if job[0] == "search":
            item = job[1]
            out = await self.browse_until_done(search_goal(item), attempts=2, tab=tab, max_steps=14, stop_extra=extra)
            self.spawn(self.after_search(item, out))
            return
        kind, material, item = job[:3]
        missing = job[3] if len(job) > 3 else ""
        if kind != "record" and self.blocked(material):
            self.log.info("%s %s skipped: material blocked", kind, material)
            return
        out = await self.browse_until_done(material_goal(kind, material, item, missing), attempts=2, tab=tab,
                                           max_steps=16, stop_extra=extra)
        self.spawn(self.after_material(kind, material, item, out))

    async def worker(self, tab: Tab | None) -> None:
        """Takes jobs until none is queued and no running job or extraction can add more (or time is up)."""
        try:
            while self.ctx.time_left() > ANALYSIS_TIME_S:
                job = self.take()
                if job is None:
                    if self.pending <= 0 and self.producer_done:
                        return
                    await asyncio.sleep(1.0)
                    continue
                label = f"{job[0]} {job[1] if job[0] != 'search' else job[1]['group']}"
                try:
                    with timeline.span("erp.job", label):
                        await self.do(job, tab)
                except Exception as error:
                    self.ctx.board.error(self.name, f"{label}: {error}")
                finally:
                    self.pending -= 1
        finally:
            if tab is not None:
                await tab.close()

    async def run(self) -> None:
        try:
            self.site_id = self.site_id or await site_for(self.ctx, self.role)
            if self.ctx.board.get("requirements") is None:      # use the wait: log in and pass the ERP's gates now
                await self.browse_until_done("Log in, accept cookies, pass any system notice or access gate, open "
                                             "material search help of material display transaction. Do NOT search "
                                             "yet. done once search form open.", attempts=1, max_steps=12)

            async def produce():
                try:
                    while self.ctx.time_left() > ANALYSIS_TIME_S:
                        for item in await _items(self.ctx):
                            self.add(("search", item))
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
            if self.background:                       # the last extractions (time is up for new jobs)
                await asyncio.wait(set(self.background), timeout=max(1.0, self.ctx.time_left()))
        finally:
            await self.ctx.board.set("done.erp", True)
