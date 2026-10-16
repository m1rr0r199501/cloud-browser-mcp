"""Function hooking -- non-blocking instrumentation of page JavaScript.

Hooks and breakpoints answer different questions, which is why this gateway has
both:

* A **breakpoint** stops the world. You need to know *where* to break first, and
  while it is paused nothing else on the page can run.
* A **hook** wraps a function and records every call -- arguments, return value
  and **call stack** -- while the page keeps running. That is how you find the
  interesting code in the first place: hook ``JSON.stringify`` and
  ``XMLHttpRequest.send``, click login, and the recorded stack tells you exactly
  which function built the signed payload.

The two combine: ``break_on_match`` makes a hook fire ``debugger;`` only when an
argument matches a pattern, so execution stops *inside* the signing routine with
its locals intact, ready for ``js_pause_state`` and ``js_eval_frame``.

Mechanics
---------
The runtime is injected with ``add_init_script``, so it installs **before any
page script runs** -- hooking after load would miss everything that happens
during startup, which is usually where key material is set up.

Observability of the instrumentation itself is taken seriously, because the
pages under study are often the ones that look for it:

* the runtime's global and its CDP binding are given **per-lease random names**,
  so nothing matches a signature like ``window.__cbhook``;
* ``Function.prototype.toString`` is patched through a WeakMap so a hooked
  function still reports ``[native code]`` -- otherwise one
  ``JSON.stringify.toString()`` exposes the whole scheme;
* wrappers carry **no own properties**; "already hooked" is tracked in a WeakMap.

What cannot be hidden: ``Runtime.enable``/``Debugger.enable`` (needed for
breakpoints and for the binding), and the timing signature of a ``debugger;``
statement. Use ``debug='hooks'`` to get instrumentation without those, or
``debug='off'`` for none at all.
"""

from __future__ import annotations

import json
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .errors import InvalidArgument
from .log import get_logger

log = get_logger("hooks")


def new_hook_namespace() -> tuple[str, str]:
    """Per-lease (global object key, CDP binding name)."""
    token = secrets.token_hex(6)
    return f"_{token}", f"_b{token}"


# Preset bundles. Each entry is an instruction handed to the in-page installer.
PRESETS: dict[str, list[dict[str, Any]]] = {
    "xhr": [
        {"kind": "method", "path": "XMLHttpRequest.prototype.open", "label": "xhr.open"},
        {"kind": "method", "path": "XMLHttpRequest.prototype.send", "label": "xhr.send"},
        {
            "kind": "method",
            "path": "XMLHttpRequest.prototype.setRequestHeader",
            "label": "xhr.setRequestHeader",
        },
    ],
    "fetch": [{"kind": "method", "path": "window.fetch", "label": "fetch"}],
    "websocket": [
        {"kind": "method", "path": "WebSocket.prototype.send", "label": "ws.send"},
        {"kind": "ctor", "path": "WebSocket", "label": "ws.new"},
    ],
    "json": [
        {"kind": "method", "path": "JSON.stringify", "label": "JSON.stringify"},
        {"kind": "method", "path": "JSON.parse", "label": "JSON.parse"},
    ],
    "cookie": [{"kind": "accessor", "path": "document.cookie", "label": "document.cookie"}],
    "storage": [
        {"kind": "method", "path": "Storage.prototype.setItem", "label": "storage.setItem"},
        {"kind": "method", "path": "Storage.prototype.getItem", "label": "storage.getItem"},
    ],
    "encode": [
        {"kind": "method", "path": "window.btoa", "label": "btoa"},
        {"kind": "method", "path": "window.atob", "label": "atob"},
        {"kind": "method", "path": "window.encodeURIComponent", "label": "encodeURIComponent"},
        {"kind": "method", "path": "TextEncoder.prototype.encode", "label": "TextEncoder.encode"},
    ],
    "crypto": [
        {"kind": "method", "path": "crypto.subtle.digest", "label": "subtle.digest"},
        {"kind": "method", "path": "crypto.subtle.sign", "label": "subtle.sign"},
        {"kind": "method", "path": "crypto.subtle.encrypt", "label": "subtle.encrypt"},
        {"kind": "method", "path": "crypto.subtle.importKey", "label": "subtle.importKey"},
        {"kind": "method", "path": "crypto.getRandomValues", "label": "getRandomValues"},
        # Common bundled crypto libraries, hooked only when present.
        {"kind": "method", "path": "CryptoJS.AES.encrypt", "label": "CryptoJS.AES.encrypt"},
        {"kind": "method", "path": "CryptoJS.MD5", "label": "CryptoJS.MD5"},
        {"kind": "method", "path": "CryptoJS.HmacSHA256", "label": "CryptoJS.HmacSHA256"},
    ],
    "eval": [
        {"kind": "method", "path": "window.eval", "label": "eval"},
        {"kind": "ctor", "path": "Function", "label": "new Function"},
    ],
    "timer": [
        {"kind": "method", "path": "window.setTimeout", "label": "setTimeout"},
        {"kind": "method", "path": "window.setInterval", "label": "setInterval"},
    ],
    "dom": [
        {"kind": "accessor", "path": "Element.prototype.innerHTML", "label": "innerHTML"},
        {"kind": "method", "path": "document.createElement", "label": "createElement"},
    ],
}

