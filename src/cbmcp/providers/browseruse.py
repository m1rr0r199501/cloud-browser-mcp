"""Browser Use Cloud provider (API v4 ``/browsers``).

Endpoints used, as documented at docs.browser-use.com (checked 2026-10):

    POST   /api/v4/browsers            -> 201 BrowserSessionItemView (has cdpUrl)
    GET    /api/v4/browsers            -> paged list, filterBy=active|stopped
    GET    /api/v4/browsers/{id}       -> single session
    PATCH  /api/v4/browsers/{id}       -> {"action":"stop"}
    GET    /api/v2/billing/account     -> concurrentSessionLimit, activeSessionCount

Auth is the raw key in ``X-Browser-Use-API-Key`` -- no ``Bearer`` prefix.

Two behaviours worth knowing because they cost money:

* ``proxyCountryCode`` defaults to ``us`` server-side, i.e. the proxy is ON
  unless you explicitly send ``null``. ``NetworkSpec(kind="direct")`` sends null.
* Disconnecting CDP or calling Playwright's ``browser.close()`` does **not** stop
  the session. Only the PATCH stop does. Everything in this project is built
  around that fact.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any

import httpx

from ..errors import ProviderConcurrencyLimit, ProviderError, ProviderQuotaError
from ..log import get_logger
from .base import AccountInfo, NetworkSpec, ProviderSession, SessionSpec

log = get_logger("provider.browseruse")

_RETRYABLE = (500, 502, 503, 504)


def _parse_dt(value: Any) -> float | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _to_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class BrowserUseProvider:
    name = "browseruse"
    supports_profiles = True
    supports_custom_proxy = True

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.browser-use.com",
        *,
        timeout: float = 45.0,
    ) -> None:
        if not api_key:
            raise ProviderError(
                "BROWSER_USE_API_KEY is not set",
                hint="Put the key in .env as BROWSER_USE_API_KEY and restart the server.",
            )
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={
                "X-Browser-Use-API-Key": api_key,
                "Content-Type": "application/json",
                "User-Agent": "cbmcp/0.1",
            },
            timeout=httpx.Timeout(timeout, connect=15.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # ----- plumbing --------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        params: Any = None,
        retries: int = 2,
    ) -> Any:
        """Send a request, mapping platform errors onto our taxonomy.

        ``retries`` is deliberately 0 for session creation: a POST that times out
        may well have created a browser we would then never learn about, and a
        blind retry would double the bill. Those cases are recovered by
        reconciliation via the owner tag in session metadata instead.
        """
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = await self._client.request(method, path, json=json_body, params=params)
            except httpx.HTTPError as exc:
                if attempt <= retries:
                    await asyncio.sleep(min(2 ** attempt * 0.4, 5.0))
                    continue
                raise ProviderError(f"network error talking to Browser Use: {type(exc).__name__}") from exc

            if resp.status_code in _RETRYABLE and attempt <= retries:
                await asyncio.sleep(min(2 ** attempt * 0.4, 5.0))
                continue
            if resp.status_code == 429 and attempt <= retries:
                delay = _to_float(resp.headers.get("Retry-After")) or 2.0
                body = self._safe_json(resp)
                # A project-level throttle is short; a concurrency rejection is not
                # something to wait out in a loop.
                if isinstance(body, dict) and body.get("limit_rps"):
                    await asyncio.sleep(min(delay, 10.0))
                    continue
                raise ProviderConcurrencyLimit(
                    "Browser Use rejected the request: too many concurrent sessions",
                    retry_after_seconds=delay,
                )
            return self._handle(resp)

    def _handle(self, resp: httpx.Response) -> Any:
        if resp.is_success:
            if not resp.content:
                return None
            return self._safe_json(resp)

        body = self._safe_json(resp)
        detail = ""
        if isinstance(body, dict):
            detail = str(body.get("detail") or body.get("message") or body.get("error") or "")
        detail = detail[:400]

        code = resp.status_code
        if code == 402:
            raise ProviderQuotaError(
                f"Browser Use refused: insufficient credits or monthly spend cap reached. {detail}"
            )
        if code == 429:
            raise ProviderConcurrencyLimit(
                f"Browser Use refused: concurrent session limit reached. {detail}",
                retry_after_seconds=_to_float(resp.headers.get("Retry-After")) or None,
            )
        if code == 401 or code == 403:
            raise ProviderError(
                f"Browser Use refused (HTTP {code}): {detail or 'check the API key and limits'}",
                hint="Verify BROWSER_USE_API_KEY and that the requested timeout is <= 240 minutes.",
            )
        if code == 404:
            raise ProviderError(f"Browser Use: not found. {detail}")
        raise ProviderError(f"Browser Use HTTP {code}: {detail}")

    @staticmethod
    def _safe_json(resp: httpx.Response) -> Any:
        try:
            return resp.json()
        except ValueError:
            return {"raw": resp.text[:400]}

    # ----- mapping ---------------------------------------------------------

    @staticmethod
    def _to_session(data: dict) -> ProviderSession:
        return ProviderSession(
            id=str(data.get("id", "")),
            cdp_url=data.get("cdpUrl"),
            status=str(data.get("status") or "unknown"),
            started_at=_parse_dt(data.get("startedAt")),
            timeout_at=_parse_dt(data.get("timeoutAt")),
            finished_at=_parse_dt(data.get("finishedAt")),
            live_url=data.get("liveUrl"),
            cost_usd=_to_float(data.get("browserCost")),
            proxy_cost_usd=_to_float(data.get("proxyCost")),
            proxy_mb=_to_float(data.get("proxyUsedMb")),
            metadata={str(k): str(v) for k, v in (data.get("metadata") or {}).items()},
        )

    @staticmethod
    def _network_payload(net: NetworkSpec) -> dict[str, Any]:
        if net.kind == "direct":
            # Explicit null: the server-side default is "us", which would silently
            # route through a billed proxy.
            return {"proxyCountryCode": None}
        if net.kind == "platform_proxy":
            return {"proxyCountryCode": (net.country_code or "us").lower()}
        if net.kind == "custom_proxy":
            if not net.custom:
                raise ProviderError("custom_proxy selected but no proxy details supplied")
            payload: dict[str, Any] = {
                "proxyCountryCode": None,
                "customProxy": {
                    "host": net.custom.host,
                    "port": int(net.custom.port),
                    "ignoreCertErrors": bool(net.custom.ignore_cert_errors),
                },
            }
            if net.custom.username:
                payload["customProxy"]["username"] = net.custom.username
            if net.custom.password:
                payload["customProxy"]["password"] = net.custom.password
            return payload
        raise ProviderError(f"unknown network kind {net.kind!r}")

    # ----- API -------------------------------------------------------------

    async def create_session(self, spec: SessionSpec) -> ProviderSession:
        body: dict[str, Any] = {
            "timeout": int(spec.timeout_minutes),
            "solveCaptchas": bool(spec.solve_captchas),
            "pdfRendererEnabled": bool(spec.pdf_renderer_enabled),
            "enableRecording": bool(spec.enable_recording),
        }
        body.update(self._network_payload(spec.network))
        if spec.screen_width:
            body["browserScreenWidth"] = int(spec.screen_width)
        if spec.screen_height:
            body["browserScreenHeight"] = int(spec.screen_height)
        if spec.profile_id:
            body["profileId"] = spec.profile_id
        if spec.metadata:
            body["metadata"] = {k: str(v)[:1000] for k, v in list(spec.metadata.items())[:10]}

        data = await self._request("POST", "/api/v4/browsers", json_body=body, retries=0)
        if not isinstance(data, dict) or not data.get("id"):
            raise ProviderError(f"unexpected create response shape: {str(data)[:200]}")
        session = self._to_session(data)
        if not session.cdp_url:
            raise ProviderError(
                "Browser Use created the session but returned no cdpUrl",
                platform_session_id=session.id,
            )
        log.info(
            "created platform session id=%s timeout_min=%s network=%s",
            session.id,
            spec.timeout_minutes,
            spec.network.kind,
        )
        return session

    async def get_session(self, session_id: str) -> ProviderSession | None:
        try:
            data = await self._request("GET", f"/api/v4/browsers/{session_id}")
        except ProviderError as exc:
            if "not found" in str(exc).lower():
                return None
            raise
        if not isinstance(data, dict):
            return None
        return self._to_session(data)

    async def list_sessions(
        self, *, active_only: bool = True, limit: int = 500
    ) -> list[ProviderSession]:
        out: list[ProviderSession] = []
        page = 1
        page_size = 100
        while len(out) < limit:
            params: list[tuple[str, Any]] = [("pageSize", page_size), ("pageNumber", page)]
            if active_only:
                params.append(("filterBy", "active"))
            data = await self._request("GET", "/api/v4/browsers", params=params)
            if not isinstance(data, dict):
                break
            items = data.get("items") or []
            out.extend(self._to_session(item) for item in items if isinstance(item, dict))
            total = int(data.get("totalItems") or 0)
            if len(items) < page_size or len(out) >= total:
                break
            page += 1
            if page > 20:  # safety valve against a misbehaving pager
                break
        return out[:limit]

    async def stop_session(self, session_id: str) -> bool:
        """Stop a session. Idempotent: stopping an already-stopped session is fine."""
        try:
            await self._request(
                "PATCH", f"/api/v4/browsers/{session_id}", json_body={"action": "stop"}, retries=3
            )
            return True
        except ProviderError as exc:
            msg = str(exc).lower()
            if "not found" in msg:
                return True
            log.warning("stop failed for %s: %s", session_id, exc)
            return False

    async def confirm_stopped(self, session_id: str) -> bool:
        session = await self.get_session(session_id)
        return session is None or session.status == "stopped"

    async def account_info(self) -> AccountInfo:
        data = await self._request("GET", "/api/v2/billing/account")
        if not isinstance(data, dict):
            return AccountInfo()
        # The live API reports totalCreditsBalanceUsd (checked 2026-10);
        # creditBalance is kept as a fallback for older responses.
        balance = data.get("totalCreditsBalanceUsd")
        if balance is None:
            balance = data.get("creditBalance")
        if isinstance(balance, dict):
            balance = balance.get("usd") or balance.get("amount")
        return AccountInfo(
            project_id=data.get("projectId"),
            concurrent_session_limit=data.get("concurrentSessionLimit"),
            active_session_count=data.get("activeSessionCount"),
            credit_balance_usd=_to_float(balance) if balance is not None else None,
            raw={k: v for k, v in data.items() if k != "raw"},
        )

    async def ping(self) -> dict[str, Any]:
        started = time.time()
        info = await self.account_info()
        return {
            "ok": True,
            "latency_ms": int((time.time() - started) * 1000),
            "project_id": info.project_id,
            "concurrent_session_limit": info.concurrent_session_limit,
            "active_session_count": info.active_session_count,
            "credit_balance_usd": info.credit_balance_usd,
        }
