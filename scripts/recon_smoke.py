#!/usr/bin/env python3
"""Explicit paid-cloud smoke test; never run automatically by pytest.

Key is read with terminal echo disabled. Only the requested ephemeral sessions
are created/stopped. Fixture HTTP is fulfilled locally through the cloud CDP
connection; example.com is the separate real external HTTPS check.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import logging
import os
import secrets
import sys
import tempfile
import time
from pathlib import Path

import cbmcp.config as gateway_config
from cbmcp.app import App, set_app
from cbmcp.config import Settings
from cbmcp.server import build_server

ORIGIN = "https://recon-fixture.invalid"
HTML = """<!doctype html><meta charset=utf-8><title>Recon fixture</title><script>
window.boot = {custom: window.__recon_custom_before === true, storage: localStorage.getItem('mock-login'), runs: window.__recon_custom_runs};
window.done = (async () => {
  const menu = await (await fetch('/api/menu')).json();
  const response = await fetch(new Request(location.origin+'/api/denied', {
    method:'POST', headers:{'Content-Type':'application/json'},body:JSON.stringify({title:'synthetic'})}));
  const patched = await response.json();
  const untouched = await (await fetch('/api/unchanged')).json();
  const list = await (await fetch('/api/list')).json();
  const plain = await (await fetch('/plain')).json();
  const xhr = (url,type) => new Promise((resolve,reject) => {
    const x = new XMLHttpRequest(), states=[];
    x.open('GET',url); x.responseType=type;
    x.addEventListener('readystatechange',()=>states.push(x.readyState));
    x.addEventListener('load',()=>resolve({status:x.status,body:type==='json'?x.response:JSON.parse(x.responseText),states,ct:x.getResponseHeader('content-type')}));
    x.onerror=()=>reject(new Error('XHR failed')); x.send();
  });
  return {boot,menu,patched,httpStatus:response.status,untouched,list,plain,
    xhrMock:await xhr('/api/menu','json'),xhrPatch:await xhr('/api/denied','json'),
    xhrText:await xhr('/api/menu','text')};
})();
</script>"""


def unpack(result):
    if isinstance(result, dict):
        return result
    if isinstance(result, tuple):
        for item in result:
            if isinstance(item, dict):
                return item
        result = result[0]
    structured = getattr(result, "structuredContent", None)
    if structured:
        return structured
    for item in getattr(result, "content", result):
        if getattr(item, "type", None) == "text":
            return json.loads(item.text)
    raise AssertionError("MCP tool returned no structured object")


def fixture_handler(counts):
    async def fixture(route):
        from urllib.parse import urlsplit

        path = urlsplit(route.request.url).path
        counts[path] = counts.get(path, 0) + 1
        if path == "/fixture":
            await route.fulfill(status=200, content_type="text/html", body=HTML)
        else:
            body = {"code": 401, "message": "not logged in", "data": {"fixture": True}}
            await route.fulfill(
                status=401 if path == "/api/denied" else 200,
                content_type="application/json",
                body=json.dumps(body),
            )

    return fixture


async def run(key, output):
    root = Path(tempfile.mkdtemp(prefix="cloud-recon-smoke-", dir="/private/tmp"))
    settings = Settings(
        _env_file=None,
        BROWSER_USE_API_KEY=key,
        provider="browseruse",
        data_dir=root,
        owner_tag="recon-test-" + secrets.token_hex(6),
        max_concurrency=1,
        max_lease_seconds=300,
        idle_timeout_seconds=180,
        platform_timeout_minutes=7,
        release_grace_seconds=0,
        scope_mode="enforce",
        scope_allow=["recon-fixture.invalid", "example.com"],
        reconcile_on_startup=False,
        reconcile_stop_untagged=False,
        log_level="ERROR",
    )
    gateway_config._settings = settings
    app = App(settings)
    set_app(app)
    mcp = build_server()
    logging.disable(logging.CRITICAL)  # provider/CDP errors may contain signed connection URLs
    started = time.monotonic()
    report = {
        "cloud": True,
        "local_browser_launched": False,
        "fixture_is_synthetic": True,
        "checks": [],
        "sessions_stopped": [],
        "passed": False,
        "key_saved": False,
    }

    async def call(name, **arguments):
        result = unpack(await mcp.call_tool(name, arguments))
        if result.get("ok") is False:
            raise AssertionError(name + " failed: " + str(result.get("error")))
        return result

    def check(name, value):
        if not value:
            raise AssertionError(name)
        report["checks"].append(name)
        print("PASS " + name, flush=True)

    try:
        await app.start()
        account = await app.PR.account_info()
        report["platform_concurrency_limit"] = account.concurrent_session_limit
        for mode in ("forward", "offline"):
            lease = None
            platform_id = None
            try:
                print("Creating one cloud session for " + mode, flush=True)
                acquired = await call(
                    "browser_acquire",
                    task_label="recon-controlled-smoke",
                    idempotency_key="smoke-" + mode + "-" + secrets.token_hex(4),
                    scope_allow=["recon-fixture.invalid", "example.com"],
                    debug="full",
                    solve_captchas=False,
                )
                if acquired.get("status") == "queued":
                    acquired = await call(
                        "browser_queue_status", ticket_id=acquired["ticket_id"], wait_seconds=60
                    )
                lease = acquired.get("lease")
                check(mode + ": real cloud lease ready", bool(lease))
                obj = app.P.resolve_token(lease)
                platform_id = obj.platform_session_id
                session = obj.session
                counts = {}
                fixture = fixture_handler(counts)
                await session.context.route(ORIGIN + "/**", fixture)
                config = {
                    "origins": [ORIGIN],
                    "mode": mode,
                    "api_prefixes": ["/api/"],
                    "stubs": [
                        {"path": "/api/menu", "body": {"code": 0, "data": {"items": ["reports"]}}}
                    ],
                    "neutralize": {
                        "paths": ["/api/denied"],
                        "fields": ["code"],
                        "failure_values": [401],
                        "success": 0,
                        "flags": {"message": "client mock"},
                        "http_status": 200,
                    },
                    "local_storage": {"mock-login": mode},
                    "init_script": "window.__recon_custom_before = true; window.__recon_custom_runs = (window.__recon_custom_runs || 0) + 1;",
                }
                installed = await call("js_recon", lease=lease, action="install", config=config)
                check(mode + ": document-start installed", installed["installed"])
                await call(
                    "page_navigate",
                    lease=lease,
                    url=ORIGIN + "/fixture",
                    wait_until="domcontentloaded",
                )
                result = (await call("page_eval", lease=lease, expression="window.done"))["value"]
                check(
                    mode + ": before business script",
                    result["boot"] == {"custom": True, "storage": mode, "runs": 1},
                )
                check(mode + ": fetch L1 menu", result["menu"]["data"]["items"] == ["reports"])
                check(
                    mode + ": XHR JSON L1/events",
                    result["xhrMock"]["body"]["data"]["items"] == ["reports"]
                    and result["xhrMock"]["states"] == [2, 3, 4]
                    and "json" in result["xhrMock"]["ct"],
                )
                check(mode + ": XHR text L1", result["xhrText"]["body"]["code"] == 0)
                check(mode + ": no upstream L1 request", counts.get("/api/menu", 0) == 0)
                check(
                    mode + ": non-API still forwarded",
                    result["plain"]["code"] == 401 and counts.get("/plain", 0) == 1,
                )
                if mode == "forward":
                    check(
                        "forward: Request object body and L2 HTTP/JSON",
                        result["patched"]["code"] == 0 and result["httpStatus"] == 200,
                    )
                    check(
                        "forward: XHR JSON L2",
                        result["xhrPatch"]["body"]["code"] == 0
                        and result["xhrPatch"]["status"] == 200,
                    )
                    check(
                        "forward: unconfigured JSON unchanged", result["untouched"]["code"] == 401
                    )
                    check(
                        "forward: requests really reached fixture transport",
                        counts.get("/api/denied") == 2,
                    )
                else:
                    check(
                        "offline: L3 list is empty array",
                        result["list"]["code"] == 0 and result["list"]["data"] == [],
                    )
                    check(
                        "offline: API requests not forwarded",
                        all(
                            not counts.get(p)
                            for p in ("/api/denied", "/api/unchanged", "/api/list")
                        ),
                    )
                await call(
                    "page_navigate", lease=lease, action="reload", wait_until="domcontentloaded"
                )
                refreshed = (await call("page_eval", lease=lease, expression="window.done"))[
                    "value"
                ]
                check(
                    mode + ": survives reload before startup",
                    refreshed["boot"] == result["boot"] and refreshed["menu"] == result["menu"],
                )
                status = await call("js_recon", lease=lease, action="status")
                check(
                    mode + ": marker present",
                    any(v and v.get("installed") for v in status["pages"].values()),
                )
                if mode == "forward":
                    check(
                        "reconnect: same cloud browser restored",
                        await app.P.reconnect(obj, why="controlled recon smoke"),
                    )
                    session = obj.session
                    await session.context.route(ORIGIN + "/**", fixture)
                    await call(
                        "page_navigate",
                        lease=lease,
                        url=ORIGIN + "/fixture",
                        wait_until="domcontentloaded",
                    )
                    resumed = (await call("page_eval", lease=lease, expression="window.done"))[
                        "value"
                    ]
                    check(
                        "reconnect: custom startup script runs once",
                        resumed["boot"] == result["boot"],
                    )
                    extra = await session.new_page()
                    await extra.page.goto(ORIGIN + "/fixture", wait_until="domcontentloaded")
                    popup = await extra.page.evaluate("window.done")
                    check(
                        "new tab: startup mock inherited once",
                        popup["boot"] == result["boot"] and popup["menu"] == result["menu"],
                    )
                # Flush pending expose-binding callbacks before inspecting capture files.
                for _ in range(30):
                    rows = await call("js_recon", lease=lease, action="records", limit=50)
                    if rows["total"] >= 16:
                        break
                    await asyncio.sleep(0.1)
                report.setdefault("capture_counts", {})[mode] = {
                    "records": rows["total"],
                    "dropped": rows["dropped_records"],
                }
                check(
                    mode + ": capture not dropped",
                    rows["dropped_records"] == 0 and rows["total"] >= 16,
                )
                files = [f for row in rows["records"] for f in row["files"]]
                check(
                    mode + ": raw file hashes match",
                    all(
                        hashlib.sha256(Path(f["path"]).read_bytes()).hexdigest()
                        == f["content_hash"]
                        for f in files
                    ),
                )
                check(
                    mode + ": files private",
                    all(Path(f["path"]).stat().st_mode & 0o777 == 0o600 for f in files),
                )
                if mode == "forward":
                    l2 = next(
                        row
                        for row in rows["records"]
                        if row["layer"] == "L2" and row["transport"] == "fetch"
                    )
                    by_kind = {
                        f["source_kind"]: json.loads(Path(f["path"]).read_text())
                        for f in l2["files"]
                    }
                    check(
                        "forward: raw 401 retained separately from mock 200",
                        by_kind["target_runtime"]["material"]["status"] == 401
                        and by_kind["mock_runtime"]["material"]["response"]["status"] == 200,
                    )
                    request_body = by_kind["target_runtime"]["request"]["body"]["text"]
                    check(
                        "forward: Request body preserved",
                        json.loads(request_body) == {"title": "synthetic"},
                    )
                    await call(
                        "page_navigate",
                        lease=lease,
                        url="https://example.com",
                        wait_until="domcontentloaded",
                    )
                    title = (await call("page_eval", lease=lease, expression="document.title"))[
                        "value"
                    ]
                    check("real external HTTPS navigation", title == "Example Domain")
                first = await call("js_recon", lease=lease, action="records", limit=1)
                second = await call(
                    "js_recon", lease=lease, action="records", offset=first["next_offset"], limit=1
                )
                check(
                    mode + ": records pagination",
                    second["records"][0]["id"] == first["records"][0]["id"] + 1,
                )
                rejected = unpack(
                    await mcp.call_tool(
                        "js_recon",
                        {
                            "lease": lease,
                            "action": "install",
                            "config": {
                                **config,
                                "mode": "offline" if mode == "forward" else "forward",
                            },
                        },
                    )
                )
                check(
                    mode + ": changed config cannot leave stale scripts",
                    rejected.get("ok") is False,
                )
            finally:
                if lease:
                    await call(
                        "browser_release", lease=lease, reason="controlled recon smoke finished"
                    )
                if platform_id:
                    stopped = await app.PR.confirm_stopped(platform_id)
                    report["sessions_stopped"].append(stopped)
                    check(mode + ": platform confirms stopped", stopped)
        report["passed"] = True
    except Exception as exc:
        # No exception message, URL, lease token, key or CDP URL is exported.
        report["failure_type"] = type(exc).__name__
        if isinstance(exc, AssertionError):
            report["failure_check"] = str(exc).replace(key, "<redacted>")[:200]
        print("FAILED " + type(exc).__name__, flush=True)
        raise
    finally:
        await app.stop()
        set_app(None)
        report["duration_seconds"] = round(time.monotonic() - started, 2)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        os.chmod(output, 0o600)
        print("Sanitized result: " + str(output), flush=True)


async def run_stdio(key, output):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    root = Path(tempfile.mkdtemp(prefix="cloud-recon-stdio-", dir="/private/tmp"))
    env = dict(
        os.environ,
        BROWSER_USE_API_KEY=key,
        CBMCP_PROVIDER="browseruse",
        CBMCP_DATA_DIR=str(root),
        CBMCP_OWNER_TAG="stdio-test-" + secrets.token_hex(6),
        CBMCP_RECONCILE_ON_STARTUP="false",
        CBMCP_RECONCILE_STOP_UNTAGGED="false",
        CBMCP_ENABLE_DEBUGGER_TOOLS="true",
        CBMCP_LOG_LEVEL="CRITICAL",
    )
    params = StdioServerParameters(command=sys.executable, args=["-m", "cbmcp.server"], env=env)
    result = {"passed": False, "key_saved": False, "sessions_created": 0, "checks": []}
    try:
        with open(os.devnull, "w") as errors:
            async with stdio_client(params, errlog=errors) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=60) as client:
                    await client.initialize()
                    result["checks"].append("actual MCP stdio initialize")
                    tools = await client.list_tools()
                    names = {t.name for t in tools.tools}
                    assert {"js_recon", "browser_state", "browser_acquire", "browser_release", "js_hook"} <= names
                    result["checks"].append("extension and original tools visible together")
                    status = unpack(await client.call_tool("browser_status", {}))
                    assert status.get("ok") is True
                    result["checks"].append("original browser_status callable")
                    invalid = unpack(
                        await client.call_tool("js_recon", {"lease": "invalid", "action": "status"})
                    )
                    assert invalid.get("ok") is False
                    result["checks"].append("extension enforces lease capability")
                    state_tool=next(t for t in tools.tools if t.name=="browser_state")
                    schema=state_tool.input_schema
                    assert {"expected_hash","origins","account_key"} <= set(schema["properties"])
                    result["checks"].append("state import account/origin/hash schema visible")
                    invalid=unpack(await client.call_tool("browser_state",{
                        "lease":"invalid","action":"import","origins":["https://fixture.invalid"],
                        "account_key":"user_a"}))
                    assert invalid.get("ok") is False
                    result["checks"].append("state import enforces lease capability")
                    snapshot_tool=next(t for t in tools.tools if t.name=='page_snapshot')
                    assert {'frame_ref','frames_only','frame_offset','frame_limit'} <= set(snapshot_tool.input_schema['properties'])
                    result['checks'].append('iframe directory and targeted snapshot parameters visible')
                    import importlib.metadata
                    result["version"]=importlib.metadata.version("cbmcp")
                    result["tool_count"]=len(names)
                    from cbmcp import __version__
                    assert result["version"] == __version__
                    result["checks"].append("permanent runtime distribution version" + __version__)
        result["passed"] = True
        print("PASS actual MCP stdio entrypoint ("+str(len(result['checks']))+" checks; no browser acquired)", flush=True)
    finally:
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        os.chmod(output, 0o600)


NETWORK_HTML = """<!doctype html><meta charset=utf-8><title>Network rules fixture</title><script>
window.fetchResult = async (path,method='GET') => {
  const start=performance.now();
  try { const r=await fetch(path,{method}); return {path,kind:'fetch',status:r.status,body:await r.json(),elapsed:performance.now()-start}; }
  catch(e) { return {path,kind:'fetch',error:e.name,elapsed:performance.now()-start}; }
};
window.xhrResult = (path) => new Promise(resolve=>{
  const start=performance.now(),x=new XMLHttpRequest(); x.open('GET',path); x.responseType='json';
  const finish=event=>resolve({path,kind:'xhr',event,status:x.status,body:x.response,elapsed:performance.now()-start});
  x.onload=()=>finish('load'); x.onerror=()=>finish('error'); x.onabort=()=>finish('abort'); x.ontimeout=()=>finish('timeout'); x.send();
});
</script>"""


async def run_network_rules(key, output):
    """Real cloud Chromium, synthetic transport fixture, original four abort codes."""
    from urllib.parse import urlsplit

    root = Path(tempfile.mkdtemp(prefix="cloud-network-rules-smoke-", dir="/private/tmp"))
    settings = Settings(
        _env_file=None, BROWSER_USE_API_KEY=key, provider="browseruse", data_dir=root,
        owner_tag="network-test-" + secrets.token_hex(6), max_concurrency=1,
        max_lease_seconds=600, idle_timeout_seconds=180, platform_timeout_minutes=12,
        release_grace_seconds=0, scope_mode="enforce", scope_allow=["recon-fixture.invalid"],
        reconcile_on_startup=False, reconcile_stop_untagged=False, log_level="ERROR",
    )
    gateway_config._settings = settings
    app = App(settings)
    set_app(app)
    mcp = build_server()
    logging.disable(logging.CRITICAL)
    started = time.monotonic()
    report = {"cloud": True, "fixture_is_synthetic": True, "local_browser_launched": False,
              "key_saved": False, "checks": [], "sessions_stopped": [], "passed": False}

    async def call(name, **arguments):
        result = unpack(await mcp.call_tool(name, arguments))
        if result.get("ok") is False:
            raise AssertionError(name + " failed: " + str(result.get("error")))
        return result

    def check(name, value):
        if not value:
            raise AssertionError(name)
        report["checks"].append(name)
        print("PASS " + name, flush=True)

    errors = {"connectionrefused": "ERR_CONNECTION_REFUSED", "timedout": "ERR_TIMED_OUT",
              "connectionreset": "ERR_CONNECTION_RESET", "internetdisconnected": "ERR_INTERNET_DISCONNECTED"}
    try:
        await app.start()
        account = await app.PR.account_info()
        report["platform_concurrency_limit"] = account.concurrent_session_limit
        for mode in ("forward", "offline"):
            lease = platform_id = None
            recon = None
            try:
                print("Creating one cloud session for network_rules " + mode, flush=True)
                acquired = await call("browser_acquire", task_label="network-rules-controlled-smoke",
                                      idempotency_key=mode + "-" + secrets.token_hex(6),
                                      scope_allow=["recon-fixture.invalid"], debug="full", solve_captchas=False)
                if acquired.get("status") == "queued":
                    acquired = await call("browser_queue_status", ticket_id=acquired["ticket_id"], wait_seconds=60)
                lease = acquired.get("lease")
                check(mode + ": real cloud lease ready", bool(lease))
                obj = app.P.resolve_token(lease)
                session = obj.session
                platform_id = obj.platform_session_id
                check(mode + ": clean lease has no inherited recon", not (await call("js_recon", lease=lease))["installed"])
                counts = {}

                async def fixture(route):
                    path = urlsplit(route.request.url).path
                    counts[path] = counts.get(path, 0) + 1
                    if path == "/fixture":
                        await route.fulfill(status=200, content_type="text/html", body=NETWORK_HTML)
                    else:
                        status = 401 if path == "/api/delayed-real" else 503 if path == "/api/status-error" else 200
                        await route.fulfill(status=status, content_type="application/json",
                                            body=json.dumps({"code": 401, "source": "controlled-upstream"}))

                await session.context.route(ORIGIN + "/**", fixture)
                config = {"origins": [ORIGIN], "mode": mode,
                          "stubs": [{"path": "/api/fault/" + e, "body": {"should_not_win": True}} for e in errors]
                                   + [{"path": "/api/delayed-stub", "status": 202, "body": {"data": "loaded"}},
                                      {"path": "/api/unaffected-stub", "body": {"untouched": True}}],
                          "neutralize": {"paths": ["/api/delayed-real"], "fields": ["code"], "failure_values": [401],
                                         "success": 0, "http_status": 200},
                          "network_rules": [{"path": "/api/fault/" + e, "method": "GET", "error": e} for e in errors]
                                           + [{"path": "/api/delayed-stub", "delay_ms": 3000},
                                              {"path": "/api/delayed-real", "delay_ms": 200},
                                              {"path": "/api/list", "delay_ms": 200},
                                              {"path": "/api/pending", "delay_ms": 120000}]}
                installed = await call("js_recon", lease=lease, action="install", config=config)
                check(mode + ": driver rules installed", installed["network_rules"] == 8)
                recon = session.recon
                await call("page_navigate", lease=lease, url=ORIGIN + "/fixture", wait_until="domcontentloaded")
                result = (await call("page_eval", lease=lease, expression="""async () => {
                  const codes=['connectionrefused','timedout','connectionreset','internetdisconnected'];
                  return await Promise.all(codes.flatMap(e=>[fetchResult('/api/fault/'+e),xhrResult('/api/fault/'+e)]));
                }"""))["value"]
                report.setdefault("browser_failure_observations", {})[mode] = [
                    {k: r[k] for k in ("kind", "error", "event", "status") if k in r}
                    | {"requested_error": r["path"].rsplit("/", 1)[-1]} for r in result]
                for error in errors:
                    pair = [r for r in result if r["path"].endswith(error)]
                    check(mode + ": fetch and XHR browser failure " + error,
                          len(pair) == 2 and all(r.get("error") or r.get("event") in {"error", "timeout"} and r.get("status") == 0 for r in pair))
                    net = (await call("net_requests", lease=lease, url_contains="/api/fault/" + error, only_failed=True))["requests"]
                    for _ in range(20):
                        if len(net) == 2 and all(r.get("failed") for r in net):
                            break
                        await asyncio.sleep(0.1)
                        net = (await call("net_requests", lease=lease, url_contains="/api/fault/" + error, only_failed=True))["requests"]
                    report.setdefault("chromium_failure_observations", {}).setdefault(mode, {})[error] = [
                        {k: r.get(k) for k in ("failed", "source_kind", "type")} for r in net]
                    check(mode + ": Chromium error code " + error,
                          len(net) == 2 and all(errors[error] in (r["failed"] or "") and r.get("source_kind") == "mock_runtime" for r in net))
                check(mode + ": fault beats L1 without contacting fixture", all(not counts.get("/api/fault/" + e) for e in errors))
                result = (await call("page_eval", lease=lease, expression="""async () => {
                  return await Promise.all([fetchResult('/api/delayed-stub'),xhrResult('/api/delayed-stub'),
                    fetchResult('/api/delayed-real'),fetchResult('/api/list'),fetchResult('/plain'),
                    fetchResult('/api/unaffected-stub'),fetchResult('/api/fault/timedout','POST')]);
                }"""))["value"]
                delayed = [r for r in result if r["path"] == "/api/delayed-stub"]
                check(mode + ": fetch and XHR delayed L1 preserve status/body",
                      len(delayed) == 2 and all(r["status"] == 202 and r["body"] == {"data": "loaded"} for r in delayed))
                check(mode + ": original three second delay is observed", all(r["elapsed"] >= 2800 for r in delayed))
                check(mode + ": delayed L1 does not contact fixture", not counts.get("/api/delayed-stub"))
                real = next(r for r in result if r["path"] == "/api/delayed-real")
                check(mode + ": delay keeps normal L2 or L3 outcome", real["status"] == 200 and real["body"]["code"] == 0)
                check(mode + ": delay survives for real/fallback result", real["elapsed"] >= 150)
                plain = next(r for r in result if r["path"] == "/plain")
                check(mode + ": unmatched non-API request unchanged", plain["body"].get("source") == "controlled-upstream")
                check(mode + ": unrelated L1 unchanged", next(r for r in result if r["path"] == "/api/unaffected-stub")["body"] == {"untouched": True})
                check(mode + ": method mismatch is not aborted", next(r for r in result if r["path"] == "/api/fault/timedout")["body"] == {"should_not_win": True})
                if mode == "offline":
                    check("offline: delayed L3 retains list shape and stays offline",
                          next(r for r in result if r["path"] == "/api/list")["body"]["data"] == []
                          and not counts.get("/api/list") and not counts.get("/api/delayed-real"))
                else:
                    check("forward: delayed L2 actually reaches controlled upstream", counts.get("/api/delayed-real") == 1)
                rows = await call("js_recon", lease=lease, action="records", limit=50)
                for _ in range(30):
                    if any(r["layer"] == ("L2" if mode == "forward" else "L3") for r in rows["records"]):
                        break
                    await asyncio.sleep(0.1)
                    rows = await call("js_recon", lease=lease, action="records", limit=50)
                affected = [r for r in rows["records"] if r.get("network_intervention")]
                check(mode + ": driver interventions have actual applied records",
                      len(affected) >= 12 and all(r["network_intervention"]["state"] == "applied" for r in affected))
                check(mode + ": intervention files never invent an upstream response",
                      all(f["source_kind"] == "mock_runtime" for r in affected for f in r["files"]))
                check(mode + ": private files and hashes intact", all(
                    Path(f["path"]).stat().st_mode & 0o777 == 0o600 and hashlib.sha256(Path(f["path"]).read_bytes()).hexdigest() == f["content_hash"]
                    for r in rows["records"] for f in r["files"]))
                if mode == "forward":
                    l2 = next(r for r in rows["records"] if r["layer"] == "L2")
                    material = {f["source_kind"]: json.loads(Path(f["path"]).read_text()) for f in l2["files"]}
                    check("forward: unmodified401 and presented200 remain separate",
                          material["target_runtime"]["material"]["status"] == 401
                          and material["mock_runtime"]["material"]["response"]["status"] == 200)
                net = (await call("net_requests", lease=lease, url_contains="/api/delayed-stub"))["requests"]
                detail = await call("net_request", lease=lease, request_id=net[0]["id"])
                # Network body collection is asynchronous; page JS completion is not its completion signal.
                for _ in range(100):
                    entry = session.netlog.get(net[0]["id"])
                    if entry.body_path or entry.body_error:
                        break
                    await asyncio.sleep(0.1)
                body = await call("net_body", lease=lease, request_id=net[0]["id"])
                check(mode + ": net detail and body expose mock provenance", detail.get("source_kind") == body.get("source_kind") == "mock_runtime")
                har = await call("net_har", lease=lease)
                entries = json.loads(Path(har["path"]).read_text())["log"]["entries"]
                check(mode + ": HAR preserves per-request intervention provenance", any(
                    e.get("_sourceKind") == "mock_runtime" and e.get("_networkIntervention", {}).get("delay_ms") == 3000 for e in entries))
                first = await call("js_recon", lease=lease, action="records", limit=1)
                second = await call("js_recon", lease=lease, action="records", offset=first["next_offset"], limit=1)
                check(mode + ": intervention record pagination works", second["records"][0]["id"] == first["records"][0]["id"] + 1)
                changed = unpack(await mcp.call_tool("js_recon", {"lease": lease, "action": "install", "config": {**config, "network_rules": []}}))
                check(mode + ": network configuration stays immutable", changed.get("ok") is False)
                if mode == "forward":
                    before_reconnect = session.reconnects
                    session.liveness_failures = 1  # bypass the pool's already-connected fast path
                    check("reconnect: same cloud session restored", await app.P.reconnect(obj, why="controlled network rules smoke"))
                    session = obj.session
                    check("reconnect: a fresh CDP connection was actually established", session.reconnects == before_reconnect + 1)
                    handler = recon._route_handler
                    check("reconnect: native rules reattached automatically", handler is not None and recon.context is session.context)
                    # The fixture is the synthetic upstream; keep it behind the already-restored control route.
                    await session.context.unroute("**/*", handler)
                    await session.context.route(ORIGIN + "/**", fixture)
                    await session.context.route("**/*", handler)
                    await call("page_navigate", lease=lease, url=ORIGIN + "/fixture", wait_until="domcontentloaded")
                    after = (await call("page_eval", lease=lease, expression="fetchResult('/api/fault/timedout')"))["value"]
                    check("reconnect: browser still reports the selected fault", after.get("error") == "TypeError")
                    popup = await session.new_page()
                    await popup.page.goto(ORIGIN + "/fixture", wait_until="domcontentloaded")
                    after = await popup.page.evaluate("xhrResult('/api/fault/connectionreset')")
                    check("new tab: selected fault and preload both inherited", after["event"] == "error" and after["status"] == 0)
                await call("page_eval", lease=lease, expression="() => { window.pendingNetworkTest=fetch('/api/pending').catch(()=>null); return true; }")
                pending = await call("js_recon", lease=lease, action="status")
                check(mode + ": pending long delay is visible before release", pending["pending_network_interventions"] == 1)
            finally:
                if lease:
                    await call("browser_release", lease=lease, reason="controlled network rules smoke finished")
                if platform_id:
                    stopped = await app.PR.confirm_stopped(platform_id)
                    report["sessions_stopped"].append(stopped)
                    check(mode + ": platform confirms stopped", stopped)
                if recon is not None and any(r.get("network_intervention", {}).get("delay_ms") == 120000 for r in recon.rows):
                    check(mode + ": release cancels pending intervention", any(
                        r.get("network_intervention", {}).get("state") == "cancelled" for r in recon.rows))
        report["passed"] = True
    except Exception as exc:
        report["failure_type"] = type(exc).__name__
        if isinstance(exc, AssertionError):
            report["failure_check"] = str(exc).replace(key, "<redacted>")[:200]
        print("FAILED " + type(exc).__name__, flush=True)
        raise
    finally:
        await app.stop()
        set_app(None)
        report["duration_seconds"] = round(time.monotonic() - started, 2)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        os.chmod(output, 0o600)
        print("Sanitized result: " + str(output), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--stdio-only",
        action="store_true",
        help="Check the deployed MCP entrypoint without renting a browser",
    )
    parser.add_argument("--network-rules-only", action="store_true", help="Controlled cloud fault/delay integration checks")
    args = parser.parse_args()
    credential = getpass.getpass("Browser Use key (not saved): ")
    try:
        asyncio.run(
            run_stdio(credential, args.output) if args.stdio_only else
            run_network_rules(credential, args.output) if args.network_rules_only else run(credential, args.output)
        )
    except Exception:
        raise SystemExit(1) from None
