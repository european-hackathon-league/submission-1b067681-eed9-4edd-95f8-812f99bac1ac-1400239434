"""Model-driven browsing via the browser-use framework. The MODEL decides where to go and what matters; browser-use gives
it hands and eyes. This module only wires it to our setup: the run's CDP browsers, the Qwen proxy, no telemetry, navigation
limited to the task's websites, and three actions of our own:

- `read_page`: extract the full text of the page the model chose (iframes, collapsed sections, tooltips, canvas labels),
  optionally open a URL first and follow a "next page" link the model names; with a `topic` the text is kept as evidence.
- `record`: save one fact the model read.
- `fill_form`: set several fields of the current form at once (helper for long forms such as the bid configurator).

    out = await browse(ctx, goal="...", browser=1, site_id="erp", on_record=callback)

Rule: ONE browse() at a time per browser index (an agent works in the browser's active tab). Use different indices (0-3)
for parallel agents. Per-site knowledge goes into `goal`/`hints` as general strategy, never scenario values.
"""
import asyncio
import json
import logging
import os
from urllib.parse import urljoin, urlparse

# No telemetry / cloud sync / version check: the sandbox may only talk to the platform and the four sites.
for _flag in ("ANONYMIZED_TELEMETRY", "BROWSER_USE_CLOUD_SYNC", "BROWSER_USE_VERSION_CHECK"):
    os.environ[_flag] = "false"
os.environ.setdefault("BROWSER_USE_LOGGING_LEVEL", "info")
# The sandbox browsers are slower than a laptop: give page-state capture more time before browser-use calls it a failure.
os.environ.setdefault("TIMEOUT_BrowserStateRequestEvent", "60")
os.environ.setdefault("TIMEOUT_NavigateToUrlEvent", "45")

from browser_use import ActionResult, Agent, BrowserProfile, BrowserSession, ChatOpenAI, Tools  # noqa: E402,F401
from browser_use.browser.watchdogs.aboutblank_watchdog import AboutBlankWatchdog  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402


async def _no_loading_animation(self, target_id, browser_session_label) -> None:
    """browser-use draws a loading animation on about:blank tabs that loads its logo from an external host. The sandbox
    cannot reach that host, so the tab stayed "loading" and the first navigation in that browser hung."""


AboutBlankWatchdog._show_dvd_screensaver_loading_animation_cdp = _no_loading_animation

import browsers  # noqa: E402
import models  # noqa: E402
from swarm.context import SUBMIT_RESERVE_S  # noqa: E402
from swarm.page_text import EXTRACT_JS, NEXT_JS, clean  # noqa: E402

log = logging.getLogger("browse")

PAGE_TEXT_LIMIT = 12_000      # characters of one read the model sees (the saved evidence keeps up to 10x more)

SYSTEM_EXTRA = """
Working rules:
- Dismiss cookie walls, surveys and popups (close them; do not follow their links). If a session expired or you see a
  login form again, log in again (credentials are in the task). Retry error pages ("erneut versuchen") a few times.
- Use `read_page` to read a page: it returns ALL text including iframes, collapsed sections, tooltips and canvas labels,
  which your screenshot/DOM view misses. Pass `url` to open a link and read it in one step, or `urls` to read several
  pages you selected (e.g. every tab of a record, several messages) in one step. Pass `next_page_link` with
  the exact text of the paging link (e.g. the "next page" link) to read every page of a paged list at once.
  Pass `topic` (short label) when the page holds facts the analysts need; it is then saved as evidence.
- Use `fill_form` to fill a form in one step (also sliders: set the slider to its maximum; checkboxes: 'true').
- Watch for traps: look-alike or superseded records, decoy links, oldest-first document views, archive folders,
  number formats marked by locale (de-DE 1.234,5 vs en-US 1,234.5), dates in other timezones or MM/DD order.
- Never invent values. Finish with `done` and a short summary when the goal is met or you are stuck."""


