# v0.7.1 Unreleased patch candidate signoff

Status: **code integration, fresh signed artifacts, and local package acceptance
complete; final CI and immutable publication pending**. The latest published
public beta remains [v0.7.0](https://github.com/Oscar-V4/apple-reminders/releases/tag/v0.7.0).
Its [immutable publication record](public-beta-0.7.0.md) and asset hashes remain
historical evidence for that release only.

## Bounded patch scope

- PR #57: expose the exact verified image attachment ID after final matching;
  retain unknown targets for pending/unmatched receipts and require the same
  verification on replay.
- Diagnostics changes `83ab407` and `09fed706`: distinguish missing framework
  paths from access/metadata errors. Use the fixed explanation only for the
  complete otherwise-admitted metadata-only case with a verified signed helper
  and one inconclusive warning. Keep degraded/attention status and every blocker.
- Repository-only maintainer preflight from `385c01`/`bd0df3f`:
  `scripts/prepare_helper_signing.py` checks exact branch/source/main identity
  and signing inputs, produces canonical refs for the two existing workflows,
  and requires a reviewed plan digest for explicit dispatch. It is not an
  installed user tool and changes no helper workflow input or capability admission.
- No new Native capability admission, helper `.m` behavior, component identity
  migration, or signing-workflow rewrite is included.

PRs #57, #59, and #60 are merged. Both helper artifacts use the frozen source
`5dd71825aea8174d84f446f2bb2d70bfc44fe5da` and signing workflow commit
`0c474fbb9369c3b1f93c7710ae45a6d41115286e`.

## Verified signing evidence

| Component | Successful signing run | Local verification |
| --- | --- | --- |
| EventKit | [34062821857](https://github.com/Oscar-V4/apple-reminders/actions/runs/34062821857) | Manifest and ZIP attestations, Developer ID signature, notarization/stapling, exact imported bytes |
| Native | [34062823456](https://github.com/Oscar-V4/apple-reminders/actions/runs/34062823456) | Manifest and ZIP attestations, Developer ID signature, notarization/stapling, exact imported bytes |

Each authenticated SLSA statement contained exactly its helper ZIP, helper
manifest, and `SHA256SUMS`, with all three digests matched. The
[EventKit manifest](../../plugins/apple-reminders/native/eventkit-helper-build.json)
and [Native manifest](../../plugins/apple-reminders/native/native-helper-build.json)
now declare v0.7.1 and match the imported signed app bytes. The prior intentional
artifact/version drift is resolved; no audit or version check was weakened.

## Local package checkpoint

Results recorded by the coordinating release task after artifact assembly:

- Full suite: 1,070 tests in 141.850 seconds, overall success with one skipped test.
- Source audit: 75 files passed. Plugin validation: five skills passed. Public
  claim checks passed.
- Actual extracted ZIP smoke: default 15 tools, core-only nine tools, legacy URL
  mode 15 tools, and a positive public packaging diagnosis call all passed.
- Tested ZIP SHA-256:
  `99432881f6a85fbf5cd02676dd4eeca0a7292b7c8cb1aa0411bf3e5c6680774b`.
  This is the tested local checkpoint, not a published release asset. Later
  documentation edits change deterministic ZIP bytes and require final validation.
- [Synthetic image acceptance](native-image-acceptance-0.7.1.md) passed through
  the extracted ZIP's default MCP, including exact attachment target identity,
  Core field preservation, CloudKit sync evidence, diagnosis explanation, and
  verified cleanup.

## Release gates

| Gate | Current status | Remaining evidence |
| --- | --- | --- |
| Source identity and code integration | Complete | Preserve the reviewed source/workflow ancestry and unchanged signed inputs |
| Both v0.7.1 signed helper pairs | Verified | Preserve exact app/manifest bytes in the final package |
| Local suite, audit, and package smoke | Passed at the recorded checkpoint | Final checks after documentation changes and current-head CI |
| Synthetic Native image flow | Passed on one admitted arm64 host | Other hosts/capabilities and fresh-user acceptance remain separate |
| Final CI | Pending | Successful jobs for the exact final PR/merge inputs |
| Immutable publication | Pending | Exact reviewed tag, immutable public beta release, canonical redownload verifier and final read-only job |
| External acceptance | Still separate | Clean-user permission/CRUD, real Intel/minimum macOS coverage, other Native capabilities, sections/tags, and direct iPhone observation |

Installation examples for v0.7.1 remain conditional on verified publication.
The v0.7.0 launch/tester packet and receipt example continue to describe the
latest published public beta. Update that boundary and new versioned evidence
only after publication. Neither these local results nor signing success claims
that v0.7.1 is already released or generally available.
