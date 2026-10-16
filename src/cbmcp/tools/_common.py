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
from ..errors import CbmcpError, InvalidArgument, LeaseNotActive
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


@asynccontextmanager
async def use_lease(lease: str, *, exclusive: bool = True, timeout: float = 180.0):
    """Resolve a lease token and hold its guard for the duration of a tool call."""
    app = get_app()
    obj: Lease = app.P.resolve_token(lease)
    async with obj.guard(exclusive=exclusive, timeout=timeout):
        if obj.session is None:
            raise LeaseNotActive(f"lease {obj.lease_id} has no live browser")
        yield obj, obj.session


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
