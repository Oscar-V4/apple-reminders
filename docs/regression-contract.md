# User-Validated Regression Contract

This document freezes the user-facing behavior that must survive the 0.4
Interface. A refactor is rejected if any protected behavior
regresses even when the package becomes smaller or a microbenchmark improves.

## Public tool surface

The public schema contains exactly 15 tools:

- Core: `request_reminders_access`, `list_reminder_lists`, `fetch_reminders`,
  `read_reminder`, `create_reminder`, `change_reminder`, `delete_reminder`, and
  `ensure_reminder_list`.
- Native Extension: `inspect_reminder_native`, `create_reminder_section`,
  `organize_reminder`, and `change_reminder_attachment`.
- Recovery: `inspect_recently_deleted` and `recover_deleted_reminder`.
- Diagnostics: `diagnose_reminders`.

Every input schema is closed and bounded. Bounded reads, snapshot-bound cursors,
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
   confirmation. Image replacement and deletion use a ReminderKit
   `removeAttachment:` save with no direct SQLite connection held open; success
   requires the old exact attachment to be detached from the exact Reminder,
   and the replacement to remain active. Release verification additionally
   observes the native UI showing one image after replacement and none after
   deletion. Native attach holds no SQLite write transaction. An unknown
   removal outcome performs no compensation and requires a fresh inspection
   before retry.
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
    Every verification-pending public Receipt carries a structured
    `sync_pending` error plus safe read-before-retry recovery. The recovery
    action must match the affected resource: `fetch_reminders` for Reminder
    creation, `list_reminder_lists` for list creation,
    `inspect_reminder_native` for section creation, and `read_reminder` for
    reminder-scoped changes. Public pending-mutation errors set
    `retryable=false`; only the separate fresh-read action is safe. The result
    never degrades into an outer contract failure or authorizes an automatic
    mutation retry.
    Malformed optional fields after dispatch preserve an unknown possible
    write rather than becoming `failed_no_mutation`.
13. The explicit access result preserves pre/post authorization,
    `request_attempted`, `prompt_expected`, and an unobservable
    `prompt_observed: null` on success and permission denial. It never claims
    that macOS UI was observed, never verifies a non-full final state, and
    never directs the access tool back to itself.
14. The release package resolves only bundled backends and excludes tests,
    databases, backups, caches, screenshots, and other local artifacts.
15. Recently Deleted list mode issues no write authority. Exact item mode alone
    issues one short-lived `del1`; recovery consumes it once, rejects changed
    private and native pre-save guards or cross-account destinations, verifies
    actual image backing bytes plus pre/native/post attachment counts, and
    requires exact EventKit read-back before `verified`. Any post-save read
    failure preserves a committed/pending outcome. Backend mutation state is
    carried independently from the public status into contract validation.
16. `copy_image` independently revalidates fresh source and destination `rev1`
    References, consumes both after dispatch, keeps backing paths private,
    preserves the source, and requires byte-identical SHA-512, size, and
    dimensions on the exact destination attachment. Decoded UTI normalization
    is allowed only when the bytes remain exact.
17. A fetch continuation carries a cursor-contained digest of the full ordered
    identifier/revision snapshot. Changed membership or revisions discard the
    page and return `concurrent_modification/pagination_snapshot_stale`.
18. Broad cleanup discovers summaries first, obtains each write Reference just
    in time, operates in authorized 25–40 item chunks, verifies every item, and
    halts the whole run at the first ambiguous, stale, pending, partial, or
    failed result.
19. A URL A-to-B retry that observes native A+B after EventKit already reads B
    never reports `unchanged` and never guesses which extra URL is disposable.
    It returns a no-write ambiguity, issues no fresh writable Reference, and
    directs an exact native inspection before attachment-ID cleanup.
20. Every top-level discriminated MCP input repeats its callable fields inside
    each object branch. Host schema conversion must expose bounded fetch
    filters and exact Recently Deleted selectors instead of reducing a branch
    to only its `status` or `kind` discriminator.
