# Changelog

Notable user-visible changes to Apple Reminders are recorded here. The project follows semantic versioning after its first tagged public beta.

## Unreleased — 0.3.0 public beta

### Changed

- Replaced the internal 32-tool development surface with a static 13-tool
  Interface: eight Core tools, four Native Extension tools, and one Diagnostics
  tool.
- Replaced caller-assembled `last_modified`/`reminder_version` preconditions
  with one short-lived opaque `rev1` Reference from an exact read.
- Made bounded Core work the first-use path. `diagnose_reminders` is a targeted,
  content-free follow-up after a relevant failure rather than an onboarding
  preflight.
- Matched runtime ownership to the public 8/4/1 split: list creation now belongs
  to Core, diagnosis is independent of Native Extension, and production backend
  implementations are isolated behind lazy composition.
- Centralized public result and Receipt validation while keeping compact
  input-only MCP tool discovery.
- Replaced environment-selected backend test paths with an explicitly injected
  `BackendPaths` seam in the source-only MCP harness. Packaged runtime always
  resolves bundled backends.
- Added public support, security, privacy, terms, install, temporary-disable,
  uninstall, and troubleshooting documentation.
- Added a separately gated, redacted live smoke harness that uses one exact
  disposable list, verifies bounded reads, idempotent replay, actual stale
  revision rejection, and native visibility evidence, and always attempts
  identity-checked cleanup.

### Preserved

- Bounded EventKit reads, exact-identity list and reminder targeting, create
  idempotency, guarded concurrent writes, and truthful unknown outcomes.
- Visible URL create/change with final exact EventKit read-back.
- ReminderKit image and section saves, exact-list section scope, fresh-version
  tag changes, and normalized mutation Receipts.

### Withheld from the public Interface

- Unused-tag cleanup, attachment repair, backup/Snapshot, restore, and log purge.
- Flag mutation and native `show_reminder` handoff.
- Deprecated direct adapter writes and Maintenance commands remain internal
  migration or diagnostic seams; public skills do not fall back to them.

### Planned release work

- Run one external first-install check on a clean supported Mac and record the
  supported macOS/Python combination.
- Create the first tag, deterministic ZIP/checksum, GitHub Release, and matching
  release notes after the beta candidate is approved.

## 0.2.0 — internal development baseline

This version was exercised locally but was not published as a tagged public
release. It introduced the original broad typed MCP, purpose-specific skills,
EventKit writes, visible URL attachments, ReminderKit image and section saves,
Doctor, and guarded internal Maintenance operations.
