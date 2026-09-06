# v0.7.1 local synthetic image acceptance

Status at the recorded candidate checkpoint: **local test passed; publication
was pending**. The later [publication record](public-beta-0.7.1.md) documents
release verification without changing this earlier test's scope.
The coordinating release task exercised the actual extracted v0.7.1 ZIP through
its default MCP session on an arm64 Mac running macOS 26.5.2. Reminders permission
was already granted. This was not a clean-user test or direct iPhone observation.

The process used an invalid `DEVELOPER_DIR`, system `PATH`, and an unset
`APPLE_REMINDERS_NATIVE_ALLOW_SOURCE_BUILD`. No contributor compilation path was
enabled. The tested ZIP hash and signing provenance are recorded in the
[patch signoff](patch-candidate-0.7.1.md).

| Check | Observed result |
| --- | --- |
| Synthetic Core URL create | Verified URL metadata with no native card |
| Attach a public test icon | Verified image attachment through the bundled Native helper |
| Exact attachment identity | Structured target ID matched the final attachment ID and compact-summary target ID |
| Core field preservation | Requested metadata and unrelated Core state remained preserved |
| Sync evidence | CloudKit synchronization evidence passed; no direct device-view claim |
| Targeted metadata diagnosis | Fixed inconclusive-framework explanation, attention still required, and no recommendation to repeat identical diagnosis |
| Cleanup | Exact synthetic cleanup verified |

The metadata-only diagnosis did not reinterpret filesystem-path absence as a
verified runtime load or an installation failure. Its attention state remained
visible. This does not weaken unknown-build, schema, helper, access-error, or
additional-warning blocks.

Private receipts remain local. This public record includes no Reminder titles,
identifiers, attachment IDs, account information, or user content. It establishes
one bounded image workflow on the observed admitted host; section/tag admission,
other Native actions, fresh-user permission flows, real Intel/minimum-macOS
execution, and direct iPhone visibility remain separate evidence.
