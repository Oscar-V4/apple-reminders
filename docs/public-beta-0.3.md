# Apple Reminders 0.3 Public Beta

This is the implementation and review specification for the first GitHub/repo-marketplace public beta. Domain terms are defined in [`CONTEXT.md`](../CONTEXT.md).

Fixed point: `07c60604c27a939708ab1966fea416b9a3105a75`

## Outcome

A first-time macOS Codex user can install one plugin, immediately perform routine Reminder work, and encounter permission or diagnosis steps only when the requested operation needs them. Existing EventKit, native URL, ReminderKit image/section, concurrency, idempotency, and Receipt behavior remains intact behind a smaller Interface.

## Public Interface

The default MCP tool list contains exactly these 13 tools.

### Core

1. `request_reminders_access`
2. `list_reminder_lists`
3. `fetch_reminders`
4. `read_reminder`
5. `create_reminder`
6. `change_reminder`
7. `delete_reminder`
8. `ensure_reminder_list`

### Native Extension

9. `inspect_reminder_native`
10. `create_reminder_section`
11. `organize_reminder`
12. `change_reminder_attachment`

### Diagnostics

13. `diagnose_reminders`

The tool count is a reviewable release Interface, not the product goal. A future recognizable user goal may justify a new tool.

## Interface requirements

### Core works first

- Skills attempt the requested bounded Core read or change without Doctor or capability preflight.
- A permission-required operation returns one `next_action`; access is
  requested only then, followed by one retry. If that explicit access request
  is denied, its bounded receipt is returned without a self-retry loop.
- A helper-build environment failure points to content-free packaging diagnosis
  and the command-line-tools installer instead of a blind retry. Native
  Extension failure does not block otherwise available Core behavior.
- The local MCP launcher finds a supported Python in the Codex process `PATH`
  or standard Homebrew/python.org paths without sourcing shell startup files;
  an older-only environment preserves the structured no-write runtime failure.
- `list_reminder_lists` includes the account identity needed to distinguish duplicate names; a display name is never identity.

### Exact references and revisions

- Exact reads return one opaque Reminder reference token that internally binds stable identity, store identity, expiry, and a fresh Revision; callers cannot mix those parts.
- Existing-Reminder changes accept that reference instead of exposing a choice between EventKit `last_modified` and private `reminder_version`.
- A stale Revision returns `concurrent_modification` with no mutation.
- Page reads are semantically bounded and their cursors are bound to the original filter fingerprint.
- Omitted patch fields remain unchanged. Due dates, alarms, and recurrence stay distinct.

### Mutations and Receipts

- `create_reminder` keeps create idempotency and returns the exact created Reminder plus its fresh reference.
- `change_reminder` covers public-field patching, completion/reopen, and list moves with a closed discriminated action.
- `delete_reminder` uses EventKit native delete only and verifies local absence without claiming UI selection.
- Receipt states remain `unchanged`, `verified`, `committed_verification_pending`, `partial_success`, `failed_no_mutation`, and `failed_manual_repair_required`.
- `verified` requires an exact read-back after the final mutation step.

### Visible URLs

- A URL supplied to Core create/change remains one product operation.
- Its Implementation performs EventKit metadata save, visible native URL attachment, and a final exact read.
- The returned Revision and `after` state come from that final read.
- A failed attachment step preserves the existing partial-success and recovery semantics.

### Native Extension

- `inspect_reminder_native` returns requested section, tag, attachment, and native sync evidence plus a fresh opaque Revision.
- Section queries and changes use exact list identity. Duplicate list names cannot broaden scope.
- `organize_reminder` covers section moves and tag add/remove through closed actions.
- `change_reminder_attachment` covers image/URL attach, replace, and delete through closed actions.
- Image inputs must be absolute regular non-symlink files decoded as PNG or JPEG, at most 25 MiB, at most 16,384 pixels per dimension, and at most 40,000,000 total pixels.
- ReminderKit image and section saves retain their CloudKit/mobile-sync evidence. Evidence is not described as direct iPhone observation.
- Flag mutation is not promised until a typed, exact-ID, read-back-verified Interface exists.
- `show_reminder` is absent until exact UI selection can be observed; opening the app alone is not `verified`.

