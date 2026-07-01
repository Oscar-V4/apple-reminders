# 0001. Adapter First

## Status

Accepted.

## Decision

Build the Apple Reminders integration around a dependency-light local JSON adapter first.

MCP is optional and should only be a thin shim over the adapter if Codex needs first-class tool exposure later.

## Rationale

Apple Reminders is a native local macOS app, not a hosted SaaS API like Google Calendar. The useful data and attachment files already live on the user's machine.

The integration should therefore be background-first and local-first:

- no foreground UI gestures for normal operation
- no Shortcut dependency for core operations
- no MCP server as the core implementation
- no duplicated source-of-truth database unless needed for indexing

## Consequences

- The first implementation target is a CLI/library with JSON input and output.
- The skill layer can call or reason over this adapter contract.
- A future MCP server can expose the same operations without owning the business logic.
- Adapter operations must remain schema-checked, transactional, and easy to verify.
