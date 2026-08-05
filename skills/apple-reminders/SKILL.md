---
name: apple-reminders
description: Manage native Apple Reminders data from Codex. Use when the user wants to inspect lists, sections, reminders, notes, URLs, due dates, priority, flags, completion state, image attachments, task organization, cleanup proposals, or safe create/update/delete changes in Apple Reminders.
---

# Apple Reminders

## Overview

Use this skill to turn Apple Reminders state into grounded task briefs, capture plans, organization proposals, and safe reminder updates. Keep answers tied to actual list names, section names, reminder titles, dates, notes, tags, URLs, completion state, and attachment evidence.

For exact adapter invocation, backend selection, and command names, read [references/adapter-cli.md](references/adapter-cli.md). The bundled adapter is a local macOS helper for this personal plugin. It is not the OpenMinis contribution surface. Do not copy this local skill into MinisSkills; use the separately allowlisted `minis/apple-reminders/` export, which targets only Minis' built-in command surface.

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

1. Read the relevant Reminders state first so the request is grounded in actual lists, sections, reminders, and attachments.
2. Normalize relative time language into explicit dates, times, and timezone-aware ranges before reasoning about due dates or reminders.
3. Keep reads bounded. Prefer explicit list, section, status, date window, tag, or search text constraints. If the user does not state a horizon, choose a narrow default and say so.
4. When a bounded read returns too much, page or summarize within that same scope before widening the scan.
5. When the user leaves something ambiguous, inspect Reminders history and current list structure for a clear precedent before choosing a default.
6. When a list, section, or reminder is referenced indirectly, search the bounded relevant state before asking the user for details.
7. For image or URL attachments, resolve the exact target reminder first, attach/delete/replace the explicit file path, URL, or attachment id, then read back the reminder or attachment row to verify it. For image attachments, use the `attach_image` default ReminderKit background backend and verify mobile sync evidence before reporting success for user-facing work.
8. For sections, preserve list-level section membership and ordering. Do not treat a section name as global unless the data proves it is unique.
9. When creating a new list, choose a subject-appropriate emoji badge/emblem and color instead of leaving the default list icon. Infer a tasteful emoji from the list purpose, matching nearby user conventions when possible, and verify the list's visual identity after creation. If the adapter cannot set the badge/emblem, report that limitation instead of silently leaving a generic icon.
10. For bulk edits, inspect a reasonable bounded set first. If the current user has granted standing delegation, apply the change and report the exact affected set afterward; otherwise restate the qualifying reminders before applying changes.
11. Use foreground UI automation only as a fallback for verification or unsupported flows. Prefer public APIs and the local background Reminders adapter for normal operation.
12. Surface conflicts, duplicate matches, missing target lists, sync uncertainty, and destructive effects before writing.
13. If the request is still ambiguous after checking for precedent or scanning a reasonable bounded scope, summarize the candidate targets or exact diff before writing anything.

## Daily Brief Defaults

- Use active incomplete reminders only unless the user asks for completed items.
- Resolve “today” in the current local timezone. Treat overdue as before today's local midnight, due today by local display date, and “this week” as after today through the coming Sunday. Use a rolling seven-day window only when the user says “next seven days.”
- Bound the initial snapshot at 100 reminders. Show at most 20 no-due-date items, group them by list or section, and report omitted or truncated counts.
- Treat all-day and timed reminders by their local display date. State the date and timezone basis in the brief.
- Do not include note bodies, URLs, attachment metadata, source paths, or raw database paths unless the request requires them or they are necessary to disambiguate a target.

## Write Safety

