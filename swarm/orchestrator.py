"""Runs the registered agents phase by phase. Agents of one phase run concurrently; a failing agent never stops others.

Time: non-SUBMIT phases are cut off when the useful time is gone (ctx.time_left() <= 0), then SUBMIT always runs.
Local dev: SWARM_ONLY=a,b runs only those agents; SWARM_BOARD_IN=.swarm/board.json preloads a saved board (ignored
in scored runs). The board is dumped to .swarm/board.json after every phase.
"""
import asyncio
import importlib
import logging
import os
import pkgutil
from pathlib import Path

import agents as agents_pkg
from swarm.agent import REGISTRY, Agent, Phase
from swarm.blackboard import Blackboard
from swarm.browser_pool import BrowserPool
from swarm.context import FINAL_MARGIN_S, Context
from swarm.llm import LLM

log = logging.getLogger("swarm")
BOARD_DUMP = Path(".swarm/board.json")


def discover() -> list[type[Agent]]:
    """Import every module in agents/ so its @register decorators run."""
    for mod in pkgutil.iter_modules(agents_pkg.__path__):
        importlib.import_module(f"agents.{mod.name}")
    return list(REGISTRY)


def select(classes: list[type[Agent]], only: str | None) -> list[type[Agent]]:
    if not only:
        return classes
    wanted = {n.strip() for n in only.split(",") if n.strip()}
    return [c for c in classes if (c.name or c.__name__) in wanted or c.__name__ in wanted]


async def _run_agent(agent: Agent, deadline_bound: bool) -> None:
    try:
        budget = agent.ctx.time_left() if deadline_bound else agent.ctx.seconds_left() - FINAL_MARGIN_S
        await asyncio.wait_for(agent.run(), timeout=max(1.0, budget))
    except asyncio.TimeoutError:
        agent.ctx.board.error(agent.name, "timed out")
        log.warning("%s timed out", agent.name)
    except Exception as error:
        agent.ctx.board.error(agent.name, f"{type(error).__name__}: {error}")
        log.exception("%s failed", agent.name)


async def run_swarm(task: dict, playwright, scored: bool, env=os.environ):
    board = Blackboard()
    preload = env.get("SWARM_BOARD_IN")
    if preload and not scored:                       # never replay data in a scored run
        board = Blackboard.load(Path(preload))
    ctx = Context(task=task, board=board, llm=LLM(), pool=BrowserPool(playwright), scored=scored)
    classes = select(discover(), env.get("SWARM_ONLY"))
    log.info("agents: %s", [c.__name__ for c in classes])
    for phase in Phase:
        agents = [c(ctx) for c in classes if c.phase == phase]
        if not agents:
            continue
        bound = phase != Phase.SUBMIT
        if bound and ctx.time_left() <= 0:
            log.warning("skipping phase %s: out of time", phase.name)
            continue
        log.info("phase %s: %s (%.0fs left)", phase.name, [a.name for a in agents], ctx.seconds_left())
        await asyncio.gather(*(_run_agent(a, bound) for a in agents))
        if not scored:
            board.dump(BOARD_DUMP)
    log.info("model calls: %d tokens: %s errors: %d", ctx.llm.calls, ctx.llm.tokens, len(board.errors))
    if (task.get("submission") or {}).get("kind", "api") != "api":
        log.info("answer (entered on the website): %s", board.get("answer"))
        return None                      # a "site" submission was entered by the submitter agent
    return board.get("answer")
