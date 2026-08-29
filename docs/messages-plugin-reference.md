# OpenAI Messages plugin comparison

The bundled OpenAI Messages plugin is a product-discipline reference for this
community Apple Reminders plugin, not a code or endorsement claim. The local
comparison inspected Messages 1.0.1000901 and its live MCP discovery contract
on 2026-08-29 without reading message content.

## Useful Messages patterns

The six discovered tools are `find_chats`, `read_messages`, `search_messages`,
`send_message`, `count_message_activity`, and `read_image`.

- Conversation mutations use a stable `chat_guid`; participant display names
  are resolved before the write.
- Read and search pages return opaque cursors tied to the original filters.
  Activity pagination also freezes omitted time bounds at the first request.
- Page-local references cannot be reused across responses, which prevents
  accidental identity carry-over.
- Attachment list results expose metadata rather than local paths. Image bytes
  cross the boundary only through the dedicated exact `read_image` tool.
- `send_message` has a closed recipient-or-chat shape, caps text and attachment
  counts, and the manifest says sends require user approval by default.
- Rich output schemas make relationships between messages, chats, senders, and
  participants explicit.

## Translation into Apple Reminders 0.4

| Messages discipline | Reminders implementation |
| --- | --- |
| Stable `chat_guid` before send | Exact list/reminder/attachment IDs before mutation |
| Page-local identity | Short-lived one-use `rev1` and deleted-item `del1` References |
| Cursor-bound reads | Opaque v3 active-item cursor plus a snapshot-bound Recently Deleted cursor, both with ordered identity/revision drift rejection |
| Attachment metadata without paths | Native inspection returns bounded metadata and never a private backing path |
| Dedicated exact image access | Closed `copy_image` moves exact bytes internally without exporting them |
| Approval-aware send | Broad/destructive skills show the bounded target set unless standing delegation covers it |
| Explicit structured relationships | Central v2 result/Receipt validation plus compact content-free MCP summaries |
| Open-world mutation signaling | Every Reminders mutation advertises open-world effects because Apple sync and shared lists can carry writes beyond the local process |
| Unverified-send error signaling | Pending, partial, and failed Reminders mutations set MCP `isError` while retaining their exact Receipt and read-before-retry action |

The comparison directly motivated snapshot-stale pagination behavior and the
choice to close the consolidation workflow with an internal exact copy rather
than publish private attachment paths.

Recently Deleted discovery is paged rather than capped at its first 200 items.
Its cursor is reusable only with the identical account and limit; membership or
revision drift rejects the next page and requires a restart from page one.

## Intentional differences

Messages is an OpenAI proprietary bundled plugin backed by the shared Computer
Use client. Apple Reminders is an independently maintained, source-distributed
community plugin with EventKit plus version-sensitive local ReminderKit/store
adapters. It must therefore publish stronger platform and evidence caveats.

Messages can return an image to the model through `read_image`. Reminders 0.4
does not expose arbitrary attachment bytes: the demonstrated user need is
cross-Reminder consolidation, which `copy_image` completes while keeping the
backing file private. A future preview/download feature needs a separate
privacy, transfer-size, and rendering contract.

Messages advertises full output schemas. Reminders omits duplicate discovery
schemas to stay inside its tested startup and payload budget, but applies one
centralized runtime validator and boundary fixtures to every structured result.

## Remaining product follow-up

- Repeat permission behavior on a genuinely fresh macOS TCC subject and record
  only directly observed prompt evidence.
- Decide whether a bounded, rendered image-preview tool is worth its privacy
  and payload cost; it is not required for copy/consolidation.
- Continue sacrificial recovery/copy checks after macOS or Reminders upgrades,
  because one tested private-framework build is not a compatibility guarantee.

The first maintainer-orchestrated audit pass on PR #14 found release-blocking
gaps in recovery outcome classification, URL fresh-retry truthfulness, and
private recovery error text. The branch now carries targeted fixes and
regressions for those findings, plus branch-complete callable schemas; a
no-blocker conclusion is reserved for fresh CI and follow-up audit of the
updated head.
