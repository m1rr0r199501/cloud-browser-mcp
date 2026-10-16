#!/usr/bin/env python
"""Acceptance test: many subagents, one MCP connection, isolated cloud browsers.

This reproduces the real topology rather than a convenient one. Claude Code
subagents share their parent's MCP connection, so the server sees interleaved
tool calls on a single stdio transport and has nothing but the lease token to
tell them apart. That is exactly what this drives.

    python scripts/concurrency_test.py [--agents 5] [--max-concurrency 3]

With more agents than slots, some must queue -- which is the point: the queue,
the slot ceiling and per-lease isolation are all exercised together. Every lease
is released in a finally block, and the run ends by asserting the cloud account
has nothing left active.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.client.stdio import get_default_environment

ROOT = Path(__file__).resolve().parents[1]
URL = "https://example.com/"


def unwrap(result: Any) -> dict:
    """MCP tool results come back as content blocks; tools here return JSON."""
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except ValueError:
                return {"_text": text}
    return {}


class Agent:
    """One simulated subagent: rent a browser, use it, prove isolation, release."""

    def __init__(self, session: ClientSession, index: int) -> None:
        self.s = session
        self.i = index
        self.name = f"agent{index}"
        self.secret = f"secret-{index}-{int(time.time()) % 100000}"
        self.lease: str | None = None
        self.log: list[str] = []
        self.queued = False
        self.waited = 0.0
        self.error: str | None = None

    def note(self, message: str) -> None:
        self.log.append(message)
        print(f"  [{self.name}] {message}")

    async def call(self, tool: str, **args: Any) -> dict:
        return unwrap(await self.s.call_tool(tool, args))

    async def acquire(self) -> bool:
        got = await self.call(
            "browser_acquire", task_label=self.name, idempotency_key=self.name
        )
        waited = 0.0
        while got.get("status") in ("queued", "creating"):
            self.queued = self.queued or got["status"] == "queued"
            self.note(f"{got['status']} (slots {got.get('slots')}), polling")
            started = time.time()
            got = await self.call(
                "browser_queue_status", ticket_id=got["ticket_id"], wait_seconds=30
            )
            waited += time.time() - started
            self.waited = waited
            if waited > 300:
                self.error = "gave up waiting"
                return False
        if got.get("status") != "ready":
            self.error = f"acquire failed: {got.get('error')} {got.get('message')}"
            return False
        self.lease = got["lease"]
        self.note(f"got a browser after {waited:.0f}s of waiting")
        return True

    async def work(self) -> bool:
        """Do what a security researcher would do, then prove nobody else is here."""
        assert self.lease
        nav = await self.call("page_navigate", lease=self.lease, url=URL)
        if not nav.get("ok"):
            self.error = f"navigate failed: {nav}"
            return False
        self.note(f"navigated: {nav.get('title')!r}")

        snap = await self.call("page_snapshot", lease=self.lease)
        if not snap.get("ok"):
            self.error = f"snapshot failed: {snap}"
            return False
        self.note(f"snapshot: {snap.get('node_count')} nodes")

        # Write a value only this agent knows, into both cookie and storage.
        expression = (
            "() => { document.cookie = 'probe=SECRET; path=/';"
            " localStorage.setItem('probe', 'SECRET');"
            " return document.cookie; }"
        ).replace("SECRET", self.secret)
        written = await self.call("page_eval", lease=self.lease, expression=expression)
        if not written.get("ok"):
            self.error = f"could not write the probe value: {written}"
            return False

        cookies = await self.call("page_cookies", lease=self.lease, action="get")
        values = [c["value"] for c in cookies.get("cookies", []) if c["name"] == "probe"]
        if not values:
            self.error = f"probe cookie was not set at all (wrote: {written.get('value')!r})"
            return False
        if values != [self.secret]:
            # Seeing someone else's value here would mean the browsers are shared.
            self.error = f"ISOLATION BREACH: expected only {self.secret!r}, saw {values!r}"
            return False
        self.note(f"sees exactly its own cookie ({len(cookies.get('cookies', []))} total)")

        stored = await self.call(
            "page_eval", lease=self.lease, expression="localStorage.getItem('probe')"
        )
        if stored.get("value") != self.secret:
            self.error = (
                f"storage isolation: expected {self.secret!r}, got {stored.get('value')!r}"
            )
            return False
        self.note("localStorage also isolated")

        # A guessed token must be refused. (A *valid* token held by someone else
        # does work -- possession is the identity, by design and by necessity:
        # subagents share one MCP connection, so the transport cannot tell them
        # apart. Unguessability is the guarantee being tested here.)
        guessed = await self.call("page_snapshot", lease="lst_" + "A" * 43)
        if guessed.get("ok"):
            self.error = "SECURITY: a fabricated lease token was accepted"
            return False
        self.note(f"a guessed token is refused ({guessed.get('error')})")

        net = await self.call(
            "net_requests", lease=self.lease, resource_types=["document"]
        )
        self.note(f"network: {net.get('count')} document request(s)")

        status = await self.call("browser_status", lease=self.lease)
        self.note(
            f"lease {status.get('lease_id')} "
            f"{status.get('tool_calls')} calls, {status.get('age_seconds')}s old"
        )
        return True

    async def run(self) -> bool:
        try:
            if not await self.acquire():
                return False
            return await self.work()
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}"
            return False
        finally:
            if self.lease:
                released = await self.call(
                    "browser_release", lease=self.lease, reason="concurrency_test"
                )
                self.note(
                    f"released: {released.get('status')} "
                    f"{released.get('browser_seconds')}s ${released.get('cost_usd')}"
                )


async def run(args: argparse.Namespace) -> int:
    env = get_default_environment()
    env.update(
        {
            "CBMCP_MAX_CONCURRENCY": str(args.max_concurrency),
            "CBMCP_ACQUIRE_BLOCK_SECONDS": "20",
            "CBMCP_IDLE_TIMEOUT_SECONDS": "180",
            "CBMCP_LOG_LEVEL": "WARNING",
        }
    )
    params = StdioServerParameters(
        command=str(ROOT / ".venv/bin/python"),
        args=["-m", "cbmcp.server"],
        cwd=str(ROOT),
        env=env,
    )

    print(
        f"{args.agents} agents sharing ONE MCP connection, "
        f"{args.max_concurrency} browser slots\n"
    )
    started = time.time()
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {t.name for t in (await session.list_tools()).tools}
            print(f"connected, {len(tools)} tools\n")

            agents = [Agent(session, i) for i in range(1, args.agents + 1)]

            # No barrier on purpose: with fewer slots than agents, waiting for
            # everyone to acquire before anyone works would deadlock.
            async def drive(agent: Agent) -> bool:
                if not await agent.acquire():
                    return False
                try:
                    return await agent.work()
                finally:
                    if agent.lease:
                        token = agent.lease
                        released = await agent.call(
                            "browser_release", lease=token, reason="concurrency_test"
                        )
                        agent.note(
                            f"released: {released.get('status')} "
                            f"{released.get('browser_seconds')}s ${released.get('cost_usd')}"
                        )
                        agent.lease = None
                        # A released token must stop working, and releasing twice
                        # must stay harmless.
                        after = await agent.call("page_snapshot", lease=token)
                        again = await agent.call("browser_release", lease=token)
                        if after.get("ok"):
                            agent.error = "SECURITY: a released token still drives the browser"
                        elif again.get("status") not in ("already_released", "released"):
                            agent.error = f"double release was not harmless: {again}"
                        else:
                            agent.note("released token is dead; double release is a no-op")

            outcomes = await asyncio.gather(
                *[drive(a) for a in agents], return_exceptions=True
            )

            print("\n--- final server state ---")
            stats = unwrap(await session.call_tool("admin_stats", {}))
            capacity = stats.get("capacity", {})
            budget = stats.get("budget", {}).get("today", {})
            sessions_now = unwrap(
                await session.call_tool("admin_sessions", {"action": "list"})
            )

    elapsed = time.time() - started
    ok = [
        a for a, r in zip(agents, outcomes, strict=False) if r is True and not a.error
    ]
    bad = [a for a in agents if a not in ok]
    queued = [a for a in agents if a.queued]

    print(f"\n{'=' * 70}")
    print(f"{len(ok)}/{len(agents)} agents completed in {elapsed:.0f}s")
    print(f"agents that queued:       {len(queued)} ({[a.name for a in queued]})")
    print(
        "agents that waited:       "
        f"{len([a for a in agents if a.waited > 0])} "
        f"(longest {max((a.waited for a in agents), default=0):.0f}s)"
    )
    print(f"slots in use at the end:  {capacity.get('slots_used')}")
    print(f"active leases at the end: {capacity.get('active_leases')}")
    print(f"cloud sessions still up:  {sessions_now.get('active_on_platform')}")
    print(f"spend today: ${budget.get('cost_usd', 0):.5f} over {budget.get('sessions')} sessions")

    failures: list[str] = []
    if bad:
        failures += [f"{a.name}: {a.error}" for a in bad]
    if capacity.get("slots_used") != 0:
        failures.append(f"slots did not return to zero: {capacity.get('slots_used')}")
    if sessions_now.get("active_on_platform"):
        failures.append(
            f"{sessions_now['active_on_platform']} cloud session(s) left running"
        )
    waited = [a for a in agents if a.waited > 0]
    if args.agents > args.max_concurrency and not waited:
        failures.append("more agents than slots, yet nobody ever waited for one")

    if failures:
        print("\nFAILURES:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nPASS: every agent got an isolated browser, the queue worked, "
          "and nothing was left running.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agents", type=int, default=5)
    parser.add_argument("--max-concurrency", type=int, default=3)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
