# Architecture

Apple Reminders is a local macOS integration. It borrows the operating
discipline of mature calendar connectors—bounded reads, exact identity,
preserved fields, and verified writes—without copying their hosted architecture
or treating any reference plugin as proof of quality.

## Public shape

The 0.3 public beta has one static 13-tool Interface:

### Core

- `request_reminders_access`
- `list_reminder_lists`
- `fetch_reminders`
- `read_reminder`
- `create_reminder`
- `change_reminder`
- `delete_reminder`
- `ensure_reminder_list`

### Native Extension

- `inspect_reminder_native`
- `create_reminder_section`
- `organize_reminder`
- `change_reminder_attachment`

### Diagnostics

- `diagnose_reminders`

The schema at `plugins/apple-reminders/schemas/mcp-tools.json` is the public tool contract. Inputs are
closed and bounded. Results remain versioned and centrally validated, but MCP
`outputSchema` is not duplicated into discovery because it is optional and made
the compact 13-tool payload disproportionately large.

Unused-tag cleanup, attachment repair, backup/Snapshot, restore, privacy-log
purge, flag mutation, and native UI `show_reminder` are not public 0.3 tools.
Lower-level implementations can remain internal without becoming an implied
user-facing promise.

## Modules and dependencies

1. **Skills**
   - Express bounded workflow, ambiguity, mutation, and reporting policy.
   - Start with the requested Core operation rather than preflight diagnosis.
2. **Local MCP boundary**
   - Provides JSON-RPC/stdio transport, tool input validation, dispatch, result
     sanitization, concise text summaries, and centralized result validation.
   - Production resolves only bundled backend paths.
3. **Core Module**
   - Uses EventKit for access, lists, bounded reminder fetches, exact reads,
     create/change/complete/reopen/list moves, and deletion.
   - Preserves omitted fields and keeps due dates, alarms, and recurrence typed
     separately.
   - Composes visible URL writes across EventKit and the native URL attachment
     path, then performs one final exact EventKit read.
4. **Native Extension Module**
   - Uses private ReminderKit/store-backed adapters only for Reminders-specific
     section, tag, image, and native URL attachment behavior.
   - Keeps Core usable when a private capability is unavailable.
5. **Diagnostics Module**
   - Runs one content-free diagnosis after a relevant failure and reports only
     the requested area.
   - Static private-framework path absence is inconclusive when dyld can load
     from the shared cache.
6. **Internal adapter and helpers**
   - Retain version-sensitive implementation commands, compatibility seams,
     cache/recovery research, and tests.
   - Are not a second public API and are not a fallback for skills.

There is no equivalent hosted Codex connector for the user's local native
Reminders store, so a bundled local MCP is warranted. That server remains a
boundary rather than a second copy of Reminders business logic.

## Exact identity and opaque References

List display names are not identity. Public operations use `list_id`/`list_ids`
and preserve account identity so duplicate names cannot broaden scope.

An exact `read_reminder` returns one short-lived opaque `rev1` Reference. The
Reference binds reminder identity, store identity, expiry, and the current
EventKit/private revision needed by the selected operation. Callers never
assemble or mix `last_modified` and `reminder_version` values themselves.

Existing-item changes consume the Reference. Stale, expired, already-consumed,
or failed-revalidation References produce a no-write concurrency result and
require another exact read. A post-dispatch transport failure consumes the
Reference and returns an unknown/pending outcome so blind retry cannot masquerade
as safe.

## Read and write flow

```text
skill -> bounded Core/Native tool -> Module -> selected backend
                                      |              |
                                      +---- exact read-back
                                              |
                                      validated result/Receipt
```

Normal operation is background-first:

- bounded reads use semantic scope, numeric limits, and filter-bound opaque
  cursors;
- exact writes use a current Reference and preserve omitted fields;
- every terminal success names its final read-back evidence;
- `committed_verification_pending` and `partial_success` remain visible instead
  of being translated into optimistic success;
- UI automation is not used as the normal data path.

For image attachments and sections, local Mac rendering is insufficient. A
SQLite-only row can appear locally without a native CloudKit save. The Native
Module therefore uses ReminderKit saves and CloudKit version evidence. That
evidence is described as likely mobile visibility, never direct iPhone-screen
observation.

## Diagnosis policy

Core work does not run Doctor as onboarding. If an operation reports a relevant
permission, environment, build, schema, or capability failure, the next action
may be `request_reminders_access` or targeted `diagnose_reminders`. A Native
failure does not globally block Core.

Diagnosis is content-free. It can inspect toolchain, filesystem metadata,
permission symptoms, and schema/capability metadata, but not reminder titles,
notes, list/section/tag names, attachment contents, caches, journals, or backup
contents.

## Production and test boundaries

The packaged server always uses its bundled adapter, EventKit bridge, and
Doctor. Production backend paths cannot be changed through environment
variables.

Source tests construct `BackendPaths(adapter=..., eventkit_bridge=...,
doctor=...)` and inject it into `mcp.server.main(backend_paths=...)` through the
source-only harness. This makes subprocess behavior deterministic without
adding a production configuration surface.

## Behaviors the smaller Interface must preserve

- EventKit reads and primary-field mutations with exact identity and bounds.
- Create idempotency and guarded update/complete/reopen/move/delete.
- Visible URL composition and its final exact read.
- ReminderKit image and section writes with native sync evidence.
- Exact-list section scope and fresh-revision tag assignment.
- Stale-write rejection, one-use References, and unknown-outcome safety.
- Normalized Receipts with verified, unchanged, pending, partial, and failed
  states.

The adapter's backup, repair, cache, log-purge, flag, and UI-handoff code can be
maintained for internal evidence without increasing public discovery or asking
first-time users to understand recovery internals.