class RecordParams(BaseModel):
    kind: str = Field(description="what this fact is, e.g. a label agreed with the analyzers")
    data: dict = Field(description="the facts exactly as shown on the page")
    source: str = Field(default="", description="URL / document / mail subject where it was read")
    note: str = Field(default="", description="caveats: superseded, withdrawn, other timezone, unsure")


class ReadParams(BaseModel):
    url: str = Field(default="", description="optional: open this URL (a link you saw on this site) before reading")
    urls: list[str] = Field(default_factory=list, description="optional: several URLs on this site to open and read "
                                                               "one after another in this one action (e.g. all tabs of "
                                                               "a record, or several messages you selected)")
    topic: str = Field(default="", description="short label to save the text as evidence; empty = just read")
    next_page_link: str = Field(default="", description="optional: exact text of the link to the next page of a "
                                                         "paged list; all pages are read and joined")
    max_pages: int = Field(default=10, description="limit for next_page_link")


class FieldValue(BaseModel):
    field: str = Field(description="the field's label text or its name attribute")
    value: str = Field(description="text to type, option to select (its text or value), or 'true'/'false' for a checkbox")


class FillParams(BaseModel):
    fields: list[FieldValue] = Field(description="fields in the order they must be filled")
    apply_button: str = Field(default="", description="optional: text of a button that must be clicked when the next "
                                                       "field is still disabled or lacks the wanted option (cascading selects)")
    then_click: str = Field(default="", description="optional: text of a button to click after all fields are set")


def allowed_domains(task: dict) -> list[str]:
    """Only the task's websites may be opened (fair play: no other hosts)."""
    out = []
    for site in task.get("access", []):
        parts = urlparse(site.get("url") or "")
        if parts.netloc:
            out.append(f"{parts.scheme}://{parts.netloc}")
    return out


class QwenChat(ChatOpenAI):
    """ChatOpenAI that switches Qwen's separate reasoning pass off (browser-use already asks for its own short
    thinking in the reply), which roughly halves the time per step, and asks for a JSON object reply (otherwise the
    model sometimes answers in prose and the step fails). SWARM_BROWSER_THINKING=1 turns the reasoning pass back on."""

    def get_client(self):
        client = super().get_client()
        if os.environ.get("SWARM_BROWSER_THINKING") == "1" or getattr(client, "_qwen_patched", False):
            return client
        create = client.chat.completions.create

        async def create_without_thinking(*args, **kwargs):
            kwargs["extra_body"] = {**(kwargs.get("extra_body") or {}), **models.NO_THINKING}
            kwargs.setdefault("response_format", {"type": "json_object"})   # without it Qwen sometimes replies in prose
            reply = await create(*args, **kwargs)
            for choice in getattr(reply, "choices", None) or []:   # Qwen sometimes writes a sentence before the JSON
                text = (choice.message.content or "").strip()
                start, end = text.find("{"), text.rfind("}")
                if start > 0 and end > start:
                    choice.message.content = text[start:end + 1]
            return reply

        client.chat.completions.create = create_without_thinking
        client._qwen_patched = True
        return client


def make_llm() -> ChatOpenAI:
    """The hosted Qwen through the proxy. Settings tuned for a non-OpenAI model; adjust here if tool calls misbehave."""
    return QwenChat(model=models.QWEN_MODEL, base_url=os.environ["QWEN_BASE_URL"], api_key=os.environ["QWEN_API_KEY"],
                    temperature=0.0, frequency_penalty=None, timeout=360, max_retries=3,
                    add_schema_to_system_prompt=True, dont_force_structured_output=True,
                    remove_min_items_from_schema=True, remove_defaults_from_schema=True)


class RobustSession(BrowserSession):
    """A BrowserSession whose navigation recovers from a tab stuck while loading: stop the old load and retry once."""

    async def _navigate_and_wait(self, url, target_id, timeout=None, wait_until="load"):
        try:
            return await super()._navigate_and_wait(url, target_id, timeout=timeout, wait_until=wait_until)
        except RuntimeError as error:
            if "timed out" not in str(error):
                raise
            log.warning("navigation stuck, stopping the old load and retrying: %s", url)
            session = await self.get_or_create_cdp_session(target_id, focus=False)
            try:
                await asyncio.wait_for(session.cdp_client.send.Page.stopLoading(session_id=session.session_id), 5)
            except Exception as stop_error:
                log.debug("stopLoading failed: %s", stop_error)
            return await super()._navigate_and_wait(url, target_id, timeout=timeout, wait_until=wait_until)


