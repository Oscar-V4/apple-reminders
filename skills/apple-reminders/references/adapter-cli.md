# Apple Reminders MCP and Adapter Reference

## Preferred Surface

The bundled stdio MCP declared by `.mcp.json` is the normal agent-facing interface. Its canonical tool definitions live in `schemas/mcp-tools.json`; inspect that schema rather than translating user requests into shell commands. Public account/list/reminder operations route to EventKit, while Reminders-specific sections, tags, and attachments route to the private adapter.

First-run tools are deliberately separate:

- `reminders_plugin_doctor`: content-free local readiness and schema report; summary is the default, while `detail_level=full` preserves the existing troubleshooting report. It does not request permission or return reminder/account content.
- `get_reminders_capabilities`: EventKit capability and current authorization state.
- `request_reminders_access`: the only explicit EventKit TCC prompt.

Public reads use `list_reminder_accounts`, `list_reminder_lists`, `fetch_reminders`, and `read_reminder`. Existing-reminder EventKit writes require the exact ID plus `expected_last_modified`; create requires an idempotency key. `fetch_reminders` requires a native semantic bound in addition to a limit: calendar IDs, an incomplete due range, or a completed completion range. Its opaque cursor is valid only with unchanged filters, sort, and page size.

A non-null URL passed to the public MCP create/update tools is a deliberate hybrid operation: after EventKit succeeds, the MCP obtains a fresh private reminder version and verifies the matching native URL attachment. Call `attach_url_to_reminder` directly only for extra URL attachments or explicit recovery from `partial_success`.

The rest of this reference documents the lower-level private implementation and maintenance escape hatch.

## Adapter CLI

The local adapter is at `../../scripts/reminders_adapter.py` relative to this skill directory. Invoke it with Python and JSON output:

```bash
python3 ../../scripts/reminders_adapter.py <command> [options]
```

Always run `<command> --help` before using a command you have not recently used. Keep reads bounded with list, section, search, date, status, or limit filters.

## Contribution Boundary

This adapter uses local macOS Reminders storage details, AppleScript, and private ReminderKit helpers for native image attachments and list-section saves. It is suitable for the personal plugin workflow, not for an OpenMinis contribution as-is.

The repository's allowlisted export lives at `minis/apple-reminders/`. Validate it with `python3 scripts/validate_minis_export.py` from the repository root. Do not copy this local skill or include `scripts/reminders_adapter.py`, the Objective-C ReminderKit helper, private database notes, local backups, caches, screenshots, or user Reminders data.

## Commands

Diagnostics and backups:

- `doctor`: inspect Reminders container access, schema, and adapter readiness.
- `backup_store`: create a whole-container archive for cross-store recovery work. Managed archives keep at most two/300 MB; explicit output paths are not auto-pruned.
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

The direct adapter reminder create/update/complete/reopen/delete commands above
are deprecated compatibility paths in 0.2.x. Use their typed MCP/EventKit tools
for normal work. They remain functional until a separately reviewed 0.3.0
breaking removal.

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
- Private existing-item mutations require `--if-version` from a fresh `list_attachments` read. Use `--limit 1` when only the token is needed. This includes tag assignment, section moves, URL/image attachment, attachment replacement, and attachment removal. The DB branches of update, complete, and reopen enforce the same precondition; native AppleScript compatibility branches remain optional.
- Normal MCP deletion does not use this adapter command; it uses public EventKit with a fresh `expected_last_modified`. Keep adapter `delete_reminder --backend auto/db` for compatibility or diagnostic parity work only. Its DB path still requires a fresh `--if-version` plus exact environment evidence, and an uncertain native attempt must never fall through to DB.
- For `attach_image`, prefer the default `--backend reminderkit` path for user-facing image capture. It should report mobile-visible evidence before success is claimed.
- Treat `mobile_visible_likely: true` as sync evidence rather than direct iPhone-screen confirmation.
- Use `attach_image --backend db` only for diagnostics, repair fallback, or local-only cases where iPhone visibility is not required.
- Section creation and section membership moves use the native `remkit_sections.m` save path and verify the relevant CloudKit version. Direct SQLite section writes are diagnostic-only and report `committed_verification_pending`; a local row is not mobile-sync evidence.
- Use SQLite-backed commands for tags, URL attachments, cache reads, audits, and repair flows because public APIs do not expose those surfaces.
- URL attachment replace/delete mirrors the native app: retain the attachment cloud-state tombstone, remove the old URL object row, and preserve the selected display order on replacement. Image-object removal remains a soft-delete and never hard-deletes copied files.
- `cleanup_tags --apply` is an intentional label-row hard-delete maintenance operation. It requires tag/prefix scope plus the exact digest from its preview; candidates are escaped literally, account-aware when requested, locked, revalidated as zero-reference, and read back. Multi-label cleanup takes a single-database SQLite online backup by default; managed database backups keep at most five/100 MB. A single removed label is treated as recreatable. Local verification does not prove iCloud propagation.
- `repair_attachments --apply` likewise requires the exact `--preview-digest` from an untruncated dry run and takes a whole-container backup by default. Managed container archives keep at most two/300 MB.

## MCP Attachment Tools

- Resolve the exact reminder, then call `list_reminder_attachments`; its response includes the private `reminder_version` needed by every concurrency-checked existing-item attachment write.
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
