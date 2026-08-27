---
name: apple-reminders-quick-capture
description: Capture new Apple Reminders with exact fields and verified results. Use when the user wants to add, jot down, save, capture, or create a reminder with an optional list, note, URL, image, priority, due date, alarm, recurrence, or all-day date.
---

# Apple Reminders Quick Capture

Preserve the user's wording, choose an exact destination, and create at most once.

## Workflow

1. Normalize only the requested title, list, notes, priority, due value, alarms, recurrence, and timezone. Do not invent missing fields.
2. Call `list_reminder_lists`. If the destination is missing or duplicate by display name, show exact account/list candidates. Do not create a list unless asked.
3. If asked to create a list, use `ensure_reminder_list` with exact `source_id`, exact name, and a fresh idempotency key. The public interface does not set list color or emblem.
4. Call `create_reminder` with exact `list_id`, preserved title, and a fresh idempotency key. Include only supplied or clearly delegated fields.
5. Use typed due values. A timed due includes RFC 3339 offset and IANA timezone. Alerts belong in `alarms`; never turn a due date into an alarm without a notification request.
6. Pass a supplied URL in the same create call. A verified result already includes the visible native URL attachment and final exact read; do not attach it again.
7. On `verified` or `unchanged`, report the returned exact state, ID, and fresh reference. An extra `read_reminder` is unnecessary unless the returned result lacks a final reference.
8. On `partial_success` or `committed_verification_pending`, report the limitation and perform only the indicated read-only resolution step before any retry.

Quick capture creates one new Reminder; it does not recreate a deleted Reminder as a substitute for preserving identity. Route a restore request through exact `inspect_recently_deleted` item inspection and `recover_deleted_reminder` instead.

Timed example:

```json
{
  "list_id": "EXACT-LIST-ID",
  "title": "Submit project",
  "due": {
    "kind": "timed",
    "date_time": "2026-08-06T19:00:00+09:00",
    "time_zone": "Asia/Seoul"
  },
  "idempotency_key": "UNIQUE-KEY-FOR-THIS-CAPTURE"
}
```

All-day due: `{"kind":"all_day","date":"YYYY-MM-DD"}`. Absolute alarms use their own RFC 3339 `date_time`; location alarms require explicit coordinates and `enter` or `leave`. One recurrence rule is supported and requires a due value. Relative alarms are unsupported.

## Image follow-up

- Create the Reminder first. Use the returned fresh reference with `change_reminder_attachment` and action `attach_image`.
- Resolve exactly one absolute regular non-symlink PNG or JPEG within the 25 MiB, 16,384-pixel-per-dimension, and 40,000,000-pixel limits; include a fresh attachment idempotency key.
- Treat `mobile_visible_likely` as sync evidence, not direct iPhone confirmation.

When a newly created Reminder is the destination for consolidation, verify the create first. For each image that already belongs to another active Reminder, read both exact Reminders and use `change_reminder_attachment` action `copy_image` with the fresh destination `reference`, fresh `source_reference`, exact source image `attachment_id`, and a unique idempotency key. Both input references are consumed after dispatch; use the returned fresh destination reference and re-read the source before the next copy. Finish and verify all copies before any source deletion.

For a URL on an existing Reminder, call `read_reminder` and use `change_reminder` with a `patch.url`. Use `change_reminder_attachment` with `attach_url` only for an additional attachment or explicit recovery from a partial hybrid write.

## Output

- Report title, list, due value, alarm summary, recurrence, priority when set, exact ID, and Receipt status.
- Do not echo long note bodies. Mention omitted fields only when that prevents a likely misunderstanding.
