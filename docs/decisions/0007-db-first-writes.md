# 0007. DB-First Writes

## Status

Superseded by 0009. Historical rationale retained; 0008 still governs image attachments.

## Decision

The original decision used direct Reminders database writes as the primary backend for local write operations except image attachments and reminder deletion.

The current policy instead prefers EventKit for public reminder fields and deletion, and uses capability-gated, fresh-version-checked private writes only for Reminders-specific surfaces. MCP deletion requires a fresh last-modified precondition and never falls through to a private backend. The adapter's DB soft-delete remains diagnostic-only and requires exact environment recovery/sync evidence.

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

Deletion must preserve Reminders' recovery model. The DB backend may only reproduce soft-delete state; it must never remove reminder rows. A DB read-back alone does not prove Recently Deleted or iCloud parity, so receipts must label unverified recovery semantics explicitly.

## Fallbacks

AppleScript may remain available for:

- validating native behavior during reverse engineering
- fields not yet understood in the database schema
- emergency fallback if schema checks fail
