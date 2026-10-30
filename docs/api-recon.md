# Document-start API recon

`js_recon` is a native tool in the JavaScript group. Run the normal
`python -m cbmcp.server` entrypoint; no integrating application, database or
outer launcher is required. Disabling `CBMCP_ENABLE_DEBUGGER_TOOLS` also hides
this tool.

The purpose is to discover requests constructed by the real frontend even when
its login, menu or permission checks prevent business modules from mounting.
Read the target's code first, then configure its actual response structure and
client-side success values. A permissive client mock does not create or prove a
valid backend session.

## Execution

Use a clean browser lease. Install before navigation:

```python
lease = browser_acquire(
    task_label="api-recon", scope_allow=["target.example"],
    idempotency_key="this-recon-work", debug="full"
)["lease"]
js_recon(lease=lease, action="install", config={
    "origins": ["https://target.example"],
    "mode": "forward",
    "api_prefixes": ["/api/"],
    "stubs": [{
        "path": "/api/menu", "method": "GET", "status": 200,
        "body": {"code": 0, "data": {"items": ["reports"]}}
    }],
    "neutralize": {
        "paths": ["/api/bootstrap/"],
        "fields": ["code"], "failure_values": [401], "success": 0,
        "flags": {"message": "client mock"}
    },
    "local_storage": {"client-login-placeholder": "1"},
    "session_storage": {},
    "block_login_paths": ["/login"]
})
page_navigate(lease=lease, url="https://target.example/reports")
js_recon(lease=lease, action="status")
# Drive permitted module/tab/form interactions, then inspect captures.
js_recon(lease=lease, action="records", offset=0, limit=20)
browser_release(lease=lease, reason="captures saved")
```

This example's menu and response fields are illustrative. Replace them with the
structure actually consumed by the target. Verify the returned configuration
hash and per-page startup marker, actual UI/module behavior and resulting
requests. Installing after a page loaded does not retroactively cover startup:
navigate or reload after installation.

## Configuration

| Field | Behavior |
|---|---|
| `origins` | Required exact HTTP(S) origins, including nondefault ports, without paths, trailing slash, query or credentials. Checked against the lease scope. Only these page and request origins are instrumented. |
| `stubs` | L1 exact URL pathname and optional method. Query is excluded from matching. Each rule has JSON `body`, optional `status` (default 200) and response `headers`. L1 wins in both modes and does not send the request. Use target-specific code if matching must distinguish query or other complex conditions. |
| `mode="forward"` | Unmatched requests use native fetch/XHR in the cloud browser. They are not replayed from the gateway host. Network failures are not converted into fake successes or automatically retried. |
| `neutralize` | L2 applies only to configured path prefixes, JSON objects and explicitly matched `failure_values` at the specified dot-separated `fields`. Requires `success`; optional root-level `flags` apply only after a match. HTTP status is preserved by default. Explicit `http_status:200` can let the client continue after a real HTTP 401; the original response is saved separately. |
| `mode="offline"` | L3 supplies an empty success body to unmatched `api_prefixes` (default `/api/`) without sending them. It leaves non-API traffic alone. |
| `fallback_body` | Optional target-specific L3 JSON body. The default is `{code:0,data:[]}` for list-like paths and `{code:0,data:{}}` otherwise. A default body shape alone does not prove a module mounted. |
| `local_storage`, `session_storage` | Key/string pairs written at document start. Prepare JSON/base64/etc. according to the real decoder. Placeholders are not validated sessions. |
| `cookies` | Optional current-lease mock cookies: `url` must be a configured exact origin; require `name` and string `value`; optional `secure`, `httpOnly`, `sameSite`. Use a clean lease instead of modifying an unrelated kept profile. |
| `block_login_paths` | Same-origin login path prefixes. Wraps history navigation and attempts Location methods. Chromium may make Location methods unmodifiable; page status reports that limitation. It does not guarantee suppression of every full-page redirect. |
| `init_script` | Optional analyst-authored, target-specific JavaScript, executed in the same init script after the common shim, before business scripts on configured origins. Useful for client checks not covered by generic rules. It runs once per document, including after reconnect; check `custom_done` or `custom_init_failed` in page status. |
| `network_rules` | Optional selected-request driver faults/delays. Exact `path`, optional `method`, and exactly one of `error` or `delay_ms`; see below. Only fetch/XHR from a configured page origin to a configured request origin; the first matching rule wins. |

### Selected-request failures and delays

Use when observing a retry, fallback, refresh or other client branch is useful.
Install before navigation through the same `js_recon` tool:

```json
{
  "origins": ["https://target.example"],
  "mode": "forward",
  "stubs": [{"path":"/api/slow","body":{"data":"loaded"}}],
  "network_rules": [
    {"path":"/api/offline","method":"GET","error":"internetdisconnected"},
    {"path":"/api/slow","method":"GET","delay_ms":3000}
  ]
}
```

