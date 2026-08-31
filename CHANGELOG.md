# Changelog

Notable user-visible changes to Apple Reminders are recorded here. The project follows semantic versioning after its first tagged public beta.

## Unreleased

### Changed

- Made Doctor metadata-only and non-executing by default. Core diagnosis no
  longer starts developer-tool shims; the private-helper syntax gate now
  requires explicit Experimental toolchain mode and checks `xcode-select -p`
  before `clang`.
- Added structured Stable Core, compiler-free private, and CLT-required private
  capability boundaries plus an exact Recovery diagnosis scope.
- Clarified Finder-launched Python discovery, new-task plugin reloads, and the
  separate first-request, denied/revoked, and signed-helper-update TCC paths.
- Split runtime capability reporting into Stable Core and Experimental
  Internals, including compiler requirement, runtime verification state, exact
  build/schema compatibility, and precise blocked reasons.
- Made Core-first list/text/note/archive alternatives the default guidance for
  sections, tags, native attachments, and destructive recovery workflows.

### Fixed

- Added an immutable exact macOS/Reminders build and command-schema allowlist
  before every private mutation and exact recovery path. Missing metadata,
  unknown builds, schema drift, and missing required Command Line Tools now fail
  before private dispatch without disabling Stable Core.
- Preserved partial native failure and recovery mismatch as explicit
  pending/no-write outcomes instead of falling back or reporting success.

## 0.5.2 — 2026-09-01

### Changed

- Consolidated existing-Reminder verification behind one canonical semantic
  projection in the native EventKit helper and public Core. Requested changes
  are combined with every stable user-authored field, while provider-owned or
  derived identity, timestamp, completion-date, and display-title fields are
  explicitly excluded.
- Added a fresh exact Reference revalidation before Core action preflight, so
  validation uses current Reminder state and stale, cross-store, expired, or
  failed-revalidation grants are consumed before dispatch.

### Fixed

- Prevented title changes, completion/reopen actions, and Reminder List moves
  from reporting `verified` or issuing a new `rev1` when EventKit drops or
  transforms absolute, location, writable relative, or read-only alarms.
- Preserved alarm arrays as order-insensitive multisets with duplicate counts,
  and made due, recurrence, start, list, and other stable user state part of
  the same final-read integrity decision instead of alarm-type-specific
  condition branches.
- Refreshed the universal EventKit helper through the protected default-branch
  signing workflow, retaining Developer ID signing, notarization, stapling,
  Gatekeeper validation, source/build-input hashes, and artifact attestations.

## 0.5.1 — 2026-08-31

### Changed

- Moved release and legacy helper artifact transfers to reviewed immutable
  `actions/upload-artifact` 7.0.1 and `actions/download-artifact` 8.0.1 commits,
  both using Node 24. The provenance-bound source-signing workflow retains its
  reviewed v4 pins until a dedicated bootstrap migration can update that build
  input without invalidating the committed helper manifest.
- Reworked the public plugin card around Core-safe meeting-note, screenshot,
  and upcoming-work prompts. It now states the capability-specific dependency
  split: Core, tag assignments, and native URL attachments do not invoke
  `clang`; section writes, image-attachment changes, and exact Recently Deleted
  inspection or recovery require Xcode Command Line Tools.
- Defined writable relative alarms as the faithful bare default-display subset:
  integral `offset_seconds` from `-31536000` through `0`, with the lower bound
  exactly 31,536,000 seconds (365 elapsed days) before due. Existing unsupported
  trigger, offset, or action variants remain readable as `read_only:true` with bounded
  action metadata.
- Made the complete-array contract explicit: supplying `alarms` replaces every
  alarm, omitting it preserves the array, and `null` or `[]` explicitly clears
  it. A non-empty replacement is rejected before mutation when an existing
  read-only alarm could not be reconstructed faithfully.

### Fixed

- Expanded verification dependencies for due-relative alarms. When the
  resulting Reminder contains a relative alarm, due-only and alarm-only changes
  now verify both `due` and `alarms`; list/account moves verify the destination
  (`calendar_id` natively and `list_id` publicly), `due`, and `alarms`. Native
  verification performs a fresh identifier lookup after save, and public
  verification repeats the expanded comparison against a separate exact read.
- Rejected JSON `false` and `true` as relative `offset_seconds` at the native
  helper boundary, closing the Foundation Boolean-as-`NSNumber` bypass before
  mutation.
- Preserved read-only alarm action metadata across moves, completion changes,
  and unrelated patches. Lossy native trigger state can no longer compare equal
  by accident; such a committed write remains verification-pending.
