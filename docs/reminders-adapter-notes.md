# Reminders Adapter Notes

These notes capture local findings from the initial macOS Reminders investigation.

## Public Surfaces

AppleScript can create and inspect lists and reminders. It exposes common fields such as title, body, due date, reminder date, completion, priority, and flagged state.

EventKit exposes reminders and calendars but does not expose native image attachments or list sections.

## Private Store Surfaces

The Reminders store lives under:

`~/Library/Group Containers/group.com.apple.reminders/Container_v1/Stores/`

Attachment files live under:

`~/Library/Group Containers/group.com.apple.reminders/Container_v1/Files/<Account>/Attachments/`

Observed Core Data entities:

- `REMCDReminder`
- `REMCDBaseList`
- `REMCDBaseSection`
- `REMCDImageAttachment`
- `REMCKCloudState`

Image attachment proof:

- Copy image file into the account attachment folder using its SHA512 digest as filename.
- Insert a `REMCDImageAttachment` row linked to the reminder.
- Insert/update related `REMCKCloudState`.
- Restart/read Reminders and verify native thumbnail display.

Section proof:

- Insert a `REMCDListSection` row.
- Link it to the list.
- Update list section membership JSON so reminder UUIDs map to section UUIDs.
- Restart/read Reminders and verify native section rendering.

## Adapter Rules

- Always run a schema doctor before private writes.
- Always back up the Reminders container before experiments or broad changes.
- Keep transactions narrow.
- Verify every write by reading back through the app state or database.
- Treat private-store writes as local-first until iCloud behavior is tested more deeply.
