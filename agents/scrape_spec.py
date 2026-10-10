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
    hints = ("Portal habits: a cookie wall after login (its accept button is not the logout button); a document "
             "register that is paged; documents behind a usage form (choose the purpose for binding/verbindliche "
             "documents, not preview or archive, and copy the displayed receipt number exactly - read_page shows "
             "numbers drawn on canvas); viewers open the OLDEST revision first; status and values can sit in "
             "tooltips; requirement tables are paged inside an iframe viewer whose URL you can open directly.")

    def goal(self) -> str:
        return (
            "Collect the buyer's technical specification (Lastenheft) for the tender in the context.\n"
            "1. Log in, accept the necessary cookies, find the specification for this tender in the documents "
            "(read_page the register with next_page_link if it is paged) and get read access to the binding version.\n"
            "2. read_page the specification page ONCE with topic 'spec revisions': its text includes the revision "
            "history with every status (statuses hidden behind buttons/tooltips appear as '(tooltip: ...)'; no need "
            "to click them). Decide from that text which revision is marked current/valid (not superseded).\n"
            "3. Right away open the requirement table of THAT revision (its revision link, or the viewer URL with that "
            "revision) and read_page it with topic 'spec requirements <revision>' and next_page_link set to the exact "
            "next-page link text, so all requirement rows are saved. This step matters most; do not spend steps on "
            "other revisions.\n"
            "4. If requirement data sheets or set definitions (pieces per set) are separate documents, read_page them too.\n"
            "5. Finish with done, naming the current revision.")

    def required(self):
        return [(("requirement", "anforderung"),
                 "Open the specification (Lastenheft) for this tender, find the revision marked current/valid in the "
                 "revision history (read_page shows statuses as tooltips), open THAT revision's requirement table "
                 "and read_page it with topic 'spec requirements <revision>' and next_page_link set to the exact "
                 "next-page link text. Then finish with done.")]
