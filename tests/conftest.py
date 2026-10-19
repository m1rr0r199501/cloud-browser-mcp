"""Test fixtures.

The pool tests use a fake provider and a stub browser session, so the
concurrency, queue, idempotency and reclaim logic is exercised without a cloud
account or a real Chromium. That is the logic most likely to cost money when it
is wrong, so it must be testable offline.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from cbmcp.budget import BudgetGuard
from cbmcp.config import Settings
from cbmcp.errors import ProviderConcurrencyLimit
from cbmcp.pool import BrowserPool
from cbmcp.providers.base import AccountInfo, ProviderSession, SessionSpec
from cbmcp.store import Store


class FakeProvider:
    name = "fake"
    supports_profiles = True
    supports_custom_proxy = True

    def __init__(
        self,
        *,
        concurrent_limit: int | None = None,
        create_delay: float = 0.0,
        fail_create: bool = False,
        stop_confirms: bool = True,
        stop_accepts: bool = True,
    ) -> None:
        self.sessions: dict[str, ProviderSession] = {}
        self.seq = 0
        self.concurrent_limit = concurrent_limit
        self.create_delay = create_delay
        self.fail_create = fail_create
        self.stop_confirms = stop_confirms
        self.stop_accepts = stop_accepts
        # Reject this many creates with "account at its concurrency limit".
        self.concurrency_rejections = 0
        # After an accepted stop, keep reporting "active" for this many polls
        # (Browser Use's status lags the stop by a moment).
        self.stop_lag_polls = 0
        self._lag: dict[str, int] = {}
        self.created: list[SessionSpec] = []
        self.stopped: list[str] = []

    async def create_session(self, spec: SessionSpec) -> ProviderSession:
        if self.create_delay:
            await asyncio.sleep(self.create_delay)
        if self.fail_create:
            raise RuntimeError("provider refused to create a session")
        if self.concurrency_rejections > 0:
            self.concurrency_rejections -= 1
            raise ProviderConcurrencyLimit("account at its concurrent-session limit")
        self.seq += 1
        sid = f"fake{self.seq}"
        now = time.time()
        session = ProviderSession(
            id=sid,
            cdp_url=f"ws://127.0.0.1:1/{sid}",
            status="active",
            started_at=now,
            timeout_at=now + spec.timeout_minutes * 60,
            metadata=dict(spec.metadata),
        )
        self.sessions[sid] = session
        self.created.append(spec)
        return session

    async def get_session(self, session_id: str) -> ProviderSession | None:
        session = self.sessions.get(session_id)
        if session is not None and self._lag.get(session_id):
            self._lag[session_id] -= 1
            if self._lag[session_id] == 0:
                session.status = "stopped"
                session.finished_at = time.time()
        return session

    async def list_sessions(
        self, *, active_only: bool = True, limit: int = 500
    ) -> list[ProviderSession]:
        items = list(self.sessions.values())
        if active_only:
            items = [s for s in items if s.status == "active"]
        return items[:limit]

    async def stop_session(self, session_id: str) -> bool:
        if not self.stop_accepts:
            return False  # e.g. the API was unreachable
        self.stopped.append(session_id)
        session = self.sessions.get(session_id)
        if session is None:
            return True
        if self.stop_confirms and self.stop_lag_polls:
            self._lag[session_id] = self.stop_lag_polls
        elif self.stop_confirms:
            session.status = "stopped"
            session.finished_at = time.time()
            session.cdp_url = None
        return True

    async def account_info(self) -> AccountInfo:
        return AccountInfo(
            project_id="test",
            concurrent_session_limit=self.concurrent_limit,
            active_session_count=len(await self.list_sessions()),
        )

    async def aclose(self) -> None:
        return None


class StubSession:
    """Stands in for BrowserSession: connects instantly, never touches a browser."""

    instances: list[StubSession] = []

    def __init__(self, **kwargs: Any) -> None:
        self.lease_id = kwargs["lease_id"]
        self.s = kwargs["settings"]
        self.scope = kwargs["scope"]
        self.dir = Path(kwargs["work_dir"])
        self.debug_mode = kwargs.get("debug_mode", "full")
        self.connected = False
        self.closed = False
        self._alive = True
        self.pages = {}
        self._active_page_id = "p1"
        StubSession.instances.append(self)

    async def connect(self, cdp_url: str) -> dict[str, Any]:
        self.connected = True
        return {"pages": ["p1"], "active_page": "p1", "title": "stub", "connect_ms": 1}

    async def alive(self) -> bool:
        return self._alive

    def stats(self) -> dict[str, Any]:
        return {"pages": 1, "network": {"entries": 0}, "hooks": {"records": 0}}

    async def aclose(self) -> None:
        self.closed = True
        self.connected = False


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    # _env_file=None keeps the developer's own .env out of the tests, so results
    # do not depend on whatever happens to be configured locally.
    return Settings(
        _env_file=None,
        provider="local",  # cosmetic: the pool gets FakeProvider injected directly
        browser_use_api_key="test",
        owner_tag="cbmcp-test",
        data_dir=tmp_path / "state",
        max_concurrency=2,
        acquire_block_seconds=1.0,
        queue_timeout_seconds=30.0,
        idle_timeout_seconds=30.0,
        max_lease_seconds=120.0,
        platform_timeout_minutes=5,
        release_grace_seconds=0.2,
        reaper_interval_seconds=1.0,
        reconcile_interval_seconds=10.0,
        reconcile_on_startup=False,
        budget_daily_usd=1.0,
        budget_monthly_usd=10.0,
        respect_platform_limit=True,
        network_mode="direct",
        scope_mode="off",
    )


@pytest.fixture
async def store(settings: Settings) -> Store:
    settings.ensure_dirs()
    s = Store(settings.db_path)
    await s.open()
    yield s
    await s.close()


@pytest.fixture
async def pool(settings: Settings, store: Store, monkeypatch) -> BrowserPool:
    StubSession.instances.clear()
    monkeypatch.setattr("cbmcp.pool.BrowserSession", StubSession)
    provider = FakeProvider(concurrent_limit=None)
    budget = BudgetGuard(settings, store)
    p = BrowserPool(
        settings=settings,
        store=store,
        provider=provider,
        budget=budget,
        playwright=None,  # StubSession never uses it
    )
    return p
