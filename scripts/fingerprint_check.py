#!/usr/bin/env python
"""Measure how detectable this gateway's instrumentation is.

Rents two cloud browsers -- one at debug='off', one at debug='full' with hooks
installed -- loads a purpose-built bot-detection page in each, and runs a set of
probes that a real anti-automation script would use. The output is a side-by-side
comparison, so "does this leave a fingerprint?" gets answered with data rather
than an opinion.

    python scripts/fingerprint_check.py
    python scripts/fingerprint_check.py --url https://bot.sannysoft.com/

Both leases are released in a finally block, including on failure, so a crashed
run cannot leave a cloud browser billing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cbmcp.app import App, set_app  # noqa: E402
from cbmcp.pool import AcquireRequest  # noqa: E402

# Probes a detection script would actually run. Each returns a small JSON-safe
# value; nothing here depends on the page under test.
PROBES_JS = r"""
() => {
  const out = {};
  const safe = (name, fn) => { try { out[name] = fn(); } catch (e) { out[name] = 'ERR: ' + String(e).slice(0, 80); } };

  // 1. The classic: does the driver announce itself?
  safe('navigator_webdriver', () => navigator.webdriver);

  // 2. Would a signature scan find a known instrumentation global?
  safe('known_global_names', () => ['__cbhook', '__playwright', '__puppeteer', '__nightmare',
       '_phantom', 'callPhantom', '__selenium_unwrapped', 'cdc_adoQpoasnfa76pfcZLmcfl_Array']
       .filter(k => k in window));

  // 3. Enumerable globals: our namespace must not appear here.
  safe('suspicious_enumerable', () => Object.keys(window)
       .filter(k => /^_[0-9a-f]{10,14}$/.test(k)));

  // 4. Non-enumerable globals are still reachable this way -- an honest check.
  safe('suspicious_own_property_names', () => Object.getOwnPropertyNames(window)
       .filter(k => /^_[0-9a-f]{10,14}$/.test(k)));

  // 5. toString spoofing: a hooked native must still look native.
  safe('JSON_stringify_toString', () => Function.prototype.toString.call(JSON.stringify));
  safe('JSON_parse_toString', () => Function.prototype.toString.call(JSON.parse));
  safe('fetch_toString', () => Function.prototype.toString.call(window.fetch));
  safe('xhr_send_toString', () => Function.prototype.toString.call(XMLHttpRequest.prototype.send));
  safe('toString_itself', () => Function.prototype.toString.call(Function.prototype.toString));
  safe('cookie_getter_toString', () => {
    const d = Object.getOwnPropertyDescriptor(Document.prototype, 'cookie');
    return d && d.get ? Function.prototype.toString.call(d.get) : 'no-descriptor';
  });

  // 6. DOM traces from element refs.
  safe('data_cbref_nodes', () => document.querySelectorAll('[data-cbref]').length);
  safe('random_data_attr_nodes', () => {
    let n = 0;
    for (const el of document.querySelectorAll('*')) {
      for (const a of el.attributes) {
        if (/^data-[0-9a-f]{10}$/.test(a.name)) { n++; break; }
      }
    }
    return n;
  });

  // 7. Runtime.enable probe: with the inspector consuming console output,
  //    argument previews are generated eagerly and getters fire.
  safe('runtime_enable_getter_fired', () => {
    let fired = false;
    const err = new Error('probe');
    try {
      Object.defineProperty(err, 'stack', { get() { fired = true; return ''; }, configurable: true });
    } catch (e) { return 'ERR: cannot redefine'; }
    console.debug(err);
    return fired;
  });

  // 8. Headless and environment tells, for context.
  safe('user_agent_has_headless', () => /headless/i.test(navigator.userAgent));
  safe('plugins_count', () => navigator.plugins.length);
  safe('languages', () => (navigator.languages || []).join(','));
  safe('hardware_concurrency', () => navigator.hardwareConcurrency);
  safe('chrome_object', () => typeof window.chrome);
  safe('permissions_query', () => typeof (navigator.permissions || {}).query);
  safe('webgl_vendor', () => {
    const c = document.createElement('canvas').getContext('webgl');
    if (!c) return 'no-webgl';
    const d = c.getExtension('WEBGL_debug_renderer_info');
    return d ? c.getParameter(d.UNMASKED_RENDERER_WEBGL) : 'no-ext';
  });
  return out;
}
"""

NATIVE = "[native code]"


def verdict(value: object) -> str:
    if isinstance(value, str) and "function" in value:
        return "NATIVE" if NATIVE in value else "WRAPPED (detectable)"
    return str(value)


async def acquire_waiting(app: App, request: AcquireRequest, *, budget: float = 180) -> dict:
    """Acquire, polling the ticket exactly as an agent is told to.

    Cloud cold start regularly exceeds the bounded acquire wait, so a first
    response of "creating" (or "queued") is normal, not a failure.
    """
    result = await app.P.acquire(request)
    deadline = asyncio.get_running_loop().time() + budget
    while result.get("status") in ("queued", "creating"):
        if asyncio.get_running_loop().time() > deadline:
            return result
        print(f"  {result['status']}: waited {result['waited_seconds']}s, polling...")
        result = await app.P.queue_status(result["ticket_id"], wait_seconds=30)
    return result


async def profile(app: App, *, url: str, debug: str, with_hooks: bool) -> dict:
    label = f"debug={debug}{' +hooks' if with_hooks else ''}"
    print(f"\n=== {label} ===")
    acquired = await acquire_waiting(
        app, AcquireRequest(task_label=f"fingerprint-{debug}", debug=debug)
    )
    if acquired.get("status") != "ready":
        print(f"  could not get a browser: {acquired}")
        return {"label": label, "error": acquired}

    token = acquired["lease"]
    lease = app.P.resolve_token(token)
    session = lease.session
    assert session is not None
    result: dict = {"label": label}

    try:
        # Hold the lease guard, exactly as the MCP tools do. Without it the
        # reaper sees an idle lease with no in-flight call and may reclaim the
        # browser mid-navigation.
        async with lease.guard(exclusive=True, timeout=300):
            if with_hooks:
                session.hooks.add(
                    targets=["xhr", "fetch", "json", "cookie", "encode"], capture_stack=True
                )
                applied = await session.apply_hooks()
                print(f"  hooks installed: {json.dumps(applied.get('pages', {}))[:200]}")

            handle = session.active()
            print(f"  loading {url} ...")
            response = await handle.page.goto(url, wait_until="load", timeout=60_000)
            status = response.status if response else None
            await asyncio.sleep(3)  # let detection scripts finish

            result["http_status"] = status
            result["final_url"] = handle.page.url
            try:
                result["title"] = await handle.page.title()
            except Exception:
                result["title"] = None
            print(f"  HTTP {status}  title={result['title']!r}")

            # Snapshot first, so the element-ref probes see the DOM traces that
            # taking a snapshot actually leaves behind.
            snap = await session.snapshot(handle, max_nodes=400)
            result["snapshot_nodes"] = snap.get("nodes")

            result["probes"] = await handle.page.evaluate(PROBES_JS)
            result["probes"]["ref_attr_in_use"] = session.ref_attr

            # Does a `debugger;` statement in page code freeze the renderer?
            # Measured both in the default state (pausing suppressed) and with
            # pausing switched on, since that is the trade-off being made.
            async def debugger_statement_pauses() -> bool:
                debuggers = [h.debugger for h in session.pages.values() if h.debugger]
                task = asyncio.create_task(
                    handle.page.evaluate("() => { debugger; return 1; }")
                )
                await asyncio.sleep(2.0)
                paused = any(d.is_paused for d in debuggers)
                for d in debuggers:
                    if d.is_paused:
                        await d.step("resume")
                try:
                    await asyncio.wait_for(task, timeout=10)
                except Exception:
                    task.cancel()
                return paused

            if session.debug_mode == "full":
                result["debugger_statement_pauses_default"] = await debugger_statement_pauses()
                debugger = await session.debugger_for()
                await debugger.set_pauses_enabled(True)
                result["debugger_statement_pauses_when_enabled"] = (
                    await debugger_statement_pauses()
                )
                await debugger.set_pauses_enabled(False)
            else:
                result["debugger_statement_pauses_default"] = False
                result["debugger_statement_pauses_when_enabled"] = "n/a (no debugger)"

            # What the detection page itself reports, in its own words.
            text = await session.page_text(handle)
            result["page_report"] = (text.get("text") or "")[:2500]

            result["network_entries"] = session.netlog.stats()["entries"]
            if with_hooks:
                if not session.hooks.use_binding:
                    await session.drain_hooks()
                result["hook_records"] = len(session.hooks.records)
                result["hook_labels"] = session.hooks.stats()["calls_by_label"]
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        print(f"  ERROR: {result['error']}"[:300])
    finally:
        released = await app.P.release_by_token(token, reason="fingerprint_check")
        seconds = released.get("browser_seconds")
        cost = released.get("cost_usd")
        print(
            f"  released: {released.get('status')}"
            + (f" {seconds}s" if seconds is not None else "")
            + (f" ${cost}" if cost is not None else "")
        )
        result["cost_usd"] = cost or 0.0
    return result


async def run(args: argparse.Namespace) -> int:
    app = App()
    set_app(app)
    await app.start()
    try:
        clean = await profile(app, url=args.url, debug="off", with_hooks=False)
        instrumented = await profile(app, url=args.url, debug="full", with_hooks=True)

        print("\n" + "=" * 78)
        print("FINGERPRINT COMPARISON")
        print("=" * 78)
        keys = [
            "navigator_webdriver",
            "known_global_names",
            "suspicious_enumerable",
            "suspicious_own_property_names",
            "data_cbref_nodes",
            "random_data_attr_nodes",
            "runtime_enable_getter_fired",
            "JSON_stringify_toString",
            "JSON_parse_toString",
            "fetch_toString",
            "xhr_send_toString",
            "cookie_getter_toString",
            "toString_itself",
            "user_agent_has_headless",
            "plugins_count",
            "chrome_object",
            "webgl_vendor",
        ]
        a = clean.get("probes", {})
        b = instrumented.get("probes", {})
        print(f"{'probe':<34} {'debug=off':<22} {'debug=full +hooks':<22}")
        print("-" * 78)
        for key in keys:
            left, right = verdict(a.get(key)), verdict(b.get(key))
            flag = "  <-- differs" if left != right else ""
            print(f"{key:<34} {left[:21]:<22} {right[:21]:<22}{flag}")
        print("-" * 78)
        print(
            f"{'debugger; pauses (default)':<34} "
            f"{str(clean.get('debugger_statement_pauses_default')):<22} "
            f"{str(instrumented.get('debugger_statement_pauses_default')):<22}"
        )
        print(
            f"{'debugger; pauses (opted in)':<34} "
            f"{str(clean.get('debugger_statement_pauses_when_enabled')):<22} "
            f"{str(instrumented.get('debugger_statement_pauses_when_enabled')):<22}"
        )
        print(
            f"{'network entries recorded':<34} "
            f"{str(clean.get('network_entries')):<22} "
            f"{str(instrumented.get('network_entries')):<22}"
        )
        print(
            f"{'hook records':<34} {'n/a':<22} "
            f"{str(instrumented.get('hook_records')):<22}"
        )
        if instrumented.get("hook_labels"):
            print(f"\nhooked calls observed: {instrumented['hook_labels']}")

        print("\nWhat the detection page reported (debug=full):")
        print("-" * 78)
        print((instrumented.get("page_report") or "")[:1800])

        total = (clean.get("cost_usd") or 0) + (instrumented.get("cost_usd") or 0)
        print(f"\ntotal cost of this run: ${total:.6f}")

        out = Path(args.out)
        out.write_text(
            json.dumps({"clean": clean, "instrumented": instrumented}, indent=2, default=str)
        )
        print(f"full results written to {out}")
        return 0
    finally:
        await app.stop()
        set_app(None)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="https://bot.sannysoft.com/")
    parser.add_argument("--out", default="fingerprint-report.json")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
