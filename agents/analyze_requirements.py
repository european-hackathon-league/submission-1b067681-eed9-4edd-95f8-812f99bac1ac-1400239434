# OWNER: (name)   ROLE: analyst   NEEDS: spec.page (done.spec), results["tender"]   WRITES: results["spec"], results["requirements"]
"""Requirements of the specification revision marked current, with the tender Q&A's scoped amendments applied.

results["spec"] = {"revisions": [{"revision", "date", "status", "is_current"}], "current": "R-..."}
results["requirements"] = {ref: {"bore", "stroke", "stroke_kind": "nominal"|"usable", "pressure", "mounting",
    "certification", "kataloggruppe", "pieces_per_set", "reserve_key", "revision", "amended": [..]}}
Numbers are plain strings without locale noise (e.g. "200"); stroke is the value after amendments (usable travel if
stroke_kind == "usable"; the matcher adds the end reserves from the technical release mail).
It is written up to three times: early, as soon as a requirement table page is saved (so the ERP search can start
while the spec scraper finishes), then from all spec pages, then with amendments, followed by
results["requirements_final"] = True.
"""
import asyncio
import logging

from swarm import Agent, Phase, register
from swarm.extract import extract, page_block, pages
from swarm.normalize import norm_ref, to_datetime, to_decimal

log = logging.getLogger("RequirementAnalyzer")

SHAPE = """{"revisions": [{"revision": "", "date": "ISO date (mind the stated MM/DD or DD/MM order)", "status": "as shown",
               "is_current": true/false}],
 "requirements": [{"revision": "revision these values belong to", "ref": "requirement reference as shown",
   "bore": "number as shown incl. locale marker", "stroke": "number as shown incl. locale marker",
   "stroke_kind": "nominal, or usable if the stroke is a usable travel (nutzbarer Weg) that needs end reserves",
   "pressure": "number as shown incl. locale marker", "mounting": "", "certification": "",
   "kataloggruppe": "catalogue group to search in the ERP", "pieces_per_set": "pieces per set (Satz) if stated, else null",
   "reserve_key": "technical release / end reserve key if referenced, else null",
   "original_text": "the requirement's original wording if shown"}]}"""

NUMERIC = ("bore", "stroke", "pressure")


def plain_number(text) -> str | None:
    value = to_decimal(text)
    return None if value is None else format(value.normalize(), "f")


def apply_amendments(requirements: dict, current: str, clarifications: list[dict]) -> dict:
    """Latest dated amendment per (requirement, attribute) that names the current revision; other revisions ignored."""
    fields = {"bore": ("bohrung", "bore", "durchmesser"), "stroke": ("hub", "stroke", "stellweg", "weg"),
              "pressure": ("druck", "pressure"), "mounting": ("befestigung", "mounting"),
              "certification": ("zertifi", "certification", "prüf")}
    amendments = sorted((c for c in clarifications if c.get("amendment")),
                        key=lambda c: to_datetime(c.get("date")) or to_datetime("1970-01-01"))
    for c in amendments:
        a = c["amendment"]
        if norm_ref(a.get("revision")) != norm_ref(current):
            continue
        req = requirements.get(next((r for r in requirements if norm_ref(r) == norm_ref(a.get("requirement"))), ""))
        attr = next((f for f, words in fields.items() if any(w in (a.get("attribute") or "").lower() for w in words)), None)
        if req is None or attr is None or a.get("value") in (None, ""):
            continue
        req[attr] = plain_number(a["value"]) if attr in NUMERIC else str(a["value"]).strip()
        req.setdefault("amended", []).append(f"{attr} by Q&A {c.get('number')} ({c.get('date')})")
    return requirements


async def analyse(ctx) -> tuple[list, str, dict]:
    """Extract pages independently and publish every completed requirement table immediately."""
    cache = ctx.shared.setdefault("spec.extracted", {})
    async with ctx.lock("spec.extract"):
        async def one(e):
            key = (e.data.get("url"), hash(e.data.get("text", "")))
            if key in cache:
                return
            try:
                out = await extract(ctx, "Extract specification revision statuses and requirement rows. Mark current only with explicit valid/current status; never superseded.",
                                    SHAPE, [page_block(e)], list_key=("requirements", "revisions"))
                if out.get("_incomplete"):
                    return
                cache[key] = out
                revisions, current, requirements = merge_spec(ctx)
                if current:
                    await ctx.board.set("spec", {"revisions":revisions, "current":current})
                    ctx.shared["spec.base_requirements"] = requirements
                    await publish(ctx)
            except Exception as error:
                ctx.board.error("RequirementAnalyzer", f"spec extraction: {type(error).__name__}")
        await asyncio.gather(*(one(e) for e in pages(ctx, "spec")))
    return merge_spec(ctx)


