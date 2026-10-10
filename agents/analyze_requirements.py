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

from swarm import Agent, Phase, register
from swarm.extract import extract, page_block, pages
from swarm.normalize import norm_ref, to_datetime, to_decimal

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


@register
class RequirementAnalyzer(Agent):
    phase = Phase.COLLECT

    async def run(self) -> None:
        early = asyncio.create_task(self.early())
        try:
            await self.ctx.board.wait_for(key="done.spec", timeout=max(1.0, self.ctx.time_left()))
        finally:
            if not early.done():
                early.cancel()
        if not pages(self.ctx, "spec"):
            raise RuntimeError("no specification pages were saved")
        revisions, current, requirements = await self.analyse()
        await self.ctx.board.set("spec", {"revisions": revisions, "current": current})
        await self.ctx.board.set("requirements", requirements)
        self.log.info("current revision %s, requirements %s", current, list(requirements))
        tender = await self.ctx.board.wait_for(key="tender", timeout=max(1.0, self.ctx.time_left()))
        amended = apply_amendments({k: dict(v) for k, v in requirements.items()}, current,
                                   (tender or {}).get("clarifications") or [])
        await self.ctx.board.set("requirements", amended)
        await self.ctx.board.set("requirements_final", True)

    async def early(self) -> None:
        """As soon as a requirement table is saved, publish a first version so the ERP search can start while the
        spec scraper is still finishing (the final version replaces it)."""
        while True:
            topics = " ".join(str(e.data.get("topic", "")).lower() for e in pages(self.ctx, "spec"))
            if "requirement" in topics or "anforderung" in topics:
                revisions, current, requirements = await self.analyse()
                if requirements and self.ctx.board.get("requirements") is None:
                    await self.ctx.board.set("spec", {"revisions": revisions, "current": current})
                    await self.ctx.board.set("requirements", requirements)
                    self.log.info("early requirements (%s): %s", current, list(requirements))
                return
            await asyncio.sleep(3)

    async def analyse(self) -> tuple[list, str, dict]:
        blocks = [page_block(e) for e in pages(self.ctx, "spec")]
        out = await extract(self.ctx, "Extract the specification's revision list and the requirement rows (for every "
                                      "revision whose rows are on the pages). A revision is current only if its status "
                                      "says so (e.g. gültig/aktuell/current), never if superseded (ersetzt).",
                            SHAPE, blocks, list_key=("requirements", "revisions"))
        revisions = out.get("revisions") or []
        flagged = [r for r in revisions if r.get("is_current") is True]
        flagged.sort(key=lambda r: to_datetime(r.get("date")) or to_datetime("1970-01-01"))
        current = flagged[-1]["revision"] if flagged else ""
        rows = out.get("requirements") or []
        if current:
            rows = [r for r in rows if norm_ref(r.get("revision")) == norm_ref(current)] or rows
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
            requirements.setdefault(ref, entry)
        return revisions, current, requirements
