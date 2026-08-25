---
name: apple-reminders-organize-cleanup
description: Plan and safely execute Apple Reminders organization and cleanup. Use when the user wants to sort reminders, move items into lists or sections, create sections, clean stale tags, complete batches, delete clutter, deduplicate, or reorganize a list.
---

# Apple Reminders Organize Cleanup

## Overview

Use this skill when the user's goal is to improve reminder structure or reduce clutter. The default deliverable is a bounded proposal with exact IDs and a preview of affected reminders before any high-impact write.

## Workflow

1. Define the scope: list, section, search text, tag, date window, completed state, or maximum item count. If absent, enumerate lists and begin with `fetch_reminders` over one explicit `calendar_id`, limited to 100. Resolve section scope with `list_reminder_sections` and that exact list ID; never use a list name as the selector because names can repeat across accounts.
2. Read current state before proposing changes. Use exact IDs, list/calendar IDs, section names, due dates, `last_modified`, completion state, and tag names from tool output.
3. Group candidates by operational intent: move, create section, complete, delete, tag change, or leave untouched.
4. For each proposed write, show current state and intended state. Include the exact command family, but do not run it until the affected set is clear.
5. For broad completion, deletion, tag cleanup, or many moves, require explicit user delegation unless standing delegation is already established.
6. After applying any write, read back the affected list, section, tag, or reminder IDs and report normalized receipt statuses exactly.

## Safe Commands

Read `../apple-reminders/references/adapter-cli.md` before invoking commands.

- Use `create_reminder_section` for new sections after confirming the list. It saves through ReminderKit and returns `verified` only after the section CloudKit version reaches iCloud; an older same-name local-only section is repaired in place.
- Use `move_reminder_to_section` only within the same list and pass the fresh `reminder_version` from `list_reminder_attachments` as `if_version`. It saves through ReminderKit and verifies the list CloudKit version; calling it again with the same target repairs an older local-only membership. Use `move_reminder_to_list` for an EventKit list-to-list move; it requires the target `calendar_id` and fresh `expected_last_modified`.
- Use `complete_reminder` for completion. Preserve notes, date, priority, flag, list, tags, URLs, and attachments.
- Use `delete_reminder` by exact ID with the fresh `last_modified` from the resolving public read. It uses EventKit, verifies local absence, and never retries through AppleScript or the private DB. Describe Recently Deleted as expected unless it was checked in the actual UI.
- Use `add_reminder_tag` and `remove_reminder_tag` for tag assignment changes, passing the fresh `reminder_version` as `if_version`.
- Unused-label cleanup intentionally hard-deletes only orphan label rows. First call `preview_unused_reminder_tags` with tag or literal prefix, account scope when available, and a limit. Apply with `cleanup_unused_reminder_tags` only when the preview is untruncated and its exact `candidate_digest` is unchanged; report backup and recovery semantics.

## Preview Format

For destructive or bulk changes, present:

- scope and read command used
- total candidates and truncation status
- per-reminder title, ID, list, section, due/display date, and reason
- intended command per item or batch
- fields that will be preserved
- confirmation needed or standing-delegation basis

## Output Rules

- Keep recommendations short and action-oriented.
- Never move or delete title-only matches when duplicates exist; ask the user to choose from bounded candidates.
- Do not expose note bodies unless necessary for classification.
- Distinguish "proposed" and "applied" from receipt statuses `unchanged`, `verified`, `committed_verification_pending`, and `partial_success`; failed calls use stable error codes.
