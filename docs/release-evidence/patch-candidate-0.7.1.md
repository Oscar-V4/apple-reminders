# v0.7.1 Unreleased patch candidate signoff

Status: **code integration complete; fresh helper signing, final package
verification, and publication pending**. The latest published public beta remains
[v0.7.0](https://github.com/Oscar-V4/apple-reminders/releases/tag/v0.7.0). Its
[immutable publication record](public-beta-0.7.0.md), signed artifacts, source
identities, and asset hashes remain historical evidence for that release only.

## Bounded patch scope

- PR #57: expose the exact verified image attachment ID after final matching;
  retain unknown targets for pending/unmatched receipts and require the same
  verification on replay.
- Diagnostics integration from `83ab407` and `09fed706`: distinguish missing
  framework paths from access/metadata errors; use a fixed explanation only for
  the complete otherwise-admitted metadata-only case with a verified signed
  helper and one inconclusive warning. Keep degraded/attention status and all
  blockers; do not load a framework or repeat the same inconclusive diagnosis.
- No new Native capability admission or helper `.m` changes are part of this
  preparation. No component identity migration or signing-workflow rewrite is
  included.

PRs #57, #59, and #60 are merged. The signing source is frozen at
`5dd71825aea8174d84f446f2bb2d70bfc44fe5da`, based on main commit
`0c474fbb9369c3b1f93c7710ae45a6d41115286e`. Final package verification remains
pending until the matching v0.7.1 signed helper artifacts are assembled.

## Release gates

| Gate | Current status | Required evidence |
| --- | --- | --- |
| Source version | Prepared | plugin.json and MCP SERVER_VERSION are 0.7.1; plugin description unchanged |
| Patch code integration | Complete | PRs #57/#59/#60 merged after their current-head CI; exact signing source frozen as recorded above |
| EventKit v0.7.1 artifact | Pending | Existing proven source-signing workflow; exact source/workflow history and complete signed/notarized/stapled app+manifest |
| Native v0.7.1 artifact | Pending | Existing proven source-signing workflow; exact three-subject attestation, six build inputs, complete signed app+manifest |
| Current artifact drift | Expected failure | Checked-in v0.7.0 helper manifests intentionally disagree with v0.7.1 source until new signing; audit and verifier must keep rejecting this |
| Final package | Pending | Source audit, full tests, exact component inventory, deterministic ZIP and installed three-mode/packaging smoke |
| Publication | Pending | Exact reviewed tag, immutable public beta release, canonical redownload verifier and final read-only job |
| External acceptance | Still separate | Clean-user permission/CRUD, real Intel/minimum macOS coverage, other Native capabilities, sections/tags, direct iPhone observation |

Retain the signed-source plugin description, helper sources, build inputs, and
existing provenance until the new reviewed artifacts are produced. Never edit
old manifest versions or weaken artifact/version checks to make this source
candidate appear signed. Successful v0.7.0 verification is not v0.7.1 acceptance.

Installation examples for v0.7.1 remain conditional on verified publication.
The existing v0.7.0 launch/tester packet and receipt example continue to describe
the latest published public beta; a source version bump does not update their
release evidence. After new publication, update that boundary and the new
versioned evidence together.
