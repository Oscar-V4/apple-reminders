# Privacy

Apple Reminders is a local Codex plugin prototype for managing the native macOS Reminders app.

## Data Access

The plugin can read Apple Reminders data stored on the local Mac, including reminder titles, notes, due dates, list and section names, tags, completion state, URLs, and attachment metadata.

When write commands are used, the local adapter can modify Reminders data through AppleScript/EventKit, private ReminderKit, and narrow SQLite-backed operations against the local Reminders store.

## Local Storage

The adapter writes operational files only on the local Mac:

- action journal: `~/Library/Application Support/apple-reminders-codex/actions.jsonl`
- disposable cache: `~/Library/Caches/apple-reminders-codex/cache.json`
- compiled ReminderKit helper: `~/Library/Caches/apple-reminders-codex/remkit_attach_image`
- optional Reminders container backups created by `backup_store` or repair flows

The disposable cache intentionally stores lightweight metadata only. It does not store image contents or full reminder notes.
Plugin-owned directories and files use owner-only permissions. A backup is a best-effort archive of the live Reminders container, so users should verify it before treating it as a recovery point.

## Network Use

This plugin does not send Reminders data to a plugin-owned server. Apple Reminders and iCloud may sync reminder data through Apple services according to the user's Apple account settings.

## Private API Boundary

Mobile-visible image attachment support uses Apple's private ReminderKit framework on macOS. This is a local-only integration detail and is not suitable for App Store app code or an OpenMinis app PR.

## User Control

The plugin is intended to read actual Reminders state before writes, keep destructive changes bounded, create backups before broad repair operations, and verify writes with read-back evidence.
