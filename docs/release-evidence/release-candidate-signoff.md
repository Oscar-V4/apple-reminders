# v0.7.0 Unreleased candidate signoff

Status: **not published; new EventKit and Native signed artifacts pending**.
This is a release checklist, not a receipt that any pending action succeeded.
The source version is v0.7.0. Read the eventual manifests for exact source and
workflow identities; do not copy earlier v0.6.1 helper evidence into this release.

## Required release gates

| Gate | Status at this documentation checkpoint | Required evidence |
| --- | --- | --- |
| Matching signed EventKit artifact | Pending | v0.7.0 manifest, signed/notarized app, source/workflow provenance, exact bundled bytes |
| Matching signed universal Native artifact | Pending | Complete app/manifest pair, three-source and six-build-input fingerprints, signatures, notarization/stapling, exact three-subject signing attestation |
| Bundled Python provenance | Reverification required for candidate tag | Both architecture capsules verified against trusted source/workflow history |
| Candidate source/package validation | Must run after artifact assembly | Plugin/source audit, document mirrors, complete tests, deterministic package rebuild |
| Published release authenticity | Pending publication | Exact tag on canonical main history, immutable two-asset release, shared SLSA statement, canonical release verifier and final read-only job |
| Normal packaged startup | Acceptance pending | 15-tool discovery without external Python/compiler, explicit nine-tool core-only rejection, scoped packaging call |
| Clean-user Core permission and CRUD | External acceptance pending | Fresh allow/deny and upgrade identity; real Intel and minimum macOS 14 execution |
| Native operation compatibility | Per-capability acceptance required | Exact OS/app/schema admission plus final native/EventKit read-back; no compiler installation |
| Sections and tags | No current acceptance evidence | Exact reviewed command-schema and operation evidence before claiming support |
| iCloud/iPhone visibility | Separate observation required | Local/native/CloudKit evidence must not be relabeled direct device confirmation |

The release verifier authenticates the Native manifest and the expanded app
bytes without executing Native helper operations. Its signature/provenance
success does not establish Reminders permission or capability compatibility.
A complete valid optional Native pair is accepted; a partial or invalid pair
blocks release. Missing Native availability at runtime still leaves healthy Core
usable.

## Release signoff procedure

1. Assemble matching helper artifacts from the reviewed signing workflows; keep
   credentials out of source and receipts. Record exact manifests and workflow
   evidence before changing pending status.
2. Run source/package tests after assembly, then review the dependency/capability
   matrix against the actual admitted evidence. Preserve the metadata-only Core
   URL default and explicit attachment-card boundary.
3. Publish only the reviewed immutable candidate tag and verify it with
   `python3 scripts/verify_release_assets.py v0.7.0` from a clean tag checkout.
4. Update this record only with observed results. External testing and social
   announcement remain separate actions; examples in docs authorize neither.

The current closed external tester receipt has a historical
`clt_only_experimental` scenario. That scenario cannot establish v0.7.0 Native
operation on a machine without compiler tools. A dedicated no-compiler Native
receipt scenario remains follow-up work; do not submit invented enum values or
claim that a historical CLT receipt satisfies this new acceptance gate.
