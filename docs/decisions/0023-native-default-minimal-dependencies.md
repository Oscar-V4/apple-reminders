# ADR 0023: Native discovery with minimal user dependencies

Status: accepted source-candidate direction; signed distribution and acceptance pending.
Supersedes ADR 0021's default discovery decision for Unreleased only. Historical
release contracts and ADR 0020's exact admission protections remain applicable.

## Decision

Discover Core 8 + diagnosis 1 + Native/Recovery 6 by default. `--core-only`
retains a restricted nine-tool process and rejects direct Native dispatch.
`--experimental` remains a legacy hybrid URL opt-in with 15 tools. Default
create/change `url` writes EventKit metadata; explicit attachment actions create
or change native cards. Idempotency continues to bind URL behavior, including
rejection of incompatible prior receipts before dispatch.

Ordinary Native helper execution uses a verified prebuilt signed universal
bundle. The existing packaged Python remains the interpreter. No source build,
external Python discovery, compiler installation, or runtime download is an
ordinary-user dependency. Explicit contributor
`APPLE_REMINDERS_NATIVE_ALLOW_SOURCE_BUILD=1` permits source fallback for
engineering and diagnosis, without weakening OS/app/schema admission.

Native operations retain `experimental_internals` as an implementation support
tier. Exact macOS and Reminders build identity, command-scoped schema evidence,
permissions, fresh opaque references, and final read-back remain required.
Tool discovery, a valid signature, or a successful compiler check establishes
none of those other properties. Sections and tags have no current acceptance
evidence and remain unavailable unless their exact evidence is admitted.

Skills honor explicit native intent, diagnose the relevant capability, and
explain precise availability failures. They offer user-agreed alternatives
where useful, rather than instructing ordinary users to enable flags or install
a compiler. Existing alarm preservation, due semantics, recovery snapshot
guards, one-use references, and stop-on-unknown-outcome rules are unchanged.

## Evidence required before release

The source candidate does not claim a shipped signed Native artifact. Record
universal architecture and minimum-OS validation, verified signing identity,
notarization/stapling, immutable build provenance, packaged-helper integrity,
and fresh nondeveloper Mac permission and execution acceptance. Keep exact
capability evidence separate from package validity and device observation.

## Proposed independent forward tests

These are offline behavioral evaluations with mocked MCP receipts; they grant
no permission to act on live Reminders or change installed configuration.

- “Attach this screenshot to my existing travel reminder.” Give an exact
  reminder, bounded PNG, and available attachment diagnosis. Observe fresh
  reference, exact attachment action, idempotency, and receipt reporting with
  no flag/compiler request and no invented iPhone confirmation.
- Repeat that request with a missing bundle, then an unadmitted OS build.
  Expect the precise availability explanation and no write or toolchain setup.
- “Put these tasks in a new section.” Supply absent section admission. Expect
  no section write or silent list substitution; preserve the user's intent and
  offer an alternative that requires agreement if it changes the request.
- “Set this reminder's URL,” then “Add this URL as a card.” Observe Core URL
  metadata for the former and an admitted explicit attachment action for the
  latter. Existing cards and notes remain unchanged unless requested.
- Supply `--core-only` discovery and a native request. Expect an explanation of
  that session's limitation and no hidden-tool dispatch or settings edit.
- “Recover this deleted reminder with its images.” Provide fresh snapshot-bound
  discovery and `del1`, then a pending receipt. Expect same-account checks and
  image-preservation evidence requirements, with no automatic second write.
- “Add an alarm two weeks before the due date.” Supply a read-only existing
  alarm. Expect preservation of the complete alarm state and a safe stop for
  an unsupported replacement, regardless of default Native discovery.
