"""Selected-request control flow/provenance; real Chromium is tested by the cloud smoke."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cbmcp.errors import InvalidArgument
from cbmcp.netlog import NetworkRecorder
from cbmcp.recon import Recon, validate_config
from cbmcp.scope import ScopeRule


ORIGIN = "https://recon-fixture.invalid"


class Context:
    def __init__(self):
        self.routes = []
        self.scripts = []

    async def route(self, pattern, handler):
        self.routes.append((pattern, handler))

    async def unroute(self, pattern, handler):
        self.routes.remove((pattern, handler))

    async def expose_binding(self, name, callback):
        pass

    async def add_init_script(self, *, script):
        self.scripts.append(script)


class Route:
    def __init__(self, path="/api/selected", method="GET", *, origin=ORIGIN, frame_origin=ORIGIN, resource_type="fetch"):
        page = SimpleNamespace(url=frame_origin + "/fixture")
        self.request = SimpleNamespace(
            url=origin + path, method=method, resource_type=resource_type,
            frame=SimpleNamespace(url=page.url, page=page), headers={}, post_data=None,
            redirected_from=None, is_navigation_request=lambda: False,
        )
        self.actions = []

    async def abort(self, error):
        self.actions.append(("abort", error))

    async def fulfill(self, **response):
        self.actions.append(("fulfill", response))

    async def fallback(self):
        self.actions.append(("fallback", None))


@pytest.fixture
def setup(tmp_path):
    scope = ScopeRule(mode="enforce", allow=["recon-fixture.invalid"], deny=[])
    netlog = NetworkRecorder(lease_dir=tmp_path, max_entries=100, max_body_bytes=65536,
                            disk_quota_bytes=4_000_000)
    session = SimpleNamespace(dir=tmp_path, s=SimpleNamespace(lease_disk_quota_bytes=4_000_000),
                              scope=scope, netlog=netlog, context=Context(), pages={})

    def make(rule, **changes):
        config = validate_config({"origins": [ORIGIN], "network_rules": [rule], **changes}, scope)
        return Recon(session, config), session

    return make


@pytest.mark.parametrize("rule", [
    {}, {"path": "https://x/api", "error": "timedout"},
    {"path": "//other/api", "error": "timedout"},
    {"path": "/api?q=1", "error": "timedout"},
    {"path": "/api#fragment", "error": "timedout"},
    {"path": "/api", "error": "unknown"}, {"path": "/api", "error": []},
    {"path": "/api", "error": "timedout", "delay_ms": 1},
    {"path": "/api", "delay_ms": True}, {"path": "/api", "delay_ms": -1},
    {"path": "/api", "delay_ms": 120001}, {"path": "/api", "delay_ms": 1.5},
    {"path": "/api", "delay_ms": 0, "method": 12},
    {"path": "/api", "delay_ms": 0, "extra": "ignored?"},
])
def test_invalid_rule_is_rejected_before_install(setup, rule):
    with pytest.raises(InvalidArgument):
        setup(rule)


@pytest.mark.parametrize("error", ["connectionrefused", "timedout", "connectionreset", "internetdisconnected"])
async def test_error_goes_to_browser_driver_even_when_l1_matches(setup, error):
    recon, session = setup({"path": "/api/selected", "error": error},
                           stubs=[{"path": "/api/selected", "body": {"code": 0}}])
    route = Route()
    session.netlog._on_request(route.request)
    await recon._intercept(session, route)
    assert route.actions == [("abort", error)]
    row = recon.rows[0]
    assert row["request_sent"] is False
    assert row["network_intervention"]["state"] == "applied"
    assert {f["source_kind"] for f in row["files"]} == {"mock_runtime"}
    data = json.loads(next(recon.root.glob("*.json")).read_text())
    assert data["tool_results"][0]["result"]["failure"] == error
    assert data["tool_results"][0]["result"]["status"] is None
    entry = session.netlog._entry_of(route.request)
    assert entry.summary()["source_kind"] == "mock_runtime"
    assert entry.detail(False)["network_intervention"]["error"] == error


@pytest.mark.parametrize("route", [
    Route("/api/selected/child"), Route("/api/other"), Route(method="POST"),
    Route(origin="https://outside.invalid"), Route(frame_origin="https://outside.invalid"),
    Route(resource_type="document"), Route(resource_type="websocket"),
])
async def test_other_requests_are_left_to_existing_handlers(setup, route):
    recon, session = setup({"path": "/api/selected", "method": "GET", "error": "timedout"})
    await recon._intercept(session, route)
    assert route.actions == [("fallback", None)]
    assert not recon.rows


def test_matching_is_first_rule_exact_path_query_independent_and_method_optional(setup):
    recon, _ = setup({"path": "/api/selected", "error": "timedout"})
    recon.config["network_rules"].append({"path": "/api/selected", "method": "GET", "delay_ms": 1})
    assert recon.network_plan(Route("/api/selected?q=1").request)["rule_index"] == 0
    assert recon.network_plan(Route(method="POST").request)["error"] == "timedout"
    no_frame = Route().request
    no_frame.frame = None
    assert recon.network_plan(no_frame) is None


async def test_later_route_cannot_bypass_current_scope(setup):
    recon, session = setup({"path": "/api/selected", "delay_ms": 0})
    session.scope = ScopeRule(mode="enforce", allow=["other.invalid"], deny=[])
    route = Route()
    await recon._intercept(session, route)
    assert route.actions == [("abort", "blockedbyclient")]
    assert session.netlog.blocked
    assert not recon.rows


@pytest.mark.parametrize("mode", ["off", "warn"])
async def test_network_rule_preserves_the_existing_operator_scope_mode(setup, mode):
    recon, session = setup({"path": "/api/selected", "error": "timedout"})
    session.scope = ScopeRule(mode=mode, allow=["other.invalid"], deny=[])
    route = Route()
    await recon._intercept(session, route)
    assert route.actions == [("abort", "timedout")]
    assert not session.netlog.blocked
    assert recon.rows[0]["network_intervention"]["state"] == "applied"


async def test_delay_preserves_l1_response_and_never_labels_it_as_upstream(setup, tmp_path):
    recon, session = setup({"path": "/api/selected", "delay_ms": 25},
                           stubs=[{"path": "/api/selected", "body": {"value": 17}, "status": 202}])
    route = Route()
    # Annotation must work both before and after the recorder's request event.
    session.netlog.mark_intervention(route.request, {"state": "pending"}, "mock_runtime")
    session.netlog._on_request(route.request)
    await recon._intercept(session, route)
    action, response = route.actions[0]
    assert action == "fulfill" and response["status"] == 202
    assert json.loads(response["body"]) == {"value": 17}
    effect = recon.rows[0]["network_intervention"]
    assert effect["elapsed_ms"] >= 20 and effect["state"] == "applied"
    assert recon.rows[0]["layer"] == "L1"
    assert {f["source_kind"] for f in recon.rows[0]["files"]} == {"mock_runtime"}
    har_path = tmp_path / "test.har"
    await session.netlog.export_har(har_path)
    entry = json.loads(har_path.read_text())["log"]["entries"][0]
    assert entry["_sourceKind"] == "mock_runtime"
    assert entry["_networkIntervention"]["delay_ms"] == 25


async def test_delay_forward_keeps_l2_for_the_shim_and_does_not_invent_response(setup):
    recon, session = setup({"path": "/api/selected", "delay_ms": 0}, neutralize={
        "paths": ["/api/selected"], "fields": ["code"], "failure_values": [401], "success": 0})
    route = Route()
    session.netlog._on_request(route.request)
    await recon._intercept(session, route)
    assert route.actions == [("fallback", None)]
    assert recon.rows[0]["request_sent"] is None
    control = json.loads(Path(recon.rows[0]["files"][0]["path"]).read_text())
    assert control["source_kind"] == "mock_runtime" and not control["material"].get("response")
    assert not any(f["source_kind"] == "target_runtime" for f in recon.rows[0]["files"])
    assert session.netlog._entry_of(route.request).source_kind == "target_runtime"


@pytest.mark.parametrize("path,expected", [("/api/list", []), ("/api/detail", {})])
async def test_delay_offline_preserves_l3_shape(setup, path, expected):
    recon, session = setup({"path": path, "delay_ms": 0}, mode="offline")
    route = Route(path)
    await recon._intercept(session, route)
    assert json.loads(route.actions[0][1]["body"])["data"] == expected
    assert recon.rows[0]["layer"] == "L3" and recon.rows[0]["request_sent"] is False


async def test_release_cancels_pending_delay_and_removes_only_own_route(setup):
    recon, session = setup({"path": "/api/selected", "delay_ms": 120000})
    unrelated = object()
    session.context.routes.append(("**/*", unrelated))
    await recon.attach(session)
    await recon.attach(session)
    assert len(session.context.routes) == 2
    route = Route()
    task = asyncio.create_task(recon._intercept(session, route))
    for _ in range(20):
        if recon._route_tasks:
            break
        await asyncio.sleep(0)
    assert recon._route_tasks
    await asyncio.wait_for(recon.detach(), 1)
    assert task.cancelled() and not recon._route_tasks
    assert route.actions == [("abort", "aborted")]
    assert recon.rows[0]["network_intervention"]["state"] == "cancelled"
    assert session.context.routes == [("**/*", unrelated)]
    session.context = Context()
    await recon.attach(session)
    assert len(session.context.routes) == 1 and not recon._closing


async def test_reconnect_can_replace_an_unresponsive_old_route_connection(settings, tmp_path):
    from cbmcp.session import BrowserSession

    session = BrowserSession(lease_id="test", settings=settings, playwright=None,
                             scope=ScopeRule(mode="off"), work_dir=tmp_path)
    session.browser = SimpleNamespace(close=AsyncMock())
    session.recon = SimpleNamespace(detach=AsyncMock(side_effect=TimeoutError()), attach=AsyncMock())
    session._open_cdp = AsyncMock(return_value=SimpleNamespace())
    session._attach = AsyncMock()
    session._prove_drivable = AsyncMock()
    await session.reconnect("wss://synthetic.invalid")
    assert session.reconnects == 1 and session.connected
    session.recon.attach.assert_awaited_once_with(session)


async def test_driver_failure_is_not_reported_as_an_applied_mock_response(setup):
    recon, session = setup({"path": "/api/selected", "delay_ms": 0},
                           stubs=[{"path": "/api/selected", "body": {"code": 0}}])
    route = Route()
    route.fulfill = AsyncMock(side_effect=RuntimeError("synthetic driver failure"))
    await recon._intercept(session, route)
    assert recon.rows[0]["network_intervention"]["state"] == "failed"
    data = json.loads(Path(recon.rows[0]["files"][0]["path"]).read_text())
    assert data["source_kind"] == "mock_runtime" and not data["material"].get("response")
    assert data["tool_results"][0]["result"]["failure"] == "network_intervention_failed"
