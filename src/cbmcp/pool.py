"""Dynamic allocation: concurrency ceiling, fair queue, and the create/release path.

There is no pool of pre-warmed browsers and no fixed instance count. Browsers
are created when an agent asks and destroyed when it is done, so the steady
state with no work is zero running sessions and zero spend. The only limit is a
configurable ceiling, further clamped by the platform's own concurrency quota.

Three subtleties that this design exists to handle:

**Slots are held by uncertainty, not just by use.** ``creating``, ``active``,
``releasing`` and ``unknown`` all occupy a slot. A session we cannot prove is
stopped may still be billing, so it keeps its slot until reconciliation says
otherwise. Releasing a slot we are unsure about would overshoot the real quota
and get 429s from the platform.

**Queue waits must not collide with the MCP client's tool timeout.** If
``browser_acquire`` blocked indefinitely, Claude Code would time out and the
agent would retry -- creating a second paid session, which is the exact failure
the requirements call out. So a wait is bounded by ``acquire_block_seconds``
(well under the client timeout) and then hands back a ticket to poll. The
ticket, not the caller, owns the slot: fulfilment continues in the background,
so a lease is ready and waiting by the time the agent polls.

**Creation is written down before it happens.** ``record_create_intent`` plus an
owner tag in the session's platform metadata give two independent ways to find a
session whose creation we never learned the outcome of.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import Playwright

from .budget import BudgetGuard
from .config import Settings
from .errors import (
    CapacityExhausted,
    CbmcpError,
    InvalidArgument,
    LeaseExpired,
    LeaseNotActive,
    LeaseNotFound,
    ProviderConcurrencyLimit,
    ProviderError,
)
from .lease import Lease
from .log import get_logger
from .providers import network_spec_from_settings
from .providers.base import Provider, SessionSpec
from .scope import ScopeRule, global_scope
from .session import BrowserSession
from .store import (
    ST_ACTIVE,
    ST_CREATING,
    ST_FAILED,
    ST_RELEASED,
    ST_RELEASING,
    ST_UNKNOWN,
    Store,
    new_lease_id,
    new_lease_token,
)

log = get_logger("pool")


@dataclass
class AcquireRequest:
    task_label: str | None = None
    idempotency_key: str | None = None
    network_mode: str | None = None
    scope_allow: list[str] | None = None
    scope_deny: list[str] | None = None
    scope_mode: str | None = None
    lease_seconds: float | None = None
    profile_id: str | None = None
    debug: str | None = None
    screen_width: int | None = None
    screen_height: int | None = None
    solve_captchas: bool | None = None


@dataclass
class Ticket:
    ticket_id: str
    request: AcquireRequest
    created_at: float = field(default_factory=time.time)
    event: asyncio.Event = field(default_factory=asyncio.Event)
    granted_at: float | None = None
    lease: Lease | None = None
    error: CbmcpError | None = None
    cancelled: bool = False
    task: asyncio.Task | None = None

    @property
    def settled(self) -> bool:
        return self.lease is not None or self.error is not None or self.cancelled


class BrowserPool:
    def __init__(
        self,
        *,
        settings: Settings,
        store: Store,
        provider: Provider,
        budget: BudgetGuard,
        playwright: Playwright,
    ) -> None:
        self.s = settings
        self.store = store
        self.provider = provider
        self.budget = budget
        self._pw = playwright

        self._slots_used = 0
        self._slot_lock = asyncio.Lock()
        self._pump_lock = asyncio.Lock()
        # Money committed by acquires that hold a slot but have not yet written
        # their reservation row. Guarded by _slot_lock.
        self._pending_reserve = 0.0
        self._queue: list[Ticket] = []
        self._tickets: dict[str, Ticket] = {}
        self._leases: dict[str, Lease] = {}
        self._by_token: dict[str, str] = {}
        # token -> (lease_id, release reason, when), for leases that are gone.
        self._gone: OrderedDict[str, tuple[str, str, float]] = OrderedDict()
        self._by_idempotency: dict[str, str] = {}
        # Slots counted at startup for leases a dead process left behind. They
        # are not in self._leases, so settle_unknown needs this to return them.
        self._restored_slot_ids: set[str] = set()
        # Slots held by sibling gateways (other Claude windows), cached briefly.
        # Display only: the ceiling itself is enforced by claims in the database.
        self._others = 0
        self._others_at = 0.0
        # This gateway's rows in slot_claims, one per slot it holds.
        self._claims: list[str] = []
        # Set when the platform says the account is full; no new creation is
        # attempted before it, so a full account is not hammered with POSTs.
        self._platform_backoff_until = 0.0
        self.platform_limit: int | None = None
        self._global_scope: ScopeRule = global_scope(
            settings.scope_mode, settings.scope_allow, settings.scope_deny
        )

    # ----- capacity --------------------------------------------------------

    @property
    def effective_max(self) -> int:
        limit = self.s.max_concurrency
        if self.s.respect_platform_limit and self.platform_limit:
            limit = min(limit, self.platform_limit)
        return max(1, limit)

    async def refresh_platform_limit(self) -> int | None:
        with contextlib.suppress(Exception):
            info = await self.provider.account_info()
            if info.concurrent_session_limit:
                if self.platform_limit != info.concurrent_session_limit:
                    log.info(
                        "platform concurrency limit is %s (configured max %s)",
                        info.concurrent_session_limit,
                        self.s.max_concurrency,
                    )
                self.platform_limit = int(info.concurrent_session_limit)
        return self.platform_limit

    async def restore_slots(self, live_instances: set[str] | None = None) -> None:
        """Re-establish the slot count from persisted state after a restart.

        Only leases no live process owns are counted. Leases of a sibling
        gateway that is still running occupy *its* slots, not ours, and it will
        free them itself -- counting them here would leak capacity forever.
        """
        orphaned = await self.store.orphaned_slot_holders(
            self.s.instance_id, live_instances or set()
        )
        held = len(orphaned)
        claims: list[str] = []
        if held and self.s.share_concurrency_across_gateways:
            # Those sessions may still be running, so they count against the
            # machine-wide ceiling too, until their stop is confirmed.
            with contextlib.suppress(Exception):
                claims = await self.store.add_claims(self.s.instance_id, held)
        async with self._slot_lock:
            self._restored_slot_ids = set(orphaned)
            self._slots_used = held
            self._claims = claims
        if held:
            log.warning(
                "%d slot(s) still held by sessions from a previous run; the reaper "
                "will chase them until the platform confirms they are stopped",
                held,
            )

    def stats(self) -> dict[str, Any]:
        live = [lease for lease in self._leases.values() if lease.state in (ST_ACTIVE, ST_CREATING)]
        return {
            "active_leases": len([x for x in live if x.state == ST_ACTIVE]),
            "creating": len([x for x in live if x.state == ST_CREATING]),
            "slots_used": self._slots_used,
            "slots_used_by_other_gateways": self._others,
            "platform_full_retry_in_seconds": round(self.platform_backoff_seconds, 1) or None,
            "max_concurrency_configured": self.s.max_concurrency,
            "max_concurrency_effective": self.effective_max,
            "platform_concurrency_limit": self.platform_limit,
            "queue_length": len([t for t in self._queue if not t.settled]),
            "tickets_tracked": len(self._tickets),
        }

    # ----- lease lookup ----------------------------------------------------

    def resolve_token(self, token: str) -> Lease:
        if not token or not isinstance(token, str):
            raise InvalidArgument(
                "lease token missing",
                hint="Pass the 'lease' value that browser_acquire returned.",
            )
        lease_id = self._by_token.get(token.strip())
        lease = self._leases.get(lease_id) if lease_id else None
        if lease is None:
            gone = self._gone.get(token.strip())
            if gone is not None:
                self._explain_gone(*gone)
            raise LeaseNotFound("unknown or expired lease token")
        return lease

    _RECLAIMED = {
        "idle_timeout": "no tool call for {idle:.0f}s",
        "max_lifetime": "it reached the maximum lease lifetime",
        "platform_timeout": "the platform's hard time limit was about to end it",
        "platform_session_gone": "the cloud platform stopped the session",
        "connection_lost": "the connection to the cloud browser broke",
        "ticket_never_claimed": "it was never picked up from the queue",
    }

    def _explain_gone(self, lease_id: str, reason: str, at: float) -> None:
        """Say WHY a token stopped working, not just that it is unknown.

        Measured in a usage simulation: an agent that paused past the idle
        timeout came back to "unknown or expired lease token" -- and had no way
        to learn that browser_renew would have kept its browser.
        """
        ago = time.time() - at
        if reason in self._RECLAIMED:
            why = self._RECLAIMED[reason].format(idle=self.s.idle_timeout_seconds)
            hint = (
                "Call browser_acquire for a new browser (the page state is gone). "
                "Before a long pause between browser steps, call browser_renew."
                if reason == "idle_timeout"
                else "Call browser_acquire for a new browser; the page state is gone."
            )
            raise LeaseExpired(
                f"lease {lease_id} was reclaimed {ago:.0f}s ago: {why}",
                hint=hint,
                reason=reason,
            )
        raise LeaseNotActive(
            f"lease {lease_id} was released {ago:.0f}s ago ({reason})",
            hint="This browser is gone. Call browser_acquire only if you need one again.",
            reason=reason,
        )

    def lease_by_id(self, lease_id: str) -> Lease | None:
        return self._leases.get(lease_id)

    def live_leases(self) -> list[Lease]:
        return [
            lease
            for lease in self._leases.values()
            if lease.state in (ST_ACTIVE, ST_CREATING, ST_RELEASING, ST_UNKNOWN)
        ]

    # ----- acquire ---------------------------------------------------------

    async def acquire(self, request: AcquireRequest) -> dict[str, Any]:
        # Idempotency first: a retried tool call must never buy a second browser.
        if request.idempotency_key:
            existing_id = self._by_idempotency.get(request.idempotency_key)
            existing = self._leases.get(existing_id) if existing_id else None
            if existing is not None and existing.state == ST_CREATING:
                # A retry while the browser is still starting. Handing out the
                # token now sent the agent to page tools that refuse a starting
                # browser, with an error that said to acquire a fresh one -- a
                # second paid session. Wait as a first call would, then answer
                # with the browser or with the ticket to keep polling.
                task = existing.creation_task
                if task is not None and not task.done():
                    await asyncio.wait({task}, timeout=self.s.acquire_block_seconds)
            if existing is not None and existing.state == ST_ACTIVE:
                # The token is being handed over right here, so any ticket still
                # holding this lease has done its job. Left behind, the
                # unclaimed-lease sweep would release a browser the agent has.
                for ticket_id, ticket in list(self._tickets.items()):
                    if ticket.lease is existing:
                        self._tickets.pop(ticket_id, None)
                return {**self._ready_view(existing), "idempotent_hit": True}
            for ticket in self._tickets.values():
                if (
                    ticket.request.idempotency_key == request.idempotency_key
                    and not ticket.settled
                ):
                    return {**self._ticket_view(ticket), "idempotent_hit": True}
            if existing is not None and existing.state == ST_CREATING:
                return {
                    "status": "creating",
                    "idempotent_hit": True,
                    "lease_id": existing.lease_id,
                    "next": (
                        "Your browser is still starting. Call browser_acquire again with "
                        "the same idempotency_key in a few seconds; it returns the same "
                        "browser once ready and never starts a second one."
                    ),
                }

        scope = self._global_scope.narrow(
            request.scope_allow, request.scope_deny, request.scope_mode
        )
        network = network_spec_from_settings(
            self.s,
            request.network_mode if self.s.allow_lease_network_override else None,
        )
        if request.network_mode and not self.s.allow_lease_network_override:
            if request.network_mode != self.s.network_mode:
                raise InvalidArgument(
                    f"network_mode is fixed to {self.s.network_mode!r} by the operator",
                    hint="Set CBMCP_ALLOW_LEASE_NETWORK_OVERRIDE=true to let leases choose.",
                )

        # Reject before spending anything if the budget is already gone.
        # pending_reserve covers acquires that have passed this check but have
        # not yet written their reservation row, which otherwise lets N
        # simultaneous callers each see the same "spent so far".
        await self.budget.require(
            self.s.platform_timeout_minutes, network, extra_outstanding=self._pending_reserve
        )

        # The ticket id is redeemable for the lease token via
        # browser_queue_status, so it must be as unguessable as the token.
        ticket = Ticket(ticket_id="tkt_" + secrets.token_urlsafe(16), request=request)
        self._tickets[ticket.ticket_id] = ticket

        granted = await self._try_take_slot(
            reserve=self.budget.worst_case_cost(self.s.platform_timeout_minutes, network)
        )
        if granted:
            ticket.granted_at = time.time()
            ticket.task = asyncio.create_task(self._fulfil(ticket, scope, network))
        else:
            self._queue.append(ticket)
            await self.store.bump("acquire_queued")

        await self._wait_for_ticket(ticket, self.s.acquire_block_seconds)

        if ticket.lease is not None:
            # Claimed here, so the ticket has done its job. Leaving it behind
            # would both leak the record and make the lease look unclaimed to
            # the housekeeping pass below.
            self._tickets.pop(ticket.ticket_id, None)
            return self._ready_view(ticket.lease)
        if ticket.error is not None:
            self._tickets.pop(ticket.ticket_id, None)
            raise ticket.error
        return self._ticket_view(ticket)

    async def _slots_elsewhere(self) -> int:
        """Slots held by other live gateways on this data dir (cached ~2s)."""
        if not self.s.share_concurrency_across_gateways:
            return 0
        now = time.time()
        if now - self._others_at >= 2.0:
            with contextlib.suppress(Exception):
                self._others = await self.store.claims_elsewhere(
                    self.s.instance_id, self.s.heartbeat_max_age_seconds
                )
                self._others_at = now
        return self._others

    @property
    def has_unsettled_stops(self) -> bool:
        """A slot is held only because a stop has not been confirmed yet."""
        return bool(self._restored_slot_ids) or any(
            lease.state == ST_UNKNOWN for lease in self._leases.values()
        )

    @property
    def platform_backoff_seconds(self) -> float:
        return max(0.0, self._platform_backoff_until - time.time())

    async def _try_take_slot(self, *, reserve: float = 0.0) -> bool:
        if self.platform_backoff_seconds > 0:
            return False
        async with self._slot_lock:
            if self._slots_used >= self.effective_max:
                return False
            if self.s.share_concurrency_across_gateways:
                # The machine-wide check: an atomic claim in the shared
                # database. A failed claim (ceiling reached elsewhere, or the
                # database unavailable) means no slot -- the request queues.
                claim: str | None = None
                with contextlib.suppress(Exception):
                    claim = await self.store.claim_slot(
                        self.s.instance_id,
                        self.effective_max,
                        self.s.heartbeat_max_age_seconds,
                    )
                if claim is None:
                    # Refresh the count shown to the waiting agent.
                    self._others_at = 0.0
                    await self._slots_elsewhere()
                    return False
                self._claims.append(claim)
            self._slots_used += 1
            self._pending_reserve += reserve
            return True

    async def _settle_pending_reserve(self, amount: float) -> None:
        """Drop the in-memory reservation once it is recorded in the database."""
        async with self._slot_lock:
            self._pending_reserve = max(0.0, self._pending_reserve - amount)

    async def _return_slot(self, reserve: float = 0.0) -> None:
        """Give a slot back (and its machine-wide claim) without pumping."""
        async with self._slot_lock:
            self._slots_used = max(0, self._slots_used - 1)
            self._pending_reserve = max(0.0, self._pending_reserve - reserve)
            claim = self._claims.pop() if self._claims else None
        if claim is not None:
            with contextlib.suppress(Exception):
                await self.store.release_claim(claim)

    async def _free_slot(self) -> None:
        await self._return_slot()
        await self._pump()

    def _expire_stale_tickets(self) -> int:
        """Fail tickets that have waited longer than the queue timeout."""
        now = time.time()
        count = 0
        for ticket in list(self._queue):
            if ticket.settled:
                continue
            if now - ticket.created_at > self.s.queue_timeout_seconds:
                ticket.error = CapacityExhausted(
                    f"queued for over {self.s.queue_timeout_seconds:.0f}s without a free "
                    "browser slot",
                    hint="Reduce parallel subagents, or raise CBMCP_MAX_CONCURRENCY.",
                )
                ticket.event.set()
                count += 1
        self._queue = [t for t in self._queue if not t.settled and not t.cancelled]
        return count

    async def maintain(self) -> dict[str, Any]:
        """Housekeeping, driven by the reaper on every sweep.

        ``_pump`` only runs when a slot is freed, so on its own a full pool with
        no releases would never time a queued ticket out -- it would wait for a
        slot that is not coming and never report the timeout it promised.
        """
        expired = self._expire_stale_tickets()
        reclaimed = await self._reclaim_unclaimed()
        await self._pump()
        return {"expired_tickets": expired, "reclaimed_unclaimed": reclaimed}

    async def _reclaim_unclaimed(self) -> list[str]:
        """Release leases granted to a ticket nobody ever came back for.

        An agent cancelled between ``browser_acquire`` and
        ``browser_queue_status`` leaves a live, billing browser whose token was
        never delivered to anyone. The idle reaper would get there eventually;
        this gets there in seconds instead of minutes.
        """
        deadline = self.s.acquire_block_seconds * 2 + 60
        now = time.time()
        reclaimed: list[str] = []
        for ticket_id, ticket in list(self._tickets.items()):
            lease = ticket.lease
            if lease is None:
                # A failed or timed-out ticket nobody came back to read.
                if ticket.settled and now - ticket.created_at > (
                    self.s.queue_timeout_seconds + deadline
                ):
                    self._tickets.pop(ticket_id, None)
                continue
            if ticket.cancelled:
                continue
            if now - (ticket.granted_at or ticket.created_at) < deadline:
                continue
            self._tickets.pop(ticket_id, None)
            if lease.state != ST_ACTIVE or lease.tool_calls > 0:
                continue  # it was claimed and used; not ours to reclaim
            log.warning(
                "lease %s was granted to ticket %s but never claimed; releasing",
                lease.lease_id,
                ticket_id,
            )
            with contextlib.suppress(Exception):
                await self.release(lease, reason="ticket_never_claimed")
            reclaimed.append(lease.lease_id)
        return reclaimed

    async def _pump(self) -> None:
        """Hand a freed slot to the longest-waiting live ticket (FIFO fairness).

        Serialised: releases now run in parallel (shutdown, release_all), and
        two pumps interleaving at the await below could both take a slot for
        the same head ticket, then find the queue empty -- an IndexError and a
        slot that is never given back.
        """
        async with self._pump_lock:
            while True:
                self._expire_stale_tickets()
                if not self._queue:
                    return

                head = self._queue[0]
                network = network_spec_from_settings(
                    self.s,
                    head.request.network_mode if self.s.allow_lease_network_override else None,
                )
                if not await self._try_take_slot(
                    reserve=self.budget.worst_case_cost(self.s.platform_timeout_minutes, network)
                ):
                    return
                if not self._queue or self._queue[0] is not head:
                    # The head was cancelled or expired while we took the slot.
                    await self._return_slot(
                        self.budget.worst_case_cost(self.s.platform_timeout_minutes, network)
                    )
                    continue
                ticket = self._queue.pop(0)
                ticket.granted_at = time.time()
                scope = self._global_scope.narrow(
                    ticket.request.scope_allow,
                    ticket.request.scope_deny,
                    ticket.request.scope_mode,
                )
                ticket.task = asyncio.create_task(self._fulfil(ticket, scope, network))

    async def _wait_for_ticket(self, ticket: Ticket, seconds: float) -> None:
        """Wait up to ``seconds`` for a ticket to settle, re-checking capacity.

        A slot freed by ANOTHER window's gateway raises no event here; only
        that gateway's process knew. Without re-checking, a waiter would sit
        until this gateway's next reaper sweep (15s by default). Pumping every
        couple of seconds -- the shared-count cache refreshes at the same pace
        -- picks such a slot up within about two seconds.
        """
        deadline = time.time() + max(0.0, seconds)
        while not ticket.settled:
            remaining = deadline - time.time()
            if remaining <= 0:
                return
            if ticket.granted_at is None:
                with contextlib.suppress(Exception):
                    await self._pump()
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(min(2.0, remaining)):
                    await ticket.event.wait()

    async def _fulfil(self, ticket: Ticket, scope: ScopeRule, network: Any) -> None:
        """Create the platform session and a verified browser connection.

        Owns the slot for the whole attempt and frees it on any failure.
        """
        request = ticket.request
        lease_id = new_lease_id()
        token = new_lease_token()
        now = time.time()
        lease_seconds = min(
            request.lease_seconds or self.s.max_lease_seconds, self.s.max_lease_seconds
        )
        lease = Lease(
            lease_id=lease_id,
            token=token,
            provider_name=self.provider.name,
            scope=scope,
            network_kind=network.kind,
            created_at=now,
            expires_at=now + lease_seconds,
            platform_timeout_minutes=self.s.platform_timeout_minutes,
            idle_timeout_seconds=self.s.idle_timeout_seconds,
            state=ST_CREATING,
            owner=None,
            task_label=request.task_label,
            idempotency_key=request.idempotency_key,
        )

        lease.creation_task = asyncio.current_task()

        # The slot was taken together with an in-memory reservation; both must be
        # handed back on every exit path -- cancellation at shutdown included --
        # or capacity and budget leak until the process restarts.
        reserve_amount = self.budget.worst_case_cost(self.s.platform_timeout_minutes, network)
        pending_settled = False
        intent_id: str | None = None
        platform_session = None
        session: BrowserSession | None = None
        try:
            # Checked again here: a queued ticket was only checked when it
            # arrived, and spending may have moved while it waited. Other
            # acquires' in-flight reservations count; this one's own does not.
            decision = await self.budget.require(
                self.s.platform_timeout_minutes,
                network,
                extra_outstanding=self._pending_reserve - reserve_amount,
            )
            lease.cost_reserved_usd = decision.reserve_usd

            self._leases[lease_id] = lease
            self._by_token[token] = lease_id
            if request.idempotency_key:
                self._by_idempotency[request.idempotency_key] = lease_id

            await self.store.insert_lease(
                lease_id=lease_id,
                token=token,
                provider=self.provider.name,
                state=ST_CREATING,
                owner=None,
                task_label=request.task_label,
                idempotency_key=request.idempotency_key,
                scope=list(scope.allow),
                network_mode=network.kind,
                expires_at=lease.expires_at,
                instance_id=self.s.instance_id,
                meta={"scope_mode": scope.mode, "scope_deny": list(scope.deny)},
            )
            await self.store.update_lease(lease_id, cost_reserved_usd=decision.reserve_usd)
            # The reservation is now durable, so the in-memory placeholder taken
            # alongside the slot can be dropped.
            await self._settle_pending_reserve(reserve_amount)
            pending_settled = True

            intent_id = await self.store.record_create_intent(
                lease_id=lease_id, provider=self.provider.name, owner_tag=self.s.owner_tag
            )

            spec = SessionSpec(
                timeout_minutes=self.s.platform_timeout_minutes,
                network=network,
                screen_width=request.screen_width or self.s.screen_width,
                screen_height=request.screen_height or self.s.screen_height,
                solve_captchas=(
                    self.s.solve_captchas
                    if request.solve_captchas is None
                    else request.solve_captchas
                ),
                pdf_renderer_enabled=self.s.pdf_renderer_enabled,
                enable_recording=self.s.enable_recording,
                profile_id=request.profile_id,
                metadata={
                    "cbmcp_owner": self.s.owner_tag,
                    "cbmcp_instance": self.s.instance_id,
                    "cbmcp_lease": lease_id,
                },
            )

            platform_session = await self.provider.create_session(spec)
            await self.store.resolve_create_intent(intent_id, platform_session.id)
            await self.store.upsert_platform_session(
                platform_session_id=platform_session.id,
                provider=self.provider.name,
                state="active",
                lease_id=lease_id,
                instance_id=self.s.instance_id,
            )
            lease.platform_session_id = platform_session.id
            lease.platform_timeout_at = platform_session.timeout_at or (
                now + self.s.platform_timeout_minutes * 60
            )
            await self.store.update_lease(
                lease_id,
                platform_session_id=platform_session.id,
                platform_timeout_at=lease.platform_timeout_at,
            )

            session = BrowserSession(
                lease_id=lease_id,
                settings=self.s,
                playwright=self._pw,
                scope=scope,
                work_dir=self.s.artifacts_dir / lease_id,
                debug_mode=(request.debug or self.s.debug_default),
            )
            connect_info = await session.connect(platform_session.cdp_url or "")
            lease.session = session
            lease.state = ST_ACTIVE
            lease.creation_task = None
            lease.activated_at = time.time()
            lease.touch()
            await self.store.update_lease(
                lease_id,
                state=ST_ACTIVE,
                activated_at=lease.activated_at,
                last_activity_at=lease.last_activity_at,
            )
            await self.store.bump("acquire_success")
            await self.store.audit(
                "lease.acquire",
                lease_id=lease_id,
                actor=request.task_label,
                outcome="ok",
                platform_session_id=platform_session.id,
                network=network.kind,
                scope=scope.describe(),
                connect_ms=connect_info.get("connect_ms"),
            )
            ticket.lease = lease
            ticket.event.set()
            log.info(
                "lease %s active (task=%s, slots %d/%d)",
                lease_id,
                request.task_label,
                self._slots_used,
                self.effective_max,
            )
        except BaseException as exc:  # noqa: BLE001 - every failure must free the slot
            # BaseException, not Exception: a CancelledError (gateway shutdown,
            # or a release that gave up waiting) would otherwise skip all of
            # this and leak the slot and possibly a billing cloud session.
            cancelled = not isinstance(exc, Exception)
            # The account is full right now -- usually because other Claude
            # windows' gateways hold the rest of it. That clears by itself, so
            # put the request back at the head of the queue and pause creation
            # briefly, instead of failing an agent that only needed to wait.
            requeue = (
                isinstance(exc, ProviderConcurrencyLimit)
                and platform_session is None
                and not ticket.cancelled
                and time.time() - ticket.created_at < self.s.queue_timeout_seconds
            )
            if requeue:
                hinted = exc.detail.get("retry_after_seconds") if exc.detail else None
                delay = min(max(float(hinted or 10.0), 5.0), 60.0)
                self._platform_backoff_until = time.time() + delay
                log.warning(
                    "cloud account at its concurrency limit; ticket %s requeued, "
                    "retrying in %.0fs",
                    ticket.ticket_id,
                    delay,
                )
            with contextlib.suppress(Exception):
                await self._cleanup_failed_create(
                    lease=lease,
                    ticket=ticket,
                    exc=exc,
                    intent_id=intent_id,
                    platform_session_id=platform_session.id if platform_session else None,
                    session=session,
                    reserve_amount=reserve_amount if not pending_settled else 0.0,
                    requeue=requeue,
                )
            if cancelled:
                raise
            return

        # Outside the try on purpose: a failure releasing this (now healthy)
        # lease must not run the creation-failure path and free its slot twice.
        if ticket.cancelled:
            # The agent gave up while this browser was starting. Nobody will
            # ever receive its token, so stop it now rather than let it bill
            # until the idle reaper notices.
            with contextlib.suppress(Exception):
                await self.release(lease, reason="ticket_cancelled")

    async def _cleanup_failed_create(
        self,
        *,
        lease: Lease,
        ticket: Ticket,
        exc: BaseException,
        intent_id: str | None,
        platform_session_id: str | None,
        session: BrowserSession | None,
        reserve_amount: float,
        requeue: bool = False,
    ) -> None:
        lease_id = lease.lease_id
        lease.state = ST_FAILED
        lease.release_reason = "create_failed"
        lease.creation_task = None
        try:
            if reserve_amount:
                await self._settle_pending_reserve(reserve_amount)

            # Never leave a session behind just because our side failed. This
            # goes first: it is the step that stops the billing.
            if platform_session_id is not None:
                stopped = False
                with contextlib.suppress(Exception):
                    stopped = await self.provider.stop_session(platform_session_id)
                with contextlib.suppress(Exception):
                    await self.store.upsert_platform_session(
                        platform_session_id=platform_session_id,
                        provider=self.provider.name,
                        state="stopped" if stopped else "unknown",
                        lease_id=lease_id,
                        stopped=True,
                        stop_confirmed=stopped,
                        note="stopped after failed acquire",
                    )
            if session is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(session.aclose(), timeout=30)

            with contextlib.suppress(Exception):
                if intent_id is not None:
                    await self.store.resolve_create_intent(
                        intent_id, platform_session_id, note=str(exc)[:200]
                    )
                await self.store.update_lease(
                    lease_id,
                    state=ST_FAILED,
                    release_reason="create_failed",
                    released_at=time.time(),
                )
                await self.store.bump("acquire_failure")
                await self.store.audit(
                    "lease.acquire",
                    lease_id=lease_id,
                    actor=ticket.request.task_label,
                    outcome="failed",
                    error=type(exc).__name__,
                    message=str(exc)[:300],
                )
        finally:
            self._forget(lease)
            self._leases.pop(lease_id, None)
            if requeue:
                # Waiters keep waiting on the same ticket; it simply goes back
                # to the front of the line.
                ticket.granted_at = None
                ticket.task = None
                if ticket not in self._queue:
                    self._queue.insert(0, ticket)
            else:
                if isinstance(exc, CbmcpError):
                    ticket.error = exc
                elif isinstance(exc, Exception):
                    ticket.error = ProviderError(f"could not start a browser: {str(exc)[:300]}")
                else:
                    ticket.error = ProviderError("browser creation was cancelled")
                ticket.event.set()
            await self._free_slot()

    def _forget(self, lease: Lease) -> None:
        if self._by_token.pop(lease.token, None) is not None:
            # Remembered (bounded) only to explain later calls with this token.
            self._gone[lease.token] = (
                lease.lease_id,
                lease.release_reason or "released",
                time.time(),
            )
            while len(self._gone) > 1000:
                self._gone.popitem(last=False)
        if lease.idempotency_key and self._by_idempotency.get(lease.idempotency_key) == lease.lease_id:
            self._by_idempotency.pop(lease.idempotency_key, None)

    # ----- queue inspection ------------------------------------------------

    def _position(self, ticket: Ticket) -> int:
        live = [t for t in self._queue if not t.settled]
        return live.index(ticket) + 1 if ticket in live else 0

    def _ticket_view(self, ticket: Ticket, status: str = "queued") -> dict[str, Any]:
        """Describe an unsettled ticket.

        Two very different situations reach here and must not be conflated:
        waiting for a *slot* (the pool is full), and waiting for a *browser* that
        is already being created. Cloud cold start can take the better part of a
        minute, which routinely exceeds the bounded acquire wait, so telling an
        agent "all slots are busy" in that case would be plainly wrong.
        """
        waited = round(time.time() - ticket.created_at, 1)
        creating = ticket.granted_at is not None
        view: dict[str, Any] = {
            "status": "creating" if creating else "queued",
            "ticket_id": ticket.ticket_id,
            "waited_seconds": waited,
            "expires_in_seconds": round(
                max(0.0, self.s.queue_timeout_seconds - (time.time() - ticket.created_at)), 1
            ),
            "slots": f"{self._slots_used + self._others}/{self.effective_max}",
        }
        if self._others:
            view["slots_held_by_other_windows"] = self._others
        if not creating and self.platform_backoff_seconds > 0:
            view["platform_full"] = (
                "The cloud account is at its concurrent-session limit (other windows or "
                "users hold the rest). Retrying automatically in "
                f"{self.platform_backoff_seconds:.0f}s."
            )
        if creating:
            view["next"] = (
                "Your browser has a slot and is starting now -- cloud cold start usually "
                f"takes 10-60s. Poll browser_queue_status(ticket_id='{ticket.ticket_id}'), "
                "which blocks until it is ready. Do NOT call browser_acquire again: that "
                "would start a second paid session."
            )
        else:
            view["queue_position"] = self._position(ticket)
            view["queue_length"] = len([t for t in self._queue if not t.settled])
            view["next"] = (
                f"All {self.effective_max} browser slots are in use (shared by every "
                "Claude window on this machine). Poll "
                f"browser_queue_status(ticket_id='{ticket.ticket_id}') -- it blocks briefly "
                "and returns the lease as soon as a slot frees. Do NOT call browser_acquire "
                "again: that would queue a second request and could start a second paid "
                "session."
            )
        return view

    def _ready_view(self, lease: Lease) -> dict[str, Any]:
        info: dict[str, Any] = {
            "status": "ready",
            "lease": lease.token,
            **lease.public(include_session=False),
        }
        if lease.session is not None:
            info["pages"] = [h.id for h in lease.session.pages.values() if not h.closed]
            info["active_page"] = lease.session._active_page_id
        info["next"] = (
            "Pass lease=<this token> to every page_*/net_*/js_* call. Call "
            "browser_release(lease) the moment you are done -- the cloud browser bills "
            "for wall-clock time until it is stopped."
        )
        return info

    async def queue_status(self, ticket_id: str, wait_seconds: float | None = None) -> dict[str, Any]:
        ticket = self._tickets.get(ticket_id)
        if ticket is None:
            raise LeaseNotFound(
                f"unknown ticket {ticket_id!r}",
                hint="Tickets are forgotten once claimed. Call browser_acquire for a new browser.",
            )
        if ticket.lease is None and ticket.error is None and not ticket.cancelled:
            budget = wait_seconds if wait_seconds is not None else self.s.acquire_block_seconds
            await self._wait_for_ticket(ticket, max(0.0, min(budget, 60.0)))

        if ticket.lease is not None:
            self._tickets.pop(ticket_id, None)
            return self._ready_view(ticket.lease)
        if ticket.error is not None:
            self._tickets.pop(ticket_id, None)
            raise ticket.error
        if ticket.cancelled:
            self._tickets.pop(ticket_id, None)
            return {"status": "cancelled", "ticket_id": ticket_id}
        return self._ticket_view(ticket)

    async def cancel_ticket(self, ticket_id: str) -> dict[str, Any]:
        ticket = self._tickets.get(ticket_id)
        if ticket is None:
            return {"status": "unknown", "ticket_id": ticket_id}
        if ticket.lease is not None:
            # It was granted while the agent was deciding to give up; releasing is
            # the only correct move, otherwise the browser bills unattended.
            lease = ticket.lease
            self._tickets.pop(ticket_id, None)
            await self.release(lease, reason="ticket_cancelled")
            return {"status": "released", "ticket_id": ticket_id, "note": "granted then released"}
        starting = ticket.granted_at is not None and ticket.error is None
        ticket.cancelled = True
        ticket.event.set()
        self._tickets.pop(ticket_id, None)
        self._queue = [t for t in self._queue if t is not ticket]
        await self.store.bump("queue_cancelled")
        out: dict[str, Any] = {"status": "cancelled", "ticket_id": ticket_id}
        if starting:
            out["note"] = (
                "A browser was already starting for this ticket; it will be stopped "
                "as soon as it comes up."
            )
        return out

    # ----- release ---------------------------------------------------------

    async def release(self, lease: Lease, *, reason: str = "agent_release") -> dict[str, Any]:
        if lease.state == ST_CREATING:
            # Creation is still in flight. Tearing down now would race it: the
            # create task would go on to mark the lease active over our
            # "releasing" and leave a cloud browser nobody tracks. Let it
            # finish (or cancel it if it is wedged), then release the outcome.
            await self._settle_creation(lease)
            if lease.state == ST_CREATING:
                return {"status": "creating", "lease_id": lease.lease_id}
        if lease.state in (ST_RELEASED, ST_FAILED):
            # Double release must be harmless: agents retry, and so does the reaper.
            return {
                "status": "already_released",
                "lease_id": lease.lease_id,
                "release_reason": lease.release_reason,
            }
        if lease.state == ST_RELEASING:
            return {"status": "releasing", "lease_id": lease.lease_id}

        lease.state = ST_RELEASING
        lease.release_reason = reason
        await self.store.update_lease(lease.lease_id, state=ST_RELEASING, release_reason=reason)

        # Let in-flight tool calls finish rather than tearing the browser out
        # from under them; cancel after a bounded grace period.
        deadline = time.time() + self.s.release_grace_seconds
        while lease.inflight > 0 and time.time() < deadline:
            await asyncio.sleep(0.1)
        abandoned = lease.inflight

        browser_seconds = (
            time.time() - (lease.activated_at or lease.created_at) if lease.activated_at else 0.0
        )
        net_stats = lease.session.stats() if lease.session is not None else {}

        if lease.session is not None:
            # Bounded: the cloud stop below is what actually ends the billing, so
            # a wedged local teardown must never delay it.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(lease.session.aclose(), timeout=45)

        stop_accepted = True
        confirmed = False
        cost: float | None = None
        proxy_mb = 0.0
        if lease.platform_session_id:
            stop_accepted = False
            with contextlib.suppress(Exception):
                stop_accepted = await self.provider.stop_session(lease.platform_session_id)
            if not stop_accepted:
                log.warning(
                    "the platform rejected the stop request for session %s",
                    lease.platform_session_id,
                )
            # Confirm the stop rather than assume it: an unconfirmed stop is a
            # session that may still be billing, and the reaper must keep chasing.
            # Measured on Browser Use: the status sometimes still reads "active"
            # right after an accepted stop, then flips within a second or two.
            # Asking only once kept the slot blocked until the next
            # reconciliation, so ask a few times over ~3s first.
            delays = (0.0, 0.5, 1.0, 1.5) if stop_accepted else (0.0,)
            for delay in delays:
                if delay:
                    await asyncio.sleep(delay)
                with contextlib.suppress(Exception):
                    remote = await self.provider.get_session(lease.platform_session_id)
                    if remote is None or remote.status == "stopped":
                        confirmed = True
                    # While a session runs, Browser Use reports the cost reserved
                    # for its whole timeout ($0.0117 for 35 min), not what was
                    # used. Only a stopped session's figure is the real one.
                    if remote is not None and remote.status == "stopped":
                        cost = remote.total_cost_usd or None
                        proxy_mb = remote.proxy_mb
                        if remote.started_at and remote.finished_at:
                            browser_seconds = max(
                                browser_seconds, remote.finished_at - remote.started_at
                            )
                if confirmed:
                    break
            await self.store.upsert_platform_session(
                platform_session_id=lease.platform_session_id,
                provider=self.provider.name,
                state="stopped" if confirmed else "unknown",
                lease_id=lease.lease_id,
                cost_usd=cost or 0.0,
                proxy_mb=proxy_mb,
                duration_seconds=browser_seconds,
                stopped=True,
                stop_confirmed=confirmed,
                note=f"released: {reason}",
            )

        settled = await self.budget.settle(
            lease.lease_id,
            reserved=lease.cost_reserved_usd,
            actual_usd=cost,
            browser_seconds=browser_seconds,
            proxy_mb=proxy_mb,
        )

        lease.state = ST_RELEASED if confirmed else ST_UNKNOWN
        await self.store.update_lease(
            lease.lease_id,
            state=lease.state,
            released_at=time.time(),
            release_reason=reason,
        )
        await self.store.bump("release_total")
        if not confirmed:
            await self.store.bump("release_unconfirmed")
        await self.store.audit(
            "lease.release",
            lease_id=lease.lease_id,
            actor=lease.task_label,
            outcome="ok" if confirmed else "unconfirmed",
            reason=reason,
            seconds=round(browser_seconds, 1),
            cost_usd=round(settled, 6),
            stop_confirmed=confirmed,
        )

        self._forget(lease)
        # The slot is only returned once the platform confirms the stop. An
        # unconfirmed session might still be running, and over-reporting free
        # capacity leads straight to platform 429s.
        if confirmed:
            self._leases.pop(lease.lease_id, None)
            await self._free_slot()
        else:
            log.warning(
                "lease %s released but the platform did not confirm the stop; keeping its "
                "slot until reconciliation settles it",
                lease.lease_id,
            )

        result = {
            "status": "released" if confirmed else "stop_unconfirmed",
            "lease_id": lease.lease_id,
            "browser_seconds": round(browser_seconds, 1),
            "cost_usd": round(settled, 6),
            "stop_confirmed": confirmed,
            "slots": f"{self._slots_used}/{self.effective_max}",
        }
        if abandoned:
            result["abandoned_in_flight_calls"] = abandoned
        if net_stats:
            result["captured"] = {
                "network_entries": net_stats.get("network", {}).get("entries"),
                "hook_records": net_stats.get("hooks", {}).get("records"),
            }
        if not confirmed:
            result["note"] = (
                "The stop command was sent but the platform has not yet reported the "
                "session as stopped. Reconciliation will keep retrying; check "
                "admin_stats for leftovers."
            )
        return result

    async def _settle_creation(self, lease: Lease, timeout: float = 60.0) -> None:
        task = lease.creation_task
        if task is None or task.done() or task is asyncio.current_task():
            return
        done, _ = await asyncio.wait({task}, timeout=timeout)
        if not done:
            log.warning(
                "lease %s still creating after %.0fs; cancelling creation", lease.lease_id, timeout
            )
            task.cancel()
            await asyncio.wait({task}, timeout=30)

    async def release_by_token(self, token: str, reason: str = "agent_release") -> dict[str, Any]:
        try:
            lease = self.resolve_token(token)
        except (LeaseNotFound, LeaseExpired, LeaseNotActive) as exc:
            # Idempotent by design: releasing something already gone is success.
            out: dict[str, Any] = {
                "status": "already_released",
                "note": "No live lease matches that token; nothing to stop.",
            }
            if not isinstance(exc, LeaseNotFound):
                out["note"] = exc.message
            return out
        return await self.release(lease, reason=reason)

    async def stop_all_now(self) -> int:
        """Send the platform stop for every live session at once.

        The shutdown fast path. An MCP client closing a window gives the server
        about two seconds after closing stdin before it kills the process
        (measured with the MCP SDK: 2s, then SIGTERM, then SIGKILL). Releasing
        leases one by one took ~0.6s each against Browser Use, so a window
        holding six browsers left three of them billing. Stopping is the part
        that ends the billing, so it goes out first, all in parallel; each
        stop is also recorded as requested, so if the process dies right after,
        the next gateway keeps chasing it.
        """
        targets = [
            lease
            for lease in self._leases.values()
            if lease.platform_session_id
            and lease.state in (ST_ACTIVE, ST_CREATING, ST_RELEASING, ST_UNKNOWN)
        ]

        async def stop(lease: Lease) -> bool:
            sid = lease.platform_session_id or ""
            accepted = False
            with contextlib.suppress(Exception):
                accepted = await self.provider.stop_session(sid)
            with contextlib.suppress(Exception):
                await self.store.upsert_platform_session(
                    platform_session_id=sid,
                    provider=self.provider.name,
                    state="unknown",
                    lease_id=lease.lease_id,
                    stopped=True,
                    stop_confirmed=False,
                    note="stop sent at gateway shutdown",
                )
            return accepted

        results = await asyncio.gather(*(stop(lease) for lease in targets))
        return sum(1 for ok in results if ok)

    async def release_all(self, reason: str = "shutdown") -> int:
        targets = [
            lease
            for lease in list(self._leases.values())
            if lease.state in (ST_ACTIVE, ST_CREATING, ST_RELEASING)
        ]

        async def release_quietly(lease: Lease) -> None:
            with contextlib.suppress(Exception):
                await self.release(lease, reason=reason)

        # In parallel: one at a time, N leases cost N round trips to the cloud.
        await asyncio.gather(*(release_quietly(lease) for lease in targets))
        return len(targets)

    async def settle_unknown(self, lease_id: str) -> None:
        """Called by reconciliation once a previously unconfirmed stop is proven."""
        lease = self._leases.get(lease_id)
        if lease is None:
            # A lease a dead process left behind holds a slot counted by
            # restore_slots but has no in-memory object; return it here.
            if lease_id in self._restored_slot_ids:
                self._restored_slot_ids.discard(lease_id)
                await self._free_slot()
                log.info("lease %s from a previous run confirmed stopped; slot returned", lease_id)
            return
        if lease.state != ST_UNKNOWN:
            return
        lease.state = ST_RELEASED
        await self.store.update_lease(lease_id, state=ST_RELEASED)
        self._leases.pop(lease_id, None)
        await self._free_slot()
        log.info("lease %s confirmed stopped; slot returned", lease_id)
