"""Concurrency, queueing, idempotency and release accounting.

These are the behaviours that cost real money when they are wrong, so they are
tested without a cloud account via the fake provider.
"""

from __future__ import annotations

import asyncio

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


async def test_fresh_provider_fixture_is_isolated():
    provider = FakeProvider()
    assert await provider.list_sessions() == []
