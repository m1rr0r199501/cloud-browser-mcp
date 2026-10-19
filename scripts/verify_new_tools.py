#!/usr/bin/env python
"""Verify the four newly added capabilities against a real cloud browser.

    python scripts/verify_new_tools.py

Checks, in order: request initiator call stacks, breakpoint-by-code-text,
environment emulation, and the rebuild bundle (including that the generated
Node scaffold actually runs). The lease is released in a finally block so a
failure cannot leave a browser billing.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cbmcp.app import App, set_app  # noqa: E402
from cbmcp.pool import AcquireRequest  # noqa: E402


def _find_node() -> Path | None:
    """Playwright's bundled node (any Python version, any OS), else node on PATH."""
    try:
        import playwright

        driver = Path(playwright.__file__).resolve().parent / "driver"
        for name in ("node", "node.exe"):
            if (driver / name).exists():
                return driver / name
    except ImportError:
        pass
    found = shutil.which("node")
    return Path(found) if found else None

URL = "https://bot.sannysoft.com/"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""))


async def acquire(app: App) -> dict:
    got = await app.P.acquire(AcquireRequest(task_label="verify-new-tools", debug="full"))
    while got.get("status") in ("queued", "creating"):
        print(f"  {got['status']}, waiting...")
        got = await app.P.queue_status(got["ticket_id"], wait_seconds=30)
    return got


