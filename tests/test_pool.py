"""Concurrency, queueing, idempotency and release accounting.

These are the behaviours that cost real money when they are wrong, so they are
tested without a cloud account via the fake provider.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from cbmcp.errors import (
    BudgetExceeded,
    CapacityExhausted,
    LeaseNotFound,
    ProviderError,
)
from cbmcp.pool import AcquireRequest
from cbmcp.store import ST_ACTIVE

from .conftest import FakeProvider


async def test_acquire_is_dynamic_and_returns_to_zero(pool):
    assert pool.stats()["slots_used"] == 0

    first = await pool.acquire(AcquireRequest(task_label="a"))
    assert first["status"] == "ready"
    assert pool.stats()["slots_used"] == 1

    released = await pool.release_by_token(first["lease"])
    assert released["status"] == "released"
    assert released["stop_confirmed"] is True
    # The whole point of the design: idle means zero running sessions.
    assert pool.stats()["slots_used"] == 0
    assert pool.provider.stopped == ["fake1"]


async def test_concurrency_ceiling_is_enforced(pool):
    a = await pool.acquire(AcquireRequest(task_label="a"))
    b = await pool.acquire(AcquireRequest(task_label="b"))
    assert a["status"] == "ready" and b["status"] == "ready"
    assert pool.stats()["slots_used"] == 2

    # max_concurrency is 2, so this one must queue rather than create a third.
    third = await pool.acquire(AcquireRequest(task_label="c"))
    assert third["status"] == "queued"
    assert third["queue_position"] == 1
    assert len(pool.provider.sessions) == 2, "no third cloud session may be created"


async def test_queue_is_served_on_release_in_fifo_order(pool):
    a = await pool.acquire(AcquireRequest(task_label="a"))
    await pool.acquire(AcquireRequest(task_label="b"))

    first_waiter = await pool.acquire(AcquireRequest(task_label="c"))
    second_waiter = await pool.acquire(AcquireRequest(task_label="d"))
    assert first_waiter["status"] == "queued"
    assert second_waiter["status"] == "queued"
    assert second_waiter["queue_position"] == 2

    await pool.release_by_token(a["lease"])

    # The freed slot goes to the longest waiter, and fulfilment happens in the
    # background so the lease is ready when the agent polls.
    granted = await pool.queue_status(first_waiter["ticket_id"], wait_seconds=5)
    assert granted["status"] == "ready"
    assert granted["task_label"] == "c"

    still_waiting = await pool.queue_status(second_waiter["ticket_id"], wait_seconds=0.2)
    assert still_waiting["status"] == "queued"


async def test_platform_limit_clamps_configured_maximum(pool):
    pool.provider.concurrent_limit = 1
    await pool.refresh_platform_limit()
    assert pool.effective_max == 1, "the platform quota must win over local config"

    first = await pool.acquire(AcquireRequest(task_label="a"))
    assert first["status"] == "ready"
    second = await pool.acquire(AcquireRequest(task_label="b"))
    assert second["status"] == "queued"


async def test_idempotency_key_prevents_a_second_paid_session(pool):
    first = await pool.acquire(AcquireRequest(task_label="a", idempotency_key="unit-1"))
    second = await pool.acquire(AcquireRequest(task_label="a", idempotency_key="unit-1"))

    assert second["status"] == "ready"
    assert second.get("idempotent_hit") is True
    assert second["lease"] == first["lease"]
    assert len(pool.provider.sessions) == 1, "a retry must not rent a second browser"
    assert pool.stats()["slots_used"] == 1


async def test_concurrent_acquires_never_exceed_the_ceiling(pool):
    """Ten simultaneous subagents, two slots: no race may overshoot."""
    pool.provider.create_delay = 0.05
    results = await asyncio.gather(
        *[pool.acquire(AcquireRequest(task_label=f"t{i}")) for i in range(10)]
    )
    ready = [r for r in results if r["status"] == "ready"]
    queued = [r for r in results if r["status"] == "queued"]

    assert len(ready) == 2
    assert len(queued) == 8
    assert len(pool.provider.sessions) == 2
    assert pool.stats()["slots_used"] == 2


async def test_release_is_idempotent(pool):
    lease = await pool.acquire(AcquireRequest(task_label="a"))
    token = lease["lease"]

    first = await pool.release_by_token(token)
    assert first["status"] == "released"

    # Agents retry, and so does the reaper. Neither may raise or double-charge.
    second = await pool.release_by_token(token)
    assert second["status"] == "already_released"
    assert pool.provider.stopped.count("fake1") == 1


async def test_unknown_token_is_rejected(pool):
    await pool.acquire(AcquireRequest(task_label="a"))
    with pytest.raises(LeaseNotFound):
        pool.resolve_token("lst_not-a-real-token")


async def test_leases_are_isolated_by_token(pool):
    a = await pool.acquire(AcquireRequest(task_label="a"))
    b = await pool.acquire(AcquireRequest(task_label="b"))

    lease_a = pool.resolve_token(a["lease"])
    lease_b = pool.resolve_token(b["lease"])
    assert lease_a.lease_id != lease_b.lease_id
    assert lease_a.platform_session_id != lease_b.platform_session_id
    assert lease_a.session is not lease_b.session


async def test_failed_creation_frees_the_slot_and_stops_nothing_behind(pool):
    pool.provider.fail_create = True
    with pytest.raises(ProviderError):
        await pool.acquire(AcquireRequest(task_label="doomed"))

    assert pool.stats()["slots_used"] == 0, "a failed acquire must not leak a slot"

    pool.provider.fail_create = False
    recovered = await pool.acquire(AcquireRequest(task_label="ok"))
    assert recovered["status"] == "ready"


async def test_unconfirmed_stop_keeps_its_slot(pool):
    """A session we cannot prove is stopped may still bill, so it keeps the slot."""
    pool.provider.stop_confirms = False
    lease = await pool.acquire(AcquireRequest(task_label="a"))

    result = await pool.release_by_token(lease["lease"])
    assert result["status"] == "stop_unconfirmed"
    assert result["stop_confirmed"] is False
    assert pool.stats()["slots_used"] == 1, "capacity must not be over-reported"

    # Once reconciliation proves it stopped, the slot comes back.
    lease_id = result["lease_id"]
    await pool.settle_unknown(lease_id)
    assert pool.stats()["slots_used"] == 0


async def test_cancelling_a_granted_ticket_releases_the_browser(pool):
    a = await pool.acquire(AcquireRequest(task_label="a"))
    await pool.acquire(AcquireRequest(task_label="b"))
    waiter = await pool.acquire(AcquireRequest(task_label="c"))
    assert waiter["status"] == "queued"

    await pool.release_by_token(a["lease"])
    await asyncio.sleep(0.2)  # let the pump fulfil the ticket

    cancelled = await pool.cancel_ticket(waiter["ticket_id"])
    # It had already been granted, so it must be released rather than abandoned.
    assert cancelled["status"] in ("released", "cancelled")
    if cancelled["status"] == "released":
        assert pool.stats()["slots_used"] == 1


async def test_budget_rejects_before_creating_a_session(pool, settings):
    settings.budget_daily_usd = 0.0001  # below one lease's worst case
    with pytest.raises(BudgetExceeded):
        await pool.acquire(AcquireRequest(task_label="too-expensive"))
    assert len(pool.provider.sessions) == 0, "no paid session may be created"
    assert pool.stats()["slots_used"] == 0


async def test_platform_metadata_tags_every_session_for_reconciliation(pool, settings):
    await pool.acquire(AcquireRequest(task_label="a"))
    spec = pool.provider.created[0]
    assert spec.metadata["cbmcp_owner"] == settings.owner_tag
    assert spec.metadata["cbmcp_instance"] == settings.instance_id
    assert spec.metadata["cbmcp_lease"].startswith("lse_")
    # The platform-side hard stop is the cost guarantee that survives a crash.
    assert spec.timeout_minutes == settings.platform_timeout_minutes


async def test_direct_network_mode_disables_the_billed_proxy(pool):
    await pool.acquire(AcquireRequest(task_label="a"))
    assert pool.provider.created[0].network.kind == "direct"


async def test_lease_state_is_persisted(pool, store):
    lease = await pool.acquire(AcquireRequest(task_label="persisted"))
    lease_id = pool.resolve_token(lease["lease"]).lease_id

    row = await store.get_lease(lease_id)
    assert row is not None
    assert row.state == ST_ACTIVE
    assert row.task_label == "persisted"
    assert row.platform_session_id == "fake1"
    # The capability token itself must never be stored.
    assert lease["lease"] not in str(row.__dict__)


async def test_release_all_stops_everything(pool):
    await pool.acquire(AcquireRequest(task_label="a"))
    await pool.acquire(AcquireRequest(task_label="b"))
    count = await pool.release_all(reason="test")
    assert count == 2
    assert pool.stats()["slots_used"] == 0
    assert len(pool.provider.stopped) == 2


async def test_queue_timeout_surfaces_as_an_error(pool, settings):
    settings.queue_timeout_seconds = 0.3
    await pool.acquire(AcquireRequest(task_label="a"))
    await pool.acquire(AcquireRequest(task_label="b"))
    waiter = await pool.acquire(AcquireRequest(task_label="c"))
    assert waiter["status"] == "queued"

    await asyncio.sleep(0.5)
    # maintain() is what the reaper calls every sweep. _pump alone only runs
    # when a slot is freed, so a full pool with no releases would otherwise
    # never time this ticket out.
    housekeeping = await pool.maintain()
    assert housekeeping["expired_tickets"] == 1

    with pytest.raises(CapacityExhausted) as exc:
        await pool.queue_status(waiter["ticket_id"], wait_seconds=0.1)
    assert "queued for over" in str(exc.value)


async def test_release_during_creation_stops_the_browser_it_produces(pool, settings):
    """Releasing a lease mid-creation used to race the create task, which then
    marked the lease active and left a cloud browser nobody tracked."""
    pool.provider.create_delay = 0.3
    settings.acquire_block_seconds = 0.0
    ticket = await pool.acquire(AcquireRequest(task_label="impatient"))
    assert ticket["status"] == "creating"
    await asyncio.sleep(0.05)
    lease = next(iter(pool._leases.values()))
    assert lease.state == "creating"

    result = await pool.release(lease, reason="test")
    assert result["status"] == "released"
    assert pool.provider.stopped == ["fake1"]
    assert pool.stats()["slots_used"] == 0


async def test_ticket_ids_are_unguessable(pool):
    """A ticket is redeemable for the lease token, so it is a capability too."""
    await pool.acquire(AcquireRequest(task_label="a"))
    await pool.acquire(AcquireRequest(task_label="b"))
    queued = await pool.acquire(AcquireRequest(task_label="c"))
    ticket_id = queued["ticket_id"]
    assert ticket_id.startswith("tkt_")
    assert len(ticket_id) >= 4 + 20


async def test_cancelled_creation_frees_the_slot(pool, settings):
    """Shutdown cancels in-flight creation; that must not leak the slot."""
    pool.provider.create_delay = 5.0
    settings.acquire_block_seconds = 0.0
    await pool.acquire(AcquireRequest(task_label="cancelled"))
    await asyncio.sleep(0.05)
    lease = next(iter(pool._leases.values()))
    task = lease.creation_task
    assert task is not None
    task.cancel()
    await asyncio.wait({task}, timeout=5)
    assert pool.stats()["slots_used"] == 0
    assert lease.state == "failed"


async def test_slot_ceiling_is_shared_with_other_live_windows(pool, store):
    """Every Claude window runs its own gateway; max_concurrency (2 here) is
    one ceiling for all of them, not 2 per window."""
    await store.heartbeat("other-window", pid=os.getpid(), host="elsewhere")
    await store.add_claims("other-window", 2)  # that window holds two browsers

    result = await pool.acquire(AcquireRequest(task_label="third"))
    assert result["status"] == "queued"
    assert result.get("slots_held_by_other_windows") == 2
    assert len(pool.provider.sessions) == 0, "the shared ceiling is already reached"


async def test_a_dead_windows_leftovers_do_not_block_capacity(pool, store):
    await store.add_claims("crashed-window", 2)  # no heartbeat: that gateway is gone
    result = await pool.acquire(AcquireRequest(task_label="a"))
    assert result["status"] == "ready"


async def test_two_gateways_racing_never_exceed_the_shared_ceiling(settings, store):
    """Measured on Browser Use: ten requests from three windows at the same
    instant all got through a ceiling of six. Two gateways, separate database
    connections, twelve simultaneous acquires, ceiling 2."""
    from cbmcp.budget import BudgetGuard
    from cbmcp.pool import BrowserPool
    from cbmcp.store import Store

    from .conftest import FakeProvider, StubSession

    other_settings = settings.model_copy(update={"instance_id": "gateway-b"})
    other_store = Store(settings.db_path)
    await other_store.open()
    try:
        await store.heartbeat(settings.instance_id, pid=os.getpid(), host="h")
        await other_store.heartbeat("gateway-b", pid=os.getpid(), host="h")
        pools = []
        for s, st in ((settings, store), (other_settings, other_store)):
            p = BrowserPool(settings=s, store=st, provider=FakeProvider(create_delay=0.2),
                            budget=BudgetGuard(s, st), playwright=None)
            pools.append(p)
        import cbmcp.pool as pool_module

        original = pool_module.BrowserSession
        pool_module.BrowserSession = StubSession
        try:
            results = await asyncio.gather(*[
                pools[i % 2].acquire(AcquireRequest(task_label=f"t{i}")) for i in range(12)
            ])
        finally:
            pool_module.BrowserSession = original
        created = sum(len(p.provider.sessions) for p in pools)
        ready = sum(1 for r in results if r["status"] == "ready")
        assert created == 2 and ready == 2, f"{created} browsers created for a ceiling of 2"
    finally:
        await other_store.close()


async def test_platform_concurrency_limit_requeues_instead_of_failing(pool):
    """Account full (other windows hold the rest): wait in line, don't fail."""
    pool.provider.concurrency_rejections = 1
    first = await pool.acquire(AcquireRequest(task_label="a"))
    assert first["status"] == "queued"
    assert "platform_full" in first
    assert pool.stats()["slots_used"] == 0

    pool._platform_backoff_until = 0.0  # the other window released a browser
    await pool.maintain()
    ready = await pool.queue_status(first["ticket_id"], wait_seconds=5)
    assert ready["status"] == "ready"


