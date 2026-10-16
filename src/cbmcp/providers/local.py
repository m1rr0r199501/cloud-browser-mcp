"""Local Chromium provider -- a free second backend.

Its real jobs:

* develop and test the gateway, the reaper and every MCP tool without spending
  a cent on cloud browser time;
* act as the fallback if the cloud account hits its quota;
* prove the provider abstraction is genuinely swappable rather than decorative.

Chromium is launched as a plain subprocess with ``--remote-debugging-port=0``;
the real port is read from the ``DevTools listening on ws://...`` line Chromium
prints on stderr. Each session gets a throwaway ``--user-data-dir``, which is
what makes local sessions as isolated from each other as cloud ones.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass, field

from ..errors import ProviderError
from ..log import get_logger
from .base import AccountInfo, ProviderSession, SessionSpec

log = get_logger("provider.local")

_WS_RE = re.compile(r"DevTools listening on (ws://\S+)")


@dataclass
class _Local:
    id: str
    process: asyncio.subprocess.Process
    cdp_url: str
    user_data_dir: str
    started_at: float
    timeout_at: float
    metadata: dict[str, str] = field(default_factory=dict)
    stopped_at: float | None = None


def _find_chromium() -> str:
    env = os.environ.get("CBMCP_CHROMIUM_PATH")
    if env and os.path.exists(env):
        return env
    try:  # Playwright's bundled Chromium is the best default when present
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            path = p.chromium.executable_path
            if path and os.path.exists(path):
                return path
    except Exception:  # noqa: BLE001 - fall through to system browsers
        pass
    for candidate in (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "google-chrome",
        "chromium",
        "chromium-browser",
    ):
        found = shutil.which(candidate) if "/" not in candidate else candidate
        if found and os.path.exists(found):
            return found
    raise ProviderError(
        "no Chromium binary found for the local provider",
        hint="Run 'playwright install chromium' or set CBMCP_CHROMIUM_PATH.",
    )


class LocalChromiumProvider:
    name = "local"
    supports_profiles = False
    supports_custom_proxy = True

    def __init__(self, *, headless: bool = True) -> None:
        self._headless = headless
        self._sessions: dict[str, _Local] = {}
        self._bg: set[asyncio.Task] = set()
        self._binary: str | None = None

    async def aclose(self) -> None:
        for sid in list(self._sessions):
            await self.stop_session(sid)

    async def create_session(self, spec: SessionSpec) -> ProviderSession:
        if self._binary is None:
            self._binary = await asyncio.to_thread(_find_chromium)
        user_data_dir = tempfile.mkdtemp(prefix="cbmcp-chrome-")
        args = [
            self._binary,
            "--remote-debugging-port=0",
            f"--user-data-dir={user_data_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-features=Translate,MediaRouter,OptimizationHints",
            f"--window-size={spec.screen_width or 1440},{spec.screen_height or 900}",
            "about:blank",
        ]
        if self._headless:
            args.insert(1, "--headless=new")
        if spec.network.kind == "custom_proxy" and spec.network.custom:
            proxy = spec.network.custom
            args.insert(1, f"--proxy-server={proxy.host}:{proxy.port}")
            if proxy.username:
                # Chromium cannot do proxy auth from the command line, and it
                # cannot do authenticated SOCKS5 at all. Say so loudly.
                raise ProviderError(
                    "the local provider cannot supply proxy credentials",
                    hint="Use provider=browseruse for authenticated proxies, "
                    "or point CBMCP_CUSTOM_PROXY_HOST at a local unauthenticated relay.",
                )

        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        cdp_url = await self._read_ws_endpoint(proc)
        if not cdp_url:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            shutil.rmtree(user_data_dir, ignore_errors=True)
            raise ProviderError("local Chromium did not report a DevTools endpoint")

        sid = f"local_{uuid.uuid4().hex[:12]}"
        now = time.time()
        entry = _Local(
            id=sid,
            process=proc,
            cdp_url=cdp_url,
            user_data_dir=user_data_dir,
            started_at=now,
            timeout_at=now + spec.timeout_minutes * 60,
            metadata=dict(spec.metadata),
        )
        self._sessions[sid] = entry
        # Enforce the platform-side hard timeout locally too, so reaper tests
        # exercise the same code path as the cloud.
        timer = asyncio.create_task(self._hard_timeout(sid, spec.timeout_minutes * 60))
        self._bg.add(timer)
        timer.add_done_callback(self._bg.discard)
        log.info("local chromium session %s up", sid)
        return self._view(entry)

    async def _read_ws_endpoint(self, proc: asyncio.subprocess.Process) -> str | None:
        assert proc.stderr is not None
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                line = await asyncio.wait_for(proc.stderr.readline(), timeout=5.0)
            except TimeoutError:
                if proc.returncode is not None:
                    return None
                continue
            if not line:
                return None
            match = _WS_RE.search(line.decode("utf-8", "replace"))
            if match:
                return match.group(1)
        return None

    async def _hard_timeout(self, sid: str, seconds: float) -> None:
        await asyncio.sleep(seconds)
        if sid in self._sessions:
            log.warning("local session %s hit its hard timeout", sid)
            await self.stop_session(sid)

    def _view(self, entry: _Local) -> ProviderSession:
        alive = entry.process.returncode is None and entry.stopped_at is None
        return ProviderSession(
            id=entry.id,
            cdp_url=entry.cdp_url if alive else None,
            status="active" if alive else "stopped",
            started_at=entry.started_at,
            timeout_at=entry.timeout_at,
            finished_at=entry.stopped_at,
            cost_usd=0.0,
            metadata=entry.metadata,
        )

    async def get_session(self, session_id: str) -> ProviderSession | None:
        entry = self._sessions.get(session_id)
        return self._view(entry) if entry else None

    async def list_sessions(
        self, *, active_only: bool = True, limit: int = 500
    ) -> list[ProviderSession]:
        views = [self._view(e) for e in self._sessions.values()]
        if active_only:
            views = [v for v in views if v.is_active]
        return views[:limit]

    async def stop_session(self, session_id: str) -> bool:
        entry = self._sessions.get(session_id)
        if entry is None:
            return True
        entry.stopped_at = time.time()
        proc = entry.process
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=8.0)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
        shutil.rmtree(entry.user_data_dir, ignore_errors=True)
        self._sessions.pop(session_id, None)
        log.info("local chromium session %s stopped", session_id)
        return True

    async def confirm_stopped(self, session_id: str) -> bool:
        return session_id not in self._sessions

    async def account_info(self) -> AccountInfo:
        return AccountInfo(
            project_id="local",
            concurrent_session_limit=None,
            active_session_count=len(self._sessions),
            credit_balance_usd=None,
            raw={"provider": "local"},
        )

    async def ping(self) -> dict[str, object]:
        return {"ok": True, "provider": "local", "active": len(self._sessions)}
