"""Offline contracts; real cloud execution is scripts/recon_smoke.py."""

import pytest

from cbmcp.errors import InvalidArgument, ScopeViolation
from cbmcp.recon import capture_envelope, validate_config
from cbmcp.scope import ScopeRule


@pytest.fixture
def scope():
    return ScopeRule(mode="enforce", allow=["recon-fixture.invalid"], deny=[])


def test_scope_is_checked_before_mock_install(scope):
    with pytest.raises(ScopeViolation):
        validate_config({"origins": ["https://outside.invalid"]}, scope)


@pytest.mark.parametrize(
    "origin",
    [
        "https://user:pass@recon-fixture.invalid",
        "https://recon-fixture.invalid/",
        "https://recon-fixture.invalid/path",
        "file:///tmp/x",
        "https://recon-fixture.invalid?secret=x",
    ],
)
def test_origin_must_be_exact(origin, scope):
    with pytest.raises(InvalidArgument):
        validate_config({"origins": [origin]}, scope)


def test_no_broad_unconfigured_json_rewrite(scope):
    with pytest.raises(InvalidArgument):
        validate_config(
            {
                "origins": ["https://recon-fixture.invalid"],
                "neutralize": {"fields": ["code"], "success": 0},
            },
            scope,
        )


def test_nested_field_and_explicit_failure_values(scope):
    c = validate_config(
        {
            "origins": ["https://recon-fixture.invalid"],
            "neutralize": {
                "paths": ["/api/"],
                "fields": ["status.code"],
                "failure_values": [401],
                "success": 0,
            },
        },
        scope,
    )
    assert c["mode"] == "forward"
    assert c["neutralize"]["fields"] == ["status.code"]


def test_original_and_presented_responses_are_independent_capture_envelopes():
    event = {
        "request": {
            "url": "https://recon-fixture.invalid/api/item",
            "method": "POST",
            "body": {"text": '{"title":"synthetic"}'},
            "headers": {},
        },
        "response": {"status": 200, "body": {"text": '{"code":0}'}},
        "request_sent": True,
    }
    raw = capture_envelope(
        event,
        {"status": 401, "body": {"text": '{"code":401}'}},
        "target_runtime",
        "a" * 64,
        "https://recon-fixture.invalid",
        1,
    )
    mock = capture_envelope(
        event, event, "mock_runtime", "a" * 64, "https://recon-fixture.invalid", 1
    )
    assert raw["tool_results"][0]["result"]["status"] == 401
    assert raw["tool_results"][1]["result"]["json"] == {"code": 401}
    assert mock["tool_results"][0]["result"]["status"] == 200
    assert mock["context"]["source_kind"] == "mock_runtime"
    assert not raw["backend_verified"] and not mock["backend_verified"]


async def test_native_registration_exposes_recon_with_existing_tools(settings):
    from mcp.server.mcpserver import MCPServer

    from cbmcp.tools import register_all

    server = MCPServer("test")
    register_all(server, settings)
    names = {tool.name for tool in await server.list_tools()}
    assert {"js_recon", "js_hook", "browser_acquire", "browser_release"} <= names
    assert len(names) == 52


async def test_disabling_javascript_group_also_hides_recon(settings):
    from mcp.server.mcpserver import MCPServer

    from cbmcp.tools import register_all

    settings.enable_debugger_tools = False
    server = MCPServer("test")
    register_all(server, settings)
    names = {tool.name for tool in await server.list_tools()}
    assert "js_recon" not in names and "js_hook" not in names
    assert "browser_acquire" in names
