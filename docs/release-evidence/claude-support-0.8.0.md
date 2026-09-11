# v0.8.0 — Codex and Claude compatibility

Status: candidate validation in progress; publication has not yet been verified.

## Scope

One local MCP runtime supports Codex, Claude Code, and a Claude Desktop MCPB
extension. Code clients receive the same five workflow skills. Desktop receives
MCP instructions and the tool catalog. No private capability admission changes.

The packaging source for signed helper preparation is
`9b78b30d9fad96ba08a59710184178f993173ad3`. Existing Python runtime capsules are
reused unchanged. EventKit and Native helpers are prepared by the existing
protected, main-owned signing workflows for version 0.8.0.

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

Pending final deterministic tests, client installation checks, live synthetic
journey, exact fixture cleanup, and independent published-asset verification.

## Limits

Maintainer-host checks do not establish fresh-user permission acceptance,
minimum-macOS end-to-end coverage, direct iPhone display, all Claude client
versions, or new Native operation support.
