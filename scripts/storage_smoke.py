#!/usr/bin/env python3
"""Explicit cloud fixture test: profile SSO reuse, state export and fresh restore."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import tempfile
import time
from urllib.parse import parse_qs, urlsplit

import cbmcp.config as gateway_config
from cbmcp.app import App, set_app
from cbmcp.config import Settings
from cbmcp.server import build_server


A = "https://a.sso-fixture.invalid"
B = "https://b.sso-fixture.invalid"
IDP = "https://login.sso-fixture.invalid"


def unpack(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, tuple):
        for item in value:
            if isinstance(item, dict):
                return item
        value = value[0]
    for item in getattr(value, "content", value):
        if getattr(item, "type", None) == "text":
            return json.loads(item.text)
    raise AssertionError("tool result format")


async def run(key, output):
    root = Path(tempfile.mkdtemp(prefix="cloud-storage-smoke-", dir="/private/tmp"))
    profile = "state-fixture-" + secrets.token_hex(8)
    settings = Settings(_env_file=None, BROWSER_USE_API_KEY=key, provider="browseruse",
        data_dir=root, default_profile="", owner_tag="storage-test-" + secrets.token_hex(6),
        max_concurrency=1, max_lease_seconds=300, idle_timeout_seconds=180,
        platform_timeout_minutes=7, release_grace_seconds=0,
        scope_mode="enforce", scope_allow=["*.sso-fixture.invalid"],
        reconcile_on_startup=False, reconcile_stop_untagged=False, log_level="CRITICAL")
    gateway_config._settings = settings
    app = App(settings)
    set_app(app)
    mcp = build_server()
    logging.disable(logging.CRITICAL)
    report = {"cloud": True, "fixture_is_synthetic": True, "local_browser_launched": False,
        "checks": [], "sessions_stopped": [], "profile_deleted": False,
        "passed": False, "credential_in_report": False}
    started = time.monotonic()
    active = {}
    seen = []
    made_profile = False

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        report["checks"].append(name)
        print("PASS " + name, flush=True)

    async def call(tool_name, **arguments):
        result = unpack(await mcp.call_tool(tool_name, arguments))
        if result.get("ok") is False:
            code = result.get("error")
            report["tool_failure"] = {"tool": tool_name,
                "code": code[:100] if isinstance(code, str) else type(code).__name__}
            if tool_name == "page_navigate" and code == "navigation_failed":
                report["fixture_navigation_message"] = str(result.get("message", "")).replace(key, "<redacted>")[:500]
            if tool_name == "browser_state" and code == "invalid_argument":
                report["state_validation_message"] = str(result.get("message", ""))[:300]
            raise AssertionError(tool_name + " succeeded")
        return result

    async def acquire(label, saved_profile=None):
        arguments = {"task_label": "controlled-storage-" + label,
            "idempotency_key": label + "-" + secrets.token_hex(5),
            "scope_allow": ["*.sso-fixture.invalid"], "debug": "full", "solve_captchas": False}
        if saved_profile is not None:
            arguments["profile"] = saved_profile
        print("Creating owned cloud session: " + label, flush=True)
        result = await call("browser_acquire", **arguments)
        while result.get("status") == "queued":
            result = await call("browser_queue_status", ticket_id=result["ticket_id"], wait_seconds=30)
        token = result.get("lease")
        check(label + ": real lease ready", bool(token))
        obj = app.P.resolve_token(token)
        active[token] = obj.platform_session_id
        return token, obj.session

    async def release(token):
        platform_id = active[token]
        await call("browser_release", lease=token, reason="controlled storage test finished")
        stopped = await app.PR.confirm_stopped(platform_id)
        report["sessions_stopped"].append(stopped)
        check("platform confirms stopped", stopped)
        active.pop(token)

    async def fixture(route):
        parsed = urlsplit(route.request.url)
        if not (parsed.hostname or "").endswith(".sso-fixture.invalid"):
            return await route.fallback()
        cookies = (await route.request.all_headers()).get("cookie", "")
        seen.append({"host": parsed.hostname, "path": parsed.path, "cookie": cookies})
        if parsed.hostname == "login.sso-fixture.invalid" and parsed.path == "/seed":
            return await route.fulfill(status=200, content_type="text/html", body="<!doctype html><title>SSO state fixture</title>")
        if parsed.hostname == "login.sso-fixture.invalid":
            if "idp-session=fixture-idp" not in cookies:
                return await route.fulfill(status=401, body="synthetic login required")
            destination = parse_qs(parsed.query).get("redirect", [B + "/callback"])[0]
            body = "<script>setTimeout(()=>location.replace(" + json.dumps(destination + "?code=fixture-code") + "),30)</script>"
            return await route.fulfill(status=200, content_type="text/html", body=body)
        if parsed.hostname == "b.sso-fixture.invalid" and parsed.path == "/callback":
            body = "<script>setTimeout(()=>location.replace(" + json.dumps(B + "/") + "),30)</script>"
            return await route.fulfill(status=200, content_type="text/html", body=body,
                headers={"set-cookie": "app-b=fixture-b; Path=/; Secure; HttpOnly; SameSite=Lax"})
        if parsed.hostname == "b.sso-fixture.invalid" and "app-b=fixture-b" not in cookies:
            destination = IDP + "/authorize?redirect=" + B + "/callback"
            body = "<script>setTimeout(()=>location.replace(" + json.dumps(destination) + "),30)</script>"
            return await route.fulfill(status=200, content_type="text/html", body=body)
        if parsed.path == "/whoami":
            return await route.fulfill(status=200, content_type="application/json",
                body=json.dumps({"subject": "fixture-sso-user", "role": "site-b-reader"}))
        body = """<!doctype html><title>Storage fixture</title><div id="identity"></div><script>