async def run() -> int:
    app = App()
    set_app(app)
    await app.start()
    token = None
    try:
        got = await acquire(app)
        if got.get("status") != "ready":
            print(f"could not acquire: {got}")
            return 1
        token = got["lease"]
        lease = app.P.resolve_token(token)
        session = lease.session
        assert session is not None

        async with lease.guard(exclusive=True, timeout=300):
            handle = session.active()

            print("\n== setup: hooks + navigate ==")
            session.hooks.add(targets=["json", "xhr", "fetch", "encode"], capture_stack=True)
            await session.apply_hooks()
            await handle.page.goto(URL, wait_until="load", timeout=60_000)
            await asyncio.sleep(3)
            print(f"  loaded, {session.netlog.stats()['entries']} requests recorded")

            # ---- 1. request initiator ------------------------------------
            print("\n== 1. request initiator (which JS made this request) ==")
            stats = session.netlog.stats()
            check(
                "initiators captured",
                stats["initiators_captured"] > 0,
                f"{stats['initiators_captured']} captured",
            )
            # Diagnostics: what did we actually capture, and does it join to the
            # Playwright-side entries at all?
            raw = list(session.netlog._initiators)
            kinds: dict[str, int] = {}
            for item in raw:
                kinds[item.kind] = kinds.get(item.kind, 0) + 1
            print(f"      initiator types: {kinds}")
            print(f"      with stacks: {sum(1 for i in raw if i.stack)}")
            entries = session.netlog.query(limit=5, newest_first=False)
            for sample in raw[:3]:
                print(f"      cdp : {sample.method} {sample.url[:70]} t={sample.wall_time:.1f}")
            for sample in entries[:3]:
                print(
                    f"      pw  : {sample.method} {sample.url[:70]} t={sample.started_at:.1f}"
                )
            matched = sum(1 for e in session.netlog.query(limit=100)
                          if session.netlog.initiator_for(e))
            print(f"      joined to entries: {matched}")
            with_stack = None
            for entry in session.netlog.query(limit=100):
                info = session.netlog.initiator_for(entry)
                if info and info.get("call_stack"):
                    with_stack = (entry, info)
                    break
            if with_stack:
                entry, info = with_stack
                check("initiator has a JS call stack", True, f"{entry.method} {entry.url[:60]}")
                print(f"      type={info['type']}  origin={info.get('origin')}")
                print(f"      break_here={str(info.get('break_here'))[:110]}")
            else:
                script_initiated = [
                    e
                    for e in session.netlog.query(limit=100)
                    if (session.netlog.initiator_for(e) or {}).get("type") == "script"
                ]
                check(
                    "initiator has a JS call stack",
                    False,
                    f"none had a stack ({len(script_initiated)} script-initiated)",
                )

            # ---- 2. breakpoint by code text ------------------------------
            print("\n== 2. breakpoint by code text ==")
            debugger = await session.debugger_for()
            print(f"  {len(debugger.scripts)} scripts loaded")
            try:
                found = await debugger.set_text_breakpoint("webdriver")
                bp = found["breakpoint"]
                check(
                    "breakpoint set from code text",
                    bool(bp["resolved_locations"]) or bp["kind"] == "url",
                    f"{found['matched']['url'][:50]}:{found['matched']['line']} "
                    f"({found['total_matches']} matches)",
                )
                print(f"      snippet: {found['matched']['snippet'][:100]}")
                await debugger.remove_breakpoint(bp["handle"])
            except Exception as exc:  # noqa: BLE001
                check("breakpoint set from code text", False, f"{type(exc).__name__}: {exc}")

            # ---- 3. emulation --------------------------------------------
            print("\n== 3. environment emulation ==")
            emulated = await session.emulate(
                handle,
                network="slow-3g",
                cpu_throttle=4,
                timezone="Asia/Shanghai",
                locale="zh-CN",
                latitude=31.23,
                longitude=121.47,
                dark_mode=True,
            )
            applied = emulated["applied"]
            # colour scheme is best-effort: Browser Use's browsers ignore both
            # Playwright's emulate_media and the raw CDP call. The code reports
            # that honestly instead of claiming success, which is what matters.
            required = {"cpu_throttle", "network", "geolocation", "timezone", "locale"}
            check(
                "emulation applied",
                required <= set(applied),
                f"{sorted(applied)} failed={emulated.get('failed')}",
            )
            observed = await handle.page.evaluate(
                "() => ({tz: Intl.DateTimeFormat().resolvedOptions().timeZone,"
                " dark: matchMedia('(prefers-color-scheme: dark)').matches})"
            )
            check(
                "page observes the overrides",
                observed["tz"] == "Asia/Shanghai",
                f"timezone={observed['tz']} (dark={observed['dark']}, "
                "colour scheme unsupported by this platform)",
            )
            # Put it back so the rest of the run is not throttled.
            await session.emulate(handle, network="none", cpu_throttle=1)

            # ---- 4. rebuild bundle ---------------------------------------
            print("\n== 4. rebuild bundle ==")
            if not session.hooks.use_binding:
                await session.drain_hooks()
            bundle = await session.export_rebuild_bundle(
                handle,
                script_urls=["sannysoft", "main", "index"],
                notes="verification run",
            )
            root = Path(bundle["path"])
            check(
                "bundle written",
                root.exists() and (root / "rebuild.js").exists(),
                f"{bundle['total_bytes']} bytes, {bundle['vector_count']} vectors, "
                f"{len(bundle['scripts_saved'])} scripts",
            )
            env = json.loads((root / "env.json").read_text())
            check(
                "env.json has the browser environment",
                bool(env.get("navigator", {}).get("userAgent")) and bool(env.get("timezone")),
                f"ua={env['navigator']['userAgent'][:40]}... tz={env['timezone']}",
            )
            # The scaffold must actually run -- Playwright ships a node binary.
            node = _find_node()
            if node is not None:
                proc = subprocess.run(
                    [str(node), str(root / "rebuild.js")],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                check(
                    "node rebuild.js runs",
                    proc.returncode == 0,
                    (proc.stdout or proc.stderr).strip().split("\n")[0][:90],
                )
            else:
                check("node rebuild.js runs", False, "no node binary found to test with")
    except Exception as exc:  # noqa: BLE001
        check("run completed without error", False, f"{type(exc).__name__}: {exc}")
    finally:
        if token:
            released = await app.P.release_by_token(token, reason="verify_new_tools")
            print(
                f"\nreleased: {released.get('status')} "
                f"{released.get('browser_seconds')}s ${released.get('cost_usd')}"
            )
        leftover = [
            s
            for s in await app.PR.list_sessions(active_only=True, limit=100)
            if s.metadata.get("cbmcp_owner") == app.s.owner_tag
        ]
        check("nothing left running", not leftover, f"{len(leftover)} active")
        await app.stop()
        set_app(None)

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    for name, _, detail in failed:
        print(f"  FAILED: {name} -- {detail}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
