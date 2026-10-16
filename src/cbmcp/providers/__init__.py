"""Provider registry."""

from __future__ import annotations

from ..config import Settings
from ..errors import ConfigError
from .base import (
    AccountInfo,
    CustomProxy,
    NetworkSpec,
    Provider,
    ProviderSession,
    SessionSpec,
)
from .browseruse import BrowserUseProvider
from .local import LocalChromiumProvider

__all__ = [
    "AccountInfo",
    "CustomProxy",
    "NetworkSpec",
    "Provider",
    "ProviderSession",
    "SessionSpec",
    "BrowserUseProvider",
    "LocalChromiumProvider",
    "build_provider",
    "network_spec_from_settings",
]


def build_provider(settings: Settings) -> Provider:
    if settings.provider == "browseruse":
        return BrowserUseProvider(
            settings.browser_use_api_key, settings.browser_use_base_url
        )
    if settings.provider == "local":
        return LocalChromiumProvider()
    raise ConfigError(f"unknown provider {settings.provider!r}")


def network_spec_from_settings(settings: Settings, mode: str | None = None) -> NetworkSpec:
    kind = mode or settings.network_mode
    if kind == "direct":
        return NetworkSpec(kind="direct")
    if kind == "platform_proxy":
        return NetworkSpec(kind="platform_proxy", country_code=settings.proxy_country_code)
    if kind == "custom_proxy":
        if not (settings.custom_proxy_host and settings.custom_proxy_port):
            raise ConfigError(
                "network mode 'custom_proxy' needs CBMCP_CUSTOM_PROXY_HOST and _PORT"
            )
        return NetworkSpec(
            kind="custom_proxy",
            custom=CustomProxy(
                host=settings.custom_proxy_host,
                port=settings.custom_proxy_port,
                username=settings.custom_proxy_username or None,
                password=settings.custom_proxy_password or None,
                ignore_cert_errors=settings.custom_proxy_ignore_cert_errors,
            ),
        )
    raise ConfigError(f"unknown network mode {kind!r}")
