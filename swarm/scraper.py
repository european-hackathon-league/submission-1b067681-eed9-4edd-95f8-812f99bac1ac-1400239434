"""Base class for site scrapers: a model-driven browse of ONE site that streams Evidence onto the blackboard.

Subclass, set `role` ('tender' | 'spec' | 'erp' | 'mail') and `browser`, and write `goal()` (what the model should
find, in general terms derived from the brief; never scenario values). The site id for the role is resolved at run
time (datasets name their sites differently). Override `run()` for several browse passes; call `self.browse(goal)`.

When the scraper ends (done, failed or out of time) it sets results[f"done.{role}"] so analysts waiting on it start.
"""
from swarm.agent import Agent, Phase
from swarm.blackboard import Evidence
from swarm.browser_agent import browse
from swarm.context import SUBMIT_RESERVE_S
from swarm.extract import brief_intro
from swarm.sites import site_for


class SiteScraper(Agent):
    phase = Phase.COLLECT
    role: str = ""
    site_id: str = ""
    max_steps: int = 40
    hints: str = ""

    def goal(self) -> str:
        raise NotImplementedError

    def login_text(self) -> str:
        site = self.ctx.site(self.site_id)
        creds = site.get("credentials") or {}
        return f"Website ({self.role}): {site['url']}\nLogin: username {creds.get('username')!r}, password {creds.get('password')!r}"

    def task_text(self) -> str:
        facts = self.ctx.board.get("brief") or {}
        known = {k: facts.get(k) for k in ("tender_reference", "tender_title", "buyer", "scenario_date", "bidder_company")
                 if facts.get(k)}
        return f"CONTEXT:\n{brief_intro(self.ctx.brief)}\nKey facts: {known}" if known else \
            f"CONTEXT:\n{brief_intro(self.ctx.brief)}"

    async def post(self, rec: dict) -> None:
        kind = f"{self.role}.page" if rec["kind"] == "page" else rec["kind"]
        await self.ctx.board.post(Evidence(kind=kind, site=self.site_id, data=rec["data"], source=rec["source"],
                                           author=self.name, note=rec["note"]))

    async def browse(self, job: str, *, browser: int | None = None, max_steps: int | None = None, **kwargs) -> dict:
        prompt = "\n\n".join([self.login_text(), self.task_text(), f"YOUR JOB:\n{job}"])
        out = await browse(self.ctx, prompt, browser=self.browser if browser is None else browser, site_id=self.site_id,
                           on_record=self.post, max_steps=max_steps or self.max_steps, hints=self.hints,
                           author=self.name, **kwargs)
        self.log.info("browse done in %d steps, %d records: %s", out["steps"], len(out["records"]), out["summary"][:300])
        return out

    async def browse_until_done(self, job: str, attempts: int = 3, **kwargs) -> dict:
        """browse(); if the agent stopped without finishing (errors, step limit), start again with a note on what
        is already saved, while time allows."""
        out = {"summary": "", "done": False}
        for attempt in range(attempts):
            note = ""
            if attempt:
                saved = sorted({e.data.get("topic") for e in self.ctx.board.query(f"{self.role}.page")} - {None})
                note = (f"\n\nNOTE: a previous attempt stopped early ({out['summary'][:300]!r}). Already saved: "
                        f"{saved or 'nothing'}. Do only what is still missing.")
            out = await self.browse(job + note, **kwargs)
            if out["done"] or self.ctx.seconds_left() - kwargs.get("stop_reserve", SUBMIT_RESERVE_S) < 120:
                break
        return out

    async def wait_brief(self, timeout: float = 180) -> dict:
        try:
            return await self.ctx.board.wait_for(key="brief", timeout=timeout) or {}
        except TimeoutError:
            return {}

    def required(self) -> list[tuple[tuple[str, ...], str]]:
        """Pages that must be saved, as (topic keywords, focused job). After the main browse, a focused browse runs
        for each one whose keywords appear in no saved topic. Override per scraper."""
        return []

    def saved_topics(self) -> str:
        return " | ".join(str(e.data.get("topic", "")).lower() for e in self.ctx.board.query(f"{self.role}.page"))

    async def run(self) -> None:
        try:
            self.site_id = self.site_id or await site_for(self.ctx, self.role)
            out = await self.browse_until_done(self.goal())   # the brief's intro already names the tender: start at once
            await self.ctx.board.set(f"{self.name}.summary", out["summary"])
            for keywords, job in self.required():
                if self.ctx.time_left() > 90 and not any(k in self.saved_topics() for k in keywords):
                    self.log.warning("nothing saved for %s: focused browse", keywords)
                    await self.browse(job, max_steps=15)
        finally:
            await self.ctx.board.set(f"done.{self.role}", True)
