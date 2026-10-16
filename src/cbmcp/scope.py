"""Authorised-target scope enforcement.

For authorised security research the blast radius matters legally, not just
technically. Checking the URL handed to ``page_navigate`` is not enough: page
JavaScript, redirects, form posts and third-party includes all reach hosts the
agent never named. So scope is enforced at the request layer (Playwright
routing) as well as at the navigation layer.

A lease may only ever *narrow* the operator's global allowlist, never widen it.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from .errors import ScopeViolation

# Schemes that never leave the browser, so they are always allowed.
_LOCAL_SCHEMES = {"about", "data", "blob", "chrome", "devtools", "javascript", "file"}


def normalise_host(host: str) -> str:
    host = (host or "").strip().lower().rstrip(".")
    if host.startswith("[") and host.endswith("]"):  # IPv6 literal
        return host
    if ":" in host and host.count(":") == 1:
        host = host.split(":", 1)[0]
    return host


def host_of(url: str) -> str | None:
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme and parsed.scheme.lower() in _LOCAL_SCHEMES:
        return None
    return normalise_host(parsed.hostname or "")


def match_pattern(host: str, pattern: str) -> bool:
    """``example.com`` matches the host and its subdomains.

    ``*.example.com`` matches subdomains only. ``*`` matches everything.
    """
    pattern = pattern.strip().lower().rstrip(".")
    if not pattern:
        return False
    if pattern == "*":
        return True
    if pattern.startswith("*."):
        suffix = pattern[2:]
        return host.endswith("." + suffix)
    return host == pattern or host.endswith("." + pattern)


@dataclass(frozen=True)
class ScopeRule:
    mode: str = "off"  # off | warn | enforce
    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()

    @property
    def restricted(self) -> bool:
        return self.mode != "off" and bool(self.allow or self.deny)

    def describe(self) -> str:
        if self.mode == "off":
            return "off (no host restriction)"
        allow = ", ".join(self.allow) if self.allow else "any"
        deny = f"; deny: {', '.join(self.deny)}" if self.deny else ""
        return f"{self.mode} -- allow: {allow}{deny}"

    def permits(self, url: str) -> tuple[bool, str]:
        host = host_of(url)
        if host is None:
            return True, "non-network scheme"
        if not host:
            return True, "no host"
        for pattern in self.deny:
            if match_pattern(host, pattern):
                return False, f"{host} matches deny pattern {pattern!r}"
        if self.allow and not any(match_pattern(host, p) for p in self.allow):
            return False, f"{host} is not in the allowlist"
        return True, "in scope"

    def enforce(self, url: str) -> None:
        if self.mode != "enforce":
            return
        ok, reason = self.permits(url)
        if not ok:
            raise ScopeViolation(
                f"out of scope: {reason}",
                url=url,
                scope=self.describe(),
            )

    def narrow(self, allow: list[str] | None, deny: list[str] | None, mode: str | None) -> ScopeRule:
        """Produce a lease scope that is at most as permissive as this one."""
        new_allow = tuple(p.strip().lower() for p in (allow or []) if p.strip())
        new_deny = tuple(p.strip().lower() for p in (deny or []) if p.strip())

        if self.allow and new_allow:
            # Every requested pattern must already be covered by the global allowlist.
            rejected = [
                p
                for p in new_allow
                if not any(
                    match_pattern(p.lstrip("*."), g) or p == g or g == "*" for g in self.allow
                )
            ]
            if rejected:
                raise ScopeViolation(
                    f"requested scope {rejected} is outside the operator's global allowlist",
                    global_allow=list(self.allow),
                    hint="A lease can only narrow CBMCP_SCOPE_ALLOW, never widen it.",
                )
            effective_allow = new_allow
        else:
            effective_allow = new_allow or self.allow

        effective_mode = mode or self.mode
        if self.mode == "enforce":
            effective_mode = "enforce"  # operators can tighten, leases cannot loosen
        if effective_mode == "off" and effective_allow:
            effective_mode = "enforce"

        return ScopeRule(
            mode=effective_mode,
            allow=effective_allow,
            deny=tuple(dict.fromkeys((*self.deny, *new_deny))),
        )


def global_scope(mode: str, allow: list[str], deny: list[str]) -> ScopeRule:
    return ScopeRule(mode=mode, allow=tuple(allow), deny=tuple(deny))
