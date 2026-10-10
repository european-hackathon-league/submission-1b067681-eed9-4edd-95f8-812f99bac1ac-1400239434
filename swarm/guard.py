"""Popup guard: keeps the page clear of noise while a browsing agent works, so its steps and context go to the data.

Every browse() runs one guard next to its agent: a background sweep every GUARD_INTERVAL_S plus one sweep right before
each agent step (the agent then sees the cleaned page). A sweep finds what covers or replaces the page content (open
dialogs, modal / fixed overlays, consent banners, and short pages such as error pages with a retry button) and asks the
model once per kind of item what to do with it:

- click: noise with a harmless button (cookie banner, survey, maintenance notice, error page "retry") -> click it;
- fill: an access check before the content (robot checkbox, slider, copy a displayed code) -> fill it, click continue;
- leave: anything an agent works with (login, search help, document viewer, data dialog, the bid wizard).

The model's decision is cached per site and item signature (its text without digits + its buttons), shared by all
agents of the run: a maintenance dialog that comes back on every page of a site costs one model call per run, and every
other tab clears it without one. Fill decisions are not cached (a displayed code changes every time). `careful=True`
(the bid submitter) only allows clicks on noise without input fields: never fills, never touches a form.
"""
import asyncio
import json
import logging
import re
from urllib.parse import urlparse

log = logging.getLogger("guard")

GUARD_INTERVAL_S = 2.0
MAX_ACTIONS_PER_ITEM = 4        # per browse: an item that keeps coming back after this many tries is left to the agent
CLASSIFY_TIMEOUT_S = 45
EVAL_TIMEOUT_S = 10            # a page script that does not answer (page frozen by a native dialog) is given up

