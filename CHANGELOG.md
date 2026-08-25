# Changelog

Notable user-visible changes to Apple Reminders are recorded here. The project follows semantic versioning after its first tagged public beta.

## Unreleased — 0.3.0 public beta

### Planned

- Replace the internal 32-tool development surface with the static Core, Native Extension, and Maintenance Interface described in `docs/public-beta-0.3.md`.
- Make normal bounded work the first-use path and reserve targeted diagnosis for failures.
- Preserve the verified EventKit, visible URL, ReminderKit image/section, concurrency, idempotency, and Receipt behavior behind the new Interface.
- Add a repo marketplace, fresh-install checks, executable workflow evidence, and public release documentation.

### Changed

- Added public support, security, privacy, and terms documents.
- Defined Core, Native Extension, Maintenance, Revision, Receipt, and Snapshot vocabulary.

## 0.2.0 — internal development baseline

This version was exercised locally but was not published as a tagged public release. It introduced the typed MCP, purpose-specific skills, EventKit writes, visible URL attachments, ReminderKit image and section saves, Doctor, and guarded Maintenance operations.
