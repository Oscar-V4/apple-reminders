# v0.7.0 Unreleased candidate signoff

Status: **signed artifacts and candidate package checks verified; not published**.
This record separates observed candidate results from pending release and
acceptance gates. Evidence below was recorded by the coordinating release task
against the assembled artifact checkpoint `7383736`, before this documentation
revision. It does not claim that a tag or immutable release already exists.

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
  alter deterministic bytes; it is not the eventual published release digest.

No user Reminder titles, identifiers, attachment contents, or other private
content are included in this public record. Private read-only observations do
not replace synthetic Native write acceptance or clean-user testing.

## Remaining release and acceptance gates

| Gate | Current status | Required next evidence |
| --- | --- | --- |
| Final candidate source/package validation | Repeat for final tag inputs | Full suite, source audit, mirrors/claims, deterministic rebuild and exact signed-component inventory after final changes |
| Bundled Python provenance | Reverify against final candidate tag | Both architecture capsules and trusted source/workflow history |
| Published release authenticity | Pending publication | Exact tag on canonical main history, immutable two-asset release, shared release SLSA statement, canonical verifier and final read-only job |
| Clean-user Core permission and CRUD | External acceptance pending | Fresh allow/deny and upgrade identity; real Intel and minimum macOS 14 execution |
| Native synthetic image acceptance | Passed on admitted arm64 host | [Packaged image flow](v0.7.0-native-image-acceptance.md) with unavailable developer-tool directory; fresh-user and other capability testing remain separate |
| Sections and tags | No current acceptance evidence | Exact reviewed command-schema and operation evidence before claiming support |
| iCloud/iPhone visibility | Separate observation required | Local/native/CloudKit evidence must not be relabeled direct device confirmation |

The release verifier authenticates the Native manifest and expanded app bytes
without executing Native helper operations. A partial or invalid Native pair
blocks release. The generic verifier permits older releases with no Native pair;
this candidate's final inventory must explicitly include both complete signed
helper pairs. Missing Native availability at runtime still leaves healthy Core
usable.

## Remaining signoff procedure

1. Preserve the authenticated source/workflow commits and signed bytes in the
   final release history. Record any subsequent synthetic acceptance separately.
2. Rerun final source/package checks after documentation changes. Keep metadata-only
   Core URL behavior and explicit attachment-card actions distinct.
3. Publish only the reviewed immutable candidate tag, then run
   `python3 scripts/verify_release_assets.py v0.7.0` from a clean tag checkout.
4. Update publication status only after the canonical verifier and final workflow
   succeed. External testing and social announcements remain separate actions.

The closed external tester receipt retains the historical
`clt_only_experimental` scenario. It cannot establish Native operation on a
machine without compiler tools. A dedicated no-compiler Native receipt scenario
remains follow-up work; use neither invented enum values nor a historical CLT
receipt as evidence for that new acceptance gate.
