# Changelog

Notable user-visible changes to Apple Reminders are recorded here. The project follows semantic versioning after its first tagged public beta.

## 0.3.1 — 2026-08-27

### Fixed

- Reject failed MCP results in the deterministic daily-brief renderer instead
  of presenting permission or backend failures as an empty day.
- Render untrusted reminder titles, locations, sections, and identifiers as
  inert Markdown text so reminder content cannot create active links, images,
  headings, or HTML in generated briefs.
- Document the exact remove-and-re-add procedure required to move a
  tag-pinned repository marketplace to another reviewed release.

### Changed

- Declare both read and write capabilities in the plugin manifest to match the
  public tool surface.
- Document reminder content as untrusted data at the runtime, skill, and
  security boundaries.

## 0.3.0 — 2026-08-27 (public beta)

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
- Added a small local MCP launcher that finds supported Homebrew or python.org
  Python installations even when the Codex app starts with a minimal GUI
  `PATH`, while preserving the existing safe unsupported-runtime response.
- Added an actionable helper-build recovery path from Core failures to
  content-free packaging diagnosis and the macOS command-line-tools installer.
- Made native image attachment derive PNG/JPEG type from decoded bytes instead
  of the filename suffix, use ReminderKit's image-data path, and reject a
  content-type mismatch during read-back. Browser screenshots saved with a
  misleading extension now render in Reminders instead of producing an
  apparently synced but invisible attachment.

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

### Post-beta validation

- Repeat first-use permission and create/read/delete validation on a genuinely
  fresh macOS permission subject, then record the observed prompt wording.
- Verify an upgrade from this tag does not unexpectedly require permission
  reauthorization.

## 0.2.0 — internal development baseline

This version was exercised locally but was not published as a tagged public
release. It introduced the original broad typed MCP, purpose-specific skills,
EventKit writes, visible URL attachments, ReminderKit image and section saves,
Doctor, and guarded internal Maintenance operations.
