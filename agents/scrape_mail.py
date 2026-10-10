# OWNER: (name)   ROLE: scraper   SITE: webmail (role "mail")
"""Reads the mails that matter for this tender, saved as page evidence ("mail.page").

Every message is read once:
1. The main tab logs in while the brief is being read.
2. Topic searches start at once, in parallel lanes (main tab + SWARM_MAIL_TABS tabs; each tab logs in: this webmail
   keeps its login per tab): one search per topic the brief names ('<reference> <topic word>'), discount approval
   first. A search for the exact reference alone lists every message and needs many result pages, so it only runs
   last, as a catch-all for what the topic searches missed. Before each job, topics that already have a valid message
   are skipped; delivery commitments only count as covered when every delivery key of the Q&A has its mail, and their
   job names the delivery keys and material numbers known by then (a webmail may find them only by article number).
   results["done.mail_core"] is set after the discount search (the discount analyst starts).
3. Messages already saved by another agent are not opened again (read_page returns the saved text). Then
   results["done.mail"].
The signing TAN is NOT read here: the submitter asks for it when it signs (agents/submit_bid.py)."""
import os
import asyncio

from agents.analyze_discount import covered_topics, publish
from agents.compute_bid import position_keys
from swarm.context import SUBMIT_RESERVE_S
from swarm import Phase, register  # noqa: F401
from swarm.browser_agent import Tab
from swarm.normalize import norm_ref
from swarm.scraper import SiteScraper
from swarm.sites import site_for

TOPIC_TABS = int(os.environ.get("SWARM_MAIL_TABS", "2"))   # parallel mail topic tabs; was 3 (sandbox CPU)

SEARCH_RULES = (
    "Mailbox search combines words with AND and shows limited hits. Look-alike references (same reference with "
    "suffix) and other tenders do not count; drafts, previews, superseded mails look similar: read each candidate "
    "message. Read selected messages with ONE read_page(urls=[their links], topic '<topic> mail') (read_page also "
    "reads collapsed or locked parts, no need to unlock). Reading pane shows no message text -> use message's own "
    "page link. Skip signing-TAN mails.")

INDEX_JOB = (
    "Catch-all: search the exact tender reference ALONE (no other word), first inbox then archive. read_page each "
    "result page with topic 'mail index'. On EACH result page, immediately read the matching message links not read "
    "yet before moving to another index page or folder. Still missing: {missing}. Use read_page(urls=[observed message "
    "links], topic='mail messages'), up to 12 URLs. Return to the observed index URL to continue pagination. Skip "
    "look-alike suffixes, other tenders and signing TANs. Then done, listing subjects read.\n" + SEARCH_RULES)

DELIVERY = "delivery_commitments"


