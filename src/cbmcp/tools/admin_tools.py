"""Operator visibility: capacity, spend, leftovers, audit trail."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from ..app import get_app
from ..errors import InvalidArgument
from ._common import guarded


def register(mcp: MCPServer) -> None:
    @mcp.tool(
        description=(
            "Operational snapshot: capacity and queue, every live lease, spend "
            "today and this month, reaper activity, and any cloud session whose "
            "stop was never confirmed. Read this first when something looks wrong."
        )
    )
    @guarded
    async def admin_stats(include_platform: bool = False) -> dict[str, Any]:
        """
        Args:
            include_platform: Also query the cloud platform for its own view of
                active sessions, concurrency limit and credit balance.
        """
        app = get_app()
        counters = await app.store.counters()
        leftovers = [
            {
                "platform_session_id": row["platform_session_id"],
                "lease_id": row["lease_id"],
                "state": row["state"],
                "created_at": row["created_at"],
                "cost_usd": row["cost_usd"],
                "note": row["note"],
            }
            for row in await app.store.unconfirmed_stops()
        ]
        out: dict[str, Any] = {
            "ok": True,
            "instance_id": app.s.instance_id,
            "owner_tag": app.s.owner_tag,
            "provider": app.s.provider,
            "capacity": app.P.stats(),
            "limits": {
                "idle_timeout_seconds": app.s.idle_timeout_seconds,
                "max_lease_seconds": app.s.max_lease_seconds,
                "platform_timeout_minutes": app.s.platform_timeout_minutes,
                "queue_timeout_seconds": app.s.queue_timeout_seconds,
                "network_mode": app.s.network_mode,
                "scope_mode": app.s.scope_mode,
                "scope_allow": app.s.scope_allow,
                "debug_default": app.s.debug_default,
            },
            "leases": [lease.public(include_session=True) for lease in app.P.live_leases()],
            "budget": await app.B.snapshot(),
            "reaper": app.R.snapshot(),
            "counters": counters,
            "unconfirmed_stops": leftovers,
            "startup_report": app.startup_report,
        }
        if leftovers:
            out["warning"] = (
                f"{len(leftovers)} cloud session(s) have not been confirmed stopped. "
                "They may still be billing. Run admin_sessions(action='reconcile')."
            )
        if include_platform:
            try:
                info = await app.PR.account_info()
                out["platform_account"] = {
                    "project_id": info.project_id,
                    "concurrent_session_limit": info.concurrent_session_limit,
                    "active_session_count": info.active_session_count,
                    "credit_balance_usd": info.credit_balance_usd,
                }
            except Exception as exc:  # noqa: BLE001
                out["platform_account_error"] = str(exc)[:200]
        return out

    @mcp.tool(
        description=(
            "Inspect and control cloud sessions directly.\n"
            "  action='list'       every active session on the account, flagged as "
            "ours, another process's, or another account user's\n"
            "  action='reconcile'  run reconciliation now: stop orphans, settle "
            "unconfirmed stops, reclaim slots\n"
            "  action='stop'       force-stop one session by platform id\n"
            "  action='release_all' release every lease on this gateway"
        )
    )
    @guarded
    async def admin_sessions(
        action: str = "list",
        platform_session_id: str | None = None,
    ) -> dict[str, Any]:
        """
        Args:
            action: 'list' | 'reconcile' | 'stop' | 'release_all'.
            platform_session_id: Required for action='stop'.
        """
        app = get_app()
        if action == "list":
            sessions = await app.PR.list_sessions(active_only=True, limit=500)
            ours = {
                lease.platform_session_id: lease.lease_id
                for lease in app.P.live_leases()
                if lease.platform_session_id
            }
            rows = []
            for session in sessions:
                owner = session.metadata.get("cbmcp_owner")
                if session.id in ours:
                    ownership = "ours_live"
                elif owner == app.s.owner_tag:
                    instance = session.metadata.get("cbmcp_instance")
                    ownership = (
                        "ours_orphaned"
                        if instance == app.s.instance_id
                        else "same_deployment_other_process"
                    )
                elif owner:
                    ownership = f"other_deployment:{owner}"
                else:
                    ownership = "not_created_by_cbmcp"
                rows.append(
                    {
                        **session.public(),
                        "ownership": ownership,
                        "lease_id": ours.get(session.id)
                        or session.metadata.get("cbmcp_lease"),
                    }
                )
            total_cost = sum(s.total_cost_usd for s in sessions)
            return {
                "ok": True,
                "active_on_platform": len(rows),
                "estimated_cost_so_far_usd": round(total_cost, 6),
                "sessions": rows,
            }

        if action == "reconcile":
            return {"ok": True, "reconcile": await app.R.reconcile()}

        if action == "stop":
            if not platform_session_id:
                raise InvalidArgument("action='stop' needs platform_session_id")
            # Agents call this tool, and an agent can be steered by the pages it
            # reads. Limit it to sessions this deployment created; anything else
            # on the account is the operator's call (`cbmcp stop <id>`).
            target = await app.PR.get_session(platform_session_id)
            if target is not None and target.metadata.get("cbmcp_owner") != app.s.owner_tag:
                raise InvalidArgument(
                    f"session {platform_session_id!r} was not created by this deployment",
                    hint="Use the operator CLI (`python -m cbmcp.cli stop <id>`) for "
                    "sessions this gateway does not own.",
                )
            stopped = await app.PR.stop_session(platform_session_id)
            await app.store.upsert_platform_session(
                platform_session_id=platform_session_id,
                provider=app.s.provider,
                state="stopped" if stopped else "unknown",
                stopped=True,
                stop_confirmed=stopped,
                note="stopped via admin_sessions",
            )
            await app.store.audit(
                "admin.stop_session",
                target=platform_session_id,
                outcome="stopped" if stopped else "failed",
            )
            return {"ok": True, "stopped": stopped, "platform_session_id": platform_session_id}

        if action == "release_all":
            count = await app.P.release_all(reason="admin_release_all")
            return {"ok": True, "released": count}

        raise InvalidArgument("action must be list, reconcile, stop or release_all")

    @mcp.tool(
        description=(
            "Recent audit records: acquires, releases, reclaims, orphan stops, "
            "budget rejections and scope violations. Credentials and CDP URLs are "
            "never recorded."
        )
    )
    @guarded
    async def admin_audit(limit: int = 50, lease_id: str | None = None) -> dict[str, Any]:
        """
        Args:
            limit: Maximum records (max 500).
            lease_id: Restrict to one lease.
        """
        app = get_app()
        records = await app.store.recent_audit(max(1, min(limit, 500)), lease_id)
        return {"ok": True, "count": len(records), "audit": records}
