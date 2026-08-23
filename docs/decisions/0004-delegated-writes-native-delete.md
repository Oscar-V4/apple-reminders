# 0004. Delegated Writes And Native Delete

## Status

Superseded in part by 0009; delegated-write policy remains accepted.

## Decision

Use delegated writes as the normal operating mode.

The assistant may create, update, move, complete, organize, attach images, create sections, and delete reminders without asking for confirmation each time when the user's request implies task management delegation.

Deletion must preserve the native recovery contract. ADR 0009 routes MCP deletion through public EventKit with a fresh last-modified precondition and no private fallback. The diagnostic adapter may use DB soft-delete only when a fresh matching reminder version and exact environment capability record prove eligibility. Direct reminder-row hard-delete remains forbidden.

## Rationale

Apple Reminders is used for frequent capture and maintenance. Asking for confirmation on every small write would make the assistant unusable as a secretary.

Native Reminders deletion already provides a recovery flow through Recently Deleted. The adapter should preserve that behavior instead of inventing a parallel archive list or bypassing it with direct database deletion.

## Guardrails

- Read the relevant state before writing.
- Keep a local action log for delegated writes.
- Verify each write with a read-back.
- Use transactions for private-store writes.
- Use exact-ID EventKit deletion with a fresh `expected_last_modified` on the MCP path. Keep the DB soft-delete CLI capability/version gated and diagnostic-only.
- Never hard-delete reminder rows directly from the Reminders database. The separate digest-gated unused-tag-label maintenance primitive is governed by 0009.
- For broad or surprising changes, provide a concise applied-change report immediately afterward.

## Consequences

- The adapter needs an action journal.
- The public bridge owns normal deletion; the adapter retains an environment-specific capability gate only for diagnostic DB soft-delete research.
- The adapter should expose rollback guidance when the native app supports recovery.
