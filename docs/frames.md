# Frame snapshots and element references

Use the existing `page_snapshot` tool; no extra tool or local browser is needed.
`page` is the browser tab id. `frame_ref` is the opaque ref of an iframe/frame
element inside that tab. `ref` is a control ref for `page_click`, `page_type`,
`page_select`, `page_upload`, hover/drag or single-element detail.

## Discover, select, act

```text
page_snapshot(lease=actual_lease, frames_only=true)
# Follow next_frame_offset with frame_offset to inspect more directory entries.
page_snapshot(lease=actual_lease, frame_ref=actual_directory_entry.frame_ref)
page_type(lease=actual_lease, ref=actual_input_ref, text=actual_test_input)
page_click(lease=actual_lease, ref=actual_button_ref)
page_snapshot(lease=actual_lease, frame_ref=the_same_iframe_element_ref)
```

These are argument shapes, not copyable target refs. A normal snapshot also
includes child snapshots, with the same directory metadata. Default
`frame_limit=8`; set1..50 and follow `next_frame_offset`. A selected frame is the
new root: offsets/counts then describe its descendants. The root itself is not
included in `child_frame_total`; nested and blank frames are included.

`frames_only=true` reads directory metadata without DOM bodies. Every chosen
entry is returned, including empty/error cases. `frames_not_in_response` counts
all descendants outside this response, including earlier pages;
`has_more_frames`/`next_frame_offset` describe later pages. The directory is live,
not a frozen pagination snapshot. `frame_directory_changed=true` means it
changed during collection: refresh it before relying on completeness.

## Identity, stale refs and ambiguity

Refs such as `e3_7` are opaque. A lease-wide counter assigns a distinct prefix
to each document, and a per-document WeakMap keeps each DOM node's identity.
Repeated snapshots preserve a live node's ref; copied attributes do not give
a clone the same identity. Navigation/document replacement creates fresh
control refs. The iframe element ref can remain valid while its child document
navigates, as long as the parent element/document survives.

Resolution checks the owning document, and duplicates produce `ref_not_found`
with an ambiguity message instead of selecting the first match. Re-snapshot
after DOM/document changes, a new lease, or a stale/ambiguous error. Live refs
can be re-associated after a same-browser CDP reconnect; still verify the
current page/frame state rather than treating a saved ref as permanent.

## Completeness and context limits

Child rows have `status=ok/empty/error` (`directory` in directory-only mode).
`empty` means no lines were emitted by that snapshot, not that no UI/API exists.
Failed reads expose `error/error_type`; an unavailable iframe element exposes
`frame_ref_error`. Do not turn these into a target-negative conclusion.

Main text and the combined child text each respect the configured snapshot
character budget. Child previews are at most6000 characters each; clipping and
node limits are explicit. To inspect a clipped child, select its `frame_ref`
and obtain a focused snapshot, then use `ref` for a needed control. A text
`next_offset` is clipping metadata, not a `page_snapshot` text-page argument.
Frame pagination uses only `next_frame_offset`.

Single-element details include `frame_url` and `box_coordinate_space`. A child
element's DOM `box` is relative to that frame viewport; do not pass it as main
page coordinates to `page_mouse`. Prefer the ref-based action. Child-frame
event listeners are not queried using the main-document debugger; an explicit
note reports that boundary. Driver interaction capability does not imply full
cross-process network/debugger coverage: inspect the actual relevant tools.

## Verification

Offline tests: `python -m pytest tests/test_frame_refs.py -q`.
The JS fixture optionally uses Node without launching a browser; other tests
are Python-only. Explicit paid cloud test:

```sh
python scripts/frames_smoke.py --output /private/tmp/frames-result.json
```

It prompts for the key without echo and uses controlled pages fulfilled over
cloud CDP, including a different origin, nesting and more than8 frames. No
local Chromium or business target is used. Cleanup confirms the owned session
stopped; output omits keys and signed connection URLs.
