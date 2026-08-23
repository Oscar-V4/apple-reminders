# 0008. Mobile-First Image Attachments

## Status

Accepted.

## Decision

Use the ReminderKit background backend as the default path for image attachments.

Treat SQLite-created image attachments as local-only unless read-back evidence shows a CloudKit server record and `ZINCLOUD=1`.

## Rationale

Testing showed that DB-only image attachment rows can render correctly in macOS Reminders while not appearing in iOS Reminders. The missing signal was CloudKit attachment state: local-only rows lacked server record data and synced cloud-state evidence.

The private ReminderKit path creates native image attachments that Reminders syncs through CloudKit. It works without foreground UI gestures and satisfies the actual user-facing requirement: images must be visible on iPhone.

## Consequences

- `attach_image` defaults to `--backend reminderkit`.
- A helper success is accepted only when it resolves to one newly created attachment id and read-back proves mobile-visible CloudKit evidence.
- `attach_image --backend db` remains available only as a diagnostic fallback.
- `audit_attachments` reports image rows that are likely Mac-local only.
- `repair_attachments` can back up the Reminders container, reattach local-only images through ReminderKit, and soft-delete the old local-only attachment objects.
- Replacement and repair report partial failure and attempt compensating soft-delete of the newly created attachment if removal of the old attachment fails.
- Plugin guidance must define success as mobile-visible attachment evidence, not Mac thumbnail rendering.
