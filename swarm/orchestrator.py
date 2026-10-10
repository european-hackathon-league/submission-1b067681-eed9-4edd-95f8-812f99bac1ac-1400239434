"""Runs the registered agents phase by phase. Agents of one phase run concurrently; a failing agent never stops others.

Time: COLLECT/ANALYZE are cut off when the useful time is gone (ctx.time_left() <= 0); VERIFY and COMPUTE (plain code,
instant) and SUBMIT always run.
Local dev: SWARM_ONLY=a,b runs only those agents; SWARM_BOARD_IN=.swarm/board.json preloads a saved board (ignored
in scored runs). The board is dumped to .swarm/board.json after every phase.
"""
import asyncio
import faulthandler
import importlib
import logging
import os
import pkgutil
import sys
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


# Plain-code phases (cross-checks, the bid arithmetic) take well under a second: they run even when collecting used up
# its time, otherwise the submitter would get no bid at all (it then omits every position).
ALWAYS = (Phase.VERIFY, Phase.COMPUTE, Phase.SUBMIT)
QUICK_BUDGET_S = 30


async def _run_agent(agent: Agent, deadline_bound: bool) -> None:
    try:
        budget = agent.ctx.time_left() if deadline_bound else agent.ctx.seconds_left() - FINAL_MARGIN_S
        if agent.phase in (Phase.VERIFY, Phase.COMPUTE):
            budget = max(budget, QUICK_BUDGET_S)
        await asyncio.wait_for(agent.run(), timeout=max(1.0, budget))
    except asyncio.TimeoutError:
        agent.ctx.board.error(agent.name, "timed out")
        log.warning("%s timed out", agent.name)
    except Exception as error:
        agent.ctx.board.error(agent.name, f"{type(error).__name__}: {error}")
        log.exception("%s failed", agent.name)


def _position_keys(tender: dict) -> dict:
    try:
        from agents.compute_bid import position_keys
        return {str(p.get("position")): position_keys(tender.get("clarifications") or [], str(p.get("position")))["keys"]
                for p in tender.get("positions") or []}
    except Exception as error:  # noqa: BLE001 - a summary must never fail a run
        return {"error": str(error)[:80]}


def board_summary(board: Blackboard) -> dict:
    """A compact view of what the swarm knows (one log line), so a run's log shows it even if early lines are lost."""
    r = board.results
    erp = r.get("erp") or {}
    return {"pages": {k: sum(1 for e in board.evidence if e.kind == f"{k}.page") for k in ("tender", "spec", "erp", "mail")},
            "positions": len((r.get("tender") or {}).get("positions") or []),
            "qa": len((r.get("tender") or {}).get("clarifications") or []),
            "requirements": sorted(r.get("requirements") or {}),
            "materials": {p: (m.get("material"), m.get("reason")) for p, m in (r.get("materials") or {}).items()},
            "erp": {"materials": len(erp.get("materials") or {}), "conditions": len(erp.get("conditions") or []),
                    "stock": sorted(erp.get("stock") or {})},
            "discount": r.get("discount"),
            # which release mails were resolved, and which keys the Q&A gave each position: the two sides that must
            # meet for packaging, commitments, setup pools, project bonus and condition groups
            "mail_topics": {t: sorted(v) if isinstance(v, dict) else v for t, v in (r.get("mail") or {}).items()},
            "position_keys": _position_keys(r.get("tender") or {}),
            "open_gaps": [g.get("id") for g in r.get("gaps") or []],
            "tips": sum(len(v) for v in (r.get("tips") or {}).values()), "errors": len(board.errors)}


STALL_S = 45     # the event loop blocked this long: every thread's stack goes to stderr (diagnosis of frozen runs)


async def _heartbeat() -> None:
    """Re-arm the stall dump while the event loop runs; if one synchronous call blocks the loop, the dump fires."""
    try:
        while True:
            faulthandler.dump_traceback_later(STALL_S, repeat=True, file=sys.stderr)
            await asyncio.sleep(STALL_S / 3)
    finally:
        faulthandler.cancel_dump_traceback_later()


async def run_swarm(task: dict, playwright, scored: bool, env=os.environ):
    board = Blackboard()
    preload = env.get("SWARM_BOARD_IN")
    if preload and not scored:                       # never replay data in a scored run
        board = Blackboard.load(Path(preload))
    ctx = Context(task=task, board=board, llm=LLM(), pool=BrowserPool(playwright), scored=scored)
    classes = select(discover(), None if scored else env.get("SWARM_ONLY"))
    from swarm.browser_agent import _MAIN_TABS, _OWNERS, _OPENERS
    from swarm import guard
    _MAIN_TABS.clear()
    _OWNERS.clear()
    _OPENERS.clear()
    guard._DECISIONS.clear()
    guard._PENDING.clear()
    log.info("agents: %s", [c.__name__ for c in classes])
    heartbeat = asyncio.create_task(_heartbeat())
    try:
        await ctx.pool.prepare()
        for phase in Phase:
            agents = [c(ctx) for c in classes if c.phase == phase]
            if not agents:
                continue
            bound = phase != Phase.SUBMIT
            if phase not in ALWAYS and ctx.time_left() <= 0:
                log.warning("skipping phase %s: out of time", phase.name)
                continue
            log.info("phase %s: %s (%.0fs left)", phase.name, [a.name for a in agents], ctx.seconds_left())
            await asyncio.gather(*(_run_agent(a, bound) for a in agents))
            if phase == Phase.COLLECT:
                # Guard classification tasks are collection work too. None may survive into signing.
                pending = list(guard._TASKS)
                for t in pending:
                    t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                from agents import analyze_discount, analyze_materials, analyze_quantities, analyze_requirements
                await analyze_quantities.publish(ctx)
                await analyze_requirements.publish(ctx)
                await analyze_discount.publish(ctx)
                await analyze_materials.publish(ctx)
                log.info("board after collect (%.0fs left): %s", ctx.seconds_left(), board_summary(board))
            if not scored:
                board.dump(BOARD_DUMP)
        log.info("model calls: %d tokens: %s errors: %d", ctx.llm.calls, ctx.llm.tokens, len(board.errors))
        if (task.get("submission") or {}).get("kind", "api") != "api":
            receipt = board.get("submission") or {}
            if not receipt.get("accepted"):
                raise RuntimeError("No confirmed portal receipt; the run did not verify submission")
            log.info("portal acceptance confirmed: %s", receipt.get("reference"))
            return None                      # a "site" submission was entered by the submitter agent
        answer = board.get("answer")
        if answer is None:
            raise RuntimeError("No schema-valid API answer was produced")
        return answer
    finally:
        heartbeat.cancel()
        pending = list(guard._TASKS)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await ctx.pool.close()
        if ctx.llm._client is not None:
            await ctx.llm._client.close()
