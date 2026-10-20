"""Crash recovery and orphan reconciliation.

The scenario that matters: the process dies while holding leases. Nothing local
can drive those browsers any more, but the platform keeps billing them, so the
next start must find and stop them.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time

import pytest

from cbmcp.budget import BudgetGuard
from cbmcp.pool import AcquireRequest, BrowserPool
from cbmcp.providers.base import ProviderSession
from cbmcp.reaper import Reaper
from cbmcp.store import ST_ACTIVE, ST_RELEASED, ST_UNKNOWN, new_lease_token


def _reaper(settings, store, pool) -> Reaper:
    return Reaper(
        settings=settings,
        store=store,
        provider=pool.provider,
        pool=pool,
        budget=BudgetGuard(settings, store),
    )


async def test_idle_lease_is_reclaimed(settings, store, pool):
    settings.idle_timeout_seconds = 0.1
    lease = await pool.acquire(AcquireRequest(task_label="abandoned"))
    obj = pool.resolve_token(lease["lease"])
    obj.idle_timeout_seconds = 0.1
    obj.last_activity_at = time.time() - 5  # pretend the agent walked away

    outcome = await _reaper(settings, store, pool).sweep()
    assert outcome["idle"] == 1
    assert pool.stats()["slots_used"] == 0
    assert pool.provider.sessions["fake1"].status == "stopped"


async def test_a_reclaimed_token_says_why_and_how_to_avoid_it(settings, store, pool):
    """Simulation finding: an agent back from a long pause got only 'unknown
    token' and could not learn that browser_renew would have kept its browser."""
    from cbmcp.errors import LeaseExpired, LeaseNotActive

    lease = await pool.acquire(AcquireRequest(task_label="wandered-off"))
    obj = pool.resolve_token(lease["lease"])
    obj.idle_timeout_seconds = 0.1
    obj.last_activity_at = time.time() - 5
    await _reaper(settings, store, pool).sweep()

    with pytest.raises(LeaseExpired) as exc:
        pool.resolve_token(lease["lease"])
    assert "no tool call" in exc.value.message and "browser_renew" in exc.value.hint

    other = await pool.acquire(AcquireRequest(task_label="done"))
    await pool.release_by_token(other["lease"])
    with pytest.raises(LeaseNotActive):
        pool.resolve_token(other["lease"])
    again = await pool.release_by_token(other["lease"])
    assert again["status"] == "already_released"


async def test_max_lifetime_is_enforced_even_while_busy(settings, store, pool):
    lease = await pool.acquire(AcquireRequest(task_label="looping"))
    obj = pool.resolve_token(lease["lease"])
    obj.touch()  # actively used...
    obj.expires_at = time.time() - 1  # ...but past its absolute deadline

    outcome = await _reaper(settings, store, pool).sweep()
    assert outcome["max_age"] == 1
    assert pool.stats()["slots_used"] == 0


async def test_dead_connection_is_reclaimed(settings, store, pool):
    lease = await pool.acquire(AcquireRequest(task_label="broken"))
    obj = pool.resolve_token(lease["lease"])
    obj.session._alive = False

    outcome = await _reaper(settings, store, pool).sweep()
    assert outcome["dead"] == 1
    assert pool.provider.sessions["fake1"].status == "stopped"


async def test_dropped_connection_to_a_running_browser_is_reconnected(settings, store, pool):
    """Reported from real use: connection_lost reclaimed browsers that were
    still running. The reaper now re-attaches before giving up on one."""
    lease = await pool.acquire(AcquireRequest(task_label="flaky link"))
    obj = pool.resolve_token(lease["lease"])
    obj.session._alive = False
    obj.session.reconnectable = True

    reaper = _reaper(settings, store, pool)
    outcome = await reaper.sweep()
    assert outcome["dead"] == 0
    assert reaper.stats["reconnected"] == 1
    assert obj.session.reconnects == 1
    assert pool.provider.sessions["fake1"].status == "active"
    assert pool.resolve_token(lease["lease"]) is obj


