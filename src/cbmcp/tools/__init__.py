"""Tool registration.

Groups can be switched off in configuration to keep the agent's tool list (and
therefore its context) small when a deployment does not need them.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

from ..config import Settings
from . import admin_tools, inspect_tools, js_tools, lease_tools, net_tools, page_tools, recon_tools, state_tools


def register_all(mcp: MCPServer, settings: Settings) -> list[str]:
    registered = ["lease", "page", "inspect", "network", "storage"]
    lease_tools.register(mcp)
    page_tools.register(mcp)
    inspect_tools.register(mcp)
    net_tools.register(mcp)
    state_tools.register(mcp)
    if settings.enable_debugger_tools:
        js_tools.register(mcp)
        recon_tools.register(mcp)
        registered.append("javascript")
    if settings.enable_admin_tools:
        admin_tools.register(mcp)
        registered.append("admin")
    return registered