- Bound refreshed signed-helper provenance to a separately recorded,
  default-branch-owned workflow commit, with no target-controlled code executed
  inside the signing or attestation permission boundaries.

## 0.5.0 — 2026-08-31

### Added

- Bundled a universal `arm64`/`x86_64` EventKit Core helper for macOS 14 and
  newer. The app is Developer ID signed, notarized by Apple, stapled for
  offline Gatekeeper verification, and bound to its reviewed source and build
  inputs by a committed provenance manifest and GitHub artifact attestation.
- Added writable EventKit relative alarms with bounded negative
  `offset_seconds`, an existing or same-request due-date anchor, final exact
  read-back, and public create/change schemas. Quick capture now preserves
  requests such as “two weeks before” instead of converting them to an
  absolute alarm date.

### Changed

- Removed Xcode Command Line Tools from ordinary Core setup. Core now resolves
  only the reviewed helper inside the installed plugin; a missing or invalid
  bundle fails before mutation instead of downloading or automatically
  compiling a replacement. Runtime trust checks parse Mach-O architecture and
  minimum-version metadata directly and use only the macOS system `codesign`
  tool; explicit contributor builds retain the legacy source path.
- Kept Xcode Command Line Tools as a capability-specific dependency for section
  writes, image-attachment changes, and exact Recently Deleted inspection or
  recovery, which compile three private Objective-C helpers locally. Tag
  assignments and native URL attachment operations remain guarded
  Python/SQLite paths and do not invoke `clang`.
- Migrated the Core helper once from its legacy ad-hoc signing identity to the
  stable Developer ID signing identity used by project releases. macOS may ask
  for Reminders access again at this upgrade boundary.
- Added a secrets-free tag release path that independently verifies the helper
  attestation, provenance, default-branch ancestry, package allowlist, file
  modes, and deterministic ZIP before publishing exact checksums and assets.

## 0.4.0 — 2026-08-29

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

- Routed all bundled subprocesses through one byte-bounded, deadline-aware
  process-group runner. Timeouts, oversized output, invalid UTF-8, and exited
  leaders with lingering descendants terminate the full group and reap the
  direct leader; only a typed pre-launch failure can prove that a mutation was
  not dispatched.
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
- Isolated MCP initialization, rate limits, backend paths, and lazy Facade
  ownership inside one `McpRuntime` per stdio session instead of mutable module
  globals. Test backend injection is now constructor-owned and cannot leak into
  another runtime.
- Replaced private JSON dispatch-provenance flags with a closed internal
  `TransportResult`. Only the parent launcher can prove that a subprocess never
  started; child output, timeouts, transport errors, and malformed results
  cannot forge that fact or clear a durable mutation fence.
- Carried independent `not_mutated`/`committed`/`unknown` facts through every
  Core, Native, and Recovery mutation, durable replay, public projection, and
  server fallback. Final-proof loss keeps commits but degrades unproven
  no-write claims to unknown instead of reconstructing state from display
  status.
- Extracted the durable write-ahead fence from the private adapter into one
  shared deep Module. Core create and exact list ensure now use its narrow
  function and shared typed errors; list check-then-create is serialized across
  runtimes without a competing facade cache. Fresh keys recheck current list
  state; unresolved same-input work blocks redispatch.
- Durable replay now rejects missing, malformed, or unknown store versions
  before dispatch, rejects ambiguous record/timestamp shapes, and removes
  malformed completed-result payloads without making their write-ahead fences
  eligible for capacity eviction.
- Extracted strict EventKit response validation and outcome-unknown Receipt
  construction into one pure protocol Module shared by the bridge, server, and
  Core. Server/Core no longer load the executable bridge in-process for those
  rules.

### Removed

- Removed the obsolete 0.2 adapter CLI surface: direct Core writes, broad
  snapshot/search/cache routes, backup and log maintenance, unused-label
  cleanup, attachment audit/repair, and native UI handoff. The shipped parser
  now exposes exactly the 16 commands reached by the public Modules, with no
  deprecated command aliases or legacy-command shims. Retained Native commands
  keep only their documented schema-gated backend branches.
- Removed the standalone recovery-backup helper and its package/test surface.
  Recently Deleted recovery remains in the guarded `del1` Module path; durable
  idempotency storage remains unchanged.
- Reduced the deterministic release-archive hard ceiling from the temporary
  1.28 MiB allowance to 1.20 MiB after the deletion recovered package headroom.

### Fixed

- Rejected non-object records in EventKit list/fetch pages, Recently Deleted
  pages, and final public collection results. A malformed backend member now
  fails the whole bounded read instead of being preserved, silently dropped,
  or returned as a verified MCP result.
