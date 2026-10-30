"""Reading the page: snapshot, text, HTML, screenshot, evaluate, console."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import Image, MCPServer
from playwright.async_api import Error as PwError

from ..app import get_app
from ._common import clip, guarded, page_condition_flags, use_lease

SNAPSHOT_DOC = """Structured snapshot of the page with stable element refs.

This is the main way to see a page. Each interactive element gets an opaque ref
such as [ref=e3_7], unique to its live document. Keep it unchanged when passing
it to page_click / page_type / page_select. Prefer it over a screenshot:
it is far cheaper in context and gives you refs you can act on.

For security work it deliberately includes things a visual or accessibility
snapshot would drop: hidden input fields with their values (CSRF tokens, signed
state), form action/method/enctype, and field name/type/pattern attributes.

Pass `ref` to get a full dump of one element instead -- every attribute, its
form context with sibling field values, and (with debug='full') the JavaScript
event listeners bound to it with their source locations.

Use frames_only=true for a lightweight child-frame directory. Follow
next_frame_offset with frame_offset; totals, empty/failed frames and omitted
counts are explicit. Use a returned frame_ref (the iframe element ref) to read
one iframe's document, including cross-origin frames reachable by the driver.
Refs inside that document work with the same page_click/page_type tools.
"""


def register(mcp: MCPServer) -> None:
    @mcp.tool(description=SNAPSHOT_DOC)
    @guarded
    async def page_snapshot(
        lease: str,
        page: str | None = None,
        ref: str | None = None,
        include_hidden: bool = False,
        max_nodes: int = 1500,
        frame_ref: str | None = None,
        frames_only: bool = False,
        frame_offset: int = 0,
        frame_limit: int = 8,
    ) -> dict[str, Any]:
        """
        Args:
            lease: Lease token.
            page: Tab id. Defaults to the active tab.
            ref: Dump this single element in full instead of the whole page.
            include_hidden: Include elements that are not visible (form fields
                are always included regardless).
            max_nodes: Cap on nodes in the output.
            frame_ref: Iframe/frame element ref from this page's snapshot/directory;
                inspect that document instead of the main document. Not a tab id.
            frames_only: Return only the child-frame directory, without DOM bodies.
            frame_offset: Child-frame directory item offset; follow next_frame_offset.
            frame_limit: Child frames per response,1..50 (default8). Does not increase
                the per-call text budget; inspect an individual frame for detail.
        """
        app = get_app()
        async with use_lease(lease) as (obj, session):
            handle = session.handle(page)
            if ref:
                if frame_ref or frames_only or frame_offset:
                    from ..errors import InvalidArgument
                    raise InvalidArgument('Use ref for one control OR frame_ref/directory options for a frame snapshot.')
                return {"ok": True, "page": handle.id, "element": await session.element_detail(handle, ref)}

            result = await session.snapshot(
                handle, include_hidden=include_hidden, max_nodes=max_nodes,
                frame_ref=frame_ref, frames_only=frames_only,
                frame_offset=frame_offset, frame_limit=frame_limit,
            )
            lines = result.pop("lines", "")
            payload: dict[str, Any] = {
                "ok": True,
                "page": handle.id,
                "url": result.get("url"),
                "title": result.get("title"),
                "ready_state": result.get("readyState"),
                "node_count": result.get("nodes"),
                **clip(lines, app.s.max_snapshot_chars, label="snapshot"),
                **(page_condition_flags(session, handle, url=result.get("url"))
                   if result.get('root_frame_is_main') and not frames_only else {}),
            }
            if payload.get('truncated'):
                payload['hint'] = 'Snapshot text clipped; use frame_ref for the needed iframe or ref for one element. Text next_offset is not a page_snapshot input.'
            if result.get("truncated"):
                payload["node_limit_reached"] = True
                payload["hint"] = (
                    "Node limit reached. Raise max_nodes, or narrow the area of interest "
                    "with page_snapshot(ref=...) on a container."
                )
            for key in ('child_frame_total', 'frame_offset', 'next_frame_offset', 'has_more_frames',
                        'frames_not_in_response', 'frame_directory_changed', 'directory_only',
                        'root_frame_ref', 'root_frame_is_main'):
                payload[key] = result.get(key)
            payload['frames'] = []
            remaining = app.s.max_snapshot_chars
            for frame in result.get('frames', []):
                row = {k:v for k,v in frame.items() if k not in {'lines','nodes','truncated'}}
                if 'lines' in frame:
                    row.update(clip(frame['lines'], min(6000, remaining), label='snapshot'))
                    remaining = max(0, remaining - len(row['snapshot']))
                    row['node_count'] = frame.get('nodes')
                    if frame.get('truncated'):
                        row['node_limit_reached'] = True
                    if row.get('truncated') or row.get('node_limit_reached'):
                        row['hint'] = 'Use page_snapshot(frame_ref=<this iframe ref>) for its document, then ref for a specific control; this is not complete frame content.'
                payload['frames'].append(row)
            payload['frame_errors_in_response'] = sum(f.get('status')=='error' or bool(f.get('frame_ref_error')) for f in payload['frames'])
            payload['frame_note'] = 'frame_ref identifies the iframe element; ref identifies a control. Follow next_frame_offset for more children. Empty/error/truncated are not absence. Refs are document-scoped and must be refreshed after document replacement.'
            return payload

    @mcp.tool(
        description=(
            "Visible text of the page, main-content first. Cheaper than a snapshot "
            "when you only need to read. Paginate with `offset`."
        )
    )
    @guarded
    async def page_text(
        lease: str,
        page: str | None = None,
        selector: str | None = None,
        max_chars: int | None = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        """
        Args:
            lease: Lease token.
            page: Tab id.
            selector: Restrict extraction to this CSS selector.
            max_chars: Characters to return (server capped).
            offset: Start position, for reading a long page in windows.
        """
        app = get_app()
        async with use_lease(lease) as (obj, session):
            handle = session.handle(page)
            result = await session.page_text(handle, selector)
            limit = min(max_chars or app.s.max_text_chars, app.s.max_text_chars)
            return {
                "ok": True,
                "page": handle.id,
                "url": result.get("url"),
                "title": result.get("title"),
                **clip(result.get("text") or "", limit, offset=offset),
                **page_condition_flags(session, handle, url=result.get("url")),
            }

    @mcp.tool(
        description=(
            "Raw HTML of the page or of one element. Use it to inspect markup the "
            "snapshot summarises away -- inline scripts, comments, data attributes."
        )
    )
    @guarded
    async def page_html(
        lease: str,
        page: str | None = None,
        selector: str | None = None,
        ref: str | None = None,
        max_chars: int | None = None,
        offset: int = 0,
    ) -> dict[str, Any]:
        """
        Args:
            lease: Lease token.
            page: Tab id.
            selector: Return this element's outerHTML.
            ref: Return this ref's outerHTML.
            max_chars: Characters to return (server capped).
            offset: Start position for pagination.
        """
        app = get_app()
        async with use_lease(lease) as (obj, session):
            handle = session.handle(page)
            session.ensure_runnable(handle)
            if ref or selector:
                locator = await session.locator_for(
                    handle, ref=ref, selector=selector, text=None
                )
                html = await locator.evaluate("el => el.outerHTML")
            else:
                html = await handle.page.content()
            limit = min(max_chars or app.s.max_text_chars, app.s.max_text_chars * 3)
            return {
                "ok": True,
                "page": handle.id,
                "url": handle.page.url,
                **clip(html or "", limit, offset=offset, label="html"),
                **page_condition_flags(
                    session, handle, html=None if (ref or selector) else (html or "")
                ),
            }

    @mcp.tool(
        description=(
            "Screenshot the page or one element. Use it for visual checks and "
            "CAPTCHA/challenge inspection; use page_snapshot to decide what to click."
        )
    )
    @guarded
    async def page_screenshot(
        lease: str,
        page: str | None = None,
        full_page: bool = False,
        ref: str | None = None,
        inline: bool = True,
    ) -> Any:
        """
        Args:
            lease: Lease token.
            page: Tab id.
            full_page: Capture the whole scrollable page.
            ref: Screenshot just this element.
            inline: Return the image itself. Set false to only save it and get a path.
        """
        async with use_lease(lease) as (obj, session):
            handle = session.handle(page)
            shot = await session.screenshot(handle, full_page=full_page, ref=ref)
            data = shot.pop("data")
            meta = {"ok": True, "page": handle.id, **shot}
            if not inline:
                return meta
            if shot["too_large_to_inline"]:
                meta["note"] = (
                    "Image too large to inline; it was saved to disk. Retry with "
                    "full_page=false or ref=<element> for a smaller capture."
                )
                return meta
            fmt = "jpeg" if shot["format"] == "jpeg" else "png"
            return [meta, Image(data=data, format=fmt)]

    @mcp.tool(
        description=(
            "Run JavaScript in the page and return the result. Multi-statement code "
            "is wrapped automatically; promises are awaited. For values trapped in a "
            "closure, use a breakpoint plus js_eval_frame instead -- this runs in "
            "global scope and cannot see them."
        )
    )
    @guarded
    async def page_eval(
        lease: str,
        expression: str,
        page: str | None = None,
        timeout_ms: int = 15_000,
        max_chars: int | None = None,
    ) -> dict[str, Any]:
        """
        Args:
            lease: Lease token.
            expression: JavaScript expression, or a function body / arrow function.
            page: Tab id.
            timeout_ms: Evaluation timeout.
            max_chars: Cap on the serialised result.
        """
        app = get_app()
        async with use_lease(lease) as (obj, session):
            handle = session.handle(page)
            result = await session.evaluate(handle, expression, timeout_ms=timeout_ms)
            if not result.get("ok"):
                result["hint"] = (
                    "For multiple statements use an arrow function: "
                    "'() => { const a = 1; return a; }'. DOM nodes and circular "
                    "objects cannot be returned -- return a plain value instead."
                )
                return result
            value = result["value"]
            if isinstance(value, str):
                limit = min(max_chars or app.s.max_text_chars, app.s.max_text_chars)
                return {"ok": True, "page": handle.id, "type": "str", **clip(value, limit, label="value")}
            return {"ok": True, "page": handle.id, "value": value, "type": result.get("type")}

    @mcp.tool(
        description=(
            "Console messages and uncaught JavaScript errors with stack traces. "
            "Errors are the quickest signal that a page broke, or that your own "
            "injected code did."
        )
    )
    @guarded
    async def page_console(
        lease: str,
        page: str | None = None,
        types: list[str] | None = None,
        contains: str | None = None,
        limit: int = 50,
        errors_only: bool = False,
    ) -> dict[str, Any]:
        """
        Args:
            lease: Lease token.
            page: Tab id. Omit to cover every tab.
            types: Filter by console type, e.g. ['error', 'warning'].
            contains: Only messages containing this substring.
            limit: Maximum messages to return.
            errors_only: Return only uncaught JS errors.
        """
        async with use_lease(lease, exclusive=False) as (obj, session):
            handles = (
                [session.handle(page)]
                if page
                else [h for h in session.pages.values() if not h.closed]
            )
            messages: list[dict[str, Any]] = []
            errors: list[dict[str, Any]] = []
            for handle in handles:
                errors.extend(handle.errors)
                if errors_only:
                    continue
                for entry in handle.console:
                    if types and entry.get("type") not in types:
                        continue
                    if contains and contains.lower() not in (entry.get("text") or "").lower():
                        continue
                    messages.append(entry)
            messages.sort(key=lambda e: e.get("ts", 0))
            errors.sort(key=lambda e: e.get("ts", 0))
            return {
                "ok": True,
                "js_errors": errors[-limit:],
                "js_error_count": len(errors),
                "console": messages[-limit:] if not errors_only else [],
                "console_count": len(messages),
            }

    @mcp.tool(
        description=(
            "Inventory of what the page loads and stores: script sources (with "
            "integrity/crossorigin), form targets and field names, localStorage and "
            "sessionStorage contents, iframes with sandbox flags, meta CSP, and "
            "recently added window globals. A fast orientation pass on a new target."
        )
    )
    @guarded
    async def page_inventory(lease: str, page: str | None = None) -> dict[str, Any]:
        """
        Args:
            lease: Lease token.
            page: Tab id.
        """
        async with use_lease(lease) as (obj, session):
            handle = session.handle(page)
            data = await session.inventory(handle)
            try:
                headers: dict[str, Any] = {}
                entries = session.netlog.query(limit=30, newest_first=True)
                for entry in entries:
                    if entry.is_navigation and entry.response_headers:
                        headers = {
                            k: v
                            for k, v in entry.response_headers.items()
                            if k.lower()
                            in (
                                "content-security-policy",
                                "strict-transport-security",
                                "x-frame-options",
                                "x-content-type-options",
                                "referrer-policy",
                                "permissions-policy",
                                "set-cookie",
                                "access-control-allow-origin",
                                "server",
                            )
                        }
                        break
                if headers:
                    data["security_headers"] = headers
            except PwError:
                pass
            return {"ok": True, "page": handle.id, **data}