### Diagnostics and withheld maintenance

- `diagnose_reminders` runs one content-free diagnosis, defaults to a small Core summary, and reports only the requested area.
- A missing framework path cannot by itself prove that a dyld-shared-cache capability is blocked.
- Unused-tag cleanup, attachment repair, local-log deletion, backup, and Snapshot operations remain internal in 0.3. They are not normal Reminder work and are withheld until a complete preview/apply contract and verified restoration story justify their public cost and risk.
- Disposable cache and deprecated DB-first write commands are not part of the public runtime Interface.

### Schemas and annotations

- Every tool has a closed, bounded input schema. MCP `outputSchema` is optional and is intentionally not duplicated into `tools/list`; the 13-tool discovery contract stays below 32 KiB while centralized Module validators and boundary tests enforce result envelopes and mutation Receipts.
- Size, count, date-range, cursor, selector, and path bounds are explicit.
- Annotations describe local private-state effects accurately; URL attachment is not marked as open-world public-web access.
- Structured results always include `schema_version` and `operation`. MCP text output is a concise status summary and does not duplicate large structured payloads.

## Behavior migration

| Existing behavior | 0.3 Interface |
|---|---|
| capability/access request | Core error flow + `request_reminders_access` |
| accounts and lists | `list_reminder_lists` |
| bounded fetch and exact read | `fetch_reminders`, `read_reminder` |
| create | `create_reminder` |
| update, complete, reopen, move | `change_reminder` |
| delete | `delete_reminder` |
| list creation | `ensure_reminder_list` |
| sections, tags, attachments inspection | `inspect_reminder_native` |
| section creation | `create_reminder_section` |
| tag changes and section move | `organize_reminder` |
| image/URL attach, replace, delete | `change_reminder_attachment` |
| Doctor | `diagnose_reminders` |
| unused-tag cleanup, attachment repair, backup, and log purge | withheld internal maintenance |
| UI handoff | withheld pending exact-selection evidence |

The deprecated adapter CLI may exist temporarily only as an internal migration seam. Skills and the public MCP must not fall back to it.

## Test seams

Tests observe behavior only through these agreed Interfaces.

1. MCP schema and dispatch Interface.
2. Core, Native Extension, and Diagnostics Module Interfaces with deterministic Adapters.
3. Opt-in live Reminders smoke Interface on a disposable list.
4. Installed release artifact Interface from a clean unpacked package.

Replace shallow route tests once the same behavior is protected at a new Interface; do not layer duplicate implementation-coupled tests.

## Required evidence

- The repo marketplace points to the canonical `plugins/apple-reminders` runtime subtree; a marketplace install does not copy repository tests, development docs, workflows, screenshots, or `dist`.
- Every behavior change starts with a failing Interface-level test and reaches green before the next slice.
- The full deterministic suite passes on the documented Python and macOS matrix.
- A live disposable workflow covers list/create idempotent replay, a bounded
  fetch, parallel exact reads followed by stale-revision rejection,
  patch/complete/reopen/delete, URL, image, section, sync evidence, and exact
  cleanup. Tag regression remains covered by isolated tests and one-off live
  evidence; the repeatable harness does not create persistent tag tombstones.
- The clean packaged artifact initializes and lists the expected tools without using repository-relative development files.
- The packaged manifest starts successfully with a minimal GUI-style `PATH`
  when a supported Python exists in a standard install location.
- Skill evals cover direct, indirect, incomplete, should-not-activate, and destructive edge prompts.
- README includes install, first permission, first prompts, upgrade, disable, uninstall, local-data cleanup, and troubleshooting.
- Repository marketplace metadata, manifest, CHANGELOG, support/security/privacy/terms, tag, ZIP, checksum, and release version agree.

## Non-goals

- OpenAI universal public-directory submission.
- Automatic restore.
- Guaranteed iCloud convergence time or direct iPhone UI observation.
- A companion macOS app.
- Large adapter/server refactoring that does not earn an Interface behavior or release requirement.

## Completion gate

The beta is ready only when all 13 tools satisfy their centralized result contracts, every migrated behavior above has execution evidence, no old skill depends on a removed route, the packaged fresh-install flow passes, and a two-axis Standards/Spec review has no unresolved release-blocking finding.
