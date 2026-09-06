---
name: apple-reminders-attachment-maintenance
description: Inspect, attach, copy, replace, or delete Apple Reminders image and URL attachments safely. Use for screenshots, images, files, URLs, iPhone visibility evidence, cross-reminder consolidation, exact attachment replacement, or removal. Bulk repair apply is not public.
---

# Apple Reminders Attachments

Native attachments use the `experimental_internals` support tier and are
available for discovery by default. Diagnose the requested capability; a
`--core-only` session or an unadmitted build may block it. Explain the precise
availability limit and offer an agreed note-link or manual alternative when
needed. Ordinary attachment work uses the verified bundled helper and does not
require enabling a mode or installing developer tools.

## Workflow

1. Preserve explicit image or URL-card intent. For a general link request,
   resolve whether the user means a URL field, note link, or native card. Use
   a text alternative only when it satisfies the request or the user agrees.
2. For an explicitly requested native attachment, run
   `diagnose_reminders {scope:"attachments"}` first. Continue only when the
   exact action's Experimental capability is `available=true` and build/schema
   admission passed. `runtime_state=runtime_unverified` together with
   `reason_code=runtime_verification_required` is an admitted metadata preflight:
   continue the requested operation, whose backend checks runtime prerequisites
   and verifies the result after the write. A static private-framework path
   warning alone is inconclusive when the exact capability is available.
   Stop on `available=false` or a blocking `reason_code`, including
   `runtime_unverified`, `unsupported_build`, `native_helper_unavailable`,
   `compiler_required`, or schema mismatch. A failed runtime operation is still
   a failure; follow its Receipt rather than treating preflight admission as
   success. Then call `read_reminder` for the exact destination and fresh
   opaque reference and inspect the exact native state.
3. Resolve exactly one local source image, URL, existing destination `attachment_id`, or exact active source Reminder plus image attachment ID. For cross-reminder copy, call `read_reminder` and native attachment inspection for the source immediately before the write. Do not guess what “this screenshot” means when no unique conversation attachment or local file is available.
4. Call `change_reminder_attachment` with the fresh reference and exactly one action.
5. Treat only `verified` or `unchanged` as completed after exact destination read-back. On pending or partial status, follow the indicated read-only recovery; do not repeat the original mutation. Once fresh Core and native reads confirm the requested attachment and preserved fields, continue remaining authorized images with the newly returned reference. Unresolved or ambiguous state stops the write chain.

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
  does not claim a native URL card. Only the legacy `--experimental` URL opt-in
  retains the hybrid metadata-plus-attachment behavior for string URLs. Do not
  add a verified hybrid URL again.
- In legacy hybrid URL mode, if a later same-URL Core patch finds the matching URL plus another URL attachment, it intentionally performs no write and returns an ambiguity. Call `read_reminder`, inspect the exact native attachment IDs, and delete only a user-intended stale object; never infer that every non-matching link is the old URL.
- Use `attach_url` here for an explicitly requested URL card or additional URL attachment or explicit recovery after resolving a partial Core write.
- Clearing Core `patch.url` does not delete attachment objects. Use an exact attachment ID for deletion.

## Evidence and withheld repair

- `mobile_visible_likely` means CloudKit/mobile-sync evidence, not direct iPhone-screen confirmation.
- Local Mac rendering alone is not mobile evidence.
- Bulk attachment audit/repair apply, raw attachment export, and backup/Snapshot apply are withheld. A request to repair many local-only attachments may receive bounded inspection, diagnosis, and a proposal, but not a private maintenance write.
- The verified bundled helper does not override an unallowlisted build or
  missing runtime evidence. Report availability without requesting a compiler.
- Image removal follows the adapter's recoverable object lifecycle; do not hard-delete copied files.

## Output

Include Reminder title/ID, exact attachment ID and type, file name or URL, Receipt status, and verification evidence. Avoid full local paths unless needed for disambiguation.
