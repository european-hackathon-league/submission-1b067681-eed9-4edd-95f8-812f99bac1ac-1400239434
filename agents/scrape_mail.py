# OWNER: (name)   ROLE: scraper   SITE: webmail (role "mail")
"""Reads the mails that matter for this tender, saved as page evidence ("mail.page").

Every message is read once:
1. The main tab logs in while the brief is being read.
2. Index pass (main tab): one search for the exact tender reference alone, in inbox and archive; the model picks the
   messages about this tender from the result lists and reads them all with read_page(urls=[...]). Then
   results["done.mail_core"] (the discount analyst starts).
3. The analyst's extraction says which topics the brief names (authorised senders: discount approval, condition
   rules, technical release, packaging, ...) have no valid message yet. Only those get a topic search, in parallel
   tabs (each logs in: this webmail keeps its login per tab). Messages already saved by another agent are not opened
   again (read_page returns the saved text). Then results["done.mail"].
The signing TAN is NOT read here: the submitter asks for it when it signs (agents/submit_bid.py)."""
import os
import asyncio

from agents.analyze_discount import covered_topics, refresh
from swarm import Phase, register  # noqa: F401
from swarm.scraper import SiteScraper
from swarm.sites import site_for

TOPIC_TABS = int(os.environ.get("SWARM_MAIL_TABS", "2"))   # parallel mail topic tabs; was 3 (sandbox CPU)
MAIL_STOP_EXTRA_S = 150   # mail browsing ends this long before collecting does (mail extraction is the slowest)

SEARCH_RULES = (
    "Mailbox search combines words with AND and shows limited hits. Look-alike references (same reference with "
    "suffix) and other tenders do not count; drafts, previews, superseded mails look similar: read each candidate "
    "message. Read selected messages with ONE read_page(urls=[their links], topic '<topic> mail') (read_page also "
    "reads collapsed or locked parts, no need to unlock). Reading pane shows no message text -> use message's own "
    "page link. Skip signing-TAN mails.")

INDEX_JOB = (
    "Search the exact tender reference ALONE (no other word), first inbox then archive. read_page each result "
    "page with topic 'mail index'. On EACH result page, immediately read the matching message links before "
    "moving to another index page or folder; do not spend the entire budget collecting indexes. Prioritize "
    "discount approval and technical release messages from the authorized senders, then other commercial terms. "
    "Use read_page(urls=[observed message links], topic='mail messages'), up to 12 URLs. Return to the observed "
    "index URL to continue pagination. Read all messages for the exact reference, excluding look-alike suffixes, "
    "other tenders and signing TANs. Then done, listing subjects read.\n" + SEARCH_RULES)


@register
class MailScraper(SiteScraper):
    role = "mail"
    browser = 3
    max_steps = 24
    hints = ("Adapt to the observed software; these are possible patterns, not requirements. Webmail habits: cookie wall after login; maintenance popups (close them, never follow their links); "
             "search may sit behind slider/robot gate: first try ONE fill_form (slider = its maximum, robot checkbox = "
             "true); still locked -> focus the slider and press End, or click its right end; never loop on evaluate; "
             "inbox and archive folder; paged result lists; message headers "
             "(sender, date) in collapsed 'envelope/headers' section, which read_page includes.")

    async def browse(self, job: str, **kwargs):
        kwargs.setdefault("stop_extra", MAIL_STOP_EXTRA_S)
        return await super().browse(job, **kwargs)

    def stop_extra(self, kwargs: dict) -> float:
        return kwargs.get("stop_extra", MAIL_STOP_EXTRA_S)

    async def login(self) -> None:
        try:
            await self.browse("Log in, accept cookie wall, close popups, open inbox. Do NOT search yet. Then done.",
                              max_steps=8)
        except Exception as error:
            self.log.warning("login browse failed: %s", error)

    def topic_jobs(self, facts: dict, covered: set[str]) -> list[str]:
        """One search job per brief topic that has no valid message yet (discount approval first)."""
        senders = dict(facts.get("authorized_senders") or {})
        senders.setdefault("discount_approval", facts.get("director_sender") or "")
        words = dict(facts.get("topic_words") or {})
        words.setdefault("discount_approval", facts.get("discount_topic") or "Rabatt")
        missing = [t for t in dict.fromkeys([*senders, *words]) if t not in covered and t != "signing_tan"]
        missing.sort(key=lambda t: t != "discount_approval")
        jobs = []
        for topic in missing:
            word = words.get(topic) or topic.replace("_", " ")
            sender = senders.get(topic)
            jobs.append(f"Find mails on topic '{word}' ({topic.replace('_', ' ')}) for this tender"
                        f"{f' (sender {sender})' if sender else ''}: search '<reference> {word}' in inbox and "
                        f"archive; read every matching message.\n{SEARCH_RULES}\nThen done.")
        return jobs

    async def run(self) -> None:
        try:
            self.site_id = self.site_id or await site_for(self.ctx, self.role)
            facts, _ = await asyncio.gather(self.wait_brief(), self.login())
            facts = self.ctx.board.get("brief") or facts
            out = await self.browse_until_done(INDEX_JOB, attempts=2)
            await self.ctx.board.set(f"{self.name}.summary", out["summary"])
            await self.ctx.board.set("done.mail_core", True)
            try:        # the index pass's messages decide which topic searches are still needed (no duplicate reads)
                valid = await asyncio.wait_for(refresh(self.ctx), timeout=45)
            except TimeoutError:
                from agents.analyze_discount import publish
                valid = await publish(self.ctx)
            jobs = self.topic_jobs(facts, covered_topics(valid))
            if jobs and self.ctx.time_left() > 120:
                self.log.info("topics without a valid message: %d searches", len(jobs))
                groups = [jobs[i::TOPIC_TABS] for i in range(min(TOPIC_TABS, len(jobs)))]
                main, rest = groups[0], groups[1:]

                async def in_main() -> None:
                    for job in main:
                        await self.browse_until_done(job, attempts=1, max_steps=16)
                await asyncio.gather(in_main(), self.browse_in_tabs(["\n\n".join(g) for g in rest], attempts=1,
                                                                    max_steps=22))
        finally:
            await self.ctx.board.set("done.mail_core", True)
            await self.ctx.board.set("done.mail", True)
