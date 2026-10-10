"""Your agent's entry point: agent.json runs `uv run --no-sync python agent.py`, in the sandbox and on your machine.

The kit only does the plumbing: run.py reads the run and submits an answer, browsers.py attaches to the run's
browsers, models.py calls the hosted Qwen and Clef. Everything else is yours to build, in `solve` below.
Docs: $API/docs/building-an-agent.md, $API/docs/run-contract.md, and the rules: $API/docs/fair-play.md
"""
import asyncio
import json
import logging
import sys

from playwright.async_api import async_playwright

import run as contract
from swarm.orchestrator import run_swarm


async def solve(task: dict, playwright, scored: bool) -> dict | None:
    """Runs the swarm (see docs/SWARM.md). Agents live in agents/; the orchestrator runs them phase by phase.

    For an "api" submission the swarm leaves the answer in board.results["answer"]; for a "site" submission the
    submit agent enters it on the website and the result is None.
    """
    return await run_swarm(task, playwright, scored)


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    task, token = contract.load_run()
    sites = ", ".join(f"{site.get('siteId')} {site.get('url')}" for site in task["access"])
    print(f"Run {task.get('run_id') or '(local: the scouting logins, nothing counts)'}; websites: {sites}; "
          f"answer via {task['submission']['kind']}", flush=True)
    async with async_playwright() as playwright:
        answer = await solve(task, playwright, scored=bool(token))
    if answer is None:
        print("Nothing to post: the bid is entered and signed on the website by the submitter agent"
              if task["submission"].get("kind") == "site" else "No answer to submit", flush=True)
        return 0
    if not token:
        print("Your answer (locally nothing is submitted):", json.dumps(answer, ensure_ascii=False), flush=True)
        return 0
    status, body = contract.submit_answer(task, token, answer)
    print(f"Answer: HTTP {status} {body[:2000]}", flush=True)
    return 0 if status == 202 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
