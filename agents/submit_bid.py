# OWNER: ksiox1337   ROLE: submitter   NEEDS: results["bid"] (optional), results["tan"]   WRITES: results["submission"]
"""Enter the bid in the tender portal's form and sign it. ALWAYS runs: a signed partial bid beats nothing.

Reads (all optional, degrades gracefully):
  results["bid"]     the answer as swarm.bidmath.answer() builds it. Each position may also carry
                     "attributes": {"bore": .., "stroke": .., "mounting": .., "certification": ..} for a
                     configurator that picks the material by its attributes.
  results["bidder"]  {"company": .., "vat": .., "email": ..}; otherwise the model reads them from the brief.
  results["tan"]     {"tan": "..", "at": <epoch s>}: set by whoever reads the mailbox, after this agent
                     announces a request in results["tan_request"] = {"at": <epoch s>}.
Writes:
  results["submission"] = {"submitted": bool, "finished": bool, "turns": int, "note": str}

Without results["bid"] it submits the smallest valid bid it can: the tender reference from the brief,
decision "bid", every position omitted, discount 0, total 0.
Locally (no run token) it stops right before requesting the TAN unless SUBMIT_LOCAL=1.
"""
import logging
import os
import re
import time

from swarm import Agent, Phase, register
from swarm.bidmath import D, plain, round_cents
from swarm.formpilot import pilot

BRIEF_FACTS = """From the task brief below, return JSON only:
{"tender_reference": "...", "bidder_company": "...", "bidder_vat_id": "...", "bidder_email": "... or null",
 "how_to_submit": "the brief's submission instructions, step by step, in at most 12 short lines"}
Copy values exactly as written in the brief. Use null for anything the brief does not state."""


log = logging.getLogger("BidSubmitter")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def de(value) -> str:
    """German number format without thousands separators, two decimals: 1234,5 -> '1234,50'."""
    return f"{round_cents(value):f}".replace(".", ",")


def de_percent(value) -> str:
    text = f"{D(value).normalize():f}"
    return text.replace(".", ",")


class TanWaiter:
    """Announce a TAN request on the board and wait for a TAN that arrived after it."""

    def __init__(self, ctx, timeout_cap: float = 240):
        self.ctx, self.cap = ctx, timeout_cap

    async def __call__(self, since: float):
        board = self.ctx.board
        board.results.pop("tan", None)
        await board.set("tan_request", {"at": since})
        timeout = max(5.0, min(self.cap, self.ctx.seconds_left() - 45))
        log.info("TAN requested: waiting up to %.0f s for results['tan'] from the mailbox reader", timeout)
        try:
            got = await board.wait_for(key="tan", timeout=timeout)
        except Exception:  # noqa: BLE001 - timed out: the model is told and decides what to do
            log.warning("no TAN arrived within %.0f s", timeout)
            return None
        tan = got.get("tan") if isinstance(got, dict) else got
        log.info("TAN received after %.0f s", time.time() - since)
        return plain(tan) if tan else None


