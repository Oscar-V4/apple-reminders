# Apple Reminders Codex Plugin

Apple Reminders plugin prototype for Codex. The goal is a local assistant layer that can understand and manage the native macOS Reminders app with the same discipline as the Google Calendar plugin: read real state first, keep scans bounded, propose exact changes, and apply writes through structured tools.

## Current Status

- Plugin scaffold exists.
- Main `apple-reminders` skill contract is drafted.
- Local reverse-engineering has proven that reminders, image attachments, and sections can be manipulated without foreground UI gestures.
- MCP/tool implementation is not complete yet.

## Intended Shape

- Skill layer: planning, safety, output conventions, bounded reads, and write policy.
- Local adapter layer: AppleScript/EventKit for public reminder fields; SQLite adapter for Reminders-only surfaces such as image attachments and sections.
- Verification layer: schema checks, transaction backups, dry-run previews, and post-write reads.
- Optional MCP shim: a thin wrapper only if Codex needs first-class tool calls. The adapter should work without it.

## Reference

This plugin intentionally follows the structure of the Google Calendar plugin:

- product-level plugin manifest
- a primary skill that encodes workflow and write safety
- future purpose-specific skills for review, capture, cleanup, and daily task briefs
- connector/tool backing for actual reads and writes

Google Calendar gets its tool backing from a hosted Codex app connector. Apple Reminders should use a local background adapter instead, because it is a native macOS app and the useful data is already on the user's machine.

## Safety Notes

The macOS Reminders app exposes only part of its model through public APIs. Image attachments and sections currently require a local adapter over the Reminders store. That adapter must stay narrow, transactional, schema-checked, and easy to disable.
