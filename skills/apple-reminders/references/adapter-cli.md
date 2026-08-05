# Apple Reminders MCP and Adapter Reference

## Preferred Surface

The bundled stdio MCP declared by `.mcp.json` is the normal agent-facing interface. Its canonical tool definitions live in `schemas/mcp-tools.json`; inspect that schema rather than translating user requests into shell commands. Public account/list/reminder operations route to EventKit, while Reminders-specific sections, tags, and attachments route to the private adapter.

First-run tools are deliberately separate:

- `reminders_plugin_doctor`: content-free local readiness and schema report; it does not request permission or return reminder/account content.
- `get_reminders_capabilities`: EventKit capability and current authorization state.
- `request_reminders_access`: the only explicit EventKit TCC prompt.

Public reads use `list_reminder_accounts`, `list_reminder_lists`, `fetch_reminders`, and `read_reminder`. Existing-reminder EventKit writes require the exact ID plus `expected_last_modified`; create requires an idempotency key. `fetch_reminders` requires a native semantic bound in addition to a limit: calendar IDs, an incomplete due range, or a completed completion range. Its opaque cursor is valid only with unchanged filters, sort, and page size.

The rest of this reference documents the lower-level private implementation and maintenance escape hatch.

## Adapter CLI

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
- `purge_logs`: remove the current redacted action journal and rotated journals.

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
- `reopen_reminder`
- `delete_reminder`
- `show_reminder`
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

- Prefer MCP/EventKit for public reminder fields. Direct adapter create/update/complete commands remain compatibility and private-diagnostic paths.
- Keep due values and alerts separate. The private SQLite path rejects `--remind-at`; use typed EventKit `due` and `alarms` instead of guessing a mapping.
- EventKit does not expose the native Reminders flag field. If a user explicitly asks to set or clear a flag, use an exact private `read_reminder --id` to obtain `version`, then `update_reminder --backend applescript --id ... --flagged/--no-flagged --if-version ...`, and read back. Never claim the typed `create_reminder` wrote a flag.
- For `delete_reminder`, keep the default `--backend auto`. It chooses DB soft-delete only when a capability record proves recovery and sync parity for the exact OS, Reminders build, and schema fingerprint; otherwise it uses AppleScript. Never retry through DB after an uncertain native attempt.
- For `attach_image`, prefer the default `--backend reminderkit` path for user-facing image capture. It should report mobile-visible evidence before success is claimed.
- Treat `mobile_visible_likely: true` as sync evidence rather than direct iPhone-screen confirmation.
- Use `attach_image --backend db` only for diagnostics, repair fallback, or local-only cases where iPhone visibility is not required.
- Use SQLite-backed commands for sections, tags, URL attachments, cache reads, audits, and repair flows because public APIs do not expose those surfaces.
- `cleanup_tags --apply` is an intentional label-row hard-delete maintenance operation. It requires tag/prefix scope plus the exact digest from its preview; candidates are escaped literally, account-aware when requested, locked, revalidated as zero-reference, and read back. Multi-label cleanup takes a container backup by default; a single removed label is treated as recreatable. Local verification does not prove iCloud propagation.
- `repair_attachments --apply` likewise requires the exact `--preview-digest` from an untruncated dry run and takes a backup by default.

## MCP Attachment Tools

- Resolve the exact reminder, then call `list_reminder_attachments`; its response includes the private `reminder_version` needed by concurrency-checked image and replacement writes.
- `attach_image_to_reminder` requires an absolute `image_path`, exact ID, `if_version`, and idempotency key. It intentionally does not expose the SQLite image backend.
- `replace_reminder_attachment` requires the exact old attachment ID, exact reminder ID/version, idempotency key, and exactly one new image path or URL.
- `preview_reminder_attachment_repairs` returns the candidate digest consumed by `apply_reminder_attachment_repairs`. Keep its default backup unless the user explicitly accepts reduced recovery.

## Safety Rules

- Resolve ambiguous reminder/list/section references before writing.
- Use bounded MCP reads, `read_reminder`, or `list_attachments` to capture the current state and concurrency token before updates, deletes, moves, and attachment changes.
- For bulk cleanup or repair flows, run a bounded dry run first and apply only with its unchanged candidate digest. Keep the default backup unless the user explicitly opts out.
- Do not write directly to a database path outside the discovered Reminders group container.
- Do not report image attachment success from local Mac rendering alone; use mobile sync evidence or native Reminders-created attachment evidence.
- If a command returns partial success, sync uncertainty, multiple matches, or schema warnings, surface that limitation and stop before further writes.
- Successful mutation receipts use only `unchanged`, `verified`, `committed_verification_pending`, or `partial_success`. Values such as `persisted_sync_pending` belong inside verification details. Failures use a stable error code and are never success receipts.
