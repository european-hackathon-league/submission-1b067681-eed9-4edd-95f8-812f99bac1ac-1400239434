# OWNER: (name)   ROLE: scraper   SITE: tender portal (role "tender")
"""Reads the tender: overview, bill of quantities and ALL bidder Q&A, saved as page evidence ("tender.page").
Analysts turn the pages into positions, quantities, amendments and commercial keys (agents/analyze_quantities.py).

The main tab logs in and opens the tender; then the documents (main tab) and the Q&A (a second tab, sharing the
login) are read at the same time."""
import asyncio

from swarm import Phase, register  # noqa: F401
from swarm.scraper import SiteScraper
from swarm.sites import site_for

QA_JOB = ("Open this tender's bidder questions/answers (Q&A, Bieterfragen) and read_page them with topic 'bidder Q&A' "
          "and next_page_link = exact text of next-page link, so every page read in one action. Then done.")
DOCS_JOB = ("Open this tender's documents. read_page (topic naming the document) the document(s) listing positions "
            "with quantities, units, requirement references (bill of quantities / position overview). Documents locked "
            "-> register interest first. One read per document. Then done, listing what you saved.")


@register
class TenderScraper(SiteScraper):
    role = "tender"
    browser = 0
    max_steps = 28
    hints = ("Adapt to the observed software; these are possible patterns, not requirements. Portal habits: cookie wall after login; error pages that go away on retry; survey popups and decoy "
             "'open'/'accept' links leading away (close popups with their x button); documents may need expression of "
             "interest first, may open in viewers/iframes or ZIP downloads (prefer viewer); Q&A list paged, answers in "
             "collapsed rows inside iframes; dates may be in other timezone.")

    def goal(self) -> str:
        return ("Collect tender data for the reference in context. Do NOT enter or submit a bid.\n"
                "1. Log in, accept necessary cookies, open the tender (search its reference if not listed).\n"
                "2. read_page tender overview with topic 'tender overview' (reference, buyer, deadline + timezone).\n"
                "3. Documents locked -> register interest now.\n"
                "4. If the submission link's URL is already visible, share_tip it. This is optional: do not open "
                "the wizard or spend extra steps finding the link. Finish immediately after saving the overview "
                "and obtaining document access so collection can start.")

    def required(self):
        return [(("q&a", "bieterfrage", "frage", "question"), QA_JOB),
                (("leistungsverzeichnis", "positions", "quantities", "bill"),
                 "Open this tender's documents and read_page the bill of quantities / position overview with topic "
                 "'bill of quantities'. Then done.")]

    async def run(self) -> None:
        try:
            self.site_id = self.site_id or await site_for(self.ctx, self.role)
            first = await self.browse_until_done(self.goal(), attempts=1, max_steps=8)
            await self.ctx.board.set(f"{self.name}.summary", first["summary"])
            # main tab: documents; new tab: Q&A (shares the login)
            await asyncio.gather(self.browse_until_done(DOCS_JOB, attempts=2, max_steps=20),
                                 self.browse_in_tabs([QA_JOB], max_steps=20))
            await self.required_pass()
        finally:
            await self.ctx.board.set(f"done.{self.role}", True)
