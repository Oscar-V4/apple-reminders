# Apple Reminders Adapter CLI

The local adapter is at `../../scripts/reminders_adapter.py` relative to this skill directory. Invoke it with Python and JSON output:

```bash
python3 ../../scripts/reminders_adapter.py <command> [options]
```

Always run `<command> --help` before using a command you have not recently used. Keep reads bounded with list, section, search, date, status, or limit filters.

## Contribution Boundary

This adapter uses local macOS Reminders storage details, AppleScript, and a private ReminderKit helper for native image attachments. It is suitable for the personal plugin workflow, not for an OpenMinis contribution as-is.

The repository's allowlisted export lives at `minis/apple-reminders/`. Validate it with `python3 scripts/validate_minis_export.py` from the repository root. Do not copy this local skill or include `scripts/reminders_adapter.py`, the Objective-C ReminderKit helper, private database notes, local backups, caches, screenshots, or user Reminders data.

## Commands

Diagnostics and backups:

- `doctor`: inspect Reminders container access, schema, and adapter readiness.
- `backup_store`: create a local backup before risky repair or bulk write flows.

Cache and read paths:

- `cache_rebuild`, `cache_info`, `cache_search`, `cache_query`
- `list_lists`, `list_sections`, `snapshot`
- `search_reminders`, `read_reminder`
- `list_tags`

Writes:

- `create_list`
- `create_reminder`
- `update_reminder`
- `complete_reminder`
- `delete_reminder`
- `create_section`
- `move_to_section`
- `add_tag`, `remove_tag`, `cleanup_tags`

Attachments:

- `attach_image`
- `attach_url`
- `list_attachments`
- `audit_attachments`
- `repair_attachments`
- `delete_attachment`
- `replace_attachment`

## Backend Guidance

- Prefer the default backend for normal commands.
- For `delete_reminder`, prefer `--backend applescript` so deleted items go through native Reminders behavior and Recently Deleted.
- For `attach_image`, prefer the default `--backend reminderkit` path for user-facing image capture. It should report mobile-visible evidence before success is claimed.
- Treat `mobile_visible_likely: true` as sync evidence rather than direct iPhone-screen confirmation.
- Use `attach_image --backend db` only for diagnostics, repair fallback, or local-only cases where iPhone visibility is not required.
- Use SQLite-backed commands for sections, tags, URL attachments, cache reads, audits, and repair flows because public APIs do not expose those surfaces.

## Safety Rules

- Resolve ambiguous reminder/list/section references before writing.
- Use `search_reminders`, `read_reminder`, or `list_attachments` to capture the current state before updates, deletes, moves, and attachment changes.
- For bulk edits or repair flows, run a bounded dry run first. Use `repair_attachments --apply` only after reviewing the affected set and taking a backup unless the user explicitly opts out.
- Do not write directly to a database path outside the discovered Reminders group container.
- Do not report image attachment success from local Mac rendering alone; use mobile sync evidence or native Reminders-created attachment evidence.
- If a command returns partial success, sync uncertainty, multiple matches, or schema warnings, surface that limitation and stop before further writes.
