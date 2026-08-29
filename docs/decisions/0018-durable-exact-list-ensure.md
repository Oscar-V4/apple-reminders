# 0018 — Serialize exact Reminder List ensure behind the durable fence

## Context

`ensure_reminder_list` performs an EventKit read followed, when no exact
source/name match exists, by a create. The former facade-local result cache
prevented repeat work only inside one Python object. Two MCP runtimes, or two
different idempotency keys for the same source/name, could both observe no
match and create duplicate lists. A process crash also erased replay state.

## Options considered

1. Keep the facade cache and add an in-process mutex. This cannot coordinate
   independent MCP runtimes and leaves crash replay unchanged.
2. Add a lock keyed by source/name beside the durable idempotency Module. This
   narrows contention, but creates a second lock lifecycle and ordering rules
   around the existing write-ahead fence.
3. Move ensure through the existing durable Module and hold its existing
   process lock across the exact check and possible create. This reuses one
   authority and one fail-closed recovery model, at the cost of serializing
   unrelated keyed mutations that share the same support directory.
4. Make check-and-create atomic in the native EventKit helper. EventKit exposes
   no atomic create-if-absent primitive, so a helper-local check has the same
   cross-process race.

## Decision

Choose option 3. Core Backend maps the public `ensure_reminder_list` operation
to the internal `eventkit_ensure_reminder_list` namespace and calls
`execute_idempotent`. The lock encloses EventKit validation, the exact
source/name check, and any create. The facade keeps validation and public
projection but owns no replay cache or mutex.

The input hash covers normalized source ID and list name. The store never keeps
the raw idempotency key, list title, source title, or warning prose. Its privacy
snapshot may retain Receipt status/backend/verification/recovery metadata,
exact identifiers, validated capability booleans and types, counts, and warning
codes. The existing EventKit protocol Module owns the one closed List/Source
vocabulary, identity validator, public projection, and safe-metadata predicate
used by Core and durability. A hash-matched replay validates stored identity
before reconstructing the list title and approved warning prose. A redacted
source title is not fabricated.

For this operation only, a new user key scans for an unresolved same-input
entry before dispatch. An unresolved or identity-invalid Receipt returns
verification-pending and a content-free alias atomically binds the new key, so
later reuse with different input still conflicts. The alias is non-mutating and
capacity-evictable; only the original write fence receives indefinite
retention. A completed result is not replayed under a fresh key: the callback
performs a current exact read while still holding the same lock.
A malformed input hash blocks all new ensures rather than authorizing a blind
create, while a valid different input remains independent.

Only trusted pre-dispatch or contract-valid no-write evidence clears the fence.
Timeouts, malformed Receipts, identity mismatches, and other uncertain outcomes
remain fenced and replay as verification-pending without redispatch. Identity-
invalid stored success Receipts are unresolved for retention too. Unresolved
fences do not age out; the 30-day cutoff applies only to redispatch-safe records.

## Consequences

Independent runtimes and different keys cannot overlap their check-then-create
windows on the same Mac when they use the same plugin support directory. The
guarantee does not coordinate separate Macs or separately configured support
directories. The existing global lock can delay unrelated durable writes while
EventKit is slow; this is accepted for the current bounded local workload in
exchange for one auditable lock order. If measured contention becomes material,
per-identity locking requires a new decision with an explicit lock-order proof.

Public MCP schemas stay unchanged. Behavior intentionally changes: replay now
survives process restarts, same-key/different-input calls conflict before
dispatch, and same-name concurrent ensures create at most one exact list.
