# 0004. Delegated Writes And Native Delete

## Status

Superseded in part by 0009; delegated-write policy remains accepted.

## Decision

Use delegated writes as the normal operating mode.

The assistant may create, update, move, complete, organize, attach images, create sections, and delete reminders without asking for confirmation each time when the user's request implies task management delegation.

Deletion must preserve the native recovery contract. ADR 0009 permits a DB soft-delete only when an exact environment capability record proves Recently Deleted and sync parity; otherwise the adapter uses native Reminders deletion. Direct reminder-row hard-delete remains forbidden.

## Rationale

Apple Reminders is used for frequent capture and maintenance. Asking for confirmation on every small write would make the assistant unusable as a secretary.

Native Reminders deletion already provides a recovery flow through Recently Deleted. The adapter should preserve that behavior instead of inventing a parallel archive list or bypassing it with direct database deletion.

## Guardrails

- Read the relevant state before writing.
- Keep a local action log for delegated writes.
- Verify each write with a read-back.
- Use transactions for private-store writes.
- Use `backend=auto` for deletion and require verified recovery/sync evidence before the DB soft-delete path is eligible.
- Never hard-delete reminder rows directly from the Reminders database. The separate digest-gated unused-tag-label maintenance primitive is governed by 0009.
- For broad or surprising changes, provide a concise applied-change report immediately afterward.

## Consequences

- The adapter needs an action journal.
- The adapter needs native-delete support plus an environment-specific capability gate for any equivalent DB soft-delete fast path.
- The adapter should expose rollback guidance when the native app supports recovery.
