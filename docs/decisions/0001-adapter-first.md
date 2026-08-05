# 0001. Adapter First

## Status

Accepted as historical sequencing; amended by 0009.

## Decision

Build the Apple Reminders integration around a dependency-light local JSON adapter first.

The original implementation made MCP optional. ADR 0009 now requires a bundled typed MCP shim while retaining this decision's boundary: transport does not own business logic.

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
- The bundled MCP server exposes EventKit and adapter operations without owning the business logic.
- Adapter operations must remain schema-checked, transactional, and easy to verify.