# Runs in the page: () => {url, items: [{id, kind, text, buttons: [...], fields: [...]}]}. Element references stay in
# window.__swarmGuard for the click op. Looks into same-origin iframes and open shadow roots.
SCAN_JS = r"""() => {
  const norm = s => (s || '').replace(/[​-‍⁠﻿]/g, '').replace(/\s+/g, ' ').trim();
  const docs = [document];
  for (let i = 0; i < docs.length && i < 60; i++) {
    const d = docs[i];
    for (const f of d.querySelectorAll('iframe')) { try { if (f.contentDocument) docs.push(f.contentDocument); } catch (e) {} }
    for (const el of d.querySelectorAll('*')) { if (el.shadowRoot) docs.push(el.shadowRoot); }
  }
  const W = window.innerWidth || 1280, H = window.innerHeight || 800;
  const styleOf = el => { try { return (el.ownerDocument.defaultView || window).getComputedStyle(el); } catch (e) { return null; } };
  const shown = el => {
    const s = styleOf(el); if (!s) return false;
    const r = el.getBoundingClientRect();
    return r.width >= 40 && r.height >= 16 && s.display !== 'none' && s.visibility !== 'hidden' && parseFloat(s.opacity || '1') > 0.05;
  };
  const ours = el => /^(browser-use|playwright)/i.test(el.id || '') || /browser-use|playwright-highlight/i.test(el.className || '');
  const cands = [];
  for (const d of docs) {
    for (const el of d.querySelectorAll('*')) {
      if (ours(el)) continue;
      const role = (el.getAttribute('role') || '').toLowerCase();
      let dialog = (el.tagName === 'DIALOG' && el.open) || role === 'dialog' || role === 'alertdialog' || el.getAttribute('aria-modal') === 'true';
      let overlay = false;
      if (!dialog) {
        const s = styleOf(el);
        if (!s || (s.position !== 'fixed' && s.position !== 'sticky')) continue;
        const r = el.getBoundingClientRect();
        const area = Math.max(0, Math.min(r.right, W) - Math.max(r.left, 0)) * Math.max(0, Math.min(r.bottom, H) - Math.max(r.top, 0));
        const z = parseInt(s.zIndex) || 0;
        const hasButton = !!el.querySelector('button, [role=button], input[type=button], input[type=submit]');
        overlay = area >= 0.25 * W * H || (hasButton && z >= 1 && area >= 0.03 * W * H);
        if (!overlay) continue;
      }
      if (!shown(el)) continue;
      cands.push(el);
    }
  }
  const outer = cands.filter(el => !cands.some(o => o !== el && o.contains(el)));
  const btnSel = 'button, [role=button], input[type=button], input[type=submit], a';
  const labelOf = b => norm(b.innerText || b.value || b.getAttribute('aria-label') || b.title || '').slice(0, 60);
  // a link that opens another page (other path or host): decoys in popups ("accept and continue" -> elsewhere)
  const away = b => {
    if (b.tagName !== 'A') return false;
    const href = b.getAttribute('href') || '';
    if (!href || /^(#|javascript:)/i.test(href)) return false;
    try { const to = new URL(href, location.href); return to.origin !== location.origin || to.pathname !== location.pathname; }
    catch (e) { return true; }
  };
  const fieldLabel = f => norm((f.labels && f.labels[0] && f.labels[0].innerText) || f.getAttribute('aria-label') || f.placeholder || '').slice(0, 60);
  const items = [], refs = [], btnRefs = [];
  const describe = (el, kind, textOverride) => {
    let buttons = [...el.querySelectorAll(btnSel)];
    if (el.shadowRoot) buttons = buttons.concat([...el.shadowRoot.querySelectorAll(btnSel)]);
    buttons = buttons.filter(b => shown(b) || b.getBoundingClientRect().width > 0).slice(0, 14);
    const fields = [...el.querySelectorAll('input, select, textarea')]
      .filter(f => f.type !== 'hidden' && (shown(f) || f.type === 'checkbox' || f.type === 'range')).slice(0, 10)
      .map(f => ({type: (f.type || f.tagName).toLowerCase(), name: f.name || f.id || '', label: fieldLabel(f)}));
    let text = textOverride !== undefined ? textOverride : norm(el.innerText || el.textContent || '');
    for (const c of el.querySelectorAll('canvas, [role=img][aria-label]')) text += ' [' + norm(c.getAttribute('aria-label') || c.textContent) + ']';
    if (text.length < 3 && !buttons.length) return;
    refs.push(el); btnRefs.push(buttons);
    items.push({id: items.length, kind, text: text.slice(0, 1200),
                buttons: buttons.map((b, i) => ({i, label: labelOf(b), away: away(b)})),
                fields});
  };
  outer.slice(0, 6).forEach(el => describe(el, 'overlay'));
  // inline access gates (a slider to drag to the end, mostly with a robot checkbox) that lock part of a page: described
  // by their controls and status only (the content they lock changes on every page); skipped once unlocked
  for (const d of docs) {
    for (const r of d.querySelectorAll('input[type=range], [role=slider]')) {
      if (refs.some(o => o.contains(r))) continue;
      let box = r.parentElement;
      while (box && box.parentElement && !box.querySelector('input[type=checkbox], [role=checkbox]')) box = box.parentElement;
      if (!box || refs.includes(box)) continue;
      const checks = [...box.querySelectorAll('input[type=checkbox]')];
      const max = r.max !== undefined && r.max !== '' ? Number(r.max) : Number(r.getAttribute('aria-valuemax') || 100);
      const value = r.value !== undefined && r.value !== '' ? Number(r.value) : Number(r.getAttribute('aria-valuenow') || 0);
      if (value >= max && checks.every(c => c.checked)) continue;
      const parts = [box.getAttribute('aria-label') || ''];
      for (const f of box.querySelectorAll('input, [role=slider], [role=checkbox]')) if (f.type !== 'hidden') parts.push(fieldLabel(f));
      for (const st of box.querySelectorAll('[role=status], canvas, [role=img][aria-label]')) parts.push('[' + norm(st.getAttribute('aria-label') || st.innerText || '') + ']');
      const whole = norm(box.innerText || '');
      describe(box, 'gate', whole.length < 500 ? undefined : norm(parts.filter(Boolean).join(' | ')));
    }
  }
  if (!items.length && document.body) {
    // a short page (error page with a retry link, notice page): its whole text, shadow roots included, is short
    let size = (document.body.innerText || '').length;
    for (const d of docs.slice(1)) size += ((d.host ? d : d.body || d).textContent || '').length;
    const text = norm(document.body.innerText || '');
    const hasPassword = docs.some(d => d.querySelector('input[type=password]'));
    if (text.length > 0 && size < 900 && !hasPassword && document.body.querySelector(btnSel)) describe(document.body, 'page');
  }
  window.__swarmGuard = {refs, btnRefs};
  return {url: location.href, items};
}"""

# Runs in the page: ({id, button}) => "clicked" | "gone" | "link". Never follows a link to another page.
CLICK_JS = r"""(a) => {
  const g = window.__swarmGuard;
  const b = g && g.btnRefs[a.id] && g.btnRefs[a.id][a.button];
  if (!b || !b.isConnected) return 'gone';
  if (b.tagName === 'A') {
    const href = b.getAttribute('href') || '';
    if (href && !/^(#|javascript:)/i.test(href)) {
      const to = new URL(href, location.href);
      if (to.origin !== location.origin || to.pathname !== location.pathname) return 'link';
    }
  }
  b.click();
  return 'clicked';
}"""

