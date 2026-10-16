"""Budget control with pre-flight reservation.

The naive design -- "add up what we've spent, compare to the cap" -- fails under
concurrency, which is exactly the condition this gateway is built for: twenty
simultaneous acquires all read the same "spent so far" and all pass.

So money is *reserved* at allocation time, at the worst-case price of the lease
(full platform timeout at the browser rate, plus a proxy allowance), and settled
against the provider's reported cost on release. The reservation is held in the
lease row, so it survives a restart and is visible to ``admin_stats``.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Settings
from .errors import BudgetExceeded
from .log import get_logger
from .providers.base import NetworkSpec
from .store import Store

log = get_logger("budget")

# Rough proxy allowance per lease when a billed proxy is in play, in megabytes.
# Only used for the reservation; the real figure comes back as proxyUsedMb.
_PROXY_ALLOWANCE_MB = 50.0


@dataclass
class BudgetDecision:
    allowed: bool
    reserve_usd: float
    reason: str = ""
    day_spent: float = 0.0
    month_spent: float = 0.0
    reserved_outstanding: float = 0.0


class BudgetGuard:
    def __init__(self, settings: Settings, store: Store) -> None:
        self.s = settings
        self.store = store

    def worst_case_cost(self, timeout_minutes: int, network: NetworkSpec) -> float:
        browser = (timeout_minutes * 60) * self.s.price_browser_usd_per_second
        proxy = 0.0
        if network.kind in ("platform_proxy", "custom_proxy"):
            proxy = (_PROXY_ALLOWANCE_MB / 1024.0) * self.s.price_proxy_usd_per_gb
        return browser + proxy

    async def check(
        self,
        timeout_minutes: int,
        network: NetworkSpec,
        *,
        extra_outstanding: float = 0.0,
    ) -> BudgetDecision:
        """
        ``extra_outstanding`` is money already committed by an acquire that holds
        a slot but has not yet written its reservation row. Without it, callers
        racing through this check all read the same "spent so far" and all pass.
        """
        reserve = self.worst_case_cost(timeout_minutes, network)
        day = await self.store.usage_for_day()
        month = await self.store.usage_for_month()
        outstanding = await self.store.reserved_total() + max(0.0, extra_outstanding)

        day_spent = float(day["cost_usd"])
        month_spent = float(month["cost_usd"])

        decision = BudgetDecision(
            allowed=True,
            reserve_usd=reserve,
            day_spent=day_spent,
            month_spent=month_spent,
            reserved_outstanding=outstanding,
        )
        if not self.s.budget_enforce:
            return decision

        if self.s.budget_daily_usd > 0 and day_spent + outstanding + reserve > self.s.budget_daily_usd:
            decision.allowed = False
            decision.reason = (
                f"daily budget ${self.s.budget_daily_usd:.2f} would be exceeded "
                f"(spent ${day_spent:.4f}, committed ${outstanding:.4f}, "
                f"this lease reserves ${reserve:.4f})"
            )
            return decision

        if (
            self.s.budget_monthly_usd > 0
            and month_spent + outstanding + reserve > self.s.budget_monthly_usd
        ):
            decision.allowed = False
            decision.reason = (
                f"monthly budget ${self.s.budget_monthly_usd:.2f} would be exceeded "
                f"(spent ${month_spent:.4f}, committed ${outstanding:.4f})"
            )
        return decision

    async def require(
        self,
        timeout_minutes: int,
        network: NetworkSpec,
        *,
        extra_outstanding: float = 0.0,
    ) -> BudgetDecision:
        decision = await self.check(
            timeout_minutes, network, extra_outstanding=extra_outstanding
        )
        if not decision.allowed:
            await self.store.bump("budget_rejections")
            await self.store.audit(
                "budget.reject", outcome="denied", reason=decision.reason
            )
            raise BudgetExceeded(decision.reason)
        return decision

    async def settle(
        self,
        lease_id: str,
        *,
        reserved: float,
        actual_usd: float | None,
        browser_seconds: float,
        proxy_mb: float,
    ) -> float:
        """Replace a reservation with the real figure.

        When the provider has not reported a cost yet (it often lags a few
        seconds behind the stop), the elapsed-time estimate is used so the ledger
        is never quietly understated.
        """
        estimated = browser_seconds * self.s.price_browser_usd_per_second
        if proxy_mb:
            estimated += (proxy_mb / 1024.0) * self.s.price_proxy_usd_per_gb
        cost = actual_usd if (actual_usd and actual_usd > 0) else estimated

        await self.store.add_usage(
            cost_usd=cost, browser_seconds=browser_seconds, proxy_mb=proxy_mb, sessions=1
        )
        await self.store.update_lease(
            lease_id, cost_actual_usd=cost, cost_reserved_usd=cost, proxy_mb=proxy_mb
        )
        log.info(
            "settled %s: $%.6f (reserved $%.6f, %.1fs, %.1fMB proxy)",
            lease_id,
            cost,
            reserved,
            browser_seconds,
            proxy_mb,
        )
        return cost

    async def snapshot(self) -> dict[str, object]:
        day = await self.store.usage_for_day()
        month = await self.store.usage_for_month()
        outstanding = await self.store.reserved_total()
        return {
            "enforced": self.s.budget_enforce,
            "daily_cap_usd": self.s.budget_daily_usd,
            "monthly_cap_usd": self.s.budget_monthly_usd,
            "today": {
                "cost_usd": round(float(day["cost_usd"]), 6),
                "browser_minutes": round(float(day["browser_seconds"]) / 60, 2),
                "proxy_mb": round(float(day["proxy_mb"]), 2),
                "sessions": int(day["sessions"]),
            },
            "month": {
                "cost_usd": round(float(month["cost_usd"]), 6),
                "browser_minutes": round(float(month["browser_seconds"]) / 60, 2),
                "proxy_mb": round(float(month["proxy_mb"]), 2),
                "sessions": int(month["sessions"]),
            },
            "committed_but_unsettled_usd": round(outstanding, 6),
            "price_browser_usd_per_hour": self.s.price_browser_usd_per_hour,
        }
