---
name: apple-reminders-organize-cleanup
description: Plan and safely apply bounded Apple Reminders organization. Use for list or section moves, section creation, tag assignment, completion, deletion, deduplication proposals, and clutter review. Unused-label database cleanup is not a public operation.
---

# Apple Reminders Organize

Start with a bounded proposal that names exact candidates. Do not infer deletion or a global scope from “clean this up.”

## Workflow

1. Define an exact list, section, query, date window, completion state, supported sort, or candidate limit. If absent, enumerate lists and ask for or infer one bounded list from current context.
2. Use `fetch_reminders` for one bounded API snapshot. Call `read_reminder` for every item that may change; its opaque reference is the only write precondition callers handle. Reuse a cursor only with identical filters, sort, and limit.
3. Use `inspect_reminder_native` with `kind=sections` and exact `list_id` for section structure. Never treat section names as global.
4. Group candidates into leave, patch, completion, list move, section move, tag assignment, attachment dependency, or deletion. Show current and intended state for broad/high-impact sets unless standing delegation already covers them.
5. Complete and verify every non-destructive dependency first. Apply Core changes through `change_reminder`: `set_completion`, `move_to_list`, or `patch`. Apply deletion through `delete_reminder` only after dependency work.
6. Create a section with `create_reminder_section {list_id, name}`. Move to a section or add/remove a tag with `organize_reminder {reference, action}`.
7. Delete exact sources one at a time with fresh references, stopping after any stale, pending, partial, failed, or manual-repair result.
8. Read back affected reminders or exact list/section state.

Native organization actions:

```json
{"kind":"move_to_section","section_id":"EXACT-SECTION-ID"}
{"kind":"add_tag","tag":"Exact Tag"}
{"kind":"remove_tag","tag":"Exact Tag"}
```

## Destructive composition

For “delete the top four, recover them, and consolidate their images,” do not follow the stated order. Resolve an exact API scope/sort and IDs; current UI order is unsupported. Inspect all sources and the destination. Public tools cannot restore reminders or export existing image bytes, so consolidate only from exact local image files and verify the destination before deletion. Otherwise stop before deletion and leave UI/Recently Deleted work to a local macOS worker.

## Boundaries

- Ordinary tag add/remove changes assignments on exact reminders. Deleting unused label rows across the private store is withheld from public 0.3; offer a read-only tag inventory or proposal instead.
- Preserve notes, due values, alarms, recurrence, priority, list, section, tags, URL, and attachments unless the user asked to change them.
- Never mutate a title-only match when duplicates exist.
- Native section success is CloudKit/native read-back evidence, not direct iPhone observation.

## Preview for broad changes

Include the bounded scope, explicit API sort, truncation state, candidate title and ID, current list/section/due/completion state, intended action, preserved fields, and the authority basis for applying it.

## Output

Keep recommendations short. Clearly separate proposed and applied work, and report Receipt status exactly.