async def test_cancelling_while_the_browser_starts_stops_it_once_up(pool, settings):
    pool.provider.create_delay = 0.3
    settings.acquire_block_seconds = 0.0
    ticket = await pool.acquire(AcquireRequest(task_label="changed-my-mind"))
    assert ticket["status"] == "creating"

    out = await pool.cancel_ticket(ticket["ticket_id"])
    assert out["status"] == "cancelled"
    # The release runs in the background and returns the slot only after the
    # stop is confirmed and settled, so wait for the end state, not the stop.
    for _ in range(80):
        if pool.provider.stopped and pool.stats()["slots_used"] == 0:
            break
        await asyncio.sleep(0.05)
    assert pool.provider.stopped == ["fake1"], "the browser nobody will claim is stopped"
    assert pool.stats()["slots_used"] == 0


async def test_shutdown_sends_every_stop_at_once(pool, store):
    """Closing a window leaves ~2s before the MCP client kills the gateway;
    the stops (what ends the billing) must all go out first, together."""
    await pool.acquire(AcquireRequest(task_label="a"))
    await pool.acquire(AcquireRequest(task_label="b"))
    stopped = await pool.stop_all_now()
    assert stopped == 2
    assert sorted(pool.provider.stopped) == ["fake1", "fake2"]
    # Recorded as requested, so a gateway that dies right now is still chased.
    assert len(await store.unconfirmed_stops()) == 2

    assert await pool.release_all(reason="shutdown") == 2
    assert pool.stats()["slots_used"] == 0


