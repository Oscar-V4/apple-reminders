---
name: apple-reminders-attachment-maintenance
description: Audit, attach, replace, delete, or repair Apple Reminders image and URL attachments safely. Use when the user mentions screenshots, images, files, URLs, iPhone visibility, broken attachments, local-only attachments, attachment cleanup, or replacing/removing reminder attachments.
---

# Apple Reminders Attachment Maintenance

## Overview

Use this skill for attachment-specific work. The safety target is exact reminder resolution, explicit attachment IDs, and truthful receipt status, especially for image attachments where iPhone visibility depends on mobile-sync evidence.

## Workflow

1. Resolve the target reminder by exact ID or by a bounded search that proves there is one match. Stop and show candidates on ambiguity.
2. Resolve the exact source image path or URL before writing. Do not guess from "the screenshot" when multiple files or no attached image are available. Image input must be an absolute regular non-symlink PNG or JPEG within the tool's 25 MiB, 16,384-pixel-per-dimension, and 40,000,000-total-pixel limits.
3. Call `list_reminder_attachments` first and retain its `reminder_version`. For image attachment, use `attach_image_to_reminder` with the exact reminder ID, absolute source path, that required `if_version`, and a fresh idempotency key. Its backend is fixed to the normal ReminderKit path. Treat `mobile_visible_likely: true` as CloudKit/mobile-sync evidence, not direct iPhone-screen confirmation.
4. For an additional URL attachment or recovery from a partial combined URL write, call `attach_url_to_reminder` with the normalized URL and captured `reminder_version` as `if_version`, then read back `list_reminder_attachments`. A non-null `create_reminder.url` or `update_reminder.patch.url` already ensures one visible URL attachment and should not be followed by a redundant explicit attach after `verified`.
5. For replace/delete, list attachments first, pass that read's `reminder_version` as `if_version`, and select by `attachment_id`, `attachment_pk`, exact filename, or exact URL. Do not remove the only plausible match without showing candidates when selection is ambiguous.
6. For repair, run bounded `audit_reminder_attachments` with `problems_only=true`, then `preview_reminder_attachment_repairs`. Apply only by passing the unchanged `candidate_digest` to `apply_reminder_attachment_repairs`; keep the default backup unless the user explicitly accepts the recovery tradeoff.
7. Report the top-level receipt status exactly: `unchanged`, `verified`, `committed_verification_pending`, or `partial_success`. `persisted_sync_pending` may appear only as nested verification evidence, not as a receipt status. Surface stable failure codes separately.

## Command Guidance

Read `../apple-reminders/references/adapter-cli.md` before invoking commands.

Common MCP flow for a new image:

```text
fetch_reminders (bounded exact candidates)
→ list_reminder_attachments (exact ID, type=image, bounded limit; capture reminder_version)
→ attach_image_to_reminder (exact ID + source path + if_version + idempotency key)
→ list_reminder_attachments (read-back)
```

## Safety Rules

- Never claim "confirmed on iPhone" unless an actual device or UI read-back was performed.
- Say "mobile visibility evidence was found" for `mobile_visible_likely: true`.
- Treat absent or false mobile evidence as pending or local-only, even if the Mac UI can render the image.
- For `replace_reminder_attachment` and `apply_reminder_attachment_repairs`, surface any partial-success risk and recovery details because those flows cross ReminderKit and SQLite-backed cleanup.
- Do not hard-delete copied image files. Use the adapter only: image objects take its recoverable soft-delete path, while URL metadata rows use the native row-removal plus retained cloud-state tombstone behavior.

## Output Rules

- Include reminder title, reminder ID, attachment ID, attachment type, and verification evidence.
- Include file name or URL, but avoid exposing full local paths unless needed for disambiguation.
- Separate dry-run candidates from applied changes.
- Stop after ambiguity, sync uncertainty, schema warnings, or partial success unless the next safe action is read-only.
