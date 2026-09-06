# v0.7.0 public beta signoff and candidate history

Status: **published immutable public beta; release verification passed**.
The release remains `isPrerelease:true`, not general availability. The
[versioned publication record](public-beta-0.7.0.md) contains the exact tag,
asset digests, successful four-job release run, and independent local verifier
result. Clean-user and capability-specific acceptance remain separate.

The signing and early package evidence below was recorded against assembled
artifact checkpoint `7383736`. Its 1,031-test count and smoke ZIP hash are
historical checkpoint evidence; final release validation supersedes them as
recorded below without changing what those earlier checks tested.

## Verified signing evidence

| Component | Signing run | Authenticated source | Workflow commit |
| --- | --- | --- | --- |
| EventKit | [34057055569](https://github.com/Oscar-V4/apple-reminders/actions/runs/34057055569) | `fea1ba9566ce81ef57123755d9fb5625aa2549ca` | `fce05cbf98bb99dd532d38babbaf484311f72486` |
| Native | [34056591270](https://github.com/Oscar-V4/apple-reminders/actions/runs/34056591270) | `e9dfe50bb191421837a1dd52e2a6bc75d4ee2dc0` | `fce05cbf98bb99dd532d38babbaf484311f72486` |

Both complete app/manifest pairs passed Developer ID signature, notarization,
stapled-ticket, and exact GitHub signing-attestation verification. Each signing
statement contained exactly its helper archive, helper manifest, and
`SHA256SUMS`, with matching digests. Read the
[EventKit manifest](../../plugins/apple-reminders/native/eventkit-helper-build.json)
and [Native manifest](../../plugins/apple-reminders/native/native-helper-build.json)
for the byte inventories and source/build-input fingerprints. Neither artifact
is evidence of private operation compatibility on an unadmitted build.

The production Native resolver passed for all three helper executables: image,
section, and recovery. Resolver/signature success does not establish that their
Reminders operations ran or passed final read-back.

## Candidate validation checkpoint

- Source audit: 75 files passed; plugin validation: five skills passed.
- Full suite: 1,031 tests completed in approximately 127 seconds, with one
  skipped test and an overall successful result. This is the pre-documentation
  checkpoint suite, not a claim that every later revision was retested.
- Installed ZIP smoke: default discovery 15 tools, explicit core-only discovery
  nine tools, and legacy URL-mode discovery 15 tools. The positive public
  packaging diagnosis call passed without Reminders store/schema, permission,
  or Native-runtime probes.
- Smoke ZIP SHA-256:
  `d4ddbeb64cf48ace65f0e4bbfde88c8fb0d8dbdfeade6ab7d6be3ba834c94e04`.
  This identifies the tested candidate ZIP only. Documentation/package changes
  alter deterministic bytes; it is not the published release digest.

No user Reminder titles, identifiers, attachment contents, or other private
content are included in this public record. Private read-only observations do
not replace synthetic Native write acceptance or clean-user testing.

## Final release and remaining acceptance gates

| Gate | Current status | Required next evidence |
| --- | --- | --- |
| Final source/package validation | Passed | 1,035 tests with one skip; PR #56 CI four jobs passed; release source audit and deterministic rebuild verified |
| Bundled Python provenance | Verified | Release workflow and independent local canonical verifier checked both capsules and trusted history |
| Published release authenticity | Verified public beta | Immutable two-asset release, exact tag, shared SLSA, four successful release jobs, and independent local canonical verification |
| Clean-user Core permission and CRUD | External acceptance pending | Fresh allow/deny and upgrade identity; real Intel and minimum macOS 14 execution |
| Native synthetic image acceptance | Passed on admitted arm64 host | [Packaged image flow](v0.7.0-native-image-acceptance.md) with unavailable developer-tool directory; fresh-user and other capability testing remain separate |
| Sections and tags | No current acceptance evidence | Exact reviewed command-schema and operation evidence before claiming support |
| iCloud/iPhone visibility | Separate observation required | Local/native/CloudKit evidence must not be relabeled direct device confirmation |

The release verifier authenticates the Native manifest and expanded app bytes
without executing Native helper operations. A partial or invalid Native pair
blocks release. The generic verifier permits older releases with no Native pair;
the published package includes both complete signed helper pairs. Missing Native availability at runtime still leaves healthy Core
usable.

## Post-publication boundary

The release workflow and separate local canonical verifier passed. Installed
package smoke passed in default, core-only, and legacy URL modes. These results
do not establish that the user's currently running Codex task has reloaded the
new plugin; installation and a new task are separate steps.

Future source/package edits require fresh tests and produce different ZIP bytes.
Keep the published asset digests in the versioned record unchanged. External
acceptance, demo recording, and social announcements remain separate work.

The closed external tester receipt retains the historical
`clt_only_experimental` scenario. It cannot establish Native operation on a
machine without compiler tools. A dedicated no-compiler Native receipt scenario
remains follow-up work; use neither invented enum values nor a historical CLT
receipt as evidence for that new acceptance gate.
