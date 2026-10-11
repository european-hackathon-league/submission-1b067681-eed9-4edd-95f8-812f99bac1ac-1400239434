"""Everything an agent gets: the task, the blackboard, the model, the browsers and the clock."""
import asyncio
import os
import time
from dataclasses import dataclass, field

import run as contract
from swarm.blackboard import Blackboard
from swarm.llm import LLM

# Collecting and analysing stop `submit_reserve()` seconds before expiry; the rest belongs to SUBMIT (filling the bid
# form and signing takes several minutes of browser steps). SUBMIT itself stops FINAL_MARGIN_S before expiry.
# signing 4 positions took 466 s in run_7732ba545b50b3a5 and ended 60 s before expiry; a 5th position adds ~90 s.
# SWARM_SUBMIT_RESERVE_S > 0 fixes the reserve; by default it is estimated during the run from the positions to enter
# and the measured pace of browsing steps (see Context.submit_reserve), within [MIN_RESERVE_S, MAX_RESERVE_S].
SUBMIT_RESERVE_S = float(os.environ.get("SWARM_SUBMIT_RESERVE_S", 0)) or 600     # the upper bound / fixed value
FIXED_RESERVE = float(os.environ.get("SWARM_SUBMIT_RESERVE_S", 0)) > 0
MIN_RESERVE_S = float(os.environ.get("SWARM_MIN_RESERVE_S", 390))
MAX_RESERVE_S = SUBMIT_RESERVE_S
FINAL_MARGIN_S = 40
# Submission model, in browsing steps: wizard steps independent of the positions (login, tender, wizard start, bidder,
# decision, documents, TAN request/entry, summary, confirmation, receipt), steps per position, and the steps the
# wizard warm-up (agents/submit_bid.WizardPrep) already did. Full-reasoning submitter steps are slower than the
# flash steps measured during collection. TAN lookup runs next to the wizard and costs extra time only at the end.
# run_7b060fa870cea9b6: an entered position took 3 steps (one 6: cascading selects), an omitted one 1 step
BASE_STEPS, STEPS_PER_POSITION, PREP_SAVES_STEPS = 12, 3, 4
# Measured submitter pace vs collection pace: run_56cfdff4ff83d569 36 steps / 470 s (13 s), run_7a11d3a90d55e1ce
# 26 steps / 316 s (12 s, collection median ~11-12 s at the time): full-reasoning steps are only slightly slower.
FULL_REASONING_FACTOR, TAN_S, SAFETY_S = 1.15, 60, 45
MIN_SUBMIT_STEP_S = float(os.environ.get("SWARM_MIN_SUBMIT_STEP_S", 13))   # a submitter step is never assumed faster
# A scored run lasts 20 minutes; test runs may last 2 hours. Every run plans with at most RUN_LIMIT_S from process
# start, so test runs show the timing (and the follow-ups that fit) of a scored run. The run's real expiry still wins
# when it is earlier.
RUN_LIMIT_S = float(os.environ.get("SWARM_RUN_LIMIT_S", 1200))


@dataclass
class Context:
    task: dict
    board: Blackboard
    llm: LLM
    pool: object                      # swarm.browser_pool.BrowserPool (None in unit tests)
    scored: bool = False              # True in a real run (has a run token); False for local scouting
    t0: float = field(default_factory=time.time)
    monotonic_start: float = field(default_factory=time.monotonic)
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
            return float(simulated) - self.elapsed()
        return min(left, RUN_LIMIT_S - self.elapsed()) if RUN_LIMIT_S > 0 else left

    def time_left(self) -> float:
        """Seconds of collecting/analysing left (run clock minus what SUBMIT needs)."""
        return self.seconds_left() - self.submit_reserve()

    def submit_reserve(self) -> float:
        """What SUBMIT needs, from the positions to enter and the measured step pace (fixed when configured)."""
        if FIXED_RESERVE:
            return SUBMIT_RESERVE_S
        from swarm import timeline
        positions = len((self.board.get("tender") or {}).get("positions") or []) or 5
        step = min(30.0, max(6.0, timeline.current().median_step()))
        steps = BASE_STEPS - (PREP_SAVES_STEPS if self.shared.get("wizard_ready") else 0) + STEPS_PER_POSITION * positions
        submit_step = max(MIN_SUBMIT_STEP_S, step * FULL_REASONING_FACTOR)
        estimate = steps * submit_step + TAN_S + SAFETY_S + FINAL_MARGIN_S
        return min(MAX_RESERVE_S, max(MIN_RESERVE_S, estimate))

    def stop_at(self, extra: float = 0.0) -> float:
        """Seconds-left mark at which collection work with `extra` seconds of follow-up analysis must stop."""
        return self.submit_reserve() + extra

    def elapsed(self) -> float:
        return time.monotonic() - self.monotonic_start

    def lock(self, name: str) -> asyncio.Lock:
        """A named lock shared by all agents of this run (e.g. so only one agent resolves the site roles)."""
        return self._locks.setdefault(name, asyncio.Lock())