class TextSession(RobustSession):
    """A BrowserSession that never captures screenshots for the step state (browser-use always asks for one).
    In the sandbox, screenshots of the run's browsers stalled the page-state capture for 30 s per step."""

    async def get_browser_state_summary(self, include_screenshot: bool = True, cached: bool = False,
                                        include_recent_events: bool = False):
        return await super().get_browser_state_summary(include_screenshot=False, cached=cached,
                                                       include_recent_events=include_recent_events)


def make_session(ctx, browser: int = 0, vision: bool = False) -> BrowserSession:
    """Attach to the run's CDP browser `browser` (locally browser-use starts its own Chromium; HEADLESS=1 hides it)."""
    urls = browsers.cdp_urls()
    cls = RobustSession if vision else TextSession
    profile = dict(allowed_domains=allowed_domains(ctx.task), enable_default_extensions=False, keep_alive=True)
    if urls:
        return cls(cdp_url=urls[browser % len(urls)], **profile)
    return cls(headless=os.environ.get("HEADLESS") == "1", window_size={"width": 1280, "height": 800}, **profile)


async def _evaluate(browser_session: BrowserSession, js: str, *args):
    page = await browser_session.must_get_current_page()
    raw = await page.evaluate(js, *args)
    try:
        return json.loads(raw) if isinstance(raw, str) and raw[:1] in ("[", "{") else raw
    except ValueError:
        return raw


async def read_current_page(browser_session: BrowserSession) -> str:
    """Full text of the current page (see swarm.page_text). Retries while the page is still loading."""
    for attempt in range(4):
        try:
            return clean(str(await _evaluate(browser_session, EXTRACT_JS, {})))
        except Exception as error:   # page navigating / not ready yet
            if attempt == 3:
                raise
            log.debug("read retry: %s", error)
            await asyncio.sleep(1.0)
    return ""


def _same_site(url: str, current: str, task: dict) -> str | None:
    full = urljoin(current, url)
    host = f"{urlparse(full).scheme}://{urlparse(full).netloc}"
    return full if host in allowed_domains(task) else None


async def browse(ctx, goal: str, *, browser: int = 0, site_id: str = "", on_record=None, max_steps: int = 40,
                 hints: str = "", author: str = "browse", use_vision=False, stop_reserve: float | None = None,
                 extra_tools=None, max_actions_per_step: int = 4) -> dict:
    """Run a browser-use agent until done / max_steps / out of time. Returns {"summary", "records", "steps", "done"}.

    Screenshots are off by default (`use_vision=False`): capturing them hung in the sandbox browsers, and `read_page`
    already reads canvas labels and hidden text. `stop_reserve`: stop when the run has this many seconds left
    (default: the SUBMIT reserve). `extra_tools(tools)`
    may register more actions. Pages read with a `topic` reach `on_record` as {"kind": "page", "data": {...}}.
    """
    records: list[dict] = []
    reserve = SUBMIT_RESERVE_S if stop_reserve is None else stop_reserve

    async def emit(rec: dict) -> None:
        records.append(rec)
        if on_record:
            await on_record(rec)

    tools = build_tools(ctx, site_id, author, emit)
    if extra_tools:
        extra_tools(tools)

    session = make_session(ctx, browser, vision=bool(use_vision))
    agent = Agent(task=goal, llm=make_llm(), browser_session=session, tools=tools, use_vision=use_vision,
                  use_judge=False, generate_gif=False, max_failures=6, max_actions_per_step=max_actions_per_step,
                  llm_timeout=150, step_timeout=240,
                  extend_system_message=SYSTEM_EXTRA + (f"\n{hints}" if hints else ""))

    async def stop_when_out_of_time(_agent) -> None:
        if ctx.seconds_left() <= reserve:
            agent.stop()

    try:
        history = await agent.run(max_steps=max_steps, on_step_start=stop_when_out_of_time)
    finally:
        await session.kill() if not browsers.cdp_urls() else await session.stop()   # never kill the run's browsers
    return {"summary": history.final_result() or "", "records": records, "steps": len(history.history),
            "done": history.is_done()}


