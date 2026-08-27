# Apple Reminders public interface

Use these MCP tools directly. The adapter, EventKit bridge, ReminderKit helpers, and private store are implementation details; do not invoke their writes as fallbacks.

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

Omitted patch fields remain unchanged. `null` clears only fields whose input schema permits null; inspect the live tool schema instead of assuming clear behavior.

## Selection and destructive composition

- “Top,” “first,” and “visible” are evidence-relative. Directly observe the current Reminders UI and resolve every visible row to one exact ID, or capture one bounded `fetch_reminders` API snapshot with exact list/filter/status, one supported sort (`due`, `modified`, or `title`), limit, IDs, and truncation. Stop on duplicate UI-to-ID mapping. State which basis was used; API order is not current UI order.
- A v3 cursor is reusable only with identical filters, sort, and limit. It privately binds the ordered Reminder IDs and revisions; a later membership/revision change fails the page read as `concurrent_modification` / `pagination_snapshot_stale`. Discard the partial paged set and restart without a cursor. Re-read every exact item immediately before mutation because the pagination fingerprint is not a write precondition.
- Finish and verify every non-destructive dependency before source deletion. For attachment consolidation, copy each exact image to the destination and verify it there before obtaining fresh source references and deleting one source at a time.

## Recently Deleted recovery

- `inspect_recently_deleted {kind:"list", account_id?, limit?}`: return a bounded local inventory. List mode issues no recovery reference and no full attachment list.
- `inspect_recently_deleted {kind:"item", reminder_id, attachment_limit?}`: re-read one exact deleted Reminder, return bounded attachment metadata, and issue one opaque `del1` recovery reference.
- `recover_deleted_reminder {reference, list_id, idempotency_key}`: consume a fresh `del1` and recover that exact Reminder into an exact compatible same-account list. `verified` requires native attachment preservation evidence plus an exact active EventKit read-back.

`del1` is distinct from `rev1`: it is short-lived, one-use, opaque, and valid only for the deleted snapshot that issued it. Inspect the exact item again after an expired, consumed, or stale-token result. Do not automatically retry an unknown recovery outcome.

This recovery surface is bounded by the local 30-day Recently Deleted retention window and depends on compatible private ReminderKit frameworks and local Reminders store schema. It is macOS-only and version-sensitive. A successful local UI observation or recovery run is evidence for that Mac and moment, not a generic platform, account, iCloud, or iPhone guarantee.

## Native Extension

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
| Reminder | list/read/create/change/complete/move/delete; exact Recently Deleted recovery | no title-only mutation or bulk write without bounded review |
| Reminder List | list and exact-account create-or-return | no rename, delete, color, or emblem write |
| Section | exact-list inspect/create and Reminder move | no rename or delete; names are list-scoped |
| Tag | bounded inspect and exact Reminder assignment add/remove | no global unused-label row deletion |
| Attachment | inspect metadata; attach/copy/replace/delete closed actions | no raw export/download, private path disclosure, or bulk repair apply |
| Due/alarm/recurrence | typed due, explicit supported alarms, one validated recurrence rule | no invented alarm, relative alarm, or messaging alarm |

## Diagnostics

`diagnose_reminders {scope?, detail_level?}` runs one content-free diagnosis and reports the requested area. Use it only after a relevant failure. Public scopes are `core`, `access`, `native_extension`, `sections`, `tags`, `attachments`, and `packaging`.

## Receipt rules

- `unchanged`: no mutation was needed; a fresh reference may be returned.
- `verified`: the final exact read matched; a fresh reference may be returned for an existing/created reminder.
- `committed_verification_pending`: a write may have committed, but final truth is unavailable. Do not retry before a fresh read.
- `partial_success`: one part committed and another did not verify. Follow the returned recovery guidance; do not continue automatically.
- `failed_no_mutation`: the failure is known to precede mutation.
- `failed_manual_repair_required`: a mutation or unknown outcome needs explicit repair.

Only `unchanged` and `verified` may include a writable `rev1` reference. Never decode a reference or infer a Reminder ID from a rejected token.

Broad cleanup uses 25–40 candidates per authorized chunk, with the final remainder allowed to be smaller. References are just-in-time: read one exact item, perform its approved action, verify it, discard its reference, and stop the whole run on the first uncertainty before moving to another item or chunk.

## Withheld

Native flag mutation, MCP-level UI selection/order, unused-label row deletion, attachment export/download, attachment repair apply, backup/Snapshot apply, list/section rename or deletion, and log purge are not public tools. A user request for one of these may receive a read-only diagnosis or proposal, but not an adapter/SQLite/AppleScript/UI-automation fallback write.
