# 0008. Mobile-First Image Attachments

## Status

Accepted for the native image path. The audit/repair CLI consequences were
superseded by ADR 0014 and physically removed before 0.4.

## Decision

Use the ReminderKit background backend as the default path for image attachments.

Pass decoded image data with a content-derived PNG/JPEG UTI. Do not infer the
attachment UTI from the source filename extension.

Treat SQLite-created image attachments as local-only unless read-back evidence shows a CloudKit server record and `ZINCLOUD=1`.

Use ReminderKit `removeAttachment:` saves for image replacement, deletion, and
compensation. A raw `ZMARKEDFORDELETION` update is not native-removal evidence.

## Rationale

Testing showed that DB-only image attachment rows can render correctly in macOS Reminders while not appearing in iOS Reminders. The missing signal was CloudKit attachment state: local-only rows lacked server record data and synced cloud-state evidence.

The private ReminderKit path creates native image attachments that Reminders
syncs through CloudKit. It works without foreground UI gestures and targets the
actual user-facing requirement of cross-device visibility, while CloudKit
evidence alone does not claim direct iPhone observation.

## Historical consequences and retained constraints

- `attach_image` defaults to `--backend reminderkit`.
- A helper success is accepted only when it uses the image-data transport,
  resolves to one newly created attachment id, preserves the content-derived
  UTI on read-back, and proves mobile-visible CloudKit evidence.
- `attach_image --backend db` remains available only as a diagnostic fallback.
- The former `audit_attachments` and `repair_attachments` commands reported and
  repaired likely Mac-local rows; ADR 0014 removed both commands and their
  backup machinery from the shipped adapter.
- Replacement, deletion, and compensation close direct SQLite
  connections before the ReminderKit save, then verify that the exact old
  attachment is detached from the exact Reminder. The native save may retain
  the old object and CloudKit state as a detached sync tombstone.
- Native image attachment may use a SQLite connection for bounded pre/post
  reads, but it must not hold a write transaction while ReminderKit saves. A
  live reproduction showed that `BEGIN IMMEDIATE` around the helper can stall
  the public operation until its 30-second subprocess timeout.
- An unknown native removal outcome stops the workflow without compensation.
  Deleting the replacement at that point could leave zero images if removal of
  the old attachment actually committed before the result was lost.
- Retained replacement reports partial failure and attempts compensating native
  removal of the newly created attachment if removal of the old attachment fails.
- Plugin guidance must define success as mobile-visible attachment evidence, not Mac thumbnail rendering.
