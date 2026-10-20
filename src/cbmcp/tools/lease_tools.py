"""Lease lifecycle tools: acquire, queue, status, renew, release."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from ..app import get_app
from ..errors import InvalidArgument
from ..pool import AcquireRequest
from ._common import guarded, use_lease

ACQUIRE_DOC = """Rent a cloud browser for this task. Returns a lease token.

Use this whenever a task needs a real web browser: opening or operating a
website, logging in, filling in forms, clicking through a flow, screenshots,
downloads, or inspecting network traffic / JavaScript.

Browsers are created on demand and cost money for every second they run, so:
  - call this ONLY when HTTP requests are not enough (you need JavaScript
    execution, dynamic rendering, a login flow, or network/JS debugging);
  - call browser_release as soon as you are done, without waiting to be asked;
  - pass the returned `lease` token to every page_*/net_*/js_* call.

If all slots are busy you get status="queued" plus a ticket_id. Poll
browser_queue_status with it -- do NOT call browser_acquire again, since that
queues a second request and can start a second billable session.

Pass `idempotency_key` (any stable string for this unit of work) so a retried
call returns the same browser instead of renting another one.

The lease is reclaimed automatically if you stop using it (idle timeout), so a
crashed or cancelled agent cannot leave a browser running. Long analysis
between calls should call browser_renew.

`profile` keeps browser state between tasks: pass a name (e.g. "main") and
cookies, logins and local storage saved by earlier browsers on that profile
are loaded; this browser's state is saved back when it is released. Created on
first use. One browser per profile at a time (error profile_in_use otherwise).
Omit it for a clean browser.

`debug` controls instrumentation:
  "full"  (default) breakpoints, call stacks, scope inspection, hooks
  "hooks" function hooks only -- no Runtime.enable/Debugger.enable, so less
          detectable by the page, but no breakpoints
  "off"   plain driving and network capture; nothing injected
