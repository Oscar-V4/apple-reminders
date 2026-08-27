#!/usr/bin/env python3
"""Render a bounded Apple Reminders daily brief as Markdown."""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
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
    due_at: datetime | None
    completion_raw: str | None
    completion_date: date | None
    all_day: bool
    flagged: bool
    priority: int | None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render an Apple Reminders daily brief from MCP or adapter JSON.")
    parser.add_argument("--date", required=True, help="Target local date as YYYY-MM-DD.")
    parser.add_argument("--timezone", required=True, help="IANA timezone such as Asia/Seoul.")
    parser.add_argument("--input", help="Optional JSON input path. Defaults to stdin.")
    parser.add_argument(
        "--status",
        choices=("incomplete", "completed"),
        default="incomplete",
        help="Render active reminders by default, or an explicitly bounded completed result.",
    )
    parser.add_argument(
        "--completion-start",
        help="RFC 3339 lower bound used for the completed fetch.",
    )
    parser.add_argument(
        "--completion-end",
        help="RFC 3339 upper bound used for the completed fetch.",
    )
    parser.add_argument("--limit-unscheduled", type=int, default=20, help="Maximum no-due-date reminders to show.")
    return parser.parse_args()


def load_payload(path: str | None) -> Any:
    if path:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    return json.load(sys.stdin)


def _failure_code(payload: dict[str, Any], *, depth: int = 4) -> str | None:
    """Return a content-free code when this transport layer reports failure."""

    status = payload.get("status")
    error = payload.get("error")
    failed = (
        payload.get("ok") is False
        or payload.get("isError") is True
        or (isinstance(status, str) and status.startswith("failed_"))
        or isinstance(error, dict)
    )
    if not failed:
        return None
    errors = payload.get("errors")
    if isinstance(errors, list):
        for error in errors:
            if isinstance(error, dict) and isinstance(error.get("code"), str):
                return error["code"]
    if isinstance(error, dict):
        error_code = error.get("code")
        if isinstance(error_code, str):
            return error_code
        if isinstance(error_code, int) and not isinstance(error_code, bool):
            return f"jsonrpc_error_{error_code}"
    if isinstance(status, str):
        return status
    if depth > 0:
        for name in ("structuredContent", "result", "data"):
            candidate = payload.get(name)
            if isinstance(candidate, dict):
                nested_code = _failure_code(candidate, depth=depth - 1)
                if nested_code and nested_code != "unknown_failure":
                    return nested_code
    return "unknown_failure"


def _unwrap_payload(payload: Any) -> Any:
    """Reject failures, then unwrap a small number of transport envelopes."""

    current = payload
    for _ in range(4):
        if not isinstance(current, dict):
            break
        failure_code = _failure_code(current)
        if failure_code:
            raise ValueError(f"Cannot render failed reminder result: {failure_code}")
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
    failure_code = _failure_code(payload)
    if failure_code:
        raise ValueError(f"Cannot render failed reminder result: {failure_code}")
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


def parse_completion_range(
    status: str,
    start_raw: str | None,
    end_raw: str | None,
) -> tuple[datetime, datetime] | None:
    """Require the same explicit completed range that bounded the source fetch."""

    if status not in {"incomplete", "completed"}:
        raise ValueError("status must be incomplete or completed")
    if status == "incomplete":
        if start_raw is not None or end_raw is not None:
            raise ValueError(
                "--completion-start and --completion-end require --status completed"
            )
        return None
    if start_raw is None or end_raw is None:
        raise ValueError(
            "--status completed requires both --completion-start and --completion-end"
        )

    parsed: list[datetime] = []
    for option, value in (
        ("--completion-start", start_raw),
        ("--completion-end", end_raw),
    ):
        try:
            candidate = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{option} must be an RFC 3339 timestamp") from exc
        if candidate.tzinfo is None or candidate.utcoffset() is None:
            raise ValueError(f"{option} must include a UTC offset")
        parsed.append(candidate)

    start, end = parsed
    if start >= end:
        raise ValueError("--completion-start must be earlier than --completion-end")
    if end - start > timedelta(days=90):
        raise ValueError("completed reminder range must not exceed 90 days")
    return start, end


