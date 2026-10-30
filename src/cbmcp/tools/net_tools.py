"""HTTP and WebSocket observation tools."""

from __future__ import annotations

import base64
import json
from typing import Any

from mcp.server.mcpserver import MCPServer

from ..app import get_app
from ..errors import NotFound
from ._common import clip, guarded, try_parse_json, use_lease

OBSERVABILITY_NOTE = (
    "Scope: this is Chrome DevTools Protocol observation, not a packet capture. "
    "Request/response headers, query, bodies, redirects, failures and WebSocket "
    "frames are visible. TLS handshakes, certificates, HTTP/2 frame detail and "
    "other processes' traffic are not. Requests made before the recorder attached "
    "(i.e. before browser_acquire returned) are absent, and Service Worker or "
    "cross-process iframe traffic may be partial."
    " Requests affected by js_recon network_rules carry source_kind and "
    "network_intervention; a driver-fulfilled mock or injected error is not an upstream response."
)

REQUESTS_DOC = f"""List observed HTTP requests, newest first, with filters.

Start here when you want the API behind a page: filter resource_types=['xhr',
'fetch'] to drop images, CSS and fonts. Then use net_request for one entry's
full headers and net_body for its payload.

{OBSERVABILITY_NOTE}
"""


def register(mcp: MCPServer) -> None:
    @mcp.tool(description=REQUESTS_DOC)
    @guarded
    async def net_requests(
        lease: str,
        url_contains: str | None = None,
        method: str | None = None,
        resource_types: list[str] | None = None,
        status_min: int | None = None,
        status_max: int | None = None,
        only_failed: bool = False,
        only_with_body: bool = False,
        since_id: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """
        Args:
            lease: Lease token.
            url_contains: Substring match on the URL.
            method: HTTP method filter, e.g. 'POST'.
            resource_types: e.g. ['xhr', 'fetch', 'document', 'script', 'websocket'].
            status_min: Minimum response status.
            status_max: Maximum response status.
            only_failed: Only failed requests and 4xx/5xx responses.
            only_with_body: Only entries whose response body was captured.
            since_id: Return only entries newer than this id, for incremental polling.
            limit: Maximum entries (max 200).
        """
        async with use_lease(lease, exclusive=False) as (obj, session):
            entries = session.netlog.query(
                url_contains=url_contains,
                method=method,
                resource_types=resource_types,
                status_min=status_min,
                status_max=status_max,
                only_failed=only_failed,
                only_with_body=only_with_body,
                since_id=since_id,
                limit=max(1, min(limit, 200)),
            )
            stats = session.netlog.stats()
            out: dict[str, Any] = {
                "ok": True,
                "count": len(entries),
                "requests": [e.summary() for e in entries],
                "totals": stats,
            }
            if stats.get("dropped_oldest"):
                out["note"] = (
                    f"{stats['dropped_oldest']} oldest entries were dropped (ring buffer "
                    "full). Poll with since_id during long sessions, or export with net_har."
                )
            if session.netlog.blocked:
                out["scope_blocked"] = list(session.netlog.blocked)[-10:]
            hint = session.netlog.throttle_hint()
            if hint:
                out["rate_limit_warning"] = hint
            if any(e.browser_error_page for e in entries):
                out["browser_error_page_note"] = (
                    "Entries marked browser_error_page got an error status with no usable "
                    "body; the browser showed its own error page, which is NOT stored as "
                    "their body."
                )
            return out

    @mcp.tool(
        description=(
            "Full detail for one request: all request and response headers, parsed "
            "query parameters, the request body, timing, server IP, redirect origin "
            "and failure reason.\n"
            "Also returns the **initiator**: the JavaScript call stack that caused "
            "the request. That tells you which function built it without setting a "
            "breakpoint first, and comes with a ready-made js_breakpoint call for "
            "the exact line. Requires the lease's debug mode to be 'full'."
        )
    )
    @guarded
    async def net_request(
        lease: str, request_id: str, redact_auth: bool = False
    ) -> dict[str, Any]:
        """
        Args:
            lease: Lease token.
            request_id: Entry id from net_requests, e.g. 'r42'.
            redact_auth: Mask Authorization/Cookie headers in the output.
        """
        async with use_lease(lease, exclusive=False) as (obj, session):
            entry = session.netlog.get(request_id)
            if entry is None:
                raise NotFound(
                    f"no request {request_id!r} on this lease",
                    hint="Ids come from net_requests and are dropped once the buffer wraps.",
                )
            detail = entry.detail(redact=redact_auth)
            initiator = session.netlog.initiator_for(entry)
            if initiator is not None:
                detail["initiator"] = initiator
            elif session.debug_mode != "full":
                detail["initiator"] = {
                    "unavailable": "initiator capture needs debug='full' on the lease"
                }
            return {"ok": True, **detail}

    @mcp.tool(
        description=(
            "Response body for one request. JSON is parsed and returned as "
            "structured data; other text is returned as text; binary comes back "
            "base64. Paginate long bodies with `offset`."
        )
    )
    @guarded
    async def net_body(
        lease: str,
        request_id: str,
        max_chars: int | None = None,
        offset: int = 0,
        raw: bool = False,
    ) -> dict[str, Any]:
        """
        Args:
            lease: Lease token.
            request_id: Entry id from net_requests.
            max_chars: Characters to return (server capped).
            offset: Start position for pagination.
            raw: Skip JSON parsing and return text as-is.
        """
        app = get_app()
        async with use_lease(lease, exclusive=False) as (obj, session):
            data, entry = await session.netlog.read_body(request_id)
            if entry is None:
                raise NotFound(f"no request {request_id!r} on this lease")
            if data is None:
                return {
                    "ok": False,
                    "error": "body_unavailable",
                    **entry.provenance(),
                    "message": entry.body_error or "no body was captured for this request",
                    "request": entry.summary(),
                    "hint": (
                        "Bodies are read as responses arrive. Redirects have none, and a "
                        "navigation discards the old document's buffer. Set "
                        "CBMCP_CAPTURE_BODIES=all to widen capture, then reload."
                    ),
                }
            limit = min(max_chars or app.s.max_text_chars, app.s.max_text_chars * 2)
            meta = {
                "ok": True,
                **entry.provenance(),
                "request_id": entry.id,
                "url": entry.url,
                "status": entry.status,
                "mime_type": entry.mime_type,
                "bytes": entry.body_bytes,
                "body_truncated_at_capture": entry.body_truncated,
            }
            if entry.body_is_base64:
                encoded = base64.b64encode(data).decode()
                return {**meta, "encoding": "base64", **clip(encoded, limit, offset=offset, label="body")}
            if not raw:
                parsed = try_parse_json(data)
                if parsed is not None:
                    # Parsed JSON still has to respect the output budget: a 2 MB
                    # API response returned whole would cost far more in agent
                    # context than the browser time that fetched it.
                    rendered = json.dumps(parsed, ensure_ascii=False, default=str)
                    if len(rendered) <= limit:
                        return {**meta, "encoding": "json", "json": parsed}
                    return {
                        **meta,
                        "encoding": "json_truncated",
                        "note": (
                            f"The parsed JSON is {len(rendered)} chars, over the "
                            f"{limit} limit, so it is returned as text. Page through it "
                            "with offset, or narrow it with page_eval on the live object."
                        ),
                        **clip(rendered, limit, offset=offset, label="body"),
                    }
            return {
                **meta,
                "encoding": "text",
                **clip(data.decode("utf-8", "replace"), limit, offset=offset, label="body"),
            }

    @mcp.tool(
        description=(
            "WebSocket connections and their frames in both directions. Omit "
            "`ws_id` to list connections, pass it to read frames."
        )
    )
    @guarded
    async def net_websocket(
        lease: str,
        ws_id: str | None = None,
        contains: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """
        Args:
            lease: Lease token.
            ws_id: Connection id from the list, e.g. 'ws1'.
            contains: Only frames whose payload contains this substring.
            limit: Maximum frames to return.
        """
        async with use_lease(lease, exclusive=False) as (obj, session):
            if ws_id is None:
                return {
                    "ok": True,
                    "websockets": [
                        {
                            "id": ws.id,
                            "url": ws.url,
                            "open": ws.closed_at is None,
                            "frames": len(ws.frames),
                            "error": ws.error,
                        }
                        for ws in session.netlog.websockets()
                    ],
                }
            ws = session.netlog.websocket(ws_id)
            if ws is None:
                raise NotFound(f"no websocket {ws_id!r} on this lease")
            frames = [
                {
                    "ts": f.ts,
                    "direction": f.direction,
                    "payload": f.payload,
                    "truncated": f.truncated,
                }
                for f in ws.frames
                if not contains or contains.lower() in f.payload.lower()
            ]
            return {
                "ok": True,
                "id": ws.id,
                "url": ws.url,
                "open": ws.closed_at is None,
                "frame_count": len(ws.frames),
                "frames": frames[-limit:],
            }

    @mcp.tool(
        description=(
            "Export everything captured to a HAR 1.2 file on the gateway host, so "
            "it can be opened in existing HTTP tooling. WebSocket frames are "
            "included under a _websockets extension. Selected-request simulations "
            "are marked per entry with _sourceKind and _networkIntervention."
        )
    )
    @guarded
    async def net_har(lease: str, include_bodies: bool = True) -> dict[str, Any]:
        """
        Args:
            lease: Lease token.
            include_bodies: Embed captured response bodies in the HAR.
        """
        async with use_lease(lease, exclusive=False) as (obj, session):
            path = session.dir / f"capture-{int(obj.age_seconds)}s.har"
            result = await session.netlog.export_har(path, include_bodies=include_bodies)
            return {"ok": True, **result, "observability": OBSERVABILITY_NOTE}

    @mcp.tool(
        description=(
            "Discard captured network data for this lease and free its disk. Handy "
            "before a specific action so the next net_requests shows only that "
            "action's traffic."
        )
    )
    @guarded
    async def net_clear(lease: str) -> dict[str, Any]:
        """
        Args:
            lease: Lease token.
        """
        async with use_lease(lease, exclusive=False) as (obj, session):
            cleared = session.netlog.clear()
            return {"ok": True, "cleared_entries": cleared}
