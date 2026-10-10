# OWNER: (name)   ROLE: scraper   SITE: buyer's supplier portal (role "spec")
"""Reads the technical specification: the revision list with status, and the requirement table of the revision
marked current (all pages), saved as page evidence ("spec.page"). See agents/analyze_requirements.py."""
from swarm import Phase, register  # noqa: F401
from swarm.scraper import SiteScraper


@register
class SpecScraper(SiteScraper):
    role = "spec"
    browser = 1
    max_steps = 32
    hints = ("Adapt to the observed software; these are possible patterns, not requirements. Portal habits: cookie wall after login (its accept button is not logout button); paged document "
             "register; documents behind usage form (choose purpose for binding/verbindliche documents, not preview or "
             "archive; copy displayed receipt number exactly - read_page shows numbers drawn on canvas); viewers open "
             "OLDEST revision first; status and values may sit in tooltips; requirement tables paged inside iframe "
             "viewer whose URL can be opened directly.")

    def goal(self) -> str:
        return (
            "Collect buyer's technical specification (Lastenheft) for the tender in context.\n"
            "1. Log in, accept necessary cookies, find this tender's specification in documents (register paged -> "
            "read_page with next_page_link), get read access to binding version.\n"
            "2. read_page specification page ONCE with topic 'spec revisions': text includes revision history with "
            "every status (statuses behind buttons/tooltips appear as '(tooltip: ...)'; no need to click). Decide from "
            "it which revision is current/valid (not superseded).\n"
            "3. Right away open requirement table of THAT revision (its revision link, or viewer URL with that "
            "revision), read_page with topic 'spec requirements <revision>' and next_page_link = exact next-page link "
            "text, so all requirement rows saved. Most important step; no steps on other revisions.\n"
            "4. Requirement data sheets or set definitions (pieces per set) in separate documents -> read_page them too.\n"
            "5. done, naming current revision.")

    def required(self):
        return [(("requirement", "anforderung"),
                 "Open this tender's specification (Lastenheft), find revision marked current/valid in revision "
                 "history (read_page shows statuses as tooltips), open THAT revision's requirement table, read_page "
                 "with topic 'spec requirements <revision>' and next_page_link = exact next-page link text. Then done.")]
