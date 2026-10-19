"""Application container: owns every long-lived object and the startup order.

Startup order matters and is enforced here:

1. open the store, so the write-ahead intent log exists before anything can
   create a paid session;
2. start Playwright once for the whole process (one driver, many connections);
3. run startup recovery *before* serving tools, so a crash in the previous
   process cannot leave a browser billing while this one happily accepts work;
4. only then start the reaper loop.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from playwright.async_api import Playwright, async_playwright

from .budget import BudgetGuard
from .config import Settings, get_settings
from .log import get_logger, setup_logging
from .pool import BrowserPool
from .providers import build_provider
from .providers.base import Provider
from .reaper import Reaper
from .store import Store

log = get_logger("app")


class App:
    def __init__(self, settings: Settings | None = None) -> None:
        self.s = settings or get_settings()
        self.s.ensure_dirs()
        self.store = Store(self.s.db_path)
        self.provider: Provider | None = None
        self.budget: BudgetGuard | None = None
        self.pool: BrowserPool | None = None
        self.reaper: Reaper | None = None
        self._pw_ctx: Any = None
        self._pw: Playwright | None = None
        self.started = False
        self.startup_report: dict[str, Any] = {}

    async def start(self) -> None:
        if self.started:
            return
        setup_logging(self.s.log_level)
        log.info(
            "starting cbmcp instance=%s provider=%s max_concurrency=%d "
            "idle=%.0fs max_lease=%.0fs platform_timeout=%dmin debug=%s",
            self.s.instance_id,
            self.s.provider,
            self.s.max_concurrency,
            self.s.idle_timeout_seconds,
            self.s.max_lease_seconds,
            self.s.platform_timeout_minutes,
            self.s.debug_default,
        )
        await self.store.open()
        self.provider = build_provider(self.s)
        self.budget = BudgetGuard(self.s, self.store)

        self._pw_ctx = async_playwright()
        self._pw = await self._pw_ctx.start()

        self.pool = BrowserPool(
            settings=self.s,
            store=self.store,
            provider=self.provider,
            budget=self.budget,
            playwright=self._pw,
        )
        self.reaper = Reaper(
            settings=self.s,
            store=self.store,
            provider=self.provider,
            pool=self.pool,
            budget=self.budget,
        )

        # Before accepting any tool call: clean up after the previous process.
        try:
            self.startup_report = await self.reaper.startup_recovery()
            if self.startup_report.get("stale_leases"):
                log.warning("startup recovery: %s", self.startup_report)
        except Exception as exc:  # noqa: BLE001 - never block startup on this
            log.warning("startup recovery incomplete: %s", exc)
            self.startup_report = {"error": str(exc)[:200]}

        await self.reaper.start()
        self.started = True
        log.info("cbmcp ready")

    async def stop(self) -> None:
        if not self.started:
            return
        log.info("shutting down; releasing all leases")
        if self.pool is not None:
            # First, before anything slow: the MCP client kills this process
            # about two seconds after closing the window. See stop_all_now.
            with contextlib.suppress(Exception):
                stopped = await asyncio.wait_for(self.pool.stop_all_now(), timeout=10)
                log.info("sent stop for %d cloud session(s)", stopped)
        if self.reaper is not None:
            await self.reaper.stop()
        if self.pool is not None:
            with contextlib.suppress(Exception):
                released = await asyncio.wait_for(
                    self.pool.release_all(reason="gateway_shutdown"), timeout=90
                )
                log.info("released %d lease(s) on shutdown", released)
        # A clean exit says so at once, rather than leaving sibling gateways to
        # wait for this heartbeat to age out before they adopt anything left over.
        with contextlib.suppress(Exception):
            await self.store.remove_instance(self.s.instance_id)
        if self._pw_ctx is not None:
            with contextlib.suppress(Exception):
                await self._pw_ctx.__aexit__(None, None, None)
        if self.provider is not None:
            with contextlib.suppress(Exception):
                await self.provider.aclose()
        await self.store.close()
        self.started = False
        log.info("cbmcp stopped")

    # ----- convenience -----------------------------------------------------

    @property
    def P(self) -> BrowserPool:
        if self.pool is None:
            raise RuntimeError("app not started")
        return self.pool

    @property
    def B(self) -> BudgetGuard:
        if self.budget is None:
            raise RuntimeError("app not started")
        return self.budget

    @property
    def R(self) -> Reaper:
        if self.reaper is None:
            raise RuntimeError("app not started")
        return self.reaper

    @property
    def PR(self) -> Provider:
        if self.provider is None:
            raise RuntimeError("app not started")
        return self.provider


_app: App | None = None


def get_app() -> App:
    if _app is None:
        raise RuntimeError("cbmcp application is not initialised")
    return _app


def set_app(app: App | None) -> None:
    global _app
    _app = app
