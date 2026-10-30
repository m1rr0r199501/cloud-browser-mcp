"""Native state artifacts for existing cloud leases; no separate browser driver."""
from mcp.server.mcpserver import MCPServer

from ..errors import InvalidArgument
from ..storage_state import export_state, import_state, restore_state
from ._common import guarded, use_lease


def register(mcp: MCPServer) -> None:
    @mcp.tool(description=(
        "Export scoped Cookie/localStorage state to a private hashed file; import "
        "one SSO origin into the current profile without clearing other state; or "
        "restore a file into an empty lease before navigation. Returns metadata, never credentials. "
        "Restored state does not prove backend identity or shared application permissions."
    ))
    @guarded
    async def browser_state(
        lease: str,
        action: str = "export",
        origins: list[str] | None = None,
        state_path: str | None = None,
        expected_hash: str | None = None,
        account_key: str | None = None,
    ) -> dict:
        """
        Args:
            lease: Actual lease token from browser_acquire.
            action: export, import (merge SSO into current profile), or restore (empty context).
            origins: Exact HTTP(S) origins; required for export and import. Import
                needs exactly the observed SSO origin. Restore may select
                a scoped subset, including a sibling to which a parent-domain cookie applies.
            state_path: Import/restore: actual gateway-readable exported file path.
            expected_hash: Import/restore: actual SHA256 returned when the file was registered.
            account_key: Account label for export/import context. Import reports this label
                and the file's source_account_key for the agent to interpret, without a label-match gate.
        """
        async with use_lease(lease) as (_, session):
            if action == "export":
                return await export_state(session, origins, account_key)
            if action == "restore":
                return await restore_state(session, state_path, expected_hash, origins)
            if action == "import":
                return await import_state(session, state_path, expected_hash, origins, account_key)
            raise InvalidArgument("action must be export, import or restore")