async def test_no_reconnect_once_the_cloud_session_has_stopped(settings, store, pool):
    lease = await pool.acquire(AcquireRequest(task_label="gone"))
    obj = pool.resolve_token(lease["lease"])
    obj.session._alive = False
    obj.session.reconnectable = True
    pool.provider.sessions["fake1"].status = "stopped"

    assert await pool.reconnect(obj, why="test") is False
    assert obj.session.reconnects == 0


async def test_lease_nearing_platform_timeout_is_reclaimed_first(settings, store, pool):
    lease = await pool.acquire(AcquireRequest(task_label="expiring"))
    obj = pool.resolve_token(lease["lease"])
    obj.platform_timeout_at = time.time() + 10  # inside the 45s pre-emption window

    outcome = await _reaper(settings, store, pool).sweep()
    assert outcome["platform"] == 1


async def test_reconciliation_stops_a_session_whose_lease_is_dead(settings, store, pool):
    """Our owner tag, but the database says the lease is finished -> orphan."""
    lease_id = "lse_dead"
    await store.insert_lease(
        lease_id=lease_id,
        token=new_lease_token(),
        provider="fake",
        state=ST_RELEASED,
        owner=None,
        task_label=None,
        idempotency_key=None,
        scope=[],
        network_mode="direct",
        expires_at=None,
        instance_id=settings.instance_id,
    )
    session = await pool.provider.create_session(
        type(
            "Spec",
            (),
            {
                "timeout_minutes": 5,
                "network": type("N", (), {"kind": "direct", "custom": None})(),
                "screen_width": None,
                "screen_height": None,
                "solve_captchas": False,
                "pdf_renderer_enabled": True,
                "enable_recording": False,
                "profile_id": None,
                "metadata": {
                    "cbmcp_owner": settings.owner_tag,
                    "cbmcp_instance": settings.instance_id,
                    "cbmcp_lease": lease_id,
                },
            },
        )()
    )

    report = await _reaper(settings, store, pool).reconcile()
    assert session.id in report["orphans_stopped"]
    assert pool.provider.sessions[session.id].status == "stopped"


async def test_reconciliation_leaves_other_accounts_alone(settings, store, pool):
    """A session created by someone else on the same cloud account is untouchable."""
    session = await pool.provider.create_session(
        type(
            "Spec",
            (),
            {
                "timeout_minutes": 5,
                "network": type("N", (), {"kind": "direct", "custom": None})(),
                "screen_width": None,
                "screen_height": None,
                "solve_captchas": False,
                "pdf_renderer_enabled": True,
                "enable_recording": False,
                "profile_id": None,
                "metadata": {"someone": "else"},
            },
        )()
    )

    report = await _reaper(settings, store, pool).reconcile()
    assert session.id not in report["orphans_stopped"]
    assert report["untagged"] == 1
    assert pool.provider.sessions[session.id].status == "active"


async def test_reconciliation_releases_a_lease_whose_session_vanished(settings, store, pool):
    lease = await pool.acquire(AcquireRequest(task_label="vanishing"))
    obj = pool.resolve_token(lease["lease"])
    # The platform stopped it underneath us (its own timeout, or an outage).
    pool.provider.sessions["fake1"].status = "stopped"

    report = await _reaper(settings, store, pool).reconcile()
    assert obj.lease_id in report["vanished"]
    assert pool.stats()["slots_used"] == 0


