"""JavaScript debugging and hooking tools.

The intended workflow, which the tool descriptions reinforce because an agent
left to guess will reach for page_eval and get stuck on closures:

    1. js_search("signature")        -- find the code, even in a minified bundle
    2. js_hook(targets=["xhr","json","crypto"])  -- watch calls and stacks
    3. <drive the page: click login>
    4. js_hook_records(text_contains="sign")     -- see args, returns, callers
    5. js_breakpoint(kind="xhr", url_substring="/api/login")
    6. <repeat the action>
    7. js_pause_state()              -- call stack plus local/closure variables
    8. js_eval_frame("JSON.stringify(params)")   -- read values page_eval cannot
    9. js_step(action="resume")
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

from ..errors import InvalidArgument
from ..hooks import PRESET_NAMES
from ._common import guarded, use_lease

SEARCH_DOC = """Search the source of every loaded script for a string or regex.

The fastest way into an unfamiliar front end. Works on minified and bundled
code because it searches the text the browser actually loaded, so a parameter
name, header name or error string leads straight to the code that builds it.
Each hit comes with a ready-made js_breakpoint call for that line.
"""

HOOK_DOC = f"""Install non-blocking hooks that record calls, returns and stacks.

Use this when you do not yet know where the interesting code is. Hooks keep the
page running and record every call, so the captured **stack trace** tells you
which function to look at next.

Presets: {', '.join(PRESET_NAMES)}
  xhr/fetch/websocket  request construction and sending
  json                 JSON.stringify / JSON.parse -- where payloads are built
                       and responses consumed
  cookie               document.cookie reads and writes
  storage              localStorage / sessionStorage
  crypto               WebCrypto subtle.*, getRandomValues, and CryptoJS if present
  encode               btoa / atob / encodeURIComponent / TextEncoder
  eval                 eval and new Function -- catches dynamically built code
  timer / dom          setTimeout/setInterval, innerHTML, createElement

`custom_paths` hooks anything else by path, e.g. ['window.app.sign',
'Object.prototype.toString'].

`arg_filter` records only calls whose arguments contain a substring, which keeps
output small on a busy page.

`break_on_match` is the bridge to the debugger: when an argument matches, the
hook fires `debugger;` so execution stops *inside* that function with its locals
intact -- then use js_pause_state and js_eval_frame. Requires debug='full'.

