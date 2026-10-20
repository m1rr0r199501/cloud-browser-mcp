"""Page driving: navigation, tabs, input, waiting, cookies."""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from playwright.async_api import Error as PwError
from playwright.async_api import TimeoutError as PwTimeout

from ..app import get_app
from ..errors import InvalidArgument, ScopeViolation, ToolTimeout
from ._common import guarded, page_condition_flags, require_one, use_lease


def _clean(exc: object) -> str:
    text = str(exc or "").split("\nCall log:")[0]
    return text.replace("\n", " ")[:300]


def _upload_paths(paths: list[str]) -> list[Path]:
    """Confine uploads to the configured directory.

    The agent choosing these paths reads untrusted pages, so an unrestricted
    upload tool is a one-call exfiltration channel for anything on this machine.
    Symlinks are resolved before the check so they cannot point outside.
    """
    if not paths:
        raise InvalidArgument("pass at least one path")
    root = get_app().s.uploads_dir.expanduser().resolve()
    out: list[Path] = []
    for raw in paths:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve()
        if not resolved.is_relative_to(root):
            raise InvalidArgument(
                f"{raw!r} is outside the upload directory {str(root)!r}",
                hint="Copy the file into the upload directory first, or have the operator "
                "set CBMCP_UPLOAD_DIR.",
            )
        if not resolved.is_file():
            raise InvalidArgument(f"no such file: {str(resolved)!r}")
        out.append(resolved)
    return out


_LINK_TARGET_JS = """e => {
  const a = e.closest('a[href]');
  return a ? { href: a.href, target: a.target || '', download: a.hasAttribute('download') } : null;
}"""


def _navigates_in_place(
    link: dict[str, Any] | None, before: str, button: str, modifiers: list[str] | None
) -> bool:
    """Is this a click on a link that should replace the current page?"""
    if not link or button != "left" or modifiers:
        return False
    href = str(link.get("href") or "")
    if not href.startswith(("http://", "https://")) or link.get("download"):
        return False
    if link.get("target") not in ("", "_self", "_top", "_parent"):
        return False
    # Same document, different #fragment: no page change to wait for.
    return href.split("#", 1)[0] != before.split("#", 1)[0]


def _flag_http_error(state: dict[str, Any], status: int | None) -> None:
    """Make a failed page load impossible to miss.

    The navigation itself "works" -- the browser shows the site's error page --
    so the call is still ok. But an agent reading ok:true tends to carry on as
    if the real page were there. Measured: Wikipedia answers Browser Use
    browsers with 429, from the datacenter IP and through the platform's
    residential proxy alike.
    """
    if not status or status < 400:
        return
    state["http_error"] = status
    if status in (403, 429, 503):
        state["warning"] = (
            f"The site answered HTTP {status}: this is its error/block page, not the "
            "content. The site is rate-limiting or blocking this browser, by IP or by "
            "recognising automation. Waiting and retrying can help; a residential proxy "
            "(CBMCP_NETWORK_MODE=platform_proxy, billed per GB) helps only with IP-based "
            "blocks. Do not hammer the site."
        )
    else:
        state["warning"] = f"The site answered HTTP {status}; this is an error page."


async def _page_state(
    session,
    handle,
    *,
    note: str | None = None,
    check_error_page: bool = False,
    response_missing: bool = False,
) -> dict[str, Any]:
    state: dict[str, Any] = {"ok": True, "page": handle.id, "url": handle.page.url}
    try:
        state["title"] = await handle.page.title()
    except PwError:
        state["title"] = None
    blocked = list(session.netlog.blocked)[-3:]
    if blocked:
        state["scope_blocked_recent"] = blocked
    href = None
    if check_error_page:
        # Right after a failed navigation Playwright still reports the
        # requested URL; the document itself knows it is chrome-error://.
        with contextlib.suppress(PwError, PwTimeout, TimeoutError):
            async with asyncio.timeout(5):
                href = await handle.page.evaluate("location.href")
    state.update(
        page_condition_flags(session, handle, url=href, response_missing=response_missing)
    )
    if state.get("browser_error_page") and state.get("status"):
        _flag_http_error(state, state["status"])
    # What a person would notice after an action: a new tab, a dialog, a file.
    opened = session.take_new_pages()
    if opened:
        state["new_tabs"] = opened
        state["new_tabs_hint"] = (
            "The page opened new tab(s). Switch with page_tabs(action='select', page=...)."
        )
    dialogs = session.take_new_dialogs()
    if dialogs:
        state["dialogs"] = dialogs
        if any(d["handled"] == "dismissed" and d["type"] in ("confirm", "prompt") for d in dialogs):
            state["dialog_hint"] = (
                "A confirm/prompt was dismissed (Cancel). To answer OK, call "
                "page_dialog(action='accept') and repeat the action."
            )
    # Finished downloads are reported once; ones still running are mentioned
    # every time until they finish, so the outcome is never missed.
    fresh = [
        d for d in session.downloads if not d.get("_reported") and d["status"] != "in_progress"
    ]
    for d in fresh:
        d["_reported"] = True
    running = [d["id"] for d in session.downloads if d["status"] == "in_progress"]
    if fresh:
        state["downloads"] = [
            {k: v for k, v in d.items() if not k.startswith("_")} for d in fresh
        ]
    if running:
        state["downloads_in_progress"] = running
    if note:
        state["note"] = note
    return state


