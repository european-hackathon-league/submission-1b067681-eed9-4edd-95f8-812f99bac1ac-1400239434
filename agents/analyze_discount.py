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
from email.utils import parseaddr
import asyncio
import logging
from datetime import datetime

from swarm import Agent, Phase, register
from swarm.extract import extract, page_block, pages, settle, start_once
from swarm.normalize import validity_end, norm_ref, to_datetime, to_decimal

EPOCH = to_datetime("1970-01-01T00:00:00Z")
log = logging.getLogger("DiscountAnalyzer")

TOPICS = ["discount_approval", "condition_rules", "technical_release", "packaging_tariffs", "delivery_commitments",
          "setup_pools", "project_bonus", "fx_fixing", "price_indices", "signing_tan", "other"]

SHAPE = """{"mailbox_owner": "our mailbox address (logged-in user / recipient) if shown",
 "messages": [{"subject": "", "sender": "full sender address", "date": "UTC ISO timestamp", "folder": "",
  "reference": "tender reference exactly as in message (keep suffixes)",
  "topic": "one of: """ + ", ".join(TOPICS) + """",
  "is_draft": "true only if this message itself is unsent draft/local preview",
  "valid_from_message_date": "true if message says it applies only from its own date on",
  "discount_percent": null, "min_order_value_eur": null,
  "entries": [{"key": "key/code as written", "revision": null, "reserve_mm_per_end": null, "eur_per_carton": null,
     "material": null, "weeks": null, "max_total_pieces": null, "pool_fee_eur": null,
     "bands": [{"from_eur": 0, "to_eur": null, "percent": 0}], "currency": null, "eur_per_currency_unit": null,
     "discount_scope": "for condition rules: base_only | base_and_surcharge | none", "index": null,
     "eur_per_kg": null, "valid_from": null, "valid_to": null}]}]}"""

INSTRUCTION = ("Extract every mail MESSAGE on these pages (ignore result lists without message text). Values from "
               "original message text, NOT from local preview/unsent draft section. Numbers -> JSON numbers using the "
               "number format the message states (e.g. decimal point). Condition rules: decide per key from prose "
               "whether order discount applies to base price only, to base price + weight-based material surcharge, "
               "or to nothing.")


def _local(addr) -> str:
    return (parseaddr(str(addr or ""))[1] or str(addr or "")).strip().lower().split("@")[0]


def _domain(addr) -> str:
    value = parseaddr(str(addr or ""))[1] or str(addr or "")
    return value.strip().lower().rpartition("@")[2] if "@" in value else ""


def valid_messages(messages: list[dict], ref: str, scenario: datetime | None, senders: dict, domain: str) -> list[dict]:
    out = []
    for m in messages:
        if m.get("is_draft") in (True, "true") or norm_ref(m.get("reference")) != norm_ref(ref):
            continue
        want = _local(senders.get(m.get("topic"), ""))
        if not want or _local(m.get("sender")) != want:
            continue
        if not domain or _domain(m.get("sender")) != domain:
            continue
        date = to_datetime(m.get("date"))
        if not ref or scenario is None or date is None or date > scenario:
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
            "fx_fixing": {}, "fx_by_key": {}, "price_indices": {}}
    fx_entries = latest_entries(messages, "fx_fixing")
    currencies = {}
    for key, e in fx_entries.items():
        currency = norm_ref(e.get("currency"))
        if currency:
            rate = s(e.get("eur_per_currency_unit"))
            mail["fx_by_key"].setdefault(key, {})[currency] = rate
            currencies.setdefault(currency, []).append(rate)
    for currency, rates in currencies.items():
        if len(set(rates)) == 1:
            mail["fx_fixing"][currency] = rates[0]
    index_messages = [m for m in messages if m.get("topic") == "price_indices"]
    # Pick the governing message first; never resurrect an older rate outside its replacement's windows.
    if index_messages:
        for e in index_messages[-1].get("entries") or []:
            start, end = to_datetime(e.get("valid_from")), validity_end(e.get("valid_to"))
            inside = scenario is not None and (start is None or start <= scenario) and (end is None or scenario <= end)
            if inside and e.get("index"):
                mail["price_indices"][norm_ref(e["index"])] = s(e.get("eur_per_kg"))
    return mail


