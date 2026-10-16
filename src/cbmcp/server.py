"""MCP server entry point (stdio).

stdout belongs to the MCP protocol; every log line goes to stderr. Startup
recovery runs inside the lifespan *before* the first tool call is served, so a
crash in a previous process cannot leave a cloud browser billing while this one
cheerfully accepts new work.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from mcp.server.mcpserver import MCPServer

from .app import App, set_app
from .config import get_settings
from .log import get_logger, setup_logging
from .tools import register_all

log = get_logger("server")

INSTRUCTIONS = """Cloud browser gateway for parallel agents.

Rent a browser with `browser_acquire`, drive it with the `page_*` tools, observe
traffic with `net_*`, and debug JavaScript with `js_*`. Release it with
`browser_release` the moment you are finished.

Rules that matter:

* A cloud browser bills for wall-clock time. Acquire late, release early. Only
  acquire one when plain HTTP requests will not do -- when you need JavaScript
  execution, dynamic rendering, a login flow, or network/JS debugging.
* Every browser tool needs the `lease` token from `browser_acquire`. There is no
  implicit "current browser": this server runs many agents' browsers at once and
  the token is what keeps them apart. Never share or guess a token.
* If `browser_acquire` returns status="queued", poll `browser_queue_status` with
  the ticket. Do not call `browser_acquire` again -- that can start a second
  paid session.
* Pass `idempotency_key` on `browser_acquire` so a retry reuses the same browser.
* Leases are reclaimed automatically when idle, so nothing is left running if you
  stop. Call `browser_renew` before a long stretch of analysis.
* Work only on hosts you are authorised to test. A lease can be pinned to a host
  allowlist with `scope_allow`, enforced on every request.

To understand how a page builds a request (a signature, token or nonce):
`js_search` to find the code, `js_hook` to record calls with their stacks,
`js_breakpoint(kind='xhr')` to stop where a request is built, then
`js_pause_state` and `js_eval_frame` to read local and closure variables.
Page tools are blocked while execution is paused -- call `js_step` to continue.
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
    try:
        build_server().run()
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