def register(mcp: MCPServer) -> None:
    @mcp.tool(
        description=(
            "Navigate: action='goto' (needs url), 'reload', 'back' or 'forward'. "
            "Reload is also how you re-run a page's startup code with hooks or the "
            "debugger already attached."
        )
    )
    @guarded
    async def page_navigate(
        lease: str,
        url: str | None = None,
        action: str = "goto",
        page: str | None = None,
        wait_until: str = "domcontentloaded",
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        """
        Args:
            lease: Lease token from browser_acquire.
            url: Target URL (required for action='goto').
            action: 'goto' | 'reload' | 'back' | 'forward'.
            page: Tab id such as 'p1'. Defaults to the active tab.
            wait_until: 'commit' | 'domcontentloaded' | 'load' | 'networkidle'.
            timeout_ms: Navigation timeout.
        """
        async with use_lease(lease) as (obj, session):
            handle = session.handle(page)
            session.ensure_runnable(handle)
            timeout = timeout_ms or 45_000

            if action == "goto":
                if not url:
                    raise InvalidArgument("action='goto' needs a url")
                ok, reason = obj.scope.permits(url)
                if not ok:
                    if obj.scope.mode == "enforce":
                        raise ScopeViolation(
                            f"navigation blocked: {reason}", url=url, scope=obj.scope.describe()
                        )
                    note = f"WARNING: {reason} (scope mode is '{obj.scope.mode}', not enforced)"
                else:
                    note = None
                try:
                    response = await handle.page.goto(
                        url, wait_until=wait_until, timeout=timeout
                    )
                except PwTimeout as exc:
                    raise ToolTimeout(
                        f"navigation to {url} timed out after {timeout}ms: {_clean(exc)}",
                        hint="Try wait_until='commit' or a longer timeout_ms; the page may "
                        "still have partially loaded -- check page_snapshot.",
                    ) from exc
                except PwError as exc:
                    failed: dict[str, Any] = {
                        "ok": False,
                        "error": "navigation_failed",
                        "message": _clean(exc),
                        "url": url,
                        "hint": "Check net_requests(only_failed=true) for the underlying "
                        "network error, and whether scope blocked it.",
                    }
                    if "ERR_HTTP_RESPONSE_CODE_FAILURE" in str(exc):
                        # Measured: an HTTP error with an empty body. Say what
                        # happened in words, and that the tab now shows Chrome's
                        # own error page, not anything the site sent.
                        failed.update(page_condition_flags(session, handle, url="chrome-error://"))
                        status = failed.get("status")
                        failed["message"] = (
                            f"The site answered HTTP {status or '4xx/5xx'} with an empty body. "
                            "The tab now shows the browser's own error page."
                        )
                        if status:
                            _flag_http_error(failed, status)
                    return failed
                suspicious = response is None or response.status >= 400
                state = await _page_state(
                    session,
                    handle,
                    note=note,
                    check_error_page=suspicious,
                    response_missing=response is None,
                )
                if response is not None:
                    state["status"] = response.status
                    state["final_url"] = response.url
                    _flag_http_error(state, response.status)
                return state

            if action not in ("reload", "back", "forward"):
                raise InvalidArgument("action must be goto, reload, back or forward")
            page_obj = handle.page
            if action == "reload":
                step = page_obj.reload
            elif action == "back":
                step = page_obj.go_back
            else:
                step = page_obj.go_forward
            before = page_obj.url
            try:
                # Measured on Browser Use: a page restored from the back/forward
                # cache never fires DOMContentLoaded or load again, so waiting
                # for either hangs until the timeout. Wait for the navigation to
                # commit, then for the requested state only if the document is
                # not already past it.
                response = await step(wait_until="commit", timeout=timeout)
            except PwTimeout as exc:
                raise ToolTimeout(
                    f"{action} timed out after {timeout}ms: {_clean(exc)}",
                    hint="Check page_snapshot -- the page may have changed anyway.",
                ) from exc
            except PwError as exc:
                return {
                    "ok": False,
                    "error": "navigation_failed",
                    "message": _clean(exc),
                    "action": action,
                    "hint": "Check net_requests(only_failed=true) for the network error.",
                }
            if wait_until != "commit":
                with contextlib.suppress(PwTimeout, PwError):
                    ready = await page_obj.evaluate("document.readyState")
                    if not (ready == "complete" or (ready == "interactive"
                                                    and wait_until == "domcontentloaded")):
                        await page_obj.wait_for_load_state(
                            wait_until, timeout=min(timeout, 15_000)
                        )
            state = await _page_state(
                session,
                handle,
                check_error_page=response is None or response.status >= 400,
            )
            if action != "reload" and response is None and page_obj.url == before:
                state["note"] = f"No {action} history entry; the page did not change."
            if response is not None:
                state["status"] = response.status
                _flag_http_error(state, response.status)
            return state

    @mcp.tool(
        description="Manage tabs: action='list' | 'new' | 'close' | 'select'."
    )
    @guarded
    async def page_tabs(
        lease: str,
        action: str = "list",
        page: str | None = None,
        url: str | None = None,
    ) -> dict[str, Any]:
        """
        Args:
            lease: Lease token.
            action: 'list' | 'new' | 'close' | 'select'.
            page: Tab id, for 'close' and 'select'.
            url: Optional URL to open in a new tab.
        """
        async with use_lease(lease) as (obj, session):
            if action == "list":
                return {"ok": True, "tabs": await session.tabs()}
            if action == "new":
                handle = await session.new_page()
                if url:
                    ok, reason = obj.scope.permits(url)
                    if not ok and obj.scope.mode == "enforce":
                        raise ScopeViolation(f"navigation blocked: {reason}", url=url)
                    await handle.page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                return {"ok": True, "page": handle.id, "tabs": await session.tabs()}
            if action == "close":
                if not page:
                    raise InvalidArgument("action='close' needs page")
                await session.close_page(page)
                return {"ok": True, "closed": page, "tabs": await session.tabs()}
            if action == "select":
                if not page:
                    raise InvalidArgument("action='select' needs page")
                handle = await session.select(page)
                return {"ok": True, "active_page": handle.id, "url": handle.page.url}
            raise InvalidArgument("action must be list, new, close or select")

    @mcp.tool(
        description=(
            "Click an element. Identify it by `ref` from page_snapshot (most "
            "reliable), or by CSS `selector`, or by visible `text`."
        )
    )
    @guarded
    async def page_click(
        lease: str,
        ref: str | None = None,
        selector: str | None = None,
        text: str | None = None,
        page: str | None = None,
        button: str = "left",
        click_count: int = 1,
        modifiers: list[str] | None = None,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        """
        Args:
            lease: Lease token.
            ref: Element ref from page_snapshot, e.g. 'e12'.
            selector: CSS selector, used if ref is omitted.
            text: Visible text, used if ref and selector are omitted.
            page: Tab id. Defaults to the active tab.
            button: 'left' | 'right' | 'middle'.
            click_count: 2 for a double click.
            modifiers: Held keys, e.g. ['Shift', 'Control'].
            timeout_ms: How long to wait for the element to be actionable.
        """
        require_one(ref=ref, selector=selector, text=text)
        async with use_lease(lease) as (obj, session):
            handle = session.handle(page)
            session.ensure_runnable(handle)
            locator = await session.locator_for(handle, ref=ref, selector=selector, text=text)
            before = handle.page.url
            link: dict[str, Any] | None = None
            with contextlib.suppress(PwError, PwTimeout):
                link = await locator.evaluate(_LINK_TARGET_JS, timeout=2_000)
            try:
                await locator.click(
                    button=button,
                    click_count=click_count,
                    modifiers=modifiers or [],
                    timeout=timeout_ms or 15_000,
                )
            except PwTimeout as exc:
                raise ToolTimeout(
                    f"the element was not clickable in time: {_clean(exc)}",
                    hint="It may be covered, disabled or off-screen. Re-run page_snapshot, "
                    "or page_scroll to it first.",
                ) from exc
            # A click on a same-tab link on a single-page app (GitHub, measured)
            # changes the URL a second or two AFTER the click, with no load
            # event, so the old page used to be reported. A person waits for the
            # new page; so does this -- for links only, so other clicks stay fast.
            if _navigates_in_place(link, before, button, modifiers):
                with contextlib.suppress(PwTimeout, PwError):
                    await handle.page.wait_for_url(lambda u: u != before, timeout=3_000)
            # A click often triggers navigation; settle briefly so the caller sees
            # the resulting page rather than the old one.
            try:
                await handle.page.wait_for_load_state("domcontentloaded", timeout=5_000)
            except (PwTimeout, PwError):
                pass
            return await _page_state(session, handle)

    @mcp.tool(
        description=(
            "Enter text. mode='fill' replaces the value in one step (fast, the "
            "default), 'type' sends real keystrokes so JS keypress handlers fire, "
            "'clear' empties the field. With mode='type' and no ref/selector the "
            "keystrokes go to whatever has focus, like a person typing -- useful "
            "for canvas editors, rich-text widgets and fields reached with Tab."
        )
    )
    @guarded
    async def page_type(
        lease: str,
        text: str = "",
        ref: str | None = None,
        selector: str | None = None,
        mode: str = "fill",
        submit: bool = False,
        delay_ms: int = 0,
        page: str | None = None,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        """
        Args:
            lease: Lease token.
            text: The text to enter.
            ref: Element ref from page_snapshot.
            selector: CSS selector, used if ref is omitted.
            mode: 'fill' | 'type' | 'clear'.
            submit: Press Enter afterwards.
            delay_ms: Per-keystroke delay for mode='type'.
            page: Tab id.
            timeout_ms: Actionability timeout.
        """
        if not (ref or selector):
            if mode != "type":
                require_one(ref=ref, selector=selector)
            async with use_lease(lease) as (obj, session):
                handle = session.handle(page)
                session.ensure_runnable(handle)
                await handle.page.keyboard.type(text, delay=delay_ms)
                if submit:
                    await handle.page.keyboard.press("Enter")
                    try:
                        await handle.page.wait_for_load_state("domcontentloaded", timeout=5_000)
                    except (PwTimeout, PwError):
                        pass
                return await _page_state(session, handle)
        async with use_lease(lease) as (obj, session):
            handle = session.handle(page)
            session.ensure_runnable(handle)
            locator = await session.locator_for(handle, ref=ref, selector=selector, text=None)
            timeout = timeout_ms or 15_000
            if mode == "clear":
                await locator.fill("", timeout=timeout)
            elif mode == "type":
                await locator.click(timeout=timeout)
                await locator.press_sequentially(text, delay=delay_ms, timeout=timeout)
            elif mode == "fill":
                await locator.fill(text, timeout=timeout)
            else:
                raise InvalidArgument("mode must be fill, type or clear")
            if submit:
                await locator.press("Enter", timeout=timeout)
                try:
                    await handle.page.wait_for_load_state("domcontentloaded", timeout=5_000)
                except (PwTimeout, PwError):
                    pass
            return await _page_state(session, handle)

    @mcp.tool(description="Choose option(s) in a <select>, by value or by label.")
    @guarded
    async def page_select(
        lease: str,
        values: list[str],
        ref: str | None = None,
        selector: str | None = None,
        by_label: bool = False,
        page: str | None = None,
    ) -> dict[str, Any]:
        """
        Args:
            lease: Lease token.
            values: Option values (or labels when by_label is true).
            ref: Element ref from page_snapshot.
            selector: CSS selector, used if ref is omitted.
            by_label: Match visible labels instead of values.
            page: Tab id.
        """
        require_one(ref=ref, selector=selector)
        async with use_lease(lease) as (obj, session):
            handle = session.handle(page)
            session.ensure_runnable(handle)
            locator = await session.locator_for(handle, ref=ref, selector=selector, text=None)
            if by_label:
                chosen = await locator.select_option(label=values, timeout=15_000)
            else:
                chosen = await locator.select_option(values, timeout=15_000)
            return {"ok": True, "selected": chosen, "page": handle.id}

    @mcp.tool(
        description=(
            "Press a key or chord, e.g. 'Enter', 'Escape', 'Control+a', 'Tab'. "
            "Targets an element when ref/selector is given, otherwise the page."
        )
    )
    @guarded
    async def page_press(
        lease: str,
        key: str,
        ref: str | None = None,
        selector: str | None = None,
        page: str | None = None,
        repeat: int = 1,
    ) -> dict[str, Any]:
        """
        Args:
            lease: Lease token.
            key: Key name or chord.
            ref: Optional element ref to focus first.
            selector: Optional CSS selector to focus first.
            page: Tab id.
            repeat: Press this many times (max 50).
        """
        async with use_lease(lease) as (obj, session):
            handle = session.handle(page)
            session.ensure_runnable(handle)
            count = max(1, min(repeat, 50))
            if ref or selector:
                locator = await session.locator_for(
                    handle, ref=ref, selector=selector, text=None
                )
                for _ in range(count):
                    await locator.press(key, timeout=15_000)
            else:
                for _ in range(count):
                    await handle.page.keyboard.press(key)
            return await _page_state(session, handle)

    @mcp.tool(
        description=(
            "Scroll the page, or scroll an element into view when ref/selector is "
            "given. Useful before clicking something below the fold."
        )
    )
    @guarded
    async def page_scroll(
        lease: str,
        direction: str = "down",
        amount: int = 600,
        ref: str | None = None,
        selector: str | None = None,
        page: str | None = None,
    ) -> dict[str, Any]:
        """
        Args:
            lease: Lease token.
            direction: 'down' | 'up' | 'left' | 'right' | 'top' | 'bottom'.
            amount: Pixels to scroll.
            ref: Element ref to scroll into view instead.
            selector: CSS selector to scroll into view instead.
            page: Tab id.
        """
        async with use_lease(lease) as (obj, session):
            handle = session.handle(page)
            session.ensure_runnable(handle)
            if ref or selector:
                locator = await session.locator_for(
                    handle, ref=ref, selector=selector, text=None
                )
                await locator.scroll_into_view_if_needed(timeout=15_000)
                return {"ok": True, "scrolled_to": ref or selector, "page": handle.id}
            deltas = {
                "down": (0, amount),
                "up": (0, -amount),
                "right": (amount, 0),
                "left": (-amount, 0),
            }
            if direction == "top":
                await handle.page.evaluate("window.scrollTo(0, 0)")
            elif direction == "bottom":
                await handle.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            elif direction in deltas:
                dx, dy = deltas[direction]
                await handle.page.mouse.wheel(dx, dy)
            else:
                raise InvalidArgument(
                    "direction must be down, up, left, right, top or bottom"
                )
            position = await handle.page.evaluate(
                "({y: window.scrollY, h: document.body.scrollHeight,"
                " vh: window.innerHeight})"
            )
            return {"ok": True, "page": handle.id, "scroll": position}

    @mcp.tool(
        description=(
            "Wait for a condition: a selector's state, visible text, a URL pattern, "
            "network idle, or a fixed delay. Prefer a condition over a fixed delay."
        )
    )
    @guarded
    async def page_wait(
        lease: str,
        selector: str | None = None,
        text: str | None = None,
        url_pattern: str | None = None,
        state: str = "visible",
        network_idle: bool = False,
        delay_ms: int | None = None,
        timeout_ms: int | None = None,
        page: str | None = None,
    ) -> dict[str, Any]:
        """
        Args:
            lease: Lease token.
            selector: CSS selector to wait for.
            text: Visible text to wait for.
            url_pattern: Glob or regex the URL must match.
            state: For selector waits: 'attached' | 'detached' | 'visible' | 'hidden'.
            network_idle: Wait until network activity settles.
            delay_ms: Fixed delay, used only when nothing else is given.
            timeout_ms: Overall timeout.
            page: Tab id.
        """
        async with use_lease(lease) as (obj, session):
            handle = session.handle(page)
            session.ensure_runnable(handle)
            timeout = timeout_ms or 20_000
            waited: list[str] = []
            try:
                if selector:
                    await handle.page.wait_for_selector(
                        selector, state=state, timeout=timeout
                    )
                    waited.append(f"selector {selector!r} -> {state}")
                if text:
                    await handle.page.get_by_text(text, exact=False).first.wait_for(
                        state="visible", timeout=timeout
                    )
                    waited.append(f"text {text!r}")
                if url_pattern:
                    await handle.page.wait_for_url(url_pattern, timeout=timeout)
                    waited.append(f"url {url_pattern!r}")
                if network_idle:
                    await handle.page.wait_for_load_state("networkidle", timeout=timeout)
                    waited.append("network idle")
                if not waited:
                    await handle.page.wait_for_timeout(min(delay_ms or 1000, 30_000))
                    waited.append(f"{delay_ms or 1000}ms delay")
            except PwTimeout as exc:
                return {
                    "ok": False,
                    "error": "wait_timeout",
                    "message": _clean(exc),
                    "waited_for": waited,
                    "url": handle.page.url,
                    "hint": "Check page_snapshot to see what actually rendered.",
                }
            return {"ok": True, "waited_for": waited, **await _page_state(session, handle)}

    @mcp.tool(
        description=(
            "Set a file input's files. Files must be inside the gateway's upload "
            "directory (CBMCP_UPLOAD_DIR, default <data dir>/uploads); relative "
            "paths are resolved against it. They are uploaded to the cloud browser."
        )
    )
    @guarded
    async def page_upload(
        lease: str,
        paths: list[str],
        ref: str | None = None,
        selector: str | None = None,
        page: str | None = None,
    ) -> dict[str, Any]:
        """
        Args:
            lease: Lease token.
            paths: Files inside the upload directory (absolute, or relative to it).
            ref: Element ref of the file input.
            selector: CSS selector of the file input.
            page: Tab id.
        """
        require_one(ref=ref, selector=selector)
        resolved = _upload_paths(paths)
        async with use_lease(lease) as (obj, session):
            handle = session.handle(page)
            session.ensure_runnable(handle)
            locator = await session.locator_for(handle, ref=ref, selector=selector, text=None)
            await locator.set_input_files([str(p) for p in resolved], timeout=30_000)
            return {"ok": True, "uploaded": [str(p) for p in resolved], "page": handle.id}

    @mcp.tool(
        description=(
            "Change the conditions the page runs under: network speed, CPU speed, "
            "geolocation, timezone, locale, user agent, viewport, colour scheme, "
            "and site permissions (what clicking 'Allow' on a prompt does).\n"
            "Sites often behave differently by geography, locale and connection "
            "quality -- different endpoints, different consent flows, different "
            "anti-fraud thresholds -- so this is how you see those paths. Network "
            "presets: offline, slow-3g, fast-3g, slow-4g, fast-4g, none (clears).\n"
            "Applies to one tab and is not inherited by tabs opened later. Reload "
            "the page if the site reads these only at startup."
        )
    )
    @guarded
    async def page_emulate(
        lease: str,
        network: str | None = None,
        cpu_throttle: float | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        timezone: str | None = None,
        locale: str | None = None,
        user_agent: str | None = None,
        accept_language: str | None = None,
        viewport_width: int | None = None,
        viewport_height: int | None = None,
        dark_mode: bool | None = None,
        grant_permissions: list[str] | None = None,
        page: str | None = None,
    ) -> dict[str, Any]:
        """
        Args:
            lease: Lease token.
            grant_permissions: Answer "Allow" to permission prompts for the current
                site, e.g. ['geolocation', 'notifications', 'clipboard-read',
                'clipboard-write', 'camera', 'microphone']. [] revokes all.
            network: 'offline' | 'slow-3g' | 'fast-3g' | 'slow-4g' | 'fast-4g' | 'none'.
            cpu_throttle: Slowdown multiplier, e.g. 4 for 4x slower.
            latitude: Geolocation latitude (pass longitude too).
            longitude: Geolocation longitude.
            timezone: IANA timezone, e.g. 'Asia/Shanghai'.
            locale: BCP-47 locale, e.g. 'zh-CN'.
            user_agent: Override the User-Agent string.
            accept_language: Override the Accept-Language header.
            viewport_width: Viewport width in pixels (pass height too).
            viewport_height: Viewport height in pixels.
            dark_mode: Emulate prefers-color-scheme.
            page: Tab id.
        """
        async with use_lease(lease) as (obj, session):
            handle = session.handle(page)
            return {
                "ok": True,
                **await session.emulate(
                    handle,
                    cpu_throttle=cpu_throttle,
                    network=network,
                    latitude=latitude,
                    longitude=longitude,
                    timezone=timezone,
                    locale=locale,
                    user_agent=user_agent,
                    accept_language=accept_language,
                    viewport_width=viewport_width,
                    viewport_height=viewport_height,
                    dark_mode=dark_mode,
                    grant_permissions=grant_permissions,
                ),
            }

    @mcp.tool(
        description=(
            "Read, add or clear cookies for this browser. action='get' returns "
            "them with their flags, which is often the point of a login analysis."
        )
    )
    @guarded
    async def page_cookies(
        lease: str,
        action: str = "get",
        urls: list[str] | None = None,
        cookies: list[dict] | None = None,
    ) -> dict[str, Any]:
        """
        Args:
            lease: Lease token.
            action: 'get' | 'set' | 'clear'.
            urls: Restrict 'get' to these URLs.
            cookies: Cookie dicts for 'set' (name, value, domain/url, path, ...).
        """
        async with use_lease(lease) as (obj, session):
            if session.context is None:
                raise InvalidArgument("browser is not connected")
            if action == "get":
                found = await session.cookies(urls)
                return {
                    "ok": True,
                    "count": len(found),
                    "cookies": [
                        {
                            "name": c.get("name"),
                            "value": c.get("value"),
                            "domain": c.get("domain"),
                            "path": c.get("path"),
                            "httpOnly": c.get("httpOnly"),
                            "secure": c.get("secure"),
                            "sameSite": c.get("sameSite"),
                            "expires": c.get("expires"),
                        }
                        for c in found
                    ],
                }
            if action == "set":
                if not cookies:
                    raise InvalidArgument("action='set' needs cookies")
                await session.context.add_cookies(cookies)  # type: ignore[arg-type]
                return {"ok": True, "added": len(cookies)}
            if action == "clear":
                await session.context.clear_cookies()
                return {"ok": True, "cleared": True}
            raise InvalidArgument("action must be get, set or clear")

    # ----- person-like pointer and browser chrome --------------------------

    @mcp.tool(
        description=(
            "Move the mouse over an element, like a person pointing at it. Opens "
            "hover menus, tooltips and mega-nav dropdowns that only render on hover."
        )
    )
    @guarded
    async def page_hover(
        lease: str,
        ref: str | None = None,
        selector: str | None = None,
        text: str | None = None,
        page: str | None = None,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        """
        Args:
            lease: Lease token.
            ref: Element ref from page_snapshot.
            selector: CSS selector, used if ref is omitted.
            text: Visible text, used if ref and selector are omitted.
            page: Tab id.
            timeout_ms: How long to wait for the element.
        """
        require_one(ref=ref, selector=selector, text=text)
        async with use_lease(lease) as (obj, session):
            handle = session.handle(page)
            session.ensure_runnable(handle)
            locator = await session.locator_for(handle, ref=ref, selector=selector, text=text)
            try:
                await locator.hover(timeout=timeout_ms or 15_000)
            except PwTimeout as exc:
                raise ToolTimeout(f"could not hover the element: {_clean(exc)}") from exc
            return await _page_state(session, handle)

    @mcp.tool(
        description=(
            "Drag with the mouse held down: an element onto another element "
            "(sortable lists, kanban cards, drop zones), or an element by an "
            "offset in pixels (sliders, range inputs, slide-to-verify widgets). "
            "Moves in small steps so pages that track the pointer see a real drag."
        )
    )
    @guarded
    async def page_drag(
        lease: str,
        source_ref: str | None = None,
        source_selector: str | None = None,
        target_ref: str | None = None,
        target_selector: str | None = None,
        offset_x: float | None = None,
        offset_y: float | None = None,
        steps: int = 20,
        page: str | None = None,
    ) -> dict[str, Any]:
        """
        Args:
            lease: Lease token.
            source_ref: Element to pick up (ref from page_snapshot).
            source_selector: CSS selector of the element to pick up.
            target_ref: Element to drop onto.
            target_selector: CSS selector of the element to drop onto.
            offset_x: Instead of a target, drag this many pixels right (negative: left).
            offset_y: Instead of a target, drag this many pixels down (negative: up).
            steps: Intermediate mouse moves (more = slower, more human).
            page: Tab id.
        """
        require_one(source_ref=source_ref, source_selector=source_selector)
        has_target = bool(target_ref or target_selector)
        if not has_target and offset_x is None and offset_y is None:
            raise InvalidArgument("pass target_ref/target_selector, or offset_x/offset_y")
        async with use_lease(lease) as (obj, session):
            handle = session.handle(page)
            session.ensure_runnable(handle)
            source = await session.locator_for(
                handle, ref=source_ref, selector=source_selector, text=None
            )
            await source.scroll_into_view_if_needed(timeout=15_000)
            start = await source.bounding_box()
            if start is None:
                raise InvalidArgument("the source element is not visible")
            sx = start["x"] + start["width"] / 2
            sy = start["y"] + start["height"] / 2
            if has_target:
                target = await session.locator_for(
                    handle, ref=target_ref, selector=target_selector, text=None
                )
                end = await target.bounding_box()
                if end is None:
                    raise InvalidArgument("the target element is not visible")
                ex = end["x"] + end["width"] / 2
                ey = end["y"] + end["height"] / 2
            else:
                ex = sx + float(offset_x or 0)
                ey = sy + float(offset_y or 0)
            mouse = handle.page.mouse
            await mouse.move(sx, sy)
            await mouse.down()
            # A first small nudge: many drag libraries start only after a
            # few pixels of movement with the button held.
            await mouse.move(sx + 3, sy + 3, steps=2)
            await mouse.move(ex, ey, steps=max(1, min(int(steps), 200)))
            await mouse.up()
            state = await _page_state(session, handle)
            state["dragged"] = {"from": [round(sx), round(sy)], "to": [round(ex), round(ey)]}
            return state

    @mcp.tool(
        description=(
            "Raw mouse at viewport coordinates (CSS pixels, the same frame as "
            "page_screenshot): move, click, double_click, down, up, wheel. For "
            "things with no element to target -- canvas, maps, charts, games, "
            "image CAPTCHAs -- or when you are working from a screenshot."
        )
    )
    @guarded
    async def page_mouse(
        lease: str,
        action: str = "click",
        x: float | None = None,
        y: float | None = None,
        button: str = "left",
        delta_x: float = 0,
        delta_y: float = 0,
        steps: int = 1,
        page: str | None = None,
    ) -> dict[str, Any]:
        """
        Args:
            lease: Lease token.
            action: 'move' | 'click' | 'double_click' | 'down' | 'up' | 'wheel'.
            x: Viewport x in CSS pixels (required except for up/wheel).
            y: Viewport y in CSS pixels.
            button: 'left' | 'right' | 'middle'.
            delta_x: For 'wheel': horizontal scroll amount.
            delta_y: For 'wheel': vertical scroll amount.
            steps: For 'move': intermediate moves along the way.
            page: Tab id.
        """
        if button not in ("left", "right", "middle"):
            raise InvalidArgument("button must be left, right or middle")
        needs_point = action in ("move", "click", "double_click", "down")
        if needs_point and (x is None or y is None):
            raise InvalidArgument(f"action={action!r} needs x and y")
        async with use_lease(lease) as (obj, session):
            handle = session.handle(page)
            session.ensure_runnable(handle)
            mouse = handle.page.mouse
            if action == "move":
                await mouse.move(x, y, steps=max(1, min(int(steps), 200)))
            elif action == "click":
                await mouse.click(x, y, button=button)
            elif action == "double_click":
                await mouse.dblclick(x, y, button=button)
            elif action == "down":
                await mouse.move(x, y)
                await mouse.down(button=button)
            elif action == "up":
                if x is not None and y is not None:
                    await mouse.move(x, y)
                await mouse.up(button=button)
            elif action == "wheel":
                if x is not None and y is not None:
                    await mouse.move(x, y)
                await mouse.wheel(delta_x, delta_y)
            else:
                raise InvalidArgument(
                    "action must be move, click, double_click, down, up or wheel"
                )
            if action in ("click", "double_click", "up"):
                try:
                    await handle.page.wait_for_load_state("domcontentloaded", timeout=5_000)
                except (PwTimeout, PwError):
                    pass
            return await _page_state(session, handle)

    @mcp.tool(
        description=(
            "How the page's alert/confirm/prompt dialogs get answered. They are "
            "answered instantly (an open dialog freezes the page); by default with "
            "Cancel. action='accept' answers OK (with prompt_text for prompt()), "
            "'dismiss' answers Cancel; once=true (default) applies to the next "
            "dialog only. action='status' lists recent dialogs and how each was "
            "answered. Set this BEFORE the click that opens the dialog."
        )
    )
    @guarded
    async def page_dialog(
        lease: str,
        action: str = "status",
        prompt_text: str | None = None,
        once: bool = True,
    ) -> dict[str, Any]:
        """
        Args:
            lease: Lease token.
            action: 'accept' | 'dismiss' | 'status'.
            prompt_text: Text to enter when answering a prompt() with accept.
            once: Apply to the next dialog only, then return to the default.
        """
        async with use_lease(lease, exclusive=False) as (obj, session):
            if action in ("accept", "dismiss"):
                session.dialog_policy = {
                    "action": action,
                    "prompt_text": prompt_text,
                    "once": once,
                }
            elif action != "status":
                raise InvalidArgument("action must be accept, dismiss or status")
            return {
                "ok": True,
                "policy": dict(session.dialog_policy),
                "default": session.s.dialog_default,
                "recent": [
                    {k: v for k, v in d.items() if not k.startswith("_")}
                    for d in list(session.dialogs)[-10:]
                ],
            }

    @mcp.tool(
        description=(
            "Files the page has downloaded in this lease (link clicks, export "
            "buttons, Content-Disposition responses). Each is saved on the gateway "
            "host; the result gives its local path and size. Downloads are captured "
            "automatically -- just click, then check here or in the click result."
        )
    )
    @guarded
    async def page_downloads(lease: str, wait_seconds: float = 0) -> dict[str, Any]:
        """
        Args:
            lease: Lease token.
            wait_seconds: Wait up to this long (max 120) for in-progress downloads.
        """
        async with use_lease(lease, exclusive=False) as (obj, session):
            deadline = time.time() + max(0.0, min(wait_seconds, 120.0))
            while time.time() < deadline and any(
                d["status"] == "in_progress" for d in session.downloads
            ):
                await asyncio.sleep(0.25)
            for d in session.downloads:
                if d["status"] != "in_progress":
                    d["_reported"] = True
            return {
                "ok": True,
                "count": len(session.downloads),
                "downloads": [
                    {k: v for k, v in d.items() if not k.startswith("_")}
                    for d in session.downloads
                ],
            }
