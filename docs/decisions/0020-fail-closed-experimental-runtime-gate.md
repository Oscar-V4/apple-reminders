# Fail closed before Experimental Internals dispatch

## Status

Accepted for the next release after issue #41. This decision does not promote,
prebuild, sign, notarize, or distribute any private helper.

## Context

Stable Core uses documented EventKit through the reviewed bundled helper. The
Experimental Internals cover private Reminders SQLite writes and locally
compiled ReminderKit helpers. Before this decision, the adapter required known
tables/columns and helper classes/selectors but did not use the detected macOS
build, Reminders build, or emitted schema fingerprint as a positive admission
control. A future build that retained those names could therefore reach a
private write even though its semantics had never been validated.

Apple's documented EventKit surface supports reminder calendars/lists, reminder
creation, reads, saves and removal, and ordinary calendar-item fields. See
[`EKEventStore`](https://developer.apple.com/documentation/eventkit/ekeventstore),
[`EKReminder`](https://developer.apple.com/documentation/eventkit/ekreminder),
and
[`EKCalendarItem`](https://developer.apple.com/documentation/eventkit/ekcalendaritem).
It does not document the section, hashtag assignment, native image attachment,
or Recently Deleted contracts implemented by this plugin's private paths.

## Decision

1. Stable Core is a separate support tier and never initializes the
   Experimental compatibility gate. Non-URL EventKit work remains usable when
   every private capability is blocked.
2. Every Experimental command resolves to one immutable capability
   specification. The specification records whether compilation is required,
   conditional, or unnecessary and which command-specific schema fingerprint
   applies.
3. Before opening the Reminders store or dispatching a private mutation, the
   adapter requires an exact four-part identity: macOS version, macOS build,
   Reminders version, and Reminders build. Missing identity is
   `runtime_unverified`; a complete identity absent from the allowlist is
   `unsupported_build`.
4. Helper-backed commands also require a selected compiler before the store is
   opened. Resolution uses fixed `/usr/bin/xcode-select -p`, rejects developer
   environment overrides, and accepts only fixed compiler-relative paths under
   that selected directory. It never trusts `PATH` or the `/usr/bin/clang`
   installer shim. Command Line Tools are a dependency, never compatibility
   evidence.
5. An admitted build receives a read-only command-schema check. Both the
   minimum field contract and exact fingerprint must match. Mutating functions
   repeat the minimum schema check at their transaction boundary.
6. There is no environment-variable, user preference, or automatic fallback
   that bypasses the allowlist. Adding evidence requires a reviewed source
   change with exact build, schema, semantic mutation, and read-back evidence.
7. Preflight failures use `failed_no_mutation`,
   `verification.write_performed=false`, public code
   `unsupported_capability` or `schema_mismatch`, and one precise reason:
   `runtime_unverified`, `unsupported_build`, `compiler_required`, or
   `schema_unverified`/`schema_fingerprint_mismatch`.
8. Passing preflight does not make a private interface Stable. The capability
   remains `experimental_internals` and `runtime_unverified` until the exact
   operation's existing read-back contract finishes.

## Initial evidence matrix

| Capability | Compiler | Exact admitted evidence | Initial state |
| --- | --- | --- | --- |
| Image attachment mutation | Required | macOS 26.5.2 / 25F84, Reminders 7.0 / 3976, attachment schema `82761d…d62` | Experimental; per-operation read-back still required |
| Native URL attachment mutation | Not required | same build, attachment schema `82761d…d62` | Experimental; Core URL composition may still be partial |
| Recently Deleted inventory | Not required | same build, recovery schema `adaa7c…634` | Experimental read-only inventory |
| Exact Recently Deleted inspection/recovery | Required | same build, recovery schema `adaa7c…634` | Experimental; exact guard/read-back required |
| Section create/move | Required | No exact command-schema evidence recorded | Disabled (`runtime_unverified`) |
| Tag assignment add/remove | Not required | No exact command-schema evidence recorded | Disabled (`runtime_unverified`) |
| Legacy direct-SQL image write | Not required | Not a supported public mutation | Disabled (`runtime_unverified`) |

The full immutable values live in
`plugins/apple-reminders/scripts/experimental_capabilities.py`. A minimum-version
range, compiler success, selector presence, or a similar-looking schema must
not be substituted for an exact row.

The recovery row is grounded in the exact build, fingerprint, guarded restores,
and native attachment read-back recorded in
[ADR 0011](0011-guarded-recently-deleted-recovery.md#local-evidence). The
attachment row is grounded in the exact URL transaction evidence in
[the adapter notes](../reminders-adapter-notes.md#url-attachment-evidence) and
the byte-preserving image-copy exercise in
[ADR 0012](0012-private-byte-copy-with-dual-guards.md#local-evidence). Those
records remain local-build evidence, not future compatibility promises.

## App Intents and Shortcuts review

App Intents does not expose another app's private data model. Apple's current
documentation describes it as a way for an app to publish **that app's own**
actions and entities to Siri, Spotlight, and Shortcuts. Adding App Intents here
would therefore wrap this plugin's existing Core or Experimental operation; it
would not create a documented Reminders section, image, or undelete API. It
would also require a separately shipped app/extension surface, so this decision
does not add it as a second integration.

Apple's documented built-in Shortcuts actions are useful for one narrower goal:
Apple records tag support in **Add New Reminder**, **Find Reminders**, and
**Edit Reminder**. A user-authored Shortcut is therefore an honest manual
alternative for tag workflows. It is not silently invoked by this plugin and
does not justify the private tag SQLite path. The reviewed Apple sources are
[App Intents](https://developer.apple.com/documentation/appintents),
[App intents](https://developer.apple.com/documentation/AppIntents/app-intents),
and
[Shortcuts tag support](https://support.apple.com/en-us/106430).

## Documented and honest alternatives

| User goal | Core-safe default | Explicit Experimental or manual option |
| --- | --- | --- |
| Group reminders | Use separate Reminder Lists, or a user-approved textual prefix/heading in title or notes. | Use sections only after targeted capability diagnosis reports the exact build/schema admission. |
| Label reminders | Preserve a plain-text label in title or notes; a user-authored Shortcut may use Apple's documented tag-capable Reminders actions. | Use native tag assignment only with explicit intent and an admitted capability. |
| Preserve an image | Store a user-provided remote reference in notes, or summarize the local image in notes without syncing its private path. | Attach a native image only when explicitly requested and the exact helper capability is admitted. |
| Preserve a link | Put a contextual link in notes for a Core-only path. | The plugin's `url` field is currently hybrid EventKit plus private native attachment; use it only when that behavior is intended and admitted. |
| Avoid losing a reminder | Move it to a dedicated archive list through EventKit before destructive cleanup. | Use Reminders UI Recently Deleted manually, or exact private recovery only on an admitted build. |

## Rejected alternatives

- **Minimum OS ranges:** private schemas and selectors can change within a
  nominal product version.
- **Schema-only or selector-only gates:** structural similarity is not semantic
  compatibility evidence.
- **A warning with opt-in:** a prompt or environment variable would turn a
  safety invariant into caller policy and allow unknown builds to mutate.
- **Automatic fallback:** SQLite, AppleScript, UI automation, or another private
  helper would hide the unsupported capability instead of failing closed.
- **Prebuilding/signing/notarizing private helpers:** this is outside the issue
  #41 policy boundary and would not establish private-API compatibility or
  Apple approval.

## Read-only Experimental decision

Bounded section, tag-label, and attachment-metadata inspection remains available
as Experimental read-only context. Those reads issue no mutation authority,
expose no private file path, and are still useful for exact disambiguation. A
successful read must not be presented as write compatibility. Broad maintenance,
raw export, unused-label cleanup, repair, and backup inspection remain withheld.

Recently Deleted is stricter: even list inventory is bound to the admitted
recovery schema, and exact item inspection additionally requires the compiler
and helper because it verifies bytes/native state and can issue `del1` recovery
authority. This keeps a schema-shaped unknown store from becoming the first step
of a private recovery flow.

## Consequences

Experimental features can be unavailable even when compilation and static
schema checks pass. That is deliberate. Stable Core remains independently
usable, capability diagnosis tells callers exactly why a path is blocked, and
new platform support cannot appear accidentally after a macOS or Reminders
update.
