"""Network recorder.

What this can and cannot see -- stated plainly, because conflating CDP network
observation with packet capture leads to wrong conclusions:

CAN see
    Request URL / method / headers / query / post body; response status /
    headers / body; redirect chains; failures with Chromium error text;
    resource type (xhr, fetch, document, script, ...); WebSocket frames.

CANNOT see
    Anything before the recorder attached (so attach happens before the first
    navigation); TLS handshakes, certificates, HTTP/2 or QUIC frame detail;
    traffic from other processes; requests made by extensions.

LIMITED
    Service Worker and cross-process iframe traffic depends on Chromium's
    auto-attach and may be partial. Response bodies are pulled from the
    browser's buffer as responses arrive -- after a navigation the buffer for
    the old document is gone, which is why bodies are fetched eagerly and
    spilled to disk rather than read on demand.

Bodies never accumulate in memory: metadata lives in a bounded ring buffer,
payloads go to per-lease files under a byte quota with oldest-first eviction.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlparse

from .log import get_logger

log = get_logger("netlog")

_TEXTUAL = (
    "application/json",
    "application/javascript",
    "application/x-javascript",
    "application/xml",
    "application/xhtml",
    "application/graphql",
    "application/x-www-form-urlencoded",
    "application/ld+json",
    "text/",
    "+json",
    "+xml",
)

_INTERESTING_TYPES = {"xhr", "fetch", "document", "script", "websocket", "manifest", "other"}

# Attribute used to tag a Playwright Request with its log entry id.
_REQUEST_TAG = "_cbmcp_entry_id"

_SENSITIVE_HEADERS = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "x-auth-token",
}


def _is_textual(content_type: str) -> bool:
    ct = (content_type or "").lower()
    return any(marker in ct for marker in _TEXTUAL)


@dataclass
class NetEntry:
    id: str
    started_at: float
    method: str
    url: str
    resource_type: str
    frame_url: str | None = None
    is_navigation: bool = False
    request_headers: dict[str, str] = field(default_factory=dict)
    query: list[tuple[str, str]] = field(default_factory=list)
    post_data: str | None = None
    post_data_truncated: bool = False
    status: int | None = None
    status_text: str | None = None
    response_headers: dict[str, str] = field(default_factory=dict)
    mime_type: str | None = None
    body_path: Path | None = None
    body_bytes: int = 0
    body_truncated: bool = False
    body_error: str | None = None
    body_is_base64: bool = False
    from_cache: bool = False
    failure: str | None = None
    ended_at: float | None = None
    redirected_from: str | None = None
    server_ip: str | None = None

    @property
    def duration_ms(self) -> int | None:
        if self.ended_at is None:
            return None
        return int((self.ended_at - self.started_at) * 1000)

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "method": self.method,
            "url": self.url if len(self.url) <= 300 else self.url[:300] + "...",
            "type": self.resource_type,
            "status": self.status,
            "mime": self.mime_type,
            "size": self.body_bytes or None,
            "ms": self.duration_ms,
            "failed": self.failure,
            "has_body": bool(self.body_path),
            "has_post_data": bool(self.post_data),
        }

    def detail(self, redact: bool) -> dict[str, Any]:
        def headers(src: dict[str, str]) -> dict[str, str]:
            if not redact:
                return src
            return {
                k: ("<redacted>" if k.lower() in _SENSITIVE_HEADERS else v)
                for k, v in src.items()
            }

        return {
            "id": self.id,
            "method": self.method,
            "url": self.url,
            "type": self.resource_type,
            "is_navigation": self.is_navigation,
            "frame_url": self.frame_url,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "request_headers": headers(self.request_headers),
            "query_params": [{"name": k, "value": v} for k, v in self.query],
            "post_data": self.post_data,
            "post_data_truncated": self.post_data_truncated,
            "status": self.status,
            "status_text": self.status_text,
            "response_headers": headers(self.response_headers),
            "mime_type": self.mime_type,
            "from_cache": self.from_cache,
            "server_ip": self.server_ip,
            "redirected_from": self.redirected_from,
            "failure": self.failure,
            "body": {
                "available": bool(self.body_path),
                "bytes": self.body_bytes,
                "truncated": self.body_truncated,
                "base64": self.body_is_base64,
                "error": self.body_error,
                "read_with": f"net_body(id='{self.id}')" if self.body_path else None,
            },
        }


@dataclass
class Initiator:
    """Which JavaScript caused a request to be sent.

    Answers "who called this API" without setting a breakpoint, which is
    normally the first thing you want to know about an interesting request and
    otherwise costs a breakpoint plus a repeat of the action.
    """

    url: str
    method: str
    wall_time: float
    kind: str  # parser | script | preload | SignedExchange | other
    stack: list[dict[str, Any]] = field(default_factory=list)
    source_url: str | None = None  # for parser-initiated requests

    def public(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.kind}
        if self.source_url:
            out["initiated_by_document"] = self.source_url
        if self.stack:
            out["call_stack"] = self.stack
            top = self.stack[0]
            out["origin"] = (
                f"{top.get('function') or '(anonymous)'} at "
                f"{top.get('url') or '<inline>'}:{top.get('line')}"
            )
            out["break_here"] = (
                "js_breakpoint(action='set', kind='url', "
                f"url_pattern={_escape_regex(top.get('url') or '')!r}, "
                f"line={top.get('line')})"
                if top.get("url")
                else None
            )
        return out


def _escape_regex(url: str) -> str:
    import re as _re

    return _re.escape(url)


@dataclass
class WsFrame:
    ts: float
    direction: str  # sent | received
    opcode: int
    payload: str
    truncated: bool


@dataclass
class WsEntry:
    id: str
    url: str
    opened_at: float
    closed_at: float | None = None
    frames: deque[WsFrame] = field(default_factory=lambda: deque(maxlen=500))
    error: str | None = None


class NetworkRecorder:
    """Attaches to a Playwright BrowserContext and records everything it sees."""

    def __init__(
        self,
        *,
        lease_dir: Path,
        max_entries: int,
        max_body_bytes: int,
        disk_quota_bytes: int,
        capture_bodies: str = "interesting",
        redact_sensitive_headers: bool = False,
    ) -> None:
        self.dir = lease_dir / "net"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.max_entries = max_entries
        self.max_body_bytes = max_body_bytes
        self.disk_quota_bytes = disk_quota_bytes
        self.capture_bodies = capture_bodies
        self.redact = redact_sensitive_headers

        self._entries: OrderedDict[str, NetEntry] = OrderedDict()
        self._by_request: dict[int, str] = {}
        self._ws: OrderedDict[str, WsEntry] = OrderedDict()
        self._initiators: deque[Initiator] = deque(maxlen=1000)
        self._seq = 0
        self._ws_seq = 0
        self._disk_used = 0
        self._tasks: set[asyncio.Task] = set()
        self.dropped = 0
        self.blocked: deque[dict[str, Any]] = deque(maxlen=200)

    # ----- lifecycle -------------------------------------------------------

    def attach(self, context: Any) -> None:
        context.on("request", self._on_request)
        context.on("response", self._on_response)
        context.on("requestfailed", self._on_request_failed)
        context.on("requestfinished", self._on_request_finished)

    def attach_page(self, page: Any) -> None:
        page.on("websocket", self._on_websocket)

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ----- event handlers --------------------------------------------------

    def _next_id(self) -> str:
        self._seq += 1
        return f"r{self._seq}"

    def _tag_request(self, request: Any, entry_id: str) -> None:
        """Remember which entry a request belongs to.

        The id is stashed on the request object itself. Keying a dict on
        ``id(request)`` looks equivalent but is not: once a request is garbage
        collected a later one can be handed the same address, and its response
        would then be written onto the earlier entry. The dict is kept only as a
        fallback for objects that refuse attributes, and is bounded.
        """
        try:
            setattr(request, _REQUEST_TAG, entry_id)
            return
        except Exception:  # noqa: BLE001 - e.g. a __slots__ class
            self._by_request[id(request)] = entry_id
            if len(self._by_request) > self.max_entries * 2:
                for key in list(self._by_request)[: len(self._by_request) // 2]:
                    self._by_request.pop(key, None)

    def _entry_of(self, request: Any) -> NetEntry | None:
        entry_id = getattr(request, _REQUEST_TAG, None)
        if entry_id is None:
            entry_id = self._by_request.get(id(request))
        return self._entries.get(entry_id) if entry_id else None

    def _on_request(self, request: Any) -> None:
        try:
            entry_id = self._next_id()
            url = request.url
            parsed = urlparse(url)
            post: str | None = None
            truncated = False
            try:
                post = request.post_data
            except Exception:  # noqa: BLE001 - binary or unavailable payloads
                post = None
            if post and len(post) > self.max_body_bytes:
                post = post[: self.max_body_bytes]
                truncated = True

            redirected_from = None
            with contextlib.suppress(Exception):
                prev = request.redirected_from
                if prev is not None:
                    redirected_from = prev.url

            entry = NetEntry(
                id=entry_id,
                started_at=time.time(),
                method=request.method,
                url=url,
                resource_type=request.resource_type,
                is_navigation=bool(getattr(request, "is_navigation_request", lambda: False)()),
                request_headers=dict(request.headers or {}),
                query=list(parse_qsl(parsed.query, keep_blank_values=True)),
                post_data=post,
                post_data_truncated=truncated,
                redirected_from=redirected_from,
            )
            with contextlib.suppress(Exception):
                frame = request.frame
                entry.frame_url = frame.url if frame else None

            self._entries[entry_id] = entry
            self._tag_request(request, entry_id)
            self._evict_entries()
        except Exception as exc:  # noqa: BLE001 - a recorder must never break the page
            log.debug("request hook failed: %s", exc)

    def _on_response(self, response: Any) -> None:
        try:
            entry = self._entry_of(response.request)
            if entry is None:
                return
            entry.status = response.status
            entry.status_text = response.status_text
            entry.response_headers = dict(response.headers or {})
            entry.mime_type = entry.response_headers.get("content-type", "")
            # server_addr() and body() are both coroutines, and this handler is
            # synchronous, so the async follow-up runs as its own task.
            self._spawn(self._finalise_response(entry, response))
        except Exception as exc:  # noqa: BLE001
            log.debug("response hook failed: %s", exc)

    async def _finalise_response(self, entry: NetEntry, response: Any) -> None:
        try:
            addr = await response.server_addr()
            if addr:
                entry.server_ip = addr.get("ipAddress")
        except Exception:  # noqa: BLE001 - not available for cached or failed loads
            pass
        if self._wants_body(entry):
            await self._capture_body(entry, response)

    def _on_request_failed(self, request: Any) -> None:
        entry = self._entry_of(request)
        if entry is None:
            return
        entry.ended_at = time.time()
        with contextlib.suppress(Exception):
            entry.failure = (request.failure or "failed") if request.failure else "failed"

    def _on_request_finished(self, request: Any) -> None:
        entry = self._entry_of(request)
        if entry is not None and entry.ended_at is None:
            entry.ended_at = time.time()

    def _on_websocket(self, ws: Any) -> None:
        self._ws_seq += 1
        entry = WsEntry(id=f"ws{self._ws_seq}", url=ws.url, opened_at=time.time())
        self._ws[entry.id] = entry
        while len(self._ws) > 50:
            self._ws.popitem(last=False)

        cap = 4096

        def record(direction: str, payload: Any) -> None:
            text = payload if isinstance(payload, str) else base64.b64encode(payload).decode()
            truncated = len(text) > cap
            entry.frames.append(
                WsFrame(
                    ts=time.time(),
                    direction=direction,
                    opcode=1 if isinstance(payload, str) else 2,
                    payload=text[:cap],
                    truncated=truncated,
                )
            )

        ws.on("framesent", lambda p: record("sent", p))
        ws.on("framereceived", lambda p: record("received", p))
        ws.on("close", lambda _=None: setattr(entry, "closed_at", time.time()))
        ws.on("socketerror", lambda e: setattr(entry, "error", str(e)[:200]))

    # ----- body capture ----------------------------------------------------

    def _wants_body(self, entry: NetEntry) -> bool:
        if self.capture_bodies == "none":
            return False
        if entry.status is not None and 300 <= entry.status < 400:
            return False  # redirects have no retrievable body
        if self.capture_bodies == "all":
            return True
        if entry.resource_type in _INTERESTING_TYPES:
            return True
        return _is_textual(entry.mime_type or "")

    async def _capture_body(self, entry: NetEntry, response: Any) -> None:
        try:
            raw = await response.body()
        except Exception as exc:  # noqa: BLE001
            entry.body_error = f"unavailable: {type(exc).__name__}"
            return
        if raw is None:
            entry.body_error = "empty"
            return
        data = raw
        if len(data) > self.max_body_bytes:
            data = data[: self.max_body_bytes]
            entry.body_truncated = True

        textual = _is_textual(entry.mime_type or "")
        path = self.dir / f"{entry.id}.bin"
        try:
            await asyncio.to_thread(path.write_bytes, data)
        except OSError as exc:
            entry.body_error = f"write failed: {exc.strerror}"
            return
        if self._entries.get(entry.id) is not entry:
            # Evicted from the ring buffer (or net_clear ran) while the body was
            # in flight; nothing references this file any more.
            with contextlib.suppress(OSError):
                path.unlink()
            return
        entry.body_path = path
        entry.body_bytes = len(data)
        entry.body_is_base64 = not textual
        self._disk_used += len(data)
        self._enforce_quota()

    def _enforce_quota(self) -> None:
        if self._disk_used <= self.disk_quota_bytes:
            return
        for entry in self._entries.values():
            if self._disk_used <= self.disk_quota_bytes * 0.8:
                break
            if entry.body_path and entry.body_path.exists():
                with contextlib.suppress(OSError):
                    size = entry.body_path.stat().st_size
                    entry.body_path.unlink()
                    self._disk_used -= size
                entry.body_path = None
                entry.body_error = "evicted (lease disk quota reached)"

    def _evict_entries(self) -> None:
        while len(self._entries) > self.max_entries:
            _, old = self._entries.popitem(last=False)
            self.dropped += 1
            if old.body_path and old.body_path.exists():
                with contextlib.suppress(OSError):
                    self._disk_used -= old.body_path.stat().st_size
                    old.body_path.unlink()

    # ----- queries ---------------------------------------------------------

    def record_initiator(self, params: dict[str, Any]) -> None:
        """Handle a CDP ``Network.requestWillBeSent`` event.

        Playwright's own request events do not carry the initiator, so this
        comes from a raw CDP session alongside them. The two are joined on
        method+URL at read time rather than by requestId, because Playwright
        does not expose Chromium's requestId.
        """
        try:
            request = params.get("request") or {}
            initiator = params.get("initiator") or {}
            frames: list[dict[str, Any]] = []

            stack = initiator.get("stack")
            depth = 0
            while stack and depth < 4:
                for frame in (stack.get("callFrames") or [])[:12]:
                    frames.append(
                        {
                            "function": frame.get("functionName") or "(anonymous)",
                            "url": frame.get("url") or None,
                            "line": int(frame.get("lineNumber", 0)) + 1,
                            "column": frame.get("columnNumber"),
                        }
                    )
                stack = stack.get("parent")
                depth += 1
                if stack and frames:
                    frames.append({"function": "--- async boundary ---", "url": None, "line": 0})

            self._initiators.append(
                Initiator(
                    url=str(request.get("url") or ""),
                    method=str(request.get("method") or "GET").upper(),
                    wall_time=float(params.get("wallTime") or time.time()),
                    kind=str(initiator.get("type") or "other"),
                    stack=frames,
                    source_url=initiator.get("url"),
                )
            )
        except Exception as exc:  # noqa: BLE001 - never break on telemetry
            log.debug("initiator hook failed: %s", exc)

    def initiator_for(self, entry: NetEntry) -> dict[str, Any] | None:
        """Join a Playwright entry to its CDP initiator.

        Matching on timestamps looks obvious and is wrong: CDP ``wallTime``
        comes from the *browser host's* clock, and a cloud browser can be tens
        of seconds off this machine (18s observed in testing). Both sequences
        arrive in request order, so the Nth entry for a given method+URL is the
        Nth initiator for it -- which needs no clock at all.
        """
        key = (entry.method.upper(), entry.url)
        ordinal = 0
        for other in self._entries.values():
            if other is entry:
                break
            if (other.method.upper(), other.url) == key:
                ordinal += 1

        candidates = [i for i in self._initiators if (i.method, i.url) == key]
        if not candidates:
            return None
        approximate = ordinal >= len(candidates)
        chosen = candidates[-1] if approximate else candidates[ordinal]

        view = chosen.public()
        view["clock_skew_seconds"] = round(chosen.wall_time - entry.started_at, 1)
        if approximate:
            view["approximate_match"] = (
                "the initiator buffer wrapped; this is the most recent one for this URL"
            )
        return view

    def note_blocked(self, url: str, reason: str) -> None:
        self.blocked.append({"ts": time.time(), "url": url[:300], "reason": reason})

    def query(
        self,
        *,
        url_contains: str | None = None,
        method: str | None = None,
        resource_types: list[str] | None = None,
        status_min: int | None = None,
        status_max: int | None = None,
        only_failed: bool = False,
        only_with_body: bool = False,
        since_id: str | None = None,
        limit: int = 50,
        newest_first: bool = True,
    ) -> list[NetEntry]:
        since_seq = 0
        if since_id and since_id.startswith("r"):
            with contextlib.suppress(ValueError):
                since_seq = int(since_id[1:])

        out: list[NetEntry] = []
        for entry in self._entries.values():
            seq = int(entry.id[1:]) if entry.id[1:].isdigit() else 0
            if seq <= since_seq:
                continue
            if url_contains and url_contains.lower() not in entry.url.lower():
                continue
            if method and entry.method.upper() != method.upper():
                continue
            if resource_types and entry.resource_type not in resource_types:
                continue
            if status_min is not None and (entry.status is None or entry.status < status_min):
                continue
            if status_max is not None and (entry.status is None or entry.status > status_max):
                continue
            if only_failed and not (entry.failure or (entry.status or 0) >= 400):
                continue
            if only_with_body and not entry.body_path:
                continue
            out.append(entry)
        if newest_first:
            out.reverse()
        return out[:limit]

    def get(self, entry_id: str) -> NetEntry | None:
        return self._entries.get(entry_id)

    async def read_body(self, entry_id: str) -> tuple[bytes | None, NetEntry | None]:
        entry = self._entries.get(entry_id)
        if entry is None or entry.body_path is None:
            return None, entry
        try:
            data = await asyncio.to_thread(entry.body_path.read_bytes)
        except OSError:
            return None, entry
        return data, entry

    def websockets(self) -> list[WsEntry]:
        return list(self._ws.values())

    def websocket(self, ws_id: str) -> WsEntry | None:
        return self._ws.get(ws_id)

    def stats(self) -> dict[str, Any]:
        statuses: dict[str, int] = {}
        for entry in self._entries.values():
            key = f"{(entry.status or 0) // 100}xx" if entry.status else "pending"
            statuses[key] = statuses.get(key, 0) + 1
        return {
            "entries": len(self._entries),
            "dropped_oldest": self.dropped,
            "websockets": len(self._ws),
            "disk_used_bytes": self._disk_used,
            "disk_quota_bytes": self.disk_quota_bytes,
            "by_status": statuses,
            "blocked_by_scope": len(self.blocked),
            "capture_bodies": self.capture_bodies,
            "initiators_captured": len(self._initiators),
        }

    def clear(self) -> int:
        count = len(self._entries)
        for entry in self._entries.values():
            if entry.body_path and entry.body_path.exists():
                with contextlib.suppress(OSError):
                    entry.body_path.unlink()
        self._entries.clear()
        self._by_request.clear()
        self._ws.clear()
        self._initiators.clear()
        self._disk_used = 0
        self.dropped = 0
        return count

    # ----- HAR export ------------------------------------------------------

    async def export_har(self, path: Path, include_bodies: bool = True) -> dict[str, Any]:
        """Write a HAR 1.2 file so findings can go into existing HTTP tooling."""
        entries = []
        for entry in self._entries.values():
            post: dict[str, Any] | None = None
            if entry.post_data is not None:
                ct = entry.request_headers.get("content-type", "")
                post = {"mimeType": ct, "text": entry.post_data}
                if "x-www-form-urlencoded" in ct.lower():
                    post["params"] = [
                        {"name": k, "value": v}
                        for k, v in parse_qsl(entry.post_data, keep_blank_values=True)
                    ]

            content: dict[str, Any] = {
                "size": entry.body_bytes,
                "mimeType": entry.mime_type or "",
            }
            if include_bodies and entry.body_path:
                data, _ = await self.read_body(entry.id)
                if data is not None:
                    if entry.body_is_base64:
                        content["text"] = base64.b64encode(data).decode()
                        content["encoding"] = "base64"
                    else:
                        content["text"] = data.decode("utf-8", "replace")

            entries.append(
                {
                    "startedDateTime": time.strftime(
                        "%Y-%m-%dT%H:%M:%S", time.gmtime(entry.started_at)
                    )
                    + f".{int(entry.started_at % 1 * 1000):03d}Z",
                    "time": entry.duration_ms or 0,
                    "request": {
                        "method": entry.method,
                        "url": entry.url,
                        "httpVersion": "HTTP/1.1",
                        "headers": [
                            {"name": k, "value": v} for k, v in entry.request_headers.items()
                        ],
                        "queryString": [{"name": k, "value": v} for k, v in entry.query],
                        "cookies": [],
                        "headersSize": -1,
                        "bodySize": len(entry.post_data or ""),
                        **({"postData": post} if post else {}),
                    },
                    "response": {
                        "status": entry.status or 0,
                        "statusText": entry.status_text or (entry.failure or ""),
                        "httpVersion": "HTTP/1.1",
                        "headers": [
                            {"name": k, "value": v} for k, v in entry.response_headers.items()
                        ],
                        "cookies": [],
                        "content": content,
                        "redirectURL": entry.response_headers.get("location", ""),
                        "headersSize": -1,
                        "bodySize": entry.body_bytes,
                        "_serverIPAddress": entry.server_ip,
                    },
                    "cache": {},
                    "timings": {"send": 0, "wait": entry.duration_ms or 0, "receive": 0},
                    "_resourceType": entry.resource_type,
                    "_failure": entry.failure,
                }
            )

        har = {
            "log": {
                "version": "1.2",
                "creator": {"name": "cbmcp", "version": "0.1.0"},
                "pages": [],
                "entries": entries,
                "_comment": (
                    "Captured via Chrome DevTools Protocol through Playwright. "
                    "This is browser-level observation, not a packet capture: no TLS "
                    "detail, no other-process traffic, and pre-attach requests are absent."
                ),
                "_websockets": [
                    {
                        "id": ws.id,
                        "url": ws.url,
                        "opened_at": ws.opened_at,
                        "closed_at": ws.closed_at,
                        "frames": [
                            {
                                "ts": f.ts,
                                "direction": f.direction,
                                "payload": f.payload,
                                "truncated": f.truncated,
                            }
                            for f in ws.frames
                        ],
                    }
                    for ws in self._ws.values()
                ],
            }
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_text, json.dumps(har, indent=1), "utf-8")
        return {
            "path": str(path),
            "entries": len(entries),
            "websockets": len(self._ws),
            "bytes": path.stat().st_size,
        }
