# OWNER: (name)   ROLE: analyst   NEEDS: mail.page (done.mail_core / done.mail), results["brief"]
# WRITES: results["discount"], results["mail"], results["mail_messages"]
"""Turns the saved mails into records, then applies the authority rules in code: only the sender the brief authorises
for a topic (local part + our mailbox domain), only the EXACT tender reference, only records dated no later than the
scenario date, newest record per key wins, drafts/previews never count.

results["discount"] = {"percent": "5", "min_order_value": "1.00" | None, "source": subject/date} (percent "0" if none)
results["mail"] = {
  "condition_rules": {key: "base_only" | "base_and_surcharge" | "none"},
  "technical_release": {key: {"revision", "reserve_mm_per_end"}},
  "packaging_tariffs": {key: eur_per_carton}, "delivery_commitments": {key: {"material", "weeks", "max_total_pieces"}},
  "setup_pools": {key: eur_fee}, "project_bonus": {key: [{"from", "to", "percent"}]},
  "fx_fixing": {currency: eur_per_unit}, "price_indices": {index: eur_per_kg}
}  (numbers as strings)
"""
import asyncio
from datetime import datetime

from swarm import Agent, Phase, register
from swarm.extract import extract, page_block, pages
from swarm.normalize import norm_ref, to_datetime, to_decimal

EPOCH = to_datetime("1970-01-01T00:00:00Z")

TOPICS = ["discount_approval", "condition_rules", "technical_release", "packaging_tariffs", "delivery_commitments",
          "setup_pools", "project_bonus", "fx_fixing", "price_indices", "signing_tan", "other"]

SHAPE = """{"mailbox_owner": "our own mailbox address (logged-in user / recipient) if shown",
 "messages": [{"subject": "", "sender": "full sender address", "date": "UTC ISO timestamp", "folder": "",
  "reference": "tender reference exactly as named in the message (keep suffixes)",
  "topic": "one of: """ + ", ".join(TOPICS) + """",
  "is_draft": "true only if this message itself is an unsent draft/local preview",
  "valid_from_message_date": "true if the message says it applies only from its own date on",
  "discount_percent": null, "min_order_value_eur": null,
  "entries": [{"key": "key/code as written", "revision": null, "reserve_mm_per_end": null, "eur_per_carton": null,
     "material": null, "weeks": null, "max_total_pieces": null, "pool_fee_eur": null,
     "bands": [{"from_eur": 0, "to_eur": null, "percent": 0}], "currency": null, "eur_per_currency_unit": null,
     "discount_scope": "for condition rules: base_only | base_and_surcharge | none", "index": null,
     "eur_per_kg": null, "valid_from": null, "valid_to": null}]}]}"""

INSTRUCTION = ("Extract every mail MESSAGE on these pages (ignore result lists without message text). Values come from "
               "the original message text, NOT from a local preview/unsent draft section. Convert numbers to JSON "
               "numbers using the number format the message states (e.g. decimal point). For condition rules decide "
               "per key from the prose whether the order discount applies to the base price only, to base price and "
               "the weight-based material surcharge, or to nothing.")


def _local(addr) -> str:
    return str(addr or "").strip().lower().split("@")[0]


def _domain(addr) -> str:
    return str(addr or "").strip().lower().rpartition("@")[2]


def valid_messages(messages: list[dict], ref: str, scenario: datetime | None, senders: dict, domain: str) -> list[dict]:
    out = []
    for m in messages:
        if m.get("is_draft") in (True, "true") or norm_ref(m.get("reference")) != norm_ref(ref):
            continue
        want = _local(senders.get(m.get("topic"), ""))
        if want and _local(m.get("sender")) != want:
            continue
        if domain and _domain(m.get("sender")) and _domain(m.get("sender")) != domain:
            continue
        date = to_datetime(m.get("date"))
        if scenario and date and date > scenario:
            continue
        out.append(m)
    return sorted(out, key=lambda m: to_datetime(m.get("date")) or EPOCH)


def latest_entries(messages: list[dict], topic: str) -> dict:
    """{key: entry} from the newest valid message per key (messages sorted oldest first)."""
    out = {}
    for m in messages:
        if m.get("topic") == topic:
            for e in m.get("entries") or []:
                if e.get("key") or e.get("index") or e.get("currency"):
                    out[norm_ref(e.get("key") or e.get("index") or e.get("currency"))] = e | {"_date": m.get("date")}
    return out


