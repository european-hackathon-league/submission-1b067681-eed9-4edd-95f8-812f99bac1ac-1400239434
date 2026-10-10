"""Page extraction helpers: turn the page a model has opened into plain text it (and the analysts) can read.

Business sites hide facts in places a screenshot or the visible text misses: collapsed <details>, hover tooltips,
canvas images with an aria-label, same-origin iframes, zero-width characters inside numbers and references. The
extractor reads all of those, and skips content that is explicitly hidden from people (`hidden`, `aria-hidden`,
closed dialogs, scripts and styles), because that content is not part of the page a person sees.
"""
import re

# Runs in the page: (opts) => text. Recurses into same-origin iframes. Kept dependency-free and short.
EXTRACT_JS = r"""(opts) => {
  const SKIP = new Set(['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','SVG','HEAD','META','LINK']);
  const BLOCK = /^(P|DIV|SECTION|ARTICLE|MAIN|ASIDE|HEADER|FOOTER|NAV|UL|OL|LI|TABLE|THEAD|TBODY|TR|H[1-6]|DL|DT|DD|FORM|FIELDSET|LEGEND|DETAILS|SUMMARY|PRE|BLOCKQUOTE|CAPTION|LABEL|FIGURE|BR|HR)$/;
  const out = [];
  const push = s => { if (s) out.push(s); };
  const hiddenForPeople = el => el.hasAttribute('hidden') || el.getAttribute('aria-hidden') === 'true'
    || (el.tagName === 'DIALOG' && !el.open)
    || (el.style && el.style.display === 'none' && el.getAttribute('role') !== 'tooltip');
  const walk = (node, depth) => {
    if (node.nodeType === 3) { push(node.nodeValue); return; }
    if (node.nodeType !== 1) return;
    const el = node, tag = el.tagName;
    if (SKIP.has(tag) || hiddenForPeople(el)) return;
    const role = el.getAttribute('role'), label = el.getAttribute('aria-label');
    if (tag === 'IFRAME') {
      let doc = null; try { doc = el.contentDocument; } catch (e) {}
      const src = el.getAttribute('src') || '';
      push(`\n[iframe ${el.getAttribute('title') || src}]\n`);
      if ((!doc || !doc.body || !doc.body.innerText.trim()) && el.getAttribute('srcdoc')) {
        doc = new DOMParser().parseFromString(el.getAttribute('srcdoc'), 'text/html');   // inline frame content
      }
      if (doc && doc.body && doc.body.innerText.trim() && depth < 4) walk(doc.body, depth + 1);
      else push(`(frame not loaded or not readable${src ? '; open it directly with read_page(url="' + src + '")' : ''})`);
      push('\n[/iframe]\n'); return;
    }
    if (tag === 'CANVAS' || (role === 'img' && label)) { push(` ${label || el.textContent || ''} `); return; }
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') {
      const t = (el.type || '').toLowerCase();
      if (t === 'hidden' || t === 'password') return;
      let v = tag === 'SELECT' ? (el.selectedOptions[0] ? el.selectedOptions[0].text : '') : el.value;
      if (t === 'checkbox' || t === 'radio') v = el.checked ? '[x]' : '[ ]';
      push(` [${el.name || label || t || 'field'}: ${v}] `); return;
    }
    if (tag === 'TIME' && el.getAttribute('datetime')) push(` [${el.getAttribute('datetime')}] `);
    if (role === 'tooltip') push(' (tooltip: ');
    if (tag === 'TD' || tag === 'TH') push(' | ');
    if (BLOCK.test(tag)) push('\n');
    if (tag === 'SUMMARY') push('▸ ');
    for (const child of el.childNodes) walk(child, depth);
    if (role === 'tooltip') push(') ');
    if (BLOCK.test(tag)) push('\n');
  };
  walk(document.body, 0);
  return out.join('');
}"""

# Runs in the page: (label) => "clicked" | "missing". Clicks the first link/button whose text matches `label`
# (the model names it), in the top document or a same-origin iframe, so a paged result set can be followed.
NEXT_JS = r"""(label) => {
  const norm = s => (s || '').replace(/[​-‍⁠﻿]/g, '').replace(/\s+/g, ' ').trim().toLowerCase();
  const want = norm(label);
  const docs = [document];
  for (let i = 0; i < docs.length && i < 20; i++) {
    for (const f of docs[i].querySelectorAll('iframe')) { try { if (f.contentDocument) docs.push(f.contentDocument); } catch (e) {} }
  }
  for (const doc of docs) {
    for (const el of doc.querySelectorAll('a[href], button')) {
      if (el.disabled || el.closest('[hidden],[aria-hidden=true]')) continue;
      if (norm(el.textContent) === want || norm(el.getAttribute('aria-label')) === want) { el.click(); return 'clicked'; }
    }
  }
  return 'missing';
}"""

_ZERO_WIDTH = re.compile(r"[​-‍⁠﻿­]")


def clean(text: str) -> str:
    """Remove zero-width characters and collapse blank space, keeping line structure."""
    text = _ZERO_WIDTH.sub("", text or "")
    lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in text.splitlines()]
    out, blank = [], False
    for line in lines:
        if line in ("", "|"):
            if not blank and out:
                out.append("")
            blank = True
            continue
        out.append(line.strip(" |") if line.count("|") == 1 else line)
        blank = False
    return "\n".join(out).strip()
