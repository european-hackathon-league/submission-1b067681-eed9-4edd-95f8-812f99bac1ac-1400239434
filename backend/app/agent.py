"""Agent definition. Change the instructions/tools here; main.py stays the same."""

import os

from agents import Agent, Runner, ToolCallItem

from .tools import ALL_TOOLS

INSTRUCTIONS = """You are a helpful assistant for a hackathon demo.
Use tools when they help:
- fetch_url to read web pages or JSON APIs,
- read_local_file to read files from the project's data folder,
- call_external_api to get (dummy) weather data for a city.
Be concise. Say which tools you used and why."""

agent = Agent(
    name="EHL Agent",
    instructions=INSTRUCTIONS,
    tools=ALL_TOOLS,
    # None -> SDK default; override with OPENAI_MODEL in .env
    model=os.getenv("OPENAI_MODEL") or None,
)


async def run_agent(message: str, history: list[dict] | None = None) -> dict:
    """Run the agent on one user message (plus optional prior chat turns)."""
    items = [{"role": m["role"], "content": m["content"]} for m in (history or [])]
    items.append({"role": "user", "content": message})

    result = await Runner.run(agent, items, max_turns=10)

    tool_calls = [
        getattr(item.raw_item, "name", type(item.raw_item).__name__)
        for item in result.new_items
        if isinstance(item, ToolCallItem)
    ]
    return {"reply": str(result.final_output), "tool_calls": tool_calls}
