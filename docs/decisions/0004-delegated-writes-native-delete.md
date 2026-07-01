# 0004. Delegated Writes And Native Delete

## Status

Accepted.

## Decision

Use delegated writes as the normal operating mode.

The assistant may create, update, move, complete, organize, attach images, create sections, and delete reminders without asking for confirmation each time when the user's request implies task management delegation.

Deletion must use the native Reminders delete path so deleted reminders land in Reminders' Recently Deleted flow. Direct database hard-delete is forbidden.

## Rationale

Apple Reminders is used for frequent capture and maintenance. Asking for confirmation on every small write would make the assistant unusable as a secretary.

Native Reminders deletion already provides a recovery flow through Recently Deleted. The adapter should preserve that behavior instead of inventing a parallel archive list or bypassing it with direct database deletion.

## Guardrails

- Read the relevant state before writing.
- Keep a local action log for delegated writes.
- Verify each write with a read-back.
- Use transactions for private-store writes.
- Use native Reminders delete behavior for deletion.
- Never remove rows directly from the Reminders database.
- For broad or surprising changes, provide a concise applied-change report immediately afterward.

## Consequences

- The adapter needs an action journal.
- The adapter needs native-delete support through AppleScript/EventKit/Reminders semantics.
- The adapter should expose rollback guidance when the native app supports recovery.
