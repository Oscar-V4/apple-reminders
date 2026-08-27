# Changelog

Notable user-visible changes to Apple Reminders are recorded here. The project follows semantic versioning after its first tagged public beta.

## 0.4.0 — 2026-08-28

### Added

- Added bounded Recently Deleted discovery and exact one-item recovery. Only an
  exact deleted-item read issues a short-lived, one-use opaque `del1`
  Reference; recovery requires a same-account destination, an idempotency key,
  native attachment preservation, and final EventKit read-back.
- Added the closed `copy_image` attachment action. It revalidates fresh source
  and destination `rev1` References, snapshots one exact private image without
  exposing its path, preserves the source, and verifies byte-identical content
  on the destination.
- Added explicit completed-range daily briefs with a separate Completed
  section and human priority labels.
- Added a current workflow capability matrix covering UI-versus-API ordering,
  dependency-first destructive flows, CRUD boundaries, and evidence limits.

### Changed

- Bound pagination to a private ordered ID-and-revision SHA-256 snapshot.
  Continuation now fails read-only with `pagination_snapshot_stale` when list
  membership or revisions change between pages.
- Added a separate snapshot-bound Recently Deleted cursor so deleted items
  beyond the first 200 remain reachable. Continuations bind the identical
  account and limit and restart from page one after snapshot drift.
- Literal UI-relative selection now requires direct current-UI identity.
  API order is offered only as an explicitly approved reinterpretation;
  ambiguous “organize” requests do not authorize deletion or imply bitmap
  compositing.
- Broad cleanup now discovers summaries first, obtains write References just
  in time, processes authorized 25–40 item chunks, verifies each item, and
  stops the entire run on the first uncertain Receipt.
- MCP text output now leads with a content-free outcome, write state, evidence
  scope, exact safe target identifiers, and the next read-only action.
- URL metadata changes now replace one exact matching visible attachment,
  preserve unrelated or ambiguous objects, and reuse an already-correct target
  attachment instead of creating a duplicate on retry.

### Fixed

- Made top-level discriminated MCP schemas branch-complete so Codex discovery
  exposes all bounded `fetch_reminders` and exact Recently Deleted arguments,
  instead of reducing branches to only a status or kind discriminator.
- Kept URL A-to-B retry state honest: if EventKit already reads B while native
  attachments contain B plus another URL, the plugin performs no write,
  returns an ambiguity, and requires exact attachment inspection rather than
  reporting `unchanged` or guessing which link to delete.
- Rejected malformed, mismatched, or incomplete native URL inventories before
  any attachment write, preventing a hidden existing URL from being duplicated.
- Re-fetched and hashed the deleted Reminder immediately before the native
  recovery save, and prevented non-empty post-state from being laundered into
  a `failed_no_mutation` Receipt.
- Bound verified recovery's raw target, before/after identity, deletion time,
  destination, and attachment counts to the authorized item and proof tuple.
  Snapshot-stale active and deleted pages now point to the same read without a
  cursor instead of falling back to diagnosis.
- Required both raw recovery attachment-set digests to equal the private
  `del1` guard before `verified`, and made a fresh unchanged URL retry with
  stale A but no B fail without a native write instead of appending B.
- Reduced Recently Deleted pagination exposure by reading only ordered
  identity/revision columns for the snapshot and full content for the current
  page inside one pinned read transaction, with page revisions checked against
  the fingerprint. Missing or expired deleted items now retain typed
  `not_found` errors.
- Added a native ReminderKit snapshot guard immediately before deleted-item
  recovery, actual image backing-byte SHA-512 checks, pre/native/post attachment
  count agreement, and independent mutation-state transport into public
  contract validation. Missing proof or any post-save read failure now fails
  closed without inventing a verified or no-write outcome.
- Mapped recovery failures to fixed public messages so private store paths and
  native helper details cannot enter structured output. Adapter-missing and
  launch-failed cases now retain proven pre-dispatch `failed_no_mutation`, while
  timeouts and malformed post-launch output remain unknown.