async def test_startup_marks_previous_run_leases_unknown_and_stops_them(settings, store):
    """A lease from a dead process cannot be driven, so it must be stopped."""
    from .conftest import FakeProvider

    provider = FakeProvider()
    session = await provider.create_session(
        type(
            "Spec",
            (),
            {
                "timeout_minutes": 5,
                "network": type("N", (), {"kind": "direct", "custom": None})(),
                "screen_width": None,
                "screen_height": None,
                "solve_captchas": False,
                "pdf_renderer_enabled": True,
                "enable_recording": False,
                "profile_id": None,
                "metadata": {},
            },
        )()
    )
    lease_id = "lse_from_crashed_run"
    await store.insert_lease(
        lease_id=lease_id,
        token=new_lease_token(),
        provider="fake",
        state="active",
        owner=None,
        task_label="before the crash",
        idempotency_key=None,
        scope=[],
        network_mode="direct",
        expires_at=None,
        instance_id="a-different-process",  # the giveaway
    )
    await store.update_lease(lease_id, platform_session_id=session.id)

    budget = BudgetGuard(settings, store)
    pool = BrowserPool(
        settings=settings, store=store, provider=provider, budget=budget, playwright=None
    )
    reaper = Reaper(
        settings=settings, store=store, provider=provider, pool=pool, budget=budget
    )

    report = await reaper.startup_recovery()
    assert report["stale_leases"] == 1
    assert provider.sessions[session.id].status == "stopped"
    row = await store.get_lease(lease_id)
    assert row is not None
    assert row.state in (ST_RELEASED, ST_UNKNOWN)


async def test_create_intent_is_written_before_the_provider_call(settings, store, pool):
    """So a crash mid-creation still leaves a trail to follow."""
    pool.provider.fail_create = True
    try:
        await pool.acquire(AcquireRequest(task_label="crashy"))
    except Exception:
        pass
    # The intent exists and was resolved with the failure noted.
    cur = await store.db.execute("SELECT * FROM create_intents")
    rows = list(await cur.fetchall())
    assert len(rows) == 1
    assert rows[0]["resolved"] == 1
    assert "refused" in (rows[0]["note"] or "")


def _spec(metadata: dict) -> object:
    return type(
        "Spec",
        (),
        {
            "timeout_minutes": 5,
            "network": type("N", (), {"kind": "direct", "custom": None})(),
            "screen_width": None,
            "screen_height": None,
            "solve_captchas": False,
            "pdf_renderer_enabled": True,
            "enable_recording": False,
            "profile_id": None,
            "metadata": metadata,
        },
    )()


async def test_reconcile_never_stops_a_session_that_is_in_use(settings, store, pool):
    """Regression: every live session also has stop_confirmed=0, and the
    unconfirmed-stop sweep used to "retry the stop" on all of them -- killing
    every agent's browser on the first reconcile after acquire."""
    lease = await pool.acquire(AcquireRequest(task_label="busy"))
    obj = pool.resolve_token(lease["lease"])

    report = await _reaper(settings, store, pool).reconcile()

    assert pool.provider.stopped == [], "a browser in use must never be stopped"
    assert pool.provider.sessions["fake1"].status == "active"
    assert obj.state == ST_ACTIVE
    assert report["orphans_stopped"] == []
    assert report["vanished"] == []


async def test_reconcile_skips_a_session_still_being_created(settings, store, pool):
    """The platform can list a session before create_session has returned."""
    pool.provider.create_delay = 0.3
    settings.acquire_block_seconds = 0.0
    ticket = await pool.acquire(AcquireRequest(task_label="cold-start"))
    assert ticket["status"] == "creating"
    await asyncio.sleep(0.05)  # the create call is now in flight
    # Simulate the platform already listing it, tagged with our lease.
    creating = next(iter(pool._leases.values()))
    pool.provider.sessions["early"] = ProviderSession(
        id="early",
        cdp_url="ws://x",
        status="active",
        metadata={
            "cbmcp_owner": settings.owner_tag,
            "cbmcp_instance": settings.instance_id,
            "cbmcp_lease": creating.lease_id,
        },
    )

    task = creating.creation_task
    report = await _reaper(settings, store, pool).reconcile()
    assert "early" not in report["orphans_stopped"]
    assert "early" not in pool.provider.stopped
    if task is not None:
        await asyncio.wait({task}, timeout=5)


