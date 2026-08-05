# 0007. DB-First Writes

## Status

Accepted, amended by 0008 for image attachments.

## Decision

Use direct Reminders database writes as the primary backend for local write operations except image attachments and reminder deletion.

AppleScript remains a fallback for most fields. Reminder deletion defaults to AppleScript so it follows native Reminders deletion and Recently Deleted behavior; the DB soft-delete backend is diagnostic/recovery-only.

Image attachments are no longer DB-first because DB-only image rows can render on macOS while failing to appear on iPhone. See 0008.

## Rationale

AppleScript preserves native semantics but is too slow and app-state-dependent for a delegated assistant workflow. Smoke tests showed public AppleScript writes can take tens of seconds.

The Reminders app is not a large opaque SaaS system. The local store is inspectable, and prior tests proved that native image attachments and sections can be written directly and rendered correctly by Reminders.

## Scope

DB-first writes may cover:

- reminder creation
- title and notes updates
- priority and flag updates
- completion
- diagnostic soft-delete fallback
- section creation
- section membership
- image attachment fallback/diagnostics only

## Hard Rule

DB-first does not mean hard delete.

Deletion must preserve Reminders' recovery model. Use native AppleScript deletion by default. If the explicit diagnostic DB backend is used, it may only reproduce native soft-delete state; it must never remove database rows.

## Fallbacks

AppleScript may remain available for:

- validating native behavior during reverse engineering
- fields not yet understood in the database schema
- emergency fallback if schema checks fail
