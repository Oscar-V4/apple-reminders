# Privacy

Apple Reminders is an experimental local macOS Codex plugin. Its bundled MCP
server uses stdio and has no plugin-owned remote endpoint, but tool results are
returned to the Codex host process. How Codex processes or transmits that
context is governed by the host product, account configuration, and applicable
privacy terms; “local MCP” does not mean reminder content necessarily remains
outside the Codex service boundary.

## Data the Plugin Can Access

Depending on the requested tool and available capability, the plugin can read:

- reminder titles, notes, identifiers, due/alert/completion dates, priority,
  flags, recurrence, URLs, and completion state;
- account, list, section, tag, and participant-related metadata;
- image/URL attachment metadata and, when explicitly needed for attachment
  work, local source files;
- bounded Recently Deleted metadata and exact attachment metadata needed for
  one-item recovery within Apple's retention window;
- Reminders store schema, filesystem metadata, and sync-related metadata used
  for capability and verification checks.

Write tools can create, modify, move, complete, reopen, delete, recover, tag,
or attach/copy content to reminders. Cross-Reminder copy reads one exact local
backing file internally but does not return its private path. Changes can sync
through Apple services and can affect other devices or shared-list participants.

## Execution and Network Boundaries

- `.mcp.json` launches a local shell shim that selects a supported Python from
  the process `PATH` or fixed local install paths, then replaces itself with
  `mcp/server.py` as a local stdio subprocess. The shim does not deliberately
  source user shell startup files.
- The MCP server invokes bundled local adapters; packaged runtime ignores
  backend path overrides and does not call a plugin-owned web service.
- EventKit and AppleScript operations communicate with macOS system services or
  the Reminders app.
- Apple Reminders and iCloud may transmit and sync data according to the user's
  Apple account and system settings.
- URLs attached to reminders are user-provided data. The plugin should not
  fetch their contents unless a separate, explicit user request authorizes it.

## Local Data Written by the Plugin

Depending on the commands used, operational files may be created under:

- `~/Library/Application Support/apple-reminders-codex/`
  - redacted action journal and its rotation;
  - idempotency metadata and lock;
  - version/schema capability records;
  - optional backup archives.
- `~/Library/Caches/apple-reminders-codex/`
  - disposable reminder metadata cache;
  - locally compiled EventKit/ReminderKit helpers and build locks.

The cache can contain reminder identifiers, titles, list/section names, dates,
status, counts, and other lightweight metadata. It intentionally omits full
note bodies and image contents, but it is still private user data.

The action journal is designed to redact sensitive values into hashes, sizes,
and counts, while retaining operation metadata and selected identifiers needed
for auditability. Hashes and identifiers can still be sensitive and should not
be published. Current code applies bounded size and retention controls; old
files produced by earlier versions should not be assumed to have the same
format.

Backups are the most sensitive artifact. A targeted SQLite backup can contain
all reminder content in one store; a container archive can additionally cover
multiple stores and attachment material. They remain local until the user
moves or uploads them. Plugin-managed database backups retain at most five or
100 MB, and plugin-managed container archives retain at most two or 300 MB;
older strictly named plugin backups are removed after a new successful backup.
An explicit user-selected output path is never auto-pruned. Container archives
are only best-effort snapshots of a live store, while targeted databases use
SQLite's online backup operation. Do not attach either form to bug reports or
treat it as a guaranteed recovery point without independent verification.

## Public and Private macOS Interfaces

The plugin prefers public EventKit or AppleScript behavior for supported
reminder fields. Advanced features can use:

- Apple's private ReminderKit framework for native image attachments, sections,
  and exact Recently Deleted recovery;
- direct, schema-gated access to the private Reminders SQLite store for fields
  not exposed by public APIs.

These private interfaces are undocumented and version-sensitive. They are not
suitable for App Store application code and may fail or change semantics after
a macOS/Reminders update. Passing a static schema or compiler check does not
prove a write is safe. Unknown or failed capability checks must block the
affected operation instead of falling back silently.

## Verification Limits

Result words are deliberately scoped:

- `verified` means the response's stated local verification evidence passed;
- `committed_verification_pending` and `partial_success` are not full success;
- mobile-sync or CloudKit evidence does not confirm display on an iPhone;
- a backup existing does not confirm that it is complete or restorable.

The plugin must not claim iCloud convergence, device visibility, participant
delivery, or recovery success without direct evidence for that specific claim.

## Diagnostics Privacy Contract

Failure-triggered diagnostics are designed to inspect platform/application metadata,
toolchain paths, directory access, database schema, permission symptoms, and
artifact metadata only. It reports that it does not read reminder rows,
titles, notes, list/section/tag names, journal/cache/backup contents; it does not
write, launch Reminders, load private frameworks, or trigger a permission
prompt. Its unit tests and CI checks use synthetic fixtures and static source
checks rather than the user's live Reminders data.

## Source and Diagnostic Hygiene

The deterministic source-package policy rejects databases, journals, caches,
bytecode, `.DS_Store`, screenshots/UI captures, archives, backups, symlinks,
empty stubs, and unapproved images. Rejected screenshots, archives, backups,
and data stores are classified by path only; validators do not inspect their
contents.

Before sharing logs or command output, remove reminder content, account/list
names, identifiers, URLs, local paths, hashes, schema fingerprints, and any
other data that could identify the user or their task history.

## User Control

- Review exact tool arguments and target IDs before mutations.
- Use bounded previews for destructive or bulk operations.
- The public Interface does not expose log purge. After removing or stopping
  the plugin and confirming that no operation is running, users may remove only
  the plugin-owned support files described in this document.
- Delete disposable caches, compiled helpers, capability records, idempotency
  metadata, or explicit-path backups when they are no longer needed and no
  operation is running. Plugin-managed backups also follow the bounded
  retention policy described above.
- Revoke Reminders or Automation access in macOS settings to disable the
  associated public integration path.

Removing plugin-owned files does not undo changes already made to Apple
Reminders or synced through iCloud.
