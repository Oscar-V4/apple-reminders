# 0.8.0 Calendar Early Reminder candidate acceptance

Status: signed candidate and maintainer-host acceptance verified. This record
is not a claim that a tagged public release has been published.

## Source and signed components

- Feature source: `6e828ddb5224b5d025395c0c08192db4ce1ec4e6`.
- Installation/documentation follow-up: `1440aa0435537c127a4920348c7fc6a5a730a588`.
- Both signing workflows were the existing main-owned definitions at
  `42c98bb804d68578130e1c5a915cf04b7c93f681`; no signing gate was weakened.
- Native signing and attestation: [run 34133555132](https://github.com/Oscar-V4/apple-reminders/actions/runs/34133555132), using the feature source.
- EventKit signing, assembled-source tests and attestation:
  [run 34134496344](https://github.com/Oscar-V4/apple-reminders/actions/runs/34134496344), using the documentation follow-up source.
- Exact workflow/main identity, closed three-subject attestations, all downloaded
  subject hashes, source fingerprints, Developer ID signatures, Hardened
  Runtime, notarization and stapled tickets passed local verification.

The first EventKit run signed successfully but failed seven version-copy
assertions because the README still selected 0.7.1. The follow-up source fixed
those assertions; the successful run above is the accepted EventKit evidence.

## Data-free checks

- Full assembled-tree suite: **1,106 tests run, one skipped, no failures**
  (152.937 seconds on the maintainer Mac).
- Native calendar arithmetic: 11 cases, including non-leap/leap month ends,
  US and European DST, a spring gap and an autumn fold. The native interval
  remains a calendar month; no 30-day or seconds approximation is used.
- Plugin, Minis and both changed skill validators passed.
- Source audit: 75 allowlisted files. Generated Python bytecode from local
  test subprocesses was removed; source audit then passed with the strict
  worktree and document-mirror checks.
- Two deterministic source builds were byte-identical.
- Default, core-only and legacy URL-mode extracted-package startups exposed
  15, 9 and 15 tools respectively; a public packaging diagnosis passed.

Tested ZIP: `apple-reminders-0.8.0.zip`, 55,075,924 bytes, SHA-256
`ba680baf37b6317b4f46d4f7797c0bc5290ddff5cb29f4077562e5c34bbe0b3c`.
This identifies the tested candidate, not an independently published release.

## Realistic user-task acceptance

Environment: arm64 Mac, macOS 26.5.2 (25F84), Reminders 7.0 (3976), existing
Reminders permission. Both journeys used the extracted ZIP's normal bundled
launcher, default mode, signed Native helper and fresh MCP processes without
the contributor source-build opt-in.

Natural request: create a yearly renewal with a note and medium priority;
remind one calendar month early; move its deadline across leap/non-leap month
ends, preserve the calendar interval, remove an obsolete ordinary relative
alarm, clear/reapply Early Reminder and reject a reused revision.

Two independent fixtures passed: one all-day and one floating 09:30 wall-clock
Reminder. Each preserved due mode, note, priority and yearly recurrence across
meaningful writes. Native inspection consistently returned month/value 1,
and ordinary alarm removal left that setting intact. Clear returned zero
Early Reminders, stale-reference reuse failed before mutation, and setting the
same month value returned unchanged. Mac UI inspection showed the matching
synthetic title, yearly recurrence and one month before for each fixture.
Exact guarded deletion and subsequent absence reads verified cleanup.

The initial diagnostic fixture also demonstrated the original symptom: a
verified ordinary relative alarm with Early Reminder unset. After native
setting, both the explicit native fetch and Mac UI showed one month before.
It was deleted after validation.

## Recorded limitation and interruption case

An earlier composed run attempted to convert an all-day Reminder to an
explicit America/Los_Angeles zoned time. Existing Core/EventKit readback became
a floating device-local time, so Core correctly returned
`committed_verification_pending`. A fresh read established that result and
proved the native month interval survived. That fixture was deleted with
verified absence; the mutation was not blindly replayed or reported successful.
This existing Core timezone-conversion limitation is not fixed or bypassed by
the Early Reminder feature. The later all-day and floating-time journeys are
separate successful runs, not a relabeling of the failed zoned conversion.

All four synthetic fixtures created during diagnosis/acceptance were cleaned
up; Recently Deleted was not emptied. Private IDs, names, raw receipts and UI
captures remain outside Git. Local/Mac UI evidence does not establish future
notification delivery, iCloud convergence or direct iPhone visibility. Native
admission remains restricted to the exact recorded build/schema.