21. Recovery errors expose fixed public messages only. Home-relative or
    absolute paths, Reminders store filenames, and native helper/class details
    are contract failures. A parent-proven missing path or request-size
    rejection preserves a no-write result; generic process `OSError`, timeout,
    or malformed post-launch output remains an unknown possible write.
22. Recently Deleted discovery is reachable beyond the first page. Its opaque
    cursor binds the exact account filter, limit, and full ordered deleted-item
    identity/revision snapshot. Drift rejects a continuation as
    `pagination_snapshot_stale`; callers discard the partial inventory and
    restart without a cursor. The snapshot digest is not returned as a
    standalone field; it is carried only inside the semantically opaque cursor
    and is neither a secret nor a write precondition.
23. Literal UI-relative selection is never silently reinterpreted as API
    order. If the current UI cannot be observed and mapped to exact IDs, the
    workflow stops and requires explicit user agreement before using a named
    API scope and sort. “Organize” alone is not delete authority, and native
    image copy does not claim to composite several images into one bitmap.
24. URL composition treats a malformed, mismatched, or incomplete native
    attachment inventory as uncertainty and performs no native attachment
    write. A `failed_no_mutation` recovery Receipt cannot erase non-empty
    post-state or other mutation evidence. Verified recovery binds its raw
    target/before/after identities and attachment counts to the authorized
    item, destination, and proof tuple; both raw attachment-set digests must
    equal the private `del1` guard digest. The native deleted-item guard is
    fetched again immediately before the ReminderKit save. Snapshot-stale
    active and deleted pages direct callers back to the same read without a
    cursor. Recently Deleted fingerprinting and page hydration share a pinned
    read transaction, and hydrated page revisions must match the fingerprint.
    A fresh unchanged EventKit URL with any non-target native URL is a no-write
    ambiguity, including the stale-A-only state where B is absent.
25. A terminal Core create/change or ordinary Native mutation is not
    `verified` until a canonical final state matches the requested fields or
    closed action. Contradictory final state issues no fresh Reference and
    requires a fresh read; an exception while projecting a post-dispatch Native
    result cannot become `failed_no_mutation`, while an exception during
    pre-dispatch Reference revalidation cannot become a possible-commit result.
    Downgrading `unchanged` after a failed final read must remain a
    contract-valid pending result with unknown write state.
26. Native UUID comparisons use canonical UUID spelling and tag comparisons
    use the adapter's hash-prefix-insensitive canonical form. Removing a tag or
    deleting/replacing an attachment requires a complete, valid,
    non-truncated inventory; omitted, malformed, oversized, or truncated rows
    cannot prove absence. Verified image attach/replace/copy binds one stable
    mode-0600 pre-dispatch snapshot to final digest, size, dimensions, decoded
    UTI where applicable, and affirmative mobile-visible CloudKit evidence.
27. Commit-capable idempotent adapter work persists a content-free
    `in_progress` fence before dispatch. An unreadable store or failed fence
    prevents dispatch; an incomplete fence on replay blocks the callback and
    returns outcome-unknown. The current key is protected from wall-clock
    pruning, and unresolved fences are never capacity-evicted to admit a new
    mutation; a full unresolved store fails closed before dispatch. An adapter
    callback error that the existing receipt boundary proves happened before
    mutation atomically clears its fence, while partial or outcome-unknown
    errors keep the fence and block redispatch. Core create translates only a
    contract-validated EventKit `failed_no_mutation` result or parent-issued
    `not_started` provenance into that cleanup contract. Missing paths and
    request-size rejection are parent-proven; a generic process `OSError`,
    timeout, oversized output, malformed output, or child-supplied provenance
    cannot authorize fence removal.

## Deliberately withheld behavior

Unused-tag cleanup, raw attachment export, attachment audit/repair, broad
backup/Snapshot restore, log purge, flag mutation, and native `show_reminder`
are not public promises.
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
source-only harness. Contract tests must exercise the 15-tool schema, Core,
Native, Recovery, and Diagnostics Modules, result validator, packaged startup,
and representative live
workflow without coupling to private route ordering.
