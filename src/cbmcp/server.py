"""MCP server entry point (stdio).

stdout belongs to the MCP protocol; every log line goes to stderr. Startup
recovery runs inside the lifespan *before* the first tool call is served, so a
crash in a previous process cannot leave a cloud browser billing while this one
cheerfully accepts new work.
"""

from __future__ import annotations

import contextlib
import os
import signal
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.server.mcpserver import MCPServer

from .app import App, set_app
from .config import get_settings
from .log import get_logger, setup_logging
from .tools import register_all

log = get_logger("server")

# Set once the lifespan starts tearing down; read by the SIGTERM handler.
_shutting_down = False

INSTRUCTIONS = """Cloud browser gateway for parallel agents.

Whenever a task needs a real web browser -- opening or operating a website,
logging in, filling in forms, clicking through a flow, taking screenshots,
downloading files, or inspecting a page's network traffic or JavaScript -- use
this server, starting with `browser_acquire`.

Rent a browser with `browser_acquire`, drive it with the `page_*` tools, observe
traffic with `net_*`, and debug JavaScript with `js_*`. Release it with
`browser_release` the moment you are finished.

Rules that matter:

* A cloud browser bills for wall-clock time. Acquire late, release early. Only
  acquire one when plain HTTP requests will not do -- when you need JavaScript
  execution, dynamic rendering, a login flow, or network/JS debugging.
* Every browser tool needs the `lease` token from `browser_acquire` (it starts
  with `lst_`; `lease_id`, starting `lse_`, is only a label and is refused).
  There is no implicit "current browser": this server runs many agents'
  browsers at once and the token is what keeps them apart. Never share or
  guess a token.
* If `browser_acquire` returns status="queued", poll `browser_queue_status` with
  the ticket. Do not call `browser_acquire` again -- that can start a second
  paid session.
* Pass `idempotency_key` on `browser_acquire` so a retry reuses the same browser.
* Leases are reclaimed automatically when idle, so nothing is left running if you
  stop. Call `browser_renew` before a long stretch of analysis.
* Work only on hosts you are authorised to test. A lease can be pinned to a host
  allowlist with `scope_allow`, enforced on every request.
* `browser_acquire(profile="name")` keeps cookies, logins and local storage
  between browsers: the state is saved when the browser is released and comes
  back the next time that profile is used. One browser per profile at a time.
* `browser_state(action="export", origins=[...])` saves a private state file.
  Use `action="import"` with its hash, the observed SSO origin and selected
  account_key to merge SSO state into your current account-specific profile.
  It preserves unrelated state and does not switch profiles or need another
  browser. Different actual accounts use different profiles. `action="restore"`
  remains for empty leases before navigation. Verify the target identity/role.
* `error="browser_connection_lost"` is transient: the gateway reconnects to the
  same cloud browser on the next call, so retry the call. A `rate_limit_warning`
  means the site is throttling this browser -- slow down instead of retrying.

Drive the page the way a person would: `page_snapshot` for refs, then
`page_click` / `page_type` / `page_hover` / `page_drag`; `page_mouse` clicks
screenshot coordinates (canvas, maps, image challenges). `confirm()` dialogs are
answered Cancel unless you call `page_dialog(action='accept')` first. Action
results report new tabs, dialogs and downloads; files land in `page_downloads`.
Browser slots are shared with every other Claude window on this machine.

To understand how a page builds a request (a signature, token or nonce):
`js_search` to find the code, `js_hook` to record calls with their stacks,
`js_breakpoint(kind='xhr')` to stop where a request is built, then
`js_pause_state` and `js_eval_frame` to read local and closure variables.
Page tools are blocked while execution is paused -- call `js_step` to continue.

For client-side API recon, use `js_recon(action='install')` before navigation
to configure exact bootstrap stubs, explicit JSON response patching or offline
API fallback. Check its status after navigation, page its capture index, then
release the lease. Mock responses do not demonstrate backend authentication.
When exploring a client retry/fallback branch, the same config's `network_rules`
can abort a selected fetch/XHR with a browser network error or delay its normal
response. Save actual intervention state/provenance; rules are optional, not a
required step for every page. See docs/api-recon.md for matching and limits.
For iframe UI, `page_snapshot(frames_only=true)` gives a paged child-frame
directory; use a returned `frame_ref` to inspect that document, then its opaque
control refs with the ordinary actions. Refs are document-scoped; stale or
ambiguous refs never select the first match. See docs/frames.md.
"""


@asynccontextmanager
async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
    app = App()
    set_app(app)
    try:
        await app.start()
    except Exception:
        set_app(None)
        raise
    try:
        yield
    finally:
        global _shutting_down
        _shutting_down = True
        await app.stop()
        set_app(None)


def build_server() -> MCPServer:
    settings = get_settings()
    setup_logging(settings.log_level)
    mcp = MCPServer(
        "cloud-browser",
        instructions=INSTRUCTIONS,
        lifespan=lifespan,
    )
    groups = register_all(mcp, settings)
    log.info("registered tool groups: %s", ", ".join(groups))
    return mcp


def _raise_fd_limit(target: int = 4096) -> None:
    """Lift the soft open-file limit where the OS default is tiny.

    macOS gives processes started from a GUI app or launchd a soft limit of 256.
    Each lease holds a CDP websocket plus body files being written, and the
    local provider adds Chromium pipes, so a handful of parallel leases can hit
    "Too many open files". The hard limit is untouched.
    """
    try:
        import resource
    except ImportError:  # Windows
        return
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        want = target if hard == resource.RLIM_INFINITY else min(target, hard)
        if soft != resource.RLIM_INFINITY and soft < want:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
    except (ValueError, OSError) as exc:
        log.debug("could not raise RLIMIT_NOFILE: %s", exc)


def _install_signal_handlers() -> None:
    """Turn SIGTERM/SIGHUP into the same orderly shutdown as Ctrl-C.

    MCP clients on macOS and Linux end a server with SIGTERM (or SIGHUP when a
    terminal closes). The default action kills the process on the spot, so the
    lifespan never runs ``app.stop()`` and every lease keeps billing until the
    reaper of the next run -- or the platform timeout -- catches it.
    """
    if os.name != "posix":
        return

    def forward(_signum: int, _frame: object) -> None:
        # The usual sequence from an MCP client is: close stdin (shutdown
        # starts), then SIGTERM two seconds later. Turning that SIGTERM into a
        # second interrupt would cancel the shutdown that is already stopping
        # cloud browsers, so once it has started, let it run.
        if _shutting_down:
            return
        signal.raise_signal(signal.SIGINT)

    for sig in (signal.SIGTERM, signal.SIGHUP):
        with contextlib.suppress(ValueError, OSError):
            signal.signal(sig, forward)


def main() -> int:
    settings = get_settings()
    setup_logging(settings.log_level)
    if settings.provider == "browseruse" and not settings.browser_use_api_key:
        print(
            "cbmcp: BROWSER_USE_API_KEY is not set.\n"
            "Put it in .env (see .env.example) or export it, then restart.\n"
            "To develop without a cloud account, set CBMCP_PROVIDER=local.",
            file=sys.stderr,
        )
        return 2
    _raise_fd_limit()
    _install_signal_handlers()
    try:
        build_server().run()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