PRESET_NAMES = tuple(PRESETS)


@dataclass
class HookRecord:
    seq: int
    ts: float
    label: str
    phase: str  # call | return | throw | get | set
    args: list[Any] = field(default_factory=list)
    result: Any = None
    stack: list[str] = field(default_factory=list)
    page_url: str | None = None
    page_id: str | None = None

    def public(self, max_chars: int = 400) -> dict[str, Any]:
        def clip(value: Any) -> Any:
            if isinstance(value, str) and len(value) > max_chars:
                return value[:max_chars] + f"...[+{len(value) - max_chars} chars]"
            return value

        return {
            "seq": self.seq,
            "label": self.label,
            "phase": self.phase,
            "args": [clip(a) for a in self.args],
            "result": clip(self.result),
            "stack": self.stack,
            "page_url": self.page_url,
            "page_id": self.page_id,
        }


class HookRegistry:
    """Per-lease hook configuration plus the bounded record buffer."""

    def __init__(
        self,
        *,
        namespace: str,
        binding: str,
        max_records: int = 4000,
        max_per_label: int = 400,
        max_stacks_per_label: int = 80,
    ) -> None:
        self.ns = namespace
        self.binding = binding
        self.use_binding = True
        self.max_per_label = max_per_label
        self.max_stacks_per_label = max_stacks_per_label
        self.specs: dict[str, dict[str, Any]] = {}
        self.records: deque[HookRecord] = deque(maxlen=max_records)
        self._seq = 0
        self.dropped = 0
        self._handle_seq = 0

    # ----- configuration ---------------------------------------------------

    def add(
        self,
        *,
        targets: list[str],
        custom_paths: list[str] | None = None,
        arg_filter: str | None = None,
        break_on_match: str | None = None,
        capture_stack: bool = True,
        max_arg_chars: int = 800,
        stack_depth: int = 8,
    ) -> dict[str, Any]:
        instructions: list[dict[str, Any]] = []
        unknown = [t for t in targets if t not in PRESETS]
        if unknown:
            raise InvalidArgument(
                f"unknown hook targets {unknown}",
                hint=f"Valid presets: {', '.join(PRESET_NAMES)}. "
                "For anything else pass custom_paths=['window.app.sign'].",
            )
        for name in targets:
            instructions.extend(PRESETS[name])
        for path in custom_paths or []:
            instructions.append({"kind": "auto", "path": path, "label": path})
        if not instructions:
            raise InvalidArgument("pass at least one target preset or custom path")

        self._handle_seq += 1
        handle = f"hk{self._handle_seq}"
        spec = {
            "handle": handle,
            "instructions": instructions,
            "arg_filter": arg_filter,
            "break_on_match": break_on_match,
            "capture_stack": capture_stack,
            "max_arg_chars": max_arg_chars,
            "stack_depth": stack_depth,
            "targets": targets,
            "custom_paths": custom_paths or [],
            "created_at": time.time(),
        }
        self.specs[handle] = spec
        return spec

    def remove(self, handle: str) -> bool:
        return self.specs.pop(handle, None) is not None

    def clear_specs(self) -> int:
        count = len(self.specs)
        self.specs.clear()
        return count

    def active_instructions(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for spec in self.specs.values():
            for instruction in spec["instructions"]:
                out.append(
                    {
                        **instruction,
                        "handle": spec["handle"],
                        "argFilter": spec["arg_filter"],
                        "breakOnMatch": spec["break_on_match"],
                        "captureStack": spec["capture_stack"],
                        "maxArgChars": spec["max_arg_chars"],
                        "stackDepth": spec["stack_depth"],
                    }
                )
        return out

    def install_payload(self) -> dict[str, Any]:
        return {
            "ns": self.ns,
            "binding": self.binding if self.use_binding else None,
            # Caps per hook label. Hooking a hot function such as JSON.stringify
            # or encodeURIComponent otherwise costs enough per call -- especially
            # with stack capture -- to make the page unusable.
            "maxPerLabel": self.max_per_label,
            "maxStacksPerLabel": self.max_stacks_per_label,
            "instructions": self.active_instructions(),
        }

    def install_script(self) -> str:
        """Self-contained script for ``add_init_script`` (which takes no arguments)."""
        return f"({RUNTIME_JS})({json.dumps(self.install_payload())});"

    # ----- records ---------------------------------------------------------

    def ingest(self, payload: str | list, *, page_id: str | None = None) -> int:
        batch: Any = payload
        if isinstance(payload, str):
            try:
                batch = json.loads(payload)
            except (TypeError, ValueError):
                return 0
        if isinstance(batch, dict):
            batch = [batch]
        if not isinstance(batch, list):
            return 0
        added = 0
        for item in batch:
            if not isinstance(item, dict):
                continue
            if len(self.records) == self.records.maxlen:
                self.dropped += 1
            self._seq += 1
            self.records.append(
                HookRecord(
                    seq=self._seq,
                    ts=float(item.get("t") or time.time()),
                    label=str(item.get("label") or "?"),
                    phase=str(item.get("phase") or "call"),
                    args=item.get("args") or [],
                    result=item.get("result"),
                    stack=[str(s) for s in (item.get("stack") or [])][:12],
                    page_url=item.get("url"),
                    page_id=page_id,
                )
            )
            added += 1
        return added

    def query(
        self,
        *,
        label_contains: str | None = None,
        text_contains: str | None = None,
        phase: str | None = None,
        since_seq: int = 0,
        limit: int = 50,
        newest_first: bool = True,
    ) -> list[HookRecord]:
        out: list[HookRecord] = []
        for record in self.records:
            if record.seq <= since_seq:
                continue
            if label_contains and label_contains.lower() not in record.label.lower():
                continue
            if phase and record.phase != phase:
                continue
            if text_contains:
                blob = json.dumps(
                    [record.args, record.result, record.stack], default=str
                ).lower()
                if text_contains.lower() not in blob:
                    continue
            out.append(record)
        if newest_first:
            out.reverse()
        return out[:limit]

    def clear_records(self) -> int:
        count = len(self.records)
        self.records.clear()
        self.dropped = 0
        return count

    def stats(self) -> dict[str, Any]:
        labels: dict[str, int] = {}
        for record in self.records:
            labels[record.label] = labels.get(record.label, 0) + 1
        return {
            "active_hooks": [
                {
                    "handle": spec["handle"],
                    "targets": spec["targets"],
                    "custom_paths": spec["custom_paths"],
                    "arg_filter": spec["arg_filter"],
                    "break_on_match": spec["break_on_match"],
                }
                for spec in self.specs.values()
            ],
            "records": len(self.records),
            "dropped_oldest": self.dropped,
            "delivery": "cdp_binding" if self.use_binding else "polled_drain",
            "calls_by_label": dict(sorted(labels.items(), key=lambda kv: -kv[1])[:30]),
        }


# The in-page runtime.
#
# Two hazards learned from running this against a real detection page, both of
# which froze the browser outright:
#
# 1. **Recursion.** The recorder serialises arguments with JSON.stringify and
#    flushes batches with it too. Hook JSON.stringify and that becomes
#    stringify -> wrapper -> record -> stringify -> ... forever. So every
#    function the recorder itself uses is captured as a *native reference before
#    anything is wrapped*, and a reentrancy flag makes any hooked function called
#    from inside the recorder fall straight through.
#
# 2. **Cost.** Hot functions such as encodeURIComponent or JSON.stringify are
#    called hundreds of times per page load, and `new Error().stack` on each call
#    is expensive enough to make a page unusable. Records and stack captures are
#    therefore capped per label, with the suppressed count reported so the
#    numbers are never silently wrong.
#
# Beyond that: a thrown error inside a hook must never break the page under
# study, so every wrapper is try/caught and always falls through to the original.
RUNTIME_JS = r"""
(payload) => {
  try {
    if (!payload || !payload.instructions || !payload.instructions.length) return;
    const NS = payload.ns;
    const BINDING = payload.binding;
    const MAX_PER_LABEL = payload.maxPerLabel || 400;
    const MAX_STACKS_PER_LABEL = payload.maxStacksPerLabel || 80;
    const G = (typeof window !== 'undefined') ? window : globalThis;

    if (!G[NS]) {
      // Captured BEFORE any wrapping, so the recorder can never call itself.
      const N = {
        stringify: JSON.stringify,
        parse: JSON.parse,
        Error: Error,
        now: Date.now,
        toString: Function.prototype.toString,
        defineProperty: Object.defineProperty,
        getDescriptor: Object.getOwnPropertyDescriptor,
        setTimeout: (typeof G.setTimeout === 'function') ? G.setTimeout.bind(G) : null,
        clearTimeout: (typeof G.clearTimeout === 'function') ? G.clearTimeout.bind(G) : null,
      };
      const state = {
        N: N,
        installed: {},
        buffer: [],
        drained: [],
        flushTimer: null,
        busy: false,
        counts: {},
        suppressed: {},
        originals: new WeakMap(),
        emit(rec) {
          try {
            this.buffer.push(rec);
            if (this.buffer.length >= 25) this.flush();
            else if (!this.flushTimer && N.setTimeout) {
              this.flushTimer = N.setTimeout(() => this.flush(), 250);
            }
          } catch (e) { /* never break the page */ }
        },
        flush() {
          try {
            if (this.flushTimer && N.clearTimeout) { N.clearTimeout(this.flushTimer); }
            this.flushTimer = null;
            if (!this.buffer.length) return;
            const batch = this.buffer.splice(0, this.buffer.length);
            const fn = this.getBinding ? this.getBinding() : null;
            if (typeof fn === 'function') fn(N.stringify(batch));
            else this.drained = this.drained.concat(batch).slice(-3000);
          } catch (e) { /* ignore */ }
        },
        drain() {
          this.flush();
          const out = this.drained;
          this.drained = [];
          return out;
        },
      };
      try {
        N.defineProperty(G, NS, {
          value: state, writable: true, enumerable: false, configurable: true,
        });
      } catch (e) { G[NS] = state; }

      // Make hooked functions still report [native code]. Without this, a single
      // JSON.stringify.toString() gives the whole thing away.
      try {
        const patched = function toString() {
          try {
            const original = state.originals.get(this);
            if (original) return N.toString.call(original);
          } catch (e) { /* fall through */ }
          return N.toString.call(this);
        };
        state.originals.set(patched, N.toString);
        N.defineProperty(Function.prototype, 'toString', {
          value: patched, writable: true, enumerable: false, configurable: true,
        });
      } catch (e) { /* ignore */ }

      try {
        G.addEventListener('beforeunload', () => { try { state.flush(); } catch (e) {} }, true);
      } catch (e) { /* ignore */ }

      // Runtime.addBinding installs an ENUMERABLE global function, so a plain
      // Object.keys(window) scan finds it. Keep the reference, drop the property.
      state.getBinding = function () {
        if (this.bindingFn) return this.bindingFn;
        if (!BINDING) return null;
        const fn = G[BINDING];
        if (typeof fn !== 'function') return null;
        this.bindingFn = fn;
        try {
          delete G[BINDING];
        } catch (e) {
          try {
            N.defineProperty(G, BINDING, {
              value: fn, writable: true, enumerable: false, configurable: true,
            });
          } catch (e2) { /* best effort */ }
        }
        return fn;
      };
      try { state.getBinding(); } catch (e) { /* binding may arrive later */ }
    }
    const H = G[NS];
    const N = H.N;

    const preview = (v, max) => {
      try {
        if (v === null) return null;
        const t = typeof v;
        if (t === 'undefined') return '<undefined>';
        if (t === 'string') return v.length > max ? v.slice(0, max) + '…[+' + (v.length - max) + ']' : v;
        if (t === 'number' || t === 'boolean') return v;
        if (t === 'function') return '<function ' + (v.name || 'anonymous') + '>';
        if (t === 'symbol' || t === 'bigint') return String(v);
        if (v instanceof ArrayBuffer) return '<ArrayBuffer ' + v.byteLength + 'B>';
        if (ArrayBuffer.isView(v)) return '<' + (v.constructor && v.constructor.name) + ' ' + v.byteLength + 'B>';
        if (typeof FormData !== 'undefined' && v instanceof FormData) {
          const o = {};
          try { for (const pair of v.entries()) o[pair[0]] = String(pair[1]).slice(0, 120); } catch (e) {}
          return { _FormData: o };
        }
        if (typeof Request !== 'undefined' && v instanceof Request) {
          return { _Request: { url: v.url, method: v.method } };
        }
        if (typeof Node !== 'undefined' && v instanceof Node) {
          return '<' + (v.nodeName || 'Node') + '>';
        }
        const seen = new WeakSet();
        const s = N.stringify(v, function (k, val) {
          if (val && typeof val === 'object') {
            if (seen.has(val)) return '<circular>';
            seen.add(val);
          }
          if (typeof val === 'string' && val.length > max) {
            return val.slice(0, max) + '…[+' + (val.length - max) + ']';
          }
          return val;
        });
        if (s === undefined) return String(v).slice(0, max);
        if (s.length > max * 3) return s.slice(0, max * 3) + '…';
        return N.parse(s);
      } catch (e) {
        try { return String(v).slice(0, max); } catch (e2) { return '<unserialisable>'; }
      }
    };

    const stackOf = (depth) => {
      try {
        const raw = (new N.Error()).stack || '';
        return raw.split('\n')
          .slice(1)
          .map(l => l.trim())
          .filter(l => l && l.indexOf('cbw') === -1 && l.indexOf(NS) === -1)
          .slice(0, depth);
      } catch (e) { return []; }
    };

    const matches = (pattern, args) => {
      if (!pattern) return true;
      try {
        const blob = N.stringify(args, (k, v) =>
          (typeof v === 'string' && v.length > 2000) ? v.slice(0, 2000) : v);
        return (blob || '').toLowerCase().indexOf(String(pattern).toLowerCase()) !== -1;
      } catch (e) { return false; }
    };

    // The single guarded entry point for all recording. Anything hooked that gets
    // called from in here falls through without recording, which is what stops
    // the JSON.stringify self-recursion.
    const record = (ins, phase, args, result) => {
      if (H.busy) return;
      H.busy = true;
      try {
        const label = ins.label;
        const n = (H.counts[label] = (H.counts[label] || 0) + 1);
        if (n > MAX_PER_LABEL) {
          H.suppressed[label] = (H.suppressed[label] || 0) + 1;
          return;
        }
        H.emit({
          t: N.now() / 1000,
          label: label,
          phase: phase,
          args: (args || []).map(a => preview(a, ins.maxArgChars || 800)),
          result: result === undefined ? undefined : preview(result, ins.maxArgChars || 800),
          stack: (ins.captureStack && n <= MAX_STACKS_PER_LABEL) ? stackOf(ins.stackDepth || 8) : [],
          url: location.href,
        });
      } catch (e) {
        /* ignore */
      } finally {
        H.busy = false;
      }
    };

    const shouldRecord = (ins, args) => {
      if (H.busy) return false;
      H.busy = true;
      try { return matches(ins.argFilter, args); }
      catch (e) { return false; }
      finally { H.busy = false; }
    };

    const wantsBreak = (ins, args) => {
      if (!ins.breakOnMatch || H.busy) return false;
      H.busy = true;
      try { return matches(ins.breakOnMatch, args); }
      catch (e) { return false; }
      finally { H.busy = false; }
    };

    const resolve = (path) => {
      const parts = String(path).split('.');
      if (parts[0] === 'window' || parts[0] === 'globalThis') parts.shift();
      let obj = G;
      for (let i = 0; i < parts.length - 1; i++) {
        if (obj == null) return null;
        obj = obj[parts[i]];
      }
      if (obj == null) return null;
      return { owner: obj, key: parts[parts.length - 1] };
    };

    const disguise = (wrapper, original) => {
      try { H.originals.set(wrapper, original); } catch (e) {}
      try {
        N.defineProperty(wrapper, 'name', {
          value: original.name || '', writable: false, enumerable: false, configurable: true,
        });
      } catch (e) {}
      try {
        N.defineProperty(wrapper, 'length', {
          value: original.length, writable: false, enumerable: false, configurable: true,
        });
      } catch (e) {}
    };

    const wrapMethod = (ins) => {
      const site = resolve(ins.path);
      if (!site || typeof site.owner[site.key] !== 'function') return false;
      const original = site.owner[site.key];
      if (H.originals.has(original)) return true;

      function cbw(...args) {
        const hit = shouldRecord(ins, args);
        if (hit) record(ins, 'call', args, undefined);
        if (hit && wantsBreak(ins, args)) {
          try { H.flush(); } catch (e) {}
          debugger;  // deliberate: inspect with js_pause_state / js_eval_frame
        }
        let out;
        try {
          out = original.apply(this, args);
        } catch (err) {
          if (hit) record(ins, 'throw', args, String(err).slice(0, 300));
          throw err;
        }
        try {
          if (hit) {
            if (out && typeof out.then === 'function') {
              out.then(
                v => record(ins, 'return', [], v),
                e => record(ins, 'throw', [], String(e).slice(0, 300))
              );
            } else {
              record(ins, 'return', [], out);
            }
          }
        } catch (e) { /* ignore */ }
        return out;
      }
      disguise(cbw, original);
      try { site.owner[site.key] = cbw; } catch (e) { return false; }
      return true;
    };

    const wrapCtor = (ins) => {
      const site = resolve(ins.path);
      if (!site || typeof site.owner[site.key] !== 'function') return false;
      const Original = site.owner[site.key];
      if (H.originals.has(Original)) return true;
      // A Proxy forwards toString to the target, so it already looks native.
      const Wrapped = new Proxy(Original, {
        construct(target, args, newTarget) {
          if (shouldRecord(ins, args)) {
            record(ins, 'call', args, undefined);
            if (wantsBreak(ins, args)) { try { H.flush(); } catch (e) {} debugger; }
          }
          return Reflect.construct(target, args, newTarget);
        },
        apply(target, thisArg, args) {
          if (shouldRecord(ins, args)) {
            record(ins, 'call', args, undefined);
            if (wantsBreak(ins, args)) { try { H.flush(); } catch (e) {} debugger; }
          }
          return Reflect.apply(target, thisArg, args);
        },
      });
      try { H.originals.set(Wrapped, Original); } catch (e) {}
      try { site.owner[site.key] = Wrapped; } catch (e) { return false; }
      return true;
    };

    const wrapAccessor = (ins) => {
      // e.g. document.cookie, Element.prototype.innerHTML
      const parts = String(ins.path).split('.');
      const prop = parts.pop();
      if (parts[0] === 'window' || parts[0] === 'globalThis') parts.shift();
      let target = G;
      for (const p of parts) {
        if (target == null) return false;
        target = target[p];
      }
      if (target == null) return false;

      let descriptor = null;
      let holder = target;
      while (holder && !descriptor) {
        descriptor = N.getDescriptor(holder, prop);
        if (!descriptor) holder = Object.getPrototypeOf(holder);
      }
      if (!descriptor) return false;
      if (!descriptor.get && !descriptor.set) return false;
      if (descriptor.get && H.originals.has(descriptor.get)) return true;
      if (descriptor.set && H.originals.has(descriptor.set)) return true;

      const originalGet = descriptor.get;
      const originalSet = descriptor.set;
      let newGet, newSet;
      if (originalGet) {
        newGet = function get() {
          const v = originalGet.call(this);
          record(ins, 'get', [], v);
          return v;
        };
        disguise(newGet, originalGet);
      }
      if (originalSet) {
        newSet = function set(v) {
          if (shouldRecord(ins, [v])) {
            record(ins, 'set', [v], undefined);
            if (wantsBreak(ins, [v])) { try { H.flush(); } catch (e) {} debugger; }
          }
          return originalSet.call(this, v);
        };
        disguise(newSet, originalSet);
      }
      try {
        N.defineProperty(holder, prop, {
          configurable: true,
          enumerable: descriptor.enumerable,
          get: newGet,
          set: newSet,
        });
        return true;
      } catch (e) { return false; }
    };

    for (const ins of payload.instructions) {
      const key = ins.handle + ':' + ins.path;
      if (H.installed[key]) continue;
      let ok = false;
      try {
        if (ins.kind === 'method') ok = wrapMethod(ins);
        else if (ins.kind === 'ctor') ok = wrapCtor(ins);
        else if (ins.kind === 'accessor') ok = wrapAccessor(ins);
        else ok = wrapMethod(ins) || wrapCtor(ins) || wrapAccessor(ins);
      } catch (e) { ok = false; }
      H.installed[key] = ok ? 'ok' : 'not-found';
    }
  } catch (e) { /* the page must survive us no matter what */ }
}
"""

STATUS_JS = r"""
(ns) => {
  const H = (typeof window !== 'undefined' ? window : globalThis)[ns];
  if (!H) return { present: false, installed: {} };
  return {
    present: true,
    installed: H.installed || {},
    buffered: (H.buffer || []).length,
    pending_drain: (H.drained || []).length,
    calls_seen: H.counts || {},
    suppressed_over_cap: H.suppressed || {},
  };
}
"""

DRAIN_JS = r"""
(ns) => {
  const H = (typeof window !== 'undefined' ? window : globalThis)[ns];
  if (!H) return [];
  try { return H.drain(); } catch (e) { return []; }
}
"""
