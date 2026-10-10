# OWNER: (name)   ROLE: scraper   SITE: customer-portal
from swarm import Phase, register  # noqa: F401
from swarm.scraper import SiteScraper


@register
class SpecScraper(SiteScraper):
    site_id = "customer-portal"
    browser = 1
    hints = "The portal may open the OLDEST revision first; viewers can be nested and tables paged."

    def goal(self) -> str:
        return ("Find the buyer's technical specification for this tender and ALL its revisions. Record which revision is "
                "marked current, each revision's date/status, and every requirement and scoped amendment (which "
                "positions it applies to). Kinds: 'spec.revision', 'spec.requirement', 'spec.amendment'.")