async def test_retry_while_starting_returns_the_ticket_not_an_unusable_token(pool, settings):
    """A retry during cold start used to hand back a token whose page tools
    then said 'acquire a fresh one' -- i.e. buy a second browser."""
    pool.provider.create_delay = 0.4
    settings.acquire_block_seconds = 0.0
    first = await pool.acquire(AcquireRequest(task_label="a", idempotency_key="k"))
    assert first["status"] == "creating"
    await asyncio.sleep(0.05)

    retry = await pool.acquire(AcquireRequest(task_label="a", idempotency_key="k"))
    assert retry["status"] == "creating"
    assert retry["idempotent_hit"] is True
    assert "lease" not in retry, "no token until the browser can be driven"
    assert retry.get("ticket_id") == first["ticket_id"]

    ready = await pool.queue_status(first["ticket_id"], wait_seconds=5)
    assert ready["status"] == "ready"
    again = await pool.acquire(AcquireRequest(task_label="a", idempotency_key="k"))
    assert again["status"] == "ready" and again["lease"] == ready["lease"]
    assert len(pool.provider.sessions) == 1, "still exactly one paid browser"


async def test_a_starting_lease_says_wait_not_acquire_again(pool, settings):
    from cbmcp.errors import LeaseNotActive

    pool.provider.create_delay = 0.4
    settings.acquire_block_seconds = 0.0
    await pool.acquire(AcquireRequest(task_label="a"))
    await asyncio.sleep(0.05)
    lease = next(iter(pool._leases.values()))
    with pytest.raises(LeaseNotActive) as exc:
        async with lease.guard():
            pass
    assert "Do NOT call browser_acquire" in exc.value.hint
    await asyncio.wait({lease.creation_task}, timeout=5)


