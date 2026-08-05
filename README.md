# Apple Reminders Codex Plugin

Apple Reminders is a local Codex plugin prototype for managing the native macOS Reminders app. It is designed for personal desktop use: read real Reminders state first, keep scans bounded, propose exact changes, apply writes through structured local commands, and verify results with read-back evidence.

This repository is not a drop-in OpenMinis contribution. OpenMinis runs skills inside a mobile Linux sandbox and already exposes an iOS native `apple-reminders` command. A public MinisSkills contribution should be exported separately as a smaller skill that targets that built-in command surface.

## Status

- Plugin manifest and primary `apple-reminders` skill are present.
- Local adapter exposes a JSON-friendly CLI plus a disposable lightweight cache.
- Reads cover reminders, lists, sections, tags, dates, completion state, priority, flags, URL attachments, image attachment metadata, and cache queries.
- Writes cover reminders, sections, tags, URLs, attachment soft-delete/replacement, and mobile-visible image attachments.
- Image attachment handling is mobile-first: the default path uses a ReminderKit background helper, and SQLite-only image rows are treated as local-only repair candidates unless CloudKit evidence proves otherwise.
- No MCP server or Codex app connector is currently bundled. The plugin uses local scripts and skill instructions.

## Requirements

- macOS with Apple's Reminders app and a populated local Reminders store.
- Python 3.10 or newer.
- `osascript` for AppleScript-based public Reminders operations.
- `sips` for image dimension reads on the SQLite diagnostic path.
- Xcode command line tools, including `clang`, for the ReminderKit image helper.
- Local filesystem access to `~/Library/Group Containers/group.com.apple.reminders/Container_v1/`.
- Reminders/iCloud permissions configured for the current macOS user.

## Architecture

- Skill layer: planning, safety, output conventions, bounded reads, and write policy.
- Local adapter layer: AppleScript/EventKit for public reminder fields and final title/body UI sync; ReminderKit for mobile-visible image attachments; SQLite adapter for Reminders-only surfaces such as sections, tags, URL attachments, cache reads, and repair/audit flows.
- Disposable cache layer: rebuildable JSON under `~/Library/Caches/apple-reminders-codex/` for lightweight list, section, reminder, tag, date, completion, priority, flag, image/URL attachment-count, and notes length/hash scans.
- Verification layer: schema checks, transaction backups, dry-run previews, action journaling, and post-write reads.

## Adapter CLI

The local adapter is `scripts/reminders_adapter.py`. It is JSON-in/JSON-out friendly and can be wrapped by MCP later without moving business logic out of the adapter.

Read and support commands:

- `doctor`
- `snapshot`
- `list_lists`
- `list_sections`
- `search_reminders`
- `read_reminder`
- `list_tags`
- `backup_store`
- `cache_rebuild`
- `cache_info`
- `cache_search`
- `cache_query`
- `audit_attachments`

Write commands:

- `create_list`
- `create_reminder`
- `update_reminder`
- `complete_reminder`
- `delete_reminder`
- `create_section`
- `move_to_section`
- `attach_image`
- `attach_url`
- `add_tag`
- `remove_tag`
- `cleanup_tags`
- `list_attachments`
- `repair_attachments`
- `delete_attachment`
- `replace_attachment`

`cache_rebuild` reads the Reminders database and writes only the disposable cache. It does not write to the Reminders store. Cache search is intentionally lightweight: it searches cached IDs, titles, list names, section names, and cached date strings, but it does not store or search full note bodies.

Date/time support covers timed reminders and all-day due dates. DB-created reminders immediately sync title/body back through AppleScript because native Reminders can otherwise render a newly inserted row with attachments/date but no visible text. Image attachments use the default `attach_image --backend reminderkit` path, which creates native attachment objects with CloudKit server-record evidence and is the expected path for iPhone-facing capture; image replacement uses the same ReminderKit path before soft-deleting the old attachment. `attach_image --backend db` remains only as a fallback/diagnostic path and should be considered Mac-local until `audit_attachments` proves mobile sync evidence. `repair_attachments` can dry-run or repair older local-only image attachments by reattaching through ReminderKit, then soft-deleting the old local-only attachment object. URL support uses native Reminders URL attachment rows so the URL appears in the app detail panel. Tag writes use Reminders hashtag label/object rows. Attachment deletion is a soft-delete of the native attachment object only; copied image files are not hard-deleted. Urgent alerts, location alerts, and message-when-messaging alerts are intentionally not exposed as write commands yet because those surfaces need more reverse-engineering before they are safe for delegated use.

## Local-Only Boundary

This plugin uses macOS-specific implementation details. It is intended for local Codex use on a Mac, not for App Store application code.

- Private ReminderKit is used for reliable iPhone-visible image attachments.
- Private SQLite-backed Reminders store access is used for sections, tags, URL attachments, cache reads, and bounded repair/audit flows.
- The adapter stores a local action journal at `~/Library/Application Support/apple-reminders-codex/actions.jsonl`.
- The adapter stores a disposable cache and compiled helper under `~/Library/Caches/apple-reminders-codex/`.
- Plugin-owned directories are restricted to the current user, and journal, cache, helper lock, and backup files are written with owner-only permissions.
- DB-backed write commands reject explicit database paths outside the discovered Reminders `Stores` directory.
- Container backups are atomic archive-file writes but only best-effort snapshots of a live Reminders container; verify an archive before relying on it for recovery.
- Apple Reminders and iCloud may sync reminder data through Apple services according to the user's account settings.

## Verification

Run the focused checks from the repository root:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests/test_reminders_adapter.py
PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile scripts/reminders_adapter.py scripts/reminders_doctor.py scripts/validate_minis_export.py
PYTHONDONTWRITEBYTECODE=1 python3 scripts/validate_minis_export.py
clang -x objective-c -fobjc-arc -framework Foundation -framework AppKit -fsyntax-only scripts/remkit_attach_image.m
```

If PyYAML is installed in the active Python environment, validate the plugin
manifest and skill contract:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 ~/.codex/skills/.system/plugin-creator/scripts/validate_plugin.py .
PYTHONDONTWRITEBYTECODE=1 python3 ~/.codex/skills/.system/skill-creator/scripts/quick_validate.py skills/apple-reminders
```

## Minis Clean Export

For OpenMinis, do not contribute this full local plugin or copy the local
`skills/apple-reminders/SKILL.md`. The repository includes a separate,
allowlisted export at `minis/apple-reminders/`:

```text
minis/apple-reminders/
├── SKILL.md
└── evals/
    └── evals.json
```

That export relies only on Minis' built-in `apple-reminders` commands: `list`,
`create`, `update`, `complete`, and `delete`. It omits the macOS adapter,
private ReminderKit helper, direct SQLite writes, local cache, attachment
repair flows, local evals, and Codex plugin manifest files. Run
`python3 scripts/validate_minis_export.py` before copying that directory to
`OpenMinis/MinisSkills/apple-reminders`; the validator fails if extra files,
symlinks, local paths, or private implementation tokens enter the package.

Use an OpenMinis issue, not an app PR, for feature requests that need new native reminder surfaces such as sections, tags, or attachments.

## Safety Notes

The macOS Reminders app exposes only part of its model through public APIs. Image attachments require private ReminderKit for reliable iPhone visibility; sections, tags, and URL attachments currently require a local adapter over the Reminders store. Those paths must stay narrow, transactional where applicable, schema-checked, audited, and easy to disable.
