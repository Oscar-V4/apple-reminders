---
name: apple-reminders-quick-capture
description: Capture new Apple Reminders with exact fields and read-back verification. Use when the user wants to add, jot down, save, capture, or create a reminder, optionally with a list, notes, URL, image, priority, due date, alarm, recurrence, or all-day date.
---

# Apple Reminders Quick Capture

## Overview

Use this skill when the user is adding new work to Reminders. Optimize for preserving the user's wording, choosing the right list, and producing a verified receipt rather than a loose confirmation.

## Workflow

1. Normalize the requested title, list, notes, priority, due date, alarms, recurrence, and timezone. Convert relative dates into exact local dates or ISO datetimes before writing.
2. Read bounded list state first with `list_reminder_lists`. If list choice depends on recent structure, use `fetch_reminders` with explicit `calendar_ids` and a bounded limit.
3. If the target list is missing or ambiguous, show candidate lists and stop. Do not invent a list unless the user asked to create one.
4. Call `create_reminder` with the exact `calendar_id`, preserved title, and a fresh idempotency key. Include only fields the user supplied or clearly delegated. When a URL is supplied, pass it in the same call; the tool writes the EventKit value and ensures a visible native URL attachment before returning a fully verified receipt.
5. Encode all-day and timed due values with the typed `due` object. Encode alerts separately in `alarms`; never turn a due date into an alarm unless the user asked to be notified. A timed due value requires an RFC 3339 offset and IANA timezone.
6. If the create receipt is `verified`, report the exact ID and written fields. If it is `partial_success` or `committed_verification_pending`, surface its verification and recovery objects instead of saying the task is fully done.
7. Read the exact created ID with `read_reminder` before reporting final success.

## Command Guidance

Use the typed MCP schema as the source of truth. Read `../apple-reminders/references/adapter-cli.md` only for a private maintenance fallback.

Typical timed create arguments:

```json
{
  "calendar_id": "EXACT-LIST-ID",
  "title": "Submit project",
  "due": {
    "kind": "timed",
    "date_time": "2026-08-06T19:00:00+09:00",
    "time_zone": "Asia/Seoul"
  },
  "idempotency_key": "NEW-KEY-FOR-THIS-CAPTURE"
}
```

For an all-day due date use `{"kind":"all_day","date":"YYYY-MM-DD"}`. Absolute alarms use their own RFC 3339 `date_time`; location alarms require explicit coordinates and `enter` or `leave` proximity. Only one validated recurrence rule is supported, and recurrence requires a due date. Relative alarms are not supported; do not guess an anchor.

## Attachments During Capture

- For image capture, resolve an absolute regular non-symlink PNG or JPEG within the documented byte and pixel limits, then resolve the target reminder, call `list_reminder_attachments` for a fresh `reminder_version`, and pass it as `if_version` to `attach_image_to_reminder` through the attachment maintenance rules.
- A URL supplied to `create_reminder` is a combined write: EventKit metadata plus a visible native URL attachment. Do not follow a `verified` create with a redundant `attach_url_to_reminder` call.
- For a newly requested URL on an existing reminder, use `update_reminder` with a non-null `patch.url`; it likewise ensures the visible attachment. Use `attach_url_to_reminder` directly only for an additional URL attachment or to recover from a reported `partial_success`, after obtaining a fresh `reminder_version`.
- Clearing `patch.url` removes only EventKit URL metadata. Never infer that the user also wants existing URL attachments deleted; attachment deletion remains explicit.
- Do not claim iPhone image visibility from local rendering. Report `mobile_visible_likely` as sync evidence only.

## Output Rules

- Return title, list, due/display date, alarm summary, recurrence, priority if set, and exact reminder ID.
- Mention preserved omissions: no notes, no date, or no attachment when the user did not provide them.
- Keep note bodies out of the response unless they are short and the user asked to confirm them.
- For failures, include the adapter status/code and the next safe action.
