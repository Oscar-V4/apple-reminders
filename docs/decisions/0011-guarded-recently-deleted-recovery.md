# Guard Recently Deleted recovery as a one-item capability

The public Interface may recover one exact Reminder from Apple's Recently
Deleted store, but it must not expose private database revisions, private store
paths, or a reusable broad restore primitive. Recovery is a distinct product
operation rather than an implied property of `delete_reminder`.

## Decision

- A bounded read lists recoverable items retained within 30 days. A list page
  never authorizes a write.
- Only an exact deleted-item read issues a short-lived, one-use opaque `del1`
  Reference. The server keeps the private store identity, Reminder revision,
  deletion timestamp, account identity, and attachment-set digest behind that
  Reference.
- Recovery requires the `del1` Reference, an exact destination `list_id`, and
  an idempotency key. The destination must belong to the same account.
- The adapter re-reads the deleted row and rejects any guard mismatch before
  dispatch. The native helper calls ReminderKit's list-scoped undelete save,
  requests CloudKit sync on the save request, and performs exact local
  read-back.
- `verified` requires the original Reminder identity, exact destination list,
  an active attachment set, an unchanged attachment digest, and a final
  EventKit read. A helper crash, timeout, malformed post-dispatch result, or
  delayed EventKit visibility is `committed_verification_pending`; it is never
  reported as a clean no-write failure.
- A consumed `del1` Reference is not replayable. An identical idempotency key
  replays its cached normalized Receipt instead of dispatching a second save.

## Alternatives considered

EventKit has no public Recently Deleted enumeration or undelete API. UI
automation can operate the visible app, but it cannot provide a stable exact-ID
contract or a machine-verifiable attachment-preservation Receipt. Direct
SQLite mutation was rejected because it would bypass ReminderKit's object graph
and CloudKit save semantics. The selected private ReminderKit path therefore
remains macOS-version-sensitive and fails closed when its schema or selectors
are unavailable.

## Local evidence

The implementation was exercised on macOS 26.5.2 (build 25F84), Reminders 7.0
(build 3976), with recovery command-schema fingerprint
`adaa7c550726b35e592085a531fba649466a6099ec8cbb863bf726143fcf5634`.
Four recently deleted reminders were restored to their original identifiers;
their four image attachments were preserved and then verified in the native
Reminders UI. This evidence proves the observed local build only, not future
macOS compatibility, iCloud convergence, shared-list delivery, or visibility
on another device.

During the exercise, an optional ReminderKit CloudKit trigger invoked with a
null completion block crashed after a successful save. The helper removed that
unsafe call and retains `setSyncToCloudKit:YES` on the save request. Runtime
error mapping is deliberately conservative because a post-save helper crash
can leave the write committed even when no JSON Receipt is emitted.

## Consequences

The Interface grows by two tools and therefore requires a minor version bump.
Deletion remains a separate exact mutation; its success does not promise that
future recovery will be available. Broad restore, cross-account recovery,
permanent deletion, private-path export, and automated retry after an uncertain
Receipt remain unsupported.
