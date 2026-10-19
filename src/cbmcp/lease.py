"""The runtime lease object.

A lease is a **capability**: the token returned by ``browser_acquire`` is the
only thing that authorises driving that browser. It is 32 bytes of entropy, only
its SHA-256 is persisted, and it is never written to a log.

This matters because of how Claude Code actually works: subagents share the
parent session's MCP connection, so the server cannot tell from the transport
which subagent is calling. Possession of the token therefore *is* the identity.
That buys unguessability and clean separation between concurrent tasks; it does
not defend against an agent that has been fed someone else's token, which is
why tokens are never echoed in tool output or audit records.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .errors import LeaseExpired, LeaseNotActive, ToolTimeout
from .scope import ScopeRule
from .store import ST_ACTIVE, ST_CREATING

if TYPE_CHECKING:
    from .session import BrowserSession


@dataclass
class Lease:
    lease_id: str
    token: str
    provider_name: str
    scope: ScopeRule
    network_kind: str
    created_at: float
    expires_at: float
    platform_timeout_minutes: int
    idle_timeout_seconds: float
    state: str = "creating"
    owner: str | None = None
    task_label: str | None = None
    idempotency_key: str | None = None
    platform_session_id: str | None = None
    platform_timeout_at: float | None = None
    session: BrowserSession | None = None
    cost_reserved_usd: float = 0.0
    last_activity_at: float = field(default_factory=time.time)
    activated_at: float | None = None
    release_reason: str | None = None
    inflight: int = 0
    tool_calls: int = 0
    # The task creating this lease's browser, while state is "creating". A
    # release that arrives mid-creation waits on it instead of racing it.
    creation_task: asyncio.Task | None = field(default=None, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    # ----- clocks ----------------------------------------------------------

    def touch(self) -> None:
        self.last_activity_at = time.time()

    @property
    def idle_seconds(self) -> float:
        return time.time() - self.last_activity_at

    @property
    def age_seconds(self) -> float:
        return time.time() - self.created_at

    @property
    def lease_remaining_seconds(self) -> float:
        return max(0.0, self.expires_at - time.time())

    @property
    def idle_remaining_seconds(self) -> float:
        return max(0.0, self.idle_timeout_seconds - self.idle_seconds)

    @property
    def is_usable(self) -> bool:
        return self.state == ST_ACTIVE and self.session is not None

    # ----- call guard ------------------------------------------------------

    @asynccontextmanager
    async def guard(self, *, exclusive: bool = True, timeout: float = 180.0):
        """Serialise work on this lease and keep the idle clock honest.

        ``exclusive=True`` is right for anything that drives the browser -- a
        browser cannot meaningfully click two things at once, and overlapping
        operations produce races that look like flaky sites.

        ``exclusive=False`` exists for the debugger wait tools: ``js_pause_state``
        may block for a minute waiting for a breakpoint, and holding the lock
        there would deadlock the very ``js_step`` call needed to release it.
        """
        self._require_active()

        self.inflight += 1
        self.tool_calls += 1
        try:
            if not exclusive:
                self.touch()
                yield self
                self.touch()
                return
            try:
                async with asyncio.timeout(timeout):
                    await self._lock.acquire()
            except TimeoutError as exc:
                raise ToolTimeout(
                    f"another operation on lease {self.lease_id} has been running for "
                    f"over {timeout:.0f}s",
                    hint="Wait for it, or release this lease and acquire a new browser.",
                ) from exc
            try:
                # The wait for the lock can outlast the lease: the reaper may
                # have released it while this call queued behind another one.
                self._require_active()
                self.touch()
                yield self
                self.touch()
            finally:
                self._lock.release()
        finally:
            self.inflight -= 1

    def _require_active(self) -> None:
        if self.state == ST_CREATING:
            # Not "no longer usable -- acquire a fresh one": that advice made
            # agents buy a second browser while the first was still starting.
            raise LeaseNotActive(
                f"lease {self.lease_id} is still starting",
                hint=(
                    "The browser is not ready yet. Wait a few seconds and retry this "
                    "call (or poll browser_queue_status with your ticket_id). Do NOT "
                    "call browser_acquire for another browser."
                ),
            )
        if self.state != ST_ACTIVE:
            if self.release_reason in ("idle_timeout", "max_lifetime", "platform_timeout"):
                raise LeaseExpired(
                    f"lease {self.lease_id} was reclaimed ({self.release_reason})"
                )
            raise LeaseNotActive(f"lease {self.lease_id} is {self.state}, not active")

    # ----- views -----------------------------------------------------------

    def public(self, *, include_session: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "lease_id": self.lease_id,
            "state": self.state,
            "task_label": self.task_label,
            "provider": self.provider_name,
            "network": self.network_kind,
            "scope": self.scope.describe(),
            "age_seconds": round(self.age_seconds, 1),
            "idle_seconds": round(self.idle_seconds, 1),
            "lease_expires_in_seconds": round(self.lease_remaining_seconds, 1),
            "reclaimed_if_idle_for_seconds": round(self.idle_remaining_seconds, 1),
            "tool_calls": self.tool_calls,
            "in_flight_calls": self.inflight,
            "cost_reserved_usd": round(self.cost_reserved_usd, 6),
            "release_reason": self.release_reason,
        }
        if self.platform_timeout_at:
            out["platform_hard_stop_in_seconds"] = round(
                max(0.0, self.platform_timeout_at - time.time()), 1
            )
        if include_session and self.session is not None:
            out["browser"] = self.session.stats()
        return out
