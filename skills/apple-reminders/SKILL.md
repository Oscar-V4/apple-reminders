---
name: apple-reminders
description: Manage native Apple Reminders data from Codex. Use when the user wants to inspect lists, sections, reminders, notes, URLs, due dates, priority, flags, completion state, image attachments, task organization, cleanup proposals, or safe create/update/delete changes in Apple Reminders.
---

# Apple Reminders

## Overview

Use this skill to turn Apple Reminders state into grounded task briefs, capture plans, organization proposals, and safe reminder updates. Keep answers tied to actual list names, section names, reminder titles, dates, notes, tags, URLs, completion state, and attachment evidence.

Use the bundled typed MCP tools as the normal operation surface. For private maintenance details, backend policy, or a missing MCP tool, read [references/adapter-cli.md](references/adapter-cli.md). The adapter is a local macOS implementation detail for this personal plugin, not the OpenMinis contribution surface. Do not copy this local skill into MinisSkills; use the separately allowlisted `minis/apple-reminders/` export, which targets only Minis' built-in command surface.

## Purpose-Specific Routing

- Use `$apple-reminders-daily-brief` for today/tomorrow/date briefs, overdue/due-today/this-week summaries, and no-due-date task readouts.
- Use `$apple-reminders-quick-capture` for adding one or more new reminders, including list choice, typed due dates/alarms, recurrence, notes, priority, URLs, or image follow-up.
- Use `$apple-reminders-organize-cleanup` for list/section organization, bounded moves, batch completion, deletion previews, tag cleanup, and deduplication proposals.
- Use `$apple-reminders-attachment-maintenance` for screenshots, images, URLs, iPhone visibility, attachment audit, repair, replacement, or deletion.

## Preferred Deliverables

- Task state summaries with exact lists, sections, counts, due dates, and completion status.
- Capture proposals or final reminder details that are ready to create.
- Organization proposals that show the current location and intended list or section move.
- Attachment actions that name the target reminder, source file or URL, attachment id when available, and verification result.
- Image attachment work that treats iPhone visibility as the success criterion, not merely local Mac rendering.
- Cleanup proposals with the exact qualifying reminder set before any bulk write.
- Daily or weekly task briefs that separate overdue, due soon, unscheduled, blocked, and recently captured items.
- Change proposals that show the current reminder and the intended update.

## Workflow

1. On first use or after an environment change, run `reminders_plugin_doctor` and `get_reminders_capabilities`. Call `request_reminders_access` only when EventKit permission is needed and the user has requested a Reminders operation; it is the explicit TCC-prompting step.
2. Read the relevant Reminders state first so the request is grounded in actual lists, sections, reminders, and attachments. Prefer `list_reminder_lists`, `fetch_reminders`, and `read_reminder` for public fields.
3. Normalize relative time language into explicit dates, times, and timezone-aware ranges before reasoning about due dates or alarms. Keep all-day/timed due values and alarm triggers distinct.
4. Keep reads semantically bounded. Use calendar IDs, the matching incomplete-due/completed-completion range, or a narrower private scope in addition to a limit. Text search or `modified_after` alone is not a native EventKit bound.
5. When a bounded read returns a continuation cursor, page within the same immutable scope; never reuse a cursor after changing filters, sort, or page size.
6. When the user leaves something ambiguous, inspect current list structure and bounded matching state for a clear precedent before choosing a default.
7. When a list, section, or reminder is referenced indirectly, search the bounded relevant state before asking the user for details.
8. For image or URL attachments, resolve the exact target reminder first, act on an explicit source path, URL, or attachment ID, then read back attachment evidence. For images, use `attach_image_to_reminder`; its normal backend is ReminderKit and success still requires mobile-sync evidence.
9. For sections, preserve list-level section membership and ordering. Do not treat a section name as global unless the data proves it is unique.
10. When creating a new list, choose a subject-appropriate emoji badge/emblem and color instead of leaving the default list icon. Infer a tasteful emoji from the list purpose, matching nearby user conventions when possible, and verify the list's visual identity after creation. If the available tool cannot set the badge/emblem, report that limitation instead of silently promising it.
11. For bulk edits, inspect a reasonable bounded set first. If the current user has granted standing delegation, apply the change and report the exact affected set afterward; otherwise restate the qualifying reminders before applying changes.
12. Use `show_reminder` only when the user asks for a native UI handoff or visual inspection. Foreground UI automation is not the normal data path.
13. Surface conflicts, duplicate matches, missing target lists, sync uncertainty, and destructive effects before writing.
14. If the request is still ambiguous after checking for precedent or scanning a reasonable bounded scope, summarize the candidate targets or exact diff before writing anything.

## Daily Brief Defaults

- Use active incomplete reminders only unless the user asks for completed items.
- Resolve “today” in the current local timezone. Treat overdue as before today's local midnight, due today by local display date, and “this week” as after today through the coming Sunday. Use a rolling seven-day window only when the user says “next seven days.”
- Enumerate the intended lists, then call `fetch_reminders` with their `calendar_ids`, `status=incomplete`, and a limit of at most 100 per page. This calendar scope includes unscheduled items; a due-date-only range does not. Follow opaque cursors only within the same scope. Show at most 20 no-due-date items and report omitted or truncated counts.
- Treat all-day and timed reminders by their local display date. State the date and timezone basis in the brief.
- Do not include note bodies, URLs, attachment metadata, source paths, or raw database paths unless the request requires them or they are necessary to disambiguate a target.

## Write Safety