- Made keyed recovery and image-mutation failures durable before returning, so
  the first result and its same-key replay keep the same conservative safety
  classification. Only the existing typed proof of no dispatch may clear a
  write-ahead fence; unclassified callback failures now require inspection and
  cannot be capacity-evicted into a later redispatch.
- Treated a missing native `mutation_attempted` failure marker as an unknown
  outcome for recovery and image removal; only an explicit `false` can prove
  that the helper did not save.
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
- Bound every terminal Core create/change and ordinary Native mutation to the
  requested field or action before returning `verified` or issuing a fresh
  `rev1`. A contradictory final read or post-dispatch projection failure now
  preserves a possible write and requires a fresh read instead of claiming
  success or `failed_no_mutation`; pre-dispatch Reference read failures remain
  proven no-write results.
- Canonicalized native UUID and tag comparisons and require complete,
  non-truncated inventories before proving a tag or attachment absent. Image
  success now binds a mode-0600 validated snapshot to the final SHA-512, byte
  size, dimensions, decoded UTI, and mobile-visible CloudKit evidence.
- Added a content-free durable idempotency fence before commit-capable adapter
  callbacks. Store damage or a failed pre-dispatch fence stops the write, while
  a crash or final-Receipt persistence failure blocks redispatch and returns
  verification-pending on replay. The current key survives wall-clock jumps,
  and unresolved fences cannot be evicted merely to make capacity for a new
  write or age into a second dispatch; the 30-day cutoff applies only to
  redispatch-safe results. A callback failure that affirmatively occurred
  before mutation clears its fence for a safe retry; possible-commit failures
  retain it. Core create
  now carries contract-validated EventKit no-write results through that same
  cleanup path instead of leaving a permanent unresolved fence.
- Removed primitive arrays and bare recurrence counts from persisted retry
  snapshots. On the next keyed operation, complete modern and legacy v1
  results are re-sanitized under the existing process lock and atomically
  rewritten without changing fence identity. A failed scrub never redispatches
  an existing callback and returns only an in-memory redacted replay plus a
  bounded warning.
- Classified non-object idempotency entries and invalid or non-finite entry
  timestamps as a typed unreadable-store failure before dispatch. Corrupt
  records can no longer fall through pruning or escape as a raw conversion
  error with ambiguous mutation state.
- Added a native ReminderKit snapshot guard immediately before deleted-item
  recovery, actual image backing-byte SHA-512 checks, pre/native/post attachment
  count agreement, and independent mutation-state transport into public
  contract validation. Missing proof or any post-save read failure now fails
  closed without inventing a verified or no-write outcome.
- Mapped recovery failures to fixed public messages so private store paths and
  native helper details cannot enter structured output. Parent-verified missing
  paths and request-bound failures retain pre-dispatch `failed_no_mutation`;
  generic process transport errors, timeouts, and malformed post-launch output
  remain outcome-unknown because an `OSError` alone cannot prove the child never
  started. Child output cannot spoof parent dispatch provenance.
- Removed an unsafe nil-completion ReminderKit sync trigger that could crash
  after a successful recovery save. A helper crash or malformed post-dispatch
  result is now conservatively verification-pending rather than a false
  no-write failure.
- Preserved local clock and IANA timezone in timed daily-brief output and
  translated Apple/EventKit priority numbers into high, medium, and low labels.
- Allowed multiple content-addressed backing-file candidates only when every
  candidate matches the stored SHA-512 digest. ReminderKit UTI normalization is
  accepted when the copied bytes, dimensions, and file size remain exact.
- Rejected contradictory `failed_no_mutation` claims across the EventKit,
  adapter, Native, Core, and public Receipt boundaries. Non-empty post-state or
  affirmative write evidence now preserves an unknown/pending outcome, and
  generic adapter failures no longer clear a durable fence without explicit
  pre-dispatch proof.

### Local validation

- On macOS 26.5.2 (25F84), Reminders 7.0 (3976), restored four exact deleted
  Reminders with all four images, created a four-image rollup, and observed the
  final state in the native app.
- Exercised `copy_image` end to end through the public MCP, observed one image
  on the destination in the native app, then deleted the disposable test
  Reminder and verified local absence.
- Re-ran the complete source-MCP live smoke after runtime isolation and typed
  transport changes. All create/replay/read/change/section/image/delete steps
  and exact synthetic-list cleanup passed. A separate synthetic Reminder was
  observed in the native Reminders list through Computer Use, then deleted via
  a fresh public Reference; local absence and UI disappearance both matched.

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
