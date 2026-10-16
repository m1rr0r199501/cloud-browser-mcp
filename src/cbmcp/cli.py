"""Operator CLI.

Deliberately includes the commands you want when something has gone wrong and
the MCP server is not the convenient way to ask:

    cbmcp check          is the config sane and the cloud account reachable?
    cbmcp sessions       what is the account actually running right now?
    cbmcp stop <id>      stop one session
    cbmcp stop-all       stop every session this deployment created
    cbmcp reconcile      full reconciliation pass
    cbmcp usage          spend today and this month
    cbmcp audit          recent audit records
    cbmcp smoke          end-to-end test: acquire, navigate, debug, release
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from .app import App, set_app
from .config import get_settings
from .log import setup_logging
from .pool import AcquireRequest
from .providers import build_provider


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, default=str, ensure_ascii=False))


async def cmd_check(_args: argparse.Namespace) -> int:
    settings = get_settings()
    print("configuration")
    print("-------------")
    for key, value in settings.redacted().items():
        print(f"  {key} = {value}")

    print("\nderived guarantees")
    print("------------------")
    print(f"  idle reclaim after        {settings.idle_timeout_seconds:.0f}s of no tool calls")
    print(f"  hard lease lifetime       {settings.max_lease_seconds:.0f}s")
    print(
        f"  platform hard stop        {settings.platform_timeout_minutes} min "
        "(survives this process being killed)"
    )
    worst = (
        settings.platform_timeout_minutes / 60.0 * settings.price_browser_usd_per_hour
    )
    print(f"  worst case per lease      ${worst:.4f} of browser time")
    print(
        f"  worst case all slots      ${worst * settings.max_concurrency:.4f} "
        f"({settings.max_concurrency} concurrent)"
    )
    if settings.network_mode == "direct":
        print("  egress                    direct platform IP, no proxy billing")
    else:
        print(f"  egress                    {settings.network_mode} (proxy billing applies)")

    if settings.provider == "browseruse" and not settings.browser_use_api_key:
        print("\nFAIL: BROWSER_USE_API_KEY is not set", file=sys.stderr)
        return 2

    provider = build_provider(settings)
    try:
        print("\nplatform")
        print("--------")
        info = await provider.account_info()
        print(f"  project_id                {info.project_id}")
        print(f"  concurrent_session_limit  {info.concurrent_session_limit}")
        print(f"  active_session_count      {info.active_session_count}")
        print(f"  credit_balance_usd        {info.credit_balance_usd}")
        if (
            info.concurrent_session_limit
            and settings.max_concurrency > info.concurrent_session_limit
        ):
            print(
                f"\n  NOTE: CBMCP_MAX_CONCURRENCY={settings.max_concurrency} exceeds the "
                f"platform limit of {info.concurrent_session_limit}; it will be clamped."
            )
        sessions = await provider.list_sessions(active_only=True)
        print(f"  active sessions now       {len(sessions)}")
        mine = [s for s in sessions if s.metadata.get("cbmcp_owner") == settings.owner_tag]
        if mine:
            print(f"  ...created by this deployment: {len(mine)}")
            for session in mine:
                print(
                    f"      {session.id}  lease={session.metadata.get('cbmcp_lease')} "
                    f"cost=${session.total_cost_usd:.4f}"
                )
        print("\nOK")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"\nFAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        await provider.aclose()


async def cmd_sessions(_args: argparse.Namespace) -> int:
    settings = get_settings()
    provider = build_provider(settings)
    try:
        sessions = await provider.list_sessions(active_only=True, limit=500)
        total = sum(s.total_cost_usd for s in sessions)
        _print(
            {
                "active": len(sessions),
                "estimated_cost_so_far_usd": round(total, 6),
                "owner_tag": settings.owner_tag,
                "sessions": [
                    {**s.public(), "mine": s.metadata.get("cbmcp_owner") == settings.owner_tag}
                    for s in sessions
                ],
            }
        )
        return 0
    finally:
        await provider.aclose()


async def cmd_stop(args: argparse.Namespace) -> int:
    settings = get_settings()
    provider = build_provider(settings)
    try:
        ok = await provider.stop_session(args.session_id)
        confirmed = await provider.get_session(args.session_id)
        _print(
            {
                "stopped": ok,
                "status": confirmed.status if confirmed else "gone",
                "cost_usd": confirmed.total_cost_usd if confirmed else None,
            }
        )
        return 0 if ok else 1
    finally:
        await provider.aclose()


async def cmd_stop_all(args: argparse.Namespace) -> int:
    settings = get_settings()
    provider = build_provider(settings)
    try:
        sessions = await provider.list_sessions(active_only=True, limit=500)
        targets = [
            s
            for s in sessions
            if args.all or s.metadata.get("cbmcp_owner") == settings.owner_tag
        ]
        if not targets:
            print("nothing to stop")
            return 0
        print(f"stopping {len(targets)} session(s):")
        results = []
        for session in targets:
            ok = await provider.stop_session(session.id)
            print(f"  {session.id}  cost=${session.total_cost_usd:.4f}  stopped={ok}")
            results.append({"id": session.id, "stopped": ok})
        return 0 if all(r["stopped"] for r in results) else 1
    finally:
        await provider.aclose()


async def _with_app(fn) -> int:
    app = App()
    set_app(app)
    await app.start()
    try:
        return await fn(app)
    finally:
        await app.stop()
        set_app(None)


async def cmd_reconcile(_args: argparse.Namespace) -> int:
    async def run(app: App) -> int:
        _print(await app.R.reconcile())
        return 0

    return await _with_app(run)


async def cmd_usage(_args: argparse.Namespace) -> int:
    async def run(app: App) -> int:
        _print(await app.B.snapshot())
        return 0

    return await _with_app(run)


async def cmd_audit(args: argparse.Namespace) -> int:
    async def run(app: App) -> int:
        _print(await app.store.recent_audit(args.limit, args.lease))
        return 0

    return await _with_app(run)


async def cmd_smoke(args: argparse.Namespace) -> int:
    """End-to-end check: rent a browser, drive it, debug it, give it back.

    Exercises the paths that are hard to unit test -- a real CDP connection, the
    debugger, hook injection, network capture -- and always releases, including
    on failure, so a failed smoke test does not leave a session billing.
    """

    async def run(app: App) -> int:
        url = args.url
        print(f"acquiring a browser (provider={app.s.provider})...")
        result = await app.P.acquire(
            AcquireRequest(task_label="smoke-test", idempotency_key="smoke")
        )
        if result.get("status") != "ready":
            _print(result)
            print("FAIL: did not get a browser", file=sys.stderr)
            return 1
        token = result["lease"]
        lease = app.P.resolve_token(token)
        session = lease.session
        assert session is not None
        print(f"  lease {lease.lease_id} ready in {result.get('age_seconds')}s")

        failures: list[str] = []
        try:
            handle = session.active()
            print(f"navigating to {url} ...")
            response = await handle.page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            print(f"  status {response.status if response else '?'}  {handle.page.url}")

            snapshot = await session.snapshot(handle, max_nodes=200)
            print(f"  snapshot: {snapshot.get('nodes')} nodes, title={snapshot.get('title')!r}")
            if not snapshot.get("lines"):
                failures.append("snapshot produced no content")

            text = await session.page_text(handle)
            print(f"  text: {len(text.get('text') or '')} chars")

            evaluated = await session.evaluate(handle, "navigator.userAgent")
            print(f"  user agent: {str(evaluated.get('value'))[:80]}")

            requests = session.netlog.query(limit=200)
            print(f"  network: {len(requests)} request(s) recorded")
            if not requests:
                failures.append("no network requests recorded")
            with_body = [r for r in requests if r.body_path]
            print(f"  bodies captured: {len(with_body)}")

            print("installing hooks (json, xhr, cookie) ...")
            session.hooks.add(targets=["json", "xhr", "cookie"], capture_stack=True)
            applied = await session.apply_hooks()
            installed = applied.get("pages", {})
            print(f"  hook status: {json.dumps(installed)[:300]}")

            print("exercising debugger ...")
            debugger = await session.debugger_for()
            print(f"  scripts loaded: {len(debugger.scripts)}")
            if debugger.scripts:
                hits = await debugger.search("function", max_hits=3)
                print(f"  search 'function': {hits['hit_count']} hit(s)")
            else:
                print("  (no scripts on this page -- try --url with a JS-heavy site)")

            # Reload so hooks see startup code, then confirm records arrive.
            await handle.page.reload(wait_until="domcontentloaded", timeout=45_000)
            await asyncio.sleep(1.5)
            if not session.hooks.use_binding:
                await session.drain_hooks()
            records = session.hooks.query(limit=5)
            print(f"  hook records after reload: {len(session.hooks.records)}")
            for record in records[:3]:
                print(f"      {record.label} {str(record.args)[:90]}")

            har = await session.netlog.export_har(session.dir / "smoke.har")
            print(f"  HAR: {har['entries']} entries -> {har['path']}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{type(exc).__name__}: {exc}")
        finally:
            print("releasing ...")
            released = await app.P.release_by_token(token, reason="smoke_test")
            _print(released)
            if not released.get("stop_confirmed"):
                failures.append("the cloud session stop was not confirmed")

        print("\nverifying nothing was left running ...")
        leftover = [
            s
            for s in await app.PR.list_sessions(active_only=True, limit=200)
            if s.metadata.get("cbmcp_owner") == app.s.owner_tag
        ]
        if leftover:
            failures.append(f"{len(leftover)} session(s) still active: {[s.id for s in leftover]}")
        else:
            print("  clean: no active sessions from this deployment")
        print(f"  slots in use: {app.P.stats()['slots_used']}")
        if app.P.stats()["slots_used"] != 0:
            failures.append("slots were not returned to zero")

        if failures:
            print("\nFAILURES:", file=sys.stderr)
            for failure in failures:
                print(f"  - {failure}", file=sys.stderr)
            return 1
        print("\nOK: acquire -> drive -> debug -> release -> zero active sessions")
        return 0

    return await _with_app(run)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cbmcp", description="Cloud browser MCP gateway -- operator CLI"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="validate config and reach the cloud platform")
    sub.add_parser("sessions", help="list active cloud sessions on the account")
    sub.add_parser("reconcile", help="run a reconciliation pass now")
    sub.add_parser("usage", help="show spend today and this month")

    stop = sub.add_parser("stop", help="stop one cloud session by id")
    stop.add_argument("session_id")

    stop_all = sub.add_parser("stop-all", help="stop sessions created by this deployment")
    stop_all.add_argument(
        "--all",
        action="store_true",
        help="stop EVERY active session on the account, including ones this "
        "deployment did not create",
    )

    audit = sub.add_parser("audit", help="show recent audit records")
    audit.add_argument("--limit", type=int, default=50)
    audit.add_argument("--lease", default=None)

    smoke = sub.add_parser("smoke", help="end-to-end test against a real cloud browser")
    smoke.add_argument("--url", default="https://example.com")

    return parser


def main() -> int:
    setup_logging(get_settings().log_level)
    args = build_parser().parse_args()
    handlers = {
        "check": cmd_check,
        "sessions": cmd_sessions,
        "stop": cmd_stop,
        "stop-all": cmd_stop_all,
        "reconcile": cmd_reconcile,
        "usage": cmd_usage,
        "audit": cmd_audit,
        "smoke": cmd_smoke,
    }
    try:
        return asyncio.run(handlers[args.command](args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
