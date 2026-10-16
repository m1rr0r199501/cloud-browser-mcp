"""Page driving: navigation, tabs, input, waiting, cookies."""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer
from playwright.async_api import Error as PwError
from playwright.async_api import TimeoutError as PwTimeout

from ..errors import InvalidArgument, ScopeViolation, ToolTimeout
from ._common import guarded, require_one, use_lease


def _clean(exc: object) -> str:
    text = str(exc or "").split("\nCall log:")[0]
    return text.replace("\n", " ")[:300]


async def _page_state(session, handle, *, note: str | None = None) -> dict[str, Any]:
    state: dict[str, Any] = {"ok": True, "page": handle.id, "url": handle.page.url}
    try:
        state["title"] = await handle.page.title()
    except PwError:
        state["title"] = None
    blocked = list(session.netlog.blocked)[-3:]
    if blocked:
        state["scope_blocked_recent"] = blocked
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
                    return {
                        "ok": False,
                        "error": "navigation_failed",
                        "message": _clean(exc),
                        "url": url,
                        "hint": "Check net_requests(only_failed=true) for the underlying "
                        "network error, and whether scope blocked it.",
                    }
                state = await _page_state(session, handle, note=note)
                if response is not None:
                    state["status"] = response.status
                    state["final_url"] = response.url
                return state

            if action == "reload":
                await handle.page.reload(wait_until=wait_until, timeout=timeout)
            elif action == "back":
                await handle.page.go_back(wait_until=wait_until, timeout=timeout)
            elif action == "forward":
                await handle.page.go_forward(wait_until=wait_until, timeout=timeout)
            else:
                raise InvalidArgument("action must be goto, reload, back or forward")
            return await _page_state(session, handle)

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
                handle = session.select(page)
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
            "'clear' empties the field."
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
        require_one(ref=ref, selector=selector)
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
            "Set a file input's files. Paths must already exist on the machine "
            "running this gateway; they are uploaded to the cloud browser."
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
            paths: Local file paths on the gateway host.
            ref: Element ref of the file input.
            selector: CSS selector of the file input.
            page: Tab id.
        """
        require_one(ref=ref, selector=selector)
        async with use_lease(lease) as (obj, session):
            handle = session.handle(page)
            session.ensure_runnable(handle)
            locator = await session.locator_for(handle, ref=ref, selector=selector, text=None)
            await locator.set_input_files(paths, timeout=30_000)
            return {"ok": True, "uploaded": paths, "page": handle.id}

    @mcp.tool(
        description=(
            "Change the conditions the page runs under: network speed, CPU speed, "
            "geolocation, timezone, locale, user agent, viewport, colour scheme.\n"
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
        page: str | None = None,
    ) -> dict[str, Any]:
        """
        Args:
            lease: Lease token.
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