- Preserve title, notes, due date, reminder alert date, priority, flag, list, section, completion state, tags, URL, and attachments unless the user asked to change them.
- Treat deletes, bulk completion, broad moves, and attachment removal as high-impact actions.
- When standing delegation applies, high-impact writes may be executed without a separate confirmation, but they must be bounded, logged, and verified with a read-back.
- When standing delegation does not apply, restate the qualifying reminder set and scope before applying high-impact writes.
- If multiple similarly named reminders, lists, or sections exist, identify the intended one explicitly before editing.
- Prefer typed MCP tools over direct CLI, free-form AppleScript, or UI gestures.
- Public reminder reads and writes use EventKit. Create calls require an idempotency key; updates, completion/reopen, list-to-list moves, and deletion require the exact ID and `expected_last_modified` from a fresh read.
- Preserve omitted fields on update. Use typed `due`, `alarms`, and `recurrence_rules`; never infer an alarm merely from a due date.
- EventKit does not expose the native Reminders flag field. If the user explicitly asks to set or clear it, follow the exact-ID, version-checked AppleScript fallback in the adapter reference and read back; never claim the typed create/update tool wrote a flag.
- For image attachments and image replacement, prefer the ReminderKit background path. It creates native Reminders attachments with CloudKit server-record evidence and is the normal path for iPhone-facing capture.
- Resolve one explicit source image before writing. If “this screenshot” could refer to multiple local files or no attached conversation image is available, identify or request the exact source instead of guessing.
- Use the private adapter-backed MCP tools only for Reminders surfaces not exposed through public APIs. Section creation and membership moves use native ReminderKit saves with CloudKit version read-back; tags, URL attachment objects, and bounded audit/repair flows retain their documented private-store boundaries.
- Before changing an existing reminder through a private adapter-backed tool, call `list_reminder_attachments` and pass its fresh `reminder_version` as `if_version`. When no attachment selection is needed, use `limit=1` to keep this token read small. This guard is required for tag assignment, section moves, URL/image attachment, attachment replacement, and attachment removal.
- The SQLite-backed adapter must run schema checks, use transactions, update related cloud-state rows, and verify with a read-back. A local SQLite section row or membership alone is not iCloud evidence and must never be reported as `verified`.
- Use `delete_reminder` only after an exact public read and pass its fresh `last_modified` as `expected_last_modified`. The MCP path uses EventKit, verifies local absence, and never falls back to AppleScript or the private DB. If the identifier is no longer resolvable, it reports a no-write not-found result because EventKit identifiers can change after a full sync; read current state before retrying. Report Recently Deleted as expected unless actual UI evidence verified it.
- Do not make direct database writes outside the Reminders group container discovered on the user's machine.
- Keep iCloud sync caveats explicit when a change relies on private ReminderKit or storage details.
- Use `add_reminder_tag` and `remove_reminder_tag` for ordinary assignment changes; those do not hard-delete labels. Unused-label maintenance is intentionally separate: preview with `preview_unused_reminder_tags`, review the bounded/account-aware candidate set, and pass its exact digest to `cleanup_unused_reminder_tags`. That apply operation hard-deletes only revalidated zero-reference label rows and reports backup/recovery semantics.
- Use safe attachment removal only through scoped adapter commands. Image objects use the recoverable soft-delete path and copied image files are never hard-deleted. URL metadata rows follow the native Reminders lifecycle: the object row is removed while its CloudKit state is retained as the sync tombstone.
- Treat SQLite-created image attachments as local-only unless audit/read-back shows mobile sync evidence. Do not use local-only image attachments for ordinary user-facing task capture when the user will review the reminder on iPhone.
- `mobile_visible_likely: true` is CloudKit/mobile-sync evidence, not direct inspection of an iPhone screen. Say “mobile visibility evidence was found”; claim “confirmed on iPhone” only after an actual device/UI read-back.
- When attachment audit finds local-only image attachments, call `preview_reminder_attachment_repairs`, then pass its unchanged candidate digest to a bounded, backed-up `apply_reminder_attachment_repairs` after delegation.
- Absolute alarms and coordinate-backed enter/leave location alarms are supported through EventKit. Do not write urgent alerts, relative alarms, or message-when-messaging alerts; those surfaces are not verified.
- Report the normalized receipt status exactly: `unchanged`, `verified`, `committed_verification_pending`, or `partial_success`. Failed operations use stable error codes and must not be described as success.

## Output Conventions

- Name the list and section for each relevant reminder when location matters.
- Use exact dates with weekdays for due or scheduled items.
- When proposing changes, show current state and intended state.
- When reporting attachment work, include the file name and distinguish local UI read-back, CloudKit/mobile-sync evidence, and actual device UI confirmation.
- When summarizing a large task set, group by operational meaning: overdue, due today, upcoming, unscheduled, waiting, reference, or cleanup candidates.
- Keep recommendations short and actionable. Do not dump raw database rows unless debugging.

## Adapter Command Surface

Use the bundled MCP tools for reads, create/update/complete/reopen/delete, list/section moves, tags, attachments, repair, diagnostics, permission handling, and native UI handoff. Read [references/adapter-cli.md](references/adapter-cli.md) only for the private implementation boundary or an advanced maintenance flow that is not exposed as a tool.

## Example Requests

- "오늘 할 일 브리핑해줘."
- "이 스크린샷을 수강신청 미리알림에 첨부해줘."
- "생각 주머니에서 실행 가능한 것만 골라서 이번 주 섹션으로 정리해줘."
- "🪣 목록 전체를 읽고 비슷한 항목끼리 섹션 제안해줘."
- "급한일 중 마감 지난 것만 보여주고 완료 처리 후보를 알려줘."
- "방금 말한 내용을 To Shop List에 사진과 같이 추가해줘."
- "취업캠프 준비 목록을 만들고 캡처 이미지와 참고 URL을 붙여서 정리해줘."
