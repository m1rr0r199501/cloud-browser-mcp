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
                    assert {"js_recon", "browser_acquire", "browser_release", "js_hook"} <= names
                    result["checks"].append("extension and original tools visible together")
                    status = unpack(await client.call_tool("browser_status", {}))
                    assert status.get("ok") is True
                    result["checks"].append("original browser_status callable")
                    invalid = unpack(
                        await client.call_tool("js_recon", {"lease": "invalid", "action": "status"})
                    )
                    assert invalid.get("ok") is False
                    result["checks"].append("extension enforces lease capability")
        result["passed"] = True
        print("PASS actual MCP stdio entrypoint (4 checks; no browser acquired)", flush=True)
    finally:
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        os.chmod(output, 0o600)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--stdio-only",
        action="store_true",
        help="Check the deployed MCP entrypoint without renting a browser",
    )
    args = parser.parse_args()
    credential = getpass.getpass("Browser Use key (not saved): ")
    try:
        asyncio.run(
            run_stdio(credential, args.output) if args.stdio_only else run(credential, args.output)
        )
    except Exception:
        raise SystemExit(1) from None
