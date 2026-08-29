# Architecture

Apple Reminders is a local macOS integration. It borrows the operating
discipline of mature calendar connectors—bounded reads, exact identity,
preserved fields, and verified writes—without copying their hosted architecture
or treating any reference plugin as proof of quality.

## Public shape

The 0.4 Interface has one static 15-tool surface:

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

### Recovery

- `inspect_recently_deleted`
- `recover_deleted_reminder`

### Diagnostics

- `diagnose_reminders`

The schema at `plugins/apple-reminders/schemas/mcp-tools.json` is the public tool contract. Inputs are
closed and bounded. Results remain versioned and centrally validated, but MCP
`outputSchema` is not duplicated into discovery because it is optional and made
the compact tool payload disproportionately large.

Unused-tag cleanup, raw attachment export, attachment repair, broad
backup/Snapshot restore, privacy-log purge, flag mutation, and native UI
`show_reminder` are not public tools.
Lower-level implementations can remain internal without becoming an implied
user-facing promise.

## Modules and dependencies

1. **Skills**
   - Express bounded workflow, ambiguity, mutation, and reporting policy.
   - Start with the requested Core operation rather than preflight diagnosis.
2. **Local MCP boundary**
   - Provides JSON-RPC/stdio transport, tool input validation, dispatch, result
     sanitization, concise text summaries, and centralized result validation.
   - Marks all eight mutation tools as open-world because Apple account sync and
     shared lists can carry their effects beyond the local process. A mutation
     Receipt that is pending, partial, or otherwise unresolved sets MCP
     `isError=true` while preserving the full structured Receipt and its safe
     read-only next action.
   - Constructs one `McpRuntime` per stdio session. Initialization, rate-limit
     history, immutable backend paths, and lazy Facade instances are not
     process-global mutable state.
   - Carries subprocess launch evidence in a typed `TransportResult`; child
     JSON cannot claim that dispatch never started.
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
5. **Recovery Module**
   - Lists Recently Deleted items without write authority and issues a
     short-lived `del1` only after an exact deleted-item read.
   - Recovers one same-account item through ReminderKit and requires native
     attachment preservation plus final EventKit read-back.
6. **Diagnostics Module**
   - Runs one content-free diagnosis after a relevant failure and reports only
     the requested area.
   - Static private-framework path absence is inconclusive when dyld can load
     from the shared cache.
7. **Durable idempotency Module**
   - Owns the write-ahead fence, process lock, privacy-preserving Receipt
     snapshot, replay, retention, and atomic v1 JSON persistence behind one
     `execute_idempotent` function.
   - Is composed directly into Core create and imported by the retained Native
     adapter commands. Core no longer loads the full private adapter in-process
     merely to obtain durability or error types.
   - Freezes the existing file format and failure ordering. Storage policy,
     hash algorithms, lock scope, and error classification are not pluggable
     extension points.
   - Re-sanitizes complete v1 results under the same lock, preserving fence
     metadata while removing primitive user-authored arrays before replay.
8. **EventKit response protocol Module**
   - Owns wire response validation, projected/full mutation Receipt validation,
     and exact outcome-unknown Receipt construction behind three pure
     functions.
   - Is imported directly by the executable EventKit bridge, MCP server, and
     Core backend. Core and server do not load the giant bridge script
     in-process to obtain protocol rules.
   - Does not own subprocess provenance, request normalization, helper
     compilation, native execution, or Core mutation-state interpretation.
9. **Internal adapter and helpers**
   - Retain only the 16 implementation commands required by the public Modules.
     The obsolete 0.2-era direct Core write, maintenance, cache, backup, and
     repair CLI has been physically removed rather than hidden behind aliases.
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

- bounded reads use semantic scope, numeric limits, and snapshot-bound opaque
  cursors that reject changed ordered membership or revisions;
- exact writes use a current Reference and preserve omitted fields;
- every terminal success names its final read-back evidence;
- `committed_verification_pending` and `partial_success` remain visible instead
  of being translated into optimistic success;
- UI automation is not used as the normal data path.

For image attachments and sections, local Mac rendering is insufficient. A
SQLite-only row can appear locally without a native CloudKit save. The Native
Module therefore uses ReminderKit saves and CloudKit version evidence. That
evidence is described as likely mobile visibility, never direct iPhone-screen
observation. The image helper also decodes the source bytes to select the PNG
or JPEG UTI, uses ReminderKit's image-data attachment path, and requires that
UTI to survive database read-back; a filename extension is not format evidence.
Cross-Reminder copy adds independent source and destination revalidation, a
private byte snapshot, exact SHA-512/size/dimension comparison, and final native
read-back without exposing the backing path. A stale source UTI may normalize
to the type decoded from byte-identical data and is not treated as corruption.

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
variables. Durable idempotency uses its fixed Application Support location in
production; tests bind the same implementation to a temporary directory rather
than substituting a different storage protocol.

Source tests construct `BackendPaths(adapter=..., eventkit_bridge=...,
doctor=...)` and inject it into a fresh `McpRuntime` (or through the source-only
stdio harness). This makes subprocess behavior deterministic without mutable
module globals or a production configuration surface. Separate runtimes do not
share initialization, rate-limit history, or lazy Facade instances.

## Behaviors the smaller Interface must preserve

- EventKit reads and primary-field mutations with exact identity and bounds.
- Create idempotency and guarded update/complete/reopen/move/delete.
- Visible URL composition and its final exact read.
- ReminderKit image and section writes with native sync evidence.
- Exact-list section scope and fresh-revision tag assignment.
- Stale-write rejection, one-use References, and unknown-outcome safety.
- Normalized Receipts with verified, unchanged, pending, partial, and failed
  states.

Historical adapter backup, repair, cache, log-purge, direct Core-write, and
UI-handoff routes are not part of the 0.4 runtime contract. They were removed
as a release-blocking cleanup rather than exposed or retained as fallbacks.
