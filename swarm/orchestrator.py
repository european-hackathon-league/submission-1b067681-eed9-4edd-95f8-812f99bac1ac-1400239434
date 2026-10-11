"""Runs the registered agents phase by phase. Agents of one phase run concurrently; a failing agent never stops others.

Time: COLLECT/ANALYZE are cut off when the useful time is gone (ctx.time_left() <= 0); VERIFY and COMPUTE (plain code,
instant) and SUBMIT always run.
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
from swarm import timeline
from swarm.agent import REGISTRY, Agent, Phase
from swarm.blackboard import Blackboard
from swarm.browser_pool import BrowserPool
from swarm.context import FINAL_MARGIN_S, Context
from swarm.llm import LLM

log = logging.getLogger("swarm")
BOARD_DUMP = Path(".swarm/board.json")
TIMELINE_DUMP = Path(".swarm/timeline.json")
PROGRESS_EVERY_S = float(os.environ.get("SWARM_PROGRESS_S", "60"))


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
    timeline.AGENT.set(agent.name)          # every span this agent (and the tasks it starts) records carries its name
    try:
        if deadline_bound and agent.phase not in (Phase.VERIFY, Phase.COMPUTE):
            await _until_collect_ends(agent.ctx, agent.run())
            return
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


async def _until_collect_ends(ctx, coro) -> None:
    """Run `coro` until it ends or the collect time is used up. The cutoff follows the live submission reserve (it
    moves with the measured pace), so it is re-checked every second rather than fixed when the phase starts."""
    task = asyncio.ensure_future(coro)
    try:
        while not task.done():
            left = ctx.time_left()
            if left <= 0:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise asyncio.TimeoutError
            await asyncio.wait({task}, timeout=min(1.0, max(0.05, left)))
        task.result()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


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
            "unresolved": (r.get("bid_draft") or {}).get("unresolved"),
            "tips": sum(len(v) for v in (r.get("tips") or {}).values()), "errors": len(board.errors)}


def progress_line(ctx) -> str:
    """One line per minute: clock, coverage of the draft bid, and where the last minute went."""
    tl = timeline.current()
    since = max(0.0, tl.now() - PROGRESS_EVERY_S)
    totals = tl.totals(since)
    cov = (ctx.board.get("bid_draft") or {}).get("coverage") or {}
    pages = "/".join(str(sum(1 for e in ctx.board.evidence if e.kind == f"{k}.page")) for k in ("tender", "spec", "erp", "mail"))

    def t(cat):
        x = totals.get(cat) or {"s": 0, "n": 0}
        return f"{x['s']:.0f}s/{x['n']}"
    return (f"progress {timeline.clock(tl.now())} left {ctx.seconds_left():.0f}s (collect {ctx.time_left():.0f}s, "
            f"reserve {ctx.submit_reserve():.0f}s) pages t/s/e/m {pages} positions {cov.get('priced', 0)}/"
            f"{cov.get('positions', 0)} priced, {cov.get('complete', 0)} complete, {cov.get('unresolved', 0)} open deps"
            f" | last {PROGRESS_EVERY_S:.0f}s: steps {t('browse.step')} queue {t('model.queue')} calls "
            f"{t('model.call')} extract {t('extract')} waits {t('wait.dep')} recovery {t('recovery')} | "
            f"model calls {ctx.llm.calls}")


async def _progress(ctx) -> None:
    while True:
        await asyncio.sleep(PROGRESS_EVERY_S)
        try:
            log.info(progress_line(ctx))
        except Exception as error:  # noqa: BLE001 - reporting must never fail a run
            log.debug("progress line failed: %s", error)


async def run_swarm(task: dict, playwright, scored: bool, env=os.environ):
    board = Blackboard()
    preload = env.get("SWARM_BOARD_IN")
    if preload and not scored:                       # never replay data in a scored run
        board = Blackboard.load(Path(preload))
    ctx = Context(task=task, board=board, llm=LLM(), pool=BrowserPool(playwright), scored=scored)
    tl = timeline.reset(ctx.monotonic_start)
    classes = select(discover(), None if scored else env.get("SWARM_ONLY"))
    from swarm.browser_agent import _CHECKED, _HUNG_AT, _MAIN_TABS, _OWNERS, _OPENERS
    from swarm import guard
    for state in (_MAIN_TABS, _OWNERS, _OPENERS, _CHECKED, _HUNG_AT):
        state.clear()
    guard._DECISIONS.clear()
    guard._PENDING.clear()
    log.info("agents: %s", [c.__name__ for c in classes])
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
            if phase == Phase.SUBMIT:
                timeline.mark("submit start", left=round(ctx.seconds_left()),
                              positions=len(((board.get("bid") or {}).get("answer") or {}).get("positions") or []))
            progress = asyncio.create_task(_progress(ctx))
            background = [asyncio.create_task(_run_agent(a, bound)) for a in agents if a.background]
            try:
                await asyncio.gather(*(_run_agent(a, bound) for a in agents if not a.background))
            finally:
                for running in background + [progress]:
                    running.cancel()
                await asyncio.gather(progress, *background, return_exceptions=True)
            if phase == Phase.COLLECT:
                timeline.mark("collect end", left=round(ctx.seconds_left()))
                tl.log_report()          # the platform ends the process right after the submission: report now too
                await _cancel_background(ctx)
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
                raise RuntimeError("No confirmed portal receipt; the run did not verify submission"
                                   + (f" ({receipt['evidence']})" if receipt.get("evidence") else ""))
            log.info("portal acceptance confirmed: %s %s", receipt.get("reference"), receipt.get("evidence") or receipt.get("quote", ""))
            return None                      # a "site" submission was entered by the submitter agent
        answer = board.get("answer")
        if answer is None:
            raise RuntimeError("No schema-valid API answer was produced")
        return answer
    finally:
        pending = list(guard._TASKS)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await _cancel_background(ctx)
        await ctx.pool.close()
        if ctx.llm._client is not None:
            await ctx.llm._client.close()
        try:
            tl.log_report()
            if not scored:
                tl.dump(TIMELINE_DUMP)
        except Exception as error:  # noqa: BLE001 - reporting must never fail a run
            log.debug("timeline report failed: %s", error)


async def _cancel_background(ctx) -> None:
    """Extraction tasks started by analysts in the background (they publish when done) end with collection."""
    tasks = [t for t in ctx.shared.get("background", set()) if not t.done()]
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