- Preserve title, notes, due date, reminder alert date, priority, flag, list, section, completion state, tags, URL, and attachments unless the user asked to change them.
- Treat deletes, bulk completion, broad moves, and attachment removal as high-impact actions.
- When standing delegation applies, high-impact writes may be executed without a separate confirmation, but they must be bounded, logged, and verified with a read-back.
- When standing delegation does not apply, restate the qualifying reminder set and scope before applying high-impact writes.
- If multiple similarly named reminders, lists, or sections exist, identify the intended one explicitly before editing.
- Prefer structured local adapter calls over free-form AppleScript or UI gestures; use foreground UI automation only when the adapter cannot do the job or a UI read-back is required.
- Prefer EventKit or AppleScript-backed behavior for public reminder fields and deletion.
- For image attachments and image replacement, prefer the ReminderKit background path. It creates native Reminders attachments with CloudKit server-record evidence and is the normal path for iPhone-facing capture.
- Resolve one explicit source image before writing. If “this screenshot” could refer to multiple local files or no attached conversation image is available, identify or request the exact source instead of guessing.
- Use the SQLite-backed adapter for Reminders surfaces not exposed through public APIs, such as URL attachments, sections, tags, full-grasp cache reads, and verified audit or repair flows.
- When creating or updating reminders through the local adapter, rely on the adapter's AppleScript title/body sync for visible native UI text; do not treat DB-only title/body writes as sufficient.
- The SQLite-backed adapter must run schema checks, use transactions, update related cloud-state rows, and verify with a read-back.
- Deletion must use native Reminders delete behavior so deleted reminders go through Reminders' Recently Deleted flow. Never hard-delete rows directly from the database.
- Do not make direct database writes outside the Reminders group container discovered on the user's machine.
- Keep iCloud sync caveats explicit when a change relies on private ReminderKit or storage details.
- Use safe tag writes only through scoped adapter commands; do not hard-delete tag labels as part of ordinary tag removal.
- Use safe attachment removal only through scoped adapter commands; these soft-delete Reminders attachment objects and do not hard-delete copied image files.
- Treat SQLite-created image attachments as local-only unless audit/read-back shows mobile sync evidence. Do not use local-only image attachments for ordinary user-facing task capture when the user will review the reminder on iPhone.
- `mobile_visible_likely: true` is CloudKit/mobile-sync evidence, not direct inspection of an iPhone screen. Say “mobile visibility evidence was found”; claim “confirmed on iPhone” only after an actual device/UI read-back.
- When attachment audit finds local-only image attachments, prefer a dry-run repair first, then a bounded, backed-up repair when the user has delegated cleanup.
- Do not write urgent alerts, location alerts, or message-when-messaging alerts until the adapter exposes verified commands for those surfaces.

## Output Conventions

- Name the list and section for each relevant reminder when location matters.
- Use exact dates with weekdays for due or scheduled items.
- When proposing changes, show current state and intended state.
- When reporting attachment work, include the file name and distinguish local UI read-back, CloudKit/mobile-sync evidence, and actual device UI confirmation.
- When summarizing a large task set, group by operational meaning: overdue, due today, upcoming, unscheduled, waiting, reference, or cleanup candidates.
- Keep recommendations short and actionable. Do not dump raw database rows unless debugging.

## Adapter Command Surface

Use the local adapter for available commands such as reads, search, create/update/complete/delete, section moves, tags, attachments, cache, backup, and diagnostics. Before invoking detailed or uncommon commands, read [references/adapter-cli.md](references/adapter-cli.md) and run the command's `--help`.

## Example Requests

- "오늘 할 일 브리핑해줘."
- "이 스크린샷을 수강신청 미리알림에 첨부해줘."
- "생각 주머니에서 실행 가능한 것만 골라서 이번 주 섹션으로 정리해줘."
- "🪣 목록 전체를 읽고 비슷한 항목끼리 섹션 제안해줘."
- "급한일 중 마감 지난 것만 보여주고 완료 처리 후보를 알려줘."
- "방금 말한 내용을 To Shop List에 사진과 같이 추가해줘."
- "취업캠프 준비 목록을 만들고 캡처 이미지와 참고 URL을 붙여서 정리해줘."
