# Apple Reminders public interface

Use these MCP tools directly. The adapter, EventKit bridge, ReminderKit helpers, and private store are implementation details; do not invoke their writes as fallbacks.

Core is the Stable, documented EventKit tier. Sections, tag assignments, native
attachments, and Recently Deleted are Experimental Internals. Before an
Experimental mutation, targeted diagnosis must report the exact capability as
available; blocked build/schema/compiler results are terminal no-write outcomes.

Default discovery contains eight Core tools plus `diagnose_reminders`. Native
Extension and Recovery tools below require an explicitly configured
`--experimental` launch; direct calls remain blocked when hidden. This opt-in
never bypasses runtime build, schema, compiler, or permission checks.

Default Core `url` writes only EventKit metadata. Use notes for a visible text
link while preserving existing text. Experimental mode additionally composes a
native URL card on string URL writes. In both modes, `url:null` clears metadata
and preserves existing attachment objects. A verified Core metadata write does
not verify any existing card, iCloud convergence, or iPhone visibility.

URL-create idempotency keys bind the mode. A key from another mode or an older
hybrid receipt is rejected before dispatch. Read the original item to resolve
its state; a conflict is not permission to create it again with a fresh key.

## Core

- `request_reminders_access {}`: explicitly request macOS Reminders access once.
- `list_reminder_lists {source_id?, writable_only?, limit?}`: return exact list and account identities.
- `fetch_reminders {list_ids?, status?, due_start?, due_end?, completion_start?, completion_end?, query?, modified_after?, sort?, limit?, cursor?}`: return a bounded summary page. It does not issue writable references.
- `read_reminder {reminder_id}`: return one exact Core projection and opaque `rev1` reference.
- `create_reminder {list_id, title, idempotency_key, ...fields}`: create once and return final exact state plus a fresh reference when verified.
- `change_reminder {reference, action}`: patch fields, set completion, or move to another list.
- `delete_reminder {reference}`: delete through EventKit and require exact local-absence evidence. That receipt does not itself prove UI state or future recoverability.
- `ensure_reminder_list {source_id, name, idempotency_key}`: return an exact existing list or create it in that account. Color and emblem are not public fields.

`change_reminder.action` is exactly one of:

```json
{"kind":"patch","patch":{"title":"…","notes":"…","url":"…","priority":5,"due":null,"alarms":[],"recurrence_rules":[]}}
{"kind":"set_completion","completed":true}
{"kind":"move_to_list","list_id":"EXACT-LIST-ID"}
```

Alarm-only relative patch sequence:

1. Call `read_reminder {"reminder_id":"EXACT-REMINDER-ID"}` immediately before the write and confirm that its exact result has a non-null `due`.
2. Call `change_reminder` with the returned fresh reference and the complete intended alarm array:

   ```json
   {"reference":"rev1.…","action":{"kind":"patch","patch":{"alarms":[{"kind":"relative","offset_seconds":-1209600}]}}}
   ```

`alarms` is a complete-array replacement. Omitting `alarms` preserves every current alarm whenever alarm-array intent is unchanged and the resulting due remains non-null; `null` or `[]` explicitly clears all alarms. Setting `due:null` while retaining a relative alarm is rejected, so pair it with `alarms:null`, `alarms:[]`, or a complete non-relative replacement. Other omitted patch fields remain unchanged; `null` clears only fields whose input schema permits it.

A returned relative alarm without `read_only:true` is in the faithful writable subset. `read_only:true` exposes an existing unsupported trigger, offset, or action and its bounded action metadata for inspection; that alarm cannot be resubmitted as part of a non-empty replacement. For an unrelated patch, preserve the complete alarm array by omitting `alarms`. If EventKit contains trigger state that cannot be represented losslessly, the write may commit but Core refuses to claim exact preservation and returns a pending verification result. If the user asks to change alarms while a read-only alarm is present, stop and report that the replacement is unsafe; clear with `null` or `[]` only when the user explicitly requests removing every alarm. Writable relative offsets are whole seconds from `-31536000` through `0`: exactly 31,536,000 seconds (365 elapsed days) before the due value through the due instant.

