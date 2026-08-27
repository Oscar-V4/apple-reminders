# User-Validated Regression Contract

This document freezes the user-facing behavior that must survive the 0.3
Interface simplification. A refactor is rejected if any protected behavior
regresses even when the package becomes smaller or a microbenchmark improves.

## Public tool surface

The public schema contains exactly 13 tools:

- Core: `request_reminders_access`, `list_reminder_lists`, `fetch_reminders`,
  `read_reminder`, `create_reminder`, `change_reminder`, `delete_reminder`, and
  `ensure_reminder_list`.
- Native Extension: `inspect_reminder_native`, `create_reminder_section`,
  `organize_reminder`, and `change_reminder_attachment`.
- Diagnostics: `diagnose_reminders`.

Every input schema is closed and bounded. Bounded reads, filter-bound cursors,
exact identity, create idempotency, opaque one-use `rev1` References, and
normalized mutation Receipts are mandatory.

Public callers do not receive or submit raw private `reminder_version` values or
select between backend preconditions. Result contracts are centrally enforced
even though optional MCP `outputSchema` descriptors are omitted from
`tools/list`.

## Behaviors proven through real use

1. Public reminder reads and primary-field writes use EventKit.
2. Create, update, complete, reopen, list moves, and deletion preserve fields
   the user did not ask to change.
3. Exact-list identity includes account context, so duplicate display names
   cannot broaden section or reminder scope.
4. Delete consumes a fresh opaque Reference, uses EventKit native deletion,
   verifies local absence, and does not claim that Recently Deleted was
   visually observed.
5. Timed and all-day due values remain distinct from alarms. Time zones,
   recurrence, coordinate-backed location alarms, and priority keep typed
   contracts.
6. A non-null URL create/change preserves EventKit metadata, verifies the
   user-visible native URL attachment, and then performs a final exact EventKit
   read. An unavailable final read remains verification-pending; a failed
   attachment step remains partial; idempotent retry does not duplicate the
   reminder or attachment.
7. Image attach and replacement use the ReminderKit image-data path, derive
   PNG/JPEG UTI from decoded bytes rather than a filename suffix, and require
   the stored UTI to match native helper read-back. CloudKit evidence can be
   described as likely mobile visibility, never direct iPhone-screen
   confirmation.
8. URL attachment replacement/deletion preserves the observed native tombstone
   and ordering behavior and rejects duplicate or stale changes before
   mutation.
9. Section creation and same-list section moves use exact list identity and
   ReminderKit/CloudKit read-back before `verified`.
10. Tag add/remove consumes a fresh Reference, remains idempotent where
    applicable, and rejects stale concurrency before mutation.
11. A Reference is consumed on a terminal mutation or unknown post-dispatch
    outcome. Stale, expired, replayed, or failed-revalidation References cannot
    authorize another write.
12. Receipts preserve `unchanged`, `verified`,
    `committed_verification_pending`, `partial_success`,
    `failed_no_mutation`, and `failed_manual_repair_required` distinctions.
13. The release package resolves only bundled backends and excludes tests,
    databases, backups, caches, screenshots, and other local artifacts.

## Deliberately withheld behavior

Unused-tag cleanup, attachment audit/repair, backup/Snapshot, restore, log
purge, flag mutation, and native `show_reminder` are not public 0.3 promises.
Their historical low-level behavior may remain covered by internal tests so
future work does not accidentally corrupt data, but skills and public MCP tools
must not expose or fall back to it.

The deprecated direct adapter CLI is an internal migration/diagnostic seam. It
does not expand the public compatibility contract.

## Diagnosis contract

Normal bounded work runs without Doctor preflight. `diagnose_reminders` is used
only after a relevant permission, environment, build, schema, or native
capability failure. It is content-free and a Native failure does not block Core.
A missing private-framework path alone is inconclusive on shared-cache systems.

## Performance guardrails

- MCP initialize plus `tools/list`: p95 at or below 300 ms.
- MCP diagnosis route: p95 at or below 1,500 ms.
- Cached EventKit helper build gate: p95 at or below 350 ms.
- Fresh EventKit helper build gate: p95 at or below 2,500 ms.
- Runtime archive: at or below the repository's validated byte budget.

These are regression budgets, not cross-machine performance claims.

## Test boundary

Production ignores backend-path environment overrides. Source integration tests
inject a `BackendPaths` instance into `mcp.server.main(...)` through the
source-only harness. Contract tests must exercise the 13-tool schema, Core and
Native Modules, result validator, packaged startup, and representative live
workflow without coupling to private route ordering.
