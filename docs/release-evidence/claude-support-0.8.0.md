# v0.8.0 — Codex and Claude compatibility

Status: **v0.8.0 published and independently verified** on 2026-09-11.
The immutable [public beta release](https://github.com/Oscar-V4/apple-reminders/releases/tag/v0.8.0)
contains the reviewed ZIP, Desktop MCPB, and checksum file.

## Scope and source

One local MCP runtime supports Codex, Claude Code, and a Claude Desktop MCPB
extension. Code clients receive five shared workflow skills. Desktop receives
MCP instructions and the tool catalog. The release incorporates the existing
calendar Early Reminder candidate (PR #66); client integration adds no private
capability admission beyond that separately recorded change.

- Client/signing source: `2e90b75513d8c8888ef9b207d0ba2d36c5a4cc94`.
- Accepted EventKit signing and attestation:
  [run 34587862726](https://github.com/Oscar-V4/apple-reminders/actions/runs/34587862726).
- The signer remains the existing main-owned workflow at
  `42c98bb804d68578130e1c5a915cf04b7c93f681`.
- Native helper source and signed bytes are retained from PR #66, with original
  source `6e828ddb5224b5d025395c0c08192db4ce1ec4e6` and
  [signing run 34133555132](https://github.com/Oscar-V4/apple-reminders/actions/runs/34133555132).
- Existing signed Python capsules are unchanged.
- Local checks verified exact component inventories, source fingerprints,
  Developer ID signatures, notarization, and closed signing attestations.

An initial EventKit preparation against an intermediate commit failed its
full documentation checks and is not accepted release evidence. The accepted
run above completed assembled-source tests and final artifact attestation.

## Deterministic and client checks

- Final local suite: **1,112 tests run, one skipped, no failures**, 150.683 seconds.
  The accepted signing workflow also ran all 1,112 tests without failures.
- Source audit: 77 allowlisted files; strict worktree and root-document mirrors passed.
- Codex plugin validation, Claude Code 2.1.195 plugin/marketplace validation,
  and MCPB manifest validation with `@anthropic-ai/mcpb` 2.1.2 passed.
- Native Objective-C syntax/plist checks and all 11 synthetic calendar cases passed.
- ZIP and MCPB deterministic rebuilds matched. Both archives retain the same
  allowlisted source and signed-helper executable modes; MCPB has a flat root.
- All three actual launch configurations passed initialize, tools/list,
  schema-rejection and packaging-only diagnosis from extracted paths with
  spaces. Claude configurations also ran from an unrelated working directory.
  Each exposed default/legacy/Core-only inventories of **15/15/9** tools.
- Claude Code's marketplace and plugin install commands succeeded in an isolated
  configuration directory. All 77 installed files matched the reviewed package.
  The CLI loaded five skills and connected to the bundled stdio server.
- The first Claude probe revealed three tools hidden because Anthropic rejects
  root `oneOf`. The final discovery projection exposes the closed root object;
  subsequent Claude logs report no skipped tools. Canonical branch constraints
  remain authoritative and reject cross-branch inputs before backend dispatch.

Tested plugin ZIP: `apple-reminders-0.8.0.zip`, 55,082,080 bytes, SHA-256
`da5681d273e5dc2daa4a9ea88edeec069d389e0fb08e4720e9a95a17a8d919ca`.

Tested Desktop bundle: `apple-reminders-0.8.0.mcpb`, 55,079,616 bytes, SHA-256
`6de24afb05283a2ef8292969b0899a620dbb9b164c7b91cf36e730fb36709e59`.

These hashes identify the candidate used for the live task below. A final
attachment-skill edit replaces its remaining Codex-only invocation with a
relative link to the shared organization skill. No runtime or signed component
changed. Skill/link validation and 27 focused workflow tests passed; all three
packaged launch profiles were checked again, and a clean Claude Code reinstall
picked up the final files.

Final reviewed ZIP SHA-256:
`97fca639436ec183643ed74f7a8a8bd0fbae458eeedd27690d628ef1bf34a4c8`.
Final reviewed MCPB SHA-256:
`c8c6dc08e07f0b3f58d1ee3d980a87230e0a8da3c43df7c5ba466e4373b1b396`.
Independent redownload and verification confirmed these exact final hashes.

## Composed user task

Natural request: “Install Apple Reminders in Claude, create a project reminder
with notes and a future deadline and alert, move the deadline while preserving
the notes and alert, mark it complete, and clean up the exact test items.”

Environment: Apple silicon, macOS 26.5.2 (25F84), Reminders 7.0 (3976), existing
Reminders permission. The test used the **actual Claude Code installed cache**
and its normal bundled launcher/public MCP, with no contributor source-build
opt-in. The existing MCP test client operated the tools; a Claude model did not.

A uniquely owned synthetic list and reminder were created. The all-day deadline
moved from 2028-04-01 to 2028-04-10. Fresh exact reads verified the new date and
preservation of title, notes, priority, URL metadata, two-day relative alarm,
list, and recurrence state. Completion was then verified with the same preserved
fields. The exact reminder was deleted and its absence checked; the reserved
synthetic list was removed through the existing exact-ID cleanup harness.

**Outcome: passed. Cleanup: verified.** Private fixture identifiers and raw
receipts remain outside the repository. Recently Deleted was not emptied.

## Interrupted installation

An isolated MCPB extraction had its selected Python capsule removed. Starting
through the Desktop manifest failed with exit 78 and empty MCP stdout before
server startup, with the expected missing/invalid-runtime error. The launcher
did not choose an external Python or a compiler fallback. No live fixture was
created by this case.

## Limits

The maintainer's Claude Code CLI is not logged in, and Desktop requests
reauthentication. Plugin loading, installation, protocol discovery, and direct
MCP task execution are verified; a Claude model conversation is not claimed.
Desktop's manifest, archive and launch configuration are verified, but its GUI
extension installation has not been completed in this acceptance run.

These results do not establish fresh-user permission flows, minimum-macOS
end-to-end coverage, direct iPhone display, every Claude client version, or
additional Native operation admission. See the separate
[Early Reminder evidence](early-reminder-0.8.0.md) for that retained feature.

## Published release and installation

- [Release workflow 34590606309](https://github.com/Oscar-V4/apple-reminders/actions/runs/34590606309)
  passed source/component verification, deterministic builds, a shared
  three-subject attestation, immutable publication, and independent redownload.
- Tag commit: `6b11b3c6c87b4d8e540905a8df54145cb20d19d9`.
- Annotated tag object: `b58fe9df2f464a3da7bd18b78fe619acd3f0fbf1`.
- A separate local `verify_release_assets.py v0.8.0` run passed immutable
  release and SLSA checks, all asset digests, byte-identical rebuilds, source
  audit, EventKit/Native provenance, and both signed Python capsule histories.
- Published assets: ZIP 55,082,113 bytes; MCPB 55,079,649 bytes;
  `SHA256SUMS` 185 bytes with SHA-256
  `e5c68117511e19df0cc174d03c6835c5e8efe1256535bbe90b17a0ff6a19a072`.
- The documented Claude Code command cloned the public GitHub repository at
  `v0.8.0` and installed successfully in an isolated configuration. All 77
  installed files matched the release; 15-tool discovery and packaging-only
  diagnosis passed from the remote installation.
- The maintainer's existing Codex installation was reinstalled from the public
  `v0.8.0` tag through the documented CLI commands. Its 77 cached files matched
  the release, and 15-tool discovery plus packaging-only diagnosis passed.
  A new Codex task is required to load the refreshed tools and skills.

PR #67 passed all four Python/CPU CI jobs before merging, and preserved the
signed-source ancestry of PR #66. Publication does not broaden the acceptance
limits listed above.