async def extract_new(ctx, wait: bool = True) -> dict:
    """Extract the mail pages not extracted yet (a task per page, cached in ctx.shared; a new page never waits for a
    slow sibling); returns the cache. `wait=False` only starts the new extractions."""
    cache = ctx.shared.setdefault("mail.extracted", {})

    async def one(e):
        key = (e.data.get("url"), hash(e.data.get("text", "")))
        try:
            result = await extract(ctx, INSTRUCTION, SHAPE, [page_block(e)], list_key="messages")
            if result.get("_incomplete"):
                return
            cache[key] = result
            await publish(ctx)  # each completed message survives cancellation of a slower sibling
        except Exception as error:
            ctx.board.error("DiscountAnalyzer", f"mail extraction: {type(error).__name__}")
    tasks = [start_once(ctx, "mail", key, lambda e=e: one(e)) for e in pages(ctx, "mail")
             if (key := (e.data.get("url"), hash(e.data.get("text", "")))) not in cache]
    if wait:
        await settle(tasks)
    return cache


async def refresh(ctx, wait: bool = True) -> list[dict]:
    """Extract new mail pages and publish discount, mail rules and messages again; returns the valid messages."""
    await extract_new(ctx, wait)
    return await publish(ctx)


async def publish(ctx) -> list[dict]:
    cache = ctx.shared.setdefault("mail.extracted", {})
    facts = ctx.board.get("brief") or {}
    ref, scenario = facts.get("tender_reference", ""), to_datetime(facts.get("scenario_date"))
    senders = {k: v for k, v in (facts.get("authorized_senders") or {}).items() if v}
    if facts.get("director_sender"):
        senders.setdefault("discount_approval", facts["director_sender"])
    owners = [r.get("mailbox_owner") for r in cache.values() if r.get("mailbox_owner")]
    domain = _domain(owners[0]) if owners else _domain(ctx.board.get("mailbox"))
    messages = [m for r in cache.values() for m in (r.get("messages") or [])]
    unique = {(m.get("subject"), m.get("date"), m.get("sender")): m for m in messages}
    valid = valid_messages(list(unique.values()), ref, scenario, senders, domain)
    approvals = [m for m in valid if m.get("topic") == "discount_approval"]
    invalid_approval = False
    if approvals:
        latest = approvals[-1]
        percent = to_decimal(latest.get("discount_percent"))
        threshold = to_decimal(latest.get("min_order_value_eur"))
        invalid_approval = (percent is None or not 0 <= percent <= 100
                            or (latest.get("min_order_value_eur") not in (None, "") and (threshold is None or threshold < 0)))
        if invalid_approval:
            approvals = []  # an unreadable replacement must not revive an older approval
            ctx.board.error("DiscountAnalyzer", "Latest discount approval has invalid numeric terms; recollect it")
    if approvals:
        a = approvals[-1]
        discount = {"percent": format(to_decimal(a["discount_percent"]).normalize(), "f"),
                    "min_order_value": (format(to_decimal(a["min_order_value_eur"]).normalize(), "f")
                                        if to_decimal(a.get("min_order_value_eur")) is not None else None),
                    "source": f"{a.get('subject')} {a.get('date')} {a.get('sender')}"}
    else:
        discount = {"percent": "0", "min_order_value": None, "source": "latest approval invalid" if invalid_approval else "no valid approval found"}
    await ctx.board.set("mail_messages", list(unique.values()))
    if owners:
        await ctx.board.set("mailbox", owners[0])
    await ctx.board.set("discount", discount)
    await ctx.board.set("mail", summarise(valid, scenario))
    log.info("discount %s; %d/%d valid messages", discount, len(valid), len(unique))
    return valid


def covered_topics(valid: list[dict]) -> set[str]:
    return {m.get("topic") for m in valid if m.get("topic")}


@register
class DiscountAnalyzer(Agent):
    phase = Phase.COLLECT

    async def run(self) -> None:
        await self.ctx.board.wait_for(key="brief", timeout=max(1, self.ctx.time_left()))
        await publish(self.ctx)
        seen = -1
        while self.ctx.time_left() > 0:
            count = len(pages(self.ctx, "mail"))
            if count != seen:
                await refresh(self.ctx, wait=False)       # each page publishes when extracted
                seen = count
            if self.ctx.board.get("done.mail") is not None:
                await refresh(self.ctx)
                await self.ctx.board.set("mail_final", True)
                return
            await asyncio.sleep(2)
