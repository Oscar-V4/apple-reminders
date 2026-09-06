# Experimental attachment preflight correction

## Reproduction

On macOS 26.5.2 (25F84), Reminders 7.0 (3976), v0.6.1's opt-in MCP
diagnosis reported `helper_syntax_check_failed` and
`schema_fingerprint_mismatch`. These are two independent defects.

The selected Xcode compiler was found, but invoking it without `-isysroot`
could not find `AppKit/AppKit.h`. Supplying the macOS SDK from the same selected
developer directory made the unchanged helper pass the non-linking check.
The correction pairs each supported fixed compiler path with its SDK: Xcode's
`Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk`, or Command Line Tools'
`SDKs/MacOSX.sdk`. Diagnosis and all three private helper builders use the
same command. Missing SDKs fail before building; no PATH fallback is added.

## Schema evidence and scope

The previously recorded attachment fingerprint
`82761d59e465cf4c90ca8c98bb51eab498c6976e81d608023535f3bf0ec63d62`
is the whole diagnostic schema fingerprint. The attachment operation compares
only tables in the `attachment_mutation_db` contract. Comparing these different
projections rejected the already recorded schema.

`tests/fixtures/attachment_schema_25F84.json` captures only Apple table and
column names: no content rows, account identifiers, titles, paths, or attachment
bytes. Its complete schema hashes to the exact previously recorded value.
Projecting that same fixture through both the adapter and Doctor produces
`4536d8d7330f95ab6f1e39dd5f7f04d6970cd52fa5da1c948a51e1d96e2e44e1`.
The attachment allowlist now contains this command-scoped value. The exact
OS/app identity and the recovery fingerprint are unchanged. Unknown builds,
extra attachment columns, and missing required fields remain rejected.

## Verification

The regression suite exercises both failures without a live database. The
native CI check invokes the same selected compiler/SDK command as production,
instead of the system clang shim that had hidden the missing SDK argument.
Local original reproduction now returns `helper_statically_buildable`, and
content-free experimental diagnosis admits the existing attachment build/schema.

Local validation on the recorded build passed 975 tests (one skipped), all
four native-source syntax checks, the manifest/Minis/source audits, and two
byte-identical package builds. Through the source checkout's signed bundled
runtime and public MCP, image creation on an existing Reminder was exercised:
the first call returned pending mobile visibility, then a fresh Core read and
native inspection found the exact image with server record, `in_cloud=1`, and
matching local/synced versions. A subsequent image returned a verified Receipt
with both images present. No retry of the pending mutation was needed. These
are local read-back and CloudKit observations, not direct iPhone UI evidence.

Per-operation attachment read-back and iCloud evidence remain required. This
correction does not make Experimental tools default, admit new macOS builds,
or establish section/tag compatibility.
