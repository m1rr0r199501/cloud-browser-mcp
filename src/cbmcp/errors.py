"""Error taxonomy.

Every error an agent can hit carries a stable ``code`` plus a ``hint`` that tells
the model what to do next. Agents recover from errors far better when the error
itself names the remedy, so the hint is not optional decoration.
"""

from __future__ import annotations


class CbmcpError(Exception):
    code = "internal_error"
    hint = ""

    def __init__(self, message: str, *, hint: str | None = None, **detail: object) -> None:
        super().__init__(message)
        self.message = message
        if hint is not None:
            self.hint = hint
        self.detail = detail

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {"ok": False, "error": self.code, "message": self.message}
        if self.hint:
            out["hint"] = self.hint
        if self.detail:
            out["detail"] = self.detail
        return out


class ConfigError(CbmcpError):
    code = "config_error"
    hint = "Fix the server configuration (.env) and restart the MCP server."


class LeaseNotFound(CbmcpError):
    code = "lease_not_found"
    hint = (
        "The lease token is unknown. It may have been released or reclaimed. "
        "Pass the 'lease' value (lst_...) that browser_acquire returned, or call "
        "browser_acquire to get a new one. Do not reuse or guess tokens."
    )


class LeaseNotActive(CbmcpError):
    code = "lease_not_active"
    hint = "This lease is no longer usable. Call browser_acquire for a fresh browser."


class LeaseExpired(CbmcpError):
    code = "lease_expired"
    hint = (
        "The lease timed out (idle or max lifetime) and the cloud browser was stopped "
        "to avoid charges. Call browser_acquire again; call browser_renew during long tasks."
    )


class CapacityExhausted(CbmcpError):
    code = "capacity_exhausted"
    hint = "All browser slots are busy. Poll browser_queue_status with your ticket_id."


class ProfileInUse(CbmcpError):
    code = "profile_in_use"
    hint = (
        "Another browser is using this profile right now (one at a time, or the two "
        "would overwrite each other's saved logins). Wait for it to be released, use "
        "a different profile name, or omit `profile` for a clean browser."
    )


class BudgetExceeded(CbmcpError):
    code = "budget_exceeded"
    hint = (
        "The configured spend cap is reached, so no new paid session was created. "
        "Stop requesting browsers and report this to the operator."
    )


class ScopeViolation(CbmcpError):
    code = "scope_violation"
    hint = (
        "The target is outside this lease's authorised scope. Only test hosts you have "
        "written authorisation for; ask the operator to widen the scope if needed."
    )


class ProviderError(CbmcpError):
    code = "provider_error"
    hint = "The cloud browser platform rejected the request. Check the message and retry later."


class ProviderQuotaError(ProviderError):
    code = "provider_quota"
    hint = (
        "The cloud platform refused on quota/credit grounds. Do not retry in a loop; "
        "retrying costs money and will keep failing. Report this to the operator."
    )


class ProviderConcurrencyLimit(ProviderQuotaError):
    """The account is at its concurrent-session limit right now.

    Unlike running out of credits this clears by itself as other sessions end
    -- typically another Claude window's gateway on the same account -- so the
    pool requeues the request instead of failing it.
    """

    code = "provider_concurrency"
    hint = (
        "The cloud account is at its concurrent-session limit. The request was "
        "queued; poll browser_queue_status with your ticket_id."
    )


class BrowserConnectionError(CbmcpError):
    code = "browser_connection_error"
    hint = "The browser connection broke. Release this lease and acquire a new one."


class BrowserConnectionLost(CbmcpError):
    """Transient: the cloud browser is usually still running."""

    code = "browser_connection_lost"
    hint = (
        "Retry the call: the gateway reconnects to the same browser (tabs, cookies and "
        "logins are kept). If it keeps failing, release the lease and acquire a new browser."
    )


class DebuggerPaused(CbmcpError):
    code = "debugger_paused"
    hint = (
        "JavaScript execution is paused at a breakpoint, so the page cannot be driven. "
        "Inspect with js_pause_state / js_eval_frame, then call js_step(action='resume')."
    )


class DebuggerNotPaused(CbmcpError):
    code = "debugger_not_paused"
    hint = "Execution is running. Set a breakpoint and use js_pause_state to wait for a hit."


class RefNotFound(CbmcpError):
    code = "ref_not_found"
    hint = "The element ref is stale (the DOM changed). Call page_snapshot again for fresh refs."


class ToolTimeout(CbmcpError):
    code = "tool_timeout"
    hint = "The operation timed out. Retry with a longer timeout_ms or a narrower selector."


class NotFound(CbmcpError):
    code = "not_found"


class InvalidArgument(CbmcpError):
    code = "invalid_argument"