def merge_spec(ctx) -> tuple[list, str, dict]:
    cache = ctx.shared.get("spec.extracted", {})
    completed = [cache.get((e.data.get("url"), hash(e.data.get("text", ""))), {}) for e in pages(ctx, "spec")]
    revisions = [r for page in completed for r in page.get("revisions") or []]
    flagged = [r for r in revisions if r.get("is_current") is True]
    flagged.sort(key=lambda r: to_datetime(r.get("date")) or to_datetime("1970-01-01"))
    current = flagged[-1]["revision"] if flagged else ""
    rows = [r for page in completed for r in page.get("requirements") or []]
    rows = [r for r in rows if current and norm_ref(r.get("revision")) == norm_ref(current)]
    requirements = {}
    for r in rows:
        ref = str(r.get("ref") or "").strip()
        if not ref:
            continue
        entry = {k: r.get(k) for k in ("mounting", "certification", "kataloggruppe", "reserve_key", "original_text")}
        entry |= {k: plain_number(r.get(k)) for k in NUMERIC}
        entry["stroke_kind"] = "usable" if "usable" in str(r.get("stroke_kind") or "").lower() else "nominal"
        entry["pieces_per_set"] = plain_number(r.get("pieces_per_set")) if r.get("pieces_per_set") else None
        entry["revision"] = r.get("revision") or current
        merged = requirements.setdefault(ref, {})
        merged.update({k: v for k, v in entry.items() if v not in (None, "")})
    return revisions, current, requirements


async def refresh(ctx, wait_tender: float = 0) -> dict:
    """Analyse the saved spec pages, apply the tender's amendments; publishes spec, requirements,
    requirements_final. `wait_tender`: seconds to wait for the tender analysis (amendments come from its Q&A)."""
    if not pages(ctx, "spec"):
        raise RuntimeError("no specification pages were saved")
    revisions, current, requirements = await analyse(ctx)
    if requirements:
        ctx.shared["spec.base_requirements"] = requirements
    await ctx.board.set("spec", {"revisions": revisions, "current": current})
    if requirements:
        await ctx.board.set("requirements", requirements)
    log.info("current revision %s, requirements %s", current, list(requirements))
    tender = ctx.board.get("tender")
    if tender is None and wait_tender > 0:
        tender = await ctx.board.wait_for(key="tender", timeout=wait_tender)
    return await publish(ctx)


async def publish(ctx) -> dict:
    """Reapply newly arrived Q&A to original revision facts, never to already amended values."""
    from copy import deepcopy
    base = ctx.shared.get("spec.base_requirements")
    if base is None:
        return ctx.board.get("requirements") or {}
    current = (ctx.board.get("spec") or {}).get("current", "")
    # A newer governing revision may have been observed before its requirements finish extracting.
    base = {k:v for k,v in base.items() if norm_ref(v.get("revision")) == norm_ref(current)}
    amended = apply_amendments(deepcopy(base), current, (ctx.board.get("tender") or {}).get("clarifications") or [])
    await ctx.board.set("requirements", amended)
    if ctx.board.get("done.spec") is not None and ctx.board.get("tender_final"):
        await ctx.board.set("requirements_final", True)
    return amended


@register
class RequirementAnalyzer(Agent):
    phase = Phase.COLLECT

    async def run(self) -> None:
        # Every newly saved spec page is a trigger. Topic spelling cannot delay ERP startup.
        seen = -1
        while self.ctx.time_left() > 0:
            count = len(pages(self.ctx, "spec"))
            if count and count != seen:
                try:
                    await refresh(self.ctx)
                    seen = count
                except Exception as error:
                    self.log.warning("spec extraction will retry: %s", type(error).__name__)
            if self.ctx.board.get("done.spec") is not None:
                if count and seen != count:
                    await refresh(self.ctx)
                # Tender amendments can arrive after the initial requirement table.
                try:
                    await self.ctx.board.wait_for(key="tender", timeout=max(1, self.ctx.time_left()))
                except TimeoutError:
                    return
                await refresh(self.ctx)
                return
            await asyncio.sleep(2)
