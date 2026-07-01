# 0005. Single Primary Skill

## Status

Accepted.

## Decision

Start with one primary `apple-reminders` skill.

Do not create purpose-specific Reminders skills in v1.

## Rationale

Google Calendar uses multiple skills because calendar workflows naturally split into scheduling, meeting prep, daily brief, and free-time optimization.

Apple Reminders should start with one strong operating contract because the first challenge is consistent native task management across lists, sections, reminders, notes, and attachments.

Usage patterns should drive later skill extraction.

## Future Candidates

Possible future skills:

- `apple-reminders-daily-brief`
- `apple-reminders-capture`
- `apple-reminders-cleanup`
- `apple-reminders-attachments`

These should be created only after repeated real usage shows that separate workflow instructions improve reliability.
