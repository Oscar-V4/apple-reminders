# 0016 — Extract the EventKit response protocol from its executable Adapter

## Context

The EventKit bridge script owns two different responsibilities: request
normalization/native helper execution and the pure response protocol. The MCP
server and Core backend need only response validation and construction of a
bounded outcome-unknown Receipt, but they dynamically loaded the full
executable bridge in-process to obtain those functions.

That loader was a false seam. There is one executable Adapter, while the pure
schema/status/Receipt rules have three real callers: the bridge, the server
transport fallback, and Core's no-write proof. Deleting those rules would make
the same safety logic reappear in all three places, so they pass the deletion
test for a deep Module.

## Options considered

1. Keep the dynamic executable-module loader. This avoids a new file but keeps
   Core and server coupled to helper compilation, request validation, CLI, and
   native-launch implementation they do not use.
2. Extract a three-function pure Module. Callers learn full wire validation,
   projected/full mutation Receipt validation, and outcome-unknown Receipt
   construction.
3. Add an `EventKitProtocol` class, version registry, expectation/result data
   classes, and injected Adapter. With one protocol dialect and no alternate
   implementation, those interfaces are speculative and shallow.
4. Move subprocess provenance and Core mutation-state interpretation into one
   caller-optimized transport interpreter. This makes call sites shorter by
   mixing transport, protocol, and Core responsibilities at the wrong seam.

## Decision

Add `scripts/eventkit_protocol.py` with exactly this public Interface:

```python
validate_response(payload: Any, operation: str) -> dict[str, Any]

validate_mutation_receipt(
    payload: Any,
    operation: str | None = None,
) -> dict[str, Any]

mutation_outcome_unknown_response(
    request: Mapping[str, Any],
    *,
    reason_code: str,
    message: str,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]
```

The Module owns EventKit response schema/status constants, mutation operation
classification, stable-error and no-write evidence validation, mutation
Receipt validation, and exact outcome-unknown Receipt construction.
It preserves existing `RuntimeError`/`ValueError` types and messages, returns
the original validated dictionary, and preserves the uppercase UUID and
pending-recovery shape of unknown outcomes.

The executable bridge directly imports and re-exports the three functions
rather than wrapping them. Server and Core import the Module directly. Core
validates fresh wire mutation results through `validate_response` and validates
privacy-projected durable replay through `validate_mutation_receipt`, because
projection intentionally omits the wire-only `schema_version` field.

Remove the dynamic bridge-module cache/loader and the `bridge_module`
constructor dependency. Keep the bridge subprocess path, request validation,
helper compilation, native invocation, and transport dispatch-certainty logic
unchanged.

## Testing

Move pure protocol cases from `test_eventkit_bridge.py` to
`test_eventkit_protocol.py`; do not duplicate them. Bridge tests retain native
process, CLI, request normalization, and helper behavior. Core tests use valid
EventKit Receipts instead of a permissive fake bridge contract. The extracted
package must construct Core with `eventkit_protocol.py` loaded while neither
`eventkit_bridge.py` nor `reminders_adapter.py` is imported in-process.

## Non-goals

This decision does not add a protocol registry, configurable schema engine,
class hierarchy, validation Adapter, DI hook, new wire version, new status, or
new public MCP behavior. It does not move subprocess execution or mutation
state ownership into the protocol Module.

## Consequences

Response-policy changes now have one locality and all three callers get the
same validation. Core's constructor loses an untyped giant-module loader, and
the server no longer executes the bridge script in-process merely to inspect a
child response. The package gains one small runtime file while deleting the
loader and duplicated test-world contract.
