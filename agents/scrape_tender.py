# OWNER: (name)   ROLE: scraper   SITE: tender-portal
from swarm import Evidence, Phase, register  # noqa: F401
from swarm.scraper import SiteScraper


@register
class TenderScraper(SiteScraper):
    site_id = "tender-portal"
    browser = 0
    max_steps = 50
    hints = "Cookie wall and flaky error pages are common: retry. Documents may sit behind an expression of interest, in iframes or ZIPs."

    def goal(self) -> str:
        return ("Read the public tender: its reference, deadline (note the timezone), the bill of quantities (every position "
                "with its specification and quantity), and ALL bidder questions & answers/clarifications with their dates, "
                "who answered, and whether withdrawn. Record each as evidence: kind 'tender.meta', 'tender.position', "
                "'tender.clarification'. Do not enter or submit any bid.")
