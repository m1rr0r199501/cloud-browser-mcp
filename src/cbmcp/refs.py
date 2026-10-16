"""Page snapshots and stable element references.

Design notes
------------
Refs are written into the DOM as an attribute and the counter lives on
``window``, so a ref stays valid across snapshots for as long as the node lives.
Resolving a ref is a plain attribute selector, which means it works identically
in every frame, needs no bookkeeping on the Python side, and -- importantly --
yields a Playwright *locator*, which auto-waits and retries. Handle-based refs
would avoid touching the DOM but lose that reliability.

**The attribute name and the counter key are randomised per lease.** A page that
greps for ``data-cbref`` or ``data-playwright`` finds nothing, and a
MutationObserver sees an attribute name that means nothing to it. This does not
make instrumentation invisible -- a site determined to flag *any* unknown
attribute appearing on its nodes still can -- but it defeats signature matching.

Two deliberate differences from a generic accessibility snapshot, both because
this gateway serves security research:

* **hidden form fields are always included** (marked ``hidden``). CSRF tokens,
  nonces and signed state live there and are usually the interesting part.
* **``name``, ``type``, ``action`` and ``method`` are always shown** for form
  controls and forms, because that is what you need to reason about a request
  before you send it.
"""

from __future__ import annotations

import secrets


def new_ref_namespace() -> tuple[str, str]:
    """A per-lease (attribute name, window counter key) pair."""
    token = secrets.token_hex(5)
    return f"data-{token}", f"_{token}"