def summarise(messages: list[dict], scenario: datetime | None) -> dict:
    s = lambda v: None if v in (None, "") else format(to_decimal(v).normalize(), "f") if to_decimal(v) is not None else None  # noqa: E731
    mail = {"condition_rules": {k: e.get("discount_scope") for k, e in latest_entries(messages, "condition_rules").items()},
            "technical_release": {k: {"revision": e.get("revision"), "reserve_mm_per_end": s(e.get("reserve_mm_per_end"))}
                                  for k, e in latest_entries(messages, "technical_release").items()},
            "packaging_tariffs": {k: s(e.get("eur_per_carton")) for k, e in latest_entries(messages, "packaging_tariffs").items()},
            "delivery_commitments": {k: {"material": e.get("material"), "weeks": s(e.get("weeks")),
                                         "max_total_pieces": s(e.get("max_total_pieces"))}
                                     for k, e in latest_entries(messages, "delivery_commitments").items()},
            "setup_pools": {k: s(e.get("pool_fee_eur")) for k, e in latest_entries(messages, "setup_pools").items()},
            "project_bonus": {k: [{"from": s(b.get("from_eur")) or "0", "to": s(b.get("to_eur")), "percent": s(b.get("percent"))}
                                  for b in e.get("bands") or []]
                              for k, e in latest_entries(messages, "project_bonus").items()},
            "fx_fixing": {}, "price_indices": {}}
    for m in messages:                                   # newest message last, so it wins
        for e in m.get("entries") or []:
            if m.get("topic") == "fx_fixing" and e.get("currency"):
                mail["fx_fixing"][norm_ref(e["currency"])] = s(e.get("eur_per_currency_unit"))
            if m.get("topic") == "price_indices" and e.get("index"):
                start, end = to_datetime(e.get("valid_from")), to_datetime(e.get("valid_to"))
                if end and end.hour == 0 and end.minute == 0:
                    end = end.replace(hour=23, minute=59, second=59)
                inside = scenario is None or ((start is None or start <= scenario) and (end is None or scenario <= end))
                if inside:
                    mail["price_indices"][norm_ref(e["index"])] = s(e.get("eur_per_kg"))
    return mail


@register
class DiscountAnalyzer(Agent):
    phase = Phase.COLLECT

    async def extract_new(self, cache: dict) -> None:
        todo = [e for e in pages(self.ctx, "mail") if (e.data.get("url"), hash(e.data.get("text", ""))) not in cache]
        replies = await asyncio.gather(*(extract(self.ctx, INSTRUCTION, SHAPE, [page_block(e)]) for e in todo),
                                       return_exceptions=True)
        for e, r in zip(todo, replies):
            cache[(e.data.get("url"), hash(e.data.get("text", "")))] = r if isinstance(r, dict) else {}

    async def publish(self, cache: dict) -> None:
        facts = self.ctx.board.get("brief") or {}
        ref, scenario = facts.get("tender_reference", ""), to_datetime(facts.get("scenario_date"))
        senders = {k: v for k, v in (facts.get("authorized_senders") or {}).items() if v}
        if facts.get("director_sender"):
            senders.setdefault("discount_approval", facts["director_sender"])
        owners = [r.get("mailbox_owner") for r in cache.values() if r.get("mailbox_owner")]
        domain = _domain(owners[0]) if owners else ""
        messages = [m for r in cache.values() for m in (r.get("messages") or [])]
        unique = {(m.get("subject"), m.get("date"), m.get("sender")): m for m in messages}
        valid = valid_messages(list(unique.values()), ref, scenario, senders, domain)
        approvals = [m for m in valid if m.get("topic") == "discount_approval" and m.get("discount_percent") is not None]
        if approvals:
            a = approvals[-1]
            discount = {"percent": format(to_decimal(a["discount_percent"]).normalize(), "f"),
                        "min_order_value": (format(to_decimal(a["min_order_value_eur"]).normalize(), "f")
                                            if a.get("min_order_value_eur") not in (None, "") else None),
                        "source": f"{a.get('subject')} {a.get('date')} {a.get('sender')}"}
        else:
            discount = {"percent": "0", "min_order_value": None, "source": "no valid approval found"}
        await self.ctx.board.set("mail_messages", list(unique.values()))
        if owners:
            await self.ctx.board.set("mailbox", owners[0])
        await self.ctx.board.set("discount", discount)
        await self.ctx.board.set("mail", summarise(valid, scenario))
        self.log.info("discount %s; %d/%d valid messages", discount, len(valid), len(unique))

    async def run(self) -> None:
        cache: dict = {}
        await self.ctx.board.wait_for(key="done.mail_core", timeout=max(1.0, self.ctx.time_left()))
        await self.extract_new(cache)
        await self.publish(cache)
        await self.ctx.board.wait_for(key="done.mail", timeout=max(1.0, self.ctx.time_left()))
        await self.extract_new(cache)
        await self.publish(cache)
        await self.ctx.board.set("mail_final", True)
