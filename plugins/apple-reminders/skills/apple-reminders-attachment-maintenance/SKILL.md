---
name: apple-reminders-attachment-maintenance
description: Inspect, attach, replace, or delete Apple Reminders image and URL attachments safely. Use for screenshots, images, files, URLs, iPhone visibility evidence, exact attachment replacement, or removal. Bulk repair apply is not public in 0.3.
---

# Apple Reminders Attachments

Resolve one exact Reminder and attachment target, then use the guarded Native Extension. Never fall back to direct SQLite or an unexposed adapter write.

## Workflow

1. Resolve a bounded candidate set, then call `read_reminder` for the exact target and fresh opaque reference. Stop on duplicate titles.
2. Call `inspect_reminder_native` with `kind=reminder`, that reference, and `include=["attachments","sync"]`. Filter by attachment type when useful.
3. Resolve exactly one source image, URL, or existing `attachment_id`. Do not guess what “this screenshot” means when no unique conversation attachment or local file is available.
4. Call `change_reminder_attachment` with the fresh reference and exactly one action.
5. Treat only `verified` as completed after final read-back. On pending or partial status, surface recovery guidance and perform no automatic second write.

Actions:

```json
{"kind":"attach_image","image_path":"/absolute/input.png","idempotency_key":"UNIQUE-KEY"}
{"kind":"attach_url","url":"https://example.com"}
{"kind":"replace_image","attachment_id":"EXACT-ID","image_path":"/absolute/input.jpg","idempotency_key":"UNIQUE-KEY"}
{"kind":"replace_url","attachment_id":"EXACT-ID","url":"https://example.com","idempotency_key":"UNIQUE-KEY"}
{"kind":"delete","attachment_id":"EXACT-ID"}
```

## Image boundary

The source must be an absolute regular non-symlink PNG or JPEG, at most 25 MiB, no dimension above 16,384 pixels, and at most 40,000,000 total pixels. Validation occurs before mutation; a changed file is rejected.

## URL behavior

- A URL supplied to Core `create_reminder` or `change_reminder` is already a combined EventKit + visible-attachment operation. Do not add it again after `verified`.
- Use `attach_url` here for an additional URL attachment or explicit recovery after resolving a partial Core write.
- Clearing Core `patch.url` does not delete attachment objects. Use an exact attachment ID for deletion.

## Evidence and withheld repair

- `mobile_visible_likely` means CloudKit/mobile-sync evidence, not direct iPhone-screen confirmation.
- Local Mac rendering alone is not mobile evidence.
- Bulk attachment audit/repair apply, backup/Snapshot apply, and restore are withheld from public 0.3. A request to repair many local-only attachments may receive bounded inspection, diagnosis, and a proposal, but not a private maintenance write.
- Image removal follows the adapter's recoverable object lifecycle; do not hard-delete copied files.

## Output

Include Reminder title/ID, exact attachment ID and type, file name or URL, Receipt status, and verification evidence. Avoid full local paths unless needed for disambiguation.
