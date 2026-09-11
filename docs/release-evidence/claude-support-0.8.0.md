# v0.8.0 — Codex and Claude compatibility

Status: candidate validation in progress; publication has not yet been verified.

## Scope

One local MCP runtime supports Codex, Claude Code, and a Claude Desktop MCPB
extension, integrated with the existing calendar Early Reminder candidate
(PR #66). Code clients receive the same five workflow skills. Desktop receives
MCP instructions and the tool catalog. Client integration adds no private
capability admission beyond the separately recorded Early Reminder change.

The Native helper and its signed source ancestry are retained from PR #66.
Existing Python runtime capsules are reused unchanged. The EventKit helper
requires a new protected signing run for the final Codex manifest. Accepted
component identities are recorded in their bundled provenance manifests.

An initial preparation attempt against an intermediate version commit failed
its full documentation/packaging checks and is not accepted release evidence.
The corrected source must complete the existing main-owned signing workflow,
including assembled-source tests and the final exact artifact attestation.

## Acceptance scenario

“Install Apple Reminders in Claude, create a synthetic project reminder with
notes and a future deadline, move the deadline while preserving the notes,
mark it complete, and clean up the exact test items.”

Completion criteria: an installed client discovers the tool catalog; the
normal packaged launcher runs without a host Python or compiler; exact public
reads confirm the created, updated, and completed states; only owned synthetic
fixtures are removed. Record client validation separately from a raw MCP test.

Interruption case: the Desktop package is missing its selected runtime capsule.
It must fail before any Reminders read or mutation, without selecting another
Python or requesting developer tools. Use an isolated extracted copy.

## Evidence

- Codex scaffold validation, Claude plugin/marketplace validation, and MCPB
  manifest schema validation passed.
- Claude Code 2.1.195 loaded five skills and connected to the actual bundled
  stdio server. The initial probe exposed three tools hidden by Anthropic's
  root `oneOf` restriction. The discovery projection removes that restriction;
  subsequent client logs have no skipped tools. Full branch validation remains
  server-side and rejects mismatched inputs before dispatch.
- 149 focused transport, contract, profile, and client compatibility tests passed.
- Full final assembly, installed-package validation, live synthetic journey,
  cleanup, and published-asset verification remain pending.

The maintainer's Claude Code CLI is not logged in. Its plugin loading and MCP
handshake can be tested, but an end-to-end Claude model conversation cannot be
claimed from these checks.

## Limits

Maintainer-host checks do not establish fresh-user permission acceptance,
minimum-macOS end-to-end coverage, direct iPhone display, all Claude client
versions, or new Native operation support.
