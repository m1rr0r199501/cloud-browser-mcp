# Browser state files and persistent profiles

`browser_state` is a native tool in the storage group, available even when the
JavaScript debugger group is disabled. Use the normal `python -m cbmcp.server`
entrypoint. Version0.3.0 requires Playwright1.64.1 or later for native context
state restoration; it connects to the existing cloud browser and launches no
additional browser driver.

## Keep the current account-specific profile

Use HTTP when an existing target contract/session suffices; acquire a browser
only for page login, dynamic construction or interaction. The Planner can name
the account, exact target session and its profile without reserving a lease.
Different actual accounts use separate profiles. Related SSO applications may
copy the selected account's SSO state into their own current profiles without
waiting for a shared profile. Each target's current identity/role is still checked.
The browser applies Cookie Domain/Path/Secure rules;
localStorage remains exact-origin storage. A business application can normally
visit its IdP and exchange that IdP session for its own application session.
An application's token is not made interchangeable by a shared profile name.

Only one lease can hold any one profile at a time; releases still save it as
before. Separate profiles remain independent copies, with no live synchronisation.

## Export selected state

```python
saved = browser_state(
    lease=actual_lease,
    action="export",
    origins=["https://app.example", "https://login.example"],
    account_key="user_a",
)
```

Origins are exact HTTP(S) origins without a path, trailing slash, query,
credentials or wildcard. Default ports are canonicalised and lease scope is
checked. The private file preserves applicable Cookie attributes and the
selected origins' localStorage. Its receipt gives only the gateway path,
SHA256, size and counts. Files are mode600 under a mode700 auth-state directory.
The64MiB artifact limit fails explicitly rather than retaining a truncated
snapshot. Copy/register the actual file before its lease artifacts are cleaned
when the caller needs a durable recovery resource.

This format covers cookies and localStorage. It does not claim to preserve
sessionStorage, page memory, IndexedDB or every component of a browser profile.
Mock instrumentation in the source is recorded; export never proves a backend
identity or vulnerability.

## Merge SSO into a profile that is already in use

```python
browser_state(
    lease=current_lease,
    action="import",
    state_path=actual_transferred_file,
    expected_hash=saved["content_hash"],
    origins=["https://login.example"],
    account_key="user_a",
)
page_navigate(lease=current_lease, url="https://app.example")
```

Import accepts exactly the observed SSO origin. It adds/updates the selected
Cookie keys and localStorage entries, leaving other entries and applications
intact, with no profile switch or additional lease. The receipt reports both the requested account_key and source_account_key for
the agent to interpret; different account labels do not block import. Explicitly
mocked sources are still rejected before mutation. Legacy
untagged state still requires source/identity verification. The account tag
does not prove who is already signed into a profile; callers must choose the
corresponding profile, then verify the actual target identity after normal SSO.
Expired cookies are skipped. A temporary routed blank document writes origin
storage without executing the real login site's JS; it closes afterwards.
An interrupted import may have applied some selected entries; inspect/retry or
log in normally rather than assuming transactional rollback.

## Restore before navigation into an empty context

```python
browser_state(
    lease=actual_fresh_lease,
    action="restore",
    state_path=actual_transferred_file,
    expected_hash=saved["content_hash"],
    origins=["https://app.example", "https://login.example"],
)
page_navigate(lease=actual_fresh_lease, url="https://app.example")
```

The file must be gateway-readable, regular, bounded, and match its expected
hash. A standard Playwright cookies/origins JSON file is also accepted. The
caller may select a scoped subset or a sibling origin to which an exported
parent-domain Cookie applies; host-only cookies and other origins' localStorage
do not become that sibling's state. Unsupported additional storage components
are rejected rather than silently discarded.

Restoration uses the existing cloud context's native set_storage_state API.
It is allowed only before business navigation and when the context has no
existing Cookie/storage state. Existing kept profile state is left intact;
reuse that profile directly or acquire a genuinely clean lease for the intended
recovery. Omitting profile is not necessarily clean if an operator configured a
default profile. The restored fields are read back and compared, with order
differences normalised. The caller must then verify the actual target identity
and role. A successful restore receipt always has backend_identity_verified_by_tool=false.

## Verification

Offline contracts run with `python -m pytest tests/ -q`. The explicitly invoked
`scripts/storage_smoke.py --output /actual/path/result.json` uses a real cloud
browser, one owned temporary profile and controlled A/IdP/B pages. It checks
profile persistence, cross-subdomain Cookie/SSO navigation, state-file restore,
exact-origin storage and that existing profiles are not overwritten. Only its
own test sessions/profile are stopped/deleted, and results contain no key,
lease token, signed CDP URL or credential values. It never logs into a real
business target.
