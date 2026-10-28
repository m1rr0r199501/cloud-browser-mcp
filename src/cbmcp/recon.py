"""A per-lease document-start recon shim; the upstream gateway owns the browser.

No model calls, browser launches, new state MCP tools or background scheduler.
Unmodified upstream responses and client mock observations have separate files.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

from .errors import InvalidArgument

MAX_CONFIG = 128_000
MAX_RECORDS = 1000
MAX_EVENT = 1_200_000


def capture_envelope(event, material, kind, digest, page_url, ident):
    """Use the existing capture adapter contract; no second importer is needed."""
    request = event.get("request", {})
    body = request.get("body", {})
    response = material if kind == "target_runtime" else event.get("response", {})
    response = response or {}
    text = response.get("body") or {}
    result = {
        "id": "recon" + str(ident),
        "url": request.get("url"),
        "method": request.get("method"),
        "request_headers": request.get("headers", {}),
        "post_data": body.get("text"),
        "post_data_truncated": body.get("truncated", False) or body.get("original_bytes") is False,
        "status": response.get("status"),
        "body": {
            "available": isinstance(text.get("text"), str),
            "truncated": text.get("truncated", False),
        },
    }
    if event.get("error"):
        result["failure"] = "fetch_failed"
    records = [{"tool": "net_request", "result": result}]
    if isinstance(text.get("text"), str):
        value = text["text"]
        try:
            parsed = json.loads(value) if not text.get("truncated") else None
        except ValueError:
            parsed = None
        records.append(
            {
                "tool": "net_body",
                "result": {
                    "request_id": result["id"],
                    "encoding": "json" if isinstance(parsed, (dict, list)) else "text",
                    "json": parsed,
                    "text": value,
                    "body_truncated_at_capture": text.get("truncated", False),
                },
            }
        )
    return {
        "source_kind": kind,
        "context": {"source_kind": kind},
        "config_hash": digest,
        "backend_verified": False,
        "page_url": event.get("page_url", page_url),
        "observed_at": event.get("observed_at"),
        "request": request,
        "material": material,
        "tool_results": records,
    }


def validate_config(value, scope):
    if not isinstance(value, dict):
        raise InvalidArgument("config must be an object")
    config = json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    if len(json.dumps(config, ensure_ascii=False).encode()) > MAX_CONFIG:
        raise InvalidArgument("config exceeds 128000 bytes")
    allowed = {
        "origins",
        "mode",
        "api_prefixes",
        "stubs",
        "neutralize",
        "fallback_body",
        "local_storage",
        "session_storage",
        "cookies",
        "block_login_paths",
        "init_script",
    }
    if set(config) - allowed:
        raise InvalidArgument("unknown recon config field")
    origins = config.get("origins")
    if not isinstance(origins, list) or not 1 <= len(origins) <= 20:
        raise InvalidArgument("origins must contain 1..20 exact HTTP(S) origins")
    for origin in origins:
        if not isinstance(origin, str):
            raise InvalidArgument("origin must be a string")
        u = urlsplit(origin)
        if (
            u.scheme not in {"http", "https"}
            or not u.hostname
            or u.username
            or u.password
            or u.path
            or u.query
            or u.fragment
        ):
            raise InvalidArgument("use exact origins without path, credentials or trailing slash")
        scope.enforce(origin + "/")
    if config.setdefault("mode", "forward") not in {"forward", "offline"}:
        raise InvalidArgument("mode must be forward or offline")
    for key, default in (("api_prefixes", ["/api/"]), ("block_login_paths", [])):
        items = config.setdefault(key, default)
        if (
            not isinstance(items, list)
            or len(items) > 100
            or any(not isinstance(p, str) or not p.startswith("/") for p in items)
        ):
            raise InvalidArgument(key + " must contain absolute path prefixes")
    stubs = config.setdefault("stubs", [])
    if not isinstance(stubs, list) or len(stubs) > 200:
        raise InvalidArgument("stubs must be an array of at most 200 exact-path rules")
    for stub in stubs:
        if (
            not isinstance(stub, dict)
            or set(stub) - {"path", "method", "body", "status", "headers"}
            or not isinstance(stub.get("path"), str)
            or not stub["path"].startswith("/")
            or "body" not in stub
        ):
            raise InvalidArgument("stub requires path and JSON body")
        if (
            not isinstance(stub.get("status", 200), int)
            or not 200 <= stub.get("status", 200) <= 599
        ):
            raise InvalidArgument("stub status must be 200..599")
        if stub.get("status", 200) in {204, 205, 304}:
            raise InvalidArgument("JSON stub cannot have an empty-body HTTP status")
        if "method" in stub and (
            not isinstance(stub["method"], str) or not stub["method"].isalpha()
        ):
            raise InvalidArgument("stub method must be an HTTP method")
    neutral = config.setdefault("neutralize", {})
    if not isinstance(neutral, dict) or set(neutral) - {
        "paths",
        "fields",
        "failure_values",
        "success",
        "flags",
        "http_status",
    }:
        raise InvalidArgument("invalid neutralize configuration")
    if neutral:
        for key in ("paths", "fields", "failure_values"):
            if not isinstance(neutral.get(key), list) or not neutral[key]:
                raise InvalidArgument(
                    "neutralize requires explicit paths, fields and failure_values"
                )
        if any(not isinstance(p, str) or not p.startswith("/") for p in neutral["paths"]):
            raise InvalidArgument("neutralize paths must be absolute path prefixes")
        if any(
            not isinstance(p, str)
            or not p
            or any(s in {"__proto__", "prototype", "constructor"} for s in p.split("."))
            for p in neutral["fields"]
        ):
            raise InvalidArgument("invalid neutralize field path")
        if "success" not in neutral or not isinstance(neutral.get("flags", {}), dict):
            raise InvalidArgument("neutralize requires success and an object of flags")
        if "http_status" in neutral and neutral["http_status"] != 200:
            raise InvalidArgument("the only optional HTTP status rewrite is explicit 200")
    for key in ("local_storage", "session_storage"):
        if not isinstance(config.get(key, {}), dict) or any(
            not isinstance(v, str) for v in config.get(key, {}).values()
        ):
            raise InvalidArgument(key + " must map keys to strings")
    if not isinstance(config.get("cookies", []), list):
        raise InvalidArgument("cookies must be an array")
    for cookie in config.get("cookies", []):
        if not isinstance(cookie, dict) or cookie.get("url") not in origins:
            raise InvalidArgument("mock cookie url must be one configured exact origin")
        if set(cookie) - {"url", "name", "value", "secure", "httpOnly", "sameSite"}:
            raise InvalidArgument("unsupported mock cookie attribute")
        if not isinstance(cookie.get("name"), str) or not isinstance(cookie.get("value"), str):
            raise InvalidArgument("mock cookie name/value must be strings")
    if not isinstance(config.get("init_script", ""), str):
        raise InvalidArgument("init_script must be Worker-authored JavaScript text")
    return config


class Recon:
    def __init__(self, session, config):
        self.config = config
        self.digest = hashlib.sha256(
            json.dumps(config, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        self.binding = "__cbmcp_recon_" + secrets.token_hex(8)
        self.marker = "__CBMCP_RECON_" + secrets.token_hex(8)
        self.root = session.dir / "recon"
        self.root.mkdir(mode=0o700, exist_ok=True)
        self.rows = []
        self.dropped = 0
        self.lock = asyncio.Lock()
        self.context = None
        self.bytes_saved = 0
        self.byte_limit = min(session.s.lease_disk_quota_bytes // 4, 64 * 1024 * 1024)

    async def ingest(self, source, event):
        async with self.lock:
            if len(self.rows) >= MAX_RECORDS:
                self.dropped += 1
                return
            if not isinstance(event, dict):
                return
            raw = json.dumps(event, ensure_ascii=False, allow_nan=False)
            if (
                len(raw.encode()) > MAX_EVENT
                or self.bytes_saved + len(raw.encode()) * 2 + 4096 > self.byte_limit
            ):
                self.dropped += 1
                return
            ident = len(self.rows) + 1
            row = {
                "id": ident,
                "transport": event.get("transport"),
                "layer": event.get("layer"),
                "method": event.get("request", {}).get("method"),
                "path": urlsplit(event.get("request", {}).get("url", "")).path,
                "request_sent": event.get("request_sent"),
                "backend_verified": False,
                "config_hash": self.digest,
                "files": [],
            }
            for kind, material in (
                ("target_runtime", event.get("upstream")),
                ("mock_runtime", {k: v for k, v in event.items() if k != "upstream"}),
            ):
                if material is None:
                    continue
                path = self.root / f"{ident:06d}-{kind}.json"
                record = capture_envelope(
                    event, material, kind, self.digest, source["page"].url, ident
                )
                data = json.dumps(record, ensure_ascii=False, indent=2).encode()
                if self.bytes_saved + len(data) > self.byte_limit:
                    self.dropped += 1
                    break
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as out:
                    out.write(data)
                self.bytes_saved += len(data)
                row["files"].append(
                    {
                        "path": str(path),
                        "source_kind": kind,
                        "content_hash": hashlib.sha256(data).hexdigest(),
                        "bytes": len(data),
                    }
                )
            self.rows.append(row)

    async def attach(self, session):
        if self.context is session.context:
            return
        context = session.context
        if context is None:
            raise InvalidArgument("browser context is unavailable")
        await context.expose_binding(self.binding, self.ingest)
        cookies = self.config.get("cookies", [])
        if cookies:
            await context.add_cookies(cookies)
        template = Path(__file__).with_name("recon_preload.js").read_text()
        payload = {
            "config": self.config,
            "marker": self.marker,
            "binding": self.binding,
            "hash": self.digest,
        }
        script = "(" + template + ")(" + json.dumps(payload, ensure_ascii=False) + ");"
        custom = self.config.get("init_script", "")
        if custom:
            # Execute as a real init script, not eval/new Function blocked by CSP.
            marker = "globalThis[" + json.dumps(self.marker) + "]"
            script += (
                "\nif ("
                + json.dumps(self.config["origins"])
                + ".includes(location.origin) && "
                + marker
                + " && !"
                + marker
                + ".custom_started) { "
                + marker
                + ".custom_started=true;\n"
                + "try { (function(){\n"
                + custom
                + "\n})(); "
                + marker
                + ".custom_done=true; }\n"
                + "catch (_) { "
                + marker
                + ".errors.push('custom_init_failed'); }\n}"
            )
        await context.add_init_script(script=script)
        self.context = context

    async def status(self, session):
        pages = {}
        for ident, handle in session.pages.items():
            if not handle.closed:
                try:
                    pages[ident] = await handle.page.evaluate(
                        "k => globalThis[k] || null", self.marker
                    )
                except Exception:
                    pages[ident] = {"unavailable": True}
        return {
            "ok": True,
            "installed": True,
            "config_hash": self.digest,
            "mode": self.config["mode"],
            "pages": pages,
            "record_count": len(self.rows),
            "dropped_records": self.dropped,
            "record_root": str(self.root),
            "bytes_saved": self.bytes_saved,
            "capture_byte_limit": self.byte_limit,
            "mutable": False,
        }
