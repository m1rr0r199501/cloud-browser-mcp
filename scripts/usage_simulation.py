#!/usr/bin/env python
"""Realistic usage simulation: several Claude windows, many subagents, real sites.

    python scripts/usage_simulation.py            # ~4 minutes, about $0.01-0.02

Uses the real cloud account from your .env (BROWSER_USE_API_KEY), but its own
throwaway data directory, so your real gateway state and sessions are never
touched. What it does:

* Five "Claude windows" (five gateway processes, as Claude Code starts one per
  window) open at different times and send out 22 subagents over time.
* Each subagent works the way an LLM agent does: read ``page_snapshot``, pick an
  element by role and name from its text, act on its ``[ref]``, think between
  calls -- on books.toscrape.com, Hacker News, MDN, GitHub, example.com and
  the-internet.herokuapp.com.
* Personas reproduce real failure modes: forgetting to release, wandering off
  past the idle timeout, renewing before a pause, retrying ``browser_acquire``,
  firing tool calls in parallel, long multi-site research sessions.
* Window 3 is closed by the user mid-task, window 2 crashes (its gateway is
  killed), window 5 is opened afterwards.
* Peak demand exceeds the shared ceiling of 6.

Time is compressed for the run: the idle timeout is 45s instead of 300s.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import signal
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from contextlib import AsyncExitStack
from pathlib import Path

from mcp import ClientSession, StdioServerParameters, stdio_client

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cbmcp.config import Settings  # noqa: E402
from cbmcp.providers import build_provider  # noqa: E402

DATA_DIR = Path(tempfile.mkdtemp(prefix="cbmcp-sim-"))
CEILING = 6
CHILD_ENV = {
    **os.environ,
    "CBMCP_DATA_DIR": str(DATA_DIR),
    "CBMCP_MAX_CONCURRENCY": str(CEILING),
    "CBMCP_IDLE_TIMEOUT_SECONDS": "45",
    "CBMCP_REAPER_INTERVAL_SECONDS": "5",
    "CBMCP_LOG_LEVEL": "ERROR",
}

RNG = random.Random(20261018)
T0 = time.time()
LAT: dict[str, list[float]] = defaultdict(list)
REF = re.compile(r"\[ref=(e\d+)\]")


def now() -> float:
    return time.time() - T0


def log(msg: str) -> None:
    print(f"[{now():6.1f}s] {msg}", flush=True)


def unwrap(result) -> dict:
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except ValueError:
                return {"_text": text}
    return {}


def find_ref(snapshot: str, role: str, name: str | None = None, pred=None) -> str | None:
    """What an LLM does with a snapshot: find '- role "name" [ref=eN]'."""
    for line in snapshot.splitlines():
        s = line.strip()
        if not s.startswith(f"- {role} ") and s != f"- {role}":
            continue
        if name is not None and name.lower() not in s.lower():
            continue
        if pred is not None and not pred(s):
            continue
        m = REF.search(s)
        if m:
            return m.group(1)
    return None


def instance_pids() -> dict[str, int]:
    """Gateway instance id -> pid, from the heartbeat table the gateways keep."""
    try:
        db = sqlite3.connect(DATA_DIR / "cbmcp.sqlite3")
        rows = db.execute("SELECT instance_id, pid FROM instances").fetchall()
        db.close()
        return {r[0]: int(r[1]) for r in rows}
    except sqlite3.Error:
        return {}


def hard_kill(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
    else:
        os.kill(pid, signal.SIGKILL)


class Restart(Exception):
    pass


class Window:
    def __init__(self, n: int):
        self.n = n
        self.s: ClientSession | None = None
        self.stack: AsyncExitStack | None = None
        self.alive = False
        self.pid: int | None = None
        self.agents: list[Agent] = []
        self.tasks: list[asyncio.Task] = []

    async def open(self) -> None:
        before = set(instance_pids())
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "cbmcp.server"], env=CHILD_ENV
        )
        self.stack = AsyncExitStack()
        await self.stack.__aenter__()
        read, write = await self.stack.enter_async_context(stdio_client(params))
        self.s = await self.stack.enter_async_context(ClientSession(read, write))
        await self.s.initialize()  # startup recovery has run, heartbeat is written
        new = {k: v for k, v in instance_pids().items() if k not in before}
        self.pid = next(iter(new.values()), None)
        self.alive = True
        log(f"window {self.n} opened (gateway pid {self.pid})")

    def spawn(self, name: str, task: str, persona: str) -> None:
        agent = Agent(self, name, task, persona)
        self.agents.append(agent)
        self.tasks.append(asyncio.create_task(agent.run()))

    async def close(self, how: str) -> None:
        self.alive = False
        for agent in self.agents:
            if not agent.finished:
                agent.outcome = f"cut ({how})"
        if how == "crash" and self.pid:
            hard_kill(self.pid)
            log(f"window {self.n} CRASHED (gateway pid {self.pid} killed)")
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        try:
            await asyncio.wait_for(self.stack.aclose(), timeout=15)
        except BaseException:  # noqa: BLE001 - a crashed transport may not close cleanly
            pass
        if how != "crash":
            log(f"window {self.n} closed ({how})")


class Agent:
    def __init__(self, win: Window, name: str, task: str, persona: str):
        self.win, self.name, self.task, self.persona = win, name, task, persona
        self.lease: str | None = None
        self.lease_ids: list[str] = []
        self.attempt = 0
        self.calls = 0
        self.errors: list[str] = []
        self.checks: list[tuple[str, bool, str]] = []
        self.queued = False
        self.wait = 0.0
        self.expired = 0
        self.expiry_reply: dict = {}
        self.paused = False
        self.finished = False
        self.outcome = "running"

    async def call(self, tool: str, **args) -> dict:
        t = time.time()
        r = unwrap(await self.win.s.call_tool(tool, args))
        LAT[tool].append(time.time() - t)
        self.calls += 1
        if isinstance(r, dict) and r.get("ok") is False and r.get("error"):
            self.errors.append(f"{tool}:{r['error']}")
        return r

    def check(self, name: str, ok, detail="") -> None:
        self.checks.append((name, bool(ok), str(detail)[:90]))

    async def think(self, lo: float = 1.5, hi: float = 6.0) -> None:
        if RNG.random() < 0.1:
            hi = 15.0  # a longer deliberation now and then
        await asyncio.sleep(RNG.uniform(lo, hi))

    async def acquire(self) -> None:
        key = f"{self.name}#{self.attempt}"
        t0 = time.time()
        r = await self.call("browser_acquire", task_label=self.name, idempotency_key=key)
        if self.persona == "retrier":
            again = await self.call("browser_acquire", task_label=self.name, idempotency_key=key)
            same = (again.get("lease") and again.get("lease") == r.get("lease")) or (
                again.get("ticket_id") and again.get("ticket_id") == r.get("ticket_id")
            )
            self.check("retry returns the same browser/ticket", same,
                       f"{r.get('status')}/{again.get('status')}")
        while r.get("status") in ("queued", "creating"):
            self.queued = self.queued or r.get("status") == "queued"
            r = await self.call("browser_queue_status", ticket_id=r["ticket_id"], wait_seconds=30)
        self.wait += time.time() - t0
        if r.get("status") != "ready":
            raise RuntimeError(f"acquire failed: {r.get('error')} {r.get('message')}")
        self.lease = r["lease"]
        self.lease_ids.append(r.get("lease_id"))
        log(f"{self.name} [{self.persona}] got a browser after {time.time() - t0:.0f}s")

    async def step(self, tool: str, **args) -> dict:
        r = await self.call(tool, lease=self.lease, **args)
        if r.get("error") in ("lease_expired", "lease_not_found", "lease_not_active"):
            # What an agent does when told its browser was reclaimed.
            self.expired += 1
            self.expiry_reply = r
            log(f"{self.name} told '{r['error']}': {str(r.get('message'))[:60]} -> new browser")
            self.lease = None
            self.attempt += 1
            await self.acquire()
            raise Restart()
        return r

    async def snapshot(self) -> str:
        return (await self.step("page_snapshot", max_nodes=800)).get("snapshot") or ""

    async def maybe_wander_off(self) -> None:
        if self.paused:
            return
        if self.persona == "distracted":
            self.paused = True
            log(f"{self.name} wanders off for 60s mid-task")
            await asyncio.sleep(60)
        elif self.persona == "renewer":
            self.paused = True
            log(f"{self.name} pauses 60s, renewing half-way")
            await asyncio.sleep(30)
            r = await self.step("browser_renew")
            self.check("renew keeps the browser", r.get("ok"), r.get("error"))
            await asyncio.sleep(30)

    async def run(self) -> None:
        try:
            await self.acquire()
            for _ in range(3):
                try:
                    await TASKS[self.task](self)
                    if self.persona == "parallel":
                        rs = await asyncio.gather(
                            self.step("page_snapshot", max_nodes=200),
                            self.step("net_requests", limit=10),
                            self.step("page_text", max_chars=300),
                        )
                        self.check("3 parallel calls on one browser",
                                   all(x.get("ok") for x in rs), [x.get("error") for x in rs])
                    break
                except Restart:
                    continue
            if self.persona == "distracted":
                reply = self.expiry_reply
                self.check("told the browser was reclaimed, and how to avoid it",
                           reply.get("error") == "lease_expired"
                           and "browser_renew" in (reply.get("hint") or ""),
                           f"{reply.get('error')}: {str(reply.get('hint'))[:60]}")
            if self.persona == "renewer":
                self.check("never lost the browser", self.expired == 0, self.expired)
            ok = all(c[1] for c in self.checks)
            self.outcome = "done" if ok else "done, with failed checks"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.outcome = f"error: {type(exc).__name__}: {str(exc)[:80]}"
        finally:
            self.finished = True
            if self.lease and self.win.alive and self.persona != "forgetful":
                try:
                    await self.call("browser_release", lease=self.lease)
                except Exception:  # noqa: BLE001
                    pass
            if self.persona == "forgetful" and self.outcome.startswith("done"):
                log(f"{self.name} finished and walked away WITHOUT releasing")


# ------------------------------------------------------------- tasks -------

async def books(a: Agent) -> None:
    await a.step("page_navigate", url="https://books.toscrape.com/")
    await a.think()
    ref = find_ref(await a.snapshot(), "link", '"Travel"')
    a.check("books: found Travel in the snapshot", ref, ref)
    await a.step("page_click", ref=ref)
    await a.think()
    await a.maybe_wander_off()
    ref = find_ref(await a.snapshot(), "link",
                   pred=lambda s: "index.html" in s and "category" not in s and "../../../" in s)
    a.check("books: found a book link", ref, ref)
    await a.step("page_click", ref=ref)
    await a.think()
    text = (await a.step("page_text", max_chars=3000)).get("text") or ""
    a.check("books: read the price", "£" in text, re.search(r"£[\d.]+", text))


async def hackernews(a: Agent) -> None:
    await a.step("page_navigate", url="https://news.ycombinator.com/")
    await a.think()
    ref = find_ref(await a.snapshot(), "link", '"More"')
    a.check("hn: found More", ref, ref)
    await a.maybe_wander_off()
    r = await a.step("page_click", ref=ref)
    a.check("hn: on page 2", "p=2" in (r.get("url") or ""), r.get("url"))
    await a.think()
    text = (await a.step("page_text", max_chars=2000)).get("text") or ""
    a.check("hn: read stories", "points" in text or "comments" in text, len(text))
    back = await a.step("page_navigate", action="back")
    a.check("hn: back", back.get("ok") and "p=2" not in (back.get("url") or ""), back.get("url"))


async def the_internet(a: Agent) -> None:
    base = "https://the-internet.herokuapp.com"
    await a.step("page_navigate", url=base + "/dropdown")
    await a.think()
    ref = find_ref(await a.snapshot(), "combobox")
    r = await a.step("page_select", ref=ref, values=["Option 2"], by_label=True)
    a.check("internet: chose a dropdown option", r.get("ok"), r.get("selected"))
    await a.think()
    await a.step("page_navigate", url=base + "/checkboxes")
    ref = find_ref(await a.snapshot(), "checkbox", pred=lambda s: "unchecked" in s)
    r = await a.step("page_click", ref=ref)
    a.check("internet: ticked a checkbox", r.get("ok") and ref, ref)
    await a.maybe_wander_off()
    await a.step("page_navigate", url=base + "/javascript_alerts")
    await a.step("page_dialog", action="accept")
    ref = find_ref(await a.snapshot(), "button", "JS Confirm")
    r = await a.step("page_click", ref=ref)
    dialogs = r.get("dialogs") or []
    a.check("internet: answered OK to a confirm",
            dialogs and dialogs[0].get("handled") == "accepted", dialogs[:1])


async def mdn(a: Agent) -> None:
    await a.step("page_navigate", url="https://developer.mozilla.org/en-US/docs/Web/CSS")
    await a.think()
    ref = find_ref(await a.snapshot(), "link", pred=lambda s: 'href="/en-US/docs/Web/CSS/' in s)
    a.check("mdn: found a CSS docs link", ref, ref)
    await a.maybe_wander_off()
    r = await a.step("page_click", ref=ref)
    moved = r.get("url") or ""
    a.check("mdn: opened it", "/en-US/docs/Web/CSS/" in moved, moved)
    await a.think()
    text = await a.step("page_text", max_chars=2000)
    a.check("mdn: read it", (text.get("total_chars") or 0) > 500, text.get("total_chars"))
    back = await a.step("page_navigate", action="back")
    a.check("mdn: back", (back.get("url") or "").rstrip("/").endswith("/Web/CSS"), back.get("url"))


async def github(a: Agent) -> None:
    await a.step("page_navigate", url="https://github.com/modelcontextprotocol/python-sdk")
    await a.think()
    ref = find_ref(await a.snapshot(), "link", "Issues",
                   pred=lambda s: 'href="/modelcontextprotocol/python-sdk/issues"' in s)
    a.check("github: found the Issues tab", ref, ref)
    await a.maybe_wander_off()
    r = await a.step("page_click", ref=ref)
    a.check("github: on the issues page", "/issues" in (r.get("url") or ""), r.get("url"))
    await a.think()
    shot = await a.win.s.call_tool("page_screenshot", {"lease": a.lease})
    a.check("github: screenshot", any(getattr(b, "type", "") == "image" for b in shot.content))


async def example(a: Agent) -> None:
    await a.step("page_navigate", url="https://example.com/")
    await a.think()
    ref = find_ref(await a.snapshot(), "link")
    a.check("example: found its only link", ref, ref)
    await a.maybe_wander_off()
    r = await a.step("page_click", ref=ref)
    a.check("example: followed it off-site", "iana.org" in (r.get("url") or ""), r.get("url"))
    await a.think()
    text = (await a.step("page_text", max_chars=1500)).get("text") or ""
    a.check("example: read the new page", len(text) > 100, len(text))


async def tour(a: Agent) -> None:
    """A research subagent: three sites in a row on one browser."""
    for task in RNG.sample([books, hackernews, mdn, example, github], 3):
        await task(a)
        await a.think(3, 8)


TASKS = {"books": books, "hackernews": hackernews, "the_internet": the_internet,
         "mdn": mdn, "github": github, "example": example, "tour": tour}

# (window, opens_at, [(arrives_at, task, persona), ...])
PLAN = [
    (1, 0, [(0, "books", "diligent"), (8, "hackernews", "parallel"),
            (25, "example", "forgetful"), (60, "mdn", "diligent")]),
    (2, 10, [(10, "the_internet", "diligent"), (30, "github", "distracted"),
             (50, "books", "retrier"), (100, "tour", "diligent"), (115, "tour", "diligent")]),
    (3, 20, [(20, "mdn", "retrier"), (35, "hackernews", "diligent"),
             (95, "tour", "diligent")]),
    (4, 40, [(40, "github", "diligent"), (45, "example", "diligent"),
             (55, "books", "distracted"), (90, "the_internet", "parallel"),
             (100, "tour", "renewer"), (105, "tour", "diligent"), (110, "tour", "diligent")]),
    (5, 150, [(150, "hackernews", "diligent"), (160, "mdn", "diligent"),
              (165, "tour", "diligent")]),
]
CLOSE_W3_AT = 120
CRASH_W2_AT = 140


async def main() -> int:
    settings = Settings(data_dir=DATA_DIR)
    provider = build_provider(settings)
    windows = {n: Window(n) for n, _, _ in PLAN}
    samples: list[int] = []
    stop = asyncio.Event()
    crash: dict = {}

    async def monitor():
        while not stop.is_set():
            try:
                live = await provider.list_sessions(active_only=True)
                samples.append(sum(1 for x in live
                                   if x.metadata.get("cbmcp_owner") == settings.owner_tag))
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(2)

    async def run_window(n, opens_at, arrivals):
        await asyncio.sleep(max(0.0, opens_at - now()))
        w = windows[n]
        await w.open()
        for i, (at, task, persona) in enumerate(arrivals):
            await asyncio.sleep(max(0.0, at - now()))
            if not w.alive:
                return
            w.spawn(f"w{n}-{task}-{i}", task, persona)

    async def watch_crashed(session_ids):
        """Poll from the moment of the crash until the platform shows them stopped."""
        t0 = time.time()
        while time.time() - t0 < 300:
            alive = 0
            for sid in session_ids:
                s = await provider.get_session(sid)
                alive += bool(s and s.status == "active")
            if not alive:
                crash["stopped_after"] = time.time() - t0
                return
            await asyncio.sleep(2)

    async def events():
        await asyncio.sleep(max(0.0, CLOSE_W3_AT - now()))
        await windows[3].close("closed by the user")
        await asyncio.sleep(max(0.0, CRASH_W2_AT - now()))
        w2_leases = {a.lease_ids[-1] for a in windows[2].agents if not a.finished and a.lease_ids}
        rows = unwrap(await windows[1].s.call_tool("admin_sessions", {"action": "list"}))
        crash["sessions"] = [r["platform_session_id"] for r in rows.get("sessions", [])
                             if r.get("lease_id") in w2_leases]
        await windows[2].close("crash")
        if crash["sessions"]:
            crash["watch"] = asyncio.create_task(watch_crashed(crash["sessions"]))

    print(f"data dir for this run: {DATA_DIR}\n")
    mon = asyncio.create_task(monitor())
    ev = asyncio.create_task(events())
    await asyncio.gather(*(run_window(n, at, arr) for n, at, arr in PLAN))
    await ev
    for w in windows.values():
        if w.alive:
            await asyncio.gather(*w.tasks, return_exceptions=True)
    if crash.get("watch"):
        await crash["watch"]

    stats = unwrap(await windows[1].s.call_tool("admin_stats", {}))
    for w in windows.values():
        if w.alive:
            await w.close("end of the day")
    stop.set()
    await mon
    await asyncio.sleep(3)
    leftovers = [x for x in await provider.list_sessions(active_only=True)
                 if x.metadata.get("cbmcp_owner") == settings.owner_tag]
    await provider.aclose()

    print("\n" + "=" * 96)
    agents = [a for w in windows.values() for a in w.agents]
    for a in agents:
        print(f"{a.name:24s} {a.persona:10s} {a.outcome:26s} calls={a.calls:3d} "
              f"wait={a.wait:5.1f}s expired={a.expired} errors={sorted(set(a.errors)) or '-'}")
        for name, ok, detail in a.checks:
            if not ok:
                print(f"{'':36s}FAIL {name}: {detail}")
    checks = [c for a in agents for c in a.checks]
    counters = stats.get("counters") or {}
    print("-" * 96)
    print(f"agents: {len(agents)}   done: {sum(a.outcome.startswith('done') for a in agents)}   "
          f"cut by window close/crash: {sum(a.outcome.startswith('cut') for a in agents)}   "
          f"errors: {sum(a.outcome.startswith('error') for a in agents)}")
    print(f"checks passed: {sum(c[1] for c in checks)}/{len(checks)}")
    print(f"browsers reclaimed then re-acquired: {sum(a.expired for a in agents)}   "
          f"idle reclaims seen by window 1: {int(counters.get('reclaim_idle_timeout', 0))}")
    print(f"peak cloud browsers at once: {max(samples, default=0)} (ceiling {CEILING})")
    if crash.get("sessions"):
        after = crash.get("stopped_after")
        print(f"crashed window held {len(crash['sessions'])} cloud browser(s): "
              + (f"all stopped {after:.0f}s after the crash" if after is not None
                 else "STILL RUNNING 300s after the crash"))
    print("tool latency (median / p95 seconds):")
    for tool in sorted(LAT):
        vals = sorted(LAT[tool])
        p95 = vals[min(len(vals) - 1, int(len(vals) * 0.95))]
        print(f"  {tool:22s} n={len(vals):3d}  {statistics.median(vals):5.2f} / {p95:5.2f}")
    print(f"cloud browsers left running at the end: {len(leftovers)}")
    print(f"wall time: {now():.0f}s")
    failed = (not all(c[1] for c in checks) or leftovers
              or any(a.outcome.startswith("error") for a in agents)
              or max(samples, default=0) > CEILING)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
