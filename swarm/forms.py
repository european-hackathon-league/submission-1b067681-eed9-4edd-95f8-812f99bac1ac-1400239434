"""Form-filling helper: the model says WHICH field gets WHICH value; this sets them in order on the current page.

Handles cascading selects (a field stays disabled or lacks the wanted option until an "apply" button reloads the form)
by clicking the button the model named and trying again. Works on the top document and same-origin iframes.
`evaluate(js, *args)` runs a JS arrow function in the page and returns its (JSON-decoded) result.
"""
import asyncio

_FIND = r"""
  const norm = s => (s || '').replace(/[​-‍⁠﻿]/g, '').replace(/\s+/g, ' ').trim().toLowerCase();
  const docs = [document];          // the page, same-origin iframes and open shadow roots (web-component forms)
  for (let i = 0; i < docs.length && i < 200; i++) {
    for (const f of docs[i].querySelectorAll('iframe')) { try { if (f.contentDocument) docs.push(f.contentDocument); } catch (e) {} }
    for (const el of docs[i].querySelectorAll('*')) { if (el.shadowRoot) docs.push(el.shadowRoot); }
  }
  const visible = el => {
    const s = el.ownerDocument.defaultView.getComputedStyle(el);
    return !el.closest('[hidden],[aria-hidden=true]') && !(el.closest('dialog') && !el.closest('dialog').open)
      && s.display !== 'none' && s.visibility !== 'hidden' && el.getClientRects().length > 0;
  };
"""