- Removed an unsafe nil-completion ReminderKit sync trigger that could crash
  after a successful recovery save. A helper crash or malformed post-dispatch
  result is now conservatively verification-pending rather than a false
  no-write failure.
- Preserved local clock and IANA timezone in timed daily-brief output and
  translated Apple/EventKit priority numbers into high, medium, and low labels.
- Allowed multiple content-addressed backing-file candidates only when every
  candidate matches the stored SHA-512 digest. ReminderKit UTI normalization is
  accepted when the copied bytes, dimensions, and file size remain exact.

### Local validation

- On macOS 26.5.2 (25F84), Reminders 7.0 (3976), restored four exact deleted
  Reminders with all four images, created a four-image rollup, and observed the
  final state in the native app.
- Exercised `copy_image` end to end through the public MCP, observed one image
  on the destination in the native app, then deleted the disposable test
  Reminder and verified local absence.

## 0.3.1 — 2026-08-27

### Fixed

- Reject failed MCP results in the deterministic daily-brief renderer instead
  of presenting permission or backend failures as an empty day.
- Render untrusted reminder titles, locations, sections, and identifiers as
  inert Markdown text so reminder content cannot create active links, images,
  headings, or HTML in generated briefs.
- Document the exact remove-and-re-add procedure required to move a
  tag-pinned repository marketplace to another reviewed release.
- Remove replaced or deleted images through ReminderKit instead of treating a
  SQLite soft-delete flag as proof that the old thumbnail disappeared. Native
  read-back now verifies exact reminder detachment while preserving the sync
  tombstone, preventing duplicate images in the Reminders UI.
- Do not hold a SQLite write transaction while ReminderKit attaches an image;
  this removes a reproducible 30-second pending result. Unknown native removal
  outcomes now require a fresh inspection and never trigger an automatic
  compensation that could remove both the old and replacement images.
- Keep native section and reminder-change Receipts contract-valid when
  verification is still pending: every such result now includes a structured
  `sync_pending` error and safe read-before-retry recovery instead of
  collapsing into a generic public-result contract failure. Unrelated journal
  warnings cannot replace the causal pending reason.
- Route every recoverable pending mutation to the matching read surface:
  Reminder creation to `fetch_reminders`, list creation to
  `list_reminder_lists`, section creation to `inspect_reminder_native`, and
  reminder-scoped changes to `read_reminder`. The public contract rejects a
  missing or mismatched recovery action, forces `sync_pending` mutation errors
  to `retryable=false`, and never authorizes automatic retry.
- Distinguish non-retryable native image transport or content-type mismatches
  from retryable iCloud visibility convergence in machine-readable errors.
- Treat malformed optional fields in a post-dispatch native Receipt as an
  unknown possible write. A damaged warning or error field can no longer fall
  through to a false `failed_no_mutation` result.
- Extend the bounded native attachment verification window from six to ten
  seconds after a live attachment became mobile-visible after roughly seven
  seconds. The mutation is still never retried automatically.
- Report an explicit Reminders access request as an attempted request, together
  with the authorization state observed before and after it. The result says
  whether a first-decision prompt was expected and explicitly records prompt
  observation as unknown. The deprecated `prompted_explicitly` flag remains
  additive v2 compatibility for “the explicit access tool ran”; it is not
  evidence that macOS displayed a prompt.
- Preserve that bounded access receipt when permission is denied and do not
  direct the access tool back to itself. Reconcile the callback, error, and
  final authorization state before returning `verified`.
- Let the native access helper return its structured 60-second timeout before
  the 70-second launcher and 80-second MCP transport limits expire.

### Changed

- Declare both read and write capabilities in the plugin manifest to match the
  public tool surface.
- Give every public MCP tool a human-readable title so discovery and
  clients that render MCP titles can present a user-facing action name.
- Document reminder content as untrusted data at the runtime, skill, and
  security boundaries.
- Extend the opt-in public MCP live smoke through image attach, native inspect,
  replace, second inspect, exact delete, and final zero-image inspection using
  two distinct synthetic PNG payloads.

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
