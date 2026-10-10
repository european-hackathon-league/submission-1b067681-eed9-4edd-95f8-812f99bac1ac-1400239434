# OWNER: (name)   ROLE: scraper   SITE: erp
from swarm import Phase, register  # noqa: F401
from swarm.scraper import SiteScraper


@register
class ErpScraper(SiteScraper):
    site_id = "erp"
    browser = 2
    hints = "SAP-style transaction codes, F4 search popups, status codes; the session times out quickly: log in again."

    def goal(self) -> str:
        return ("Find the ERP materials that could match the tender's cylinders (bore, stroke, pressure, mounting, "
                "certification, status active/inactive), their stock and lead times, price conditions (validity dates, "
                "quantity scales, currency, Konditionsgruppe, surcharges, setup fees), packaging and metal-index rates. "
                "Kinds: 'erp.material', 'erp.stock', 'erp.price_condition', 'erp.other'. Search by the attributes the "
                "tender requires; page through results.")