async def test_slot_freed_in_another_window_is_picked_up_quickly(pool, store, settings):
    """No event reaches this gateway when another window releases; the waiter
    re-checks the shared count itself instead of waiting for a reaper sweep."""
    await store.heartbeat("other-window", pid=os.getpid(), host="elsewhere")
    claims = await store.add_claims("other-window", 2)
    queued = await pool.acquire(AcquireRequest(task_label="waiting"))
    assert queued["status"] == "queued"

    async def other_window_releases() -> None:
        await asyncio.sleep(0.3)
        await store.release_claim(claims[0])

    asyncio.create_task(other_window_releases())
    started = asyncio.get_running_loop().time()
    ready = await pool.queue_status(queued["ticket_id"], wait_seconds=10)
    took = asyncio.get_running_loop().time() - started
    assert ready["status"] == "ready"
    assert took < 5, f"handoff took {took:.1f}s; it used to wait for a 15s sweep"


async def test_parallel_releases_hand_slots_to_the_queue_cleanly(pool):
    """release_all runs in parallel now; concurrent pumps must not double-take."""
    a = await pool.acquire(AcquireRequest(task_label="a"))
    b = await pool.acquire(AcquireRequest(task_label="b"))
    q1 = await pool.acquire(AcquireRequest(task_label="q1"))
    q2 = await pool.acquire(AcquireRequest(task_label="q2"))
    assert q1["status"] == q2["status"] == "queued"

    await asyncio.gather(pool.release_by_token(a["lease"]), pool.release_by_token(b["lease"]))
    r1 = await pool.queue_status(q1["ticket_id"], wait_seconds=5)
    r2 = await pool.queue_status(q2["ticket_id"], wait_seconds=5)
    assert r1["status"] == r2["status"] == "ready"
    assert pool.stats()["slots_used"] == 2
    assert len(pool.provider.sessions) == 4


