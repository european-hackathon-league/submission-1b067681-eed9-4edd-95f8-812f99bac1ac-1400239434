# OWNER: (name)   ROLE: scraper   SITE: tender portal (role "tender")
"""Reads the tender: overview, bill of quantities and ALL bidder Q&A, saved as page evidence ("tender.page").
Analysts turn the pages into positions, quantities, amendments and commercial keys (agents/analyze_quantities.py)."""
from swarm import Phase, register  # noqa: F401
from swarm.scraper import SiteScraper


@register
class TenderScraper(SiteScraper):
    role = "tender"
    browser = 0
    max_steps = 28
    hints = ("Portal habits: a cookie wall after login; error pages that go away on retry; survey popups and decoy "
             "'open'/'accept' links that lead away (close popups with their x button instead); documents may need an "
             "expression of interest first and may open in viewers/iframes or ZIP downloads (prefer the viewer); the "
             "Q&A list is paged and its answers sit in collapsed rows inside iframes; dates can be in another timezone.")

    def goal(self) -> str:
        return (
            "Collect the tender data for the reference in the context. Do NOT enter or submit a bid.\n"
            "1. Log in, accept the necessary cookies, open the tender (search for its reference if it is not listed).\n"
            "2. read_page the tender overview with topic 'tender overview' (reference, buyer, deadline and timezone).\n"
            "3. Open the bidder questions/answers (Q&A) and read_page them with topic 'bidder Q&A' and next_page_link "
            "set to the exact text of the next-page link, so every page is read in one action.\n"
            "4. Open the tender documents. read_page (with a topic naming the document) the document(s) listing the "
            "positions with quantities, units and requirement references (bill of quantities / position overview). "
            "If documents are locked, register interest first. Each document needs only one read.\n"
            "5. When all of this is saved, finish with done and list what you saved.")

    def required(self):
        return [(("q&a", "bieterfrage", "frage", "question"),
                 "Open this tender's bidder questions/answers (Q&A) and read_page them with topic 'bidder Q&A' and "
                 "next_page_link set to the exact next-page link text. Then finish with done."),
                (("leistungsverzeichnis", "positions", "quantities", "bill"),
                 "Open this tender's documents and read_page the bill of quantities / position overview with topic "
                 "'bill of quantities'. Then finish with done.")]
