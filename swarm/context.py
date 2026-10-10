"""Everything an agent gets: the task, the blackboard, the model, the browsers and the clock."""
import time
from dataclasses import dataclass, field

import run as contract
from swarm.blackboard import Blackboard
from swarm.llm import LLM

SUBMIT_MARGIN_S = 180   # non-submit work stops this long before expiry; the SUBMIT phase gets it (minus 60 s safety)


@dataclass
class Context:
    task: dict
    board: Blackboard
    llm: LLM
    pool: object                      # swarm.browser_pool.BrowserPool (None in unit tests)
    scored: bool = False              # True in a real run (has a run token); False for local scouting
    t0: float = field(default_factory=time.time)

    @property
    def brief(self) -> str:
        return self.task["brief"]

    def site(self, site_id: str) -> dict:
        """The access entry (siteId, url, credentials) of a website, e.g. ctx.site("erp")."""
        for entry in self.task["access"]:
            if entry.get("siteId") == site_id:
                return entry
        raise KeyError(f"no site {site_id!r}; have {[s.get('siteId') for s in self.task['access']]}")

    @property
    def site_ids(self) -> list[str]:
        return [s.get("siteId") for s in self.task["access"]]

    def seconds_left(self) -> float:
        return contract.seconds_left(self.task)

    def time_left(self) -> float:
        """Seconds of useful work left (run clock minus the submit margin)."""
        return self.seconds_left() - SUBMIT_MARGIN_S

    def elapsed(self) -> float:
        return time.time() - self.t0