## Selection and destructive composition

- “Top,” “first,” and “visible” are evidence-relative. A literal UI-relative request requires direct observation of the current Reminders UI and exact row-to-ID resolution. If that is unavailable, stop and show the proposed `fetch_reminders` scope/sort; capture that bounded API snapshot only after the user explicitly accepts API order as a reinterpretation. Stop on duplicate UI-to-ID mapping. State which basis was used; API order is not current UI order.
- A v3 cursor is reusable only with identical filters, sort, and limit. It privately binds the ordered Reminder IDs and revisions; a later membership/revision change fails the page read as `concurrent_modification` / `pagination_snapshot_stale`. Discard the partial paged set and restart without a cursor. Re-read every exact item immediately before mutation because the pagination fingerprint is not a write precondition.
- Finish and verify every non-destructive dependency before source deletion. For attachment consolidation, copy each exact image to the destination and verify it there before obtaining fresh source references and deleting one source at a time.

## Recently Deleted recovery

- `inspect_recently_deleted {kind:"list", account_id?, limit?, cursor?}`: return a bounded local inventory. Its opaque Recently Deleted cursor binds the identical account and limit plus the ordered deleted-item identity/revision snapshot. On `pagination_snapshot_stale`, discard the partial inventory and restart without a cursor. List mode issues no recovery reference and no full attachment list.
- `inspect_recently_deleted {kind:"item", reminder_id, attachment_limit?}`: re-read one exact deleted Reminder, verify available image backing bytes, capture private-store and native ReminderKit snapshot guards, return bounded attachment metadata, and issue one opaque `del1` recovery reference. Missing integrity evidence fails closed and issues no Reference.
- `recover_deleted_reminder {reference, list_id, idempotency_key}`: consume a fresh `del1` and recover that exact Reminder into an exact compatible same-account list. `verified` requires a matched native pre-save guard, equal pre/native/post attachment counts, actual image-byte SHA-512 preservation, and an exact active EventKit read-back.

`del1` is distinct from `rev1`: it is short-lived, one-use, opaque, and valid only for the deleted snapshot that issued it. Inspect the exact item again after an expired, consumed, or stale-token result. Do not automatically retry an unknown recovery outcome.

The deleted item's `account_id` and a destination Reminder List's `source.id` identify the same account boundary used by recovery. Compare those exact values before choosing among duplicate list titles.

This recovery surface is bounded by the local 30-day Recently Deleted retention
window and depends on an exact allowlisted macOS/Reminders build, recovery schema
fingerprint, Command Line Tools for exact inspection/recovery, and compatible
private frameworks. A successful run is evidence for that exact environment and
moment, not a generic platform, account, iCloud, or iPhone guarantee.

## Native Extension

These tools are Experimental. Prefer separate Reminder Lists or textual
grouping for sections/tags, note links for URLs, and note descriptions/links for
images. Use the native actions only for explicit intent after targeted diagnosis.

- `inspect_reminder_native`: use `kind=reminder` with a reference and requested `include` values; `kind=sections` with exact `list_id`; or `kind=tags` with an optional account/query bound.
- `create_reminder_section {list_id, name}`: create or repair one exact-list section with native read-back evidence.
- `organize_reminder {reference, action}`: `move_to_section`, `add_tag`, or `remove_tag`.
- `change_reminder_attachment {reference, action}`: attach/copy/replace an image or URL, or delete an exact attachment ID.

Attachment actions are:

```json
{"kind":"attach_image","image_path":"/absolute/input.png","idempotency_key":"…"}
{"kind":"attach_url","url":"https://example.com"}
{"kind":"copy_image","source_reference":"rev1.…","attachment_id":"EXACT-SOURCE-IMAGE-ID","idempotency_key":"…"}
{"kind":"replace_image","attachment_id":"…","image_path":"/absolute/input.jpg","idempotency_key":"…"}
{"kind":"replace_url","attachment_id":"…","url":"https://example.com","idempotency_key":"…"}
{"kind":"delete","attachment_id":"…"}
```

