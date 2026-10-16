"""Real JavaScript debugging over the CDP ``Debugger`` domain.

This is the part that makes the gateway worth more than "a browser with an
evaluate() tool". An agent studying a site can:

* search every loaded script for a string, even bundled and minified ones
  (``Debugger.searchInContent``) -- the fastest way to find where a signature,
  nonce or token is produced;
* break the moment a request to a URL is about to be built
  (``DOMDebugger.setXHRBreakpoint``) and read the local variables that went into
  it, *before* it is encrypted or signed;
* break on DOM or listener events, step through frames, and evaluate
  expressions in the paused frame's scope (``Debugger.evaluateOnCallFrame``),
  which reaches closure variables that ``page.evaluate`` can never see.

The hard constraint: **while the debugger is paused the renderer's main thread
is blocked.** Any ordinary page operation would hang. So pause state is tracked
here and the page tools refuse to run while paused, telling the agent to inspect
or resume instead. Line numbers are 1-based in this API and converted to CDP's
0-based form at the boundary.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .errors import DebuggerNotPaused, InvalidArgument, NotFound
from .log import get_logger

log = get_logger("debug")

PAUSE_ON_EXCEPTIONS = ("none", "caught", "uncaught", "all")

# Scopes worth expanding. 'global' is thousands of properties and would flood
# the agent's context for no benefit.
_USEFUL_SCOPES = {"local", "closure", "catch", "block", "with"}

_EVENT_BREAKPOINT_ALIASES = {
    "click": "listener:click",
    "submit": "listener:submit",
    "keydown": "listener:keydown",
    "load": "listener:load",
    "message": "listener:message",
    "setTimeout": "instrumentation:setTimeout",
    "setInterval": "instrumentation:setInterval",
    "requestAnimationFrame": "instrumentation:requestAnimationFrame",
    "scriptFirstStatement": "instrumentation:scriptFirstStatement",
}


@dataclass
class ScriptInfo:
    script_id: str
    url: str
    length: int = 0
    has_source_map: bool = False
    is_module: bool = False
    hash: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "script_id": self.script_id,
            "url": self.url or "<inline/eval>",
            "length": self.length,
            "source_map": self.has_source_map,
            "module": self.is_module,
        }


@dataclass
class BreakpointInfo:
    handle: str
    kind: str  # url | xhr | event | exception
    detail: str
    cdp_id: str | None = None
    condition: str | None = None
    locations: list[dict[str, Any]] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return {
            "handle": self.handle,
            "kind": self.kind,
            "detail": self.detail,
            "condition": self.condition,
            "resolved_locations": self.locations,
        }


class JsDebugger:
    """Debugger domain bound to one page's CDP session."""

    def __init__(self, cdp: Any, page_id: str) -> None:
        self.cdp = cdp
        self.page_id = page_id
        self.enabled = False
        self.scripts: dict[str, ScriptInfo] = {}
        self.breakpoints: dict[str, BreakpointInfo] = {}
        self._bp_seq = 0
        self.paused: dict[str, Any] | None = None
        self._pause_event = asyncio.Event()
        self._pause_on_exceptions = "none"
        self._blackbox: list[str] = []
        # Pausing is suppressed until the agent asks for a breakpoint; see enable().
        self.pauses_enabled = False
        self.anti_debug_pauses = 0
        # See BrowserSession._spawn: an auto-resume task that gets collected
        # would leave the renderer frozen.
        self._bg: set[asyncio.Task] = set()
        # True once a hook with break_on_match is armed: its injected `debugger;`
        # is then indistinguishable from the page's own, so pauses are honoured.
        self.expect_debugger_statements = False

    # ----- lifecycle -------------------------------------------------------

    async def enable(self) -> None:
        """Attach the Debugger domain, but with pausing suppressed.

        Learned the hard way: anti-automation pages ship ``debugger;`` statements
        on purpose. With the Debugger domain attached and pauses allowed, the
        first one freezes the renderer -- navigation never completes, every page
        tool blocks, and even closing the browser hangs.

        So pausing starts off. ``Debugger.setSkipAllPauses`` makes the page's own
        ``debugger;`` statements no-ops, which also removes the timing signature
        they are used to detect. Pausing is switched on only when the agent
        actually asks for a breakpoint, which is the only time it is wanted.
        """
        if self.enabled:
            return
        self.cdp.on("Debugger.scriptParsed", self._on_script_parsed)
        self.cdp.on("Debugger.paused", self._on_paused)
        self.cdp.on("Debugger.resumed", self._on_resumed)
        await self.cdp.send("Runtime.enable")
        await self.cdp.send("Debugger.enable", {"maxScriptsCacheSize": 100_000_000})
        await self.cdp.send("Debugger.setSkipAllPauses", {"skip": True})
        self.pauses_enabled = False
        self.enabled = True
        log.debug("debugger enabled on %s (pauses suppressed until a breakpoint is set)", self.page_id)

    async def set_pauses_enabled(self, enabled: bool) -> None:
        """Allow or suppress every kind of pause, including the page's own."""
        await self.cdp.send("Debugger.setSkipAllPauses", {"skip": not enabled})
        self.pauses_enabled = enabled
        if not enabled and self.paused:
            with contextlib.suppress(Exception):
                await self.cdp.send("Debugger.resume")
            self._on_resumed()
        log.info("pauses %s on %s", "enabled" if enabled else "suppressed", self.page_id)

    async def _allow_pauses(self) -> None:
        if not self.pauses_enabled:
            await self.set_pauses_enabled(True)

    async def disable(self) -> None:
        if not self.enabled:
            return
        # Resume first and stop future pauses: closing a browser whose renderer
        # is paused hangs, which would block the lease release and the slot.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(
                self.cdp.send("Debugger.setSkipAllPauses", {"skip": True}), timeout=5
            )
        with contextlib.suppress(Exception):
            if self.paused:
                await asyncio.wait_for(self.cdp.send("Debugger.resume"), timeout=5)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.cdp.send("Debugger.disable"), timeout=5)
        self.enabled = False
        self.paused = None
        self._pause_event.clear()

    # ----- events ----------------------------------------------------------

    def _on_script_parsed(self, params: dict[str, Any]) -> None:
        sid = params.get("scriptId")
        if not sid:
            return
        self.scripts[sid] = ScriptInfo(
            script_id=sid,
            url=params.get("url") or params.get("sourceURL") or "",
            length=int(params.get("length") or 0),
            has_source_map=bool(params.get("sourceMapURL")),
            is_module=bool(params.get("isModule")),
            hash=params.get("hash"),
        )

    def _on_paused(self, params: dict[str, Any]) -> None:
        if not self.pauses_enabled:
            # A pause nobody asked for. Navigation resets setSkipAllPauses, so an
            # anti-debug `debugger;` in page code can get through -- resume at
            # once and re-assert the suppression, otherwise the renderer stays
            # frozen and every page tool blocks behind it.
            self.anti_debug_pauses += 1
            self._spawn(self._auto_resume(suppress=True))
            return

        reason = params.get("reason")
        if reason == "debuggerStatement" and not self.expect_debugger_statements:
            # The agent asked for breakpoints, but this pause is a bare
            # `debugger;` in page code and none of its hooks inject one. Pages
            # that defend themselves run these in a loop; honouring them would
            # flood the agent and freeze the page between every pause. Resume,
            # keep the agent's own breakpoints armed, and count it.
            self.anti_debug_pauses += 1
            self._spawn(self._auto_resume(suppress=False))
            return

        self.paused = {"raw": params, "at": time.time()}
        self._pause_event.set()
        log.info("paused on %s reason=%s", self.page_id, params.get("reason"))

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def _auto_resume(self, *, suppress: bool) -> None:
        if suppress:
            with contextlib.suppress(Exception):
                await self.cdp.send("Debugger.setSkipAllPauses", {"skip": True})
        with contextlib.suppress(Exception):
            await self.cdp.send("Debugger.resume")
        if self.anti_debug_pauses in (1, 10, 100, 1000):
            log.info(
                "page on %s used an anti-debug `debugger;` statement "
                "(auto-resumed, %d so far)",
                self.page_id,
                self.anti_debug_pauses,
            )

    async def reassert_pause_suppression(self) -> None:
        """Re-apply suppression after a navigation, which resets it."""
        if self.enabled and not self.pauses_enabled:
            with contextlib.suppress(Exception):
                await self.cdp.send("Debugger.setSkipAllPauses", {"skip": True})

    def _on_resumed(self, _params: dict[str, Any] | None = None) -> None:
        self.paused = None
        self._pause_event.clear()

    @property
    def is_paused(self) -> bool:
        return self.paused is not None

    # ----- scripts ---------------------------------------------------------

    def list_scripts(
        self, url_contains: str | None = None, min_length: int = 0, limit: int = 100
    ) -> list[ScriptInfo]:
        items = [
            s
            for s in self.scripts.values()
            if s.length >= min_length
            and (not url_contains or url_contains.lower() in (s.url or "").lower())
        ]
        items.sort(key=lambda s: -s.length)
        return items[:limit]

    def _resolve_script(self, script_id: str | None, url: str | None) -> ScriptInfo:
        if script_id:
            info = self.scripts.get(script_id)
            if info is None:
                raise InvalidArgument(
                    f"unknown script_id {script_id!r}",
                    hint="Call js_scripts first; script ids change on every page load.",
                )
            return info
        if url:
            matches = [s for s in self.scripts.values() if url.lower() in (s.url or "").lower()]
            if not matches:
                raise InvalidArgument(f"no loaded script matches url {url!r}")
            matches.sort(key=lambda s: -s.length)
            return matches[0]
        raise InvalidArgument("pass either script_id or url")

    async def get_source(
        self,
        *,
        script_id: str | None = None,
        url: str | None = None,
        start_line: int = 1,
        end_line: int | None = None,
        max_chars: int = 20_000,
    ) -> dict[str, Any]:
        info = self._resolve_script(script_id, url)
        result = await self.cdp.send("Debugger.getScriptSource", {"scriptId": info.script_id})
        source = result.get("scriptSource") or ""
        lines = source.splitlines()
        total = len(lines)
        start = max(1, start_line)
        end = min(total, end_line or (start + 400))
        chunk = "\n".join(
            f"{n:>6}| {line}" for n, line in enumerate(lines[start - 1 : end], start=start)
        )
        truncated = False
        if len(chunk) > max_chars:
            chunk = chunk[:max_chars]
            truncated = True
        return {
            "script_id": info.script_id,
            "url": info.url or "<inline/eval>",
            "total_lines": total,
            "shown_lines": [start, end],
            "truncated": truncated,
            "source": chunk,
            "note": (
                "Minified bundles are often one enormous line; use js_search to locate "
                "an offset instead of paging blindly."
            )
            if total < 5 and len(source) > 50_000
            else None,
        }

    async def search(
        self,
        query: str,
        *,
        is_regex: bool = False,
        case_sensitive: bool = False,
        url_contains: str | None = None,
        max_hits: int = 60,
        context_chars: int = 160,
    ) -> dict[str, Any]:
        """Search the text of every loaded script.

        This is the single most useful tool for understanding an unfamiliar
        front end: it finds the code behind a parameter name, header or token
        format without any guessing about file layout.
        """
        hits: list[dict[str, Any]] = []
        searched = 0
        candidates = [
            s
            for s in self.scripts.values()
            if not url_contains or url_contains.lower() in (s.url or "").lower()
        ]
        candidates.sort(key=lambda s: -s.length)

        for info in candidates[:400]:
            if len(hits) >= max_hits:
                break
            searched += 1
            try:
                result = await self.cdp.send(
                    "Debugger.searchInContent",
                    {
                        "scriptId": info.script_id,
                        "query": query,
                        "caseSensitive": case_sensitive,
                        "isRegex": is_regex,
                    },
                )
            except Exception:  # noqa: BLE001 - scripts can be collected mid-search
                continue
            for match in result.get("result", []) or []:
                if len(hits) >= max_hits:
                    break
                line_text = match.get("lineContent") or ""
                line_no = int(match.get("lineNumber", 0)) + 1
                offset = 0
                if not is_regex:
                    needle = query if case_sensitive else query.lower()
                    haystack = line_text if case_sensitive else line_text.lower()
                    offset = max(0, haystack.find(needle))
                snippet = line_text[max(0, offset - context_chars // 2) : offset + context_chars]
                hits.append(
                    {
                        "script_id": info.script_id,
                        "url": info.url or "<inline/eval>",
                        "line": line_no,
                        "column_hint": offset,
                        "snippet": snippet.strip(),
                        "break_here": (
                            f"js_breakpoint(action='set', kind='url', "
                            f"url_pattern={_regex_for(info.url)!r}, line={line_no})"
                        )
                        if info.url
                        else None,
                    }
                )
        return {
            "query": query,
            "is_regex": is_regex,
            "scripts_searched": searched,
            "scripts_total": len(self.scripts),
            "hit_count": len(hits),
            "hits": hits,
        }

    # ----- breakpoints -----------------------------------------------------

    def _new_handle(self, kind: str) -> str:
        self._bp_seq += 1
        return f"bp{self._bp_seq}_{kind}"

    async def set_url_breakpoint(
        self,
        *,
        url_pattern: str,
        line: int,
        column: int | None = None,
        condition: str | None = None,
    ) -> BreakpointInfo:
        await self._allow_pauses()
        params: dict[str, Any] = {
            "lineNumber": max(0, line - 1),  # CDP is 0-based
            "urlRegex": url_pattern,
        }
        if column is not None:
            params["columnNumber"] = max(0, column)
        if condition:
            params["condition"] = condition
        result = await self.cdp.send("Debugger.setBreakpointByUrl", params)
        info = BreakpointInfo(
            handle=self._new_handle("url"),
            kind="url",
            detail=f"{url_pattern} line {line}",
            cdp_id=result.get("breakpointId"),
            condition=condition,
            locations=[
                {
                    "script_id": loc.get("scriptId"),
                    "line": int(loc.get("lineNumber", 0)) + 1,
                    "column": loc.get("columnNumber"),
                }
                for loc in result.get("locations", []) or []
            ],
        )
        self.breakpoints[info.handle] = info
        return info

    async def set_location_breakpoint(
        self, *, script_id: str, line: int, column: int | None = None, condition: str | None = None
    ) -> BreakpointInfo:
        """Break at an exact script location, for scripts with no URL (inline, eval)."""
        await self._allow_pauses()
        location: dict[str, Any] = {"scriptId": script_id, "lineNumber": max(0, line - 1)}
        if column is not None:
            location["columnNumber"] = max(0, column)
        params: dict[str, Any] = {"location": location}
        if condition:
            params["condition"] = condition
        result = await self.cdp.send("Debugger.setBreakpoint", params)
        script = self.scripts.get(script_id)
        info = BreakpointInfo(
            handle=self._new_handle("loc"),
            kind="url",
            detail=f"{(script.url if script else script_id) or '<inline>'} line {line}",
            cdp_id=result.get("breakpointId"),
            condition=condition,
            locations=[{"script_id": script_id, "line": line, "column": column}],
        )
        self.breakpoints[info.handle] = info
        return info

    async def set_text_breakpoint(
        self,
        text: str,
        *,
        is_regex: bool = False,
        url_contains: str | None = None,
        occurrence: int = 0,
        condition: str | None = None,
    ) -> dict[str, Any]:
        """Find code by its text and break there.

        The practical way into a minified bundle: you rarely know a line number,
        but you do know a string the code contains -- a parameter name, a header,
        an error message. This finds it and sets the breakpoint in one step.
        """
        found = await self.search(text, is_regex=is_regex, url_contains=url_contains, max_hits=20)
        hits = found["hits"]
        if not hits:
            raise NotFound(
                f"no loaded script contains {text!r}",
                hint="The string may be built at runtime (try js_hook with the 'json' or "
                "'encode' presets), live in a Worker, or load later -- reload and retry.",
            )
        if occurrence >= len(hits):
            raise InvalidArgument(
                f"occurrence {occurrence} out of range; {len(hits)} match(es) found"
            )
        hit = hits[occurrence]
        if hit["url"] and hit["url"] != "<inline/eval>":
            info = await self.set_url_breakpoint(
                url_pattern=_regex_for(hit["url"]), line=hit["line"], condition=condition
            )
        else:
            info = await self.set_location_breakpoint(
                script_id=hit["script_id"], line=hit["line"], condition=condition
            )
        return {
            "breakpoint": info.public(),
            "matched": {
                "url": hit["url"],
                "line": hit["line"],
                "snippet": hit["snippet"],
            },
            "other_matches": [
                {"index": i, "url": h["url"], "line": h["line"], "snippet": h["snippet"][:120]}
                for i, h in enumerate(hits)
                if i != occurrence
            ][:10],
            "total_matches": len(hits),
        }

    async def set_xhr_breakpoint(self, url_substring: str) -> BreakpointInfo:
        """Break when a request whose URL contains ``url_substring`` is about to fire.

        An empty substring breaks on every XHR/fetch. The pause happens in the
        caller's stack, so the arguments and closure state that produced the
        request are still readable.
        """
        await self._allow_pauses()
        await self.cdp.send("DOMDebugger.setXHRBreakpoint", {"url": url_substring})
        info = BreakpointInfo(
            handle=self._new_handle("xhr"),
            kind="xhr",
            detail=url_substring or "<any request>",
        )
        self.breakpoints[info.handle] = info
        return info

    async def set_event_breakpoint(self, event_name: str) -> BreakpointInfo:
        await self._allow_pauses()
        name = _EVENT_BREAKPOINT_ALIASES.get(event_name, event_name)
        if name.startswith("instrumentation:"):
            await self.cdp.send(
                "EventBreakpoints.setInstrumentationBreakpoint",
                {"eventName": name.split(":", 1)[1]},
            )
        else:
            plain = name.split(":", 1)[1] if ":" in name else name
            await self.cdp.send("DOMDebugger.setEventListenerBreakpoint", {"eventName": plain})
        info = BreakpointInfo(
            handle=self._new_handle("event"), kind="event", detail=event_name
        )
        self.breakpoints[info.handle] = info
        return info

    async def remove_breakpoint(self, handle: str) -> bool:
        info = self.breakpoints.pop(handle, None)
        if info is None:
            return False
        with contextlib.suppress(Exception):
            if info.kind == "url" and info.cdp_id:
                await self.cdp.send("Debugger.removeBreakpoint", {"breakpointId": info.cdp_id})
            elif info.kind == "xhr":
                target = "" if info.detail == "<any request>" else info.detail
                await self.cdp.send("DOMDebugger.removeXHRBreakpoint", {"url": target})
            elif info.kind == "event":
                name = _EVENT_BREAKPOINT_ALIASES.get(info.detail, info.detail)
                if name.startswith("instrumentation:"):
                    await self.cdp.send(
                        "EventBreakpoints.removeInstrumentationBreakpoint",
                        {"eventName": name.split(":", 1)[1]},
                    )
                else:
                    plain = name.split(":", 1)[1] if ":" in name else name
                    await self.cdp.send(
                        "DOMDebugger.removeEventListenerBreakpoint", {"eventName": plain}
                    )
        return True

    async def set_pause_on_exceptions(self, state: str) -> None:
        if state not in PAUSE_ON_EXCEPTIONS:
            raise InvalidArgument(f"state must be one of {PAUSE_ON_EXCEPTIONS}")
        if state != "none":
            await self._allow_pauses()
        await self.cdp.send("Debugger.setPauseOnExceptions", {"state": state})
        self._pause_on_exceptions = state

    async def set_blackbox(self, patterns: list[str]) -> None:
        await self.cdp.send("Debugger.setBlackboxPatterns", {"patterns": patterns})
        self._blackbox = patterns

    async def set_breakpoints_active(self, active: bool) -> None:
        await self.cdp.send("Debugger.setBreakpointsActive", {"active": active})

    # ----- pause inspection ------------------------------------------------

    async def wait_for_pause(self, timeout: float) -> bool:
        if self.is_paused:
            return True
        try:
            await asyncio.wait_for(self._pause_event.wait(), timeout=timeout)
        except TimeoutError:
            return False
        return self.is_paused

    async def pause_state(self, *, expand_scopes: bool = True, max_vars: int = 40) -> dict[str, Any]:
        if not self.paused:
            raise DebuggerNotPaused("execution is not paused")
        raw = self.paused["raw"]
        frames_out: list[dict[str, Any]] = []
        for index, frame in enumerate(raw.get("callFrames", [])[:24]):
            loc = frame.get("location", {})
            script = self.scripts.get(loc.get("scriptId", ""))
            entry: dict[str, Any] = {
                "index": index,
                "call_frame_id": frame.get("callFrameId"),
                "function": frame.get("functionName") or "(anonymous)",
                "url": (script.url if script else frame.get("url")) or "<inline/eval>",
                "script_id": loc.get("scriptId"),
                "line": int(loc.get("lineNumber", 0)) + 1,
                "column": loc.get("columnNumber"),
            }
            if expand_scopes and index < 3:
                entry["scopes"] = await self._expand_scopes(
                    frame.get("scopeChain", []), max_vars
                )
            frames_out.append(entry)

        reason = raw.get("reason")
        aux = raw.get("data") or {}
        return {
            "paused": True,
            "reason": reason,
            "reason_detail": _describe_reason(reason, aux),
            "hit_breakpoints": raw.get("hitBreakpoints") or [],
            "call_frames": frames_out,
            "text": _render_pause(reason, aux, frames_out),
            "next": (
                "js_eval_frame(expression=...) to read values in a frame's scope; "
                "js_step(action='over'|'into'|'out'|'resume') to continue. "
                "Page tools stay blocked until you resume."
            ),
        }

    async def _expand_scopes(
        self, scope_chain: list[dict[str, Any]], max_vars: int
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for scope in scope_chain:
            kind = scope.get("type")
            if kind not in _USEFUL_SCOPES:
                continue
            obj = scope.get("object") or {}
            object_id = obj.get("objectId")
            variables: list[dict[str, Any]] = []
            if object_id:
                with contextlib.suppress(Exception):
                    result = await self.cdp.send(
                        "Runtime.getProperties",
                        {
                            "objectId": object_id,
                            "ownProperties": True,
                            "accessorPropertiesOnly": False,
                            "generatePreview": True,
                        },
                    )
                    for prop in (result.get("result") or [])[:max_vars]:
                        variables.append(
                            {
                                "name": prop.get("name"),
                                **_render_remote(prop.get("value") or {}),
                            }
                        )
            out.append(
                {
                    "type": kind,
                    "name": scope.get("name"),
                    "variables": variables,
                    "truncated": len(variables) >= max_vars,
                }
            )
        return out

    async def evaluate_on_frame(
        self, *, frame_index: int = 0, expression: str, max_chars: int = 8000
    ) -> dict[str, Any]:
        if not self.paused:
            raise DebuggerNotPaused("execution is not paused, so there is no frame scope to read")
        frames = self.paused["raw"].get("callFrames", [])
        if frame_index < 0 or frame_index >= len(frames):
            raise InvalidArgument(
                f"frame_index {frame_index} out of range (0..{len(frames) - 1})"
            )
        result = await self.cdp.send(
            "Debugger.evaluateOnCallFrame",
            {
                "callFrameId": frames[frame_index]["callFrameId"],
                "expression": expression,
                "objectGroup": "cbmcp",
                "includeCommandLineAPI": True,
                "silent": False,
                "returnByValue": False,
                "generatePreview": True,
            },
        )
        if result.get("exceptionDetails"):
            exc = result["exceptionDetails"]
            return {
                "ok": False,
                "error": "js_exception",
                "message": (exc.get("exception") or {}).get("description")
                or exc.get("text")
                or "evaluation threw",
            }
        rendered = _render_remote(result.get("result") or {}, max_chars=max_chars)
        return {"ok": True, "frame_index": frame_index, **rendered}

    async def get_properties(
        self, object_id: str, *, max_props: int = 80, own_only: bool = True
    ) -> dict[str, Any]:
        result = await self.cdp.send(
            "Runtime.getProperties",
            {
                "objectId": object_id,
                "ownProperties": own_only,
                "generatePreview": True,
            },
        )
        props = [
            {"name": p.get("name"), **_render_remote(p.get("value") or {})}
            for p in (result.get("result") or [])[:max_props]
        ]
        internal = [
            {"name": p.get("name"), **_render_remote(p.get("value") or {})}
            for p in (result.get("internalProperties") or [])[:20]
        ]
        return {"properties": props, "internal": internal, "count": len(props)}

    async def step(self, action: str) -> dict[str, Any]:
        if not self.paused:
            raise DebuggerNotPaused("nothing to step; execution is running")
        command = {
            "resume": "Debugger.resume",
            "over": "Debugger.stepOver",
            "into": "Debugger.stepInto",
            "out": "Debugger.stepOut",
        }.get(action)
        if command is None:
            raise InvalidArgument("action must be resume, over, into or out")
        self._pause_event.clear()
        await self.cdp.send(command)
        if action == "resume":
            self.paused = None
            return {"ok": True, "action": action, "paused": False}
        # A step lands on the next statement; give it a moment to report back.
        landed = await self.wait_for_pause(5.0)
        if not landed:
            return {"ok": True, "action": action, "paused": False, "note": "execution continued"}
        return {"ok": True, "action": action, **await self.pause_state(expand_scopes=True)}

    async def event_listeners(self, object_id: str) -> list[dict[str, Any]]:
        """Which handlers are bound to a node -- CDP only, unreachable from page JS."""
        result = await self.cdp.send("DOMDebugger.getEventListeners", {"objectId": object_id})
        out = []
        for listener in result.get("listeners", []) or []:
            script = self.scripts.get(listener.get("scriptId", ""))
            out.append(
                {
                    "type": listener.get("type"),
                    "useCapture": listener.get("useCapture"),
                    "passive": listener.get("passive"),
                    "once": listener.get("once"),
                    "url": (script.url if script else None) or "<inline>",
                    "script_id": listener.get("scriptId"),
                    "line": int(listener.get("lineNumber", 0)) + 1,
                    "column": listener.get("columnNumber"),
                }
            )
        return out

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "paused": self.is_paused,
            "pauses_enabled": self.pauses_enabled,
            "anti_debug_statements_auto_resumed": self.anti_debug_pauses,
            "scripts_loaded": len(self.scripts),
            "breakpoints": [b.public() for b in self.breakpoints.values()],
            "pause_on_exceptions": self._pause_on_exceptions,
            "blackbox_patterns": self._blackbox,
        }


# ----- rendering helpers ---------------------------------------------------


def _regex_for(url: str) -> str:
    """A urlRegex that matches this exact script URL."""
    return re.escape(url)


def _render_remote(obj: dict[str, Any], max_chars: int = 400) -> dict[str, Any]:
    kind = obj.get("type")
    subtype = obj.get("subtype")
    out: dict[str, Any] = {"type": subtype or kind}

    if kind in ("string", "number", "boolean"):
        value = obj.get("value")
        text = str(value)
        if len(text) > max_chars:
            out["value"] = text[:max_chars]
            out["truncated"] = True
        else:
            out["value"] = value
        return out
    if kind == "undefined":
        out["value"] = None
        return out
    if subtype == "null":
        out["value"] = None
        return out
    if kind == "function":
        desc = obj.get("description") or "function"
        out["value"] = desc[:200]
        out["object_id"] = obj.get("objectId")
        return out

    preview = obj.get("preview")
    if preview:
        props = preview.get("properties") or []
        bits = []
        for prop in props[:12]:
            pv = prop.get("value")
            bits.append(f"{prop.get('name')}: {str(pv)[:80]}")
        shape = "[" + ", ".join(bits) + "]" if preview.get("subtype") == "array" else (
            "{" + ", ".join(bits) + "}"
        )
        if preview.get("overflow"):
            shape = shape[:-1] + ", ...}"
        out["value"] = shape[:max_chars]
    else:
        out["value"] = (obj.get("description") or kind or "?")[:max_chars]

    if obj.get("objectId"):
        out["object_id"] = obj["objectId"]
        out["expand_with"] = "js_properties(object_id=...)"
    return out


def _describe_reason(reason: str | None, data: dict[str, Any]) -> str:
    if reason == "XHR":
        return f"a request matching {data.get('url', '?')!r} is being prepared"
    if reason == "EventListener":
        return f"event listener fired: {data.get('eventName', '?')}"
    if reason == "exception":
        desc = (data.get("description") or data.get("value") or "")
        return f"exception thrown: {str(desc)[:200]}"
    if reason == "assert":
        return "console.assert failed"
    if reason == "debuggerStatement":
        return "a debugger; statement in page code"
    if reason == "breakpoint":
        return "a breakpoint you set was hit"
    if reason in ("step", "other"):
        return "stepped to the next statement"
    return str(reason or "unknown")


def _render_pause(
    reason: str | None, data: dict[str, Any], frames: list[dict[str, Any]]
) -> str:
    lines = [f"PAUSED ({reason}): {_describe_reason(reason, data)}", "", "call stack:"]
    for frame in frames:
        lines.append(
            f"  #{frame['index']} {frame['function']}  at {frame['url']}:"
            f"{frame['line']}:{frame.get('column')}"
        )
    for frame in frames[:2]:
        for scope in frame.get("scopes", []) or []:
            if not scope["variables"]:
                continue
            lines.append("")
            lines.append(f"{scope['type']} scope in frame #{frame['index']}:")
            for var in scope["variables"][:25]:
                lines.append(f"  {var['name']} = {var.get('value')!r}  ({var.get('type')})")
    return "\n".join(lines)