def due_fields(item: dict[str, Any]) -> tuple[str | None, bool, str | None]:
    """Return a due value, all-day marker, and source zone for both contracts."""

    due = item.get("due")
    if isinstance(due, dict):
        if due.get("kind") == "all_day":
            value = due.get("date")
            return (value if isinstance(value, str) else None), True, None
        if due.get("kind") == "timed":
            value = due.get("date_time") or due.get("local_date_time")
            source_zone = due.get("time_zone")
            return (
                value if isinstance(value, str) else None,
                False,
                source_zone if isinstance(source_zone, str) else None,
            )
    value = item.get("display_at") or item.get("due_at")
    source_zone = item.get("timezone")
    return (
        value if isinstance(value, str) else None,
        bool(item.get("display_date_is_all_day") or item.get("all_day")),
        source_zone if isinstance(source_zone, str) else None,
    )


def parse_local_datetime(
    raw: str | None,
    tz: ZoneInfo,
    source_zone: str | None,
) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        source_tz = tz
        if source_zone:
            try:
                source_tz = ZoneInfo(source_zone)
            except (KeyError, ValueError):
                source_tz = tz
        parsed = parsed.replace(tzinfo=source_tz)
    return parsed.astimezone(tz)


def reminder_list_name(item: dict[str, Any]) -> str | None:
    value = item.get("list_title") or item.get("list") or item.get("calendar_title")
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and isinstance(value.get("name"), str):
        return value["name"]
    return None


def completion_value(item: dict[str, Any]) -> str | None:
    value = item.get("completion_date") or item.get("completed_at")
    return value if isinstance(value, str) else None


def normalize_reminders(
    raw_items: list[dict[str, Any]],
    tz: ZoneInfo,
    status: str,
) -> list[Reminder]:
    reminders: list[Reminder] = []
    for index, item in enumerate(raw_items):
        is_completed = item.get("completed") is True
        if (status == "completed" and not is_completed) or (
            status == "incomplete" and is_completed
        ):
            continue
        due_raw, all_day, source_zone = due_fields(item)
        due_at = None if all_day else parse_local_datetime(due_raw, tz, source_zone)
        completion_raw = completion_value(item)
        priority = item.get("priority")
        reminders.append(
            Reminder(
                index=index,
                identifier=str(item.get("id") or item.get("url") or f"row-{index}"),
                title=str(item.get("title") or "(Untitled reminder)"),
                list_name=reminder_list_name(item),
                section=item.get("section"),
                due_raw=due_raw,
                due_date=(
                    parse_local_date(due_raw, tz)
                    if all_day or due_at is None
                    else due_at.date()
                ),
                due_at=due_at,
                completion_raw=completion_raw,
                completion_date=parse_local_date(completion_raw, tz),
                all_day=all_day,
                flagged=bool(item.get("flagged")),
                priority=(
                    priority
                    if isinstance(priority, int) and not isinstance(priority, bool)
                    else None
                ),
            )
        )
    if status == "completed":
        reminders.sort(
            key=lambda item: (
                item.completion_date is None,
                -(item.completion_date.toordinal() if item.completion_date else 0),
                item.list_name or "",
                item.section or "",
                item.title.casefold(),
                item.identifier,
                item.index,
            )
        )
    else:
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
    if reminder.due_at is not None:
        zone = getattr(reminder.due_at.tzinfo, "key", None) or reminder.due_at.tzname()
        return f"{reminder.due_at.strftime('%Y-%m-%d %H:%M')} {zone}"
    return label


def completion_label(reminder: Reminder) -> str:
    if reminder.completion_date is None:
        return "completed; completion date unavailable"
    return f"completed {reminder.completion_date.isoformat()}"


def priority_label(priority: int | None) -> str | None:
    """Explain Apple's 0-9 priority scale without an ambiguous bare pN."""

    if priority is None or priority == 0:
        return None
    if 1 <= priority <= 4:
        level = "high"
    elif priority == 5:
        level = "medium"
    elif 6 <= priority <= 9:
        level = "low"
    else:
        return f"unrecognized priority (Apple/EventKit {priority})"
    return f"{level} priority (Apple/EventKit {priority})"