Native mutation starts by revalidating the opaque Core reference, then captures its private concurrency value internally. `copy_image` revalidates both active source and destination references, leaves the source unchanged, and verifies the exact destination after copying a private byte snapshot. Source bytes must decode as bounded PNG or JPEG; no HEIC or other-format conversion is implied. Both input references are consumed as one-use preconditions after dispatch; re-read the source before another copy and use the returned fresh destination reference. Callers never choose between EventKit and private revisions or receive a private attachment path.

## Intended CRUD boundaries

| Resource | Public operations | Intentional boundary |
| --- | --- | --- |
| Reminder | Stable list/read/create/change/complete/move/delete; Experimental exact Recently Deleted recovery | no title-only mutation or bulk write without bounded review |
| Reminder List | list and exact-account create-or-return | no rename, delete, color, or emblem write |
| Section | Experimental exact-list inspect/create and Reminder move | prefer list/text grouping; no rename or delete |
| Tag | Experimental bounded inspect and exact assignment add/remove | prefer text labels; no global unused-label row deletion |
| Attachment | Experimental inspect/attach/copy/replace/delete | prefer notes; no raw export/download, private path disclosure, or bulk repair apply |
| Due/alarm/recurrence | typed due; absolute, due-anchored relative, or coordinate-backed location alarms; one validated recurrence rule | relative alarms require a due value; no invented or messaging alarm |

## Diagnostics

`diagnose_reminders {scope?, detail_level?, execution_mode?}` runs one content-free diagnosis and reports the requested area, support tier, compiler requirement, build/schema compatibility, runtime state, and precise block reason. Use it before an explicitly requested Experimental mutation or after a relevant failure. Public scopes are `core`, `access`, `native_extension`, `sections`, `tags`, `attachments`, `recovery`, and `packaging`. The default `metadata_only` mode runs no developer-tool process. Only explicit `experimental_toolchain` mode for a related Native Extension or Recovery scope may run the private-helper toolchain gate; it never runs `xcode-select --install`. Core and packaging diagnosis remain metadata-only.

## Receipt rules

Core revalidates the opaque Reference with a fresh exact read before action preflight, then verifies the final identifier-based read through one canonical semantic projection: the requested delta plus stable user-authored title, notes, URL, location, priority, completion state, due, start, complete alarms multiset, recurrence, and destination list. Alarm order alone is not semantic, but duplicate counts are; recurrence arrays retain canonical order. A lossy alarm projection on either side cannot verify. Provider-owned identity/display metadata and derived timestamps are excluded. Any preserved-field drift—including absolute, location, writable-relative, or read-only alarm loss or transformation—cannot produce `verified` or a fresh writable Reference.

- `unchanged`: no mutation was needed; a fresh reference may be returned.
- `verified`: the final exact read matched; a fresh reference may be returned for an existing/created reminder.
- `committed_verification_pending`: a write may have committed, but final truth is unavailable. Do not retry before a fresh read.
- `partial_success`: one part committed and another did not verify. Follow the returned recovery guidance; do not continue automatically.
- `failed_no_mutation`: the failure is known to precede mutation.
- `failed_manual_repair_required`: a mutation or unknown outcome needs explicit repair.

Only `unchanged` and `verified` may include a writable `rev1` reference. Never decode a reference or infer a Reminder ID from a rejected token.

For Experimental hybrid URL A-to-B workflows, a later same-B retry that sees native B plus another URL returns `failed_no_mutation/ambiguous_visible_url_attachment` rather than hiding unresolved A+B state. It issues no fresh writable Reference: call `read_reminder`, inspect native attachments with the new Reference, and clean up only one exact user-intended attachment ID.

Broad cleanup uses 25–40 candidates per authorized chunk, with the final remainder allowed to be smaller. References are just-in-time: read one exact item, perform its approved action, verify it, discard its reference, and stop the whole run on the first uncertainty before moving to another item or chunk.

## Withheld

Native flag mutation, MCP-level UI selection/order, unused-label row deletion, attachment export/download, attachment repair apply, backup/Snapshot apply, list/section rename or deletion, and log purge are not public tools. A user request for one of these may receive a read-only diagnosis or proposal, but not an adapter/SQLite/AppleScript/UI-automation fallback write.