"""


def register(mcp: MCPServer) -> None:
    @mcp.tool(description=ACQUIRE_DOC)
    @guarded
    async def browser_acquire(
        task_label: str | None = None,
        idempotency_key: str | None = None,
        scope_allow: list[str] | None = None,
        scope_deny: list[str] | None = None,
        lease_seconds: float | None = None,
        debug: str | None = None,
        network_mode: str | None = None,
        profile: str | None = None,
        profile_id: str | None = None,
        screen_width: int | None = None,
        screen_height: int | None = None,
        solve_captchas: bool | None = None,
    ) -> dict[str, Any]:
        """
        Args:
            task_label: Short description of what this browser is for. Appears in
                audit records and admin views; not a security boundary.
            idempotency_key: Stable key for this unit of work. A repeat call with
                the same key returns the existing lease instead of renting a
                second browser.
            scope_allow: Hostnames this lease may reach, e.g.
                ["example.com", "*.cdn.example.com"]. Enforced on every request,
                not just navigation. Can only narrow the operator's allowlist.
            scope_deny: Hostnames to block even if otherwise allowed.
            lease_seconds: Requested lifetime; capped by the server maximum.
            debug: "full" | "hooks" | "off" (see the tool description).
            network_mode: "direct" | "platform_proxy" | "custom_proxy", only if
                the operator permits per-lease choice.
            profile: Name of a persistent profile (letters, digits, . _ - @),
                for tasks that need saved logins or cookies. Created on first
                use. Omit for a clean throwaway browser.
            profile_id: A provider profile id made outside this server (e.g. in
                the Browser Use dashboard). Prefer `profile`.
            screen_width: Viewport width in pixels.
            screen_height: Viewport height in pixels.
            solve_captchas: Let the platform attempt CAPTCHA solving.
        """
        app = get_app()
        request = AcquireRequest(
            task_label=task_label,
            idempotency_key=idempotency_key,
            network_mode=network_mode,
            scope_allow=scope_allow,
            scope_deny=scope_deny,
            lease_seconds=lease_seconds,
            profile=profile,
            profile_id=profile_id,
            debug=debug,
            screen_width=screen_width,
            screen_height=screen_height,
            solve_captchas=solve_captchas,
        )
        result = await app.P.acquire(request)

        # Surface an accidental duplicate without blocking a deliberate one.
        if result.get("status") == "ready" and task_label and not idempotency_key:
            others = [
                lease
                for lease in app.P.live_leases()
                if lease.task_label == task_label and lease.token != result.get("lease")
            ]
            if others:
                result["warning"] = (
                    f"{len(others)} other live lease(s) share task_label={task_label!r}. "
                    "If that was a retry rather than deliberate parallelism, release the "
                    "extras and pass idempotency_key next time."
                )
        return result

    @mcp.tool(
        description=(
            "Check a queued browser request. Blocks briefly and returns the lease as "
            "soon as a slot frees. Set cancel=true to give up the queue position."
        )
    )
    @guarded
    async def browser_queue_status(
        ticket_id: str,
        wait_seconds: float | None = None,
        cancel: bool = False,
    ) -> dict[str, Any]:
        """
        Args:
            ticket_id: The ticket_id from a queued browser_acquire.
            wait_seconds: How long to block waiting (capped by the server).
            cancel: Abandon the request. If it was already granted, the browser
                is released so it does not bill unattended.
        """
        app = get_app()
        if cancel:
            return await app.P.cancel_ticket(ticket_id)
        return await app.P.queue_status(ticket_id, wait_seconds)

    @mcp.tool(
        description=(
            "Release a browser and stop the cloud session. Call this as soon as you "
            "finish -- billing continues until the session actually stops. Safe to "
            "call twice."
        )
    )
    @guarded
    async def browser_release(lease: str, reason: str | None = None) -> dict[str, Any]:
        """
        Args:
            lease: The lease token from browser_acquire (lst_..., not lease_id).
            reason: Optional note for the audit record.
        """
        app = get_app()
        return await app.P.release_by_token(lease, reason=reason or "agent_release")

    @mcp.tool(
        description=(
            "Lease state: time left, idle countdown, pages, captured network/hook "
            "counts and cost so far. Omit `lease` for a server-wide summary. "
            "check_network=true also reports how this browser really reaches the "
            "internet: egress IP, country, HTTP protocol (the platform routes even "
            "'direct' traffic through its own network)."
        )
    )
    @guarded
    async def browser_status(
        lease: str | None = None, check_network: bool = False
    ) -> dict[str, Any]:
        """
        Args:
            lease: The lease token (lst_...) to inspect. Omit for a server-wide summary.
            check_network: Open a short-lived tab to report the egress IP and protocol.
        """
        app = get_app()
        if lease:
            obj = app.P.resolve_token(lease)
            obj.touch()
            out = {"ok": True, **obj.public(include_session=True)}
            if check_network:
                async with use_lease(lease) as (_, session):
                    out["network_check"] = await session.network_info()
            return out
        return {
            "ok": True,
            "pool": app.P.stats(),
            "budget": await app.B.snapshot(),
            "leases": [x.public(include_session=False) for x in app.P.live_leases()],
        }

    @mcp.tool(
        description=(
            "Manage persistent browser profiles (saved cookies, logins, local storage). "
            "action='list' shows each profile and whether a browser is using it; "
            "'create' makes one ahead of time (browser_acquire(profile=...) also "
            "creates on first use); 'delete' removes one and its saved state for good."
        )
    )
    @guarded
    async def browser_profiles(action: str = "list", name: str | None = None) -> dict[str, Any]:
        """
        Args:
            action: "list" | "create" | "delete".
            name: Profile name, for create and delete.
        """
        app = get_app()
        if action == "list":
            profiles = await app.P.list_profiles()
            return {
                "ok": True,
                "profiles": profiles,
                "default_profile": app.s.default_profile or None,
            }
        if action not in ("create", "delete") or not name:
            raise InvalidArgument(
                "use action='list', or action='create'/'delete' with a name",
            )
        if action == "create":
            return {"ok": True, "status": "ready", "profile": await app.P.create_profile(name)}
        return {"ok": True, **await app.P.delete_profile(name.strip())}

    @mcp.tool(
        description=(
            "Extend a lease's idle deadline. Use it before a long stretch of "
            "thinking or analysis so the browser is not reclaimed underneath you."
        )
    )
    @guarded
    async def browser_renew(lease: str, extend_seconds: float | None = None) -> dict[str, Any]:
        """
        Args:
            lease: The lease token.
            extend_seconds: Push the absolute lifetime out by this much, still
                capped by the server maximum lease lifetime.
        """
        app = get_app()
        obj = app.P.resolve_token(lease)
        obj.touch()
        granted = 0.0
        if extend_seconds:
            ceiling = obj.created_at + app.s.max_lease_seconds
            target = min(obj.expires_at + extend_seconds, ceiling)
            granted = max(0.0, target - obj.expires_at)
            obj.expires_at = target
            await app.store.update_lease(obj.lease_id, expires_at=target)
        result = {"ok": True, "extended_seconds": round(granted, 1), **obj.public(include_session=False)}
        if extend_seconds and granted < extend_seconds:
            result["note"] = (
                "Capped by the server's maximum lease lifetime. Release and acquire "
                "a fresh browser if you need longer."
            )
        return result
