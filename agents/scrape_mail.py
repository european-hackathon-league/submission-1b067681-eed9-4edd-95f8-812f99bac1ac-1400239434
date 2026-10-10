# OWNER: (name)   ROLE: scraper   SITE: webmail (role "mail")
"""Reads the mails that matter for this tender, saved as page evidence ("mail.page"), in two passes:
1. the sales director's discount approvals (sets done.mail_core when finished),
2. the other release mails the brief names (condition rules, technical release, packaging, delivery, setup pools,
   project bonus, FX, price indices), if time allows (sets done.mail).
The signing TAN is NOT read here: the submitter asks for it when it signs (agents/submit_bid.py)."""
from swarm import Phase, register  # noqa: F401
from swarm.scraper import SiteScraper
from swarm.sites import site_for

SEARCH_RULES = (
    "The mailbox search combines words with AND and shows at most a limited number of hits: search for the exact "
    "tender reference plus ONE topic word at a time (e.g. '<reference> <topic>'). Search the inbox AND the archive "
    "folder (also page through results). Look-alike references (same reference with a suffix) and other tenders do "
    "not count, but drafts, previews and superseded mails look similar: read each candidate message.\n"
    "For every message whose subject or text names this tender's reference and the topic, read it with read_page and "
    "topic '<topic> mail' - read several selected messages at once with read_page(urls=[their links]) - (read_page "
    "also reads collapsed or locked parts of a message, so you do not need to unlock anything). If a reading pane "
    "shows no message text, open the message's own page (its 'open original'/message link) and read that instead. "
    "Skip signing-TAN mails.")


@register
class MailScraper(SiteScraper):
    role = "mail"
    browser = 3
    max_steps = 26
    hints = ("Webmail habits: a cookie wall after login; maintenance popups (close them, do not follow their links); "
             "an inbox and an archive folder; paged result lists; message headers (sender, date) are in a collapsed "
             "'envelope/headers' section, which read_page includes.")

    async def run(self) -> None:
        try:
            self.site_id = self.site_id or await site_for(self.ctx, self.role)
            facts = await self.wait_brief()
            topics = [t for t in (facts.get("mail_topics") or []) if t]
            first = (f"Find the discount approvals for this tender (sender: {facts.get('director_sender') or 'the sales director'}). "
                     f"Topic words to try: {facts.get('discount_topic') or 'Rabatt'}, Rabattfreigabe.\n{SEARCH_RULES}\n"
                     "Then also do the same for the condition-rule mails (Konditionsregeln) and the technical release "
                     "mails (Technikfreigabe) if they exist. Finish with done.")
            out = await self.browse_until_done(first, attempts=2)
            await self.ctx.board.set(f"{self.name}.summary", out["summary"])
            await self.ctx.board.set("done.mail_core", True)
            facts = self.ctx.board.get("brief") or facts          # the brief facts may have arrived meanwhile
            topics = [t for t in (facts.get("mail_topics") or []) if t]
            rest =[t for t in topics if t.lower() not in ("rabatt", "rabattfreigabe", "konditionsregeln", "technikfreigabe")]
            if rest and self.ctx.time_left() > 150:
                second = (f"Find the release mails for this tender on these topics, one search per topic: {', '.join(rest)}.\n"
                          f"{SEARCH_RULES}\nFinish with done.")
                out = await self.browse(second, max_steps=30)
                await self.ctx.board.set(f"{self.name}.summary2", out["summary"])
        finally:
            await self.ctx.board.set("done.mail_core", True)
            await self.ctx.board.set("done.mail", True)
