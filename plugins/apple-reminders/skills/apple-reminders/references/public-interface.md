# Apple Reminders 0.3 Public Interface

Use these MCP tools directly. The adapter, EventKit bridge, ReminderKit helpers, and private store are implementation details; do not invoke their writes as fallbacks.

## Core

- `request_reminders_access {}`: explicitly request macOS Reminders access once.
- `list_reminder_lists {source_id?, writable_only?, limit?}`: return exact list and account identities.
- `fetch_reminders {list_ids?, status?, due_start?, due_end?, completion_start?, completion_end?, query?, modified_after?, sort?, limit?, cursor?}`: return a bounded summary page. It does not issue writable references.
- `read_reminder {reminder_id}`: return one exact Core projection and opaque `rev1` reference.
- `create_reminder {list_id, title, idempotency_key, ...fields}`: create once and return final exact state plus a fresh reference when verified.
- `change_reminder {reference, action}`: patch fields, set completion, or move to another list.
- `delete_reminder {reference}`: delete through EventKit and require exact local-absence evidence. It is terminal on the public surface; no restore, undo, Recently Deleted inspection, or recovery token is exposed.
- `ensure_reminder_list {source_id, name, idempotency_key}`: return an exact existing list or create it in that account. Color and emblem are not public fields.

`change_reminder.action` is exactly one of:

```json
{"kind":"patch","patch":{"title":"…","notes":"…","url":"…","priority":5,"due":null,"alarms":[],"recurrence_rules":[]}}
{"kind":"set_completion","completed":true}
{"kind":"move_to_list","list_id":"EXACT-LIST-ID"}
```

Omitted patch fields remain unchanged. `null` clears only fields whose input schema permits null; inspect the live tool schema instead of assuming clear behavior.

## Selection and destructive composition

- API sort is limited to `due`, `modified`, or `title`; it does not prove current Reminders UI order. Resolve “top N” with exact scope, sort, limit, and returned IDs. Reuse cursors only with identical arguments.
- Complete and verify dependencies before deletion. Existing image attachments are metadata, not exportable bytes; consolidation requires exact local image paths and must finish before source deletion.

## Native Extension

- `inspect_reminder_native`: use `kind=reminder` with a reference and requested `include` values; `kind=sections` with exact `list_id`; or `kind=tags` with an optional account/query bound.
- `create_reminder_section {list_id, name}`: create or repair one exact-list section with native read-back evidence.
- `organize_reminder {reference, action}`: `move_to_section`, `add_tag`, or `remove_tag`.
- `change_reminder_attachment {reference, action}`: attach/replace an image or URL, or delete an exact attachment ID.

Attachment actions are:

```json
{"kind":"attach_image","image_path":"/absolute/input.png","idempotency_key":"…"}
{"kind":"attach_url","url":"https://example.com"}
{"kind":"replace_image","attachment_id":"…","image_path":"/absolute/input.jpg","idempotency_key":"…"}
{"kind":"replace_url","attachment_id":"…","url":"https://example.com","idempotency_key":"…"}
{"kind":"delete","attachment_id":"…"}
```

Native mutation starts by revalidating the opaque Core reference, then captures its private concurrency value internally. Callers never choose between EventKit and private revisions.

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

## Withheld from 0.3

Native flag mutation, exact UI selection/order, list/section rename or deletion, unused-label row deletion, attachment export/copy, attachment repair apply, backup/Snapshot apply, reminder restore, Recently Deleted inspection/recovery, and log purge are not public tools. A user request for one of these may receive a read-only diagnosis or proposal, but not an adapter/SQLite/AppleScript/UI-automation fallback write.
