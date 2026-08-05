# 0009. Google Calendar Parity Through Hybrid Backends

## Status

Accepted for the 0.2.0 development line. Live/sacrificial compatibility claims remain gated on separate approval and evidence.

## Decision

Copy Google Calendar's product contract rather than its hosted-connector implementation:

- expose a typed local stdio MCP;
- use bounded reads and stable identifiers;
- prefer EventKit for supported public reminder fields;
- preserve untouched fields and require last-modified/version preconditions for existing-item writes;
- return one normalized receipt contract for mutations;
- separate verified, verification-pending, partial, and failed outcomes;
- use purpose-specific skills and deterministic rendering;
- disclose local permissions and private-interface trust boundaries.

Keep private SQLite and ReminderKit behavior for tags, sections, advanced attachments, repair, and verified fast paths that public APIs cannot express. Every private write is command-schema-gated, path-confined, transactional where possible, read back, and explicit about recovery limits.

## Delete Policy

`delete_reminder` defaults to `backend=auto`. The adapter selects the fast DB soft-delete only when a local capability record proves Recently Deleted and sync parity for the exact OS, Reminders build, and schema fingerprint. Otherwise it uses native AppleScript. An attempted native mutation never silently falls back to SQLite.

## Cleanup Policy

`cleanup_tags --apply` remains an intentional unused-label maintenance primitive. It requires a bounded scope and preview digest, escapes wildcard characters, reacquires candidates under a write lock, proves that no assignment rows reference each label, and reports exact deleted labels plus backup/recovery semantics. Ordinary `remove_tag` only soft-deletes assignments.

## Evidence Boundary

Static checks and synthetic fixtures establish contract behavior, not real iCloud convergence or device visibility. Public release claims about DB deletion parity, ReminderKit attachment sync, tag propagation, and supported macOS builds require separately approved sacrificial tests.
