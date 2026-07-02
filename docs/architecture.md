# Architecture

Apple Reminders is not Google Calendar. The plugin should copy Google Calendar's operating discipline, not its exact integration shape.

## Google Calendar Reference

The Google Calendar plugin has two important parts:

- Skill files: the rules for how an agent should inspect calendar data, reason about it, and make safe changes.
- App connector: a hosted Codex connector declared in `.app.json`, which exposes actions such as `create_event`, `update_event`, and `search_events`.

That connector is why Google Calendar does not need a plugin-owned MCP server.

## Apple Reminders Difference

Apple Reminders is a local macOS app. There is no equivalent hosted Codex connector available for the native local Reminders store.

The dependency-light shape should therefore be:

1. Skill layer
   - Encodes behavior, safety policy, output conventions, and when to read before writing.
2. Local adapter core
   - A small local CLI/library that performs JSON-in/JSON-out operations against Reminders.
   - Uses public APIs first: AppleScript and EventKit.
   - Uses private store writes only for native Reminders surfaces not exposed publicly, such as image attachments and sections.
3. Optional MCP shim
   - A thin wrapper over the local adapter only if Codex needs first-class tool calls in the plugin UI.
   - It should not own business logic.
   - It can be deferred until the adapter contract is stable.

## Why Not UI Automation First

Foreground UI gestures conflict with the user's active desktop, dual-monitor state, and normal app usage. They should be reserved for exceptional verification or unsupported flows.

Normal operation should be background-first:

- read through AppleScript/EventKit/SQLite
- write through AppleScript/EventKit/SQLite adapter
- verify by read-back
- optionally open Reminders only when the user asks to inspect the result

## Core Adapter Contract

The adapter should expose stable JSON commands before any MCP packaging:

- `doctor`
- `snapshot`
- `list_lists`
- `list_sections`
- `search_reminders`
- `read_reminder`
- `list_tags`
- `cache_rebuild`
- `cache_info`
- `cache_search`
- `cache_query`
- `create_list`
- `create_reminder`
- `update_reminder`
- `complete_reminder`
- `delete_reminder`
- `create_section`
- `move_to_section`
- `add_tag`
- `remove_tag`
- `cleanup_tags`
- `attach_image`
- `attach_url`
- `list_attachments`
- `delete_attachment`
- `replace_attachment`
- `backup_store`

## Implementation Rule

Keep the MCP layer boring. If it exists, it should only translate Codex tool calls into the local adapter's JSON commands.

The real product quality comes from:

- the skill's operating contract
- the adapter's schema checks and transactions
- the disposable cache's narrow, rebuildable full-grasp index
- careful post-write verification
- summaries and diffs that are useful to a personal assistant workflow
