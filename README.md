# cbmcp — cloud browser gateway for parallel agents

An MCP server that rents [Browser Use Cloud](https://docs.browser-use.com/cloud/browser/quickstart)
browsers on demand for many subagents at once, and gives each one page control,
HTTP observation, **real JavaScript debugging** (breakpoints, call stacks, scope
inspection) and **function hooking**.

Browsers are created when an agent asks and destroyed when it is done. There is
no warm pool and no fixed instance count: with no work in flight, zero sessions
run and nothing is billed. The only limit is a configurable ceiling, further
clamped by the cloud account's own concurrency quota.

```
Claude Code  ──stdio MCP──▶  cbmcp gateway (one process)
                                 │  lease registry · concurrency ceiling · FIFO queue
                                 │  budget reservation · idle reaper · platform reconciliation
                                 ├─ lease A ──wss CDP──▶ cloud browser A   (its own sandbox)
                                 ├─ lease B ──wss CDP──▶ cloud browser B
                                 └─ lease C ──wss CDP──▶ cloud browser C
```

---

## Why not an existing browser MCP

Several good browser MCPs exist. None of them solves the problem this one is
built for, and two of them cannot be composed into it.

| Project | What it gives | Why it is not enough here |
|---|---|---|
| [chrome-devtools-mcp](https://github.com/ChromeDevTools/chrome-devtools-mcp) | Strong network + performance tools | Wants an **HTTP** CDP endpoint; Browser Use hands out `wss://` with a token. One browser per process. |
| [playwright-mcp](https://github.com/microsoft/playwright-mcp) | Excellent page driving | One browser per process; weak network detail; no debugger. |
| [cdp-browser-mcp](https://github.com/killaragorn/cdp-browser-mcp) | **Multi-session**, takes a `cdp_url` at runtime — fits Browser Use directly | The **agent** must obtain the CDP URL, so it needs the cloud API key and must remember to stop the session. `session_id` is agent-supplied, so any agent can address another's browser. No lease, quota, queue, idle reclaim or budget. No debugger, no hooks. |
| [js-reverse-mcp](https://github.com/zhizhuodemao/js-reverse-mcp), [JSReverser-MCP](https://github.com/NoOne-hub/JSReverser-MCP), [@ai-jshook/mcp](https://www.npmjs.com/package/@ai-jshook/mcp) | **Function hooking** and JS reverse-engineering workflows — the idea this project adopted | Single local headed Chrome; no multi-tenancy; one Node process per browser (~100 MB each); tool names differ between projects. |

The decisive point is not preference, it is **architecture**: the debugger needs
exclusive use of the CDP `Debugger` and `Runtime` domains. Two MCP clients on one
CDP endpoint fight over pause state, so "external MCP drives pages, we drive the
debugger" cannot work. Hooking is therefore implemented natively here, using the
same mechanism those projects use (`Page.addScriptToEvaluateOnNewDocument`).

What *is* reused: **Playwright** for the CDP connection, frame handling, locators
with auto-waiting, and request/response plumbing. The protocol work is not
reimplemented — only the multi-tenant resource management and the debug layer on
top of it.

---

## Install

Requires Python 3.11+ and a Browser Use Cloud API key.

```bash
cd cloud-browser-mcp
uv venv --python 3.12 .venv && . .venv/bin/activate
uv pip install -e ".[dev]"
cp .env.example .env     # then put BROWSER_USE_API_KEY in it
```

Verify the config and the account before wiring it to an agent:

```bash
python -m cbmcp.cli check
```

That prints the effective limits, the **worst-case cost per lease and for all
slots**, and the account's real concurrency quota. Register it with Claude Code:

```bash
claude mcp add cloud-browser -- /abs/path/cloud-browser-mcp/.venv/bin/python -m cbmcp.server
```

No local Chromium is needed — the gateway connects out over CDP. For free offline
development set `CBMCP_PROVIDER=local`, which launches local Chromium instead
(`playwright install chromium` first).

### macOS

Works on Apple Silicon and Intel Macs with no extra steps beyond the above. Notes:

- **Python.** The system `python3` on macOS is too old (3.9). Use `uv` as shown
  (it downloads a suitable Python), or Homebrew's `python@3.12`.
- **Use an absolute path** to `.venv/bin/python` when registering the server.
  GUI apps such as Claude Desktop start servers with a minimal `PATH` and `/` as
  the working directory; the `.env` in the project directory is still found.
- **Several Claude windows are fine.** Each window starts its own gateway, all
  sharing `~/.local/state/cbmcp`. Gateways heartbeat into the database, so a new
  one never stops another live one's browsers, and a crashed one's leftovers are
  picked up within about a minute. `CBMCP_MAX_CONCURRENCY` is **one ceiling
  shared by all windows** (set `CBMCP_SHARE_CONCURRENCY_ACROSS_GATEWAYS=false`
  for per-window ceilings). If the cloud account is still full -- other users,
  or a window that just started -- `browser_acquire` queues and retries instead
  of failing. A slot freed in one window reaches an agent waiting in another
  within about two seconds (waiters re-check the shared count while they wait).
- **Local provider.** `CBMCP_PROVIDER=local` uses Playwright's Chromium, or
  Google Chrome / Chromium / Edge / Brave from `/Applications` or
  `~/Applications`. `CBMCP_CHROMIUM_PATH` may point at the executable or at the
  `.app` bundle. Chrome is started with `--use-mock-keychain`, so a fresh profile
  does not pop a Keychain password prompt.
- **Owner tag.** A Mac's hostname changes with the network it is on, which used
  to change the default owner tag and hide this machine's own leftover sessions
  from reconciliation. The tag is now fixed on first run in
  `<data dir>/owner_tag`.
- **Shutdown.** SIGTERM/SIGHUP (what MCP clients and a closing Terminal send)
  now run the same orderly release as Ctrl-C, and the open-file limit (256 by
  default for GUI-launched processes) is raised to 4096 at start.

---

## The cost guarantees

Agent prompts cannot be trusted to release browsers, and a Claude Code subagent
shares its parent's MCP connection, so there is no socket close to detect its
exit. Reclamation runs on four independent layers, each holding if the ones above
it fail:

| Layer | Mechanism | Survives |
|---|---|---|
| 1 | **Idle timeout** — every tool call touches the lease; no calls for `CBMCP_IDLE_TIMEOUT_SECONDS` reclaims it | an agent that crashed, was cancelled, or wandered off |
| 2 | **Absolute lease lifetime** (`CBMCP_MAX_LEASE_SECONDS`) regardless of activity | a wedged loop that keeps calling tools |
| 3 | **Reconciliation** against the platform's own session list, both directions | the gateway restarting, or losing track |
| 4 | **Platform-side `timeout`** set on every session at creation | `kill -9`, a power cut, or a bug in layers 1–3 |

Layer 4 is the one that matters most and is the cheapest to get right: Browser
Use accepts a `timeout` in minutes (1–240) per session. Nothing this process does
or fails to do can extend it.

Three further details that each exist because the obvious version is wrong:

- **Creation is written down before it happens.** A `create_intents` row is
  committed *before* the provider call, and every session carries an owner tag in
  its platform `metadata`. So a crash between "API call succeeded" and "we
  recorded the id" still leaves two independent ways to find the session.
- **Budget is reserved, not just summed.** Checking "spent so far vs the cap"
  passes for all N of N simultaneous acquires. Instead each lease reserves its
  worst-case cost up front and settles against the platform's reported figure on
  release.
- **An unconfirmed stop keeps its concurrency slot.** A session we cannot prove
  is stopped may still be billing. Freeing the slot would over-report capacity
  and earn HTTP 429s from the platform.

`proxyCountryCode` defaults to `us` server-side — the billed proxy is **on**
unless you send `null`. `CBMCP_NETWORK_MODE=direct` (the default here) sends
`null` explicitly.

---

## Isolation, and its honest limit

Each lease is a **different cloud browser in a different sandbox**, so pages,
cookies, storage, login state, network log and execution context are separated by
construction, not by convention.

Access is controlled by a **capability token**: 32 bytes of entropy returned once
by `browser_acquire`, required on every call, stored only as a SHA-256, and never
written to a log or an audit record.

The limit, stated plainly: in Claude Code, subagents share the parent's MCP
connection, so the server cannot tell from the transport which subagent is
calling. Possession of the token therefore *is* the identity. This buys
unguessability and clean separation between concurrent tasks. It does **not**
defend against an agent that has been handed someone else's token — which is why
tokens never appear in tool output. The realistic threat here is prompt injection
from a page under study, not agents attacking each other.

Per-lease **scope enforcement** is available and is applied at the request layer
(Playwright routing), not just on navigation, because page JavaScript, redirects
and third-party includes all reach hosts the agent never typed:

```python
browser_acquire(task_label="audit", scope_allow=["example.com", "*.cdn.example.com"])
```

A lease can only ever narrow `CBMCP_SCOPE_ALLOW`, never widen it.

Two tools are confined for the same prompt-injection reason. `page_upload`
only sends files from `CBMCP_UPLOAD_DIR` (default `<data dir>/uploads`), so a
page cannot talk an agent into uploading `~/.ssh` or other local files.
`admin_sessions(action='stop')` only stops sessions this deployment created;
use `cbmcp stop <id>` for anything else on the account.

---

## Tools

49 tools in six groups. Set `CBMCP_ENABLE_DEBUGGER_TOOLS=false` or
`CBMCP_ENABLE_ADMIN_TOOLS=false` to trim the agent's tool list and context.

**Lease (5)** · `browser_acquire` `browser_queue_status` `browser_release`
`browser_status` `browser_renew`

**Page (16)** · `page_navigate` `page_tabs` `page_click` `page_type`
`page_select` `page_press` `page_scroll` `page_wait` `page_upload`
`page_emulate` `page_cookies` `page_hover` `page_drag` `page_mouse`
`page_dialog` `page_downloads`

The page tools cover what a person does in a browser, not just what a test
script does: hover menus (`page_hover`), drag and drop and sliders
(`page_drag`), raw mouse at screenshot coordinates for canvas, maps and image
challenges (`page_mouse`; screenshots are in CSS pixels so the two line up),
typing into whatever has focus (`page_type` without a target), answering
`confirm()`/`prompt()` with OK and text (`page_dialog`; Cancel by default),
files the page downloads (`page_downloads`, saved on the gateway host), "Allow"
on permission prompts (`page_emulate(grant_permissions=[...])`), and switching
tabs, which brings the tab to the front. Every action result also reports what
a person would notice: tabs the page opened, dialogs it showed, files it
downloaded.

**Inspect (7)** · `page_snapshot` `page_text` `page_html` `page_screenshot`
`page_eval` `page_console` `page_inventory`

**Network (6)** · `net_requests` `net_request` `net_body` `net_websocket`
`net_har` `net_clear`

**JavaScript (12)** · `js_scripts` `js_source` `js_search` `js_breakpoint`
`js_pause_state` `js_eval_frame` `js_step` `js_properties` `js_options`
`js_hook` `js_hook_records` `js_export_bundle`

**Admin (3)** · `admin_stats` `admin_sessions` `admin_audit`

`page_snapshot` deliberately includes what a visual or accessibility snapshot
drops: **hidden input fields with their values** (CSRF tokens, signed state),
form `action`/`method`/`enctype`, and field `name`/`type`/`pattern`. Pass a `ref`
to it for one element in full, including the **event listeners** bound to it with
their source locations — which is CDP-only information, unreachable from page JS.

---

## The JavaScript debugging workflow

This is what the gateway is for. The intended path, which the tool descriptions
also teach the model:

```python
lease = browser_acquire(task_label="login analysis")["lease"]
page_navigate(lease, url="https://target.example/login")

# 1. Who made this request? The initiator is the JS call stack that caused
#    it -- no breakpoint needed, and it names the line to break on next.
net_requests(lease, resource_types=["xhr", "fetch"])
net_request(lease, request_id="r42")          # -> initiator.call_stack

# 2. Find the code. Searches every loaded script's text, minified bundles
#    included, so a parameter or header name leads straight to its source.
js_search(lease, query="signature")

# 3. Watch what actually happens, without stopping the page. Each record
#    carries the arguments, the return value and the CALL STACK.
js_hook(lease, targets=["xhr", "json", "crypto", "encode"])
page_type(lease, ref="e4", text="user"); page_click(lease, ref="e7")
js_hook_records(lease, text_contains="sign")

# 4. Stop where the request is built. Either by endpoint, or straight from
#    a string in the code -- the usual way into a minified bundle.
js_breakpoint(lease, kind="xhr", url_substring="/api/login")
js_breakpoint(lease, kind="text", text="X-Signature")
page_click(lease, ref="e7")

# 5. Read what page_eval can never see: locals, arguments, closures.
js_pause_state(lease)
js_eval_frame(lease, expression="JSON.stringify({salt, nonce, raw})")
js_step(lease, action="resume")

# 6. Package it so the algorithm can be re-run outside the browser:
#    source + captured environment + input/output vectors + a Node scaffold.
js_export_bundle(lease, script_urls=["app.min.js"], hook_labels=["subtle.sign"])
```

Step 6 is the part that turns understanding into a result. `node rebuild.js`
runs every captured input through your reimplementation and compares it against
what the page actually produced, so "I think this is the algorithm" becomes
"this reproduces every observed call".

Hooks and breakpoints answer different questions, which is why both exist. A
breakpoint stops the world and needs you to know *where* first. A hook keeps the
page running and records every call, so the stack tells you where to look next.
`js_hook(break_on_match="sign")` bridges them: the hook fires `debugger;` only
when an argument matches, stopping execution *inside* the signing routine.

Hook presets: `xhr` `fetch` `websocket` `json` `cookie` `storage` `crypto`
`encode` `eval` `timer` `dom`, plus `custom_paths=["window.app.sign"]` for
anything else.

While execution is paused the renderer is blocked, so page tools refuse to run
and say so. That is deliberate — the alternative is a tool call that hangs for
its full timeout.

---

## What the network tools can and cannot see

Stated explicitly, because conflating CDP observation with packet capture leads
to wrong conclusions.

**Can see** — request URL, method, headers, query, post body; response status,
headers, body; redirect chains; failures with Chromium's error text; resource
type; WebSocket frames both ways. `net_har` exports HAR 1.2 for existing tooling.

**Cannot see** — TLS handshakes and certificates, HTTP/2 or QUIC frame detail,
other processes' traffic, extension traffic. This is browser-level observation,
not `tcpdump`.

**Partial** — Service Worker and cross-process iframe traffic depend on
Chromium's auto-attach. Requests made before the recorder attached (before
`browser_acquire` returns) are absent. Response bodies are pulled from the
browser's buffer as responses arrive and spilled to disk, because after a
navigation the previous document's buffer is gone.

Bodies never accumulate in memory: metadata lives in a bounded ring buffer and
payloads go to per-lease files under a byte quota with oldest-first eviction.
Tool output is clipped with explicit `next_offset` continuation — **agent context
is usually more expensive than browser time**, so screenshots are JPEG by
default and snapshots, request lists and bodies all paginate.

---

## Measured fingerprint cost

The architecture is irrelevant to detection: a site sees the cloud browser's IP
and the browser's behaviour, never the gateway. Everything detectable happens
*inside* the browser, so `debug` chooses what to pay for:

| mode | capability | added to the page |
|---|---|---|
| `off` | driving, network capture, screenshots | nothing |
| `hooks` | function hooks, call stacks, arguments | native functions replaced (disguised) |
| `full` *(default)* | the above plus breakpoints, call frames, scope, `break_on_match` | also `Runtime.enable` / `Debugger.enable` |

`scripts/fingerprint_check.py` measures it against a bot-detection page. Results
from a real run (`bot.sannysoft.com`, two cloud browsers, $0.001 total):

| probe | `debug=off` | `debug=full` + hooks |
|---|---|---|
| `navigator.webdriver` | False | False |
| known automation globals | `[]` | `[]` |
| `Object.keys(window)` anomaly | `[]` | `[]` |
| `Object.getOwnPropertyNames(window)` anomaly | 1 | 2 |
| `JSON.stringify.toString()` | native | **native** |
| `JSON.parse` / `fetch` / `xhr.send` / cookie getter `.toString()` | native | **native** |
| `Runtime.enable` console-getter probe | False | **False** |
| `debugger;` freezes the page | False | **False** (suppressed by default) |
| random `data-*` attributes in the DOM | 7 | 7 |
| hook records captured | n/a | 508 |

What was fixed because this test found it:

- **`toString` spoofing.** Without it, one `JSON.stringify.toString()` exposed
  the whole scheme. Hooked functions now report `[native code]` via a WeakMap of
  wrapper→original, and the patched `Function.prototype.toString` reports native
  for itself too.
- **The CDP binding was enumerable.** `Runtime.addBinding` installs a global
  function that a plain `Object.keys(window)` scan finds. The reference is now
  captured and the global property deleted.
- **Randomised names.** The runtime's namespace and the element-ref attribute are
  random per lease, so no fixed signature exists to grep for.
- **`debugger;` suppression.** Pausing starts **off** (`setSkipAllPauses`) and is
  switched on only when a breakpoint is actually set. An anti-debug `debugger;`
  in page code is then a no-op, which also removes the timing signature such
  pages look for.

What remains detectable, honestly:

- `Object.getOwnPropertyNames(window)` still finds one randomly-named
  non-enumerable property (two with hooks). Randomisation defeats *signature*
  matching; it does not defeat *anomaly* detection against a known-clean
  baseline, which advanced fingerprinters do perform.
- `page_snapshot` tags elements with a random `data-*` attribute, visible to a
  MutationObserver.
- With pauses deliberately enabled, the `debugger;` timing signature returns.

Browser Use's own stealth work is doing the heavy lifting on the rest:
`navigator.webdriver` absent, 5 plugins, a real `chrome` object, a Windows Chrome
UA. The detection page reported "WebDriver: missing (passed)" and "WebDriver
Advanced: passed" in both modes.

---

## Operations

```bash
python -m cbmcp.cli check        # config + account reachability + worst-case cost
python -m cbmcp.cli sessions     # what the account is actually running right now
python -m cbmcp.cli reconcile    # stop orphans, settle unconfirmed stops, reclaim slots
python -m cbmcp.cli usage        # spend today and this month
python -m cbmcp.cli audit        # recent lifecycle records
python -m cbmcp.cli stop <id>    # stop one session
python -m cbmcp.cli stop-all     # stop everything this deployment created
python -m cbmcp.cli smoke        # end-to-end: acquire → drive → debug → release → verify zero
```

`stop-all` and `sessions` are the two to remember: they answer "am I being
billed for something I forgot about" without needing the gateway to be healthy.

State lives in SQLite at `CBMCP_DATA_DIR` (default
`~/.local/state/cbmcp/cbmcp.sqlite3`): leases, create intents, platform sessions,
daily usage and the audit trail.

---

## Measured behaviour

From the runs above, so expectations are calibrated:

- **Cold start is 8–47 seconds** (median ~10s, worst observed 47s). This is why
  `browser_acquire` blocks for a bounded `CBMCP_ACQUIRE_BLOCK_SECONDS` (25s,
  comfortably under the MCP client's tool timeout) and then hands back a ticket.
  Fulfilment continues in the background, so the lease is usually ready the
  moment the agent polls `browser_queue_status`. The ticket — not the caller —
  owns the slot, so a client timeout cannot leak one.
- **A 20-second lease costs about $0.00033** at the documented $0.02/hour.
  Browser time is cheap; the real risks are a forgotten session and proxy
  bandwidth, which is why `direct` is the default egress.
- Account concurrency quota observed: 10. `CBMCP_MAX_CONCURRENCY` is clamped to
  it automatically.

---

## Tests

```bash
python -m pytest tests/ -q                  # offline, no cloud account needed
python scripts/usage_simulation.py          # realistic multi-window run, ~4 min, ~$0.02
python scripts/concurrency_test.py --agents 6 --max-concurrency 2
python scripts/verify_new_tools.py          # initiator, text breakpoint, emulate, bundle
python scripts/fingerprint_check.py         # how detectable the instrumentation is
```

### The usage simulation

`usage_simulation.py` is the closest thing to real use short of real agents.
Five Claude windows (five gateway processes) open at different times and send
out 22 subagents that work like LLM agents -- read `page_snapshot`, pick an
element by role and name, act on its ref, think between calls -- on real sites
(books.toscrape.com, Hacker News, MDN, GitHub, example.com,
the-internet.herokuapp.com). Personas reproduce real failure modes: forgetting
to release, pausing past the idle timeout, renewing before a pause, retrying
`browser_acquire`, parallel tool calls, long multi-site sessions. One window is
closed mid-task, one crashes, one is opened afterwards, and peak demand exceeds
the ceiling of 6. It runs on its own throwaway data dir.

Last run against Browser Use: 117/117 checks, peak 6 browsers for a ceiling of
6, the crashed window's browsers stopped 18s after the crash (as soon as the
next window opened), nothing left running.

### The acceptance test

`concurrency_test.py` reproduces the real topology rather than a convenient
one: **N agents sharing a single MCP connection**, because that is what Claude
Code subagents do — the server sees interleaved tool calls on one stdio
transport and has nothing but the lease token to tell them apart.

Last run, 6 agents against 2 slots:

```
6/6 agents completed in 51s
agents that queued:       2 (agent5, agent6)
agents that waited:       4 (longest 21s)
slots in use at the end:  0
cloud sessions still up:  0
```

Each agent wrote a value only it knew into `document.cookie` and
`localStorage`, then asserted it saw **exactly** its own and nothing else —
which is the isolation claim, checked rather than assumed. Each also confirmed
that a fabricated token is refused, that its own token stops working the moment
it is released, and that releasing twice is harmless.

What the test deliberately does *not* assert: that another agent's **valid**
token is refused. It is not — possession is the identity, by necessity, since
subagents share one transport. Unguessability is the guarantee; see
[Isolation, and its honest limit](#isolation-and-its-honest-limit).

The pool, budget, scope and recovery logic — the parts that cost money when
wrong — are tested offline against a fake provider and a stub browser session:
the concurrency ceiling under ten simultaneous acquires, FIFO queue fairness,
idempotent acquire and release, slot accounting on failure, budget reservation
under concurrency, orphan classification, and crash recovery.

Three bugs in this list were found by running against a real site rather than by
unit tests, and are worth knowing about if you extend the hook runtime:

1. **Infinite recursion.** The recorder serialises arguments with
   `JSON.stringify` and flushes batches with it. Hooking `JSON.stringify` made
   that `stringify → wrapper → record → stringify → …` and froze the browser. The
   runtime now captures native references to everything it uses *before* wrapping
   anything, plus a reentrancy flag.
2. **Hot-function cost.** `encodeURIComponent` is called hundreds of times per
   load, and `new Error().stack` on each call made pages unusable. Records and
   stack captures are now capped per label, with the suppressed count reported so
   the numbers are never silently wrong.
3. **Teardown hanging.** `browser.close()` on a renderer paused at a breakpoint
   hangs, blocking the lease release and its slot. Teardown now resumes the
   debugger first and bounds every step with a timeout.

---

## Limitations

- **One machine.** Gateways on one machine share the ceiling through atomic
  slot claims in the SQLite database (measured on Browser Use: ten requests
  from three windows at the same instant against a ceiling of six -- six ran,
  four queued). Gateways on different machines, or with different data dirs,
  do not see each other's claims; horizontal scaling would need the counter in
  Redis.
- **Some sites refuse cloud browsers.** Measured: Wikipedia answers Browser Use
  browsers with HTTP 429, through the platform's residential proxy too.
  `page_navigate` flags such responses with `http_error` and a `warning`.
- **No browser reuse between leases.** Considered and rejected: handing a used
  browser to another agent would carry cookies, storage and login state across,
  which breaks the isolation guarantee for a few cents of saved cold start.
- **`customProxy` is host/port/user/pass**; whether a given SOCKS5 endpoint works
  is the platform's behaviour, not something this gateway can determine. Test it
  with `CBMCP_NETWORK_MODE=custom_proxy` and `cbmcp check` before relying on it.
- **Persistent profiles** are supported by passing `profile_id` through, but no
  profile locking is implemented — using one profile from two leases at once is
  not prevented. Throwaway sessions are the default and the tested path.
- **Sensitive data reaches the model.** `page_eval` results, network bodies,
  hook records and screenshots all enter the agent's context, and therefore the
  model provider's logs. Traffic also transits Browser Use's infrastructure.
  Confirm your authorisation covers both before pointing this at a target.
  `js_export_bundle` writes the same material to disk and says so in its output.
- **Colour-scheme emulation does not take** on Browser Use browsers — neither
  Playwright's `emulate_media` nor the raw CDP `Emulation.setEmulatedMedia`
  changes `prefers-color-scheme`. `page_emulate` verifies the result and reports
  it as failed rather than claiming success. Everything else it offers (network
  and CPU throttling, geolocation, timezone, locale, user agent, viewport) was
  confirmed working against a real cloud browser.
- **Request initiators are joined by arrival order**, not by timestamp. CDP's
  `wallTime` comes from the browser host's clock, which was observed 18 seconds
  off this machine; matching on time silently produced zero joins. The Nth
  request for a given method+URL is matched to the Nth initiator instead, which
  needs no clock. If the initiator ring buffer wraps, the result says the match
  is approximate.
