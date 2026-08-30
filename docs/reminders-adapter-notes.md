# Reminders Adapter Notes

These notes record implementation evidence from local macOS Reminders
investigation. They describe the private adapter beneath the public Modules;
they are not a second user-facing command contract.

## Public 0.5 boundary

Core operations use EventKit for access, exact lists/reminders, typed due dates,
absolute, due-anchored relative, and coordinate-backed location alarms, recurrence, completion,
priority, URLs, and list moves. Native Extension operations cover exact
sections, tags, and native image/URL attachments.

Public callers use the 15 tools in `plugins/apple-reminders/schemas/mcp-tools.json`. An exact reminder
read returns one opaque `rev1` Reference; callers do not obtain private versions
from an attachment command or submit `if_version` directly. The Module unwraps
and revalidates the Reference immediately before the selected backend mutation.

The direct `plugins/apple-reminders/scripts/reminders_adapter.py` CLI is an
internal implementation seam containing only the 16 commands reached by the
public Modules. The former direct Core writes, Maintenance, backup, repair,
cache, log-purge, and UI-handoff commands were physically removed before 0.4;
there are no hidden aliases or compatibility routes. Exact Recently Deleted
recovery is exposed only through its guarded `del1` Recovery Module.

## Public API findings

Historical experiments showed that AppleScript can inspect and change common
Reminders fields, but it is app-state dependent. The packaged 0.5 runtime has
no AppleScript mutation or native `show_reminder` path; normal Core writes and
deletion use EventKit.

EventKit is the default public-field path. Native fetch predicates are bounded
by exact list IDs or the matching incomplete-due/completed-completion range;
text and modified-time filters are post-filters and cannot make an otherwise
broad fetch safe. Existing-reminder changes require exact identity and a fresh
precondition, carried publicly inside the opaque Reference.

Normal creation and editing keep `due` and `alarms` separate, reject
timezone-naive timed values, preserve requested relative offsets against an
existing or same-request due anchor, support explicit location-alarm
coordinates, and never infer an alert from a due date.

## Private store surfaces

The Reminders store is normally under:

`~/Library/Group Containers/group.com.apple.reminders/Container_v1/Stores/`

Attachment files are normally under:

`~/Library/Group Containers/group.com.apple.reminders/Container_v1/Files/<Account>/Attachments/`

Observed Core Data entities include `REMCDReminder`, `REMCDBaseList`,
`REMCDBaseSection`, `REMCDHashtagLabel`, `REMCDImageAttachment`,
`REMCDURLAttachment`, and `REMCKCloudState`.

These names are version-sensitive observations, not stable Apple API.

## Image attachment evidence

- Copying an image into the account folder and inserting private rows can make
  it render on the Mac without making it visible on iPhone.
- DB-only rows can lack server-record data, `ZINCLOUD=1`, or a synced local
  version; local rendering is therefore insufficient verification.
- The working path uses private ReminderKit classes through
  `remkit_attach_image.m`, decodes the actual PNG/JPEG type from image bytes,
  saves through the native image-data selector, then reads back the stored UTI
  and CloudKit evidence.
- Helper exit success alone is insufficient. Pre-existing or ambiguous rows are
  rejected, and missing evidence within the bounded window remains partial or
  pending.
- The bounded mobile-visibility window is ten seconds. A live attachment was
  observed to acquire its CloudKit/mobile-visible evidence after roughly seven
  seconds, beyond the former six-second window. If evidence still does not
  converge, the public Receipt remains verification-pending with a structured
  `sync_pending` error and requires a fresh read; it does not retry the
  attachment mutation.
- A native transport mismatch and an image content-type mismatch are distinct,
  non-retryable verification reasons. They are not mislabeled as temporary
  iCloud convergence; cleanup remains manual because the attachment may exist.
- Image replacement creates the new attachment through ReminderKit, then
  removes the exact old attachment through the change item's native
  `removeAttachment:` selector. If removal fails, native compensation is
  attempted and full success is not reported.
- A direct `ZMARKEDFORDELETION=1` update was observed to leave both the old
  1-pixel image and its replacement visible in macOS Reminders even though the
  adapter's active-row query returned one image. It is therefore not accepted
  as UI-removal evidence.
- ReminderKit removal detaches the old attachment from the exact Reminder while
  retaining its object/CloudKit state as a sync tombstone. Verification follows
  that relationship rather than requiring physical row deletion.
- All direct SQLite connections are closed during the native removal save.
  Holding or immediately reopening a read-write connection can delay the
  Core Data relationship update; bounded read-only verification starts only
  after the native save has had a short settle window.
- Native attach retains its bounded read connection but never starts a SQLite
  write transaction around the ReminderKit helper. A live public-MCP
  reproduction measured 30.6 seconds and returned verification-pending with a
  write lock, versus 4.55 seconds and `verified` after removing that lock.
- If native removal times out, loses its result, or cannot complete exact
  relationship read-back, no compensating deletion runs. The receipt requires
  an exact attachment inspection before any retry so a committed old removal
  cannot be followed by deletion of the only remaining image.
- Cross-Reminder copy revalidates independent source and destination public
  guards, acquires fresh private revisions, snapshots the source into a private
  temporary file, and rechecks both Reminders before dispatch. Reminders can
  retain byte-identical content-addressed files under more than one extension;
  they are equivalent only when every candidate matches the stored SHA-512.
