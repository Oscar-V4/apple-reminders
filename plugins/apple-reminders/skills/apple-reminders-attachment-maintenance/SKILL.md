---
name: apple-reminders-attachment-maintenance
description: Inspect, attach, replace, or delete Apple Reminders image and URL attachments safely. Use for screenshots, images, files, URLs, iPhone visibility evidence, exact attachment replacement or removal, and dependency-first cross-reminder consolidation. Bulk repair apply is not public in 0.3.
---

# Apple Reminders Attachments

Resolve one exact Reminder and attachment target, then use the guarded Native Extension. Never fall back to direct SQLite or an unexposed adapter write.

## Workflow

1. Resolve a bounded candidate set, then call `read_reminder` for the exact target and fresh opaque reference. Stop on duplicate titles.
2. Call `inspect_reminder_native` with `kind=reminder`, that reference, and `include=["attachments","sync"]`. Filter by attachment type when useful.
3. Resolve exactly one source image, URL, or existing `attachment_id`. Do not guess what “this screenshot” means when no unique conversation attachment or local file is available.
4. Call `change_reminder_attachment` with the fresh reference and exactly one action.
5. Treat only `verified` as completed after final read-back. On pending or partial status, surface the receipt's read-only recovery guidance and perform no automatic second write.

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

Existing attachment inspection returns metadata and evidence, not an exportable image file. The public tools do not download, export, or copy image bytes from one reminder to another. Never invent a local path from an attachment ID or private storage metadata.

## Cross-reminder consolidation

1. Resolve every source reminder and the exact destination before mutation. Inspect all source attachments and record exact attachment IDs for reporting.
2. Resolve each source image to an exact available local PNG/JPEG file. If even one required source exists only as an attachment object, stop before deleting or changing the source reminders.
3. Attach each local source image to the destination with a distinct idempotency key. Obtain a fresh reference as required and verify the destination after every write or after a receipt-directed fresh read.
4. Only after the destination contains every intended image with `verified` evidence may source-reminder deletion be handed back to `$apple-reminders-organize-cleanup`.
5. If the source reminders were already deleted, public attachment tools cannot recover their objects or files. Leave Recently Deleted or local UI recovery to a separately validated macOS workflow.

## URL behavior

- A URL supplied to Core `create_reminder` or `change_reminder` is already a combined EventKit + visible-attachment operation. Do not add it again after `verified`.
- Use `attach_url` here for an additional URL attachment or explicit recovery after resolving a partial Core write.
- Clearing Core `patch.url` does not delete attachment objects. Use an exact attachment ID for deletion.

## Evidence and withheld repair

- `mobile_visible_likely` means CloudKit/mobile-sync evidence, not direct iPhone-screen confirmation.
- Local Mac rendering alone is not mobile evidence.
- Bulk attachment audit/repair apply, attachment export/copy, backup/Snapshot apply, and restore are withheld from public 0.3. A request to repair many local-only attachments may receive bounded inspection, diagnosis, and a proposal, but not a private maintenance write.
- Report only Receipt and final read-back evidence for image removal. Do not infer file recovery, cloud convergence, device visibility, or local UI state.

## Output

Include Reminder title/ID, exact attachment ID and type, file name or URL, Receipt status, and verification evidence. For consolidation, list which local source files were available and which dependency blocked deletion. Avoid full local paths unless needed for disambiguation.
