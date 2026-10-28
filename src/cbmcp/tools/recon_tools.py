"""Document-start, per-lease client-side API recon execution."""

from __future__ import annotations

import hashlib
import json

from ..errors import InvalidArgument
from ..recon import Recon, validate_config
from ._common import guarded, use_lease


def register(mcp):
    @mcp.tool(
        description="Install per-lease document-start API recon mock, inspect status or page its capture index. L1 exact stubs win; L2 explicitly configured real-response patching; L3 offline API fallback. Keeps raw upstream and mock files separate. Install before navigation; configuration is immutable until browser_release. Does not authenticate to the backend."
    )
    @guarded
    async def js_recon(
        lease: str,
        action: str = "status",
        config: dict | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> dict:
        """
        Args:
            lease: Capability token from browser_acquire, not the lease_id label.
            action: install before navigation, status after navigation, or records.
            config: Required for install: exact origins, forward/offline mode and
                target-specific stubs/neutralize rules. See docs/api-recon.md.
                Same config is idempotent; changes require a clean lease.
            offset: Zero-based capture-index offset; follow next_offset.
            limit: Capture-index page size, 1..50. Bodies stay in private files.
        """
        async with use_lease(lease) as (_, session):
            recon = session.recon
            if action == "install":
                checked = validate_config(config, session.scope)
                digest = hashlib.sha256(
                    json.dumps(checked, sort_keys=True, ensure_ascii=False).encode()
                ).hexdigest()
                if recon is not None and recon.digest != digest:
                    raise InvalidArgument(
                        "recon config is immutable: release this lease and acquire a clean one"
                    )
                if recon is None:
                    recon = Recon(session, checked)
                    await recon.attach(session)
                    session.recon = recon
                return {
                    **await recon.status(session),
                    "reload_required": True,
                    "next": "Navigate or reload now; confirm the marker before claiming startup coverage.",
                }
            if action == "status":
                return await recon.status(session) if recon else {"ok": True, "installed": False}
            if action == "records":
                if recon is None:
                    return {"ok": True, "records": [], "total": 0}
                if not 0 <= offset or not 1 <= limit <= 50:
                    raise InvalidArgument("offset >= 0 and limit 1..50 required")
                rows = recon.rows[offset : offset + limit]
                end = offset + len(rows)
                return {
                    "ok": True,
                    "records": rows,
                    "total": len(recon.rows),
                    "next_offset": end if end < len(recon.rows) else None,
                    "dropped_records": recon.dropped,
                }
            raise InvalidArgument(
                "action must be install, status or records; release ends the mock"
            )