Paths/data are examples, not a site-independent recipe. Supported driver abort
codes are `connectionrefused`, `timedout`, `connectionreset` and
`internetdisconnected`. They are actual browser request aborts, not HTTP500 or
a JavaScript-only rejected Promise. XHR may dispatch `timeout` for a network
timeout; inspect the actual event and network error. Delay is an integer
0..120000 milliseconds (a tool limit, not a research stopping rule); it is
cancelled if the lease ends. End-to-end latency can be longer than that wait.

An error overrides L1/L2/L3 and does not send that selected request. A delay
waits, then preserves the ordinary result: L1 still supplies its configured
JSON; offline API L3 stays synthetic; otherwise the browser forwards and the
original L2 patching still applies. A delay rule supplies no body of its own:
the three-second example puts the response in a same-path L1 stub. The shim
allows these selected requests to reach the driver so L1/L3 cannot swallow the
effect. Unmatched requests retain the existing path, including scope checks.

Only pathname is matched: no query/fragment, no wildcard, and a missing method
means all methods. The first matching rule wins. Document navigation, WebSocket
and other resource types are not included. Service Workers or uninstrumented
frames/realms can bypass this method; no applied record means no claim of
coverage. `page_emulate(network="offline"/slow presets)` remains the separate
whole-tab network-condition tool.

Status reports rule count/transport and pending interventions, not proof a rule
has run. Capture rows/files record `network_intervention` with rule index,
config hash, requested error/wait, state and elapsed time. An intervention file
is always `mock_runtime`; it never fabricates an upstream response. A real
forward response after a delay is captured separately as `target_runtime`.
Driver-fulfilled L1/L3 responses also appear in network tools, visibly marked
`source_kind="mock_runtime"`. `net_request`/`net_body` retain this provenance;
HAR uses `_sourceKind` and `_networkIntervention` per entry. Preserve these
fields when importing; an HTTP200 in the network panel is not sufficient to
identify a genuine upstream response.

Keep the normal comparison, config/script resources and actual observed client
branch. `cancelled`/`failed` is not `applied`. Release cancels outstanding delays
and removes this lease's handler; reconnect reattaches it for future requests.
Reload and verify actual behavior after reconnect. Configuration changes still
require saving material and obtaining a clean lease.

Configuration is immutable for a lease. Reinstalling identical configuration is
idempotent; changing it returns an error. Save captures, release the browser,
then acquire a clean lease to adjust permission trees, switch modes or remove
the instrumentation. This avoids stale init scripts across Playwright versions.

The same live gateway reinstalls the configuration after reconnecting to its
cloud browser. Reload and check the marker to reproduce startup. A gateway
process restart is handled by normal lease recovery; recreate a clean lease
and reload the saved configuration rather than guessing it from a summary.

## Transports and captures

fetch supports `Request` inputs and init/body overrides. XHR maintains
per-instance request information, readystatechange/load events and JSON/text
response behavior. Generic JSON stubs do not represent genuine binary downloads
or event streams. FormData captures retain field/file metadata rather than file
bytes. Response text is capped at 65,536 characters with an explicit truncation
flag; use the network tools to obtain additional genuine material when needed.

`action="records"` returns only a bounded index (limit 1..50), with real
`next_offset`, configuration hash and private file paths/hashes. It does not
inline all response bodies into agent context. Files are mode 600 under the
lease's `recon/` directory:

- `target_runtime` holds an unmodified upstream response separately.
- `mock_runtime` holds the client presentation or simulated response.
- L2's original 401 cannot be overwritten by the 200 presented to the client.
- XHR `responseType="json"` is marked as reserialized parsed JSON, not original
  network bytes. `backend_verified` remains false.

Each JSON file includes a `tool_results` envelope using `net_request` and
`net_body` shapes for downstream importers, together with the original material
and provenance. Register/copy the actual protected file before releasing the
lease when the integrating workflow requires durable evidence. Treat mock
observations as client construction, not proof of backend acceptance.

Capture storage is bounded by 1,000 records and at most 64 MiB or one quarter
of the lease disk quota, whichever is smaller. `dropped_records` means material
was not retained. Paging cannot restore bytes already discarded at capture.

## Verification

Offline tests require no cloud account or local browser:

```sh
python -m pytest tests/ -q
```

The explicit cloud smoke test prompts for the key with terminal echo disabled:

```sh
python scripts/recon_smoke.py --output /private/tmp/recon-smoke.json
python scripts/recon_smoke.py --stdio-only --output /private/tmp/recon-stdio.json
python scripts/recon_smoke.py --network-rules-only --output /private/tmp/network-rules.json
```

The first uses the real cloud gateway/tools with a controlled fixture served
over CDP, then checks external HTTPS at example.com. It covers both modes,
fetch/XHR JSON/text/events, initialization before business code, reload,
reconnect, new tabs, separate upstream/mock files, pagination and immutable
configuration. It releases each temporary lease and confirms the platform
session stopped. It does not launch local Chromium, log in, or use real users'
data. The stdio-only mode exercises the actual `cbmcp.server` entrypoint and
lease validation without renting a browser. The output omits the key and signed
connection URLs.