def inert_markdown_text(value: str) -> str:
    """Render one untrusted Reminders field without activating Markdown."""

    one_line = " ".join(value.splitlines())
    if "@" in one_line:
        longest_backtick_run = max(
            (len(match.group(0)) for match in re.finditer(r"`+", one_line)),
            default=0,
        )
        delimiter = "`" * (longest_backtick_run + 1)
        return f"{delimiter} {one_line} {delimiter}"
    escaped_html = html.escape(one_line, quote=False)
    markdown_punctuation = frozenset(r"\`*_{}[]()#+!|>~$^")
    autolink_punctuation = {
        ".": "&#46;",
        ":": "&#58;",
    }
    rendered = "".join(
        autolink_punctuation.get(
            character,
            f"\\{character}" if character in markdown_punctuation else character,
        )
        for character in escaped_html
    )
    first_text = len(rendered) - len(rendered.lstrip())
    if rendered[first_text : first_text + 1] == "-":
        rendered = f"{rendered[:first_text]}\\-{rendered[first_text + 1:]}"
    return rendered


def line_for(reminder: Reminder, status: str) -> str:
    markers = []
    if reminder.flagged:
        markers.append("flagged")
    rendered_priority = priority_label(reminder.priority)
    if rendered_priority:
        markers.append(rendered_priority)
    suffix = f" ({', '.join(markers)})" if markers else ""
    title = inert_markdown_text(reminder.title)
    location = inert_markdown_text(location_text(reminder))
    identifier = inert_markdown_text(reminder.identifier)
    temporal_label = (
        completion_label(reminder) if status == "completed" else due_label(reminder)
    )
    return f"- {title} [{location}] id: {identifier} - {temporal_label}{suffix}"


def render_section(
    title: str,
    reminders: list[Reminder],
    empty_text: str,
    *,
    status: str,
    limit: int | None = None,
) -> list[str]:
    lines = [f"## {title}"]
    visible = reminders if limit is None else reminders[:limit]
    if visible:
        lines.extend(line_for(item, status) for item in visible)
        if limit is not None and len(reminders) > limit:
            lines.append(f"- Omitted {len(reminders) - limit} more no-due-date reminders.")
    else:
        lines.append(empty_text)
    return lines


def render(
    payload: Any,
    target: date,
    tz: ZoneInfo,
    limit_unscheduled: int,
    *,
    status: str = "incomplete",
    completion_start: str | None = None,
    completion_end: str | None = None,
) -> str:
    completion_range = parse_completion_range(
        status,
        completion_start,
        completion_end,
    )
    raw_items, truncated, total_matches = payload_reminders(payload)
    reminders = normalize_reminders(raw_items, tz, status)

    if status == "completed":
        assert completion_range is not None
        range_start, range_end = (
            value.astimezone(tz).isoformat() for value in completion_range
        )
        lines = [
            f"# Apple Reminders Brief - {target.isoformat()} ({tz.key})",
            "",
            f"Completed reminders reviewed: {len(reminders)}"
            + (f" of {total_matches}" if total_matches is not None else "")
            + ("; source was truncated" if truncated else ""),
            f"Completion range: {range_start} to {range_end}",
            "",
        ]
        lines.extend(
            render_section(
                "Completed",
                reminders,
                "No completed reminders in this bounded read.",
                status=status,
            )
        )
        return "\n".join(lines).rstrip() + "\n"

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
    lines.extend(render_section("Overdue", overdue, "None.", status=status))
    lines.append("")
    lines.extend(
        render_section("Due Today", today, "Nothing due today.", status=status)
    )
    lines.append("")
    lines.extend(
        render_section(
            f"Later This Week Through {week_end.isoformat()}",
            later_week,
            "Nothing else due this week.",
            status=status,
        )
    )
    lines.append("")
    lines.extend(
        render_section(
            "No Due Date",
            unscheduled,
            "No unscheduled reminders in this bounded read.",
            status=status,
            limit=limit_unscheduled,
        )
    )
    if upcoming:
        lines.append("")
        lines.append(f"Upcoming after this week: {len(upcoming)} not shown by default.")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    try:
        args = parse_args()
        target = date.fromisoformat(args.date)
        tz = ZoneInfo(args.timezone)
        payload = load_payload(args.input)
        output = render(
            payload,
            target,
            tz,
            args.limit_unscheduled,
            status=args.status,
            completion_start=args.completion_start,
            completion_end=args.completion_end,
        )
    except (OSError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 2
    sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
