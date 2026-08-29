# 0017 — Bound local helper processes behind one deep Module

## Context

The plugin launched 18 bundled helper, compiler, metadata, and MCP processes
with direct `subprocess.run(..., PIPE)` calls. Callers checked some output sizes
only after the complete streams were resident in memory. A timeout terminated
the immediate child but could leave a grandchild holding pipes or continuing
work, and text-mode decoding classified invalid UTF-8 differently across
callers.

These are local, substitutable dependencies, not remote services, but they
still cross one availability and mutation-certainty boundary. Reimplementing
deadlines, pipe draining, decoding, process-group cleanup, and reaping at every
call site fails the deletion test: removing any one copy would immediately
require the same behavior elsewhere.

## Options considered

1. Keep direct `subprocess.run` calls and add post-hoc length checks. This does
   not bound resident output and does not contain descendants.
2. Contain only the three top-level MCP launchers. Direct adapter/doctor CLI
   use and inner native helpers would retain the same resource and timeout
   failure modes before the outer launcher could inspect them.
3. Add one bounded process Module and migrate every production launch. Callers
   keep domain-specific Receipt and error policy while the Module owns process
   mechanics.
4. Add an executor class hierarchy, configurable worker pool, async framework,
   or dependency-injected process Adapter. There is one POSIX implementation
   and no alternate runtime, so those seams would be shallow and speculative.

## Decision

Add `scripts/bounded_process.py` with one public execution function:

```python
run(
    argv: Sequence[str],
    *,
    input: bytes | None = None,
    cwd: Path | None = None,
    timeout_s: float,
    stdout_limit: int,
    stderr_limit: int,
    output: Literal["utf8", "bytes"] = "utf8",
) -> ProcessResult
```

The Module launches with `shell=False` and `start_new_session=True`, drains both
pipes concurrently under byte limits, applies one monotonic deadline, decodes
UTF-8 strictly only after bounded capture, and reaps the direct process leader.
It terminates the whole process group with TERM followed by KILL on timeout,
output overflow, decode failure, and when a normally exited leader leaves
descendants behind.
Nonzero exit remains a normal `ProcessResult`; callers retain interpretation of
compiler diagnostics, helper JSON, and domain Receipts.

The typed failure surface is `ProcessLaunchError`, `ProcessTimeoutError`,
`ProcessOutputLimitError(stream)`, `ProcessDecodeError(stream)`, and
`ProcessIOError(stream)`. A launch error is parent-owned proof that no child
ran. Timeout, output-limit, decode, pipe-I/O, malformed JSON, and other
post-launch failures are never proof that a mutation did not start. Mutating
callers therefore preserve an unknown outcome and a read-before-retry path
unless the typed launch proof reaches the existing `MutationNotStartedError`
or `TransportResult.proves_not_started` boundary.

Every production `subprocess.run` site moves to this Module. Host JSON uses a
2,000,000-byte stdout bound and 256 KiB stderr bound; native runtime helpers use
256 KiB per stream; build tools use 256 KiB stdout and 1 MiB stderr; metadata
probes use 64 KiB per stream.

## Testing

The Module tests real child/grandchild fixtures for timeout, output overflow,
normal-leader exit, TERM/KILL containment, strict decode, split multibyte UTF-8,
stdin/cwd, nonzero exit, and launch failure. Caller tests preserve each stable
public error and independent mutation fact. A source regression forbids
production imports of `subprocess` outside this Module, and the extracted
source package must contain it.

## Non-goals

This is not a sandbox for hostile code, a Windows process abstraction, a job
queue, a retry engine, or a domain protocol. A deliberately escaping child that
creates a new session is outside the trusted bundled-helper boundary. The
Module does not parse JSON, choose stable error codes, or infer whether a
successfully launched mutation committed.

## Consequences

Process containment and byte bounds now have one locality and all 18 launchers
receive the same failure taxonomy. Domain call sites become smaller without
losing their Receipt semantics. The package gains one runtime file and tests,
while avoiding a second transport framework or configurable policy surface.
