# Architecture

Apple Reminders is not Google Calendar. The plugin should copy Google Calendar's operating discipline, not its exact integration shape.

## Google Calendar Reference

The Google Calendar plugin has two important parts:

- Skill files: the rules for how an agent should inspect calendar data, reason about it, and make safe changes.
- App connector: a hosted Codex connector declared in `.app.json`, which exposes actions such as `create_event`, `update_event`, and `search_events`.

That connector is why Google Calendar does not need a plugin-owned MCP server.

## Apple Reminders Difference

Apple Reminders is a local macOS app. There is no equivalent hosted Codex connector available for the native local Reminders store.

The dependency-light shape is therefore:

1. Skill layer
   - Encodes behavior, safety policy, output conventions, and when to read before writing.
2. Typed local MCP boundary
   - A bundled stdio server exposes discoverable JSON Schema tools, bounded reads, opaque pagination, exact identifiers, and normalized mutation receipts.
   - Packaged runtime resolves only bundled backend executables; path overrides are confined to source tests and cannot be enabled from a release archive.
   - It owns transport validation and routing, not Reminders business logic.
   - First-run tools separate content-free diagnostics, capability discovery, and the explicit EventKit permission prompt.
3. Public EventKit bridge
   - Uses EventKit for accounts, lists, bounded reminder reads, create/update, complete/reopen, list-to-list moves, and deletion.
   - Represents due dates, alarms, and recurrence as separate typed fields and preserves untouched fields on patches.
   - Requires `expected_last_modified` for existing-reminder writes, including deletion, and performs read-back verification.
   - A non-null URL create/update is completed by the MCP's verified private URL-attachment step so the user-facing link is visible in Reminders, followed by a final exact EventKit read that supplies the next safe `last_modified` precondition. If that read cannot complete, verification remains pending instead of being reported as verified.
4. Private adapter core
   - A local CLI/library performs JSON-in/JSON-out operations for Reminders-only surfaces that EventKit cannot express.
   - Uses private ReminderKit for image attachments and list-section saves because mobile visibility requires native Reminders/CloudKit transactions.
   - Uses private store writes only for remaining native Reminders surfaces not exposed publicly, such as tags, URL attachments, and bounded repair/audit flows. Direct SQLite section writes are diagnostic-only and never count as iCloud verification.
   - Requires a fresh matching reminder version before mutating an existing reminder; its DB delete command is diagnostic-only and is not an MCP fallback.
5. Native UI handoff
   - AppleScript remains a narrow compatibility path and an explicit `show_reminder` handoff, not the normal delete backend.
   - Foreground interaction is not the primary data path.

## Why Not UI Automation First

Foreground UI gestures conflict with the user's active desktop, dual-monitor state, and normal app usage. They should be reserved for exceptional verification or unsupported flows.

Normal operation should be background-first:

- read public reminder fields through EventKit and private surfaces through bounded adapter reads
- write public reminder fields through EventKit and private surfaces through ReminderKit/SQLite adapter operations
- verify by read-back
- optionally open Reminders only when the user asks to inspect the result

For image attachments and sections, the verification target is mobile-sync evidence, not local Mac rendering. A SQLite-only image or section row can show in Reminders on the Mac while failing to appear on iOS because the Reminders daemon never accepted it as a native CloudKit save. The adapter therefore uses ReminderKit for both image attachments and section writes, verifies CloudKit versions, and can repair older local-only sections in place.

## MCP Contract

The bundled MCP schema in `schemas/mcp-tools.json` is the user-facing contract. It provides:

- content-free doctor with a concise default/full diagnostic mode, capability,
  and explicit permission-request tools
- account/list enumeration and semantically bounded reminder fetches
- opaque cursors bound to an immutable filter fingerprint
- exact reminder reads and exact-ID mutations
- typed all-day/timed due values, absolute/location alarms, and recurrence rules
- purpose-specific private tools for sections, tags, URLs, and attachment maintenance
- four successful mutation outcomes: `unchanged`, `verified`, `committed_verification_pending`, and `partial_success`

The lower-level adapter continues to expose stable JSON commands for private operations and diagnostics, including:

- `doctor`
- `snapshot`
- `list_lists`
- `list_sections --list-id <exact-list-id>`
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
- `reopen_reminder`
- `delete_reminder`
- `show_reminder`
- `create_section`
- `move_to_section`
- `add_tag`
- `remove_tag`
- `cleanup_tags`
- `attach_image`
- `attach_url`
- `list_attachments`
- `audit_attachments`
- `repair_attachments`
- `delete_attachment`
- `replace_attachment`
- `backup_store`
- `purge_logs`

Direct adapter `create_reminder`, `update_reminder`, `complete_reminder`,
`reopen_reminder`, and `delete_reminder` are deprecated compatibility commands
in 0.2.x. They are not the normal MCP route and remain only to avoid undoing
existing workflows before a separately reviewed 0.3.0 removal.

Recovery snapshots sit behind the small `reminders_recovery.py` module. Tag
cleanup uses a single-database SQLite online backup; cross-store attachment
repair uses a whole-container archive. Strict filename and count/byte policies
prune only plugin-managed snapshots.

## Implementation Rule

Keep the MCP layer boring. It validates tool inputs, invokes either EventKit or the private adapter, sanitizes results, and enforces the shared receipt contract. Business rules remain in the bridge and adapter.

The real product quality comes from:

- the skill's operating contract
- the adapter's schema checks and transactions
- the disposable cache's narrow, rebuildable full-grasp index
- careful post-write verification
- capability-gated private operations and explicit recovery semantics
- summaries and diffs that are useful to a personal assistant workflow
