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

The Google Calendar skill depends on a connector declared in `.app.json`. Apple Reminders needs the same split between "agent behavior" and "actual operations", but not necessarily the same connector mechanism:

- public bridge: EventKit for accounts, lists, reminders, due dates, alarms, recurrence, notes, completion, priority, URLs, locations, and list-to-list moves
- private adapter: ReminderKit/SQLite-backed operations for image attachments, sections, tags, URL-attachment objects, and membership ordering
- verification adapter: post-write read-back, store backup, and schema doctor
- typed local MCP: first-class tool discovery, strict inputs, bounded pagination, exact identifiers, stable errors, and normalized receipts

The local MCP server is bundled because Apple Reminders has no equivalent hosted connector. It remains a thin transport shim: public tools route to EventKit, private tools route to the adapter, and business logic does not move into the server.

## Product-Parity Test

An improvement counts as Google Calendar parity only when it preserves the same operating discipline:

- read the relevant state before reasoning or writing;
- require a semantic scope in addition to a numeric limit;
- use opaque continuation cursors that cannot be replayed with changed filters;
- distinguish all-day dates, timed due dates, and alarms instead of collapsing them;
- preserve omitted fields and use last-modified preconditions for existing-item writes;
- return verified, pending, partial, and failed outcomes without optimistic wording;
- package task-oriented skills and deterministic rendering above the raw tools.

This copies the benchmark's product contract, not its hosted implementation or authentication model.