def delivery_ids(board) -> tuple[list[str], list[str]]:
    """Delivery keys the latest Q&A assigns to the positions, and the material numbers matched so far."""
    tender = board.get("tender") or {}
    clarifications = tender.get("clarifications") or []
    keys = [position_keys(clarifications, str(p.get("position")).strip())["keys"].get("delivery_key")
            for p in tender.get("positions") or []]
    materials = [m.get("material") for m in (board.get("materials") or {}).values() if isinstance(m, dict)]
    return list(dict.fromkeys(k for k in keys if k)), list(dict.fromkeys(m for m in materials if m))


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
        kwargs.setdefault("stop_reserve", SUBMIT_RESERVE_S + 150)
        return await super().browse(job, **kwargs)

    async def login(self) -> None:
        try:
            await self.browse("Log in, accept cookie wall, close popups, open inbox. Do NOT search yet. Then done.",
                              max_steps=8)
        except Exception as error:
            self.log.warning("login browse failed: %s", error)

    def topics(self, facts: dict) -> tuple[list[str], dict, dict]:
        """The topics the brief names (discount approval first), their search words and authorised senders."""
        senders = dict(facts.get("authorized_senders") or {})
        senders.setdefault("discount_approval", facts.get("director_sender") or "")
        words = dict(facts.get("topic_words") or {})
        words.setdefault("discount_approval", facts.get("discount_topic") or "Rabatt")
        topics = [t for t in dict.fromkeys([*senders, *words]) if t != "signing_tan"]
        topics.sort(key=lambda t: (t != "discount_approval", t == DELIVERY))
        return topics, senders, words

    def topic_job(self, topic: str, words: dict, senders: dict, keys=(), materials=()) -> str:
        word = words.get(topic) or topic.replace("_", " ")
        sender = senders.get(topic)
        job = (f"Find mails on topic '{word}' ({topic.replace('_', ' ')}) for this tender"
               f"{f' (sender {sender})' if sender else ''}: search '<reference> {word}' in inbox and archive; read "
               f"every matching message.")
        if topic == DELIVERY and (keys or materials):
            job += (f" Commitments are per delivery key / article: keys {list(keys)}, material numbers "
                    f"{list(materials)}. If the search hint or the results ask for another identifier, or a key has "
                    f"no hit, search '<reference> <material number>' (or the key) for each one still missing.")
        return f"{job}\n{SEARCH_RULES}\nThen done."

    def covered(self, valid: list[dict]) -> set[str]:
        """Topics with a valid message; delivery commitments only when every Q&A delivery key has its mail."""
        covered = covered_topics(valid)
        keys, _ = delivery_ids(self.ctx.board)
        found = {norm_ref(k) for k in ((self.ctx.board.get("mail") or {}).get(DELIVERY) or {})}
        if keys and any(norm_ref(k) not in found for k in keys):
            covered.discard(DELIVERY)
        return covered

    def topic_jobs(self, facts: dict, covered: set[str]) -> list[str]:
        """One search job per brief topic that has no valid message yet (discount approval first)."""
        topics, senders, words = self.topics(facts)
        return [self.topic_job(t, words, senders) for t in topics if t not in covered]

    async def lane(self, topics: list[str], facts: dict, tab=None, first=None) -> None:
        """Search `topics` one after another in one tab, skipping those covered by then."""
        _, senders, words = self.topics(facts)
        for topic in topics:
            if self.ctx.time_left() <= 60:
                return
            if topic not in self.covered(await publish(self.ctx)):
                keys, materials = delivery_ids(self.ctx.board) if topic == DELIVERY else ((), ())
                await self.browse_until_done(self.topic_job(topic, words, senders, keys, materials), attempts=1,
                                             max_steps=12, tab=tab)
            if first and topic == topics[0]:
                await first()

    async def run(self) -> None:
        try:
            self.site_id = self.site_id or await site_for(self.ctx, self.role)
            facts, _ = await asyncio.gather(self.wait_brief(), self.login())
            facts = self.ctx.board.get("brief") or facts
            topics, _, _ = self.topics(facts)
            lanes = [topics[i::TOPIC_TABS + 1] for i in range(min(TOPIC_TABS + 1, len(topics)))]
            self.log.info("mail topic lanes: %s", lanes)

            async def core_done() -> None:
                await self.ctx.board.set("done.mail_core", True)

            async def in_tab(group: list[str]) -> None:
                tab = Tab(self.browser)
                try:
                    await self.lane(group, facts, tab=tab)
                except Exception as error:
                    self.ctx.board.error(self.name, f"mail tab lane failed: {error}")
                finally:
                    await tab.close()

            await asyncio.gather(self.lane(lanes[0] if lanes else [], facts, first=core_done),
                                 *(in_tab(g) for g in lanes[1:]))
            await self.ctx.board.set("done.mail_core", True)
            missing = [t for t in topics if t not in self.covered(await publish(self.ctx))]
            if missing and self.ctx.time_left() > 120:
                self.log.info("topics without a valid message after the searches: %s", missing)
                out = await self.browse_until_done(INDEX_JOB.format(missing=missing), attempts=1, max_steps=16)
                await self.ctx.board.set(f"{self.name}.summary", out["summary"])
        finally:
            await self.ctx.board.set("done.mail_core", True)
            await self.ctx.board.set("done.mail", True)
