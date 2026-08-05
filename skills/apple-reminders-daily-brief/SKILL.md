---
name: apple-reminders-daily-brief
description: Build deterministic Apple Reminders daily briefs from semantically bounded MCP reads. Use when the user asks for today, tomorrow, a specific date, overdue items, due-today tasks, this-week tasks, unscheduled work, or a daily/weekly reminder briefing.
---

# Apple Reminders Daily Brief

## Overview

Use this skill to turn a bounded Apple Reminders MCP read into a concise operational brief. Use the bundled deterministic renderer as the default formatting path.

## Workflow

1. Resolve the target date and timezone explicitly. Treat "today" in the user's current local timezone.
2. Use `list_reminder_lists` to obtain the exact intended `calendar_ids`. Then call `fetch_reminders` with those IDs, `status=incomplete`, due sort, and a page limit no greater than 100. This calendar scope includes overdue and no-due-date items.
3. Follow `next_cursor` only with exactly the same IDs, filters, sort, and limit. If the scope is still truncated, say so. A due range without calendar IDs is suitable for a due-window-only brief but cannot establish the no-due-date bucket.
4. Do not access live Reminders data for formatting alone; feed only an already-read MCP/adapter JSON payload into `scripts/render_daily_brief.py`.
5. Run the renderer with explicit `--date` and `--timezone`.
6. Return the rendered Markdown. Add a short caveat only when the read was truncated or the user asked for a narrower focus.

## Adapter Use

Use `fetch_reminders` as the normal read surface. Read `../apple-reminders/references/adapter-cli.md` only for a private diagnostic fallback.

Default tool argument shape after list enumeration:

```json
{
  "calendar_ids": ["EXACT-LIST-ID"],
  "status": "incomplete",
  "limit": 100,
  "sort": "due"
}
```

The renderer accepts JSON from:

- `fetch_reminders` MCP/EventKit responses with an `items` array, including transport wrappers
- `snapshot` with a `reminders` array
- `cache_query` or `cache_search` with a `matches` array
- a bare JSON array of reminder objects

## Bucket Rules

- `Overdue`: incomplete reminders whose typed EventKit `due` or legacy `display_at`/`due_at` local date is before the target date.
- `Due Today`: incomplete reminders on the target date.
- `Later This Week`: incomplete reminders after the target date through the coming Sunday.
- `Upcoming`: incomplete reminders after the week boundary; include only as a count unless the user asks.
- `No Due Date`: incomplete reminders with no `display_at` or `due_at`; cap visible rows at 20 and report omitted count.

Use typed EventKit `due` first; legacy payloads use `display_at` before `due_at`. Preserve reminder titles exactly. Include reminder IDs in compact form so follow-up writes can target exact records.

## Formatter

Run:

```bash
python3 skills/apple-reminders-daily-brief/scripts/render_daily_brief.py \
  --date 2026-08-06 \
  --timezone Asia/Seoul \
  --limit-unscheduled 20
```

Pass the MCP/adapter JSON payload on stdin, or use `--input path/to/payload.json` for tests. Never pass raw database rows containing notes when the bounded public projection is enough.

## Output Rules

- State the date and timezone basis.
- Separate overdue, due today, later this week, and no due date.
- Include list and section when present.
- Include exact IDs; truncate only the visual display, not the source targeting evidence.
- Do not expose note bodies, URLs, attachment file paths, raw SQLite paths, or full JSON rows unless the user specifically asks for diagnostic detail.
