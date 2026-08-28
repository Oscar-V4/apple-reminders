# 0015 — Extract durable idempotency as one deep Module

## Context

The private adapter owned roughly 350 lines of durable mutation behavior:
canonical hashes, an exclusive process lock, a write-ahead fence, atomic JSON
persistence, privacy-preserving Receipt snapshots, replay, retention, capacity,
and the typed proof that a callback never dispatched. Core create loaded the
entire private adapter in-process only to call that behavior and obtain its two
error classes. Four retained Native adapter commands used the same function.

This was poor locality rather than merely a long file. Deleting the behavior
would force both callers to rebuild the same crash, privacy, and concurrency
rules, so it passes the deletion test for a deep Module.

## Options considered

1. Keep the implementation in the adapter. This minimizes the immediate diff
   but preserves the reversed dependency from public Core to the giant private
   adapter.
2. Extract one functional Module with a fixed production policy and only a
   temporary-directory test seam. This improves locality while keeping one
   caller-facing operation.
3. Build a configurable engine with store, serializer, clock, lock, outcome,
   and retention interfaces. This creates extension points for which there is
   only one production implementation and makes persisted safety policy appear
   optional.

## Decision

Choose option 2. `scripts/durable_idempotency.py` exposes
`execute_idempotent`; production callers provide an operation, optional key,
input fingerprint payload, and zero-argument callback. Tests may bind the same
implementation to a temporary storage directory. `AdapterError` and
`MutationNotStartedError` live with the shared Receipt/error contract so every
boundary uses one exact type identity. The private adapter imports and
re-exports those names for retained command compatibility.

The extraction preserves the existing v1 fence identity, record envelope,
compact JSON serialization, and execution order. It deliberately tightens the
privacy projection so primitive user-authored arrays are removed from new and
legacy complete results without changing the store version:

- a falsey key bypasses storage and locking;
- SHA-256 canonicalization and operation namespaces do not change;
- the `0700` support directory and `0600` JSON/lock files do not change;
- the exclusive lock remains held across callback dispatch;
- the in-progress fence is fsynced before dispatch;
- only `MutationNotStartedError` may clear a fence;
- every other callback failure retains the fence fail-closed;
- final Receipt persistence failure returns the successful live result with a
  warning and leaves replay outcome unknown;
- legacy state-less v1 records, 30-day retention, 500-entry capacity, current
  key protection, and operation aliases do not change;
- complete modern and legacy results are re-sanitized while the existing lock
  is held, then atomically rewritten with every fence metadata field intact;
- a failed existing-key scrub returns only the sanitized in-memory replay plus
  a bounded warning and never invokes the callback, while a new key still
  requires the combined scrub-and-fence write to succeed before dispatch;
- a non-object entry or invalid/non-finite timestamp is a typed unreadable-store
  proof before dispatch rather than a pruned record or raw conversion error.
- only an explicit top-level integer `version: 1` is readable; a missing,
  malformed, or unknown version fails before dispatch without rewriting bytes;
- a malformed complete `result` is removed during the locked privacy scrub
  while its fence metadata remains intact and replay stays outcome-unknown.

## Non-goals

This decision does not add idempotency to new operations, change lock
granularity, migrate JSON to SQLite, expose begin/commit/abort, or introduce a
repository, decorator, registry, DI container, generic result hierarchy, or
pluggable failure classifier. Those would change behavior or create shallow
interfaces without a second real implementation.

## Consequences

Core create depends on one callable and shared typed errors instead of loading
the private adapter. Retained Native commands keep their existing call shape.
Characterization tests own exact hashes, JSON bytes, permissions, privacy,
failure ordering, and replay behavior at the new Module boundary. Package
auditing includes the new runtime file, while public MCP schemas and tool
behavior remain unchanged. The privacy tightening needs no v2 store because it
changes neither key identity nor mutation authorization and never edits an
in-progress fence.