Hooks install before page scripts run on every new document. If the lease was
not acquired with debug='full' or 'hooks', or the page already loaded, reload the
page to capture startup behaviour.
"""


def register(mcp: MCPServer) -> None:
    # ----- scripts ---------------------------------------------------------

    @mcp.tool(
        description=(
            "List the scripts the page has loaded, largest first, with ids for "
            "js_source. Bundles are usually the biggest entries."
        )
    )
    @guarded
    async def js_scripts(
        lease: str,
        url_contains: str | None = None,
        min_length: int = 0,
        limit: int = 60,
        page: str | None = None,
    ) -> dict[str, Any]:
        """
        Args:
            lease: Lease token.
            url_contains: Only scripts whose URL contains this substring.
            min_length: Skip scripts shorter than this many characters.
            limit: Maximum scripts to list.
            page: Tab id.
        """
        async with use_lease(lease, exclusive=False) as (obj, session):
            debugger = await session.debugger_for(page)
            scripts = debugger.list_scripts(url_contains, min_length, limit)
            return {
                "ok": True,
                "count": len(scripts),
                "total_loaded": len(debugger.scripts),
                "scripts": [s.public() for s in scripts],
            }

    @mcp.tool(
        description=(
            "Read a loaded script's source by script_id or url, with line numbers. "
            "A minified bundle may be one huge line -- use js_search to find an "
            "offset rather than paging through it."
        )
    )
    @guarded
    async def js_source(
        lease: str,
        script_id: str | None = None,
        url: str | None = None,
        start_line: int = 1,
        end_line: int | None = None,
        max_chars: int = 20_000,
        page: str | None = None,
    ) -> dict[str, Any]:
        """
        Args:
            lease: Lease token.
            script_id: Script id from js_scripts.
            url: Match a script by URL substring instead.
            start_line: First line to return (1-based).
            end_line: Last line to return.
            max_chars: Cap on returned characters.
            page: Tab id.
        """
        async with use_lease(lease, exclusive=False) as (obj, session):
            debugger = await session.debugger_for(page)
            return {
                "ok": True,
                **await debugger.get_source(
                    script_id=script_id,
                    url=url,
                    start_line=start_line,
                    end_line=end_line,
                    max_chars=max_chars,
                ),
            }

    @mcp.tool(description=SEARCH_DOC)
    @guarded
    async def js_search(
        lease: str,
        query: str,
        is_regex: bool = False,
        case_sensitive: bool = False,
        url_contains: str | None = None,
        max_hits: int = 60,
        page: str | None = None,
    ) -> dict[str, Any]:
        """
        Args:
            lease: Lease token.
            query: Text or regex to find.
            is_regex: Treat query as a regular expression.
            case_sensitive: Match case exactly.
            url_contains: Restrict to scripts whose URL contains this substring.
            max_hits: Maximum hits to return.
            page: Tab id.
        """
        async with use_lease(lease, exclusive=False) as (obj, session):
            debugger = await session.debugger_for(page)
            result = await debugger.search(
                query,
                is_regex=is_regex,
                case_sensitive=case_sensitive,
                url_contains=url_contains,
                max_hits=max_hits,
            )
            if result["hit_count"] == 0:
                result["hint"] = (
                    "No hits. The string may be built at runtime (hook 'json' or "
                    "'encode' to catch that), inside a Worker, or added after load -- "
                    "reload the page and search again."
                )
            return {"ok": True, **result}

    # ----- breakpoints -----------------------------------------------------

    @mcp.tool(
        description=(
            "Set, remove or list breakpoints.\n"
            "  kind='text'  find code containing `text` and break there, in one step. "
            "The usual way into a minified bundle: you rarely know a line number, but "
            "you do know a string the code contains -- a parameter name, a header, an "
            "error message.\n"
            "  kind='xhr'   break when a request whose URL contains url_substring is "
            "built. Best starting point when you know the endpoint: it stops in the "
            "caller's stack, so the code that produced the payload is still on it.\n"
            "  kind='url'   break at url_pattern (a regex matching the script URL) and "
            "line (1-based). Take both from a js_search hit.\n"
            "  kind='event' break when a DOM event fires, e.g. 'click', 'submit'.\n"
            "After setting one, repeat the action, then call js_pause_state."
        )
    )
    @guarded
    async def js_breakpoint(
        lease: str,
        action: str = "set",
        kind: str = "xhr",
        url_substring: str | None = None,
        url_pattern: str | None = None,
        line: int | None = None,
        column: int | None = None,
        condition: str | None = None,
        event_name: str | None = None,
        text: str | None = None,
        is_regex: bool = False,
        occurrence: int = 0,
        handle: str | None = None,
        page: str | None = None,
    ) -> dict[str, Any]:
        """
        Args:
            lease: Lease token.
            action: 'set' | 'remove' | 'list'.
            kind: 'text' | 'xhr' | 'url' | 'event'.
            url_substring: For kind='xhr'. Empty string breaks on every request.
            url_pattern: For kind='url': regex matching the script URL.
            line: For kind='url': 1-based line number.
            column: For kind='url': optional column.
            condition: JavaScript expression; break only when it is truthy.
            event_name: For kind='event', e.g. 'click', 'submit', 'setTimeout'.
            text: For kind='text': the code text to find and break on.
            is_regex: For kind='text': treat text as a regular expression.
            occurrence: For kind='text': which match to use when several hit
                (0 is the first; other matches are listed in the result).
            handle: For action='remove': the breakpoint handle.
            page: Tab id.
        """
        async with use_lease(lease, exclusive=False) as (obj, session):
            debugger = await session.debugger_for(page)

            if action == "list":
                return {"ok": True, **debugger.snapshot()}
            if action == "remove":
                if not handle:
                    raise InvalidArgument("action='remove' needs handle")
                removed = await debugger.remove_breakpoint(handle)
                return {"ok": True, "removed": removed, "handle": handle}
            if action != "set":
                raise InvalidArgument("action must be set, remove or list")

            if kind == "text":
                if not text:
                    raise InvalidArgument("kind='text' needs text")
                found = await debugger.set_text_breakpoint(
                    text,
                    is_regex=is_regex,
                    occurrence=occurrence,
                    condition=condition,
                )
                return {
                    "ok": True,
                    **found,
                    "next": (
                        "Repeat the action that runs this code, then call js_pause_state. "
                        "If it never hits, another match may be the live one -- set "
                        "occurrence to one of the other_matches indexes."
                    ),
                }

            if kind == "xhr":
                if url_substring is None:
                    raise InvalidArgument(
                        "kind='xhr' needs url_substring (use '' to break on every request)"
                    )
                info = await debugger.set_xhr_breakpoint(url_substring)
            elif kind == "url":
                if not url_pattern or line is None:
                    raise InvalidArgument("kind='url' needs url_pattern and line")
                info = await debugger.set_url_breakpoint(
                    url_pattern=url_pattern, line=line, column=column, condition=condition
                )
            elif kind == "event":
                if not event_name:
                    raise InvalidArgument("kind='event' needs event_name")
                info = await debugger.set_event_breakpoint(event_name)
            else:
                raise InvalidArgument("kind must be text, xhr, url or event")

            result = {
                "ok": True,
                "breakpoint": info.public(),
                "next": (
                    "Now repeat the action that should trigger it (for example "
                    "page_click on the submit button), then call js_pause_state to wait "
                    "for the hit. Page tools are blocked while paused."
                ),
            }
            if kind == "url" and not info.locations:
                result["warning"] = (
                    "The breakpoint did not resolve to a location yet. The url_pattern "
                    "may not match any loaded script (it is a regex -- escape dots and "
                    "slashes), or the script loads later."
                )
            return result

    @mcp.tool(
        description=(
            "Wait for and report the paused state: reason, call stack, and local "
            "plus closure variables for the top frames. This is where you read "
            "values that page_eval cannot reach. Returns paused=false on timeout."
        )
    )
    @guarded
    async def js_pause_state(
        lease: str,
        wait_seconds: float = 30.0,
        expand_scopes: bool = True,
        max_vars: int = 40,
        page: str | None = None,
    ) -> dict[str, Any]:
        """
        Args:
            lease: Lease token.
            wait_seconds: How long to wait for a breakpoint hit (max 120).
            expand_scopes: Include scope variables for the top frames.
            max_vars: Cap on variables per scope.
            page: Tab id.
        """
        # Not exclusive: this call may block for a while, and holding the lease
        # lock would deadlock the js_step needed to release it.
        async with use_lease(lease, exclusive=False, timeout=200) as (obj, session):
            debugger = await session.debugger_for(page)
            hit = await debugger.wait_for_pause(max(0.5, min(wait_seconds, 120.0)))
            if not hit:
                return {
                    "ok": True,
                    "paused": False,
                    "waited_seconds": wait_seconds,
                    "breakpoints": [b.public() for b in debugger.breakpoints.values()],
                    "hint": (
                        "Nothing paused. Trigger the action again, confirm the breakpoint "
                        "resolved (js_breakpoint action='list'), or widen it -- for an "
                        "XHR breakpoint try a shorter url_substring."
                    ),
                }
            return {
                "ok": True,
                **await debugger.pause_state(expand_scopes=expand_scopes, max_vars=max_vars),
            }

    @mcp.tool(
        description=(
            "Evaluate an expression in a paused call frame's scope. Unlike "
            "page_eval this sees local variables, function arguments and closure "
            "variables -- which is usually where the key, salt or unsigned payload "
            "is. Use frame_index from js_pause_state."
        )
    )
    @guarded
    async def js_eval_frame(
        lease: str,
        expression: str,
        frame_index: int = 0,
        max_chars: int = 8000,
        page: str | None = None,
    ) -> dict[str, Any]:
        """
        Args:
            lease: Lease token.
            expression: JavaScript to evaluate in that frame.
            frame_index: Frame from js_pause_state (0 is the innermost).
            max_chars: Cap on the rendered result.
            page: Tab id.
        """
        async with use_lease(lease, exclusive=False) as (obj, session):
            debugger = await session.debugger_for(page)
            return await debugger.evaluate_on_frame(
                frame_index=frame_index, expression=expression, max_chars=max_chars
            )

    @mcp.tool(
        description=(
            "Continue execution: action='resume' | 'over' | 'into' | 'out'. Stepping "
            "returns the new paused state. Page tools stay blocked until you resume."
        )
    )
    @guarded
    async def js_step(lease: str, action: str = "resume", page: str | None = None) -> dict[str, Any]:
        """
        Args:
            lease: Lease token.
            action: 'resume' | 'over' | 'into' | 'out'.
            page: Tab id.
        """
        async with use_lease(lease, exclusive=False) as (obj, session):
            debugger = await session.debugger_for(page)
            return await debugger.step(action)

    @mcp.tool(
        description=(
            "Expand a JavaScript object by object_id (returned wherever a value was "
            "too large to inline). Use it to walk into request payloads, config "
            "objects or crypto key material."
        )
    )
    @guarded
    async def js_properties(
        lease: str,
        object_id: str,
        max_props: int = 80,
        own_only: bool = True,
        page: str | None = None,
    ) -> dict[str, Any]:
        """
        Args:
            lease: Lease token.
            object_id: Object id from js_pause_state or js_eval_frame.
            max_props: Maximum properties to return.
            own_only: Exclude inherited properties.
            page: Tab id.
        """
        async with use_lease(lease, exclusive=False) as (obj, session):
            debugger = await session.debugger_for(page)
            return {
                "ok": True,
                **await debugger.get_properties(
                    object_id, max_props=max_props, own_only=own_only
                ),
            }

    @mcp.tool(
        description=(
            "Debugger options.\n"
            "  pause_on_exceptions='all'|'uncaught'|'caught'|'none' finds where errors "
            "originate.\n"
            "  blackbox_patterns skips framework and vendor code when stepping, so you "
            "land in application code instead of inside React.\n"
            "  breakpoints_active=false disables your breakpoints temporarily.\n"
            "  pauses_enabled=false suppresses EVERY pause, including `debugger;` "
            "statements in the page's own code. Pausing starts off and is switched on "
            "automatically when you set a breakpoint. Turn it back off if a page uses "
            "anti-debug `debugger;` loops that keep freezing it -- that also removes the "
            "timing signature such pages look for."
        )
    )
    @guarded
    async def js_options(
        lease: str,
        pause_on_exceptions: str | None = None,
        blackbox_patterns: list[str] | None = None,
        breakpoints_active: bool | None = None,
        pauses_enabled: bool | None = None,
        page: str | None = None,
    ) -> dict[str, Any]:
        """
        Args:
            lease: Lease token.
            pause_on_exceptions: 'none' | 'caught' | 'uncaught' | 'all'.
            blackbox_patterns: Regexes of script URLs to step over, e.g.
                ['/node_modules/', 'react-dom'].
            breakpoints_active: Enable or disable the breakpoints you have set.
            pauses_enabled: Allow or suppress all pausing, the page's own
                `debugger;` statements included.
            page: Tab id.
        """
        async with use_lease(lease, exclusive=False) as (obj, session):
            debugger = await session.debugger_for(page)
            changed: list[str] = []
            if pauses_enabled is not None:
                await debugger.set_pauses_enabled(pauses_enabled)
                changed.append(f"pauses_enabled={pauses_enabled}")
            if pause_on_exceptions is not None:
                await debugger.set_pause_on_exceptions(pause_on_exceptions)
                changed.append(f"pause_on_exceptions={pause_on_exceptions}")
            if blackbox_patterns is not None:
                await debugger.set_blackbox(blackbox_patterns)
                changed.append(f"blackbox={len(blackbox_patterns)} pattern(s)")
            if breakpoints_active is not None:
                await debugger.set_breakpoints_active(breakpoints_active)
                changed.append(f"breakpoints_active={breakpoints_active}")
            return {"ok": True, "changed": changed, **debugger.snapshot()}

    # ----- hooks -----------------------------------------------------------

    @mcp.tool(description=HOOK_DOC)
    @guarded
    async def js_hook(
        lease: str,
        action: str = "add",
        targets: list[str] | None = None,
        custom_paths: list[str] | None = None,
        arg_filter: str | None = None,
        break_on_match: str | None = None,
        capture_stack: bool = True,
        stack_depth: int = 8,
        max_arg_chars: int = 800,
        handle: str | None = None,
    ) -> dict[str, Any]:
        """
        Args:
            lease: Lease token.
            action: 'add' | 'remove' | 'list' | 'clear'.
            targets: Preset bundles to hook (see the tool description).
            custom_paths: Arbitrary function paths, e.g. ['window.app.sign'].
            arg_filter: Only record calls whose arguments contain this substring.
            break_on_match: Fire `debugger;` when an argument matches. Needs debug='full'.
            capture_stack: Record the call stack for each call (recommended).
            stack_depth: Stack frames to keep.
            max_arg_chars: Cap on each recorded argument.
            handle: For action='remove': the hook handle.
        """
        async with use_lease(lease) as (obj, session):
            if action == "list":
                return {
                    "ok": True,
                    **session.hooks.stats(),
                    "in_page": await session.hook_status(),
                }
            if action == "clear":
                removed = session.hooks.clear_specs()
                return {
                    "ok": True,
                    "removed_specs": removed,
                    "note": (
                        "No new hooks will be installed on future page loads. Functions "
                        "already wrapped in the current document stay wrapped until a "
                        "reload -- call page_navigate(action='reload') for a clean page."
                    ),
                }
            if action == "remove":
                if not handle:
                    raise InvalidArgument("action='remove' needs handle")
                removed = session.hooks.remove(handle)
                return {
                    "ok": True,
                    "removed": removed,
                    "note": "Reload the page to drop wrappers from the current document.",
                }
            if action != "add":
                raise InvalidArgument("action must be add, remove, list or clear")

            if not targets and not custom_paths:
                raise InvalidArgument(
                    "pass targets and/or custom_paths",
                    hint=f"Presets: {', '.join(PRESET_NAMES)}",
                )
            spec = session.hooks.add(
                targets=targets or [],
                custom_paths=custom_paths,
                arg_filter=arg_filter,
                break_on_match=break_on_match,
                capture_stack=capture_stack,
                max_arg_chars=max_arg_chars,
                stack_depth=stack_depth,
            )
            applied = await session.apply_hooks()

            installed = applied.get("pages", {})
            missing: list[str] = []
            for status in installed.values():
                for key, value in (status or {}).get("installed", {}).items():
                    if value != "ok":
                        missing.append(key.split(":", 1)[-1])

            result: dict[str, Any] = {
                "ok": True,
                "handle": spec["handle"],
                "hooked": [i["label"] for i in spec["instructions"]],
                "pages": installed,
                "next": (
                    "Now drive the page (page_click, page_type...), then read "
                    "js_hook_records. The `stack` on each record names the function "
                    "that made the call -- that is where to set a breakpoint."
                ),
            }
            if applied.get("debug", {}).get("changed"):
                result["debug"] = applied["debug"]
            if missing:
                result["not_found"] = sorted(set(missing))
                result["not_found_note"] = (
                    "These paths did not exist in the page, which is normal for optional "
                    "library globals such as CryptoJS. They are retried on each new "
                    "document, so a reload may pick them up."
                )
            if break_on_match and session.debug_mode != "full":
                result["warning"] = (
                    "break_on_match needs debug='full' to pause; without a debugger "
                    "attached the injected `debugger;` does nothing. Calls are still "
                    "recorded."
                )
            return result

    @mcp.tool(
        description=(
            "Package everything needed to re-run an algorithm OUTSIDE the browser, "
            "into a directory on the gateway host.\n"
            "Understanding a signing routine in the debugger is not the end of the "
            "job -- reproducing it is. That needs three things that are easy to lose: "
            "the code, the environment values it reads, and known input/output pairs "
            "to check against. This writes all three plus a Node scaffold wired "
            "together, so `node rebuild.js` verifies a reimplementation against the "
            "calls actually observed.\n"
            "Hook the function (js_hook) and drive the page first, so there are "
            "vectors to capture."
        )
    )
    @guarded
    async def js_export_bundle(
        lease: str,
        script_urls: list[str] | None = None,
        hook_labels: list[str] | None = None,
        include_cookies: bool = False,
        include_storage: bool = True,
        notes: str | None = None,
        page: str | None = None,
    ) -> dict[str, Any]:
        """
        Args:
            lease: Lease token.
            script_urls: URL substrings of scripts to save, e.g. ['app.min.js'].
                Find them with js_scripts.
            hook_labels: Which hook labels become test vectors, e.g.
                ['JSON.stringify', 'subtle.sign']. Omit for all recorded calls.
            include_cookies: Include cookie values in the bundle (sensitive).
            include_storage: Include localStorage/sessionStorage contents.
            notes: Free text recorded in the bundle README, e.g. what you found.
            page: Tab id.
        """
        async with use_lease(lease) as (obj, session):
            handle = session.handle(page)
            return {
                "ok": True,
                **await session.export_rebuild_bundle(
                    handle,
                    script_urls=script_urls,
                    hook_labels=hook_labels,
                    include_cookies=include_cookies,
                    include_storage=include_storage,
                    notes=notes,
                ),
            }

    @mcp.tool(
        description=(
            "Read recorded hook calls, newest first. Each record has the arguments, "
            "the return value and the **call stack** that produced it. Filter with "
            "text_contains to find the call carrying a particular token or parameter, "
            "and poll incrementally with since_seq."
        )
    )
    @guarded
    async def js_hook_records(
        lease: str,
        label_contains: str | None = None,
        text_contains: str | None = None,
        phase: str | None = None,
        since_seq: int = 0,
        limit: int = 40,
        max_arg_chars: int = 400,
    ) -> dict[str, Any]:
        """
        Args:
            lease: Lease token.
            label_contains: Filter by hook label, e.g. 'xhr' or 'JSON.stringify'.
            text_contains: Only records whose arguments, result or stack contain this.
            phase: 'call' | 'return' | 'throw' | 'get' | 'set'.
            since_seq: Return records newer than this seq, for incremental polling.
            limit: Maximum records (max 200).
            max_arg_chars: Cap on each argument in the output.
        """
        async with use_lease(lease, exclusive=False) as (obj, session):
            # In debug='hooks' mode there is no CDP binding, so records are
            # buffered in the page until we pull them.
            drained = 0
            if not session.hooks.use_binding:
                drained = await session.drain_hooks()
            records = session.hooks.query(
                label_contains=label_contains,
                text_contains=text_contains,
                phase=phase,
                since_seq=since_seq,
                limit=max(1, min(limit, 200)),
            )
            stats = session.hooks.stats()
            out: dict[str, Any] = {
                "ok": True,
                "count": len(records),
                "records": [r.public(max_arg_chars) for r in records],
                "totals": {
                    "buffered": stats["records"],
                    "dropped_oldest": stats["dropped_oldest"],
                    "calls_by_label": stats["calls_by_label"],
                    "delivery": stats["delivery"],
                },
            }
            if drained:
                out["drained_now"] = drained
            if not records and not stats["records"]:
                out["hint"] = (
                    "Nothing recorded. Either the hooked functions have not been called "
                    "yet (drive the page first), or they were called during page load "
                    "before the hooks were installed -- reload the page and retry."
                )
            if records:
                out["highest_seq"] = max(r.seq for r in records)
            return out
