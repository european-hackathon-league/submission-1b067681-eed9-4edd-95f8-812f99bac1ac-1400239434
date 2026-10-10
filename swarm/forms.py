"""Form-filling helper: the model says WHICH field gets WHICH value; this sets them in order on the current page.

Handles cascading selects (a field stays disabled or lacks the wanted option until an "apply" button reloads the form)
by clicking the button the model named and trying again. Works on the top document and same-origin iframes.
`evaluate(js, *args)` runs a JS arrow function in the page and returns its (JSON-decoded) result.
"""
import asyncio

_FIND = r"""
  const norm = s => (s || '').replace(/[​-‍⁠﻿]/g, '').replace(/\s+/g, ' ').trim().toLowerCase();
  const docs = [document];
  for (let i = 0; i < docs.length && i < 20; i++) {
    for (const f of docs[i].querySelectorAll('iframe')) { try { if (f.contentDocument) docs.push(f.contentDocument); } catch (e) {} }
  }
  const visible = el => !el.closest('[hidden],[aria-hidden=true]') && !(el.closest('dialog') && !el.closest('dialog').open);
"""

SET_JS = r"""(args) => {
  const [want, value] = args;
""" + _FIND + r"""
  const labelOf = c => {
    let t = '';
    const doc = c.ownerDocument;
    if (c.id) { const l = doc.querySelector(`label[for="${CSS.escape(c.id)}"]`); if (l) t = l.textContent; }
    if (!t && c.closest('label')) {
      const clone = c.closest('label').cloneNode(true);
      clone.querySelectorAll('select,input,textarea').forEach(x => x.remove());
      t = clone.textContent;
    }
    return t || c.getAttribute('aria-label') || c.placeholder || '';
  };
  const controls = docs.flatMap(d => [...d.querySelectorAll('input,select,textarea')])
    .filter(c => c.type !== 'hidden' && visible(c));
  const w = norm(want);
  const c = controls.find(c => norm(c.name) === w) || controls.find(c => norm(labelOf(c)) === w)
    || controls.find(c => norm(labelOf(c)).startsWith(w)) || controls.find(c => w && norm(labelOf(c)).includes(w));
  if (!c) return {status: 'missing', fields: controls.map(c => labelOf(c).trim() || c.name).filter(Boolean).slice(0, 30)};
  if (c.disabled) return {status: 'disabled', name: c.name};
  const v = norm(value);
  const num = s => { const n = parseFloat(String(s).replace(/[^0-9.,-]/g, '').replace(',', '.')); return isNaN(n) ? null : n; };
  if (c.tagName === 'SELECT') {
    const opts = [...c.options];
    const o = opts.find(o => norm(o.value) === v) || opts.find(o => norm(o.text) === v)
      || opts.find(o => norm(o.text).startsWith(v + ' ')) || opts.find(o => num(o.text) !== null && num(o.text) === num(value) && /^[0-9.,\s-]+$/.test(value));
    if (!o || (!o.value && v)) return {status: 'no-option', name: c.name, options: opts.map(o => o.text.trim()).slice(0, 60)};
    c.value = o.value;
  } else if (c.type === 'radio') {
    const group = controls.filter(r => r.type === 'radio' && r.name === c.name);
    const r = group.find(r => norm(r.value) === v) || group.find(r => norm(labelOf(r)) === v) || group.find(r => norm(labelOf(r)).includes(v)) || c;
    r.checked = true; r.dispatchEvent(new Event('change', {bubbles: true}));
    return {status: 'ok', name: r.name, set: labelOf(r).trim()};
  } else if (c.type === 'checkbox') {
    c.checked = /^(true|1|yes|ja|x|on|checked)$/i.test(String(value).trim());
  } else {
    c.focus(); c.value = value;
  }
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

READY_JS = "() => document.readyState"


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
            lines.append(f"{field} = {value}: FAILED, no such option; available: {result.get('options')}")
        elif status == "missing":
            lines.append(f"{field} = {value}: FAILED, field not found; fields: {result.get('fields')}")
        else:
            lines.append(f"{field} = {value}: FAILED ({status})")
    if then_click:
        lines.append(f"click '{then_click}': {await click(evaluate, then_click)}")
    return "\n".join(lines)
