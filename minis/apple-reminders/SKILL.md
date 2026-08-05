---
name: apple-reminders
description: >
  Manage native iOS Apple Reminders from Minis with the built-in
  apple-reminders command. Use for reminder briefs, list queries, task capture,
  due-date or notes updates, completion, undo-completion, and safe deletion.
---

# Apple Reminders for Minis

Use the built-in `apple-reminders` CLI. Run `apple-reminders --help` before a
command whose current options are uncertain. Do not install a macOS adapter or
access private Reminders storage.

## Read Safely

1. Read before every update, completion, or deletion so the target comes from
   current data.
2. Start with `apple-reminders list --incomplete --limit 100`. Add
   `--list "<name>"` whenever the user names a list.
3. If the result is truncated, report the limit. Narrow by list or ask before
   widening instead of dumping all reminders.
4. Use reminder IDs from list output for mutations. When titles are duplicated
   or the intended target is unclear, show the bounded candidates and do not
   guess.
5. Keep notes out of summaries unless they are necessary for the user's request
   or to distinguish otherwise identical candidates.

## Interpret Dates

- Resolve relative dates in the device's current timezone and state the date
  basis in briefs.
- For a daily brief, include active reminders only by default. Treat items due
  before today's local midnight as overdue, items on today's local date as due
  today, and “this week” as after today through the coming Sunday. If the user
  says “next seven days,” use a rolling seven-day window instead.
- Treat all-day and timed reminders by their local display date.
- Show at most 20 no-due-date reminders in a brief, grouped by list, and report
  how many additional items were omitted.

## Write and Verify

- Create with `apple-reminders create --title "<title>"` plus only the due
  date, list, priority, and notes the user supplied or clearly delegated.
- Update with `apple-reminders update --id <id>` and preserve fields the user
  did not ask to change.
- Complete with `apple-reminders complete --id <id>`; use `--undo` only when
  the user asks to mark it incomplete again.
- Delete with `apple-reminders delete --id <id>` only after resolving one exact
  active reminder. For ambiguous or bulk deletion, present the bounded target
  set before writing unless the user already gave explicit scope and
  delegation.
- Read the affected scope again after a write. Report the exact reminder ID and
  changed fields; never infer success only from a zero exit status.

## Supported Boundary

This Minis integration supports `list`, `create`, `update`, `complete`, and
`delete`. Native sections, tags, image or URL attachments, attachment repair,
and subtasks are not available through this command surface. Explain the
limitation and suggest a feature request instead of inventing a command,
editing private storage, or claiming an unsupported action succeeded.

## Output

- Include list names when they disambiguate reminders.
- Use exact dates and weekdays for scheduled items.
- Separate current state from proposed or completed changes.
- Keep raw JSON, local paths, and unrelated note contents out of the user-facing
  response.