SNAPSHOT_JS = r"""
(opts) => {
  const maxNodes = opts.maxNodes || 1500;
  const maxText = opts.maxTextLen || 160;
  const includeHidden = !!opts.includeHidden;
  const REF_ATTR = opts.refAttr;
  const SEQ_KEY = opts.seqKey;

  if (typeof window[SEQ_KEY] !== 'number') {
    try {
      Object.defineProperty(window, SEQ_KEY, {
        value: 0, writable: true, enumerable: false, configurable: true,
      });
    } catch (e) { window[SEQ_KEY] = 0; }
  }

  const SKIP = new Set(['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','HEAD','META','LINK','TITLE','SVG','PATH','DEFS','USE','SYMBOL']);
  const FIELD = new Set(['INPUT','SELECT','TEXTAREA','BUTTON']);
  const INTERACTIVE = new Set(['A','BUTTON','INPUT','SELECT','TEXTAREA','LABEL','SUMMARY','DETAILS','IFRAME','FRAME','FORM','VIDEO','AUDIO','DIALOG']);

  let emitted = 0;
  let truncated = false;
  const lines = [];

  const clip = (s, n) => {
    if (s == null) return '';
    s = String(s).replace(/\s+/g, ' ').trim();
    return s.length > n ? s.slice(0, n) + '…' : s;
  };

  function visible(el) {
    const st = window.getComputedStyle(el);
    if (!st) return true;
    if (st.display === 'none' || st.visibility === 'hidden') return false;
    if (parseFloat(st.opacity || '1') === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 || r.height > 0;
  }

  function roleOf(el) {
    const explicit = el.getAttribute && el.getAttribute('role');
    if (explicit) return explicit.toLowerCase();
    const tag = el.tagName;
    if (tag === 'A') return el.hasAttribute('href') ? 'link' : 'generic';
    if (tag === 'BUTTON') return 'button';
    if (tag === 'SELECT') return el.multiple ? 'listbox' : 'combobox';
    if (tag === 'TEXTAREA') return 'textbox';
    if (tag === 'IFRAME' || tag === 'FRAME') return 'iframe';
    if (tag === 'FORM') return 'form';
    if (tag === 'IMG') return 'img';
    if (/^H[1-6]$/.test(tag)) return 'heading';
    if (tag === 'INPUT') {
      const t = (el.type || 'text').toLowerCase();
      if (t === 'submit' || t === 'button' || t === 'reset' || t === 'image') return 'button';
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      if (t === 'hidden') return 'hidden-input';
      if (t === 'file') return 'file-input';
      if (t === 'password') return 'password';
      if (t === 'search') return 'searchbox';
      return 'textbox';
    }
    return tag.toLowerCase();
  }

  function nameOf(el) {
    const aria = el.getAttribute && el.getAttribute('aria-label');
    if (aria) return clip(aria, maxText);
    const labelledby = el.getAttribute && el.getAttribute('aria-labelledby');
    if (labelledby) {
      const parts = labelledby.split(/\s+/)
        .map(id => { try { return (el.ownerDocument.getElementById(id) || {}).textContent || ''; } catch (e) { return ''; } });
      const joined = parts.join(' ').trim();
      if (joined) return clip(joined, maxText);
    }
    if (el.id) {
      try {
        const lab = el.ownerDocument.querySelector('label[for="' + CSS.escape(el.id) + '"]');
        if (lab && lab.textContent.trim()) return clip(lab.textContent, maxText);
      } catch (e) { /* an id that is not a valid selector */ }
    }
    if (el.closest) {
      const wrap = el.closest('label');
      if (wrap && wrap !== el && wrap.textContent.trim()) return clip(wrap.textContent, maxText);
    }
    for (const attr of ['placeholder','title','alt','aria-placeholder']) {
      const v = el.getAttribute && el.getAttribute(attr);
      if (v) return clip(v, maxText);
    }
    if (el.tagName === 'INPUT') {
      const t = (el.type || '').toLowerCase();
      if ((t === 'submit' || t === 'button' || t === 'reset') && el.value) return clip(el.value, maxText);
      return '';
    }
    if (el.tagName === 'IFRAME' || el.tagName === 'FRAME') return '';
    const own = Array.from(el.childNodes || [])
      .filter(n => n.nodeType === 3)
      .map(n => n.nodeValue)
      .join(' ')
      .trim();
    if (own) return clip(own, maxText);
    const text = (el.textContent || '').trim();
    if (text && text.length <= 120) return clip(text, maxText);
    return '';
  }

  function refOf(el) {
    let ref = el.getAttribute(REF_ATTR);
    if (!ref) {
      ref = 'e' + (++window[SEQ_KEY]);
      try { el.setAttribute(REF_ATTR, ref); } catch (e) { return null; }
    }
    return ref;
  }

  function attrBits(el) {
    const bits = [];
    const tag = el.tagName;
    const push = (k, v) => { if (v !== null && v !== undefined && v !== '') bits.push(k + '=' + JSON.stringify(String(v).slice(0, 200))); };

    if (tag === 'A') push('href', el.getAttribute('href'));
    if (tag === 'IFRAME' || tag === 'FRAME') { push('src', el.getAttribute('src')); push('name', el.getAttribute('name')); }
    if (tag === 'FORM') { push('action', el.getAttribute('action')); push('method', (el.getAttribute('method') || 'get').toLowerCase()); push('enctype', el.getAttribute('enctype')); }
    if (FIELD.has(tag)) {
      push('name', el.getAttribute('name'));
      if (tag === 'INPUT') push('type', (el.getAttribute('type') || 'text').toLowerCase());
      if (el.required) bits.push('required');
      if (el.disabled) bits.push('disabled');
      if (el.readOnly) bits.push('readonly');
      if (tag === 'INPUT' || tag === 'TEXTAREA') {
        const t = (el.type || '').toLowerCase();
        if (t === 'checkbox' || t === 'radio') { bits.push(el.checked ? 'checked' : 'unchecked'); push('value', el.value); }
        else if (t === 'password') { if (el.value) bits.push('value=<set>'); }
        else if (t === 'hidden') { push('value', el.value); }
        else if (el.value) push('value', clip(el.value, 120));
        push('maxlength', el.getAttribute('maxlength'));
        push('pattern', el.getAttribute('pattern'));
      }
      if (tag === 'SELECT') {
        const opts2 = Array.from(el.options || []).slice(0, 12).map(o => o.value);
        if (opts2.length) bits.push('options=' + JSON.stringify(opts2));
        push('selected', el.value);
      }
    }
    if (el.getAttribute && el.getAttribute('contenteditable') === 'true') bits.push('contenteditable');
    for (const attr of ['aria-expanded','aria-checked','aria-selected','aria-hidden','target','rel','onclick']) {
      const v = el.getAttribute && el.getAttribute(attr);
      if (v) push(attr, clip(v, 80));
    }
    return bits;
  }

  function emit(depth, text) {
    if (emitted >= maxNodes) { truncated = true; return false; }
    emitted++;
    lines.push('  '.repeat(Math.min(depth, 12)) + '- ' + text);
    return true;
  }

  function walk(node, depth) {
    if (emitted >= maxNodes) { truncated = true; return; }
    if (node.nodeType === 3) {
      const t = clip(node.nodeValue, maxText);
      if (t && t.length > 1) emit(depth, 'text ' + JSON.stringify(t));
      return;
    }
    if (node.nodeType !== 1) return;
    const el = node;
    const tag = el.tagName;
    if (SKIP.has(tag)) return;

    const isField = FIELD.has(tag);
    const shown = includeHidden || isField || visible(el);
    if (!shown) return;

    const role = roleOf(el);
    const interesting = INTERACTIVE.has(tag) || isField || role === 'heading'
      || (el.getAttribute && el.getAttribute('role'))
      || el.hasAttribute('onclick');

    let nextDepth = depth;
    if (interesting) {
      const ref = refOf(el);
      const name = nameOf(el);
      const bits = attrBits(el);
      if (!visible(el)) bits.push('hidden');
      let line = role;
      if (name) line += ' ' + JSON.stringify(name);
      if (ref) line += ' [ref=' + ref + ']';
      if (bits.length) line += ' ' + bits.join(' ');
      if (!emit(depth, line)) return;
      nextDepth = depth + 1;
      if (tag === 'IFRAME' || tag === 'FRAME') return;  // frames are walked separately
      if (tag === 'SELECT') return;
    }

    for (const child of Array.from(el.childNodes)) {
      walk(child, nextDepth);
      if (emitted >= maxNodes) { truncated = true; return; }
    }
  }

  const root = document.body || document.documentElement;
  if (root) walk(root, 0);

  return {
    url: location.href,
    title: document.title,
    readyState: document.readyState,
    lines: lines.join('\n'),
    nodes: emitted,
    truncated: truncated,
  };
}
"""

