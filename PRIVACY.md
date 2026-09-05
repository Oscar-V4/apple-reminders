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

- `.mcp.json` launches a local shell shim that verifies the packaged Python
  capsule and signed runtime, then starts `mcp/server.py` as a local stdio
  subprocess. It does not search for an external Python, download runtime code,
  or source user shell startup files. The bundled launcher removes inherited
  Python configuration before starting the packaged interpreter.
- The MCP server invokes bundled local adapters; packaged runtime ignores
  backend path overrides and does not call a plugin-owned web service.
- EventKit and private ReminderKit operations communicate with macOS system
  services or the Reminders app.
- Apple Reminders and iCloud may transmit and sync data according to the user's
  Apple account and system settings.
- URLs attached to reminders are user-provided data. The plugin should not
  fetch their contents unless a separate, explicit user request authorizes it.

## Local Data Written by the Plugin

Depending on the commands used, operational files may be created under:

- `~/Library/Application Support/apple-reminders-codex/`
  - redacted action journal and its rotation;
  - idempotency metadata and lock;
  - temporary or legacy version/schema capability records.
- `~/Library/Caches/apple-reminders-codex/`
  - verified Python runtime copies under `python-runtime/`, with extraction
    coordination metadata; these contain runtime code rather than Reminder data;
  - the three locally compiled advanced ReminderKit helpers for section writes,
    image-attachment changes, and exact Recently Deleted inspection or
    recovery, plus their build locks;
  - legacy or explicit contributor EventKit builds.

The current runtime does not create a reminder metadata cache or backup archive.
Earlier development versions may have left either artifact in these folders or
in an explicitly selected output directory. A legacy metadata cache can contain
reminder identifiers, titles, list/section names, dates, status, and counts;
treat it as private user data even though it omitted full notes and image bytes.

The action journal is designed to redact sensitive values into hashes, sizes,
and counts, while retaining operation metadata and selected identifiers needed
for auditability. Hashes and identifiers can still be sensitive and should not
be published. Current code applies bounded size and retention controls; old
files produced by earlier versions should not be assumed to have the same
format.

The idempotency store is designed to retain only hashed request identity,
operation identifiers/status, exact target identifiers, counts, capability
flags/types, warning codes, and bounded verification proof. It does not retain
raw titles, notes, URLs, recurrence values, warning prose, or raw user-authored
primitive arrays. Redispatch-safe records age out after 30 days. Unresolved
fences do not age out because deleting one could authorize a duplicate write;
they remain private local metadata until the outcome is resolved or the store
is removed.
The next keyed operation re-sanitizes complete modern and legacy v1 results
under the process lock and atomically rewrites them when needed. If that scrub
cannot be persisted, an existing-key replay remains redacted in memory, carries
a bounded warning, and never redispatches the callback. Treat the store itself
as private because hashes and stable identifiers remain linkable metadata.

Legacy backups are the most sensitive artifact. A targeted SQLite backup can
contain all reminder content in one store; a container archive can additionally
cover multiple stores and attachment material. The current runtime neither
creates nor prunes them, so a file left by an earlier build remains until the
user removes it. Do not attach either form to bug reports or treat it as a
guaranteed recovery point without independent verification.

## Public and Private macOS Interfaces

The packaged runtime prefers public EventKit for supported reminder fields.
Advanced features can use:

- Apple's private ReminderKit framework for native image attachments, sections,
  and exact Recently Deleted recovery;
- direct, schema-gated access to the private Reminders SQLite store for fields
  not exposed by public APIs, including tag assignments and native URL
  attachments.

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

The deterministic package policy rejects user databases, journals, caches,
`.DS_Store`, screenshots/UI captures, backup archives, unexpected symlinks,
empty stubs, and unapproved images. The exact reviewed Python runtime capsules
are a deliberate exception for distribution archives and their upstream
bytecode; their complete file inventories, digests and provenance are checked.
Other rejected screenshots, archives, backups and data stores are classified by
path only; validators do not inspect their contents.

Before sharing logs or command output, remove reminder content, account/list
names, identifiers, URLs, local paths, hashes, schema fingerprints, and any
other data that could identify the user or their task history.

## User Control

- Review exact tool arguments and target IDs before mutations.
- Use bounded previews for destructive or bulk operations.
- The public Interface does not expose log purge. After removing or stopping
  the plugin and confirming that no operation is running, users may remove only
  the plugin-owned support files described in this document.
- Delete cached Python runtimes, compiled helpers, capability records, idempotency metadata, legacy
  metadata caches, or legacy/explicit-path backups when they are no longer
  needed and no operation is running. The current runtime does not manage the
  retention of backup files produced by older builds.
- Revoke Reminders access in macOS settings to disable the associated public
  integration path. The current runtime does not use macOS Automation or Apple
  Events.

Removing plugin-owned files does not undo changes already made to Apple
Reminders or synced through iCloud.
