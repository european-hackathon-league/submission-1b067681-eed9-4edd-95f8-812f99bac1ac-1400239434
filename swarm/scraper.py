"""Base class for site scrapers: a model-driven browse of ONE site that streams Evidence onto the blackboard.

Subclass, set `site_id` and `browser`, and write `goal()` (what the model should find, in general terms derived from
the brief; never scenario values). Override `run()` if you need several browse passes (e.g. one per sub-task).
"""
from swarm.agent import Agent, Phase
from swarm.blackboard import Evidence
from swarm.browser_agent import browse


class SiteScraper(Agent):
    phase = Phase.COLLECT
    site_id: str = ""
    max_steps: int = 40
    hints: str = ""

    def goal(self) -> str:
        raise NotImplementedError

    def login_text(self) -> str:
        site = self.ctx.site(self.site_id)
        return f"Site {self.site_id}: {site['url']}  credentials: {site.get('credentials')}"

    async def post(self, rec: dict) -> None:
        await self.ctx.board.post(Evidence(kind=rec["kind"], site=self.site_id, data=rec["data"],
                                           source=rec["source"], author=self.name, note=rec["note"]))

    async def run(self) -> None:
        prompt = "\n\n".join([self.login_text(), f"BRIEF:\n{self.ctx.brief}", f"YOUR JOB:\n{self.goal()}"])
        out = await browse(self.ctx, prompt, browser=self.browser or 0, site_id=self.site_id, on_record=self.post,
                           max_steps=self.max_steps, hints=self.hints, author=self.name)
        await self.ctx.board.set(f"{self.name}.summary", out["summary"])
        self.log.info("done in %d steps, %d records: %s", out["steps"], len(out["records"]), out["summary"][:200])
