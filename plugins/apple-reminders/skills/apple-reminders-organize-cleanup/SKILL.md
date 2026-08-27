---
name: apple-reminders-organize-cleanup
description: Plan and safely apply bounded Apple Reminders organization. Use for list or section moves, section creation, tag assignment, completion, deletion, deduplication proposals, and clutter review. Unused-label database cleanup is not a public operation.
---

# Apple Reminders Organize

Start with a bounded proposal that names exact candidates. Do not infer deletion or a global scope from “clean this up.”

## Workflow

1. Define an exact list, section, query, date window, completion state, supported sort, or candidate limit. If absent, enumerate lists and ask for or infer one bounded list from current context.
2. Use `fetch_reminders` for one explicit API snapshot. Call `read_reminder` for every item that may change; its opaque reference is the only write precondition callers handle. Reuse a cursor only with the same filters, sort, and limit.
3. Treat “top,” “first,” “currently selected,” and similar UI-relative language as unresolved until it is grounded in an explicit API sort (`due`, `modified`, or `title`) and exact IDs. API order is not evidence of the current Reminders UI order. Leave exact UI selection to a local macOS-capable worker.
4. Use `inspect_reminder_native` with `kind=sections` and exact `list_id` for section structure. Never treat section names as global. Inspect exact reminders before any workflow that depends on tags or attachments.
5. Group candidates into leave, patch, completion, list move, section move, tag assignment, attachment dependency, or deletion. Show current and intended state for broad/high-impact sets unless standing delegation already covers them.
6. Complete and verify every non-destructive dependency first. Apply Core changes through `change_reminder`: `set_completion`, `move_to_list`, or `patch`. Create sections and apply section/tag changes through the Native Extension. Route attachment consolidation through `$apple-reminders-attachment-maintenance` before deleting any source.
7. Delete each exact source one at a time through `delete_reminder`, using a fresh reference obtained after dependency work. Stop immediately after stale, pending, partial, failed, or manual-repair status.
8. Read back affected reminders or exact list/section state. A verified local-absence delete receipt is terminal on the public surface; it does not provide a restore token or prove Recently Deleted, iCloud, iPhone, or UI state.

Native organization actions:

```json
{"kind":"move_to_section","section_id":"EXACT-SECTION-ID"}
{"kind":"add_tag","tag":"Exact Tag"}
{"kind":"remove_tag","tag":"Exact Tag"}
```

## Destructive dependency ordering

For a request such as “delete the top four reminders, recover them, and consolidate their image attachments into one reminder”:

1. Do not execute the request in its stated order. Public recovery is unavailable, and “top four” is not an exact selector without an explicit API scope and sort.
2. Resolve a bounded snapshot and present the four exact IDs. If the user means visible UI order, stop and leave selection to a local macOS worker.
3. Read and natively inspect every source plus the destination before deletion. Preserve the source list, fields, and exact attachment metadata needed for the remaining workflow.
4. The public attachment surface cannot export or copy existing image bytes. Consolidate only when every source image is available as an exact validated local file; attach those images to the destination and obtain `verified` read-back first.
5. If any required local source image is unavailable, or if any receipt is pending/partial/failed, stop before deletion. Do not rely on future recovery as a safety mechanism.
6. Only after all dependencies are verified may the exact source reminders be deleted one by one. If deletion already happened, report the public recovery boundary and record a macOS-only follow-up rather than invoking hidden paths.

## Boundaries

- Ordinary tag add/remove changes assignments on exact reminders. Deleting unused label rows across the private store is withheld from public 0.3; offer a read-only tag inventory or proposal instead.
- List rename/delete, section rename/delete, attachment export/copy, reminder restore, Recently Deleted inspection, and exact UI selection are not public operations.
- Preserve notes, due values, alarms, recurrence, priority, list, section, tags, URL, and attachments unless the user asked to change them.
- Never mutate a title-only match when duplicates exist.
- Native section success is CloudKit/native read-back evidence, not direct iPhone observation.

## Preview for broad changes

Include the bounded scope, explicit API sort, truncation state, candidate title and ID, current list/section/due/completion state, intended action, preserved dependencies, and the authority basis for applying it.

## Output

Keep recommendations short. Clearly separate proposed and applied work, report Receipt status exactly, and name any macOS-only follow-up without claiming it was performed.
