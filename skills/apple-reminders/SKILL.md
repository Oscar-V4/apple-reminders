---
name: apple-reminders
description: Manage native Apple Reminders data from Codex. Use when the user wants to inspect lists, sections, reminders, notes, due dates, priority, flags, completion state, image attachments, task organization, cleanup proposals, or safe create/update/delete changes in Apple Reminders.
---

# Apple Reminders

## Overview

Use this skill to turn raw Apple Reminders state into clear task decisions, capture plans, organization proposals, and safe reminder updates. Keep answers grounded in actual Reminders data: list names, section names, reminder titles, due dates, notes, completion state, tags when available, and attachment evidence.

## Preferred Deliverables

- Task state summaries with exact lists, sections, counts, due dates, and completion status.
- Capture proposals or final reminder details that are ready to create.
- Organization proposals that show the current location and intended list or section move.
- Image attachment actions that name the target reminder, source file, and verification result.
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
7. For image attachments, resolve the exact target reminder first, attach the explicit file path, then read back the reminder or attachment row to verify the attachment.
8. For sections, preserve list-level section membership and ordering. Do not treat a section name as global unless the data proves it is unique.
9. For bulk edits, inspect a reasonable bounded set first. If the current user has granted standing delegation, apply the change and report the exact affected set afterward; otherwise restate the qualifying reminders before applying changes.
10. Use foreground UI automation only as a fallback for verification or unsupported flows. Prefer public APIs and the local background Reminders adapter for normal operation.
11. Surface conflicts, duplicate matches, missing target lists, sync uncertainty, and destructive effects before writing.
12. If the request is still ambiguous after checking for precedent or scanning a reasonable bounded scope, summarize the candidate targets or exact diff before writing anything.

## Write Safety

- Preserve title, notes, due date, reminder alert date, priority, flag, list, section, completion state, tags, URL, and attachments unless the user asked to change them.
- Treat deletes, bulk completion, broad moves, and attachment removal as high-impact actions.
- When standing delegation applies, high-impact writes may be executed without a separate confirmation, but they must be bounded, logged, and verified with a read-back.
- When standing delegation does not apply, restate the qualifying reminder set and scope before applying high-impact writes.
- If multiple similarly named reminders, lists, or sections exist, identify the intended one explicitly before editing.
- Prefer structured local adapter calls over free-form AppleScript or UI gestures.
- Prefer EventKit or AppleScript for public reminder fields.
- Use the SQLite-backed adapter only for Reminders surfaces not exposed through public APIs, such as image attachments and sections.
- The SQLite-backed adapter must run schema checks, use transactions, update related cloud-state rows, and verify with a read-back.
- Deletion must use native Reminders delete behavior so deleted reminders go through Reminders' Recently Deleted flow. Never hard-delete rows directly from the database.
- Do not make direct database writes outside the Reminders group container discovered on the user's machine.
- Keep iCloud sync caveats explicit when a change relies on private storage details.

## Output Conventions

- Name the list and section for each relevant reminder when location matters.
- Use exact dates with weekdays for due or scheduled items.
- When proposing changes, show current state and intended state.
- When reporting attachment work, include the file name and whether the native Reminders UI or database read-back confirmed it.
- When summarizing a large task set, group by operational meaning: overdue, due today, upcoming, unscheduled, waiting, reference, or cleanup candidates.
- Keep recommendations short and actionable. Do not dump raw database rows unless debugging.

## Relevant Future Actions

- `list_accounts`
- `list_lists`
- `list_sections`
- `search_reminders`
- `read_reminder`
- `create_reminder`
- `update_reminder`
- `complete_reminder`
- `move_reminder`
- `create_section`
- `attach_image`
- `backup_store`
- `doctor`

## Example Requests

- "오늘 할 일 브리핑해줘."
- "이 스크린샷을 수강신청 미리알림에 첨부해줘."
- "생각 주머니에서 실행 가능한 것만 골라서 이번 주 섹션으로 정리해줘."
- "🪣 목록 전체를 읽고 비슷한 항목끼리 섹션 제안해줘."
- "급한일 중 마감 지난 것만 보여주고 완료 처리 후보를 알려줘."
- "방금 말한 내용을 To Shop List에 사진과 같이 추가해줘."
