# 0002. Hybrid Read Policy

## Status

Accepted.

## Decision

Use a hybrid read policy:

- Assistant-style reasoning starts with a lightweight global index.
- Explicit single-target actions use a narrow partial read.
- Deep reads are performed only for the relevant lists, sections, reminders, notes, and attachments.

## Rationale

Apple Reminders is not organized primarily by time windows like a calendar. Useful context can be spread across lists, sections, unscheduled reminders, notes, and attachments.

However, always deep-reading every reminder and attachment would be slow and noisy.

The adapter should therefore maintain a lightweight map of the Reminders store:

- accounts
- lists
- sections
- reminder titles
- completion state
- due and reminder dates
- priority and flags
- parent/list/section membership
- attachment presence and counts
- notes presence or length

Then it should deep-read only when the task needs it.

## Examples

Use global index first:

- "오늘 할 일 브리핑해줘"
- "내 Reminders 전체 정리 방향 잡아줘"
- "비슷한 항목끼리 섹션 제안해줘"
- "방치된 미리알림 찾아줘"

Use narrow partial read first:

- "이 사진을 방금 만든 미리알림에 붙여줘"
- "수강신청 목록에 이 항목 추가해줘"
- "이 미리알림 완료 처리해줘"

## Consequences

- The adapter needs a `snapshot` or `index` command.
- The adapter also needs targeted reads such as `read_reminder`, `list_sections`, and `search_reminders`.
- The skill should decide which path fits the user's request before reading.
