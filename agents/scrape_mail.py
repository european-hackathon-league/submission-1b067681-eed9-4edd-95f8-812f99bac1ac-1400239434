# OWNER: (name)   ROLE: scraper   SITE: webmail (role "mail")
"""Reads the mails that matter for this tender, saved as page evidence ("mail.page").

Every message is read once:
1. The main tab logs in while the brief is being read.
2. Topic searches start at once, in parallel lanes (main tab + SWARM_MAIL_TABS tabs; each tab logs in: this webmail
   keeps its login per tab): one search per topic the brief names ('<reference> <topic word>'), the topics that
   decide most of the bid first (TOPIC_PRIORITY); a search that ends unfinished gets one more try at the end of its
   lane. A search for the exact reference alone lists every message and needs many result pages, so it only runs
   last, as a catch-all for what the topic searches missed. Before each job, topics that already have a valid message
   are skipped; delivery commitments only count as covered when every delivery key of the Q&A has its mail, and their
   job names the delivery keys and material numbers known by then (a webmail may find them only by article number).
   results["done.mail_core"] is set after the discount search (the discount analyst starts).
3. Messages already saved by another agent are not opened again (read_page returns the saved text). Then
   results["done.mail"].
The signing TAN is NOT read here: the submitter asks for it when it signs (agents/submit_bid.py)."""
import os
import asyncio

from agents.analyze_discount import covered_topics, publish, refresh
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
    "page link. Result rows without a visible link: click the subject once, then read_page the opened message "
    "(its own page link if it opens in a frame); do not search the page for links more than once. "
    "Skip signing-TAN mails.")

INDEX_JOB = (
    "Catch-all: search the exact tender reference ALONE (no other word), first inbox then archive. read_page each "
    "result page with topic 'mail index'. On EACH result page, immediately read the matching message links not read "
    "yet before moving to another index page or folder. Still missing: {missing}. Use read_page(urls=[observed message "
    "links], topic='mail messages'), up to 12 URLs. Return to the observed index URL to continue pagination. Skip "
    "look-alike suffixes, other tenders and signing TANs. Then done, listing subjects read.\n" + SEARCH_RULES)

DELIVERY = "delivery_commitments"
# What a missing mail costs the bid, most first: the discount, the technical release (end reserves decide which
# material fits a usable-travel requirement) and FX (a price in another currency cannot be bid without it) gate whole
# positions; price indices and packaging enter every price. Lanes take topics round-robin, so the first three each
# start a lane.
TOPIC_PRIORITY = ["discount_approval", "technical_release", "fx_fixing", "price_indices", "packaging_tariffs",
                  "condition_rules", "project_bonus", "setup_pools", DELIVERY]


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
        kwargs.setdefault("stop_reserve", SUBMIT_RESERVE_S + 90)    # the mail analyst needs the last pages too
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
        rank = {t: i for i, t in enumerate(TOPIC_PRIORITY)}
        topics.sort(key=lambda t: rank.get(t, rank[DELIVERY] - 0.5))     # unknown topics before delivery
        return topics, senders, words

    def topic_job(self, topic: str, words: dict, senders: dict, keys=(), materials=(), reserve_keys=()) -> str:
        word = words.get(topic) or topic.replace("_", " ")
        sender = senders.get(topic)
        job = (f"Find mails on topic '{word}' ({topic.replace('_', ' ')}) for this tender"
               f"{f' (sender {sender})' if sender else ''}: search '<reference> {word}' in inbox and archive; read "
               f"every matching message.")
        if topic == DELIVERY and (keys or materials):
            job += (f" Commitments are per delivery key / article: keys {list(keys)}, material numbers "
                    f"{list(materials)}. If the search hint or the results ask for another identifier, or a key has "
                    f"no hit, search '<reference> <material number>' (or the key) for each one still missing.")
        if topic == "technical_release" and reserve_keys:
            job += f" The specification refers to release / reserve keys {list(reserve_keys)}."
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
        queue, retried, before = list(topics), set(), {}
        while queue:
            topic = queue.pop(0)
            if self.ctx.time_left() <= 60:
                return
            if topic not in self.covered(await publish(self.ctx)):
                keys, materials = delivery_ids(self.ctx.board) if topic == DELIVERY else ((), ())
                reserve_keys = [r.get("reserve_key") for r in (self.ctx.board.get("requirements") or {}).values()
                                if isinstance(r, dict) and r.get("reserve_key")]
                out = {"done": False}
                try:
                    job = self.topic_job(topic, words, senders, keys, materials, list(dict.fromkeys(reserve_keys)))
                    if topic in retried:
                        job += ("\nSECOND TRY: the first search saved no readable message for this topic (maybe only "
                                "the result list). Open each matching message itself and read_page it. The first try "
                                f"reported: {before.get(topic, '')!r}. If it names a message link or id, read_page "
                                "that link first.")
                    out = await self.browse_until_done(job, attempts=1, max_steps=12, tab=tab)
                except Exception as error:      # one failed search must not end the other topics
                    self.ctx.board.error(self.name, f"mail search {topic} failed: {type(error).__name__}: {error}")
                # The model may call it done with only the result list saved: judge by what the analyst extracted.
                if topic not in retried and (not out.get("done") or topic not in self.covered(await refresh(self.ctx))):
                    retried.add(topic)
                    before[topic] = str(out.get("summary") or "")[:400]
                    # Aborted after a step or two (browser trouble, not a real search): try again at once, since the
                    # lane's first topics matter most. A real attempt that missed goes to the end of the lane.
                    if (out.get("steps") or 0) < 3:
                        queue.insert(0, topic)
                    else:
                        queue.append(topic)
            if first and topic == topics[0]:
                await first()
                first = None

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
