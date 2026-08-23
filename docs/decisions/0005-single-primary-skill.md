# 0005. Single Primary Skill

## Status

Superseded by [0009. Google Calendar parity with hybrid backends](0009-google-calendar-parity-hybrid-backends.md).

## Decision

The original v1 decision was to start with one primary `apple-reminders` skill.

Purpose-specific skills were intentionally deferred in v1. Version 0.2.0 later
added four small workflow skills after the primary contract and tool surface had
stabilized. They remain routing and presentation layers over the same MCP and do
not duplicate backend implementations.

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
