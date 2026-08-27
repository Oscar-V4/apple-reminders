# 0009. Google Calendar Parity Through Hybrid Backends

## Status

Superseded by ADR 0010 for the 0.3 public Interface. Its hybrid-backend,
verification, and real-use evidence decisions remain historical constraints on
the Modules that implement the smaller Interface; its public Maintenance and
raw-precondition shape do not carry forward. ADR 0014 subsequently removed the
remaining direct Core-write, cleanup, backup, cache, and repair CLI surfaces.

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

## Visible URL Policy

A non-null `url` supplied to MCP reminder create or update is one hybrid product operation. EventKit stores the public URL metadata, then the private adapter obtains a fresh reminder version and creates the matching native URL attachment that the Reminders app displays. The combined receipt is `verified` only after attachment read-back. If EventKit committed but attachment verification fails, the receipt is `partial_success` with explicit attachment recovery rather than a false success. Create retries replay the combined receipt and do not duplicate either object. Setting `patch.url` to null clears only EventKit metadata; URL attachment deletion remains a separate explicit operation because a reminder can own multiple attachments.

If a later fresh retry already sees EventKit URL B but the native inventory
contains B plus another URL attachment, the plugin cannot recover A-to-B
provenance from the fresh snapshot without risking deletion of an unrelated
link. It therefore performs no native write, returns a bounded ambiguity rather
than `unchanged`, issues no fresh writable Reference, and requires exact native
inspection followed by attachment-ID cleanup.

## Delete Policy

The MCP `delete_reminder` path uses public EventKit with an exact reminder ID and fresh `expected_last_modified`. It verifies that the reminder is absent from the local EventKit store and reports Recently Deleted as expected but unverified until UI evidence is available. An identifier that is already unresolvable is a no-write not-found result, not proof of a prior successful deletion, because EventKit identifiers can change after a full sync. The caller must read current state before retrying. The path never falls back to AppleScript or SQLite. ADR 0014 removed the former capability-gated DB soft-delete CLI path.

## Cleanup Policy

In the 0.2 design, `cleanup_tags --apply` was an intentional unused-label
maintenance primitive with bounded scope, a preview digest, literal matching,
a write lock, zero-reference proof, and read-back. ADR 0014 removed the command
and backup machinery; ordinary retained `remove_tag` still soft-deletes only the
exact assignment.

The historical implementation backed up a selected SQLite store for cleanup and
the broader container for cross-store attachment repair. Those backup paths are
no longer packaged; legacy artifacts remain user-controlled local files.

## 0.2.x Compatibility Policy

The typed MCP/EventKit tools are the public write seam. Direct adapter public
write commands remained present but deprecated through 0.2.x so prior working
flows were not silently broken. ADR 0014 completed their separately reviewed
physical removal before 0.4.

## Evidence Boundary

Static checks and synthetic fixtures establish contract behavior, not real iCloud convergence or device visibility. Public release claims about DB deletion parity, ReminderKit attachment sync, tag propagation, and supported macOS builds require separately approved sacrificial tests.
