"""Provider abstraction.

The gateway only ever talks to a cloud browser through this interface, so
swapping Browser Use Cloud for another vendor (or for the bundled local
Chromium backend) is a one-class change.

Two requirements shape the interface:

* ``create_session`` must accept a *platform-side* hard timeout. That timeout is
  the only cost guarantee that survives this process being killed -9.
* ``list_sessions`` must be able to enumerate everything the account currently
  runs, with its owner metadata, so reconciliation can spot orphans.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

NetworkKind = Literal["direct", "platform_proxy", "custom_proxy"]


@dataclass(frozen=True)
class CustomProxy:
    host: str
    port: int
    username: str | None = None
    password: str | None = None
    ignore_cert_errors: bool = False


@dataclass(frozen=True)
class NetworkSpec:
    kind: NetworkKind = "direct"
    country_code: str | None = None
    custom: CustomProxy | None = None

    def describe(self) -> str:
        if self.kind == "direct":
            return "direct (platform datacenter IP, no proxy billing)"
        if self.kind == "platform_proxy":
            return f"platform proxy ({self.country_code or 'default'}) -- billed per GB"
        if self.custom:
            auth = " with auth" if self.custom.username else ""
            return f"custom proxy {self.custom.host}:{self.custom.port}{auth}"
        return self.kind


@dataclass(frozen=True)
class SessionSpec:
    timeout_minutes: int
    network: NetworkSpec
    screen_width: int | None = None
    screen_height: int | None = None
    solve_captchas: bool = False
    pdf_renderer_enabled: bool = True
    enable_recording: bool = False
    profile_id: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass
class ProviderSession:
    id: str
    cdp_url: str | None
    status: str  # "active" | "stopped" | "unknown"
    started_at: float | None = None
    timeout_at: float | None = None
    finished_at: float | None = None
    live_url: str | None = None
    cost_usd: float = 0.0
    proxy_cost_usd: float = 0.0
    proxy_mb: float = 0.0
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def total_cost_usd(self) -> float:
        return self.cost_usd + self.proxy_cost_usd

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    def public(self) -> dict[str, object]:
        """Safe for logs and agent-visible output: never includes the CDP URL."""
        return {
            "platform_session_id": self.id,
            "status": self.status,
            "timeout_at": self.timeout_at,
            "started_at": self.started_at,
            "cost_usd": round(self.total_cost_usd, 6),
            "proxy_mb": self.proxy_mb,
            "metadata": self.metadata,
        }


@dataclass
class AccountInfo:
    project_id: str | None = None
    concurrent_session_limit: int | None = None
    active_session_count: int | None = None
    credit_balance_usd: float | None = None
    raw: dict[str, object] = field(default_factory=dict)


@runtime_checkable
class Provider(Protocol):
    name: str
    supports_profiles: bool
    supports_custom_proxy: bool

    async def create_session(self, spec: SessionSpec) -> ProviderSession: ...

    async def get_session(self, session_id: str) -> ProviderSession | None: ...

    async def list_sessions(
        self, *, active_only: bool = True, limit: int = 500
    ) -> list[ProviderSession]: ...

    async def stop_session(self, session_id: str) -> bool: ...

    async def account_info(self) -> AccountInfo: ...

    async def aclose(self) -> None: ...