SYSTEM = """You keep web pages clear for agents that collect business data. For each item (dialog, overlay, banner, or a
short page) decide:
- "click": noise blocking the work: cookie/consent banner (button that accepts necessary/all cookies, never a settings
  link), survey, newsletter, chat or ad popup (its x/close/"no thanks" button), maintenance or system notice that only
  needs acknowledging (prefer its x/close button: "accept and continue" links often lead to another page and lose the
  agent's work), download/info bars (x), transient error page or message with a retry button or link (retry).
- "fill": access check in front of the content: robot checkbox, slider to move to the end, a displayed code/number to
  copy, an acknowledgement checkbox. Give fields [{"field": label or name, "value": ...}]: checkbox "true", slider
  "max", code copied exactly from the item text; then_click = text of its confirm/continue button.
- "leave": everything an agent works with or decides itself: login forms, search forms and search help, result lists,
  records, document viewers, forms asking for a purpose or choice, registration/interest forms, bid / offer / signing /
  TAN / submit / confirm / send / delete dialogs, page navigation, headers, menus.
Never choose a button marked "away": true (a link to another page; decoys) unless it is the retry of an error page.
Unsure -> "leave".
Reply JSON: {"items": [{"id": 0, "kind": "cookie|survey|notice|banner|error|gate|content|other", "action": "click|fill|leave",
"button": <button index for click>, "fields": [], "then_click": ""}]}"""

_DECISIONS: dict[tuple[str, str], dict] = {}     # (site host, item signature) -> cached click/leave decision
_PENDING: dict[tuple[str, str], bool] = {}         # item kinds the model is classifying right now
_TASKS: set = set()


def signature(item: dict) -> str:
    """Item identity across pages and tabs: its text without digits (codes, dates, counters change) and its buttons."""
    text = re.sub(r"\d+", "#", item.get("text", "").lower())
    text = re.sub(r"\s+", " ", text)[:1200]
    buttons = "|".join(sorted(re.sub(r"\d+", "#", b.get("label", "").lower()) for b in item.get("buttons") or []))
    return f"{item.get('kind')}:{text}:{buttons}:{len(item.get('fields') or [])}"


def cacheable_fill(decision: dict) -> bool:
    """A fill that only ticks boxes and moves sliders is the same every time (cache it); one that copies a displayed
    code is not."""
    values = [str(f.get("value", "")).strip().lower() for f in decision.get("fields") or [] if isinstance(f, dict)]
    return bool(values) and all(v in ("true", "false", "max", "100") for v in values)


def host(url: str) -> str:
    return urlparse(url or "").netloc


NOISE = ("cookie", "survey", "notice", "banner", "error")


def pick_button(decision: dict, item: dict) -> int | None:
    """The button to click: the model's choice, unless it is a link to another page (a decoy) and the item has exactly
    one other button that stays on the page (its x/close): then that one. Retry links of error pages may lead away."""
    index = decision.get("button")
    buttons = item.get("buttons") or []
    if not isinstance(index, int) or not 0 <= index < len(buttons):
        return None
    if buttons[index].get("away") and decision.get("kind") != "error":
        stay = [b["i"] for b in buttons if not b.get("away")]
        return stay[0] if len(stay) == 1 else None
    return index


def allowed(decision: dict, item: dict, careful: bool) -> bool:
    """What the guard may do with `item`: in careful mode only clicks on noise that has no input fields."""
    action = decision.get("action")
    if action == "click":
        if pick_button(decision, item) is None:
            return False
        return not careful or (decision.get("kind") in NOISE and not item.get("fields"))
    if action == "fill":
        return not careful and bool(decision.get("fields") or decision.get("then_click"))
    return False


