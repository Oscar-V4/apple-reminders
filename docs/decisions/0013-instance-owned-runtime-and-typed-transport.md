# Own mutable MCP state per runtime and type dispatch certainty

The stdio server previously stored initialization, rate-limit history, lazy
Facade instances, and test backend paths in module globals. That made one MCP
connection capable of influencing another in the same interpreter and forced
tests to mutate production globals. Subprocess launch certainty also crossed
the backend boundary through a private `__dispatch_phase` JSON key, even though
only the parent launcher can prove that a child never started.

## Decision

- Construct one `McpRuntime` for each stdio session. It owns initialization,
  rate-limit history, immutable `BackendPaths`, and one lazy `_LocalToolDispatch`
  Facade graph.
- Keep explicit closed routing for the 15 public tools. Do not introduce a
  registry, dependency-injection container, dynamic plugin loading, or async
  lifecycle framework.
- Return `TransportResult` from the adapter and EventKit launchers. Its
  `DispatchCertainty` is either `PROVEN_NOT_STARTED` or `MAY_HAVE_STARTED` and
  is independent from child success and public mutation state.
- Only a parent-observed missing executable or oversized prelaunch request may
  produce `PROVEN_NOT_STARTED`. Timeout, `OSError`, oversized output, malformed
  JSON, and every child response remain `MAY_HAVE_STARTED`.
- Strip child-supplied private provenance fields. Core create and Recently
  Deleted recovery may consult only the typed parent-owned fact when deciding
  whether a write-ahead fence or no-write result is safe.
- Inside the durable idempotency callback, only the explicit
  `MutationNotStartedError` subtype may clear a persisted fence. Arbitrary
  `AdapterError.details` flags are data, not dispatch proof.
- Keep `ToolOutcome` separate. It carries a Facade result and mutation fact at
  the Facade-to-MCP boundary; `TransportResult` carries subprocess evidence at
  a lower trust boundary.
- Require every mutation-owning Core, Native, and Recovery Facade to implement
  `call_with_state`. The server rejects mutation Facades without that interface
  and never reconstructs the fact from the public `status` it must validate.
- Classify validated Receipts with write evidence, preserve exact state through
  durable replay and contract fallback, and align the public projection once at
  each Facade boundary. A lost final identity/read preserves `committed` but
  degrades `not_mutated` to `unknown`; a contradictory no-write Receipt can
  never erase affirmative write evidence.

## Consequences

Runtime instances no longer leak initialization, rate limits, Facade caches,
or injected paths across sessions. Tool discovery remains lazy. Test seams are
ordinary constructor arguments instead of mutable production state.

The typed transport and independent Core/Native/Recovery mutation-state
channels add a reviewed package cost, so the source ZIP budget moves from 1.25
MiB to 1.28 MiB. This is not permission for unbounded growth; retiring the
obsolete 0.2-era adapter CLI remains a v0.4 release blocker and will reduce the
package substantially in a separately reviewable change.

## Rejected alternatives

- A process-wide singleton runtime preserves the original cross-session
  coupling.
- A registry/dispatcher/transport framework adds more seams than the closed
  15-tool server needs.
- Reusing `ToolOutcome` conflates subprocess launch evidence with public
  mutation evidence.
- Accepting tuple transports or child JSON provenance as compatibility paths
  leaves two internal contracts and keeps the unsafe convention alive.
