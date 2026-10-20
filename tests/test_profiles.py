"""Persistent named profiles: created once, one browser at a time, freed on release."""

from __future__ import annotations

import asyncio

import pytest

from cbmcp.errors import InvalidArgument, ProfileInUse
from cbmcp.pool import AcquireRequest


async def test_profile_is_created_once_and_reused(pool):
    first = await pool.acquire(AcquireRequest(profile="main"))
    assert first["status"] == "ready"
    assert first["profile"] == "main"
    assert pool.provider.created[-1].profile_id == "prof1"
    await pool.release_by_token(first["lease"])

    second = await pool.acquire(AcquireRequest(profile="main"))
    assert pool.provider.created[-1].profile_id == "prof1"
    assert len(pool.provider.profiles) == 1
    await pool.release_by_token(second["lease"])


async def test_one_browser_per_profile(pool):
    first = await pool.acquire(AcquireRequest(profile="main"))
    with pytest.raises(ProfileInUse):
        await pool.acquire(AcquireRequest(profile="main"))
    # A different profile, or none, is unaffected.
    other = await pool.acquire(AcquireRequest(profile="other"))
    assert other["status"] == "ready"
    await pool.release_by_token(other["lease"])

    # Released (and the stop confirmed) -> usable again.
    await pool.release_by_token(first["lease"])
    again = await pool.acquire(AcquireRequest(profile="main"))
    assert again["status"] == "ready"
    await pool.release_by_token(again["lease"])


async def test_profile_stays_locked_until_the_stop_is_confirmed(pool):
    """The platform saves a profile when the browser stops; starting the next
    browser on it before that would load stale state."""
    pool.provider.stop_confirms = False
    first = await pool.acquire(AcquireRequest(profile="main"))
    released = await pool.release_by_token(first["lease"])
    assert released["status"] == "stop_unconfirmed"
    with pytest.raises(ProfileInUse):
        await pool.acquire(AcquireRequest(profile="main"))

    lease_id = first["lease_id"]
    pool.provider.sessions["fake1"].status = "stopped"
    await pool.settle_unknown(lease_id)
    pool.provider.stop_confirms = True
    again = await pool.acquire(AcquireRequest(profile="main"))
    assert again["status"] == "ready"


async def test_lock_is_shared_across_gateways(pool, store):
    """Another window's gateway holding the profile blocks this one too."""
    pid = await pool.profile_id_for("main", create=True)
    await store.insert_lease(
        lease_id="lse_otherwindow",
        token="lst_x",
        provider="fake",
        state="active",
        owner=None,
        task_label=None,
        idempotency_key=None,
        scope=[],
        network_mode="direct",
        expires_at=None,
        instance_id="another-gateway",
    )
    assert await store.lock_profile(pid, "lse_otherwindow", "another-gateway", stale_after=600) is None
    with pytest.raises(ProfileInUse) as exc:
        await pool.acquire(AcquireRequest(profile="main"))
    assert exc.value.detail["held_by"] == "lse_otherwindow"

    # That lease ends (released row) -> the stale lock no longer blocks.
    await store.update_lease("lse_otherwindow", state="released")
    ok = await pool.acquire(AcquireRequest(profile="main"))
    assert ok["status"] == "ready"


async def test_default_profile_falls_back_to_a_clean_browser(pool, settings):
    object.__setattr__(settings, "default_profile", "main")
    first = await pool.acquire(AcquireRequest(task_label="a"))
    assert first["profile"] == "main"
    second = await pool.acquire(AcquireRequest(task_label="b"))
    assert second["status"] == "ready"
    assert "profile" not in second
    assert "in use" in second["profile_note"]
    assert pool.provider.created[-1].profile_id is None


async def test_profile_name_is_validated(pool):
    with pytest.raises(InvalidArgument):
        await pool.acquire(AcquireRequest(profile="../etc"))
    with pytest.raises(InvalidArgument):
        await pool.acquire(AcquireRequest(profile="a", profile_id="b"))


async def test_delete_refuses_a_profile_in_use(pool):
    lease = await pool.acquire(AcquireRequest(profile="main"))
    with pytest.raises(ProfileInUse):
        await pool.delete_profile("main")
    await pool.release_by_token(lease["lease"])
    assert (await pool.delete_profile("main"))["status"] == "deleted"
    assert pool.provider.profiles == {}
    assert await pool.list_profiles() == []


async def test_next_browser_waits_for_the_platform_to_save_the_profile(pool, settings):
    """Measured on Browser Use: the profile is written back after the stop is
    reported, and a browser started at once loads the old state."""
    pool.provider.saves_profile_after_stop = True
    object.__setattr__(settings, "profile_save_seconds", 1.5)
    first = await pool.acquire(AcquireRequest(profile="main"))
    released = await pool.release_by_token(first["lease"])
    assert "being saved" in released["profile_note"]
    listing = await pool.list_profiles()
    assert listing[0]["in_use_by"] is None and listing[0]["saving_for_seconds"] > 0

    object.__setattr__(settings, "acquire_block_seconds", 5.0)
    loop = asyncio.get_running_loop()
    started = loop.time()
    again = await pool.acquire(AcquireRequest(profile="main"))
    waited = loop.time() - started
    assert again["status"] == "ready"
    assert waited >= 1.0, f"loaded the profile before its save finished ({waited:.2f}s)"
    await pool.release_by_token(again["lease"])


async def test_lease_id_passed_as_token_gets_a_clear_hint(pool):
    lease = await pool.acquire(AcquireRequest(task_label="x"))
    with pytest.raises(InvalidArgument) as exc:
        pool.resolve_token(lease["lease_id"])
    assert "lst_" in exc.value.hint
    await pool.release_by_token(lease["lease"])
