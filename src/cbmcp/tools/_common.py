"""Shared helpers for the MCP tool layer.

Three conventions apply to every tool:

**Errors are returned, not raised.** A structured ``{"ok": false, "error":
"...", "hint": "..."}`` tells the model what to do next; an MCP protocol error
tells it only that something broke.

**Every browser tool takes ``lease``.** That token is the capability. There is
no "current browser" fallback -- with Claude Code subagents sharing one MCP
connection, an implicit default would silently hand one agent another's browser.

**Output is bounded.** Page text, bodies and snapshots are clipped with an
explicit marker and a continuation hint. Oversized output costs the agent's
context, which in practice is more expensive than the browser time.
"""

from __future__ import annotations

import functools
import json
from contextlib import asynccontextmanager
from typing import Any

from ..app import get_app
from ..errors import (
    BrowserConnectionError,
    BrowserConnectionLost,
    CbmcpError,
    InvalidArgument,
    LeaseNotActive,
)
from ..lease import Lease
from ..log import get_logger
from ..session import BrowserSession

log = get_logger("tools")


def guarded(fn):
    """Turn exceptions into structured, actionable tool results."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any):
        try:
            return await fn(*args, **kwargs)
        except CbmcpError as exc:
            return exc.to_dict()
        except TimeoutError:
            return {
                "ok": False,
                "error": "tool_timeout",
                "message": "the operation timed out",
                "hint": "Retry with a larger timeout_ms, or check the page is not paused.",
            }
        except Exception as exc:  # noqa: BLE001 - a tool must not kill the server
            log.exception("unhandled error in %s", fn.__name__)
            return {
                "ok": False,
                "error": "internal_error",
                "message": f"{type(exc).__name__}: {exc}"[:400],
            }

    return wrapper


_DROP_MARKERS = (
    "has been closed",
    "connection closed",
    "target closed",
    "browser closed",
    "websocket",
    "disconnected",
)


def _is_connection_drop(exc: BaseException) -> bool:
    if type(exc).__module__.split(".")[0] != "playwright":
        return False
    text = str(exc).lower()
    return any(marker in text for marker in _DROP_MARKERS)


@asynccontextmanager
async def use_lease(lease: str, *, exclusive: bool = True, timeout: float = 180.0):
    """Resolve a lease token and hold its guard for the duration of a tool call."""
    app = get_app()
    obj: Lease = app.P.resolve_token(lease)
    if obj.session is not None and obj.state == "active" and not obj.session.is_connected():
        # Connection dropped since the last call: restore it before acting
        # rather than failing the call (and the agent's task).
        if not await app.P.reconnect(obj, why="tool call"):
            raise BrowserConnectionError(
                f"lease {obj.lease_id}: the connection to the cloud browser was lost and "
                "could not be restored",
                hint="Release this lease and acquire a new browser.",
            )
    async with obj.guard(exclusive=exclusive, timeout=timeout):
        if obj.session is None:
            raise LeaseNotActive(f"lease {obj.lease_id} has no live browser")
        session = obj.session
        try:
            yield obj, session
        except Exception as exc:
            # The websocket to the cloud browser went away mid-call. The browser
            # itself usually still runs; the next call reconnects. (A closed
            # page with the connection intact is a different error -- let it
            # through as is.)
            if _is_connection_drop(exc) and not session.is_connected():
                raise BrowserConnectionLost(
                    f"the connection to the cloud browser dropped: {str(exc)[:200]}"
                ) from exc
            raise


def clip(text: str, limit: int, *, offset: int = 0, label: str = "text") -> dict[str, Any]:
    """Clip with enough information for the agent to fetch the next window."""
    total = len(text)
    start = max(0, min(offset, total))
    chunk = text[start : start + limit]
    out: dict[str, Any] = {label: chunk, "total_chars": total, "offset": start}
    end = start + len(chunk)
    if end < total:
        out["truncated"] = True
        out["next_offset"] = end
        out["hint"] = f"{total - end} more characters; call again with offset={end}."
    return out


def page_arg(session: BrowserSession, page: str | None):
    return session.handle(page)


def page_condition_flags(
    session: BrowserSession,
    handle: Any,
    *,
    url: str | None = None,
    html: str | None = None,
    response_missing: bool = False,
) -> dict[str, Any]:
    """What a person would notice about the page that its content hides.

    * The browser showing its OWN error page (``chrome-error://``): measured, a
      403 with an empty body made page_text return Chrome's "HTTP ERROR 403"
      text and net_body a 180 KB Chrome page -- easy to record as the site's.
      Pass ``url`` as read inside the page (``location.href``) when available:
      right after a navigation Playwright still reports the requested URL.
    * The site answering 403/429/503 in bulk (rate limiting / blocking).
    """
    out: dict[str, Any] = {}
    url = url or getattr(handle.page, "url", "") or ""
    nav = session.netlog.last_navigation()
    is_error = url.startswith("chrome-error://") or (
        html is not None and 'id="main-frame-error"' in html[:400_000]
    )
    # Measured: a navigation answered 403 with an empty body yields no response
    # object at all, while the network log has the 403 -- the browser has
    # replaced the page with its own error page.
    if response_missing and nav is not None and (
        (nav.status or 0) >= 400 or nav.failure
    ):
        is_error = True
    if is_error:
        out["browser_error_page"] = True
        if nav is not None:
            out["status"] = nav.status
            out["failed_url"] = nav.url
            if nav.failure:
                out["network_error"] = nav.failure
        out["error_page_note"] = (
            "The browser is showing its OWN error page"
            + (f" for HTTP {nav.status}" if nav is not None and nav.status else "")
            + ", not the site's content: the site sent no usable body or the request "
            "failed. page_text / page_html / page_snapshot return Chrome's error text "
            "here; do not record it as the site's response."
        )
    hint = session.netlog.throttle_hint()
    if hint:
        out["rate_limit_warning"] = hint
    return out


def require_one(**options: Any) -> None:
    supplied = [k for k, v in options.items() if v not in (None, "", [])]
    if not supplied:
        raise InvalidArgument(
            f"pass one of: {', '.join(options)}",
            hint="Element refs come from page_snapshot and are the most reliable.",
        )


def as_json_text(value: Any, limit: int = 20_000) -> str:
    try:
        text = json.dumps(value, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(value)
    if len(text) > limit:
        return text[:limit] + f"\n... [truncated, {len(text) - limit} more chars]"
    return text


def try_parse_json(data: bytes) -> Any | None:
    """Agents reason far better about a parsed API response than about raw text."""
    try:
        return json.loads(data.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        return None
