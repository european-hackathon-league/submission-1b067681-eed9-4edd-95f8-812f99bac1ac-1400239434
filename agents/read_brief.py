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
import re

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


def mask_secrets(brief: str, task: dict) -> str:
    """The brief with every login value replaced by a placeholder: logins never reach a model prompt.
    (From test_brief_reader's swarm/brief.py.)"""
    for site in task.get("access") or []:
        for key, value in (site.get("credentials") or {}).items():
            if isinstance(value, str) and len(value) >= 4:
                brief = brief.replace(value, f"<{key}>")
    return brief


def checked(facts: dict, brief: str) -> dict:
    """Keep only values the brief actually contains (guards against invented values; from test_brief_reader):
    bidder data verbatim, sender local parts as written in the brief ('name@' or '/name')."""
    flat = " ".join(str(brief or "").split())
    facts = {k: (" ".join(v.split()) if isinstance(v, str) else v) for k, v in facts.items()}
    for name in ("bidder_company", "bidder_vat_id"):
        if facts.get(name) and facts[name] not in flat:
            facts[name] = ""
    senders = facts.get("authorized_senders") if isinstance(facts.get("authorized_senders"), dict) else {}
    facts["authorized_senders"] = {topic: local for topic, local in senders.items()
                                   if isinstance(local, str) and local.strip("@ ")
                                   and re.search(rf"(?<![\w.]){re.escape(local.strip('@ '))}(@|\b)", flat)}
    director = str(facts.get("director_sender") or "").strip("@ ")
    if director and not re.search(rf"(?<![\w.]){re.escape(director)}@", flat):
        facts["director_sender"] = ""
    return facts


@register
class BriefReader(Agent):
    phase = Phase.COLLECT

    async def run(self) -> None:
        facts = await self.ctx.llm.ask_json(
            "Extract key facts from task brief. Copy values exactly. Reply JSON only.",
            f"Shape:\n{SHAPE}\n\nBRIEF:\n{mask_secrets(self.ctx.brief, self.ctx.task)}")
        facts = checked(facts or {}, self.ctx.brief)
        if norm_ref(facts.get("tender_reference")) not in norm_ref(self.ctx.brief):
            self.log.warning("reference %r not in brief; dropped", facts.get("tender_reference"))
            facts["tender_reference"] = ""
        if facts.get("scenario_date") and not to_datetime(facts["scenario_date"]):
            facts["scenario_date"] = ""
        await self.ctx.board.set("brief", facts)
        self.log.info("brief facts: %s", facts)
        await resolve_roles(self.ctx)
