"""Crash recovery and orphan reconciliation.

The scenario that matters: the process dies while holding leases. Nothing local
can drive those browsers any more, but the platform keeps billing them, so the
next start must find and stop them.
"""

from __future__ import annotations

import time

from cbmcp.budget import BudgetGuard
from cbmcp.pool import AcquireRequest, BrowserPool
from cbmcp.reaper import Reaper
from cbmcp.store import ST_RELEASED, ST_UNKNOWN, new_lease_token


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
