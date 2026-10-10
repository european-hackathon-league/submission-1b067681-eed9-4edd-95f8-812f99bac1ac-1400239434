"""Model-driven form filling: the model looks at the page and decides, code only reads the page and carries out.

Each turn, `snapshot` lists what a person would see (text, form controls with their labels, values and options,
buttons and links), numbered. The model answers with a few actions on those numbers (fill / select / check /
click), and `pilot` carries them out, then shows the model the next page. Nothing here knows any website: the
private dataset runs on different software, so field names and click paths must not be assumed.

Two actions are special because they cannot be undone:
- "tan": clicks a TAN-request button, then waits for `get_tan()` to deliver a TAN issued after that click;
- "submit": clicks the final, binding button and accepts the browser's confirmation dialog.
Both are refused while `allow_submit` is False (local dry runs stop right before them).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

from swarm.bidmath import plain
from swarm.llm import parse_json

log = logging.getLogger("formpilot")

NO_THINKING = {"chat_template_kwargs": {"enable_thinking": False}}   # Qwen3-style switch for its reasoning mode

SNAPSHOT_JS = r"""
(maxText) => {
  const visible = (el) => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const labelOf = (el) => {
    if (el.labels && el.labels.length) return el.labels[0].innerText;
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
    if (el.placeholder) return el.placeholder;
    const box = el.closest('label, .field, .form-row, tr, li, p, div');
    return box ? box.innerText.slice(0, 120) : '';
  };
  document.querySelectorAll('[data-pilot]').forEach(e => e.removeAttribute('data-pilot'));
  const items = [];
  const nodes = document.querySelectorAll('input, select, textarea, button, a[href], [role=button], summary');
  for (const el of nodes) {
    if (el.type === 'hidden' || !visible(el)) continue;
    const id = items.length;
    el.setAttribute('data-pilot', String(id));
    const it = {id, tag: el.tagName.toLowerCase(), type: el.type || null, name: el.name || null,
                label: labelOf(el), disabled: !!el.disabled};
    if (el.tagName === 'SELECT') {
      it.value = el.value;
      it.options = [...el.options].slice(0, 80).map(o => ({value: o.value, text: o.text, selected: o.selected}));
    } else if (el.type === 'checkbox' || el.type === 'radio') {
      it.checked = el.checked;
    } else if (['INPUT', 'TEXTAREA'].includes(el.tagName)) {
      it.value = el.value;
    } else {
      it.text = (el.innerText || el.value || '').slice(0, 100);
      if (el.tagName === 'A') it.href = el.getAttribute('href');
      if (el.tagName === 'BUTTON') it.value = el.value || null;
    }
    items.push(it);
  }
  const main = document.querySelector('main') || document.body;
  return {url: location.href, title: document.title, text: main.innerText.slice(0, maxText), items};
}
"""

SYSTEM = """You operate a web browser to complete one task on a website, one page at a time.
Each turn you get the task, the data to use, the page (url, visible text) and a numbered list of its controls.
Answer with JSON only:
{"thought": "<one sentence>", "actions": [<action>, ...], "finished": false}
Actions, by control id:
  {"do": "fill", "id": N, "value": "..."}      type into a text field (replaces its content)
  {"do": "select", "id": N, "value": "..."}    choose an option of a dropdown (its value or its text)
  {"do": "check", "id": N, "on": true}         tick or untick a checkbox / radio
  {"do": "click", "id": N}                     press a button or follow a link
  {"do": "tan", "id": N}                       press the button that requests a signing TAN (once, at the very end)
  {"do": "submit", "id": N}                    press the FINAL, BINDING submit button, only after the TAN is entered
