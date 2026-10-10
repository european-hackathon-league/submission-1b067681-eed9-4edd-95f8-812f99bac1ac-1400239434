# OWNER: (name)   ROLE: scraper   SITE: our ERP (role "erp")
"""Looks up ERP candidates for the required cylinders, saved as page evidence ("erp.page").

The ERP only lists records for narrow searches, so the work starts once the requirements are known
(results["requirements"], from agents/analyze_requirements.py): one work item per catalogue group (Kataloggruppe).
Items sit in a shared queue; ErpScraper (browser 2) works on it, ErpHelper joins on browser 1 once the spec scraper
has released it and ErpHelperTender on browser 0 after the tender scraper. results["done.erp"] is set when every item
has been worked on.
"""
import asyncio

from swarm import Phase, register  # noqa: F401
from swarm.context import SUBMIT_RESERVE_S
from swarm.scraper import SiteScraper
from swarm.sites import site_for

ANALYSIS_TIME_S = 120    # what the material matcher needs after the last ERP page

HINTS = ("ERP habits: SAP-style transaction codes (the start page / menu lists the transactions for material display, "
         "stock overview and price conditions); a system notice or access gate after login (copy the displayed number "
         "exactly - read_page shows numbers drawn on canvas -, confirm, continue; a slider/robot gate can be passed "
         "with ONE fill_form: slider = its maximum (e.g. 100), robot checkbox = true, then the number and the "
         "confirmation checkbox, then_click the continue button); a material search help (F4) that only lists "
         "results for a narrow search term; records with several tabs or registers (basic data, technical data, "
         "planning/status with lead times; condition header and quantity scales; stock per plant and per storage "
         "location); status codes explained in a legend or tooltip; a short session timeout - log in again when the "
         "login page appears. Save steps: once you have seen the links of a record's tabs/registers, read them all "
         "with ONE read_page(urls=[...]) call, and use the same URL pattern for the next candidate.")


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


def item_goal(item: dict) -> str:
    reqs = "\n".join(f"- requirement {r['ref']}: bore {r['bore']} mm, stroke {r['stroke']} mm"
                     f"{' (usable travel; the nominal stroke will be longer)' if r.get('stroke_kind') == 'usable' else ''}, "
                     f"pressure {r['pressure']} bar, mounting {r['mounting']}, certification {r['certification']}"
                     for r in item["requirements"])
    what = f"catalogue group (Kataloggruppe) {item['group']}" if item["group"] else "these technical attributes"
    return (
        f"Find our ERP materials for {what}. Required:\n{reqs}\n"
        "1. Log in if needed and open the material search (search help of the material display transaction). Search "
        f"for {item['group'] or 'the attributes'} and read_page the result list with topic 'erp search {item['group']}'.\n"
        "2. Candidates are results whose bore, pressure, mounting and certification match (the stroke may differ "
        "slightly; keep candidates with any stroke). For each candidate open the material record and read_page EVERY "
        "tab of it (technical data; planning/status with lead times) with topic 'erp material <number>'.\n"
        "3. For each candidate whose status is active/released (not blocked): read_page its price conditions with "
        "the quantity-scale register open (topic 'erp prices <number>'); if there are several condition records, "
        "pass next_page_link with the exact text of the link to the next condition record so all are read. Then "
        "read_page its stock: open the storage-location level and the freely usable location so the quantity is "
        "shown (topic 'erp stock <number>').\n"
        "Do not stop after the first candidate. Finish with done listing the candidates and their status.")


async def _queue(ctx):
    async with ctx.lock("erp.queue"):
        if "erp.queue" not in ctx.shared:
            requirements = await ctx.board.wait_for(key="requirements", timeout=max(1.0, ctx.time_left()))
            used = {str(p.get("requirement_ref") or "").strip().upper()
                    for p in ((ctx.board.get("tender") or {}).get("positions") or [])}
            if used - {""}:                       # only requirements some position refers to
                requirements = {k: v for k, v in requirements.items() if k.strip().upper() in used} or requirements
            items = work_items(requirements)
            queue = asyncio.Queue()
            for item in items:
                queue.put_nowait(item)
            ctx.shared["erp.queue"] = queue
            ctx.shared["erp.pending"] = len(items)
            await ctx.board.set("erp.work_items", items)
        return ctx.shared["erp.queue"]


@register
class ErpScraper(SiteScraper):
    role = "erp"
    browser = 2
    max_steps = 30
    hints = HINTS

    async def work(self) -> None:
        queue = await _queue(self.ctx)
        while self.ctx.time_left() > ANALYSIS_TIME_S:
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:   # stop browsing early enough for the matcher to extract and match before collecting ends
                await self.browse_until_done(item_goal(item), attempts=2,
                                             stop_reserve=SUBMIT_RESERVE_S + ANALYSIS_TIME_S)
            except Exception as error:
                self.ctx.board.error(self.name, f"{item.get('group')}: {error}")
            finally:
                self.ctx.shared["erp.pending"] -= 1
                if self.ctx.shared["erp.pending"] <= 0:
                    await self.ctx.board.set("done.erp", True)

    async def run(self) -> None:
        try:
            self.site_id = self.site_id or await site_for(self.ctx, self.role)
            if self.ctx.board.get("requirements") is None:      # use the wait: log in and pass the ERP's gates now
                await self.browse("Log in, accept cookies, pass any system notice or access gate, and open the "
                                  "material search help of the material display transaction. Do NOT search yet. "
                                  "Finish with done once the search form is open.", max_steps=12)
            await self.work()
        finally:                                    # wait for the helper's last item, then release the analysts
            while self.ctx.shared.get("erp.pending", 0) > 0 and self.ctx.time_left() > ANALYSIS_TIME_S:
                await asyncio.sleep(2)
            await self.ctx.board.set("done.erp", True)


@register
class ErpHelper(ErpScraper):
    """Second ERP worker on browser 1, after the spec scraper is done with it."""
    browser = 1
    after = "done.spec"

    async def run(self) -> None:
        try:
            await self.ctx.board.wait_for(key=self.after, timeout=max(1.0, self.ctx.time_left()))
            self.site_id = self.site_id or await site_for(self.ctx, self.role)
            await self.work()
        except TimeoutError:
            return


@register
class ErpHelperTender(ErpHelper):
    """Third ERP worker on browser 0, after the tender scraper is done with it (the submitter needs it only later)."""
    browser = 0
    after = "done.tender"
