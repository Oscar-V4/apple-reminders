---
name: apple-reminders-attachment-maintenance
description: Inspect, attach, copy, replace, or delete Apple Reminders image and URL attachments safely. Use for screenshots, images, files, URLs, iPhone visibility evidence, cross-reminder consolidation, exact attachment replacement, or removal. Bulk repair apply is not public.
---

# Apple Reminders Attachments

Native attachments are Experimental Internals and require an already enabled
`--experimental` session. If the tools are absent or `experimental_disabled` is
returned, explain the limit before a write. Do not change configuration or
install developer tools to satisfy an attachment request. Prefer a Core-safe note link or
plain-text image description unless the user explicitly requests a native
attachment. Never fall back to direct SQLite or an unexposed adapter write.

## Workflow

1. Resolve whether the goal can be met with a contextual URL in `notes` or a
   text description. Use that Stable Core substitute by default.
2. For an explicitly requested native attachment, run
   `diagnose_reminders {scope:"attachments"}` first. Continue only when the
   exact action's Experimental capability is `available=true`; stop on
   `runtime_unverified`, `unsupported_build`, `compiler_required`, or schema
   mismatch. Then call `read_reminder` for the exact destination and fresh
   opaque reference and inspect the exact native state.
3. Resolve exactly one local source image, URL, existing destination `attachment_id`, or exact active source Reminder plus image attachment ID. For cross-reminder copy, call `read_reminder` and native attachment inspection for the source immediately before the write. Do not guess what “this screenshot” means when no unique conversation attachment or local file is available.
4. Call `change_reminder_attachment` with the fresh reference and exactly one action.
5. Treat only `verified` or `unchanged` as completed after exact destination read-back. On pending or partial status, surface recovery guidance and perform no automatic second write.

Actions:

```json
{"kind":"attach_image","image_path":"/absolute/input.png","idempotency_key":"UNIQUE-KEY"}
{"kind":"attach_url","url":"https://example.com"}
{"kind":"copy_image","source_reference":"rev1.FRESH-SOURCE","attachment_id":"EXACT-SOURCE-IMAGE-ID","idempotency_key":"UNIQUE-KEY"}
{"kind":"replace_image","attachment_id":"EXACT-ID","image_path":"/absolute/input.jpg","idempotency_key":"UNIQUE-KEY"}
{"kind":"replace_url","attachment_id":"EXACT-ID","url":"https://example.com","idempotency_key":"UNIQUE-KEY"}
{"kind":"delete","attachment_id":"EXACT-ID"}
```

## Image boundary

For `attach_image` and `replace_image`, the source path must be an absolute regular non-symlink PNG or JPEG, at most 25 MiB, no dimension above 16,384 pixels, and at most 40,000,000 total pixels. Validation occurs before mutation; a changed file is rejected.

`copy_image` is the path for an image that already belongs to another active Reminder. It requires fresh, distinct source and destination `rev1` references and one exact active source image attachment ID. The backing bytes must decode as PNG or JPEG under the same byte/dimension/pixel bounds; HEIC and other formats are not converted. Both references are consumed as one-use preconditions after dispatch even though the source stays unchanged, so re-read the source before another copy. The backend snapshots the source bytes privately, never exposes the storage path, and reports `verified` or `unchanged` only after exact destination read-back. Recover a deleted source through exact `del1` recovery before copying; deleted attachment metadata is not a copy authority.

For consolidation, inspect all dependencies, copy and verify every destination image first, then hand any authorized source deletion to `$apple-reminders-organize-cleanup`. After each copy, use the returned fresh destination reference for the next mutation. Stop the chain on a stale, missing, ambiguous, pending, partial, or manual-repair result.

Cross-Reminder copy can place several source images as separate attachments on one destination Reminder. It cannot generate one composited bitmap. Clarify that distinction before acting on “make these one image,” and never interpret “organize” alone as permission to delete the source Reminders.

## URL behavior

- In the default session, Core `url` saves EventKit metadata only. Use Core
  for a URL field or preserve a link in `notes` for ordinary visible text; this
  does not claim a native URL card. An explicitly enabled Experimental session
  retains the hybrid metadata-plus-attachment behavior for string URLs. Do not
  add a verified hybrid URL again.
- In Experimental mode, if a later same-URL Core patch finds the matching URL plus another URL attachment, it intentionally performs no write and returns an ambiguity. Call `read_reminder`, inspect the exact native attachment IDs, and delete only a user-intended stale object; never infer that every non-matching link is the old URL.
- Use `attach_url` here for an additional URL attachment or explicit recovery after resolving a partial Core write.
- Clearing Core `patch.url` does not delete attachment objects. Use an exact attachment ID for deletion.

## Evidence and withheld repair

- `mobile_visible_likely` means CloudKit/mobile-sync evidence, not direct iPhone-screen confirmation.
- Local Mac rendering alone is not mobile evidence.
- Bulk attachment audit/repair apply, raw attachment export, and backup/Snapshot apply are withheld. A request to repair many local-only attachments may receive bounded inspection, diagnosis, and a proposal, but not a private maintenance write.
- A compiler is only a dependency for image helper paths; it never overrides an
  unallowlisted build or missing runtime evidence.
- Image removal follows the adapter's recoverable object lifecycle; do not hard-delete copied files.

## Output

Include Reminder title/ID, exact attachment ID and type, file name or URL, Receipt status, and verification evidence. Avoid full local paths unless needed for disambiguation.