@register
class BidSubmitter(Agent):
    phase = Phase.SUBMIT
    browser = 0

    async def facts_from_brief(self) -> dict:
        try:
            return await self.ctx.llm.ask_json(BRIEF_FACTS, self.ctx.brief, temperature=0)
        except Exception as error:  # noqa: BLE001
            self.log.warning("could not read the brief: %s", error)
            return {}

    def data_for_form(self, facts: dict) -> dict:
        bid = self.ctx.board.get("bid") or {}
        bidder = self.ctx.board.get("bidder") or {}
        site = self.ctx.site(self.ctx.task["submission"].get("siteId") or "tender-portal")
        positions = []
        for p in bid.get("positions") or []:
            entry = {"position": plain(p["position"]), "material_number": plain(p["material_number"]),
                     "quantity_pieces": int(p["quantity"]), "net_unit_price_after_discount": de(p["unit_price"]),
                     "lead_time_weeks": int(p["lead_time_weeks"])}
            for key, label in (("base_unit_price", "base_price"), ("surcharge_per_unit", "surcharge_per_piece"),
                               ("packaging_per_unit", "packaging_per_piece"), ("rebate_total", "project_bonus")):
                if p.get(key) is not None:
                    entry[label] = de(p[key])
            if p.get("attributes"):
                entry["material_attributes"] = p["attributes"]
            positions.append(entry)
        decision = bid.get("decision") or "bid"
        return {
            "login": {"url": site["url"], **(site.get("credentials") or {})},
            "tender_reference": plain(bid.get("tender_reference") or facts.get("tender_reference") or ""),
            "bidder": {"company": bidder.get("company") or facts.get("bidder_company"),
                       "vat_id": bidder.get("vat") or facts.get("bidder_vat_id"),
                       "contact_email": bidder.get("email") or facts.get("bidder_email")},
            "decision": decision,
            "positions_to_enter": positions if decision == "bid" else [],
            "every_other_position": "omit it (partial submission)" if decision == "bid" else "n/a",
            "discount_percent": de_percent(bid.get("discount_percent") or 0),
            "net_total_eur": de(bid.get("total_net") or 0),
            "number_format": "German: comma as decimal separator, no thousands separator",
        }

    async def mailbox_address(self) -> str | None:
        """The bidder's own mailbox address, which the bid form's contact e-mail must match: the webmail shows
        it once logged in. A short model-driven visit; None if it cannot be found in time."""
        mail = next((s for s in self.ctx.task["access"] if s.get("siteId") == "mail"), None)
        if not mail:
            return None
        page = await self.ctx.pool.new_page(3)
        try:
            await page.goto(mail["url"], wait_until="domcontentloaded")
            result = await pilot(
                page, self.ctx.llm,
                "Log in to this webmail and stop as soon as the page shows which mailbox address you are logged in "
                "as. Then set finished to true and put ONLY that e-mail address in thought. Do not open messages.",
                {"login": {"url": mail["url"], **(mail.get("credentials") or {})}},
                deadline=time.time() + min(120, max(10, self.ctx.seconds_left() - 300)), allow_submit=False,
                max_turns=8)
        finally:
            await page.close()
        found = EMAIL.search(result.note or "")
        return found.group(0) if found else None

    async def run(self) -> None:
        facts = await self.facts_from_brief()
        data = self.data_for_form(facts)
        if not data["bidder"].get("contact_email"):
            data["bidder"]["contact_email"] = await self.mailbox_address()
            self.log.info("contact e-mail from the webmail: %s", data["bidder"]["contact_email"])
        task = (
            "Submit the bid described in DATA for this tender on the tender portal, then sign it.\n"
            f"The brief's submission instructions:\n{facts.get('how_to_submit') or '(not available)'}\n"
            "Rules: log in if asked (DATA.login). Find the tender by its reference, register interest if the "
            "portal requires it, open the bid submission. Register the bidder with DATA.bidder. Pick the main "
            "offer for decision 'bid', the waiver for 'no_bid'; never an alternative offer. Dropdowns can come "
            "preselected with a wrong value: set each one explicitly. For a position in positions_to_enter, "
            "configure the material (pick its attribute dropdowns one after another, applying the selection when "
            "the next one stays disabled, then its catalogue/material number), and enter quantity, net unit "
            "price after discount, the breakdown fields given, and lead time. Omit every other position with the "
            "portal's omit/skip control; never add positions. Enter discount and NET total as given; displayed "
            "gross amounts are not the net total. Acknowledge only the tender documents; do not accept own "
            "terms/AGB, surveys or banners. On the final review page request the TAN once (action \"tan\"); it is "
            "fetched for you and appears in DATA.tan, so stay on the page; then enter it and submit, and "
            "confirm the binding submission. Never leave this website. Never press a back/Zurück button: going "
            "back discards every later step. If a page looks empty or half-loaded, take no action and look again."
        )
        allow = self.ctx.scored or os.environ.get("SUBMIT_LOCAL") == "1"
        page = await self.ctx.pool.new_page(self.browser or 0)
        await page.goto(data["login"]["url"], wait_until="domcontentloaded")
        deadline = time.time() + self.ctx.seconds_left() - 30
        result = await pilot(page, self.ctx.llm, task, data, deadline=deadline, allow_submit=allow,
                             get_tan=TanWaiter(self.ctx))
        await self.ctx.board.set("submission", {"submitted": result.submitted, "finished": result.finished,
                                                "turns": result.turns, "note": result.note})
        self.log.info("submission: submitted=%s finished=%s turns=%d note=%s", result.submitted,
                      result.finished, result.turns, result.note[:200])
