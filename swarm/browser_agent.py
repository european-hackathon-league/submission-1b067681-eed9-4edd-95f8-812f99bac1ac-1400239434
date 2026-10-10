"""Model-driven browsing via the browser-use framework. The MODEL decides where to go and what matters; browser-use gives
it hands and eyes. This module only wires it to our setup: the run's CDP browsers, the Qwen proxy, no telemetry, navigation
limited to the task's websites, and a `record` action that streams facts onto the blackboard.

    out = await browse(ctx, goal="...", browser=1, site_id="erp", on_record=callback)

Rule: ONE browse() at a time per browser index (an agent works in the browser's active tab). Use different indices (0-3)
for parallel agents. Per-site knowledge goes into `goal`/`hints` as general strategy, never scenario values.
"""
import logging
import os
from urllib.parse import urlparse

# No telemetry / cloud sync / version check: the sandbox may only talk to the platform and the four sites.
for _flag in ("ANONYMIZED_TELEMETRY", "BROWSER_USE_CLOUD_SYNC", "BROWSER_USE_VERSION_CHECK"):
    os.environ[_flag] = "false"
os.environ.setdefault("BROWSER_USE_LOGGING_LEVEL", "info")

from browser_use import ActionResult, Agent, BrowserProfile, BrowserSession, ChatOpenAI, Tools  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

import browsers  # noqa: E402
import models  # noqa: E402

log = logging.getLogger("browse")

SYSTEM_EXTRA = """
Working rules:
- Dismiss cookie walls/popups; if a session expired, log in again (credentials are in the task); retry flaky error pages.
- Watch for traps: look-alike or superseded records, paging, hidden tabs/iframes, archive folders, other timezones.
- Call `record` for every relevant fact as soon as you see it, copied exactly as shown. Never invent values.
- Finish with `done` and a short summary when the goal is met or you are stuck."""


class RecordParams(BaseModel):
    kind: str = Field(description="what this fact is, e.g. a label agreed with the analyzers")
    data: dict = Field(description="the facts exactly as shown on the page")
    source: str = Field(default="", description="URL / document / mail subject where it was read")
    note: str = Field(default="", description="caveats: superseded, withdrawn, other timezone, unsure")


def allowed_domains(task: dict) -> list[str]:
    """Only the task's websites may be opened (fair play: no other hosts)."""
    out = []
    for site in task.get("access", []):
        parts = urlparse(site.get("url") or "")
        if parts.netloc:
            out.append(f"{parts.scheme}://{parts.netloc}")
    return out


def make_llm() -> ChatOpenAI:
    """The hosted Qwen through the proxy. Settings tuned for a non-OpenAI model; adjust here if tool calls misbehave."""
    return ChatOpenAI(model=models.QWEN_MODEL, base_url=os.environ["QWEN_BASE_URL"], api_key=os.environ["QWEN_API_KEY"],
                      temperature=0.0, frequency_penalty=None, timeout=360, max_retries=3,
                      add_schema_to_system_prompt=True, dont_force_structured_output=True,
                      remove_min_items_from_schema=True, remove_defaults_from_schema=True)


def make_session(ctx, browser: int = 0) -> BrowserSession:
    """Attach to the run's CDP browser `browser` (locally browser-use starts its own Chromium; HEADLESS=1 hides it)."""
    urls = browsers.cdp_urls()
    profile = dict(allowed_domains=allowed_domains(ctx.task), enable_default_extensions=False, keep_alive=True)
    if urls:
        return BrowserSession(cdp_url=urls[browser % len(urls)], **profile)
    return BrowserSession(headless=os.environ.get("HEADLESS") == "1", **profile)


async def browse(ctx, goal: str, *, browser: int = 0, site_id: str = "", on_record=None, max_steps: int = 40,
                 hints: str = "", author: str = "browse", use_vision="auto") -> dict:
    """Run a browser-use agent until done / max_steps / out of time. Returns {"summary", "records", "steps", "done"}."""
    records: list[dict] = []
    tools = Tools()

    @tools.action("Save a fact you found (kept even if you run out of time). Use for every relevant fact.",
                  param_model=RecordParams)
    async def record(params: RecordParams, browser_session: BrowserSession):
        url = await browser_session.get_current_page_url()
        rec = {"kind": params.kind, "data": params.data, "source": params.source or url, "note": params.note,
               "site": site_id, "author": author}
        records.append(rec)
        if on_record:
            await on_record(rec)
        return ActionResult(extracted_content=f"recorded {params.kind}")

    session = make_session(ctx, browser)
    agent = Agent(task=goal, llm=make_llm(), browser_session=session, tools=tools, use_vision=use_vision,
                  use_judge=False, generate_gif=False, extend_system_message=SYSTEM_EXTRA + (f"\n{hints}" if hints else ""))

    async def stop_when_out_of_time(_agent) -> None:
        if ctx.time_left() <= 0:
            agent.stop()

    try:
        history = await agent.run(max_steps=max_steps, on_step_start=stop_when_out_of_time)
    finally:
        await session.kill() if not browsers.cdp_urls() else await session.stop()   # never kill the run's browsers
    return {"summary": history.final_result() or "", "records": records, "steps": len(history.history),
            "done": history.is_done()}
