# User-Validated Regression Contract

This document freezes the user-facing behavior that must survive internal
simplification. A refactor is rejected if any item below regresses, even when
the package becomes smaller or a microbenchmark improves.

## Public Tool Surface

- Keep all 32 MCP tools in `schemas/mcp-tools.json` throughout the 0.2.x line.
- Preserve exact-ID targeting, bounded reads, opaque pagination, idempotent
  create, fresh-version/last-modified preconditions, and normalized receipts.
- Preserve the successful mutation states `unchanged`, `verified`,
  `committed_verification_pending`, and `partial_success`.

The exact 0.2.x tool-name contract lives in
`tests/fixtures/golden_mcp_tools.json` and is enforced by
`tests/test_golden_regressions.py`.

## Behaviors Proven Through Real Use

1. Public reminder reads and primary-field writes use EventKit.
2. Create, update, complete, reopen, list moves, and deletion preserve fields
   the user did not ask to change.
3. Delete uses an exact current EventKit identifier plus a fresh
   `expected_last_modified`, verifies local absence, and does not claim that
   Recently Deleted was visually confirmed.
4. Timed and all-day due values stay distinct from alarms. Time zones,
   recurrence, location alarms, and priority keep their typed contracts.
5. A non-null URL create/update preserves EventKit metadata and also verifies
   the user-visible native URL attachment. A failed second step is reported as
   `partial_success`, and retry does not duplicate the reminder or attachment.
6. Image attachment and replacement use the native ReminderKit path. CloudKit
   evidence may be described as mobile-visibility evidence, never as direct
   iPhone-screen confirmation.
7. URL attachment replacement and deletion preserve native tombstone and
   ordering behavior and reject duplicates or stale versions before mutation.
8. Section creation and same-list membership moves use native ReminderKit and
   require CloudKit read-back before `verified`.
9. Tag assignment uses a fresh reminder version. Unused-tag cleanup keeps the
   intentional preview-digest, literal-scope, zero-reference, backup, and
   hard-delete behavior.
10. Attachment audit/preview/apply repair keeps digest binding, per-reminder
    version tracking, compensation, and manual-repair receipts.
11. `show_reminder` remains an explicit native UI handoff, not the normal data
    path.
12. The release package resolves only bundled backends and excludes tests,
    databases, backups, caches, screenshots, and other local artifacts.

## Performance Guardrails

- MCP initialize plus `tools/list`: p95 at or below 300 ms.
- MCP doctor route: p95 at or below 1,500 ms.
- Cached EventKit helper build gate: p95 at or below 350 ms.
- Fresh EventKit helper build gate: p95 at or below 2,500 ms.
- Runtime archive: at or below the repository's validated byte budget.

These are regression budgets, not cross-machine performance claims.

## Compatibility Policy

The 0.2.x line may mark direct adapter public-write commands as deprecated, but
must not remove or silently change them. Their implementation can be removed
only in a separately reviewed 0.3.0 breaking release after MCP behavior and
migration guidance are complete.
