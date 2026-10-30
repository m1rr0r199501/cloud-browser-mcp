"""Scoped, private Cookie/localStorage artifacts for the existing cloud context."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import stat
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from .errors import InvalidArgument


MAX_STATE_BYTES = 64 * 1024 * 1024
RECORD_TYPE = "cloud_browser_storage_state"


def normalise_origins(values, scope):
    if not isinstance(values, list) or not values or len(values) > 100:
        raise InvalidArgument("origins must contain 1..100 explicit HTTP(S) origins")
    result = []
    for value in values:
        if not isinstance(value, str) or value != value.strip() or any(ord(char) < 32 for char in value):
            raise InvalidArgument("each origin must be an HTTP(S) origin string")
        try:
            parsed = urlsplit(value)
            port = parsed.port
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or parsed.path or parsed.query or parsed.fragment
                    or (port is not None and not 1 <= port <= 65535)):
                raise ValueError
            host = parsed.hostname.lower().encode("idna").decode()
            if "*" in host or "%" in host:
                raise ValueError
            authority = "[" + host + "]" if ":" in host else host
            if port is not None and port != (443 if parsed.scheme == "https" else 80):
                authority += ":" + str(port)
            origin = parsed.scheme + "://" + authority
        except (ValueError, UnicodeError):
            raise InvalidArgument("origins must exclude paths, credentials, query and fragment") from None
        scope.enforce(origin)
        if origin not in result:
            result.append(origin)
    return result


def cookie_applies(cookie, origins):
    domain = cookie.get("domain")
    if not isinstance(domain, str) or not domain:
        raise InvalidArgument("state cookie needs its original domain")
    wanted = domain.lstrip(".").lower()
    if wanted.startswith("[") and wanted.endswith("]"):
        wanted = wanted[1:-1]
    for origin in origins:
        host = urlsplit(origin).hostname or ""
        if host == wanted or (domain.startswith(".") and host.endswith("." + wanted)):
            return True
    return False


def filter_state(state, origins):
    """Keep original Cookie flags, exact-origin localStorage, and no page code."""
    if not isinstance(state, dict) or not isinstance(state.get("cookies"), list) or not isinstance(state.get("origins"), list):
        raise InvalidArgument("state needs cookies and origins arrays")
    cookies, entries = [], []
    for cookie in state["cookies"]:
        if not isinstance(cookie, dict) or not all(isinstance(cookie.get(key), str) for key in ("name", "value", "domain", "path")):
            raise InvalidArgument("invalid cookie record in state")
        if set(cookie) - {"name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite", "partitionKey"}:
            raise InvalidArgument("state contains unsupported Cookie attributes")
        if any(key in cookie and not isinstance(cookie[key], bool) for key in ("httpOnly", "secure")):
            raise InvalidArgument("invalid Cookie flags")
        if "sameSite" in cookie and cookie["sameSite"] not in {"Lax", "Strict", "None"}:
            raise InvalidArgument("invalid Cookie sameSite")
        expires = cookie.get("expires")
        if expires is not None and (isinstance(expires, bool) or not isinstance(expires, (int, float)) or not math.isfinite(expires)):
            raise InvalidArgument("invalid cookie expiry in state")
        if cookie_applies(cookie, origins):
            cookies.append(dict(cookie))
    seen = set()
    for item in state["origins"]:
        if not isinstance(item, dict) or not isinstance(item.get("origin"), str) or not isinstance(item.get("localStorage"), list):
            raise InvalidArgument("invalid origin storage record")
        if item["origin"] not in origins:
            continue
        if set(item) - {"origin", "localStorage"}:
            raise InvalidArgument("this state flow covers cookies/localStorage; additional storage components need their own supported flow")
        if item["origin"] in seen:
            raise InvalidArgument("duplicate origin storage record")
        seen.add(item["origin"])
        storage, keys = [], set()
        for entry in item["localStorage"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str) or not isinstance(entry.get("value"), str):
                raise InvalidArgument("localStorage records need string name/value pairs")
            if entry["name"] in keys:
                raise InvalidArgument("duplicate localStorage key")
            keys.add(entry["name"])
            storage.append({"name": entry["name"], "value": entry["value"]})
        entries.append({"origin": item["origin"], "localStorage": storage})
    return {"cookies": cookies, "origins": entries}


def canonical_state(state):
    return {"cookies": sorted(state["cookies"], key=lambda item: json.dumps(item, sort_keys=True)),
        "origins": sorted([{"origin": item["origin"],
            "localStorage": sorted(item["localStorage"], key=lambda entry: entry["name"])}
            for item in state["origins"]], key=lambda item: item["origin"])}


def _private_artifact(directory, payload):
    raw = (json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode()
    if len(raw) > MAX_STATE_BYTES:
        raise InvalidArgument("state exceeds the64MiB artifact limit; no truncated state was saved")
    directory = Path(directory) / "auth-state"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / ("state-" + uuid4().hex + ".json")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return {"path": str(path), "content_hash": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}


def _read_artifact(path, expected_hash):
    if not isinstance(path, str) or not Path(path).is_absolute():
        raise InvalidArgument("state_path must be an absolute path to the transferred state artifact")
    if not isinstance(expected_hash, str) or re.fullmatch(r"[a-f0-9]{64}", expected_hash) is None:
        raise InvalidArgument("restore needs the actual SHA256 content_hash")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_STATE_BYTES:
                raise InvalidArgument("state must be a bounded regular file")
            raw = stream.read(MAX_STATE_BYTES + 1)
        if len(raw) > MAX_STATE_BYTES:
            raise InvalidArgument("state exceeds the artifact limit")
        if hashlib.sha256(raw).hexdigest() != expected_hash:
            raise InvalidArgument("state hash changed; do not restore a different version")
        payload = json.loads(raw, parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
    except (OSError, UnicodeError, ValueError):
        raise InvalidArgument("state artifact is unavailable or not valid JSON") from None
    if (isinstance(payload, dict) and set(payload) == {"cookies", "origins"}
            and isinstance(payload["cookies"], list) and isinstance(payload["origins"], list)):
        return {"schema_version": 1, "record_type": RECORD_TYPE,
            "selected_origins": [item["origin"] for item in payload["origins"]
                if isinstance(item, dict) and isinstance(item.get("origin"), str)],
            "storage_state": payload, "source_format": "playwright",
            "client_mock_installed": None}
    if (not isinstance(payload, dict) or payload.get("schema_version") != 1
            or payload.get("record_type") != RECORD_TYPE):
        raise InvalidArgument("expected an exported cloud browser state artifact")
    return payload


def checked_account_key(value):
    if value is not None and (not isinstance(value, str) or value != value.strip()
            or not value or any(ord(char) < 32 for char in value)):
        raise InvalidArgument("account_key must be the selected stable nonsecret account alias")
    return value


async def export_state(session, origins, account_key=None):
    if session.context is None:
        raise InvalidArgument("browser is not connected")
    account_key = checked_account_key(account_key)
    selected = normalise_origins(origins, session.scope)
    state = filter_state(await session.context.storage_state(), selected)
    payload = {"schema_version": 1, "record_type": RECORD_TYPE,
        "captured_at": time.time(), "source_lease_id": session.lease_id,
        "selected_origins": selected, "storage_state": state,
        "account_key": account_key,
        "client_mock_installed": session.recon is not None,
        "backend_identity_verified_by_tool": False}
    artifact = await asyncio.to_thread(_private_artifact, session.dir, payload)
    return {"ok": True, "action": "export", **artifact,
        "origins": selected, "account_key": account_key, "cookie_count": len(state["cookies"]),
        "storage_origin_count": len(state["origins"]),
        "covers": ["cookies", "localStorage"],
        "client_mock_installed": payload["client_mock_installed"],
        "backend_identity_verified_by_tool": False}


async def restore_state(session, path, expected_hash, origins=None):
    if session.context is None:
        raise InvalidArgument("browser is not connected")
    if not callable(getattr(session.context, "set_storage_state", None)):
        raise InvalidArgument("state restore requires the documented Playwright1.64.1+ runtime")
    payload = await asyncio.to_thread(_read_artifact, path, expected_hash)
    selected = normalise_origins(origins if origins is not None else payload.get("selected_origins"), session.scope)
    state = filter_state(payload.get("storage_state"), selected)
    if any(urlsplit(page.url).scheme in {"http", "https"} for page in session.context.pages):
        raise InvalidArgument("restore before target navigation in a clean lease; reuse an existing profile directly")
    current = await session.context.storage_state(indexed_db=True, opfs=True, credentials=True)
    if current.get("cookies") or current.get("origins") or current.get("credentials"):
        raise InvalidArgument("restore requires an empty context; existing profile state was left intact")
    try:
        await session.context.set_storage_state(state)
    except Exception:
        raise InvalidArgument("browser rejected the state; use a fresh lease and recheck its shape without exposing credentials") from None
    restored = filter_state(await session.context.storage_state(), selected)
    if canonical_state(restored) != canonical_state(state):
        raise InvalidArgument("browser did not retain the requested state; verify the fresh lease before use")
    return {"ok": True, "action": "restore", "content_hash": expected_hash,
        "origins": selected, "cookie_count": len(restored["cookies"]),
        "storage_origin_count": len(restored["origins"]),
        "client_mock_installed_in_source": payload.get("client_mock_installed"),
        "source_format": payload.get("source_format", "cloud_browser_storage_state_v1"),
        "backend_identity_verified_by_tool": False,
        "next": "Navigate to the actual target and verify its current identity and role before saving an auth_session."}


async def import_state(session, path, expected_hash, origins, account_key=None):
    """Merge one observed SSO origin into the current profile, without clearing it."""
    if session.context is None:
        raise InvalidArgument("browser is not connected")
    account_key = checked_account_key(account_key)
    selected = normalise_origins(origins, session.scope)
    if len(selected) != 1:
        raise InvalidArgument("SSO import needs exactly the observed login origin")
    payload = await asyncio.to_thread(_read_artifact, path, expected_hash)
    source_account = checked_account_key(payload.get("account_key"))
    if payload.get("client_mock_installed") is True:
        raise InvalidArgument("mock source state cannot be reused as a real SSO login")
    state = filter_state(payload.get("storage_state"), selected)
    now = time.time()
    cookies = [item for item in state["cookies"]
        if item.get("expires", -1) == -1 or item["expires"] > now]
    # add_cookies only updates the supplied Cookie keys. Never clear Cookies or
    # call set_storage_state against an already active profile.
    try:
        if cookies:
            await session.context.add_cookies(cookies)
        for item in state["origins"]:
            if not item["localStorage"]:
                continue
            page = await session.context.new_page()
            try:
                # Give this isolated helper document the actual storage origin
                # without requesting a login/logout endpoint or running its JS.
                async def blank(route):
                    await route.fulfill(status=200, content_type="text/html", body="<!doctype html><title>State import</title>")
                await page.route("**/*", blank)
                await page.goto(item["origin"] + "/", wait_until="domcontentloaded")
                await page.evaluate("entries => { for (const entry of entries) localStorage.setItem(entry.name, entry.value); }",
                    item["localStorage"])
            finally:
                await page.close()
    except Exception:
        raise InvalidArgument("SSO state import was incomplete; keep this profile, inspect its actual state and retry or log in normally") from None
    actual = filter_state(await session.context.storage_state(), selected)
    cookie_keys = {(item["name"], item["domain"], item["path"], item.get("partitionKey")):
        item for item in actual["cookies"]}
    stores = {item["origin"]: {entry["name"]: entry["value"] for entry in item["localStorage"]}
        for item in actual["origins"]}
    if (any(any(cookie_keys.get((item["name"], item["domain"], item["path"], item.get("partitionKey")), {}).get(key) != value
                for key, value in item.items()) for item in cookies)
            or any(stores.get(item["origin"], {}).get(entry["name"]) != entry["value"]
                for item in state["origins"] for entry in item["localStorage"])):
        raise InvalidArgument("browser did not retain the requested SSO entries; verify the current profile before reuse")
    return {"ok": True, "action": "import", "content_hash": expected_hash,
        "origins": selected, "account_key": account_key, "source_account_key": source_account,
        "cookie_count": len(cookies),
        "storage_origin_count": len(state["origins"]),
        "expired_cookie_count_skipped": len(state["cookies"]) - len(cookies),
        "preserves_unrelated_state": True, "profile_switched": False,
        "client_mock_installed_in_source": payload.get("client_mock_installed"),
        "backend_identity_verified_by_tool": False,
        "next": "Retry the target's normal SSO flow in this profile and verify its actual identity and role."}
