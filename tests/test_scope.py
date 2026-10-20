"""Scope rules: a lease may narrow the operator's allowlist, never widen it."""

from __future__ import annotations

import pytest

from cbmcp.errors import ScopeViolation
from cbmcp.scope import ScopeRule, global_scope, host_of, match_pattern


def test_bare_domain_covers_subdomains():
    assert match_pattern("example.com", "example.com")
    assert match_pattern("api.example.com", "example.com")
    assert not match_pattern("notexample.com", "example.com")
    assert not match_pattern("example.com.evil.net", "example.com")


def test_wildcard_matches_subdomains_only():
    assert match_pattern("api.example.com", "*.example.com")
    assert not match_pattern("example.com", "*.example.com")


def test_host_extraction_ignores_non_network_schemes():
    assert host_of("https://Example.COM:8443/path") == "example.com"
    assert host_of("about:blank") is None
    assert host_of("data:text/html,hi") is None
    assert host_of("blob:https://example.com/abc") is None


def test_enforce_blocks_out_of_scope():
    rule = ScopeRule(mode="enforce", allow=("example.com",))
    assert rule.permits("https://api.example.com/x")[0]
    assert not rule.permits("https://evil.net/x")[0]
    with pytest.raises(ScopeViolation):
        rule.enforce("https://evil.net/x")
    rule.enforce("https://example.com/ok")


def test_lease_cannot_switch_off_operator_warnings():
    lease = global_scope("warn", [], []).narrow(None, None, "off")
    assert lease.mode == "warn"
    assert global_scope("off", [], []).narrow(None, None, "enforce").mode == "enforce"


def test_warn_mode_does_not_raise():
    rule = ScopeRule(mode="warn", allow=("example.com",))
    assert not rule.permits("https://evil.net")[0]
    rule.enforce("https://evil.net")  # warn mode never blocks


def test_deny_beats_allow():
    rule = ScopeRule(mode="enforce", allow=("example.com",), deny=("admin.example.com",))
    assert rule.permits("https://www.example.com")[0]
    assert not rule.permits("https://admin.example.com")[0]


def test_lease_can_narrow_the_global_allowlist():
    operator = global_scope("enforce", ["example.com", "test.internal"], [])
    lease = operator.narrow(["api.example.com"], None, None)
    assert lease.allow == ("api.example.com",)
    assert lease.permits("https://api.example.com/x")[0]
    # Narrowing means the rest of the operator's allowlist is now out of reach.
    assert not lease.permits("https://other.example.com/x")[0]


def test_lease_cannot_widen_the_global_allowlist():
    operator = global_scope("enforce", ["example.com"], [])
    with pytest.raises(ScopeViolation) as exc:
        operator.narrow(["unrelated.net"], None, None)
    assert "outside the operator's global allowlist" in str(exc.value)


def test_lease_cannot_downgrade_enforcement():
    operator = global_scope("enforce", ["example.com"], [])
    lease = operator.narrow(None, None, "off")
    assert lease.mode == "enforce", "a lease must not be able to turn enforcement off"


def test_lease_deny_list_is_additive():
    operator = global_scope("enforce", ["example.com"], ["admin.example.com"])
    lease = operator.narrow(None, ["beta.example.com"], None)
    assert set(lease.deny) == {"admin.example.com", "beta.example.com"}


def test_requesting_scope_turns_enforcement_on():
    operator = global_scope("off", [], [])
    lease = operator.narrow(["example.com"], None, None)
    assert lease.mode == "enforce"
    assert not lease.permits("https://elsewhere.net")[0]


def test_lease_allowlist_is_enforced_under_an_operator_warn_mode():
    """Reported from real use: scope_allow under CBMCP_SCOPE_MODE=warn let
    redirects leave the list. Naming an allowlist now means enforcing it."""
    operator = global_scope("warn", [], [])
    lease = operator.narrow(["example.com"], None, None)
    assert lease.mode == "enforce"
    with pytest.raises(ScopeViolation):
        lease.enforce("https://elsewhere.net/")
    # Asking for "warn" explicitly is still honoured (it is the operator's mode).
    assert operator.narrow(["example.com"], None, "warn").mode == "warn"
    # No allowlist on the lease: the operator's mode stands.
    assert operator.narrow(None, None, None).mode == "warn"


def test_default_scope_mode_is_enforce():
    from cbmcp.config import Settings

    assert Settings.model_fields["scope_mode"].default == "enforce"


def test_unrestricted_scope_permits_everything():
    rule = global_scope("off", [], [])
    assert rule.restricted is False
    assert rule.permits("https://anything.example")[0]
