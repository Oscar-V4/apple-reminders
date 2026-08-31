---
name: apple-reminders-organize-cleanup
description: Plan and safely apply bounded Apple Reminders organization. Use for list or section moves, section creation, tag assignment, completion, deletion, Recently Deleted recovery, deduplication proposals, and clutter review. Unused-label database cleanup is not public.
---

# Apple Reminders Organize

Start with a bounded proposal that names exact candidates. Do not infer deletion or a global scope from “clean this up.”

Default to Stable Core organization: exact Reminder Lists, title/note grouping,
completion, and archive-list moves. Sections, native tags, and exact recovery are
Experimental and require explicit intent plus an admitted capability.

## Workflow

1. Define an exact list, section, query, date window, completion state, supported sort, or candidate limit. If absent, enumerate lists and ask for or infer one bounded list from current context. For literal UI-relative “top” or “visible” targets, directly observe the current UI and resolve every visible row to one exact ID. When UI observation is unavailable, stop and offer a bounded API scope/sort; do not use it until the user explicitly approves that reinterpretation. Stop on duplicate UI-to-ID mapping and report which evidence basis was used, because API order is not current UI order.
2. Discover candidates with `fetch_reminders` summaries and record filters, sort, limit, returned IDs, truncation, or remaining pages. A broad proposal must not pre-issue or retain writable references. Follow a cursor only with identical arguments. If any next page returns `pagination_snapshot_stale`, discard the partial candidate set and restart discovery without a cursor.
3. Prefer a separate exact list or a user-approved textual heading when the goal
   is grouping. Only if the user explicitly requests native sections, run
   `diagnose_reminders {scope:"sections"}` and continue when the exact
   Experimental capability is available. Then inspect sections with exact
   `list_id`; never treat section names as global.
4. Group candidates into leave, patch, completion, list move, section move, tag assignment, attachment dependency, or deletion. Show current and intended state for broad/high-impact sets, then obtain approval unless standing delegation already covers that exact scope. Complete and verify every non-destructive dependency before a source deletion.
5. After approval, choose a chunk size from 25 to 40 candidates; the final remainder may be smaller. Within each chunk, process candidates one at a time:
   - Call `read_reminder` immediately before the intended write. Compare the exact result with the approved summary and revalidate identity, current state, destination, and preserved fields. Treat any mismatch as stale and halt the run.
   - If a native action depends on current section, tag, attachment, or sync state, inspect it using the fresh reference from that exact read.
   - Apply only the approved action using that fresh reference, then immediately read back the affected reminder or exact list/section state. Do not cache a reference for a later item or chunk.
6. Apply Core changes through `change_reminder`: `set_completion`,
   `move_to_list`, or `patch`. Prefer moving items to an archive list over
   deletion. Apply deletion through `delete_reminder` only after dependency work
   and exact authority.
   Native section create/move or tag add/remove is allowed only when explicitly
   requested and its targeted diagnosis reports `available=true`; otherwise use
   the agreed Core substitute and do not attempt a fallback.
7. Halt the entire run on the first `committed_verification_pending`, `partial_success`, concurrent-modification/stale result, ambiguity, or failure. Report the exact unresolved target and its read-only next action before considering another chunk.
8. After a fully verified chunk, report applied and unchanged counts, refresh summaries for the next chunk, and continue only while it remains inside the approved scope.

Native organization actions:

```json
{"kind":"move_to_section","section_id":"EXACT-SECTION-ID"}
{"kind":"add_tag","tag":"Exact Tag"}
{"kind":"remove_tag","tag":"Exact Tag"}
```

## Recently Deleted recovery

Before deletion, offer a Core move to an exact archive list; this preserves the
active Reminder without relying on private undelete. If an item is already
deleted and the user explicitly requests recovery, diagnose `scope=recovery`
first. Continue only on an admitted exact build/schema/compiler result. Then use
`inspect_recently_deleted {kind:"list"}` for bounded discovery and exact item
mode immediately before one-item recovery. A `del1` is short-lived, one-use, and
cannot substitute for an active `rev1`. Halt on the first non-verified receipt.

Recovery depends on the local macOS Reminders store, compatible private ReminderKit frameworks, and the 30-day Recently Deleted retention window. Report that compatibility boundary and keep UI observation, native read-back, EventKit read-back, iCloud evidence, and device evidence separate.

## Destructive composition

For “top four, delete, recover, and consolidate images,” literal “top” requires directly observed current UI identity; an API snapshot is a separately approved reinterpretation, never an automatic fallback. Clarify whether “one image” means one destination Reminder containing several copied image attachments (supported) or one newly composited bitmap (not a public operation). “Organize” or “clean this up” does not authorize deletion. If the items are active, inspect and copy every required image to the destination and verify it there; re-read and delete sources only when the user separately authorized those exact deletions. If they are already deleted, recover exact items through fresh `del1` references before reading active source attachments. In both paths, any authorized source deletion is last and one-at-a-time.

## Boundaries

- Ordinary tag add/remove changes assignments on exact reminders. Deleting unused label rows across the private store is withheld; offer a read-only tag inventory or proposal instead.
- When section or tag admission is unavailable, use the approved list/text
  substitute. `runtime_unverified`, `unsupported_build`, `compiler_required`,
  and schema mismatch are terminal no-write capability results, not retry hints.
- Preserve notes, due values, alarms, recurrence, priority, list, section, tags, URL, and attachments unless the user asked to change them.
- Never mutate a title-only match when duplicates exist.
- Native section success is CloudKit/native read-back evidence, not direct iPhone observation.

## Preview for broad changes

Include the bounded scope, evidence basis, API sort when used, truncation state, candidate title and ID, current list/section/due/completion state, intended action, preserved fields, and the authority basis for applying it.

## Output

Keep recommendations short. Clearly separate proposed and applied work, and report Receipt status exactly.