ELEMENT_DETAIL_JS = r"""
(opts) => {
  const el = document.querySelector('[' + opts.refAttr + '="' + opts.ref + '"]');
  if (!el) return null;
  const attrs = {};
  for (const a of Array.from(el.attributes || [])) {
    if (a.name === opts.refAttr) continue;
    attrs[a.name] = String(a.value).slice(0, 500);
  }
  const st = window.getComputedStyle(el) || {};
  const r = el.getBoundingClientRect();
  let form = null;
  if (el.form) {
    form = {
      action: el.form.getAttribute('action'),
      method: (el.form.getAttribute('method') || 'get').toLowerCase(),
      enctype: el.form.getAttribute('enctype'),
      fields: Array.from(el.form.elements || []).slice(0, 60).map(f => ({
        name: f.name || null,
        type: (f.type || f.tagName || '').toLowerCase(),
        value: (f.type === 'password') ? (f.value ? '<set>' : '') : String(f.value == null ? '' : f.value).slice(0, 200),
        disabled: !!f.disabled,
      })),
    };
  }
  const clean = (html) => String(html || '').split(opts.refAttr + '="').join('data-x="');
  return {
    tag: el.tagName.toLowerCase(),
    text: (el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 1000),
    inner_html: clean(el.innerHTML).slice(0, 4000),
    outer_html_head: clean(el.outerHTML).slice(0, 1000),
    attributes: attrs,
    box: { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height) },
    visible: !(st.display === 'none' || st.visibility === 'hidden' || parseFloat(st.opacity || '1') === 0),
    disabled: !!el.disabled,
    form: form,
  };
}
"""

PAGE_TEXT_JS = r"""
(opts) => {
  const pick = () => {
    for (const sel of ['main', 'article', '[role="main"]', '#content', '#main']) {
      const el = document.querySelector(sel);
      if (el && (el.innerText || '').trim().length > 200) return el;
    }
    return document.body || document.documentElement;
  };
  const el = opts && opts.selector ? document.querySelector(opts.selector) : pick();
  if (!el) return { text: '', url: location.href, title: document.title };
  return {
    text: (el.innerText || el.textContent || '').replace(/\n{3,}/g, '\n\n'),
    url: location.href,
    title: document.title,
  };
}
"""

# Collects what a page *says about itself* in ways that matter for a security
# review and are easy to miss by reading rendered text: script sources, form
# targets, storage contents, and the shape of injected globals.
PAGE_INVENTORY_JS = r"""
() => {
  const take = (store) => {
    const out = {};
    try {
      for (let i = 0; i < Math.min(store.length, 60); i++) {
        const k = store.key(i);
        out[k] = String(store.getItem(k) || '').slice(0, 300);
      }
    } catch (e) { return { _error: String(e).slice(0, 120) }; }
    return out;
  };
  const scripts = Array.from(document.scripts || []).slice(0, 120).map(s => ({
    src: s.src || null,
    type: s.type || null,
    inline_len: s.src ? 0 : (s.textContent || '').length,
    integrity: s.integrity || null,
    crossorigin: s.crossOrigin || null,
  }));
  const forms = Array.from(document.forms || []).slice(0, 40).map(f => ({
    action: f.getAttribute('action'),
    method: (f.getAttribute('method') || 'get').toLowerCase(),
    enctype: f.getAttribute('enctype'),
    field_names: Array.from(f.elements || []).slice(0, 60).map(e => e.name).filter(Boolean),
  }));
  return {
    url: location.href,
    title: document.title,
    scripts: scripts,
    forms: forms,
    local_storage: take(window.localStorage),
    session_storage: take(window.sessionStorage),
    iframes: Array.from(document.querySelectorAll('iframe,frame')).slice(0, 30)
      .map(f => ({ src: f.getAttribute('src'), sandbox: f.getAttribute('sandbox') })),
    meta_csp: (document.querySelector('meta[http-equiv="Content-Security-Policy"]') || {}).content || null,
    globals: Object.keys(window).filter(k => !/^(webkit|chrome|on)/.test(k)).slice(-80),
  };
}
"""


def ref_selector(ref: str, attr: str) -> str:
    safe = ref.replace("\\", "").replace('"', "")
    return f'[{attr}="{safe}"]'
