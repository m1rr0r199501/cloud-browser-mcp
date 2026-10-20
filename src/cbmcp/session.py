"""The per-lease browser session.

One ``BrowserSession`` owns exactly one cloud browser: its Playwright
connection, its pages, its network recorder, its debugger sessions and its hook
registry. Nothing is shared between sessions, which is what makes two agents'
leases genuinely isolated -- they are different browsers in different cloud
sandboxes, not two contexts in one browser.

Instrumentation level
---------------------
``debug_mode`` trades capability against how visible the instrumentation is to
the page. The network path (local gateway -> wss -> cloud browser) is invisible
to the site either way; everything detectable happens *inside* the browser.

=========  ================================================  ====================
mode       what it enables                                   what the page can see
=========  ================================================  ====================
``off``    plain driving, network recording, screenshots      nothing added
``hooks``  function hooks, call stacks, argument capture      native functions are
                                                              replaced (disguised
                                                              via toString patch)
``full``   the above plus breakpoints, call frames, scope,    additionally
           XHR/event breakpoints, ``break_on_match``          Runtime.enable and
                                                              Debugger.enable, and
                                                              ``debugger;`` timing
=========  ================================================  ====================

``Runtime.enable`` is the irreducible cost of real debugging: it makes
``console.log(obj)`` eagerly evaluate getters, which is a well-known probe.
Everything else that *can* be hidden, is -- see ``hooks.py`` and ``refs.py``.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import time
import urllib.parse
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import Browser, BrowserContext, Page, Playwright
from playwright.async_api import Error as PwError
from playwright.async_api import TimeoutError as PwTimeout

from .config import Settings
from .debug import JsDebugger
from .errors import (
    BrowserConnectionError,
    DebuggerPaused,
    InvalidArgument,
    NotFound,
    RefNotFound,
    ScopeViolation,
    ToolTimeout,
)
from .hooks import DRAIN_JS, RUNTIME_JS, STATUS_JS, HookRegistry, new_hook_namespace
from .log import get_logger
from .netlog import NetworkRecorder
from .refs import (
    ELEMENT_DETAIL_JS,
    PAGE_INVENTORY_JS,
    PAGE_TEXT_JS,
    SNAPSHOT_JS,
    new_ref_namespace,
    ref_selector,
)
from .scope import ScopeRule

log = get_logger("session")

DEBUG_LEVELS = {"off": 0, "hooks": 1, "full": 2}

# Approximations of the Chrome DevTools network presets, in bytes/second and
# milliseconds of added latency.
NETWORK_PRESETS: dict[str, dict[str, Any]] = {
    "offline": {
        "offline": True,
        "latency": 0,
        "downloadThroughput": 0,
        "uploadThroughput": 0,
    },
    "slow-3g": {
        "offline": False,
        "latency": 2000,
        "downloadThroughput": 51_200,
        "uploadThroughput": 51_200,
    },
    "fast-3g": {
        "offline": False,
        "latency": 563,
        "downloadThroughput": 209_715,
        "uploadThroughput": 96_000,
    },
    "slow-4g": {
        "offline": False,
        "latency": 400,
        "downloadThroughput": 393_216,
        "uploadThroughput": 131_072,
    },
    "fast-4g": {
        "offline": False,
        "latency": 150,
        "downloadThroughput": 1_310_720,
        "uploadThroughput": 655_360,
    },
}


class PageHandle:
    def __init__(self, page_id: str, page: Page) -> None:
        self.id = page_id
        self.page = page
        self.cdp: Any | None = None
        self.debug_attached = False
        self.debugger: JsDebugger | None = None
        self.console: list[dict[str, Any]] = []
        self.errors: list[dict[str, Any]] = []
        self.closed = False
        self.created_at = time.time()


class BrowserSession:
    # Consecutive failed liveness probes before a connected-but-unresponsive
    # browser is treated as dead. At the default 15s sweep that is ~45s.
    LIVENESS_STRIKES = 3

    def __init__(
        self,
        *,
        lease_id: str,
        settings: Settings,
        playwright: Playwright,
        scope: ScopeRule,
        work_dir: Path,
        debug_mode: str = "full",
    ) -> None:
        self.lease_id = lease_id
        self.s = settings
        self._pw = playwright
        self.scope = scope
        self.dir = work_dir
        self.dir.mkdir(parents=True, exist_ok=True)

        if debug_mode not in DEBUG_LEVELS:
            raise InvalidArgument(f"debug must be one of {sorted(DEBUG_LEVELS)}")
        self.debug_mode = debug_mode
        self.debug_upgraded_late = False

        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.pages: dict[str, PageHandle] = {}
        self._page_seq = 0
        self._active_page_id: str | None = None

        # Per-lease random identifiers: no fixed signature for a page to match.
        self.ref_attr, self.ref_seq_key = new_ref_namespace()
        namespace, binding = new_hook_namespace()

        self.netlog = NetworkRecorder(
            lease_dir=self.dir,
            max_entries=settings.max_net_entries,
            max_body_bytes=settings.max_body_bytes,
            disk_quota_bytes=settings.lease_disk_quota_bytes,
            capture_bodies=settings.capture_bodies,
        )
        self.hooks = HookRegistry(namespace=namespace, binding=binding)
        self.hooks.use_binding = debug_mode == "full"
        self.connected = False
        self._artifact_seq = 0
        self._init_script_installed = False
        self._hook_init_script: Any | None = None
        # What to do with alert/confirm/prompt/beforeunload. A person clicks OK
        # on "Are you sure?"; dismissing everything made such flows impossible.
        # The default stays "dismiss" (never confirm something unasked), and
        # page_dialog switches it, once or until changed.
        self.dialog_policy: dict[str, Any] = {
            "action": settings.dialog_default,
            "prompt_text": None,
            "once": False,
        }
        self.dialogs: deque[dict[str, Any]] = deque(maxlen=50)
        # Files the page downloaded, saved on this host.
        self.downloads: list[dict[str, Any]] = []
        self._download_seq = 0
        # Tabs/popups opened by the page since the last tool call reported them.
        self._new_pages: list[str] = []
        self._browser_cdp: Any | None = None
        self.reconnects = 0
        self.liveness_failures = 0
        # Strong references to fire-and-forget tasks. The event loop only keeps
        # weak ones, so a task nobody holds can be collected mid-execution --
        # which here would mean a page never adopted or a pause never resumed.
        self._bg: set[asyncio.Task] = set()

    def _spawn(self, coro: Any) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        return task

    @property
    def debug_level(self) -> int:
        return DEBUG_LEVELS[self.debug_mode]

    # ----- connection ------------------------------------------------------

    async def connect(self, cdp_url: str) -> dict[str, Any]:
        """Connect and prove the browser is actually drivable.

        ``browser_acquire`` must not report success on a session that merely
        exists: an unusable browser still bills, and an agent that believes it
        has a browser will waste a dozen tool calls finding out otherwise.
        """
        started = time.time()
        self.browser = await self._open_cdp(cdp_url)
        await self._attach()
        handle, title = await self._prove_drivable()

        self.connected = True
        log.info(
            "lease %s connected in %dms (pages=%d, debug=%s)",
            self.lease_id,
            int((time.time() - started) * 1000),
            len(self.pages),
            self.debug_mode,
        )
        return {
            "pages": [p.id for p in self.pages.values()],
            "active_page": self._active_page_id,
            "title": title,
            "url": handle.page.url,
            "connect_ms": int((time.time() - started) * 1000),
        }

    # Per-attempt CDP connect timeouts. Measured on Browser Use from a distant
    # client: connects took 3-52s, and one in ten timed out at 60s on the first
    # try while an immediate retry went through.
    CONNECT_ATTEMPTS_MS = (40_000, 40_000, 40_000)

    async def _open_cdp(self, cdp_url: str) -> Browser:
        last: Exception | None = None
        attempts = len(self.CONNECT_ATTEMPTS_MS)
        for i, timeout in enumerate(self.CONNECT_ATTEMPTS_MS):
            try:
                return await self._pw.chromium.connect_over_cdp(cdp_url, timeout=timeout)
            except PwError as exc:
                last = exc
                log.warning(
                    "lease %s: CDP connect attempt %d/%d failed: %s",
                    self.lease_id,
                    i + 1,
                    attempts,
                    _clean(exc),
                )
                if i < attempts - 1:
                    await asyncio.sleep(2.0 * (i + 1))
        raise BrowserConnectionError(
            f"could not open a CDP connection to the cloud browser after {attempts} "
            f"attempts: {_clean(last)}"
        )

    async def _attach(self, previous: list[tuple[str, str]] | None = None) -> None:
        """Take over the browser's context and pages on a fresh connection.

        ``previous`` is [(page_id, url), ...] from before a reconnect: a page at
        the same URL gets its old id back, so an agent's ``page='p2'`` still
        means the same tab.
        """
        assert self.browser is not None
        contexts = self.browser.contexts
        self.context = contexts[0] if contexts else await self.browser.new_context()
        self.context.set_default_timeout(self.s.default_timeout_ms)
        self.context.set_default_navigation_timeout(self.s.navigation_timeout_ms)

        self.netlog.attach(self.context)
        self.context.on("page", lambda page: self._spawn(self._adopt(page)))
        if self.scope.mode == "enforce" and self.scope.restricted:
            await self._install_scope_guard()

        existing = list(self.context.pages)
        if not existing:
            existing = [await self.context.new_page()]
        unused = list(previous or [])
        for page in existing:
            wanted = next((pid for pid, url in unused if url == page.url), None)
            if wanted is not None:
                unused = [(pid, url) for pid, url in unused if pid != wanted]
            await self._adopt(page, page_id=wanted)

    async def _prove_drivable(self) -> tuple[PageHandle, str]:
        """The renderer must execute script and answer, not merely be connected."""
        handle = self.active()
        try:
            async with asyncio.timeout(20):
                value = await handle.page.evaluate("1+1")
                title = await handle.page.title()
        except (PwError, TimeoutError) as exc:
            raise BrowserConnectionError(
                f"the browser connected but is not responding: {_clean(exc)}"
            ) from exc
        if value != 2:
            raise BrowserConnectionError("the browser returned an unexpected probe result")
        return handle, title

    def is_connected(self) -> bool:
        return bool(self.browser is not None and self.connected and self.browser.is_connected())

    async def reconnect(self, cdp_url: str) -> dict[str, Any]:
        """Re-attach to the SAME cloud browser after the connection dropped.

        The cloud browser keeps running when our websocket dies (a Mac going to
        sleep, a network change, a blip on the platform side) -- its tabs,
        cookies and logged-in state are all still there. Reported from real
        use: four connection losses in four minutes, each costing the agent its
        browser and its task. Reconnecting keeps both.

        Lost and not restored: breakpoints, emulation overrides and anything
        else that lived on the old CDP connection. Hooks and the scope guard are
        re-installed.
        """
        started = time.time()
        # Closed ones count too: a dropped connection makes Playwright report
        # every tab as closed (measured: the tab came back as p2 instead of p1).
        # A tab that really was closed is not in the browser any more, so it
        # cannot match; still-open handles are preferred for duplicate URLs.
        handles = sorted(self.pages.values(), key=lambda h: h.closed)
        previous = [(h.id, h.page.url) for h in handles]
        old_active = self._active_page_id
        self.connected = False
        for task in list(self._bg):
            task.cancel()
        self._bg.clear()
        if self.browser is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.browser.close(), timeout=5)
        self.browser = None
        self.context = None
        self._browser_cdp = None
        self.pages = {}
        self._active_page_id = None

        self.browser = await self._open_cdp(cdp_url)
        await self._attach(previous)
        if old_active in self.pages and not self.pages[old_active].closed:
            self._active_page_id = old_active
        self._hook_init_script = None
        if self.hooks.specs:
            with contextlib.suppress(Exception):
                await self.apply_hooks()
        await self._prove_drivable()
        self.connected = True
        self.liveness_failures = 0
        self.reconnects += 1
        ms = int((time.time() - started) * 1000)
        log.info("lease %s reconnected in %dms (pages=%d)", self.lease_id, ms, len(self.pages))
        return {
            "reconnect_ms": ms,
            "pages": [h.id for h in self.pages.values() if not h.closed],
            "active_page": self._active_page_id,
            "not_restored": "breakpoints and emulation settings (set them again if needed)",
        }

    async def network_info(self) -> dict[str, Any]:
        """How this browser really reaches the internet.

        Reported from real use: in 'direct' mode CDP showed a private 10.x
        server_ip and HTTP/1.1 only, because the platform routes traffic through
        its own network. A trace page fetched BY the browser shows what sites
        actually see. Runs in a temporary tab so the agent's page is untouched.
        """
        url = "https://www.cloudflare.com/cdn-cgi/trace"
        ok, reason = self.scope.permits(url)
        if not ok and self.scope.mode == "enforce":
            return {"available": False, "reason": f"blocked by this lease's scope: {reason}"}
        previous_active = self._active_page_id
        handle = await self.new_page()
        text = ""
        try:
            async with asyncio.timeout(20):
                await handle.page.goto(url, wait_until="domcontentloaded")
                text = await handle.page.evaluate("document.body.innerText")
        except (PwError, TimeoutError) as exc:
            return {"available": False, "reason": _clean(exc)}
        finally:
            with contextlib.suppress(Exception):
                await self.close_page(handle.id)
            if previous_active in self.pages and not self.pages[previous_active].closed:
                self._active_page_id = previous_active
        fields = dict(line.split("=", 1) for line in str(text).splitlines() if "=" in line)
        return {
            "available": True,
            "egress_ip": fields.get("ip"),
            "country": fields.get("loc"),
            "http_protocol": fields.get("http"),
            "tls": fields.get("tls"),
            "edge": fields.get("colo"),
            "measured_with": url,
        }

    async def _browser_responds(self) -> bool:
        """Ask the browser itself, not a page: a busy page is not a dead browser."""
        if self.browser is None:
            return False
        try:
            if self._browser_cdp is None:
                self._browser_cdp = await self.browser.new_browser_cdp_session()
            async with asyncio.timeout(10):
                await self._browser_cdp.send("Browser.getVersion")
            return True
        except Exception:  # noqa: BLE001
            self._browser_cdp = None
            return False

    async def _install_scope_guard(self) -> None:
        """Block out-of-scope requests at the request layer, not just on navigate.

        Page JavaScript, redirects and third-party includes all reach hosts the
        agent never typed, so URL checking in ``page_navigate`` alone would be
        theatre.
        """
        assert self.context is not None

        async def guard(route: Any) -> None:
            url = route.request.url
            ok, reason = self.scope.permits(url)
            if ok:
                with contextlib.suppress(PwError):
                    await route.continue_()
                return
            self.netlog.note_blocked(url, reason)
            with contextlib.suppress(PwError):
                await route.abort("blockedbyclient")

        await self.context.route("**/*", guard)
        log.info("scope guard active for lease %s: %s", self.lease_id, self.scope.describe())

    async def _adopt(self, page: Page, page_id: str | None = None) -> PageHandle:
        for handle in self.pages.values():
            if handle.page is page:
                return handle
        if page_id is None or page_id in self.pages:
            self._page_seq += 1
            page_id = f"p{self._page_seq}"
        handle = PageHandle(page_id, page)
        self.pages[page_id] = handle
        if self._active_page_id is None:
            self._active_page_id = page_id
        if self.connected:
            # Opened after connect: a popup, target=_blank link or window.open.
            self._new_pages.append(page_id)

        page.on("console", lambda msg: self._on_console(handle, msg))
        page.on("pageerror", lambda err: self._on_page_error(handle, err))
        page.on("close", lambda _p=None: self._on_page_closed(handle))
        page.on("dialog", lambda dialog: self._spawn(self._on_dialog(handle, dialog)))
        page.on("download", lambda download: self._spawn(self._on_download(handle, download)))
        page.on("framenavigated", lambda frame: self._on_frame_navigated(handle, frame))
        self.netlog.attach_page(page)

        if self.debug_mode == "full":
            with contextlib.suppress(Exception):
                await self._attach_cdp(handle)
        return handle

    async def ensure_debug(self, level: str = "full") -> dict[str, Any]:
        """Raise the instrumentation level, attaching what the new level needs.

        Called by the js_* and hook tools. When the lease was acquired at a lower
        level this happens after the page has already run its scripts, so the
        caller is told that instrumentation added now only sees *future* activity.
        """
        if level not in DEBUG_LEVELS:
            raise InvalidArgument(f"level must be one of {sorted(DEBUG_LEVELS)}")
        want = DEBUG_LEVELS[level]
        if want <= self.debug_level:
            return {"debug_mode": self.debug_mode, "changed": False}

        previous = self.debug_mode
        self.debug_mode = level
        attached: list[str] = []
        if want == DEBUG_LEVELS["full"]:
            self.hooks.use_binding = True
            for handle in self.pages.values():
                if handle.closed or handle.debug_attached:
                    continue
                with contextlib.suppress(Exception):
                    await self._attach_cdp(handle)
                    attached.append(handle.id)
        self.debug_upgraded_late = True
        log.info("lease %s debug level %s -> %s", self.lease_id, previous, level)
        return {
            "debug_mode": self.debug_mode,
            "changed": True,
            "previous": previous,
            "attached_pages": attached,
            "note": (
                f"Instrumentation was raised from '{previous}' to '{level}' after the page "
                "had already loaded, so code that already ran was not observed. Call "
                "page_navigate(action='reload') to capture startup behaviour. Note that "
                "'full' enables Runtime.enable/Debugger.enable, which a page can detect."
            ),
        }

    async def _ensure_cdp(self, handle: PageHandle) -> Any | None:
        """A raw CDP session, with no debug domains enabled.

        Emulation needs CDP but not the debugger, and the debugger's domains are
        what make the browser detectable -- so the two are separate steps.
        """
        if handle.cdp is not None:
            return handle.cdp
        if self.context is None:
            return None
        try:
            handle.cdp = await self.context.new_cdp_session(handle.page)
        except PwError as exc:
            log.warning("no CDP session for %s: %s", handle.id, _clean(exc))
            return None
        return handle.cdp

    async def _attach_cdp(self, handle: PageHandle) -> None:
        assert self.context is not None
        cdp = await self._ensure_cdp(handle)
        if cdp is None:
            return
        handle.debug_attached = True
        await cdp.send("Runtime.enable")

        # Hook records leave the page through this binding, so they survive
        # navigation instead of dying with the document.
        with contextlib.suppress(PwError):
            await cdp.send("Runtime.addBinding", {"name": self.hooks.binding})
        cdp.on("Runtime.bindingCalled", lambda params: self._on_binding(handle, params))

        # Playwright's request events carry no initiator, so the JS call stack
        # behind each request comes from a raw CDP session alongside them.
        with contextlib.suppress(PwError):
            await cdp.send("Network.enable")
            cdp.on(
                "Network.requestWillBeSent",
                lambda params: self.netlog.record_initiator(params),
            )

        if self.s.enable_debugger_tools:
            debugger = JsDebugger(cdp, handle.id)
            with contextlib.suppress(PwError):
                await debugger.enable()
            handle.debugger = debugger
            # Breakpoint intents set earlier in the lease apply to new tabs too.
            await self._reapply_breakpoints(debugger)

    async def _reapply_breakpoints(self, debugger: JsDebugger) -> None:
        source = self._primary_debugger()
        if source is None or source is debugger:
            return
        for info in source.breakpoints.values():
            with contextlib.suppress(Exception):
                if info.kind == "xhr":
                    target = "" if info.detail == "<any request>" else info.detail
                    await debugger.set_xhr_breakpoint(target)
                elif info.kind == "event":
                    await debugger.set_event_breakpoint(info.detail)

    def _primary_debugger(self) -> JsDebugger | None:
        for handle in self.pages.values():
            if handle.debugger is not None and not handle.closed:
                return handle.debugger
        return None

    # ----- event handlers --------------------------------------------------

    def _on_console(self, handle: PageHandle, msg: Any) -> None:
        try:
            entry = {
                "ts": time.time(),
                "page": handle.id,
                "type": msg.type,
                "text": msg.text[:2000],
            }
            with contextlib.suppress(Exception):
                loc = msg.location or {}
                entry["source"] = f"{loc.get('url', '')}:{loc.get('lineNumber', 0)}"
            handle.console.append(entry)
            if len(handle.console) > 500:
                del handle.console[:250]
        except Exception:  # noqa: BLE001
            pass

    def _on_page_error(self, handle: PageHandle, err: Any) -> None:
        text = str(err)
        handle.errors.append(
            {
                "ts": time.time(),
                "page": handle.id,
                "message": text.split("\n")[0][:500],
                "stack": "\n".join(text.split("\n")[1:9])[:2000],
            }
        )
        if len(handle.errors) > 200:
            del handle.errors[:100]

    def _on_page_closed(self, handle: PageHandle) -> None:
        handle.closed = True
        if self._active_page_id == handle.id:
            self._active_page_id = next(
                (h.id for h in self.pages.values() if not h.closed), None
            )

    async def _on_dialog(self, handle: PageHandle, dialog: Any) -> None:
        # An unhandled dialog blocks the page forever, which would look like a
        # hung browser and burn the whole lease. So every dialog is answered at
        # once, the way page_dialog says to (dismiss by default), and recorded.
        kind, message = dialog.type, dialog.message
        policy = self.dialog_policy
        action = policy["action"]
        text = policy.get("prompt_text")
        try:
            if action == "accept":
                if kind == "prompt" and text is not None:
                    await dialog.accept(str(text))
                else:
                    await dialog.accept()
            else:
                await dialog.dismiss()
            outcome = "accepted" if action == "accept" else "dismissed"
        except PwError as exc:
            outcome = f"error: {_clean(exc)}"
        if policy.get("once"):
            self.dialog_policy = {
                "action": self.s.dialog_default,
                "prompt_text": None,
                "once": False,
            }
        record = {
            "ts": time.time(),
            "page": handle.id,
            "type": kind,
            "message": message[:1000],
            "default_value": getattr(dialog, "default_value", "") or None,
            "handled": outcome,
            "prompt_text": text if action == "accept" and kind == "prompt" else None,
        }
        self.dialogs.append(record)
        handle.console.append(
            {
                "ts": record["ts"],
                "page": handle.id,
                "type": "dialog",
                "text": f"{outcome} {kind}: {message[:300]}",
            }
        )

    async def _on_download(self, handle: PageHandle, download: Any) -> None:
        """Save a file the page downloaded, the way a person's browser would.

        ``save_as`` works for a local browser. A cloud browser writes the file
        to its own disk and ``save_as`` hands back an empty file, so the bytes
        are then fetched another way (see ``_fetch_download_bytes``).
        """
        self._download_seq += 1
        download_id = f"d{self._download_seq}"
        suggested = getattr(download, "suggested_filename", "") or "download"
        target = self.dir / "downloads" / f"{download_id}-{_safe_filename(suggested)}"
        entry: dict[str, Any] = {
            "id": download_id,
            "page": handle.id,
            "url": download.url,
            "filename": suggested,
            "status": "in_progress",
            "path": None,
            "bytes": None,
            "started_at": time.time(),
        }
        self.downloads.append(entry)
        del self.downloads[:-100]
        target.parent.mkdir(parents=True, exist_ok=True)

        problem = ""
        try:
            async with asyncio.timeout(300):
                await download.save_as(target)
            size = target.stat().st_size
            if size > 0:
                entry.update(status="saved", path=str(target), bytes=size)
                return
            # Measured against Browser Use Cloud: save_as "succeeds" but the
            # file is empty, because the browser wrote it to the cloud
            # machine's disk. Treat that as a miss and fetch the bytes below.
            problem = "the browser saved it on its own (remote) disk"
        except Exception as exc:  # noqa: BLE001 - fall back below
            problem = _clean(exc)

        url = download.url or ""
        try:
            data, via = await self._fetch_download_bytes(handle, url)
        except _DownloadUnavailable as exc:
            entry.update(status="failed", error=f"{problem}; {exc}"[:300])
            return
        except ScopeViolation as exc:
            entry.update(status="blocked", error=exc.message)
            return
        await asyncio.to_thread(target.write_bytes, data)
        entry.update(status="saved", path=str(target), bytes=len(data), via=via)

    async def _fetch_download_bytes(self, handle: PageHandle, url: str) -> tuple[bytes, str]:
        """Get a download's bytes when the browser kept the file to itself.

        * ``data:``  -- decoded right here;
        * ``blob:``  -- read inside the page that created it (typical for
          client-side "Export CSV/PDF" buttons), if it has not been revoked;
        * ``http(s):`` -- re-requested with this browser's cookies. A one-time
          link, or a download produced by a POST, cannot be repeated this way.
        """
        if url.startswith("data:"):
            header, _, payload = url.partition(",")
            if header.endswith(";base64"):
                return base64.b64decode(payload), "data_url"
            return urllib.parse.unquote_to_bytes(payload), "data_url"

        if url.startswith("blob:"):
            try:
                async with asyncio.timeout(60):
                    result = await handle.page.evaluate(_READ_URL_JS, [url, 50 * 1024 * 1024, False])
            except (PwError, TimeoutError) as exc:
                raise _DownloadUnavailable(f"could not read the blob: {_clean(exc)}") from exc
            if not isinstance(result, dict) or result.get("error"):
                raise _DownloadUnavailable(
                    "the page already revoked the blob URL"
                    if not isinstance(result, dict)
                    else str(result["error"])[:200]
                )
            return base64.b64decode(result["b64"]), "page_blob"

        if url.startswith(("http://", "https://")) and self.context is not None:
            self.scope.enforce(url)
            # First from inside the cloud browser, so the site sees the same IP,
            # cookies and TLS fingerprint as for the click that started it.
            # (context.request would send it from the machine running this
            # gateway -- the user's own IP.) Cross-origin files may be refused
            # by CORS; only then fall back.
            with contextlib.suppress(PwError, TimeoutError):
                async with asyncio.timeout(120):
                    result = await handle.page.evaluate(
                        _READ_URL_JS, [url, 50 * 1024 * 1024, True]
                    )
                if isinstance(result, dict) and result.get("b64") is not None:
                    return base64.b64decode(result["b64"]), "fetched_in_cloud_browser"
            try:
                response = await self.context.request.get(url, timeout=120_000)
            except Exception as exc:  # noqa: BLE001
                raise _DownloadUnavailable(f"re-fetch failed: {_clean(exc)}") from exc
            if not response.ok:
                raise _DownloadUnavailable(f"re-fetch returned HTTP {response.status}")
            return await response.body(), "refetched_from_gateway_host_ip"

        raise _DownloadUnavailable(f"no way to retrieve a {url.split(':', 1)[0]}: download")

    def take_new_dialogs(self) -> list[dict[str, Any]]:
        """Dialogs answered since the last call, so a tool can say what popped up."""
        fresh = [d for d in self.dialogs if not d.get("_reported")]
        for record in fresh:
            record["_reported"] = True
        return [{k: v for k, v in d.items() if not k.startswith("_")} for d in fresh]

    def take_new_pages(self) -> list[str]:
        """Tabs the page opened by itself since the last call, then forget them."""
        opened = [p for p in self._new_pages if p in self.pages and not self.pages[p].closed]
        self._new_pages.clear()
        return opened

    def _on_frame_navigated(self, handle: PageHandle, frame: Any) -> None:
        # Navigation resets Debugger.setSkipAllPauses, which would let a page's
        # anti-debug `debugger;` freeze the renderer again.
        if handle.debugger is None or frame != handle.page.main_frame:
            return
        self._spawn(handle.debugger.reassert_pause_suppression())

    def _on_binding(self, handle: PageHandle, params: dict[str, Any]) -> None:
        if params.get("name") != self.hooks.binding:
            return
        self.hooks.ingest(params.get("payload") or "", page_id=handle.id)

    # ----- page access -----------------------------------------------------

    def active(self) -> PageHandle:
        if self._active_page_id and self._active_page_id in self.pages:
            handle = self.pages[self._active_page_id]
            if not handle.closed:
                return handle
        for handle in self.pages.values():
            if not handle.closed:
                self._active_page_id = handle.id
                return handle
        raise BrowserConnectionError(
            "this browser has no open page left",
            hint="Call page_tabs(action='new') to open one.",
        )

    def handle(self, page_id: str | None) -> PageHandle:
        if page_id is None:
            return self.active()
        handle = self.pages.get(page_id)
        if handle is None or handle.closed:
            raise NotFound(
                f"no open page {page_id!r}",
                hint="Call page_tabs(action='list') to see open tabs.",
            )
        return handle

    async def select(self, page_id: str) -> PageHandle:
        """Switch tabs the way a person does: the tab comes to the front.

        Only moving our pointer left the tab in the background, where Chromium
        stops painting it (screenshots hang until timeout) and throttles its
        timers and animations.
        """
        handle = self.handle(page_id)
        self._active_page_id = handle.id
        await self.bring_to_front(handle)
        return handle

    async def bring_to_front(self, handle: PageHandle) -> None:
        if handle.debugger is not None and handle.debugger.is_paused:
            return
        with contextlib.suppress(PwError, TimeoutError):
            async with asyncio.timeout(5):
                await handle.page.bring_to_front()

    def ensure_runnable(self, handle: PageHandle) -> None:
        """Refuse page operations while the debugger has the renderer stopped."""
        if handle.debugger is not None and handle.debugger.is_paused:
            raise DebuggerPaused(
                f"page {handle.id} is paused at a breakpoint and cannot be driven"
            )

    async def new_page(self) -> PageHandle:
        if self.context is None:
            raise BrowserConnectionError("not connected")
        page = await self.context.new_page()
        handle = await self._adopt(page)
        self._active_page_id = handle.id
        # The agent asked for this tab, so it is not news to report later.
        with contextlib.suppress(ValueError):
            self._new_pages.remove(handle.id)
        return handle

    async def close_page(self, page_id: str) -> None:
        handle = self.handle(page_id)
        with contextlib.suppress(PwError):
            await handle.page.close()
        handle.closed = True
        self._on_page_closed(handle)

    async def tabs(self) -> list[dict[str, Any]]:
        out = []
        for handle in self.pages.values():
            if handle.closed:
                continue
            url, title = handle.page.url, ""
            if handle.debugger is None or not handle.debugger.is_paused:
                with contextlib.suppress(PwError, TimeoutError):
                    async with asyncio.timeout(5):
                        title = await handle.page.title()
            out.append(
                {
                    "page": handle.id,
                    "active": handle.id == self._active_page_id,
                    "url": url,
                    "title": title,
                    "paused": bool(handle.debugger and handle.debugger.is_paused),
                }
            )
        return out

    # ----- element refs ----------------------------------------------------

    async def resolve_ref(self, handle: PageHandle, ref: str) -> Any:
        """Find a ref in any frame of the page.

        Searching every frame costs one count() per frame and removes the need to
        track which frame a ref came from, which would go stale anyway.
        """
        selector = ref_selector(ref, self.ref_attr)
        frames = [
            handle.page.main_frame,
            *[f for f in handle.page.frames if f != handle.page.main_frame],
        ]
        for frame in frames:
            with contextlib.suppress(PwError, PwTimeout):
                locator = frame.locator(selector).first
                if await locator.count() > 0:
                    return locator
        raise RefNotFound(f"element ref {ref!r} is not present on page {handle.id}")

    async def locator_for(
        self, handle: PageHandle, *, ref: str | None, selector: str | None, text: str | None
    ) -> Any:
        if ref:
            return await self.resolve_ref(handle, ref)
        if selector:
            return handle.page.locator(selector).first
        if text:
            return handle.page.get_by_text(text, exact=False).first
        raise InvalidArgument(
            "pass ref, selector or text",
            hint="Prefer ref from page_snapshot: it survives re-renders better than a CSS path.",
        )

    # ----- reads -----------------------------------------------------------

    def _snapshot_opts(self, **extra: Any) -> dict[str, Any]:
        return {"refAttr": self.ref_attr, "seqKey": self.ref_seq_key, **extra}

    async def snapshot(
        self, handle: PageHandle, *, include_hidden: bool = False, max_nodes: int = 1500
    ) -> dict[str, Any]:
        self.ensure_runnable(handle)
        try:
            async with asyncio.timeout(30):
                result = await handle.page.evaluate(
                    SNAPSHOT_JS,
                    self._snapshot_opts(
                        maxNodes=max_nodes, includeHidden=include_hidden, maxTextLen=160
                    ),
                )
        except (PwError, TimeoutError) as exc:
            raise ToolTimeout(f"snapshot failed: {_clean(exc)}") from exc

        frames: list[dict[str, Any]] = []
        for frame in handle.page.frames:
            if frame == handle.page.main_frame:
                continue
            with contextlib.suppress(PwError, TimeoutError):
                async with asyncio.timeout(10):
                    sub = await frame.evaluate(
                        SNAPSHOT_JS,
                        self._snapshot_opts(
                            maxNodes=300, includeHidden=include_hidden, maxTextLen=120
                        ),
                    )
                if sub and sub.get("lines"):
                    frames.append({"url": frame.url, "name": frame.name, "lines": sub["lines"]})
        result["frames"] = frames
        result["page"] = handle.id
        return result

    async def element_detail(self, handle: PageHandle, ref: str) -> dict[str, Any]:
        self.ensure_runnable(handle)
        detail = None
        for frame in handle.page.frames:
            with contextlib.suppress(PwError, TimeoutError):
                detail = await frame.evaluate(
                    ELEMENT_DETAIL_JS, {"ref": ref, "refAttr": self.ref_attr}
                )
            if detail:
                break
        if not detail:
            raise RefNotFound(f"element ref {ref!r} not found on page {handle.id}")

        if handle.debugger is not None and handle.cdp is not None:
            with contextlib.suppress(Exception):
                evaluated = await handle.cdp.send(
                    "Runtime.evaluate",
                    {
                        # json.dumps, not repr: a Python repr is not always a
                        # valid JS string literal.
                        "expression": (
                            f"document.querySelector({json.dumps(ref_selector(ref, self.ref_attr))})"
                        ),
                        "returnByValue": False,
                    },
                )
                object_id = (evaluated.get("result") or {}).get("objectId")
                if object_id:
                    detail["event_listeners"] = await handle.debugger.event_listeners(object_id)
        return detail

    async def page_text(self, handle: PageHandle, selector: str | None = None) -> dict[str, Any]:
        self.ensure_runnable(handle)
        try:
            async with asyncio.timeout(30):
                return await handle.page.evaluate(PAGE_TEXT_JS, {"selector": selector})
        except (PwError, TimeoutError) as exc:
            raise ToolTimeout(f"text extraction failed: {_clean(exc)}") from exc

    async def inventory(self, handle: PageHandle) -> dict[str, Any]:
        self.ensure_runnable(handle)
        async with asyncio.timeout(30):
            return await handle.page.evaluate(PAGE_INVENTORY_JS)

    async def evaluate(
        self, handle: PageHandle, expression: str, *, timeout_ms: int = 15_000
    ) -> dict[str, Any]:
        """Run an expression in the page.

        Falls back to wrapping multi-statement bodies in an arrow function, and to
        a stringified result when the value is not JSON-serialisable, so the agent
        is not forced to guess the right syntax.
        """
        self.ensure_runnable(handle)
        attempts = [expression]
        stripped = expression.strip()
        if not stripped.startswith(("(", "function", "async", "=>")) and (
            ";" in stripped or "\n" in stripped or stripped.startswith(("var ", "let ", "const "))
        ):
            attempts = [f"() => {{ {expression} }}", expression]

        last: Exception | None = None
        for attempt in attempts:
            try:
                async with asyncio.timeout(timeout_ms / 1000 + 2):
                    value = await handle.page.evaluate(attempt)
                return {"ok": True, "value": value, "type": type(value).__name__}
            except PwError as exc:
                last = exc
                message = str(exc)
                if "Unexpected token" in message or "SyntaxError" in message:
                    continue
                if "circular" in message.lower() or "could not be cloned" in message:
                    with contextlib.suppress(PwError, TimeoutError):
                        async with asyncio.timeout(timeout_ms / 1000 + 2):
                            text = await handle.page.evaluate(
                                "(e) => { try { return String(eval(e)); } "
                                "catch (err) { return 'ERR: ' + String(err); } }",
                                expression,
                            )
                        return {"ok": True, "value": text, "type": "str", "coerced": True}
                break
            except TimeoutError as exc:
                last = exc
                break
        return {
            "ok": False,
            "error": "js_error",
            "message": _clean(last) if last else "evaluation failed",
        }

    async def screenshot(
        self,
        handle: PageHandle,
        *,
        full_page: bool = False,
        ref: str | None = None,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        self.ensure_runnable(handle)
        if sum(1 for h in self.pages.values() if not h.closed) > 1:
            # A background tab is not painted, so capturing it would hang.
            await self.bring_to_front(handle)
        self._artifact_seq += 1
        ext = "jpg" if self.s.screenshot_format == "jpeg" else "png"
        path = self.dir / f"shot{self._artifact_seq}.{ext}"
        # scale="css": one image pixel is one CSS pixel, so a point read off the
        # screenshot is exactly what page_mouse(x, y) clicks, on HiDPI too.
        options: dict[str, Any] = {"timeout": 30_000, "scale": "css"}
        if self.s.screenshot_format == "jpeg":
            options["type"] = "jpeg"
            options["quality"] = self.s.screenshot_quality
        try:
            if ref:
                locator = await self.resolve_ref(handle, ref)
                # Same type as the page path: this used to fall back to PNG while
                # the result still claimed JPEG.
                data = await locator.screenshot(**options)
            else:
                data = await handle.page.screenshot(full_page=full_page, **options)
        except (PwError, TimeoutError) as exc:
            raise ToolTimeout(f"screenshot failed: {_clean(exc)}") from exc

        await asyncio.to_thread(path.write_bytes, data)
        limit = max_bytes or self.s.max_screenshot_bytes
        viewport = handle.page.viewport_size
        if viewport is None:
            with contextlib.suppress(PwError):
                viewport = await handle.page.evaluate(
                    "({width: innerWidth, height: innerHeight})"
                )
        return {
            "path": str(path),
            "bytes": len(data),
            "format": self.s.screenshot_format,
            "too_large_to_inline": len(data) > limit,
            "data": data,
            "url": handle.page.url,
            "viewport": viewport,
            "coordinates": (
                "CSS pixels; page_mouse(x, y) uses the same frame"
                if not (ref or full_page)
                else "element/full-page capture: offsets differ from viewport coordinates"
            ),
        }

    async def emulate(
        self,
        handle: PageHandle,
        *,
        cpu_throttle: float | None = None,
        network: str | None = None,
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
    ) -> dict[str, Any]:
        """Change the conditions the page runs under.

        Useful for security work because sites behave differently by geography,
        locale and connection quality -- different endpoints, different consent
        flows, different anti-fraud thresholds. Applies to this tab only.
        """
        self.ensure_runnable(handle)
        cdp = await self._ensure_cdp(handle)
        if cdp is None:
            raise BrowserConnectionError("could not open a CDP session for this page")

        applied: dict[str, Any] = {}
        failed: dict[str, str] = {}

        async def attempt(name: str, method: str, params: dict[str, Any], note: Any) -> None:
            try:
                await cdp.send(method, params)
                applied[name] = note
            except Exception as exc:  # noqa: BLE001 - report, never abort the batch
                failed[name] = str(exc)[:150]

        if cpu_throttle is not None:
            await attempt(
                "cpu_throttle",
                "Emulation.setCPUThrottlingRate",
                {"rate": max(1.0, float(cpu_throttle))},
                f"{cpu_throttle}x slowdown",
            )

        if network is not None:
            preset = NETWORK_PRESETS.get(network)
            if preset is None and network != "none":
                raise InvalidArgument(
                    f"unknown network preset {network!r}",
                    hint=f"One of: {', '.join(NETWORK_PRESETS)}, or 'none' to clear.",
                )
            with contextlib.suppress(Exception):
                await cdp.send("Network.enable")
            conditions = preset or {
                "offline": False,
                "latency": 0,
                "downloadThroughput": -1,
                "uploadThroughput": -1,
            }
            await attempt(
                "network", "Network.emulateNetworkConditions", conditions, network
            )

        if grant_permissions is not None:
            # What a person does by clicking "Allow" on a permission prompt.
            # An empty list revokes everything granted so far.
            try:
                if self.context is None:
                    raise BrowserConnectionError("not connected")
                if grant_permissions:
                    parts = urlsplit(handle.page.url)
                    origin = (
                        f"{parts.scheme}://{parts.netloc}"
                        if parts.scheme in ("http", "https")
                        else None
                    )
                    await self.context.grant_permissions(grant_permissions, origin=origin)
                    applied["permissions"] = {"granted": grant_permissions, "origin": origin}
                else:
                    await self.context.clear_permissions()
                    applied["permissions"] = "cleared"
            except Exception as exc:  # noqa: BLE001 - report, never abort the batch
                failed["permissions"] = str(exc)[:150]

        if latitude is not None and longitude is not None:
            await attempt(
                "geolocation",
                "Emulation.setGeolocationOverride",
                {"latitude": latitude, "longitude": longitude, "accuracy": 50},
                f"{latitude},{longitude}",
            )

        if timezone:
            await attempt(
                "timezone", "Emulation.setTimezoneOverride", {"timezoneId": timezone}, timezone
            )
        if locale:
            await attempt("locale", "Emulation.setLocaleOverride", {"locale": locale}, locale)

        if user_agent or accept_language:
            params: dict[str, Any] = {"userAgent": user_agent or ""}
            if accept_language:
                params["acceptLanguage"] = accept_language
            await attempt(
                "user_agent",
                "Emulation.setUserAgentOverride",
                params,
                (user_agent or "")[:80] or f"accept-language={accept_language}",
            )

        if dark_mode is not None:
            scheme = "dark" if dark_mode else "light"
            ok = False
            try:
                await handle.page.emulate_media(color_scheme=scheme)
                ok = await handle.page.evaluate(
                    "(s) => matchMedia('(prefers-color-scheme: ' + s + ')').matches", scheme
                )
            except PwError as exc:
                failed["color_scheme"] = str(exc)[:150]
            if not ok:
                # Playwright's emulate_media does not always take on a remote
                # browser, so fall back to the raw CDP call with an explicit
                # media field, then confirm rather than assume.
                await attempt(
                    "color_scheme",
                    "Emulation.setEmulatedMedia",
                    {
                        "media": "screen",
                        "features": [{"name": "prefers-color-scheme", "value": scheme}],
                    },
                    scheme,
                )
                with contextlib.suppress(PwError):
                    ok = await handle.page.evaluate(
                        "(s) => matchMedia('(prefers-color-scheme: ' + s + ')').matches", scheme
                    )
                if not ok:
                    applied.pop("color_scheme", None)
                    failed["color_scheme"] = "the page still reports the other scheme"
            else:
                applied["color_scheme"] = scheme

        if viewport_width and viewport_height:
            with contextlib.suppress(PwError):
                await handle.page.set_viewport_size(
                    {"width": int(viewport_width), "height": int(viewport_height)}
                )
                applied["viewport"] = f"{viewport_width}x{viewport_height}"

        return {
            "page": handle.id,
            "applied": applied,
            "failed": failed or None,
            "note": (
                "Emulation is per tab and is not inherited by tabs opened later. "
                "Reload the page if the site reads these values only at startup."
            ),
        }

    async def cookies(self, urls: list[str] | None = None) -> list[dict[str, Any]]:
        if self.context is None:
            raise BrowserConnectionError("not connected")
        return await self.context.cookies(urls) if urls else await self.context.cookies()

    # ----- hooks -----------------------------------------------------------

    async def sync_hook_init_script(self) -> bool:
        """Make the init script that runs on future documents match the hook set.

        Each js_hook call used to add one more init script and none were ever
        removed, so a removed or cleared hook kept being installed on every
        later page load. Returns False when this Playwright version cannot
        remove an init script (``add_init_script`` returning a Disposable is
        recent), in which case removed hooks persist until the lease ends.
        """
        if self.context is None:
            raise BrowserConnectionError("not connected")
        previous = self._hook_init_script
        self._hook_init_script = None
        if self.hooks.specs:
            # Add the new one before dropping the old, so no document loads
            # in between without the runtime.
            self._hook_init_script = await self.context.add_init_script(
                self.hooks.install_script()
            )
            self._init_script_installed = True
        if previous is None:
            return True
        dispose = getattr(previous, "dispose", None)
        if dispose is None:
            return False
        with contextlib.suppress(PwError):
            await dispose()
        return True

    async def apply_hooks(self) -> dict[str, Any]:
        """Install the current hook set into the context and all open pages."""
        if self.context is None:
            raise BrowserConnectionError("not connected")
        upgrade = await self.ensure_debug("hooks")
        # Future documents: the runtime must be present before page scripts run.
        await self.sync_hook_init_script()
        payload = self.hooks.install_payload()

        # break_on_match works by injecting `debugger;`, which does nothing while
        # pauses are suppressed. Turn pausing on only when a hook actually wants it.
        wants_break = any(spec.get("break_on_match") for spec in self.hooks.specs.values())
        for handle in self.pages.values():
            if handle.debugger is None:
                continue
            handle.debugger.expect_debugger_statements = wants_break
            if wants_break:
                with contextlib.suppress(Exception):
                    await handle.debugger.set_pauses_enabled(True)

        applied: dict[str, Any] = {}
        for handle in self.pages.values():
            if handle.closed or (handle.debugger and handle.debugger.is_paused):
                continue
            with contextlib.suppress(PwError, TimeoutError):
                await handle.page.evaluate(RUNTIME_JS, payload)
                applied[handle.id] = await handle.page.evaluate(STATUS_JS, self.hooks.ns)
        return {"pages": applied, "debug": upgrade}

    async def hook_status(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for handle in self.pages.values():
            if handle.closed or (handle.debugger and handle.debugger.is_paused):
                continue
            with contextlib.suppress(PwError, TimeoutError):
                out[handle.id] = await handle.page.evaluate(STATUS_JS, self.hooks.ns)
        return out

    async def drain_hooks(self) -> int:
        """Pull records the CDP binding could not deliver (the debug='hooks' path)."""
        total = 0
        for handle in self.pages.values():
            if handle.closed or (handle.debugger and handle.debugger.is_paused):
                continue
            with contextlib.suppress(PwError, TimeoutError):
                batch = await handle.page.evaluate(DRAIN_JS, self.hooks.ns)
                if batch:
                    total += self.hooks.ingest(batch, page_id=handle.id)
        return total

    # ----- rebuild bundle --------------------------------------------------

    async def export_rebuild_bundle(
        self,
        handle: PageHandle,
        *,
        script_urls: list[str] | None = None,
        hook_labels: list[str] | None = None,
        include_cookies: bool = False,
        include_storage: bool = True,
        notes: str | None = None,
        max_script_bytes: int = 4 * 1024 * 1024,
        max_vectors: int = 200,
    ) -> dict[str, Any]:
        """Package everything needed to re-run an algorithm outside the browser.

        Understanding a signing routine in the debugger is not the end of the
        job -- reproducing it is. That needs three things that are easy to lose:
        the code, the environment values it reads, and known input/output pairs
        to check the reproduction against. This writes all three plus a Node
        scaffold wired together.
        """
        self.ensure_runnable(handle)
        self._artifact_seq += 1
        root = self.dir / f"rebuild-{self._artifact_seq}"
        (root / "scripts").mkdir(parents=True, exist_ok=True)

        env = await handle.page.evaluate(ENVIRONMENT_JS)
        if not include_storage:
            env.pop("local_storage", None)
            env.pop("session_storage", None)
        if include_cookies:
            env["cookies"] = [
                {k: c.get(k) for k in ("name", "value", "domain", "path", "httpOnly", "secure")}
                for c in await self.cookies()
            ]

        saved: list[dict[str, Any]] = []
        if script_urls:
            debugger = await self.debugger_for(handle.id)
            # Snapshot first: scriptParsed events keep mutating this dict
            # while the awaits below are in flight.
            for index, info in enumerate(list(debugger.scripts.values())):
                if not any(u.lower() in (info.url or "").lower() for u in script_urls):
                    continue
                try:
                    result = await debugger.cdp.send(
                        "Debugger.getScriptSource", {"scriptId": info.script_id}
                    )
                except Exception:  # noqa: BLE001 - scripts can be collected
                    continue
                source = result.get("scriptSource") or ""
                truncated = len(source) > max_script_bytes
                name = f"{index:02d}-{_safe_name(info.url)}.js"
                await asyncio.to_thread(
                    (root / "scripts" / name).write_text, source[:max_script_bytes], "utf-8"
                )
                saved.append(
                    {
                        "file": f"scripts/{name}",
                        "url": info.url or "<inline/eval>",
                        "bytes": min(len(source), max_script_bytes),
                        "truncated": truncated,
                    }
                )

        records = self.hooks.query(limit=max_vectors, newest_first=False)
        if hook_labels:
            wanted = {label.lower() for label in hook_labels}
            records = [
                r for r in records if any(w in r.label.lower() for w in wanted)
            ]
        vectors = [
            {
                "seq": r.seq,
                "label": r.label,
                "phase": r.phase,
                "args": r.args,
                "result": r.result,
                "stack": r.stack[:4],
            }
            for r in records
        ]

        manifest = {
            "captured_at": time.time(),
            "page_url": handle.page.url,
            "lease": self.lease_id,
            "notes": notes,
            "scripts": saved,
            "vector_count": len(vectors),
        }
        await asyncio.to_thread(
            (root / "env.json").write_text,
            json.dumps(env, indent=2, ensure_ascii=False, default=str),
            "utf-8",
        )
        await asyncio.to_thread(
            (root / "vectors.json").write_text,
            json.dumps(vectors, indent=2, ensure_ascii=False, default=str),
            "utf-8",
        )
        await asyncio.to_thread(
            (root / "manifest.json").write_text,
            json.dumps(manifest, indent=2, ensure_ascii=False, default=str),
            "utf-8",
        )
        await asyncio.to_thread(
            (root / "rebuild.js").write_text, _REBUILD_SCAFFOLD, "utf-8"
        )
        await asyncio.to_thread(
            (root / "README.md").write_text,
            _bundle_readme(handle.page.url, saved, len(vectors), notes),
            "utf-8",
        )

        total = sum(f.stat().st_size for f in root.rglob("*") if f.is_file())
        return {
            "path": str(root),
            "files": {
                "rebuild.js": "Node scaffold: stubs the captured environment, "
                "runs the vectors as assertions",
                "env.json": "navigator/screen/timezone/storage the page could read",
                "vectors.json": f"{len(vectors)} recorded input/output pair(s) from hooks",
                "manifest.json": "what was captured and from where",
                "scripts/": f"{len(saved)} saved script source(s)",
            },
            "scripts_saved": saved,
            "vector_count": len(vectors),
            "total_bytes": total,
            "next": (
                "Open rebuild.js, paste the extracted algorithm into the marked "
                "section, then run `node rebuild.js` to check it against the captured "
                "vectors. If a vector set is missing, hook the function first "
                "(js_hook) and drive the page again."
            ),
            "warning": (
                "env.json and vectors.json may contain tokens, cookies or personal "
                "data captured from the page. Treat the bundle as sensitive."
            ),
        }

    # ----- debugger access -------------------------------------------------

    async def debugger_for(self, page_id: str | None = None) -> JsDebugger:
        if not self.s.enable_debugger_tools:
            raise InvalidArgument(
                "debugger tools are disabled on this server",
                hint="Set CBMCP_ENABLE_DEBUGGER_TOOLS=true and restart.",
            )
        await self.ensure_debug("full")
        if page_id:
            handle = self.handle(page_id)
            if handle.debugger is None:
                raise NotFound(f"no debugger attached to {page_id}")
            return handle.debugger
        # Prefer a page that is already paused: that is where the agent's
        # attention is, regardless of which tab is nominally active.
        for handle in self.pages.values():
            if handle.debugger is not None and handle.debugger.is_paused:
                return handle.debugger
        handle = self.active()
        if handle.debugger is None:
            raise NotFound(
                f"no debugger attached to {handle.id}",
                hint="The CDP session may have failed; try page_tabs(action='new').",
            )
        return handle.debugger

    def paused_pages(self) -> list[str]:
        return [
            h.id for h in self.pages.values() if h.debugger is not None and h.debugger.is_paused
        ]

    # ----- diagnostics / teardown -----------------------------------------

    async def alive(self) -> bool:
        """Is this browser still usable?

        A single failed probe does not mean dead. ``page.evaluate`` legitimately
        blocks or throws while the page is mid-navigation, and declaring the
        session dead there would tear a browser away from an agent in the middle
        of loading a slow site. The authoritative signal is the CDP socket; an
        unresponsive-but-connected browser has to fail repeatedly to count.
        """
        if self.browser is None or not self.connected:
            return False
        if not self.browser.is_connected():
            self.liveness_failures = self.LIVENESS_STRIKES
            return False
        if self.paused_pages():
            self.liveness_failures = 0
            return True  # a paused renderer is working, not dead
        handle = next((h for h in self.pages.values() if not h.closed), None)
        if handle is None:
            self.liveness_failures = 0
            return True
        try:
            async with asyncio.timeout(10):
                await handle.page.evaluate("1")
            self.liveness_failures = 0
            return True
        except Exception:  # noqa: BLE001
            # The page did not answer. If the browser itself still does, the
            # page is busy (heavy script, long layout), not the connection dead.
            if await self._browser_responds():
                self.liveness_failures = 0
                return True
            self.liveness_failures += 1
            if self.liveness_failures < self.LIVENESS_STRIKES:
                log.debug(
                    "lease %s liveness probe failed (%d/%d); the page may just be busy",
                    self.lease_id,
                    self.liveness_failures,
                    self.LIVENESS_STRIKES,
                )
                return True
            return False

    def stats(self) -> dict[str, Any]:
        return {
            "pages": len([h for h in self.pages.values() if not h.closed]),
            "active_page": self._active_page_id,
            "reconnects": self.reconnects,
            "paused_pages": self.paused_pages(),
            "debug_mode": self.debug_mode,
            "network": self.netlog.stats(),
            "hooks": self.hooks.stats(),
            "scope": self.scope.describe(),
            "console_entries": sum(len(h.console) for h in self.pages.values()),
            "js_errors": sum(len(h.errors) for h in self.pages.values()),
        }

    async def aclose(self, *, shutdown_browser: bool = False) -> None:
        """Drop the local connection. Stopping the cloud session is the pool's job.

        Every step is bounded. Teardown must not be able to hang, because the
        concurrency slot and the platform stop both wait behind it -- and a
        renderer paused at a breakpoint will hang a plain ``browser.close()``,
        which is exactly why the debugger is resumed first.

        ``shutdown_browser`` asks the browser process itself to exit cleanly
        (CDP ``Browser.close``), so it flushes cookies to a kept profile before
        the provider stops it. Only for providers that want it (local Chrome).
        """
        for task in list(self._bg):
            task.cancel()
        self._bg.clear()
        for handle in self.pages.values():
            if handle.debugger is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(handle.debugger.disable(), timeout=10)
            if handle.cdp is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(handle.cdp.detach(), timeout=5)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.netlog.aclose(), timeout=10)
        if shutdown_browser and self.browser is not None:
            with contextlib.suppress(Exception):
                cdp = self._browser_cdp or await self.browser.new_browser_cdp_session()
                await asyncio.wait_for(cdp.send("Browser.close"), timeout=10)
            self._browser_cdp = None
        if self.browser is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.browser.close(), timeout=15)
        self.connected = False
        self.browser = None
        self.context = None


ENVIRONMENT_JS = r"""
() => {
  // Takes a getter: merely *reading* window.localStorage throws a
  // SecurityError on opaque origins (data: URLs, sandboxed frames).
  const take = (getStore) => {
    const out = {};
    try {
      const store = getStore();
      for (let i = 0; i < Math.min(store.length, 100); i++) {
        const k = store.key(i);
        out[k] = String(store.getItem(k) || '').slice(0, 2000);
      }
    } catch (e) { return {}; }
    return out;
  };
  const nav = {};
  for (const k of ['userAgent','platform','language','languages','vendor','product',
                   'hardwareConcurrency','deviceMemory','maxTouchPoints','webdriver',
                   'doNotTrack','cookieEnabled','appVersion','appName','oscpu']) {
    try { nav[k] = navigator[k]; } catch (e) { /* skip */ }
  }
  let tz = null, tzOffset = null;
  try {
    tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    tzOffset = new Date().getTimezoneOffset();
  } catch (e) { /* skip */ }
  return {
    url: location.href,
    origin: location.origin,
    referrer: document.referrer || null,
    navigator: nav,
    screen: {
      width: screen.width, height: screen.height,
      availWidth: screen.availWidth, availHeight: screen.availHeight,
      colorDepth: screen.colorDepth, pixelDepth: screen.pixelDepth,
      devicePixelRatio: window.devicePixelRatio,
    },
    window: { innerWidth: window.innerWidth, innerHeight: window.innerHeight },
    timezone: tz,
    timezone_offset_minutes: tzOffset,
    local_storage: take(() => window.localStorage),
    session_storage: take(() => window.sessionStorage),
  };
}
"""

_REBUILD_SCAFFOLD = '''// Rebuild scaffold -- run with:  node rebuild.js
//
// Stubs the browser environment captured from the live page, then checks a
// reimplementation of the algorithm against the recorded input/output pairs.
// If a vector fails, the reimplementation reads something the stub does not
// provide yet -- add it to env.json and re-run.

const fs = require('fs');
const path = require('path');

const env = JSON.parse(fs.readFileSync(path.join(__dirname, 'env.json'), 'utf8'));
const vectors = JSON.parse(fs.readFileSync(path.join(__dirname, 'vectors.json'), 'utf8'));

// ---- environment stubs ----------------------------------------------------
const storage = (data) => ({
  _d: { ...(data || {}) },
  getItem(k) { return this._d[k] === undefined ? null : this._d[k]; },
  setItem(k, v) { this._d[k] = String(v); },
  removeItem(k) { delete this._d[k]; },
  key(i) { return Object.keys(this._d)[i] ?? null; },
  get length() { return Object.keys(this._d).length; },
});

// Node 21+ ships its own getter-only `navigator` (userAgent "Node.js/22"...),
// and plain assignment to it is silently ignored. defineProperty replaces it,
// so the reimplementation really sees the captured browser values.
const stub = (name, value) => Object.defineProperty(globalThis, name, {
  value, writable: true, configurable: true, enumerable: true,
});
stub('navigator', { ...env.navigator });
stub('screen', { ...env.screen });
stub('location', new URL(env.url));
stub('document', {
  referrer: env.referrer || '',
  cookie: (env.cookies || []).map((c) => `${c.name}=${c.value}`).join('; '),
  createElement: () => ({ style: {}, setAttribute() {}, appendChild() {} }),
  querySelector: () => null,
  querySelectorAll: () => [],
});
stub('localStorage', storage(env.local_storage));
stub('sessionStorage', storage(env.session_storage));
stub('window', globalThis);
stub('self', globalThis);
stub('top', globalThis);
stub('btoa', (s) => Buffer.from(s, 'binary').toString('base64'));
stub('atob', (s) => Buffer.from(s, 'base64').toString('binary'));

// ---------------------------------------------------------------------------
// PASTE THE EXTRACTED ALGORITHM HERE.
// Take it from scripts/ (see manifest.json for where each file came from),
// then expose the entry point as `target`.
//
// Example:
//   function sign(payload) { /* ... pasted code ... */ }
//   const target = sign;
// ---------------------------------------------------------------------------
const target = null;

if (typeof target !== 'function') {
  console.log('Paste the algorithm above and set `target` to its entry point.');
  console.log(`${vectors.length} vector(s) are waiting in vectors.json:`);
  for (const v of vectors.slice(0, 5)) {
    console.log(`  ${v.label} [${v.phase}] args=${JSON.stringify(v.args).slice(0, 160)}`);
  }
  process.exit(0);
}

// ---- check against the captured vectors -----------------------------------
// Hooks record a 'call' (arguments) and a 'return' (result) separately, so
// they are paired up by order within each label here.
let pass = 0, fail = 0, skipped = 0;
const byLabel = {};
for (const v of vectors) (byLabel[v.label] ||= []).push(v);

for (const [label, items] of Object.entries(byLabel)) {
  const calls = items.filter((v) => v.phase === 'call');
  const returns = items.filter((v) => v.phase === 'return');
  for (let i = 0; i < calls.length; i++) {
    const expected = returns[i];
    if (!expected) { skipped++; continue; }
    let actual;
    try {
      actual = target(...calls[i].args);
    } catch (err) {
      console.log(`FAIL ${label}#${i}: threw ${err.message}`);
      fail++;
      continue;
    }
    const a = JSON.stringify(actual);
    const b = JSON.stringify(expected.result);
    if (a === b) { pass++; }
    else { fail++; console.log(`FAIL ${label}#${i}\\n  expected ${b}\\n  got      ${a}`); }
  }
}
console.log(`\\n${pass} passed, ${fail} failed, ${skipped} unpaired`);
process.exit(fail ? 1 : 0);
'''


class _DownloadUnavailable(Exception):
    pass


_READ_URL_JS = r"""
async ([url, cap, withCredentials]) => {
  try {
    const resp = await fetch(url, withCredentials ? { credentials: 'include' } : {});
    if (!resp.ok) return { error: 'HTTP ' + resp.status };
    const blob = await resp.blob();
    if (blob.size > cap) return { error: 'file is ' + blob.size + ' bytes, over the cap' };
    const bytes = new Uint8Array(await blob.arrayBuffer());
    let bin = '';
    for (let i = 0; i < bytes.length; i += 0x8000) {
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    }
    return { b64: btoa(bin) };
  } catch (e) {
    return { error: 'blob unreadable (revoked by the page?): ' + String(e).slice(0, 150) };
  }
}
"""


def _safe_filename(name: str) -> str:
    base = name.replace("\\", "/").split("/")[-1].strip() or "download"
    cleaned = "".join(c if c.isalnum() or c in "-._ " else "_" for c in base)
    return cleaned.lstrip(".")[:120] or "download"


def _safe_name(url: str) -> str:
    base = (url or "inline").rstrip("/").split("/")[-1].split("?")[0] or "inline"
    cleaned = "".join(c if c.isalnum() or c in "-._" else "_" for c in base)
    return cleaned[:60] or "script"


def _bundle_readme(
    page_url: str, scripts: list[dict[str, Any]], vectors: int, notes: str | None
) -> str:
    lines = [
        "# Rebuild bundle",
        "",
        f"Captured from `{page_url}`.",
        "",
        "Everything needed to re-run the algorithm outside the browser:",
        "",
        "| file | what it is |",
        "|---|---|",
        "| `rebuild.js` | Node scaffold: stubs the environment, checks the vectors |",
        "| `env.json` | navigator / screen / timezone / storage the page could read |",
        f"| `vectors.json` | {vectors} recorded input/output pair(s) |",
        "| `manifest.json` | what was captured and from where |",
        f"| `scripts/` | {len(scripts)} saved script source(s) |",
        "",
        "## How to use it",
        "",
        "1. Find the algorithm in `scripts/` (`manifest.json` says which file is which).",
        "2. Paste it into the marked section of `rebuild.js` and set `target`.",
        "3. `node rebuild.js` — it runs every captured input through your copy and",
        "   compares against the recorded output.",
        "4. A failure usually means the code reads something the stub does not",
        "   provide. Add it to `env.json` and run again.",
        "",
        "## Caution",
        "",
        "`env.json` and `vectors.json` can contain tokens, cookies and personal data",
        "captured from the live page. Treat this directory as sensitive.",
    ]
    if notes:
        lines += ["", "## Notes", "", notes]
    if scripts:
        lines += ["", "## Scripts", ""]
        lines += [f"- `{s['file']}` — {s['url']}" for s in scripts]
    return "\n".join(lines) + "\n"


def _clean(exc: object) -> str:
    text = str(exc or "")
    text = text.split("\nCall log:")[0]
    return text.replace("\n", " ")[:300]


def encode_b64(data: bytes) -> str:
    return base64.b64encode(data).decode()
