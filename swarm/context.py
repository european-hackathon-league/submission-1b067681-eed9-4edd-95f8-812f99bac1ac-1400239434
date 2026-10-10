"""Everything an agent gets: the task, the blackboard, the model, the browsers and the clock."""
import asyncio
import os
import time
from dataclasses import dataclass, field

import run as contract
from swarm.blackboard import Blackboard
from swarm.llm import LLM

# Collecting and analysing stop this long before expiry; the rest belongs to SUBMIT (filling the bid form and signing
# takes several minutes of browser steps). SUBMIT itself stops FINAL_MARGIN_S before expiry.
SUBMIT_RESERVE_S = float(os.environ.get("SWARM_SUBMIT_RESERVE_S", 540))
FINAL_MARGIN_S = 40


@dataclass
class Context:
    task: dict
    board: Blackboard
    llm: LLM
    pool: object                      # swarm.browser_pool.BrowserPool (None in unit tests)
    scored: bool = False              # True in a real run (has a run token); False for local scouting
    t0: float = field(default_factory=time.time)
    _locks: dict = field(default_factory=dict, repr=False)
    shared: dict = field(default_factory=dict, repr=False)   # in-memory objects agents share (queues); never dumped

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
        left = contract.seconds_left(self.task)
        simulated = os.environ.get("SWARM_DEADLINE_S")          # local runs: pretend the run lasts this long
        if left == float("inf") and simulated and not self.scored:
            return self.t0 + float(simulated) - time.time()
        return left

    def time_left(self) -> float:
        """Seconds of collecting/analysing left (run clock minus what SUBMIT needs)."""
        return self.seconds_left() - SUBMIT_RESERVE_S

    def elapsed(self) -> float:
        return time.time() - self.t0

    def lock(self, name: str) -> asyncio.Lock:
        """A named lock shared by all agents of this run (e.g. so only one agent resolves the site roles)."""
        return self._locks.setdefault(name, asyncio.Lock())