class PopupGuard:
    """One per browse(): `sweep(evaluate)` clears the page once; `loop(evaluate)` sweeps until cancelled.
    `evaluate(js, *args)` runs a JS arrow function in the agent's current page."""

    def __init__(self, ctx, author: str, careful: bool = False, fill=None):
        """`fill(pairs, then_click)` sets a gate's fields (default: swarm.forms.fill_fields in the page)."""
        self.ctx, self.author, self.careful = ctx, author, careful
        self._fill = fill
        self._fresh: dict = {}             # decisions that are not cached (fills copying a code): used once
        self._lock = asyncio.Lock()
        self._tries: dict[str, int] = {}
        self.actions = 0

    async def fill(self, evaluate, pairs: list[tuple[str, str]], then_click: str) -> str:
        if self._fill is not None:
            return await self._fill(pairs, then_click)
        from swarm.forms import fill_fields
        return await fill_fields(evaluate, pairs, "", then_click)

    def _decide(self, site: str, items: list[dict]) -> dict[int, dict]:
        """Decisions for the scanned items that are known now (cached, or just classified for this guard). Unknown
        items are sent to the model in the background (one call per new item kind across all agents) and handled by
        a later sweep: a sweep never waits for the model, so the agent's step is never held up by it."""
        out, unknown = {}, []
        for item in items:
            key = (site, signature(item))
            if key in _DECISIONS:
                out[item["id"]] = _DECISIONS[key]
            elif key in self._fresh:
                out[item["id"]] = self._fresh.pop(key)
            elif key not in _PENDING:
                unknown.append((key, item))
        if unknown:
            for key, _ in unknown:
                _PENDING[key] = True
            task = asyncio.create_task(self._classify(site, unknown))
            _TASKS.add(task)                    # finishes even after this browse ended: other agents use the answer
            task.add_done_callback(_TASKS.discard)
        return out

    async def _classify(self, site: str, unknown: list[tuple]) -> None:
        try:
            shown = [{k: item[k] for k in ("id", "kind", "text", "buttons", "fields")} for _, item in unknown]
            reply = await asyncio.wait_for(
                self.ctx.llm.ask_json(SYSTEM, json.dumps({"items": shown}, ensure_ascii=False)), CLASSIFY_TIMEOUT_S)
            by_id = {d.get("id"): d for d in (reply or {}).get("items") or [] if isinstance(d, dict)}
            for key, item in unknown:
                decision = by_id.get(item["id"]) or {"action": "leave", "kind": "other"}
                if decision.get("action") != "fill" or cacheable_fill(decision):
                    _DECISIONS[key] = decision      # a fill that copies a displayed code: asked again each time
                else:
                    self._fresh[key] = decision
                log.info("guard %s: %s %s -> %s %s", site, item["kind"], item["text"][:80], decision.get("action"),
                         decision.get("kind"))
        except Exception as error:              # model slow or failing: the agent handles these items itself
            log.debug("guard classification failed: %s", error)
        finally:
            for key, _ in unknown:
                _PENDING.pop(key, None)


    async def sweep(self, evaluate) -> int:
        """Clear the current page once; returns how many items were handled."""
        if self._lock.locked():                 # a sweep is running: wait for it instead of starting another
            async with self._lock:
                return 0
        async with self._lock:
            try:
                scan = await asyncio.wait_for(evaluate(SCAN_JS), EVAL_TIMEOUT_S)
            except Exception:                   # page navigating / closed / frozen by a native dialog
                return 0
            items = (scan or {}).get("items") or [] if isinstance(scan, dict) else []
            if not items:
                return 0
            site = host(scan.get("url", ""))
            decisions = self._decide(site, items)
            handled = 0
            for item in items:
                decision = decisions.get(item["id"]) or {}
                sig = signature(item)
                if not allowed(decision, item, self.careful) or self._tries.get(sig, 0) >= MAX_ACTIONS_PER_ITEM:
                    continue
                self._tries[sig] = self._tries.get(sig, 0) + 1
                try:
                    if decision["action"] == "click":
                        button = pick_button(decision, item)
                        result = await asyncio.wait_for(evaluate(CLICK_JS, {"id": item["id"], "button": button}),
                                                        EVAL_TIMEOUT_S)
                        label = item["buttons"][button]["label"]
                    else:
                        pairs = [(str(f.get("field", "")), str(f.get("value", ""))) for f in decision.get("fields") or []
                                 if isinstance(f, dict)]
                        result = await asyncio.wait_for(self.fill(evaluate, pairs, str(decision.get("then_click") or "")),
                                                        3 * EVAL_TIMEOUT_S)
                        label = decision.get("then_click") or "fields"
                except Exception as error:      # the item went away meanwhile
                    log.debug("guard action failed: %s", error)
                    continue
                handled += 1
                self.actions += 1
                log.info("guard %s [%s]: %s '%s' on %s: %s", site, self.author, decision["action"], label,
                         decision.get("kind"), str(result)[:120].replace("\n", "; "))
                await asyncio.sleep(0.6)        # let the page react before the agent reads it
            return handled

    async def loop(self, evaluate, interval: float = GUARD_INTERVAL_S) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await self.sweep(evaluate)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                log.debug("guard sweep failed: %s", error)