SET_JS = r"""(args) => {
  const [want, value, verify] = args;
""" + _FIND + r"""
  const labelOf = c => {
    let t = '';
    const doc = c.getRootNode();
    if (c.id) { const l = doc.querySelector(`label[for="${CSS.escape(c.id)}"]`); if (l) t = l.textContent; }
    if (!t && c.closest('label')) {
      const clone = c.closest('label').cloneNode(true);
      clone.querySelectorAll('select,input,textarea').forEach(x => x.remove());
      t = clone.textContent;
    }
    return t || c.getAttribute('aria-label') || c.placeholder || '';
  };
  const controls = docs.flatMap(d => [...d.querySelectorAll('input,select,textarea,[contenteditable=true]')])
    .filter(c => c.type !== 'hidden' && visible(c));
  const w = norm(want);
  const choices = [controls.filter(c => norm(c.name) === w), controls.filter(c => norm(labelOf(c)) === w),
    controls.filter(c => w && norm(labelOf(c)).startsWith(w)), controls.filter(c => w && norm(labelOf(c)).includes(w))]
    .find(cs => cs.length) || [];
  if (choices.length > 1 && !choices.every(c => c.type === 'radio' && c.name === choices[0].name))
    return {status:'ambiguous', fields:choices.map(c => labelOf(c) || c.name)};
  const c = choices[0];
  if (!c) {
    const outputs = docs.flatMap(d => [...d.querySelectorAll('output,[role=status],[role=note]')]).filter(visible);
    if (w && outputs.some(e => norm(e.textContent) === w || norm(e.getAttribute('aria-label')).includes(w)))
      return {status:'read-only', message:'This is output text, not an editable field or checkbox. Inspect the actual fields and validation errors.'};
    // no input with that label: a custom control (toggle/switch/checkbox built from elements, often in shadow DOM)
    // whose own text names it. "true"/"false": click it when its state differs; anything else: click it once.
    const tsel = '[role=checkbox],[role=switch],[role=radio],[role=option],[role=button],[aria-pressed],[aria-checked],label,button';
    const textOf = e => norm(e.getAttribute('aria-label') || e.innerText || e.textContent);
    const toggles = docs.flatMap(d => [...d.querySelectorAll(tsel)]).filter(visible).filter(e => textOf(e));
    const near = toggles.filter(e => textOf(e) === w || textOf(e).startsWith(w) || (w && textOf(e).includes(w) && textOf(e).length < w.length + 60));
    near.sort((a, b) => textOf(a).length - textOf(b).length);       // the tightest element that carries the text
    const t = near[0];
    if (t) {
      const isOn = e => ['true', 'mixed'].includes(e.getAttribute('aria-checked')) || e.getAttribute('aria-pressed') === 'true'
        || (e.querySelector && !!e.querySelector('input[type=checkbox]:checked'))
        || /(^|\s)(on|active|checked|selected|is-checked)(\s|$)/i.test(typeof e.className === 'string' ? e.className : '');
      const bool = /^(true|1|yes|ja|x|on|checked|false|0|no|nein|off)$/i.test(String(value).trim());
      const want = /^(true|1|yes|ja|x|on|checked)$/i.test(String(value).trim());
      if (!verify && (!bool || isOn(t) !== want)) t.click();
      const readable = t.hasAttribute('aria-checked') || t.hasAttribute('aria-pressed') || !!t.querySelector('input[type=checkbox]');
      return {status: bool && readable && isOn(t) === want ? 'ok' : 'unverified', name: textOf(t).slice(0, 60),
              set: 'clicked; inspect updated state before continuing'};
    }
    return {status: 'missing', fields: controls.map(c => labelOf(c).trim() || c.name).filter(Boolean).slice(0, 30)};
  }
  if (c.disabled) return {status: 'disabled', name: c.name};
  if (c.readOnly) return {status: 'read-only', name: c.name};
  const v = norm(value);
  const num = s => { const n = parseFloat(String(s).replace(/[^0-9.,-]/g, '').replace(',', '.')); return isNaN(n) ? null : n; };
  // the prototype's setter: frameworks that track the value property (React, Lit) see the change; a plain
  // assignment to c.value is overwritten by them on the next render
  const setValue = (el, v) => {
    const win = (el.ownerDocument && el.ownerDocument.defaultView) || window;
    const proto = el.tagName === 'TEXTAREA' ? win.HTMLTextAreaElement.prototype : win.HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value');
    if (setter && setter.set) setter.set.call(el, v); else el.value = v;
  };
  if (c.tagName === 'SELECT') {
    const opts = [...c.options];
    const o = opts.find(o => norm(o.value) === v) || opts.find(o => norm(o.text) === v)
      || opts.find(o => norm(o.text).startsWith(v + ' ')) || opts.find(o => num(o.text) !== null && num(o.text) === num(value) && /^[0-9.,\s-]+$/.test(value));
    if (!o || (!o.value && v)) return {status: 'no-option', name: c.name, options: opts.map(o => o.text.trim()).slice(0, 60)};
    if (verify) return {status: c.value === o.value ? "ok" : "changed"};
    c.value = o.value;
  } else if (c.type === 'radio') {
    const group = controls.filter(r => r.type === 'radio' && r.name === c.name);
    const r = group.find(r => norm(r.value) === v) || group.find(r => norm(labelOf(r)) === v);
    if (!r) return {status: 'no-option', options: group.map(r => labelOf(r) || r.value)};
    if (!verify && !r.checked) r.click();
    return {status: r.checked ? 'ok' : 'unverified', name: r.name, set: labelOf(r).trim()};
  } else if (c.type === 'checkbox') {
    const checked = /^(true|1|yes|ja|x|on|checked)$/i.test(String(value).trim());
    if (!verify && c.checked !== checked) c.click();
    if (c.checked !== checked) return {status: 'unverified', name: c.name};
  } else if (c.isContentEditable) {
    if (verify) return {status: c.textContent === value ? "ok" : "changed"};
    c.focus(); c.textContent = value;
  } else if (c.type === 'range') {     // 'max' -> its maximum
    const expected = num(value) === null ? (c.max || '100') : String(num(value));
    if (verify) return {status: c.value === expected ? 'ok' : 'changed'};
    c.focus(); setValue(c, expected);
  } else {
    if (verify) return {status: c.value === value ? "ok" : "changed"};
    c.focus(); setValue(c, value);
  }
  if (verify) return {status: 'ok'};
  c.dispatchEvent(new Event('input', {bubbles: true}));
  c.dispatchEvent(new Event('change', {bubbles: true}));
  return {status: 'ok', name: c.name};
}"""

CLICK_JS = r"""(label) => {
""" + _FIND + r"""
  const want = norm(label);
  for (const doc of docs) {
    const els = [...doc.querySelectorAll('button, input[type=submit], input[type=button], a[href]')].filter(visible);
    const el = els.find(e => norm(e.textContent || e.value) === want) || els.find(e => norm(e.getAttribute('aria-label')) === want)
      || els.find(e => norm(e.textContent || e.value).startsWith(want));
    if (el) {
      if (el.disabled) return 'disabled';
      setTimeout(() => el.click(), 0);
      return 'clicked';
    }
  }
  return 'missing';
}"""