async def test_startup_leaves_a_running_sibling_gateways_leases_alone(settings, store):
    """Two MCP client windows = two gateways on one database. Starting the
    second must not stop the first one's browsers."""
    from .conftest import FakeProvider

    provider = FakeProvider()
    session = await provider.create_session(_spec({}))
    await store.heartbeat("sibling", pid=os.getpid(), host=socket.gethostname())
    await store.insert_lease(
        lease_id="lse_sibling",
        token=new_lease_token(),
        provider="fake",
        state="active",
        owner=None,
        task_label="someone else's agent",
        idempotency_key=None,
        scope=[],
        network_mode="direct",
        expires_at=None,
        instance_id="sibling",
    )
    await store.update_lease("lse_sibling", platform_session_id=session.id)

    budget = BudgetGuard(settings, store)
    pool = BrowserPool(
        settings=settings, store=store, provider=provider, budget=budget, playwright=None
    )
    reaper = Reaper(settings=settings, store=store, provider=provider, pool=pool, budget=budget)

    report = await reaper.startup_recovery()
    assert report["stale_leases"] == 0
    assert provider.sessions[session.id].status == "active"
    assert pool.stats()["slots_used"] == 0, "a sibling's lease is not our slot"
    row = await store.get_lease("lse_sibling")
    assert row is not None and row.state == ST_ACTIVE


async def test_slot_restored_from_a_dead_run_is_returned_once_stopped(settings, store):
    from .conftest import FakeProvider

    provider = FakeProvider(stop_accepts=False)  # the API is down at startup
    lease_id = "lse_left_behind"
    session = await provider.create_session(
        _spec(
            {
                "cbmcp_owner": settings.owner_tag,
                "cbmcp_instance": "dead-process",
                "cbmcp_lease": lease_id,
            }
        )
    )
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
        instance_id="dead-process",
    )
    await store.update_lease(lease_id, platform_session_id=session.id)

    budget = BudgetGuard(settings, store)
    pool = BrowserPool(
        settings=settings, store=store, provider=provider, budget=budget, playwright=None
    )
    reaper = Reaper(settings=settings, store=store, provider=provider, pool=pool, budget=budget)
    await reaper.startup_recovery()
    assert pool.stats()["slots_used"] == 1, "an unconfirmed stop keeps its slot"

    provider.stop_accepts = True  # the API is back
    await reaper.reconcile()
    assert provider.sessions[session.id].status == "stopped"
    assert pool.stats()["slots_used"] == 0, "the restored slot must come back"


async def test_a_crashed_gateways_slot_claims_are_freed_at_once(settings, store):
    """Same machine, process gone: its claims must not block capacity for the
    minute it takes the heartbeat to age out."""
    import subprocess
    import sys

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    await store.heartbeat("crashed", pid=proc.pid, host=socket.gethostname())
    await store.add_claims("crashed", 2)
    assert await store.claims_elsewhere(settings.instance_id, 60) == 2

    live = await store.live_instance_ids(60, host=socket.gethostname())
    assert "crashed" not in live
    assert await store.claims_elsewhere(settings.instance_id, 60) == 0


def test_pid_check_tells_a_dead_gateway_from_a_live_one():
    """How a new window knows at once that a crashed window's gateway is gone."""
    import subprocess
    import sys

    from cbmcp.store import _pid_alive

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert _pid_alive(os.getpid()) is True
    assert _pid_alive(proc.pid) is False


async def test_audit_trail_records_lifecycle_without_secrets(settings, store, pool):
    lease = await pool.acquire(AcquireRequest(task_label="audited"))
    token = lease["lease"]
    await pool.release_by_token(token)

    records = await store.recent_audit(20)
    actions = {r["action"] for r in records}
    assert "lease.acquire" in actions
    assert "lease.release" in actions
    blob = str(records)
    assert token not in blob, "the capability token must never be written to the audit log"
