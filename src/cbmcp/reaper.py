"""Automatic reclamation and platform reconciliation.

Agent prompts cannot be trusted to release browsers, and a Claude Code subagent
shares the parent's MCP connection, so there is no socket close to detect its
exit. Reclamation therefore runs on three independent layers, each of which
holds if the ones above it fail:

1. **Idle timeout.** Every tool call touches the lease. No calls for
   ``idle_timeout_seconds`` means nobody is driving it -- reclaim. This is the
   layer that catches an agent that crashed, was cancelled, or simply wandered
   off, and it needs no cooperation from the agent at all.
2. **Absolute lease lifetime.** ``max_lease_seconds`` regardless of activity, so
   a wedged loop calling tools forever still terminates.
3. **Platform-side timeout.** Set on the session at creation. This one survives
   ``kill -9`` of this process, a power cut, or a bug in everything above.

On top of that, **reconciliation** compares the platform's own list of running
sessions against local state in both directions: sessions the platform is
billing that we no longer own get stopped; sessions we think we hold that the
platform has already stopped get cleaned up locally so their slot comes back.

Orphan identification is deliberately conservative. The owner tag in session
metadata identifies this *deployment* (stable across restarts), and the shared
SQLite database is the authority on which lease a session belongs to. A session
is only stopped when the database agrees it is dead. Sessions belonging to
another live process, or to another user of the same cloud account, are never
touched.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

from .budget import BudgetGuard
from .config import Settings
from .log import get_logger
from .pool import BrowserPool
from .providers.base import Provider, ProviderSession
from .store import (
    SLOT_STATES,
    ST_ACTIVE,
    ST_CREATING,
    ST_RELEASED,
    ST_UNKNOWN,
    Store,
)

log = get_logger("reaper")


class Reaper:
    # How many reconciliation passes to keep re-sending a stop the platform has
    # not honoured before returning the slot and escalating to the operator.
    MAX_STOP_ATTEMPTS = 10

    def __init__(
        self,
        *,
        settings: Settings,
        store: Store,
        provider: Provider,
        pool: BrowserPool,
        budget: BudgetGuard,
    ) -> None:
        self.s = settings
        self.store = store
        self.provider = provider
        self.pool = pool
        self.budget = budget
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._last_reconcile = 0.0
        # platform_session_id -> how many times we have re-sent an unhonoured stop
        self._stop_attempts: dict[str, int] = {}
        self.stats: dict[str, Any] = {
            "sweeps": 0,
            "reclaimed_idle": 0,
            "reclaimed_max_age": 0,
            "reclaimed_dead": 0,
            "reclaimed_platform_timeout": 0,
            "orphans_stopped": 0,
            "orphans_seen_other_instance": 0,
            "unknown_settled": 0,
            "queue_timeouts": 0,
            "reclaimed_unclaimed": 0,
            "stops_given_up": 0,
            "reconciles": 0,
            "last_reconcile_at": None,
            "last_error": None,
        }

    # ----- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="cbmcp-reaper")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.sweep()
                if time.time() - self._last_reconcile >= self.s.reconcile_interval_seconds:
                    await self.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the reaper must never die
                self.stats["last_error"] = f"{type(exc).__name__}: {exc}"[:200]
                log.exception("reaper sweep failed")
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(self.s.reaper_interval_seconds):
                    await self._stop.wait()

    # ----- layer 1 & 2: local sweep ---------------------------------------

    async def sweep(self) -> dict[str, int]:
        self.stats["sweeps"] += 1
        outcome = {"idle": 0, "max_age": 0, "dead": 0, "platform": 0}
        now = time.time()

        # The pool's queue only advances when a slot is freed, so without this a
        # full pool with no releases would never time a queued ticket out, and a
        # lease granted to a ticket nobody polled would bill until it went idle.
        housekeeping = await self.pool.maintain()
        if housekeeping["expired_tickets"]:
            self.stats["queue_timeouts"] += housekeeping["expired_tickets"]
        if housekeeping["reclaimed_unclaimed"]:
            self.stats["reclaimed_unclaimed"] += len(housekeeping["reclaimed_unclaimed"])

        for lease in list(self.pool.live_leases()):
            if lease.state != ST_ACTIVE:
                continue
            reason: str | None = None

            if lease.idle_seconds > lease.idle_timeout_seconds:
                reason, key, stat = (
                    "idle_timeout",
                    "idle",
                    "reclaimed_idle",
                )
            elif now >= lease.expires_at:
                reason, key, stat = ("max_lifetime", "max_age", "reclaimed_max_age")
            elif lease.platform_timeout_at and lease.platform_timeout_at - now < 45:
                # Get ahead of the platform so costs and state are settled by us
                # rather than discovered later as a surprise.
                reason, key, stat = (
                    "platform_timeout",
                    "platform",
                    "reclaimed_platform_timeout",
                )
            elif not await self._is_alive(lease):
                reason, key, stat = ("connection_lost", "dead", "reclaimed_dead")

            if reason is None:
                continue
            log.info(
                "reclaiming lease %s (%s): idle=%.0fs age=%.0fs task=%s",
                lease.lease_id,
                reason,
                lease.idle_seconds,
                lease.age_seconds,
                lease.task_label,
            )
            with contextlib.suppress(Exception):
                await self.pool.release(lease, reason=reason)
            outcome[key] += 1
            self.stats[stat] += 1
            await self.store.bump(f"reclaim_{reason}")
        return outcome

    async def _is_alive(self, lease: Any) -> bool:
        if lease.session is None:
            return False
        if lease.inflight > 0:
            return True  # a call in progress is proof enough
        try:
            return await lease.session.alive()
        except Exception:  # noqa: BLE001
            return False

    # ----- reconciliation --------------------------------------------------

    async def reconcile(self) -> dict[str, Any]:
        """Compare platform truth with local state, in both directions."""
        self._last_reconcile = time.time()
        self.stats["reconciles"] += 1
        await self.pool.refresh_platform_limit()

        try:
            remote = await self.provider.list_sessions(active_only=True, limit=500)
        except Exception as exc:  # noqa: BLE001
            self.stats["last_error"] = f"list_sessions: {exc}"[:200]
            log.warning("reconcile could not list platform sessions: %s", exc)
            return {"ok": False, "error": str(exc)[:200]}

        remote_by_id = {session.id: session for session in remote}
        ours_live = {
            lease.platform_session_id: lease
            for lease in self.pool.live_leases()
            if lease.platform_session_id
        }

        report: dict[str, Any] = {
            "platform_active": len(remote),
            "ours_live": len(ours_live),
            "orphans_stopped": [],
            "other_instance": [],
            "untagged": 0,
            "vanished": [],
            "settled_unknown": [],
        }

        # Direction 1: the platform is billing something we do not hold.
        for session in remote:
            if session.id in ours_live:
                await self.store.upsert_platform_session(
                    platform_session_id=session.id,
                    provider=self.provider.name,
                    state="active",
                    cost_usd=session.total_cost_usd,
                    proxy_mb=session.proxy_mb,
                )
                continue
            verdict = await self._classify(session)
            if verdict == "orphan":
                stopped = await self.provider.stop_session(session.id)
                await self.store.upsert_platform_session(
                    platform_session_id=session.id,
                    provider=self.provider.name,
                    state="stopped" if stopped else "unknown",
                    cost_usd=session.total_cost_usd,
                    proxy_mb=session.proxy_mb,
                    stopped=True,
                    stop_confirmed=stopped,
                    note="orphan stopped by reconciliation",
                )
                await self.store.bump("orphans_stopped")
                await self.store.audit(
                    "reconcile.orphan_stopped",
                    target=session.id,
                    outcome="stopped" if stopped else "stop_failed",
                    cost_usd=session.total_cost_usd,
                    lease_id=session.metadata.get("cbmcp_lease"),
                )
                self.stats["orphans_stopped"] += 1
                report["orphans_stopped"].append(session.id)
                log.warning(
                    "stopped orphan cloud session %s (lease=%s, cost so far $%.4f)",
                    session.id,
                    session.metadata.get("cbmcp_lease"),
                    session.total_cost_usd,
                )
            elif verdict == "other_instance":
                self.stats["orphans_seen_other_instance"] += 1
                report["other_instance"].append(session.id)
            else:
                report["untagged"] += 1

        # Direction 2: we think we hold something the platform has stopped.
        for session_id, lease in list(ours_live.items()):
            if session_id in remote_by_id:
                continue
            if lease.state == ST_UNKNOWN:
                await self.store.upsert_platform_session(
                    platform_session_id=session_id,
                    provider=self.provider.name,
                    state="stopped",
                    stopped=True,
                    stop_confirmed=True,
                    note="confirmed stopped by reconciliation",
                )
                await self.pool.settle_unknown(lease.lease_id)
                self.stats["unknown_settled"] += 1
                report["settled_unknown"].append(lease.lease_id)
                continue
            if lease.state in (ST_ACTIVE, ST_CREATING):
                # The cloud session is gone but the lease still looks alive: the
                # platform timed it out or stopped it underneath us.
                log.warning(
                    "lease %s lost its cloud session %s; releasing locally",
                    lease.lease_id,
                    session_id,
                )
                with contextlib.suppress(Exception):
                    await self.pool.release(lease, reason="platform_session_gone")
                report["vanished"].append(lease.lease_id)

        await self._settle_unconfirmed_stops()
        await self._check_stale_intents()

        self.stats["last_reconcile_at"] = time.time()
        report["ok"] = True
        return report

    async def _classify(self, session: ProviderSession) -> str:
        """Decide whether a platform session is ours to stop.

        Conservative on purpose: stopping someone else's browser mid-task is
        worse than paying for a few extra minutes while we work it out.
        """
        owner = session.metadata.get("cbmcp_owner")
        if owner != self.s.owner_tag:
            if not owner and self.s.reconcile_stop_untagged:
                return "orphan"
            return "untagged"

        lease_id = session.metadata.get("cbmcp_lease")
        if not lease_id:
            return "orphan"  # our tag, but no lease to belong to

        row = await self.store.get_lease(lease_id)
        if row is None:
            return "orphan"  # our tag, lease unknown even to the database
        if row.state not in SLOT_STATES:
            return "orphan"  # the database says this lease is finished
        instance = session.metadata.get("cbmcp_instance")
        if instance == self.s.instance_id:
            # Our own instance, live in the DB, but not in memory -- it cannot be
            # driven any more, so it is ours to stop.
            return "orphan"
        # Another process on this deployment still claims it. Leave it alone and
        # let that process (or its own restart recovery) deal with it.
        return "other_instance"

    async def _settle_unconfirmed_stops(self) -> None:
        """Keep chasing stops the platform never confirmed, and settle cost drift."""
        for row in await self.store.unconfirmed_stops():
            session_id = row["platform_session_id"]
            try:
                remote = await self.provider.get_session(session_id)
            except Exception:  # noqa: BLE001
                continue
            if remote is None or remote.status == "stopped":
                recorded = float(row["cost_usd"] or 0.0)
                actual = remote.total_cost_usd if remote else 0.0
                await self.store.upsert_platform_session(
                    platform_session_id=session_id,
                    provider=self.provider.name,
                    state="stopped",
                    cost_usd=max(recorded, actual),
                    proxy_mb=remote.proxy_mb if remote else float(row["proxy_mb"] or 0.0),
                    stopped=True,
                    stop_confirmed=True,
                    note="stop confirmed on retry",
                )
                # Converge the ledger without double-counting: only the delta.
                if actual > recorded:
                    await self.store.add_usage(
                        cost_usd=actual - recorded, browser_seconds=0.0, proxy_mb=0.0
                    )
                if row["lease_id"]:
                    await self.pool.settle_unknown(row["lease_id"])
                    lease_row = await self.store.get_lease(row["lease_id"])
                    if lease_row is not None and lease_row.state == ST_UNKNOWN:
                        await self.store.update_lease(row["lease_id"], state=ST_RELEASED)
                self._stop_attempts.pop(session_id, None)
            else:
                attempts = self._stop_attempts.get(session_id, 0) + 1
                self._stop_attempts[session_id] = attempts
                log.warning(
                    "cloud session %s is still active after a stop request "
                    "(attempt %d/%d); retrying",
                    session_id,
                    attempts,
                    self.MAX_STOP_ATTEMPTS,
                )
                await self.provider.stop_session(session_id)

                if attempts >= self.MAX_STOP_ATTEMPTS:
                    # Holding the slot forever would permanently shrink capacity
                    # over a session we cannot influence. Give the slot back,
                    # record it loudly, and rely on the platform-side timeout --
                    # which is the one guarantee that does not depend on us.
                    self._stop_attempts.pop(session_id, None)
                    await self.store.bump("stop_given_up")
                    self.stats["stops_given_up"] += 1
                    await self.store.audit(
                        "reconcile.stop_given_up",
                        lease_id=row["lease_id"],
                        target=session_id,
                        outcome="abandoned",
                        attempts=attempts,
                        note="slot returned; platform timeout is now the only backstop",
                    )
                    log.error(
                        "giving up on stopping cloud session %s after %d attempts; "
                        "its slot is being returned and the platform timeout is now the "
                        "only thing that will end it -- check the account manually",
                        session_id,
                        attempts,
                    )
                    if row["lease_id"]:
                        await self.pool.settle_unknown(row["lease_id"])

    async def _check_stale_intents(self) -> None:
        """Creation attempts whose outcome we never learned.

        The owner tag sweep above normally catches these, but an intent that is
        still unresolved after a few minutes means we lost the race entirely --
        worth an audit record so it is visible rather than silent.
        """
        cutoff = time.time() - 180
        for row in await self.store.unresolved_intents(cutoff):
            await self.store.resolve_create_intent(
                row["intent_id"], None, note="abandoned; swept by reconciliation"
            )
            await self.store.audit(
                "reconcile.stale_intent",
                lease_id=row["lease_id"],
                outcome="abandoned",
                intent_id=row["intent_id"],
            )
            log.warning(
                "create intent %s (lease %s) never resolved; relying on the owner-tag "
                "sweep to stop any session it may have started",
                row["intent_id"],
                row["lease_id"],
            )

    # ----- startup ---------------------------------------------------------

    async def startup_recovery(self) -> dict[str, Any]:
        """Deal with whatever the previous process left behind, before serving."""
        stale = await self.store.mark_stale_on_startup(self.s.instance_id)
        if stale:
            log.warning(
                "%d lease(s) from a previous run were still marked live; they are now "
                "'unknown' and will be reconciled",
                len(stale),
            )
            for row in stale:
                await self.store.audit(
                    "startup.stale_lease",
                    lease_id=row.lease_id,
                    outcome="unknown",
                    platform_session_id=row.platform_session_id,
                )
                # Stop them directly: the connection died with the old process,
                # so nothing can be driving them.
                if row.platform_session_id:
                    with contextlib.suppress(Exception):
                        stopped = await self.provider.stop_session(row.platform_session_id)
                        await self.store.upsert_platform_session(
                            platform_session_id=row.platform_session_id,
                            provider=self.provider.name,
                            state="stopped" if stopped else "unknown",
                            lease_id=row.lease_id,
                            stopped=True,
                            stop_confirmed=stopped,
                            note="stopped during startup recovery",
                        )
                        if stopped:
                            await self.store.update_lease(
                                row.lease_id, state=ST_RELEASED, release_reason="startup_recovery"
                            )

        await self.pool.restore_slots()
        result: dict[str, Any] = {"stale_leases": len(stale)}
        if self.s.reconcile_on_startup:
            result["reconcile"] = await self.reconcile()
        return result

    def snapshot(self) -> dict[str, Any]:
        out = dict(self.stats)
        if out.get("last_reconcile_at"):
            out["seconds_since_reconcile"] = round(time.time() - out["last_reconcile_at"], 1)
        return out
