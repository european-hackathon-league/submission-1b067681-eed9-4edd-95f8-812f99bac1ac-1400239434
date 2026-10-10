"""Agent base class and registry. One agent = one file in `agents/`, registered with @register."""
import enum
import logging
from abc import ABC, abstractmethod


class Phase(enum.IntEnum):
    COLLECT = 1   # scrapers: read the sites, post Evidence
    ANALYZE = 2   # analyzers: turn evidence into structured facts (quantities, requirements, candidate materials...)
    VERIFY = 3    # verifiers: cross-check authority / dates / tender reference / superseded records
    COMPUTE = 4   # calculators: deterministic arithmetic, set results["bid"]
    SUBMIT = 5    # fills the configurator, signs; ALWAYS runs, with reserved time


REGISTRY: list[type["Agent"]] = []


def register(cls):
    REGISTRY.append(cls)
    return cls


class Agent(ABC):
    name: str = ""               # defaults to the class name
    phase: Phase = Phase.COLLECT
    browser: int | None = None   # index of the browser this agent drives (0-3), None = no browser
    background: bool = False     # runs alongside the phase and is cancelled when the phase's other agents are done

    def __init__(self, ctx):
        self.ctx = ctx
        self.name = self.name or type(self).__name__
        self.log = logging.getLogger(self.name)

    @abstractmethod
    async def run(self) -> None:
        """Read from `self.ctx.board`, do the work, write Evidence / results back. Raise on failure (it is logged;
        other agents carry on). Stop early when `self.ctx.time_left() <= 0`."""
