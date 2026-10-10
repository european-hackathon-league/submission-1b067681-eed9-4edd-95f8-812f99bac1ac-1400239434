# OWNER: (name)   ROLE: scraper   SITE: mail
from swarm import Phase, register  # noqa: F401
from swarm.scraper import SiteScraper


@register
class MailScraper(SiteScraper):
    site_id = "mail"
    browser = 3
    hints = "Check the archive folder and later pages too; look-alike and superseded approvals exist."

    def goal(self) -> str:
        return ("Find mails relating to THIS tender: discount approvals from the sales director (sender, exact tender "
                "reference, date, conditions/thresholds, superseded?) and the signing TAN mail (do NOT use the TAN yet). "
                "Kinds: 'mail.discount_approval', 'mail.tan', 'mail.other'. Record sender address and date exactly.")
