---
name: apple-reminders-daily-brief
description: Build deterministic Apple Reminders briefs from bounded reads. Use for today, tomorrow, a date, overdue, due-today, this-week, completed-range, unscheduled, daily, or weekly task briefings.
---

# Apple Reminders Daily Brief

Build a concise brief from a bounded `fetch_reminders` result. Use the bundled renderer for deterministic grouping and formatting.

## Workflow

1. Resolve the target date and IANA timezone. Interpret “today” in the user's current local timezone.
2. Call `list_reminder_lists` and resolve exact `list_ids`; names are not selectors.
3. For active work, call `fetch_reminders` with those IDs, `status=incomplete`, due sort, and a page limit no greater than 100. This list scope includes overdue and no-due-date reminders.
4. A due-window-only brief may instead use bounded `due_start`/`due_end`, but it cannot establish a no-due-date bucket. Completed work always uses `status=completed` with a bounded `completion_start`/`completion_end` range.
5. Follow `next_cursor` only with identical filters, sort, and limit. State when the scope remains truncated.
6. Feed the structured result to `scripts/render_daily_brief.py` with explicit `--date` and `--timezone`. Do not re-read live Reminders merely to format data already obtained.
7. Return the rendered Markdown, adding only a short caveat for truncation or a deliberately narrow scope.

Default active arguments after list resolution:

```json
{
  "list_ids": ["EXACT-LIST-ID"],
  "status": "incomplete",
  "limit": 100,
  "sort": "due"
}
```

## Buckets

- `Overdue`: incomplete reminders due before the target local date.
- `Due Today`: incomplete reminders due on the target date.
- `Later This Week`: after the target date through the coming Sunday.
- `Upcoming`: after that boundary; show a count unless requested.
- `No Due Date`: incomplete reminders with no due value; show at most 20 and report omissions.

Use typed `due` first. Preserve titles and exact IDs. Treat all-day and timed values by their local display date.

## Renderer

```bash
python3 skills/apple-reminders-daily-brief/scripts/render_daily_brief.py \
  --date 2026-08-06 \
  --timezone Asia/Seoul \
  --limit-unscheduled 20
```

Pass MCP JSON on stdin or use `--input` for a saved test fixture.

## Output

- State the date and timezone basis.
- Include list and section when present and compact exact IDs for follow-up.
- Do not expose note bodies, URLs, attachment paths, raw database paths, or full JSON rows unless the user explicitly asks for targeted detail.