def build_tools(ctx, site_id: str, author: str, emit) -> Tools:
    """Our actions for the browsing model: record, read_page, fill_form. `emit(rec)` receives records and pages."""
    tools = Tools()

    @tools.action("Save a fact you found (kept even if you run out of time).", param_model=RecordParams)
    async def record(params: RecordParams, browser_session: BrowserSession):
        url = await browser_session.get_current_page_url()
        await emit({"kind": params.kind, "data": params.data, "source": params.source or url, "note": params.note,
                    "site": site_id, "author": author})
        return ActionResult(extracted_content=f"recorded {params.kind}")

    async def read_one(target: str | None, params: ReadParams, browser_session: BrowserSession) -> tuple[str, str]:
        if target:
            await browser_session.navigate_to(target)
            await asyncio.sleep(0.8)
        url = await browser_session.get_current_page_url()
        pages = [await read_current_page(browser_session)]
        if params.next_page_link:
            for _ in range(max(0, min(params.max_pages, 20) - 1)):
                if await _evaluate(browser_session, NEXT_JS, params.next_page_link) != "clicked":
                    break
                await asyncio.sleep(1.2)
                text = await read_current_page(browser_session)
                if text == pages[-1]:
                    break
                pages.append(text)
        text = "\n\n----- next page -----\n\n".join(pages)
        if params.topic:
            title = await browser_session.get_current_page_title()
            await emit({"kind": "page", "data": {"topic": params.topic, "url": url, "title": title,
                                                 "pages": len(pages), "text": text[:10 * PAGE_TEXT_LIMIT]},
                        "source": url, "note": "", "site": site_id, "author": author})
        return url, text

    @tools.action("Read the full text of the current page (or of `url` / several `urls`), incl. iframes, collapsed "
                  "sections, tooltips; optionally follow `next_page_link` through all pages; `topic` saves it as "
                  "evidence.", param_model=ReadParams)
    async def read_page(params: ReadParams, browser_session: BrowserSession):
        current = await browser_session.get_current_page_url()
        wanted = [u for u in ([params.url] if params.url else []) + list(params.urls or []) if u][:12]
        targets = []
        for u in wanted:
            target = _same_site(u, current, ctx.task)
            if not target:
                return ActionResult(error=f"{u} is not on this task's websites")
            targets.append(target)
        reads = []
        for target in targets or [None]:
            try:
                reads.append(await read_one(target, params, browser_session))
            except Exception as error:      # one bad page must not lose the others
                reads.append((target or current, f"(could not read: {error})"))
        limit = PAGE_TEXT_LIMIT // len(reads)
        parts, memos = [], []
        for url, text in reads:
            shown = text if len(text) <= limit else \
                text[:limit] + ("\n[... truncated here; the full text is saved]" if params.topic else "\n[... truncated]")
            parts.append(f"=== {url}\n{shown}")
            memos.append(f"{url} ({len(text)} chars)")
        memo = "Read " + ", ".join(memos) + (f"; saved as '{params.topic}'" if params.topic else "")
        return ActionResult(extracted_content=memo + "\n\n" + "\n\n".join(parts), include_extracted_content_only_once=True,
                            long_term_memory=memo[:600])

    @tools.action("Fill several fields of the form on the current page at once (by label or name), handling "
                  "cascading selects via `apply_button`, then optionally click `then_click`.", param_model=FillParams)
    async def fill_form(params: FillParams, browser_session: BrowserSession):
        from swarm.forms import fill_fields
        report = await fill_fields(lambda js, *a: _evaluate(browser_session, js, *a),
                                   [(f.field, f.value) for f in params.fields], params.apply_button, params.then_click)
        return ActionResult(extracted_content=report, include_extracted_content_only_once=True,
                            long_term_memory=report[:400])

    return tools
