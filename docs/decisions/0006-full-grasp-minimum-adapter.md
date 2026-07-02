# 0006. Full-Grasp Minimum Adapter

## Status

Accepted.

## Decision

The local adapter v1 should implement the minimum command set needed for full-grasp assistant behavior.

## Required Commands

Read and diagnostics:

- `doctor`
- `snapshot`
- `list_lists`
- `list_sections`
- `search_reminders`
- `read_reminder`
- `list_tags`

Writes:

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

Safety and support:

- `backup_store`
- action journal for delegated writes

## Rationale

A CRUD-only adapter would not productize the most important findings from the investigation: native image attachments and sections can be controlled in the background.

The v1 adapter should therefore include those capabilities from the start, while still keeping the implementation local, dependency-light, and JSON-based.

## Consequences

- The adapter should be a local CLI/library first.
- MCP can be added later as a thin wrapper.
- The command contract should be stable before plugin tool exposure is attempted.
