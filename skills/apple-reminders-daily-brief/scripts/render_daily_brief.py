#!/usr/bin/env python3
"""Render a bounded Apple Reminders daily brief as Markdown."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Reminder:
    index: int
    identifier: str
    title: str
    list_name: str | None
    section: str | None
    due_raw: str | None
    due_date: date | None
    all_day: bool
    flagged: bool
    priority: int | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render an Apple Reminders daily brief from MCP or adapter JSON.")
    parser.add_argument("--date", required=True, help="Target local date as YYYY-MM-DD.")
    parser.add_argument("--timezone", required=True, help="IANA timezone such as Asia/Seoul.")
    parser.add_argument("--input", help="Optional JSON input path. Defaults to stdin.")
    parser.add_argument("--limit-unscheduled", type=int, default=20, help="Maximum no-due-date reminders to show.")
    return parser.parse_args()


def load_payload(path: str | None) -> Any:
    if path:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    return json.load(sys.stdin)


def _unwrap_payload(payload: Any) -> Any:
    """Unwrap a small number of known transport envelopes."""

    current = payload
    for _ in range(4):
        if not isinstance(current, dict):
            break
        if any(name in current for name in ("reminders", "matches", "items")):
            break
        next_value = None
        for name in ("structuredContent", "result", "data"):
            candidate = current.get(name)
            if isinstance(candidate, (dict, list)):
                next_value = candidate
                break
        if next_value is None:
            break
        current = next_value
    return current


def payload_reminders(payload: Any) -> tuple[list[dict[str, Any]], bool, int | None]:
    payload = _unwrap_payload(payload)
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)], False, None
    if not isinstance(payload, dict):
        raise ValueError("Expected a JSON object or an array of reminder objects.")
    raw = payload.get("reminders")
    if raw is None:
        raw = payload.get("matches")
    if raw is None:
        raw = payload.get("items")
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ValueError("Expected reminders, matches, or items to be an array.")
    total = payload.get("total_matches", payload.get("total_matched"))
    mcp_meta = payload.get("_mcp")
    transport_truncated = (
        bool(payload.get("truncated"))
        or bool(payload.get("has_more"))
        or bool(payload.get("next_cursor"))
        or (isinstance(mcp_meta, dict) and bool(mcp_meta.get("truncated")))
    )
    return [item for item in raw if isinstance(item, dict)], transport_truncated, total if isinstance(total, int) else None


def parse_local_date(raw: str | None, tz: ZoneInfo) -> date | None:
    if not raw:
        return None
    value = raw.strip()
    try:
        if len(value) == 10 and value[4] == "-" and value[7] == "-":
            return date.fromisoformat(value)
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz).date()


def due_fields(item: dict[str, Any]) -> tuple[str | None, bool]:
    """Return one date-like value plus an all-day marker for both contracts."""

    due = item.get("due")
    if isinstance(due, dict):
        if due.get("kind") == "all_day":
            value = due.get("date")
            return (value if isinstance(value, str) else None), True
        if due.get("kind") == "timed":
            value = due.get("date_time") or due.get("local_date_time")
            return (value if isinstance(value, str) else None), False
    value = item.get("display_at") or item.get("due_at")
    return (
        value if isinstance(value, str) else None,
        bool(item.get("display_date_is_all_day") or item.get("all_day")),
    )


def reminder_list_name(item: dict[str, Any]) -> str | None:
    value = item.get("list") or item.get("calendar_title")
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("name"), str):
        return value["name"]
    return None


def normalize_reminders(raw_items: list[dict[str, Any]], tz: ZoneInfo) -> list[Reminder]:
    reminders: list[Reminder] = []
    for index, item in enumerate(raw_items):
        if item.get("completed") is True:
            continue
        due_raw, all_day = due_fields(item)
        reminders.append(
            Reminder(
                index=index,
                identifier=str(item.get("id") or item.get("url") or f"row-{index}"),
                title=str(item.get("title") or "(Untitled reminder)"),
                list_name=reminder_list_name(item),
                section=item.get("section"),
                due_raw=due_raw,
                due_date=parse_local_date(due_raw, tz),
                all_day=all_day,
                flagged=bool(item.get("flagged")),
                priority=item.get("priority") if isinstance(item.get("priority"), int) else None,
            )
        )
    reminders.sort(
        key=lambda item: (
            item.due_date or date.max,
            item.list_name or "",
            item.section or "",
            item.title.casefold(),
            item.identifier,
            item.index,
        )
    )
    return reminders


def week_end_for(target: date) -> date:
    return target.replace(day=target.day) if target.weekday() == 6 else target.fromordinal(target.toordinal() + (6 - target.weekday()))


def location_text(reminder: Reminder) -> str:
    parts = [part for part in (reminder.list_name, reminder.section) if part]
    return " / ".join(parts) if parts else "No list"


def due_label(reminder: Reminder) -> str:
    if reminder.due_date is None:
        return "no due date"
    label = reminder.due_date.isoformat()
    if reminder.all_day:
        return f"{label} all-day"
    return label


def line_for(reminder: Reminder) -> str:
    markers = []
    if reminder.flagged:
        markers.append("flagged")
    if reminder.priority:
        markers.append(f"p{reminder.priority}")
    suffix = f" ({', '.join(markers)})" if markers else ""
    return f"- {reminder.title} [{location_text(reminder)}] id: {reminder.identifier} - {due_label(reminder)}{suffix}"


def render_section(title: str, reminders: list[Reminder], empty_text: str, *, limit: int | None = None) -> list[str]:
    lines = [f"## {title}"]
    visible = reminders if limit is None else reminders[:limit]
    if visible:
        lines.extend(line_for(item) for item in visible)
        if limit is not None and len(reminders) > limit:
            lines.append(f"- Omitted {len(reminders) - limit} more no-due-date reminders.")
    else:
        lines.append(empty_text)
    return lines


def render(payload: Any, target: date, tz: ZoneInfo, limit_unscheduled: int) -> str:
    raw_items, truncated, total_matches = payload_reminders(payload)
    reminders = normalize_reminders(raw_items, tz)
    week_end = week_end_for(target)

    overdue = [item for item in reminders if item.due_date and item.due_date < target]
    today = [item for item in reminders if item.due_date == target]
    later_week = [item for item in reminders if item.due_date and target < item.due_date <= week_end]
    upcoming = [item for item in reminders if item.due_date and item.due_date > week_end]
    unscheduled = [item for item in reminders if item.due_date is None]

    lines = [
        f"# Apple Reminders Brief - {target.isoformat()} ({tz.key})",
        "",
        f"Active reminders reviewed: {len(reminders)}"
        + (f" of {total_matches}" if total_matches is not None else "")
        + ("; source was truncated" if truncated else ""),
        f"Overdue {len(overdue)} | Due today {len(today)} | Later this week {len(later_week)} | Upcoming {len(upcoming)} | No due date {len(unscheduled)}",
        "",
    ]
    lines.extend(render_section("Overdue", overdue, "None."))
    lines.append("")
    lines.extend(render_section("Due Today", today, "Nothing due today."))
    lines.append("")
    lines.extend(render_section(f"Later This Week Through {week_end.isoformat()}", later_week, "Nothing else due this week."))
    lines.append("")
    lines.extend(render_section("No Due Date", unscheduled, "No unscheduled reminders in this bounded read.", limit=limit_unscheduled))
    if upcoming:
        lines.append("")
        lines.append(f"Upcoming after this week: {len(upcoming)} not shown by default.")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    args = parse_args()
    target = date.fromisoformat(args.date)
    tz = ZoneInfo(args.timezone)
    payload = load_payload(args.input)
    sys.stdout.write(render(payload, target, tz, args.limit_unscheduled))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