async def test_a_stop_the_platform_confirms_a_moment_later_frees_the_slot_at_once(pool):
    """Measured on Browser Use: right after an accepted stop the status can still
    read 'active'. One look used to park the slot until the next reconcile."""
    pool.provider.stop_lag_polls = 2
    lease = await pool.acquire(AcquireRequest(task_label="a"))
    result = await pool.release_by_token(lease["lease"])
    assert result["status"] == "released"
    assert result["stop_confirmed"] is True
    assert pool.stats()["slots_used"] == 0


async def test_an_unconfirmed_stop_is_chased_every_sweep(settings, store, pool):
    from cbmcp.budget import BudgetGuard
    from cbmcp.reaper import Reaper

    pool.provider.stop_confirms = False
    lease = await pool.acquire(AcquireRequest(task_label="a"))
    result = await pool.release_by_token(lease["lease"])
    assert result["status"] == "stop_unconfirmed"
    assert pool.stats()["slots_used"] == 1

    pool.provider.sessions["fake1"].status = "stopped"  # the platform catches up
    reaper = Reaper(settings=settings, store=store, provider=pool.provider, pool=pool,
                    budget=BudgetGuard(settings, store))
    await reaper.sweep()  # a plain sweep, not a full reconciliation
    assert pool.stats()["slots_used"] == 0


def test_an_http_error_page_is_flagged():
    from cbmcp.tools.page_tools import _flag_http_error

    state: dict = {"ok": True}
    _flag_http_error(state, 429)
    assert state["http_error"] == 429 and "block page" in state["warning"]
    fine: dict = {"ok": True}
    _flag_http_error(fine, 200)
    assert "warning" not in fine


async def test_fresh_provider_fixture_is_isolated():
    provider = FakeProvider()
    assert await provider.list_sessions() == []
