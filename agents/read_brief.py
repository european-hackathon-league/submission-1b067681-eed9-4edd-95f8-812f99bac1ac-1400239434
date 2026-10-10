# OWNER: (shared)   ROLE: analyst (no browser)   NEEDS: task brief   WRITES: results["brief"], results["sites"]
"""Reads the brief once at the start so every agent knows the tender reference, scenario date and bidder data.

results["brief"] = {
  "tender_reference": str, "tender_title": str, "buyer": str, "scenario_date": ISO str, "deadline": str,
  "bidder_company": str, "bidder_vat_id": str, "director_sender": str (sender local part),
  "currency": str, "authorized_senders": {topic: local part}, "mail_topics": [word], "topic_words": {topic: word}
}
The reference is only accepted if it literally occurs in the brief (no model invention).
"""
import asyncio

from swarm import Agent, Phase, register
from swarm.normalize import norm_ref, to_datetime
from swarm.sites import resolve_roles

SHAPE = """{"tender_reference": "", "tender_title": "", "buyer": "", "scenario_date": "ISO 8601 with timezone",
"deadline": "", "bidder_company": "", "bidder_vat_id": "", "currency": "",
"director_sender": "local part of the sender who approves discounts, e.g. 'name@'",
"discount_topic": "German word for the discount approval topic",
"authorized_senders": {"<one of: discount_approval, condition_rules, technical_release, packaging_tariffs,
  delivery_commitments, setup_pools, project_bonus, fx_fixing, price_indices>": "<sender local part the brief names for it>"},
"mail_topics": ["every mail subject/topic word the brief says carries release data (e.g. condition rules, price indices)"],
"topic_words": {"<topic key as in authorized_senders>": "the word the brief uses for that topic (mail search term)"}}"""


@register
class BriefReader(Agent):
    phase = Phase.COLLECT

    async def run(self) -> None:
        facts = await self.ctx.llm.ask_json(
            "Extract key facts from task brief. Copy values exactly. Reply JSON only.",
            f"Shape:\n{SHAPE}\n\nBRIEF:\n{self.ctx.brief}")
        facts = {k: (v.strip() if isinstance(v, str) else v) for k, v in (facts or {}).items()}
        if norm_ref(facts.get("tender_reference")) not in norm_ref(self.ctx.brief):
            self.log.warning("reference %r not in brief; dropped", facts.get("tender_reference"))
            facts["tender_reference"] = ""
        if facts.get("scenario_date") and not to_datetime(facts["scenario_date"]):
            facts["scenario_date"] = ""
        await self.ctx.board.set("brief", facts)
        self.log.info("brief facts: %s", facts)
        await resolve_roles(self.ctx)