# Runs in the page: (label) => {x0, x1, y} | null. Where to drag the slider named `label` (range input or role=slider)
# with real mouse events, in top-page viewport coordinates (iframe offsets added). Some pages only accept a slider
# moved by a trusted (user) gesture, and reset a value set by script.
SLIDER_JS = r"""(label) => {
""" + _FIND + r"""
  const w = norm(label);
  const textOf = c => {
    let t = c.getAttribute('aria-label') || '';
    if (c.labels && c.labels[0]) t = c.labels[0].innerText + ' ' + t;
    else if (c.closest('label')) t = c.closest('label').innerText + ' ' + t;
    return norm(t + ' ' + (c.name || ''));
  };
  const sliders = docs.flatMap(d => [...d.querySelectorAll('input[type=range], [role=slider]')]).filter(visible);
  const s = sliders.find(c => w && textOf(c).includes(w)) || sliders.find(c => w && w.includes(norm(c.getAttribute('aria-label'))) && c.getAttribute('aria-label'))
    || (sliders.length === 1 && /slider|regler|schieb|zieh|drag|entsperr|unlock/.test(w) ? sliders[0] : null);
  if (!s) return null;
  s.scrollIntoView({block: 'center'});
  const track = s.getAttribute('role') === 'slider' && s.parentElement ? s.parentElement.getBoundingClientRect() : s.getBoundingClientRect();
  const r = s.getBoundingClientRect();
  let dx = 0, dy = 0, win = s.ownerDocument.defaultView;
  while (win && win.frameElement) { const f = win.frameElement.getBoundingClientRect(); dx += f.left; dy += f.top; win = win.parent; }
  return {x0: r.left + Math.min(6, r.width / 2) + dx, x1: track.right - 2 + dx, y: r.top + r.height / 2 + dy};
}"""

READY_JS = "() => document.readyState"


def scoped_evaluator(evaluate, item_id: int):
    """Keep a guard decision inside the exact observed gate, including its frames/shadow roots."""
    async def scoped(js, *args):
        root = (f"const root = window.__swarmGuard?.refs[{int(item_id)}]; "
                "if (!root?.isConnected) throw new Error('Observed form disappeared'); const docs = [root];")
        return await evaluate(js.replace("const docs = [document];", root, 1), *args)
    return scoped


def is_max(value: str) -> bool:
    return str(value).strip().lower() in ("max", "maximum", "100", "end", "ende", "rechts", "right")


async def _wait_loaded(evaluate, settle: float = 0.8) -> None:
    await asyncio.sleep(settle)
    for _ in range(20):
        try:
            if await evaluate(READY_JS) == "complete":
                return
        except Exception:
            pass
        await asyncio.sleep(0.5)


async def click(evaluate, label: str) -> str:
    result = await evaluate(CLICK_JS, label)
    if result == "clicked":
        await _wait_loaded(evaluate)
    return result


async def fill_fields(evaluate, pairs: list[tuple[str, str]], apply_button: str = "", then_click: str = "") -> str:
    """Set the fields in order; returns a short report for the model (what was set, what failed and why)."""
    lines = []
    failed = False
    for field, value in pairs:
        result = await evaluate(SET_JS, [field, value])
        status = result.get("status") if isinstance(result, dict) else str(result)
        if status in ("disabled", "no-option", "missing") and apply_button:
            if await click(evaluate, apply_button) == "clicked":
                result = await evaluate(SET_JS, [field, value])
                status = result.get("status") if isinstance(result, dict) else str(result)
        if status == "ok":
            lines.append(f"{field} = {value}: ok")
        elif status == "no-option":
            failed = True
            lines.append(f"{field} = {value}: FAILED, no such option; available: {result.get('options')}")
        elif status == "read-only":
            failed = True
            lines.append(f"{field}: read-only output, not a checkbox or editable input; remove it from the fill request and inspect numeric validation errors")
        elif status == "missing":
            failed = True
            lines.append(f"{field} = {value}: FAILED, field not found; fields: {result.get('fields')}")
        else:
            failed = True
            lines.append(f"{field} = {value}: FAILED ({status})")
    if pairs and not failed:
        # Framework changes often apply on the next render, or a later select resets an earlier field.
        await asyncio.sleep(0.2)
        for field, value in pairs:
            checked = await evaluate(SET_JS, [field, value, True])
            if not isinstance(checked, dict) or checked.get("status") != "ok":
                failed = True
                lines.append(f"{field}: FAILED readback ({checked.get('status') if isinstance(checked, dict) else checked}); inspect current form")
    if then_click and not failed:
        lines.append(f"click '{then_click}': {await click(evaluate, then_click)}")
    elif then_click:
        lines.append("Did not advance: repair or verify the fields above, then continue from the current page.")
    return "\n".join(lines)
