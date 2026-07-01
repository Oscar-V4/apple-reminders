# Google Calendar Reference Pattern

The Google Calendar plugin is the model for this plugin.

## Pattern To Copy

- The primary skill is not a command manual. It is an operating contract.
- The skill requires state reads before reasoning.
- Reads must be bounded by concrete time windows or other scope.
- Ambiguity should be resolved from existing data before asking the user.
- Writes should preserve untouched fields.
- Bulk changes require an exact qualifying set before execution.
- Output should present decisions and diffs, not raw API payloads.

## Reminders Translation

Calendar concepts map to Reminders concepts like this:

- calendar -> account or list
- event -> reminder
- recurring series -> repeated or template-like reminder patterns
- meeting attendees/rooms -> list, section, tags, notes, and attachments context
- event reminders -> due date, alert date, priority, flag, and urgent state
- availability window -> task horizon or review window
- temporary hold -> provisional reminder or staging section

## Required Tool Backing

The Google Calendar skill depends on a connector. Apple Reminders needs the same split:

- public adapter: AppleScript/EventKit for lists, reminders, dates, notes, completion, priority, and flags
- private adapter: SQLite-backed operations for image attachments, sections, and membership ordering
- verification adapter: post-write read-back, store backup, and schema doctor
