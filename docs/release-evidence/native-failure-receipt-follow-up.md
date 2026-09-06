# Native failure receipt follow-up after v0.7.1

This is a source fix for a subsequent patch. It does not change the immutable
v0.7.1 release or the installed v0.7.1 cache.

A bounded synthetic test on the installed release created a reminder with an
all-day due date and relative alarm. The image call returned
`committed_verification_pending` with `invalid_native_receipt_objects`. A fresh
read preserved the original Core fields and found no image. The image call was
not repeated; exact synthetic cleanup was verified.

The exact durable operation receipt retained `concurrent_modification`,
`failed_manual_repair_required`, and unknown write state. The adapter's failure
constructor omitted the optional `before` snapshot, which its transport
contract accepted, while the Native facade required that field. The facade
therefore replaced the original failure with its generic malformed-receipt
fallback. This supports an error-projection defect; it does not establish an
alarm-specific image defect or successful image acceptance for this fixture.

New adapter failures explicitly include an empty snapshot. The facade also
accepts an absent snapshot in previously valid durable failure receipts, while
rejecting malformed present values and retaining strict success validation.
Manual-repair failures receive the public contract's fixed warning if missing.
Dynamic diagnostic messages remain private. Original error codes, reasons,
write certainty, and read-before-retry instructions are preserved; an unknown
failure is not converted to evidence of no mutation.

Regression tests exercise the real failure constructor through NativeBackend,
the facade, and public result validation. They cover durable replay without
another operation, affirmative no-write evidence, unknown concurrency failure,
malformed snapshots/warnings, contradictory evidence, and private message
redaction. The focused set passed 112 tests before PR integration. Full PR CI
provides the final integrated validation.

The version race itself is not bypassed. No build/schema admission, Native
helper, signed input, idempotency retry rule, or user data was changed by this
source repair. A future verified release is required to install the repair.