window.done = location.hostname.startsWith('b.')
  ? fetch('/whoami').then(r=>r.json()).then(v=>{localStorage.setItem('app-token','B-only');document.querySelector('#identity').textContent='Fixture signed in on B';return v})
  : Promise.resolve({source:true});
</script>"""
        await route.fulfill(status=200, content_type="text/html", body=body)

    async def attach_fixture(session):
        await session.context.route("**/*", fixture)

    async def visit_b(token, label):
        before = len(seen)
        await call("page_navigate", lease=token, url=B + "/", wait_until="domcontentloaded")
        await call("page_wait", lease=token, text="Fixture signed in on B", timeout_ms=20_000)
        identity = (await call("page_eval", lease=token, expression="window.done"))["value"]
        check(label + ": target identity and target-specific role", identity == {
            "subject": "fixture-sso-user", "role": "site-b-reader"})
        requests = seen[before:]
        check(label + ": normal IdP redirect received its own session cookie", any(
            row["host"] == "login.sso-fixture.invalid" and "idp-session=fixture-idp" in row["cookie"]
            for row in requests))
        b_requests = [row for row in requests if row["host"] == "b.sso-fixture.invalid"]
        check(label + ": parent-domain cookie applies to sibling", any("parent-sso=fixture-parent" in row["cookie"] for row in b_requests))
        check(label + ": host-only source cookie not sent to sibling", all("app-a=" not in row["cookie"] for row in b_requests))
        check(label + ": IdP host-only cookie not sent to business site", all("idp-session=" not in row["cookie"] for row in b_requests))
        storage = (await call("page_eval", lease=token,
            expression="({token:localStorage.getItem('app-token'),source:localStorage.getItem('source-only')})"))["value"]
        check(label + ": origin storage remains separate", storage == {"token": "B-only", "source": None})

    try:
        await app.start()
        await call("browser_profiles", action="create", name=profile)
        made_profile = True
        first, session = await acquire("source", profile)
        await attach_fixture(session)
        await call("page_navigate", lease=first, url=A + "/", wait_until="domcontentloaded")
        await call("page_cookies", lease=first, action="set", cookies=[
            {"name": "parent-sso", "value": "fixture-parent", "domain": ".sso-fixture.invalid", "path": "/",
             "expires": time.time() + 3600, "secure": True, "httpOnly": True, "sameSite": "Lax"},
            {"name": "app-a", "value": "fixture-a", "url": A, "secure": True, "httpOnly": True},
            {"name": "idp-session", "value": "fixture-idp", "url": IDP, "secure": True, "httpOnly": True,
             "expires": time.time() + 3600}])
        await call("page_eval", lease=first, expression="localStorage.setItem('source-only','A-only')")
        await call("page_navigate", lease=first, url=IDP + "/seed", wait_until="domcontentloaded")
        await call("page_eval", lease=first, expression="localStorage.setItem('sso-account','fixture-sso-user')")
        exported = await call("browser_state", lease=first, action="export", origins=[A, IDP], account_key="user_a")
        path = Path(exported["path"])
        check("export: actual file/hash and private permissions", path.stat().st_mode & 0o777 == 0o600
            and hashlib.sha256(path.read_bytes()).hexdigest() == exported["content_hash"])
        check("export: receipt contains no credential values", "fixture-parent" not in json.dumps(exported))
        check("export: never claims backend identity", exported["backend_identity_verified_by_tool"] is False)
        await release(first)

        second = None
        try:
            second, session = await acquire("same-profile", profile)
            await attach_fixture(session)
            persisted = await call("page_cookies", lease=second, action="get")
            check("profile reuse: parent Cookie persisted", any(
                item["name"] == "parent-sso" and item["value"] == "fixture-parent" for item in persisted["cookies"]))
            check("profile reuse: IdP Cookie persisted", any(
                item["name"] == "idp-session" and item["value"] == "fixture-idp" for item in persisted["cookies"]))
            await visit_b(second, "profile reuse")
            refused = unpack(await mcp.call_tool("browser_state", {"lease": second, "action": "restore",
                "state_path": str(path), "expected_hash": exported["content_hash"], "origins": [B, IDP]}))
            check("restore: live profile not overwritten", refused.get("ok") is False)
            check("profile reuse: state still usable", (await call("page_eval", lease=second,
                expression="localStorage.getItem('app-token')"))["value"] == "B-only")
            report["profile_reuse_passed"] = True
        except Exception as exc:
            report["profile_reuse_passed"] = False
            report["profile_failure_type"] = type(exc).__name__
        finally:
            if second in active:
                await release(second)

        third, session = await acquire("fresh-restore")
        await attach_fixture(session)
        restored = await call("browser_state", lease=third, action="restore", state_path=str(path),
            expected_hash=exported["content_hash"], origins=[A, B, IDP])
        check("restore: no identity invented from file", restored["backend_identity_verified_by_tool"] is False)
        check("restore: selected cookies restored", restored["cookie_count"] == 3)
        reread = await call("browser_state", lease=third, action="export", origins=[A])
        captured = json.loads(Path(reread["path"]).read_text())["storage_state"]
        check("restore: exact-origin localStorage retained", any(
            item["origin"] == A and {"name": "source-only", "value": "A-only"} in item["localStorage"]
            for item in captured["origins"]))
        await visit_b(third, "file restore")
        # This lease is already in use on B and has its own application state.
        # SSO import must keep that state, keep the lease, and merge only the IdP.
        await call("page_eval", lease=third, expression="localStorage.setItem('keep-current','target-only')")
        await call("page_cookies", lease=third, action="set", cookies=[{
            "name":"keep-current", "value":"target-only", "url":B, "secure":True}])
        await session.context.clear_cookies(name="idp-session")
        await call("page_navigate",lease=third,url=IDP+"/seed",wait_until="domcontentloaded")
        await call("page_eval",lease=third,expression="localStorage.setItem('sso-account','stale');localStorage.setItem('keep-idp','target-idp')")
        await call("page_navigate",lease=third,url=B+"/",wait_until="domcontentloaded")
        await call("page_wait",lease=third,text="Fixture signed in on B",timeout_ms=20_000)
        before_context = session.context
        wrong = unpack(await mcp.call_tool("browser_state", {"lease":third,"action":"import",
            "state_path":str(path),"expected_hash":exported["content_hash"],
            "origins":[IDP],"account_key":"user_b"}))
        check("current-profile import: labels describe both requested and source account", wrong.get("ok") is True
            and wrong.get("account_key")=="user_b" and wrong.get("source_account_key")=="user_a")
        check("current-profile import: labels do not block state transfer", any(
            item["name"]=="idp-session" for item in await session.context.cookies()))
        merged = await call("browser_state", lease=third, action="import", state_path=str(path),
            expected_hash=exported["content_hash"], origins=[IDP], account_key="user_a")
        check("current-profile import: same live lease/context retained", session.context is before_context
            and merged["profile_switched"] is False)
        check("current-profile import: existing application storage retained", (await call("page_eval",lease=third,
            expression="({token:localStorage.getItem('app-token'),keep:localStorage.getItem('keep-current')})"))["value"]
            == {"token":"B-only","keep":"target-only"})
        cookies = await session.context.cookies()
        check("current-profile import: application and unrelated Cookies retained", all(
            any(item["name"]==name and item["value"]==value for item in cookies)
            for name,value in [("app-b","fixture-b"),("keep-current","target-only")]))
        idp_export = await call("browser_state",lease=third,action="export",origins=[IDP],account_key="user_a")
        idp_state = json.loads(Path(idp_export["path"]).read_text())["storage_state"]
        check("current-profile import: exact IdP localStorage retained", any(
            row["origin"]==IDP and {"name":"sso-account","value":"fixture-sso-user"} in row["localStorage"]
            and {"name":"keep-idp","value":"target-idp"} in row["localStorage"]
            for row in idp_state["origins"]))
        # Remove only our synthetic B ticket so the next normal visit must use
        # the imported IdP state rather than bypassing it with an existing ticket.
        await session.context.clear_cookies(name="app-b")
        await visit_b(third, "current-profile import")
        report["current_profile_import_passed"] = True
        await release(third)
        report["passed"] = report.get("profile_reuse_passed", False)
    except Exception as exc:
        report["failure_type"] = type(exc).__name__
        if isinstance(exc, AssertionError):
            report["failure_check"] = str(exc).replace(key, "<redacted>")[:200]
        print("FAILED " + type(exc).__name__, flush=True)
    finally:
        for token in list(active):
            try:
                await release(token)
            except Exception:
                report["cleanup_incomplete"] = True
        if made_profile:
            for _ in range(20):
                try:
                    result = unpack(await mcp.call_tool("browser_profiles", {"action": "delete", "name": profile}))
                    if result.get("ok"):
                        report["profile_deleted"] = True
                        break
                except Exception:
                    pass
                await asyncio.sleep(2)
        await app.stop()
        set_app(None)
        report["passed_checks"] = len(report["checks"])
        report["fixture_requests"] = [{"host": row["host"], "path": row["path"],
            "has_parent_cookie": "parent-sso=fixture-parent" in row["cookie"],
            "has_idp_cookie": "idp-session=fixture-idp" in row["cookie"],
            "has_app_b_cookie": "app-b=fixture-b" in row["cookie"]} for row in seen[-20:]]
        report["duration_seconds"] = round(time.monotonic() - started, 2)
        report["created_sessions"] = len(report["sessions_stopped"]) + len(active)
        report["all_test_sessions_confirmed_stopped"] = all(report["sessions_stopped"]) and not active
        report["passed"] = report["passed"] and report["profile_deleted"] and report["all_test_sessions_confirmed_stopped"]
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        os.chmod(output, 0o600)
        print("Sanitized result: " + str(output), flush=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    secret = getpass.getpass("Browser Use key (not written to test output): ")
    result = asyncio.run(run(secret, args.output))
    raise SystemExit(0 if result["passed"] else 1)
