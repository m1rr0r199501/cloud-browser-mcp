"""Configuration. Everything tunable lives here; nothing is hardcoded in logic."""

from __future__ import annotations

import contextlib
import os
import socket
import uuid
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

NetworkMode = Literal["direct", "platform_proxy", "custom_proxy"]
ScopeMode = Literal["off", "warn", "enforce"]
CaptureBodies = Literal["none", "interesting", "all"]


def _default_data_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "cbmcp"


def _host_label() -> str:
    raw = socket.gethostname().split(".")[0][:40] or "host"
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in raw)


def _stable_owner_tag(data_dir: Path) -> str:
    """The default owner tag, fixed on first run and then read back.

    It used to be derived from the hostname on every start. On a Mac the
    hostname follows the network (``MacBook-Pro`` at home, ``mbp-1234`` on
    office DHCP), so the tag silently changed and reconciliation stopped
    recognising sessions this machine had left running. The first value is now
    written to the data dir and reused, so it survives network changes.
    """
    path = data_dir / "owner_tag"
    try:
        existing = path.read_text("utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass
    tag = f"cbmcp-{_host_label()}"
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(tag + "\n", "utf-8")
    except OSError:
        pass  # read-only data dir: fall back to the hostname-derived tag
    return tag


def _project_root() -> Path:
    # src/cbmcp/config.py -> src/cbmcp -> src -> project root
    return Path(__file__).resolve().parents[2]


def _env_files() -> tuple[str, ...]:
    """Where to look for .env.

    An MCP client launches the server with whatever working directory it likes,
    so a CWD-relative .env alone would leave the API key unfound and the server
    dead on arrival. The project directory is checked as well.
    """
    candidates = [Path(".env"), _project_root() / ".env"]
    seen: list[str] = []
    for path in candidates:
        text = str(path)
        if text not in seen:
            seen.append(text)
    return tuple(seen)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="CBMCP_",
        env_file=_env_files(),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- credentials -------------------------------------------------------
    browser_use_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("CBMCP_BROWSER_USE_API_KEY", "BROWSER_USE_API_KEY"),
    )
    browser_use_base_url: str = "https://api.browser-use.com"
    provider: Literal["browseruse", "local"] = "browseruse"

    # --- concurrency -------------------------------------------------------
    max_concurrency: int = Field(default=6, ge=1, le=200)
    acquire_block_seconds: float = Field(default=25.0, ge=0.0, le=120.0)
    queue_timeout_seconds: float = Field(default=600.0, ge=5.0)
    respect_platform_limit: bool = True
    # Every Claude Code window starts its own gateway. With this on (the
    # default), max_concurrency is one ceiling shared by all gateways on this
    # data dir rather than a separate ceiling per window, so three windows do
    # not try to run 3x the browsers the account allows.
    share_concurrency_across_gateways: bool = True

    # --- lease lifecycle ---------------------------------------------------
    idle_timeout_seconds: float = Field(default=300.0, ge=15.0)
    max_lease_seconds: float = Field(default=1800.0, ge=30.0)
    platform_timeout_minutes: int = Field(default=35, ge=1, le=240)
    reaper_interval_seconds: float = Field(default=15.0, ge=1.0)
    reconcile_interval_seconds: float = Field(default=120.0, ge=10.0)
    reconcile_on_startup: bool = True
    reconcile_stop_untagged: bool = False
    owner_tag: str = ""
    release_grace_seconds: float = Field(default=10.0, ge=0.0)

    # --- network -----------------------------------------------------------
    network_mode: NetworkMode = "direct"
    proxy_country_code: str = "us"
    custom_proxy_host: str = ""
    custom_proxy_port: int = 0
    custom_proxy_username: str = ""
    custom_proxy_password: str = ""
    custom_proxy_ignore_cert_errors: bool = False
    allow_lease_network_override: bool = False

    # --- budget ------------------------------------------------------------
    budget_daily_usd: float = Field(default=5.0, ge=0.0)
    budget_monthly_usd: float = Field(default=50.0, ge=0.0)
    price_browser_usd_per_hour: float = Field(default=0.02, ge=0.0)
    price_proxy_usd_per_gb: float = Field(default=8.0, ge=0.0)
    budget_enforce: bool = True

    # --- scope -------------------------------------------------------------
    scope_mode: ScopeMode = "warn"
    # NoDecode: these arrive as comma-separated strings, not JSON. Without it
    # pydantic-settings tries json.loads() before the validator below can run,
    # and an empty value crashes startup.
    scope_allow: Annotated[list[str], NoDecode] = Field(default_factory=list)
    scope_deny: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # --- output limits -----------------------------------------------------
    max_text_chars: int = Field(default=20_000, ge=500)
    max_snapshot_chars: int = Field(default=40_000, ge=500)
    max_body_bytes: int = Field(default=2 * 1024 * 1024, ge=1024)
    max_net_entries: int = Field(default=2000, ge=50)
    lease_disk_quota_bytes: int = Field(default=256 * 1024 * 1024, ge=1024 * 1024)
    screenshot_format: Literal["jpeg", "png"] = "jpeg"
    screenshot_quality: int = Field(default=70, ge=1, le=100)
    max_screenshot_bytes: int = Field(default=1_500_000, ge=10_000)

    # --- browser defaults --------------------------------------------------
    screen_width: int = Field(default=1440, ge=320, le=6144)
    screen_height: int = Field(default=900, ge=320, le=3456)
    solve_captchas: bool = False
    pdf_renderer_enabled: bool = True
    enable_recording: bool = False
    capture_bodies: CaptureBodies = "interesting"
    # How alert/confirm/prompt dialogs are answered until page_dialog changes
    # it. "dismiss" never confirms anything the agent did not ask to confirm.
    dialog_default: Literal["dismiss", "accept"] = "dismiss"
    default_timeout_ms: int = Field(default=15_000, ge=500)
    navigation_timeout_ms: int = Field(default=45_000, ge=1000)

    # --- tool groups -------------------------------------------------------
    enable_debugger_tools: bool = True
    enable_admin_tools: bool = True
    # CDP debugging is the main purpose here, so full instrumentation is on from
    # page load by default. "hooks" gives function hooking without
    # Runtime.enable/Debugger.enable; "off" adds nothing observable to the page.
    debug_default: Literal["off", "hooks", "full"] = "full"

    # --- infra -------------------------------------------------------------
    data_dir: Path = Field(default_factory=_default_data_dir)
    # page_upload may only send files from under this directory (default:
    # <data_dir>/uploads). The tool is driven by an agent that reads untrusted
    # pages, so without a root a prompt injection could have it upload
    # ~/.ssh/id_ed25519 or a Keychain export to an attacker's form.
    upload_dir: Path | None = None
    log_level: str = "INFO"
    instance_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])

    @field_validator("scope_allow", "scope_deny", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        if isinstance(v, str):
            return [p.strip().lower() for p in v.split(",") if p.strip()]
        return v

    @field_validator("data_dir", mode="before")
    @classmethod
    def _blank_data_dir(cls, v: object) -> object:
        if v in (None, ""):
            return _default_data_dir()
        # "~/..." in .env is not expanded by the shell, so do it here.
        return Path(str(v)).expanduser()

    @field_validator("upload_dir", mode="before")
    @classmethod
    def _blank_upload_dir(cls, v: object) -> object:
        if v in (None, ""):
            return None
        return Path(str(v)).expanduser()

    @field_validator("custom_proxy_port", mode="before")
    @classmethod
    def _blank_port(cls, v: object) -> object:
        # A commented-out proxy in .env leaves this as an empty string, which
        # would otherwise fail validation and block startup entirely.
        return 0 if v in (None, "") else v

    @field_validator("proxy_country_code")
    @classmethod
    def _lower_cc(cls, v: str) -> str:
        return v.strip().lower()

    @model_validator(mode="after")
    def _finish(self) -> Settings:
        if not self.owner_tag:
            object.__setattr__(self, "owner_tag", _stable_owner_tag(self.data_dir))
        # The platform caps its hard stop at 240 minutes, so a longer lease
        # could never be backed by it; cap the lease instead.
        if self.max_lease_seconds > (240 - 2) * 60:
            object.__setattr__(self, "max_lease_seconds", float((240 - 2) * 60))
        # The platform hard stop must outlive the lease we hand out, otherwise the
        # browser dies under the agent's feet mid-task.
        min_platform = int(self.max_lease_seconds // 60) + 2
        if self.platform_timeout_minutes < min_platform:
            object.__setattr__(
                self, "platform_timeout_minutes", min(240, max(1, min_platform))
            )
        if self.network_mode == "custom_proxy" and not (
            self.custom_proxy_host and self.custom_proxy_port
        ):
            raise ValueError(
                "network_mode=custom_proxy requires CBMCP_CUSTOM_PROXY_HOST and _PORT"
            )
        return self

    # --- derived -----------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / "cbmcp.sqlite3"

    @property
    def artifacts_dir(self) -> Path:
        return self.data_dir / "artifacts"

    @property
    def heartbeat_max_age_seconds(self) -> float:
        """A sibling gateway silent for longer than this is presumed dead."""
        return max(60.0, self.reaper_interval_seconds * 4)

    @property
    def uploads_dir(self) -> Path:
        return self.upload_dir or (self.data_dir / "uploads")

    @property
    def price_browser_usd_per_second(self) -> float:
        return self.price_browser_usd_per_hour / 3600.0

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        # Captured bodies, cookies and hook records land here; keep them
        # private to this user (the default umask leaves them world-readable).
        if os.name == "posix":
            with contextlib.suppress(OSError):
                self.artifacts_dir.chmod(0o700)

    def redacted(self) -> dict[str, object]:
        data = self.model_dump(mode="json")
        for key in (
            "browser_use_api_key",
            "custom_proxy_username",
            "custom_proxy_password",
        ):
            if data.get(key):
                data[key] = "***"
        return data


_settings: Settings | None = None


def get_settings(reload: bool = False) -> Settings:
    global _settings
    if _settings is None or reload:
        _settings = Settings()
    return _settings
