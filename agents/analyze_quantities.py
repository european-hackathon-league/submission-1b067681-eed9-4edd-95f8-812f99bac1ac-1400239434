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


async def refresh(ctx) -> dict:
    """Extract the saved tender pages and publish results["tender"] and results["quantities"]; returns the tender."""
    blocks = [page_block(e) for e in pages(ctx, "tender")]
    if not blocks:
        raise RuntimeError("no tender pages were saved")
    ref = (ctx.board.get("brief") or {}).get("tender_reference", "")
    pos_out, qa_out = await asyncio.gather(
        extract(ctx, "List the tender's positions from the bill of quantities / position overview "
                     "(original tender quantities, not Q&A changes).", POSITIONS_SHAPE, blocks),
        extract(ctx, f"List EVERY bidder Q&A entry for tender {ref} (all pages). Classify each answer.",
                QA_SHAPE, blocks, list_key="clarifications"))
    positions = [p for p in (pos_out or {}).get("positions", []) if p.get("position")]
    seen, clarifications = set(), []
    for c in qa_out.get("clarifications", []):
        key = norm_ref(c.get("number")) or norm_ref(c.get("question"))
        if key and key not in seen:
            seen.add(key)
            clarifications.append(c)
    tender = {"meta": (pos_out or {}).get("meta", {}), "positions": positions, "clarifications": clarifications}
    await ctx.board.set("tender", tender)
    quantities = final_quantities(positions, clarifications)
    await ctx.board.set("quantities", quantities)
    log.info("%d positions, %d Q&A, quantities %s", len(positions), len(clarifications), quantities)
    return tender


@register
class QuantityAnalyzer(Agent):
    phase = Phase.COLLECT        # runs as soon as the tender scraper is done, while the other sites are still read

    async def run(self) -> None:
        await self.ctx.board.wait_for(key="done.tender", timeout=max(1.0, self.ctx.time_left()))
        await refresh(self.ctx)
