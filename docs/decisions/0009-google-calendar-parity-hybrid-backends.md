# 0009. Google Calendar Parity Through Hybrid Backends

## Status

Accepted for the 0.2.0 development line. Live/sacrificial compatibility claims remain gated on separate approval and evidence.

## Decision

Copy Google Calendar's product contract rather than its hosted-connector implementation:

- expose a typed local stdio MCP;
- use bounded reads and stable identifiers;
- prefer EventKit for supported public reminder fields;
- preserve untouched fields and require last-modified/version preconditions for existing-item writes, including EventKit deletion;
- return one normalized receipt contract for mutations;
- separate verified, verification-pending, partial, and failed outcomes;
- use purpose-specific skills and deterministic rendering;
- disclose local permissions and private-interface trust boundaries.

Keep private SQLite and ReminderKit behavior for tags, sections, advanced attachments, repair, and verified fast paths that public APIs cannot express. Section creation and membership use native ReminderKit saves plus CloudKit version read-back because direct SQLite rows can remain local-only indefinitely. Every private existing-item write consumes a fresh matching reminder version; all private writes are command-schema-gated, path-confined, transactional where possible, read back, and explicit about recovery limits.

## Delete Policy

The MCP `delete_reminder` path uses public EventKit with an exact reminder ID and fresh `expected_last_modified`. It verifies that the reminder is absent from the local EventKit store and reports Recently Deleted as expected but unverified until UI evidence is available. An identifier that is already unresolvable is a no-write not-found result, not proof of a prior successful deletion, because EventKit identifiers can change after a full sync. The caller must read current state before retrying. The path never falls back to AppleScript or SQLite. The adapter's capability-gated DB soft-delete remains a diagnostic CLI path only.

## Cleanup Policy

`cleanup_tags --apply` remains an intentional unused-label maintenance primitive. It requires a bounded scope and preview digest, escapes wildcard characters, reacquires candidates under a write lock, proves that no assignment rows reference each label, and reports exact deleted labels plus backup/recovery semantics. Ordinary `remove_tag` only soft-deletes assignments.

## Evidence Boundary

Static checks and synthetic fixtures establish contract behavior, not real iCloud convergence or device visibility. Public release claims about DB deletion parity, ReminderKit attachment sync, tag propagation, and supported macOS builds require separately approved sacrificial tests.