- A live source exposed stale `public.png` metadata for bytes decoded as JPEG.
  ReminderKit normalized the destination to `public.jpeg` while preserving the
  exact SHA-512, file size, width, and height. Copy verification therefore uses
  byte identity plus decoded destination-type verification rather than
  requiring stale source and normalized destination UTI strings to match.

The historical DB image experiments remain evidence only. The local-only
attachment repair command, its container backup helper, and automatic repair
restoration were removed from the shipped adapter rather than retained as a
hidden Maintenance surface.

## Recently Deleted recovery evidence

The public Recovery Module lists a bounded 30-day inventory without writable
references. An exact item read keeps store identity, private revision, deletion
timestamp, account identity, byte-verified attachment digest, and an opaque
native ReminderKit snapshot digest server-side and returns one short-lived
`del1`. Recovery rechecks the store guard and the native snapshot immediately
before the save, then uses ReminderKit's list-scoped undelete only for an exact
same-account destination. `verified` additionally requires matched
pre/native/post attachment counts, real backing-byte SHA-512 evidence, and the
final EventKit read.

On macOS 26.5.2 (25F84), Reminders 7.0 (3976), schema fingerprint
`adaa7c550726b35e592085a531fba649466a6099ec8cbb863bf726143fcf5634`,
four exact deleted Reminders returned with their original identifiers and four
preserved image attachments. A null-completion CloudKit trigger crashed after
one successful save, so the helper removed that optional call and retains
`setSyncToCloudKit:YES` on the save request. Missing or malformed post-dispatch
output is consequently an unknown possible write, never a clean no-write
failure. The same pending classification applies to every read-back error after
the helper confirms a save.

## URL attachment evidence

A native add/replace/delete round trip showed that URL replacement removes the
old `ZREMCDOBJECT` row, retains its `ZREMCKCLOUDSTATE` row as a sync tombstone,
and gives the new URL the previous display order. URL deletion likewise retains
and bumps the cloud-state tombstone. Image removal instead uses ReminderKit to
detach the exact attachment while retaining its CloudKit tombstone.

The live evidence was captured on macOS 26.5.2 (25F84), Reminders 7.0, with
schema fingerprint
`82761d59e465cf4c90ca8c98bb51eab498c6976e81d608023535f3bf0ec63d62`.
The private transaction touched only the exact URL object, its existing cloud
state, and the parent reminder version, and rolled back when required cloud
state or read-back was absent. This observation does not claim compatibility
with other builds.

At the product boundary, URL create/change remains one composed operation:
EventKit metadata save, matching visible native URL attachment, then a final
exact EventKit read. The final read supplies the next Reference. An unavailable
final read is pending rather than verified; a failed attachment step preserves
partial-success semantics and idempotent retry does not duplicate objects.

## Section evidence

Direct private section rows can remain local-only. The working path creates
sections and changes same-list membership through `REMSaveRequest` in
`remkit_sections.m`, allowing Reminders to generate native CRDT/CloudKit state.
Verification requires `inCloud=1` and a synced version at least as new as the
current local version; local rendering alone is insufficient.

Section enumeration and mutation are scoped by exact list ID. This prevents
duplicate list names in different accounts from broadening a request. Public
section creation and organization preserve this behavior behind
`create_reminder_section` and `organize_reminder`.

## Tag evidence

Tag labels live in `ZREMCDHASHTAGLABEL`. Assignments are reminder-linked object
rows. Add find-or-creates the label and is idempotent; remove soft-deletes only
the exact assignment. Public add/remove is exposed through a closed
`organize_reminder` action and a freshly revalidated Reference.

Unused-label cleanup is separate from ordinary tag removal. Its historical
digest and locking experiments remain documented, but the cleanup command and
backup machinery were removed from the shipped adapter. Ordinary public tag
removal still soft-deletes only the exact assignment.

## Historical private reminder creation

Earlier experiments inserted reminder rows and cloud state directly. Native
rendering also required ordering data and compressed rich-text documents, and
some reminders still displayed no title until the public Reminders object model
rewrote the text. Timed and all-day date storage also differed.

Those findings explain why normal reminder creation and primary-field editing
use EventKit. The deprecated direct DB/AppleScript create, update, completion,
reopen, and delete commands were removed instead of becoming an internal
fallback.

## Removed maintenance internals

The rebuildable cache, note-body source search, direct backup, attachment
repair, and log-purge commands belonged to the retired 0.2 CLI surface. They and
the standalone recovery-backup helper are no longer packaged. The durable
content-free idempotency journal remains because it protects the retained
mutation paths; it is not a general activity log or restoration mechanism.

## Adapter rules

- Public workflows start with the requested bounded operation, not Doctor.
- After a relevant failure, `diagnose_reminders` may inspect the affected area
  without reading Reminder content. Explicit access requests remain separate.
- Low-level private writes still enforce their own schema/capability gate inside
  the Module; callers do not manually preflight every operation.
- Keep transactions narrow and verify every terminal success by exact read-back.
- Prefer EventKit for public reminder fields and deletion. There is no private
  delete fallback in the public MCP.
- Revalidate the opaque Reference immediately before an existing-item private
  mutation and consume it on a terminal or unknown post-dispatch outcome.
- Treat ReminderKit/store writes as version-sensitive. CloudKit evidence is not
  direct iPhone observation or guaranteed convergence.
- Keep the 16-command adapter allowlist exact; do not add hidden compatibility
  routes or let an internal command silently expand the public contract.
