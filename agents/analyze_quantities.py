# OWNER: (name)   ROLE: analyst   NEEDS: tender.page (done.tender)   WRITES: results["tender"], results["quantities"]
"""Turns the saved tender pages into positions and Q&A records, then applies the quantity rules in code.

results["tender"] = {
  "meta": {"reference", "deadline", ...},
  "positions": [{"position", "short_text", "quantity" (as shown), "unit", "requirement_ref"}],
  "clarifications": [{"number", "date", "kind", "positions": [...], "new_quantity", "withdraws": [...],
                      "amendment": {...} | null, "keys": {...}, "setup_fee": str | null, "text"}]
}
results["quantities"] = {position: {"quantity": int (in the position's unit), "unit": str, "basis": str}}
Rule: tender quantity, overridden by the latest dated clarification that changes it; withdrawn answers are ignored.
"""
import asyncio
import logging

from swarm import Agent, Phase, register
from swarm.extract import extract, page_block, pages
from swarm.normalize import norm_ref, to_datetime, to_int

log = logging.getLogger("QuantityAnalyzer")

POSITIONS_SHAPE = """{"meta": {"reference": "", "title": "", "deadline": "", "timezone": ""},
 "positions": [{"position": "position number as shown", "short_text": "", "quantity": "as shown", "unit": "",
                "requirement_ref": "requirement reference as shown"}]}"""

QA_SHAPE = """{"clarifications": [{
  "number": "Q&A number", "date": "UTC ISO timestamp if shown, else as shown", "question": "", "answer": "full answer text",
  "kind": "quantity_change | withdrawal | technical_amendment | commercial_keys | setup_fee | other",
  "positions": ["position numbers this answer applies to"],
  "new_quantity": "new total quantity as shown, or null", "quantity_unit": "unit of that quantity or null",
  "withdraws": ["numbers of earlier answers this one EXPLICITLY withdraws/retracts (zurückgezogen); a newer value replacing an older one is not a withdrawal"],
  "amendment": {"document": "", "revision": "", "requirement": "", "attribute": "", "value": ""} or null,
  "keys": {"konditionsgruppe": null, "packaging_tariff": null, "delivery_key": null, "setup_pool": null, "bonus_plan": null},
  "setup_fee": "one-off setup cost amount as shown, or null"}]}"""


def withdrawn_numbers(clarifications: list[dict]) -> set[str]:
    """Numbers of answers explicitly withdrawn by a withdrawal answer (a newer value is not a withdrawal)."""
    return {norm_ref(n) for c in clarifications if c.get("kind") == "withdrawal" for n in (c.get("withdraws") or [])}


def final_quantities(positions: list[dict], clarifications: list[dict]) -> dict:
    withdrawn = withdrawn_numbers(clarifications)
    out = {}
    for p in positions:
        pos = str(p.get("position") or "").strip()
        if not pos:
            continue
        qty, basis = to_int(p.get("quantity")), "tender"
        changes = [c for c in clarifications
                   if c.get("new_quantity") not in (None, "") and norm_ref(c.get("number")) not in withdrawn
                   and pos in [str(x).strip() for x in (c.get("positions") or [])]]
        changes.sort(key=lambda c: (to_datetime(c.get("date")) is not None, to_datetime(c.get("date")) or 0,
                                    to_int(c.get("number")) or 0))
        if changes:
            latest = changes[-1]
            qty, basis = to_int(latest.get("new_quantity")), f"Q&A {latest.get('number')}"
        out[pos] = {"quantity": qty, "unit": p.get("unit"), "basis": basis}
    return out


def _key(e):
    return (e.data.get("url"), hash(e.data.get("text", "")))


async def publish(ctx) -> dict:
    """Reconcile completed page extractions in observation order, without another model call."""
    cache = ctx.shared.get("tender.extracted", {})
    meta, positions, clarifications = {}, {}, {}
    for e in pages(ctx, "tender"):
        for kind in ("positions", "clarifications"):
            result = cache.get((_key(e), kind), {})
            meta.update({k: v for k, v in (result.get("meta") or {}).items() if v})
            for row in result.get("positions") or []:
                pos = str(row.get("position") or "").strip()
                if pos:
                    positions.setdefault(pos, {}).update({k:v for k,v in row.items() if v not in (None, "")})
            for row in result.get("clarifications") or []:
                key = norm_ref(row.get("number")) or norm_ref(row.get("question"))
                if key:
                    clarifications[key] = row
    if not positions:
        return ctx.board.get("tender") or {}
    tender = {"meta":meta, "positions":list(positions.values()), "clarifications":list(clarifications.values())}
    await ctx.board.set("tender", tender)
    await ctx.board.set("quantities", final_quantities(tender["positions"], tender["clarifications"]))
    from agents import analyze_requirements
    await analyze_requirements.publish(ctx)
    return tender


async def refresh(ctx) -> dict:
    """Stream each completed extraction; a slow Q&A page cannot hide already known positions."""
    cache = ctx.shared.setdefault("tender.extracted", {})
    ref = (ctx.board.get("brief") or {}).get("tender_reference", "")
    async with ctx.lock("tender.extract"):
        async def one(e, kind, shape, instruction):
            key = (_key(e), kind)
            if key in cache:
                return
            try:
                result = await extract(ctx, instruction, shape, [page_block(e)], list_key=kind)
                if result.get("_incomplete"):
                    return
                cache[key] = result
                await publish(ctx)
            except Exception as error:
                ctx.board.error("QuantityAnalyzer", f"{kind} extraction: {type(error).__name__}")
        await asyncio.gather(*(one(e, kind, shape, instruction) for e in pages(ctx, "tender")
            for kind, shape, instruction in (
                ("positions", POSITIONS_SHAPE, "Extract original tender positions and metadata. Do not treat Q&A quantities as original quantities."),
                ("clarifications", QA_SHAPE, f"Extract every bidder Q&A for exact tender {ref}; ignore unrelated tenders."))))
    result = await publish(ctx)
    log.info("%d positions, %d Q&A", len(result.get("positions", [])), len(result.get("clarifications", [])))
    return result


@register
class QuantityAnalyzer(Agent):
    phase = Phase.COLLECT

    async def run(self) -> None:
        seen = -1
        while self.ctx.time_left() > 0:
            count = len(pages(self.ctx, "tender"))
            if count and count != seen:
                await refresh(self.ctx)
                seen = count
            if self.ctx.board.get("done.tender") is not None:
                await refresh(self.ctx)
                await self.ctx.board.set("tender_final", True)
                from agents import analyze_requirements
                await analyze_requirements.publish(self.ctx)
                return
            await asyncio.sleep(2)
