# 0007. DB-First Writes

## Status

Accepted.

## Decision

Use direct Reminders database writes as the primary backend for local write operations.

AppleScript remains a fallback, not the default write path.

## Rationale

AppleScript preserves native semantics but is too slow and app-state-dependent for a delegated assistant workflow. Smoke tests showed public AppleScript writes can take tens of seconds.

The Reminders app is not a large opaque SaaS system. The local store is inspectable, and prior tests proved that native image attachments and sections can be written directly and rendered correctly by Reminders.

## Scope

DB-first writes may cover:

- reminder creation
- title and notes updates
- priority and flag updates
- completion
- soft/native-style deletion
- section creation
- section membership
- image attachment insertion

## Hard Rule

DB-first does not mean hard delete.

Deletion must preserve Reminders' recovery model by reproducing native soft-delete semantics, such as marking a reminder for deletion and removing it from its list, instead of removing database rows.

## Fallbacks

AppleScript may remain available for:

- validating native behavior during reverse engineering
- fields not yet understood in the database schema
- emergency fallback if schema checks fail
