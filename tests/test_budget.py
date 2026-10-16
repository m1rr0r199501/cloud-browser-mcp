"""Budget reservation under concurrency.

The naive "sum what we spent, compare to the cap" check passes for every one of
N simultaneous acquires. These tests pin the reservation behaviour that fixes it.
"""

from __future__ import annotations

import pytest

from cbmcp.budget import BudgetGuard
from cbmcp.errors import BudgetExceeded
from cbmcp.providers.base import NetworkSpec
from cbmcp.store import new_lease_token


async def test_worst_case_uses_the_full_platform_timeout(settings, store):
    guard = BudgetGuard(settings, store)
    # 5 minutes at $0.02/hour
    expected = (5 * 60) * (0.02 / 3600)
    assert guard.worst_case_cost(5, NetworkSpec(kind="direct")) == pytest.approx(expected)


async def test_proxy_modes_reserve_more_than_direct(settings, store):
    guard = BudgetGuard(settings, store)
    direct = guard.worst_case_cost(5, NetworkSpec(kind="direct"))
    proxied = guard.worst_case_cost(5, NetworkSpec(kind="platform_proxy", country_code="us"))
    assert proxied > direct, "a billed proxy must raise the reservation"


async def test_outstanding_reservations_block_further_spend(settings, store):
    """The core fix: committed-but-unsettled money counts against the cap."""
    settings.budget_daily_usd = 0.01
    guard = BudgetGuard(settings, store)
    network = NetworkSpec(kind="direct")
    reserve = guard.worst_case_cost(settings.platform_timeout_minutes, network)

    # Fill the daily cap with reservations from "in-flight" leases.
    needed = int(settings.budget_daily_usd / reserve) + 1
    for index in range(needed):
        lease_id = f"lse_test{index}"
        await store.insert_lease(
            lease_id=lease_id,
            token=new_lease_token(),
            provider="fake",
            state="active",
            owner=None,
            task_label=None,
            idempotency_key=None,
            scope=[],
            network_mode="direct",
            expires_at=None,
            instance_id=settings.instance_id,
        )
        await store.update_lease(lease_id, cost_reserved_usd=reserve)

    assert await store.reserved_total() == pytest.approx(reserve * needed)
    with pytest.raises(BudgetExceeded):
        await guard.require(settings.platform_timeout_minutes, network)


async def test_settlement_replaces_the_reservation(settings, store):
    guard = BudgetGuard(settings, store)
    lease_id = "lse_settle"
    await store.insert_lease(
        lease_id=lease_id,
        token=new_lease_token(),
        provider="fake",
        state="active",
        owner=None,
        task_label=None,
        idempotency_key=None,
        scope=[],
        network_mode="direct",
        expires_at=None,
        instance_id=settings.instance_id,
    )
    await store.update_lease(lease_id, cost_reserved_usd=0.01)

    settled = await guard.settle(
        lease_id, reserved=0.01, actual_usd=0.004, browser_seconds=60, proxy_mb=0
    )
    assert settled == pytest.approx(0.004)

    usage = await store.usage_for_day()
    assert usage["cost_usd"] == pytest.approx(0.004)
    assert usage["browser_seconds"] == pytest.approx(60)
    # Reservation released: it no longer counts as outstanding commitment.
    assert await store.reserved_total() == pytest.approx(0.0)


async def test_settlement_falls_back_to_an_estimate(settings, store):
    """A provider that has not reported cost yet must not understate the ledger."""
    guard = BudgetGuard(settings, store)
    lease_id = "lse_estimate"
    await store.insert_lease(
        lease_id=lease_id,
        token=new_lease_token(),
        provider="fake",
        state="released",
        owner=None,
        task_label=None,
        idempotency_key=None,
        scope=[],
        network_mode="direct",
        expires_at=None,
        instance_id=settings.instance_id,
    )
    settled = await guard.settle(
        lease_id, reserved=0.01, actual_usd=None, browser_seconds=3600, proxy_mb=0
    )
    assert settled == pytest.approx(settings.price_browser_usd_per_hour)


async def test_enforcement_can_be_disabled(settings, store):
    settings.budget_daily_usd = 0.0000001
    settings.budget_enforce = False
    guard = BudgetGuard(settings, store)
    decision = await guard.require(5, NetworkSpec(kind="direct"))
    assert decision.allowed is True