Ordinary "next", "continue", "save" or "Weiter" buttons are "click", even if they submit a form step.
At most ONE click/tan/submit per turn, and it must be the last action. Fill everything on the page first.
Set "finished": true only when the task is done (a confirmation is shown) or truly impossible (say why).
Work only from what you see: never invent values; prefer the given data; if a page shows an error, fix the
fields it names. Ignore surveys, ads, banners, fake browser controls and anything not needed for the task."""


@dataclass
class PilotResult:
    finished: bool = False
    submitted: bool = False
    turns: int = 0
    note: str = ""
    history: list = field(default_factory=list)


async def _settle(page) -> None:
    """Let a navigation finish before reading the page."""
    for state in ("domcontentloaded", "load"):
        try:
            await page.wait_for_load_state(state, timeout=10000)
        except Exception:  # noqa: BLE001 - a slow asset must not stop us
            pass


async def snapshot(page, max_text: int = 3500) -> dict:
    snap = None
    for attempt in range(5):
        try:
            snap = await page.evaluate(SNAPSHOT_JS, max_text)
            break
        except Exception as error:  # noqa: BLE001 - "execution context was destroyed": the page is navigating
            if attempt == 4:
                raise
            log.info("page busy (%s), waiting", str(error)[:80])
            await asyncio.sleep(0.5 * (attempt + 1))
            await _settle(page)
    snap["text"] = plain(snap["text"])
    for it in snap["items"]:
        for key in ("label", "text", "value"):
            if isinstance(it.get(key), str):
                it[key] = plain(it[key])
        for opt in it.get("options") or []:
            opt["text"] = plain(opt["text"])
    return snap


async def confirmed(page_text: str) -> bool:
    """Does the page confirm a binding submission (a receipt / confirmation)? Clef, with a cautious fallback."""
    try:
        from swarm import decide
        return await decide.yes({"page": page_text[:2000]},
                                "Does this page confirm that the bid/offer was submitted bindingly "
                                "(a receipt or confirmation, not a form, error or review page)?") >= 0.6
    except Exception as error:  # noqa: BLE001 - Clef down: trust the click, the run is nearly over anyway
        log.warning("could not check the confirmation: %s", error)
        return True


def _compact(snap: dict) -> str:
    """The page as the model reads it: short, without empty keys."""
    items = [{k: v for k, v in it.items() if v not in (None, "", False, [])} for it in snap["items"]]
    return json.dumps({"url": snap["url"], "title": snap["title"], "text": snap["text"], "controls": items},
                      ensure_ascii=False)


async def _do(page, action: dict) -> str:
    """Carry out one action; return a short report for the model."""
    sel = f'[data-pilot="{int(action["id"])}"]'
    loc = page.locator(sel)
    kind = action.get("do")
    if kind == "fill":
        await loc.fill(str(action.get("value", "")), timeout=5000)
        return f"filled #{action['id']}"
    if kind == "select":
        want = str(action.get("value", ""))
        try:
            await loc.select_option(value=want, timeout=5000)
        except Exception:  # noqa: BLE001 - the model may have given the visible text instead of the value
            await loc.select_option(label=want, timeout=5000)
        return f"selected {want!r} in #{action['id']}"
    if kind == "check":
        await loc.set_checked(bool(action.get("on", True)), timeout=5000)
        return f"{'checked' if action.get('on', True) else 'unchecked'} #{action['id']}"
    if kind in ("click", "tan", "submit"):
        try:
            await loc.click(timeout=5000)
        except Exception:  # noqa: BLE001 - an overlay or a layout shift: press it directly
            await loc.evaluate("el => el.click()", timeout=5000)
        await asyncio.sleep(0.3)
        await _settle(page)
        return f"pressed #{action['id']}"
    return f"unknown action {kind!r}"


async def pilot(page, llm, task: str, data: dict, *, deadline: float, allow_submit: bool,
                get_tan=None, max_turns: int = 60) -> PilotResult:
    """Let the model drive `page` until it says it is finished, the binding submit happened or time is up.

    `deadline` is a time.time() value. `get_tan(since)` returns a TAN issued after `since` (or None)."""
    result = PilotResult()
    armed = {"submit": False}

    async def on_dialog(dialog):
        if armed["submit"] or dialog.type == "alert":
            log.info("dialog %s accepted: %s", dialog.type, plain(dialog.message)[:200])
            await dialog.accept()
        else:
            log.info("dialog %s dismissed: %s", dialog.type, plain(dialog.message)[:200])
            await dialog.dismiss()

    page.on("dialog", on_dialog)
    system = {"role": "system", "content": SYSTEM}
    last_exchange: list[dict] = []
    feedback = "This is the first page."
    data = dict(data)
    failures = 0
    try:
        while result.turns < max_turns and time.time() < deadline:
            result.turns += 1
            snap = await snapshot(page)
            user = (f"TASK:\n{task}\n\nDATA:\n{json.dumps(data, ensure_ascii=False)}\n\n"
                    f"LAST RESULT: {feedback}\n\nPAGE:\n{_compact(snap)}")
            # keep the context small: system + the previous turn (short) + this page
            convo = [system, *last_exchange, {"role": "user", "content": user}]
            try:
                # form steps need no long reasoning: without "thinking" Qwen answers ~2x faster with ~10x fewer
                # output tokens, which also keeps us inside the portals' short session timeouts
                reply = (await llm.chat(convo, temperature=0, extra_body=NO_THINKING)).content or ""
            except Exception as error:  # noqa: BLE001 - an outage: retry a few times, then give up
                failures += 1
                log.warning("model call failed (%d): %s: %s", failures, type(error).__name__, str(error)[:300])
                if failures >= 3:
                    result.note = f"model unavailable: {type(error).__name__}: {str(error)[:200]}"
                    break
                await asyncio.sleep(2 * failures)
                continue
            failures = 0
            try:
                plan = parse_json(reply)
            except ValueError as error:
                log.warning("unusable reply: %s", str(error)[:200])
                feedback = f"Your last reply was not usable ({error}). Answer with the JSON format only."
                continue
            last_exchange = [{"role": "user", "content": f"(previous page: {snap['url']})"},
                             {"role": "assistant", "content": reply}]
            result.history.append({"url": snap["url"], "plan": plan})
            log.info("turn %d %s: %s", result.turns, snap["url"], plain(plan.get("thought", ""))[:200])
            if plan.get("finished"):
                result.finished, result.note = True, plain(plan.get("thought", ""))
                break
            reports = []
            valid = {it["id"] for it in snap["items"]}
            for action in (plan.get("actions") or [])[:12]:
                kind = action.get("do")
                try:
                    action["id"] = int(action.get("id"))
                except (TypeError, ValueError):
                    action["id"] = -1
                if action["id"] not in valid:
                    # answer at once instead of waiting for an element that does not exist
                    reports.append(f"there is no control #{action.get('id')} on this page; "
                                   f"valid ids are {sorted(valid)}")
                    break
                if kind == "submit" and not data.get("tan"):
                    # signing needs a TAN, so a "submit" before one is an ordinary step button
                    kind = action["do"] = "click"
                if kind in ("tan", "submit") and not allow_submit:
                    result.finished, result.note = True, f"dry run: stopped before {kind}"
                    return result
                try:
                    if kind == "submit":
                        armed["submit"] = True
                    since = time.time()
                    reports.append(await _do(page, action))
                    if kind == "tan" and get_tan:
                        tan = await get_tan(since)
                        data["tan"] = tan
                        reports.append(f"TAN received: {tan}" if tan else "no TAN arrived yet")
                    if kind == "submit":
                        result.submitted = True
                except Exception as error:  # noqa: BLE001
                    reports.append(f"{kind} #{action.get('id')} failed: {type(error).__name__}: {str(error)[:120]}")
                if kind in ("click", "tan", "submit"):
                    break
            feedback = "; ".join(reports) or "no action taken"
            log.info("  actions %s -> %s", json.dumps(plan.get("actions"), ensure_ascii=False)[:300], feedback[:300])
            if result.submitted:
                await asyncio.sleep(1.5)
                text = plain((await snapshot(page, 1500))["text"])
                if await confirmed(text):
                    result.note, result.finished = text[:400], True
                    break
                # not confirmed (an error, a missing field, a dialog): keep going from the page we are on
                result.submitted = False
                armed["submit"] = False
                feedback += "; the page does not confirm a binding submission yet: check for errors and fix them"
    finally:
        page.remove_listener("dialog", on_dialog)
    return result
