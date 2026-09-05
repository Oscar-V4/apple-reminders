# Apple Reminders workflow capability matrix

This matrix audits the current checked-out public interface as composed user journeys. The live source of truth for tool names and closed actions is `plugins/apple-reminders/schemas/mcp-tools.json`; this document records the safe workflow around that interface.

The default session exposes nine Core/Diagnostics tools. Six Native and
Recovery tools require an explicit `--experimental` launch before any listed
Experimental workflow; all existing admission gates still apply. See
[ADR 0021](decisions/0021-core-default-experience.md).

In default Core mode, `url` writes only public EventKit metadata and preserves
existing native cards. Use a contextual note link when visible text is the goal.
Only Experimental mode retains the hybrid URL workflows described below; a
metadata-only receipt is not proof of a visible native card.

## Evidence boundary

- Repository validation proves schemas, contracts, skills, synthetic receipts, and packaging behavior. It does not by itself prove a macOS permission prompt, current Reminders UI order, iCloud convergence, shared-list delivery, or iPhone rendering.
- A local UI observation proves only the visible state on that Mac at that time. Record the observation basis and exact Reminder IDs; never turn it into a generic platform guarantee.
- Stable Core uses documented EventKit and is independent of Experimental
  Internals. Every private mutation/recovery path requires an exact allowlisted
  macOS version/build, Reminders version/build, and command-schema fingerprint;
  helper-backed paths also require a compiler. Unknown builds fail before
  private mutation.
- Recently Deleted eligibility is bounded by Apple's 30-day retention window. Expired or already-purged items are not recoverable through this interface.

## Classification

| Class | Meaning |
| --- | --- |
| **Supported** | The public interface has an exact selector, bounded contract, truthful receipt, and regression coverage. |
| **Experimental, admitted** | The public workflow remains private and runtime-unverified, but its exact build/schema/compiler preflight passed so operation-specific guards/read-back may proceed. |
| **Experimental, blocked** | Exact compatibility evidence is absent or mismatched; the workflow fails before mutation and must use a Core/manual substitute. |
| **Unsafe composition** | Individually valid calls become unsafe when ordered or scoped incorrectly. |
| **Intentional boundary** | The capability is deliberately absent; the workflow must state the limit and fail closed. |

## Runtime dependency boundary

All modes use the packaged Python runtime; there is no separate Python
installation. Compiler requirements below apply only to the selected advanced
operation. The package targets macOS 14+, while fresh-user TCC and minimum-OS
end-to-end execution remain separate acceptance evidence.

| Runtime boundary | Paths | Xcode Command Line Tools |
| --- | --- | --- |
| **Stable Core** | Core reads and changes through the bundled signed EventKit helper | Not required; Core and default diagnosis never invoke `clang`. |
| **Experimental, compiler-free private** | Tag mutation, URL-only attachment mutation, and read-only native section, tag, or attachment inspection | Not required; no runtime compilation. |
| **Experimental, CLT-required private** | Section mutation, image-attachment mutation, and exact Recently Deleted inspection or recovery | Required by the requested operation; compiler diagnosis is a separate explicit opt-in. |

## Core-first alternatives

| Experimental goal | Stable or honest default |
| --- | --- |
| Sections | Separate exact Reminder Lists, or an agreed text prefix/heading in title or notes. |
| Native tags | A plain-text label in title or notes, or a [user-authored Shortcut](https://support.apple.com/en-us/106430) using Apple's documented tag-capable Reminders actions. |
| Native image | A user-provided remote reference in notes, or a text description; never sync a private local path. |
| Native URL attachment | Use the Core `url` field for EventKit URL metadata, or a contextual link in notes. A visible native card remains experimental. |
| Recently Deleted recovery | Move active items to an archive list before deletion; use the Reminders UI manually when private recovery is blocked. |

## Capability and workflow matrix

| Journey or capability | Classification | Current contract | Boundary or follow-up |
| --- | --- | --- | --- |
| Reminder create → exact read | **Supported** | `create_reminder` requires an exact `list_id` and idempotency key. A verified result contains final exact state and may issue a fresh `rev1`. | Local verification is not remote-device convergence. |
| Exact read → change → read | **Supported** | `read_reminder` issues an opaque `rev1`; `change_reminder` supports one `patch`, `set_completion`, or `move_to_list` action. Omitted fields stay unchanged. Native verification fetches the saved Reminder again by identifier, and the public Module performs its own fresh exact read before issuing another `rev1`. | Re-read immediately before each mutation and stop on stale, pending, partial, or manual-repair results. |
| Exact read → delete | **Supported** | `delete_reminder` requires a fresh `rev1` and verifies local EventKit absence. | Deletion alone does not prove the Reminders UI, retention duration, or later recoverability. |
| Recently Deleted list → exact item | **Experimental, admitted only** | Build/schema admission precedes the bounded inventory; exact item mode additionally requires the compiler/helper before issuing `del1`. | Prefer moving active items to an archive list before deletion. On any capability block, use the visible Reminders UI manually; do not fallback. |
| Exact deleted item → recover | **Experimental, admitted only** | `recover_deleted_reminder` consumes one fresh `del1`, exact destination `list_id`, and idempotency key, with same-account/native/final EventKit verification. | No automatic retry. Unknown/new builds, missing CLT, schema drift, or guard mismatch stop before private save. |
| UI-relative selection such as “top four” | **Supported only with an explicit evidence basis** | Literal UI-relative selection requires direct current-UI observation and exact row-to-ID resolution. If unavailable, show the proposed list/filter/status/sort and use an API snapshot only after the user explicitly approves that reinterpretation. | UI order and API order are different evidence. Stop when duplicate display values prevent exact UI-to-ID resolution. Snapshot-safe pagination still does not replace exact item revalidation before a write. |
| Cross-reminder image copy | **Experimental, admitted only** | `copy_image` takes fresh source/destination references, one exact attachment, idempotency, exact build/schema/compiler admission, and destination read-back. | Prefer a note link or description. This is not image export and is unavailable on unlisted builds. |
| “Consolidate, then delete sources” | **Unsafe composition unless dependency-first** | Clarify whether the user wants separate copied attachments on one Reminder (supported) or one composited bitmap (unsupported). Inspect exact sources and destination, copy and verify each agreed image, then delete exact sources one at a time only under separate deletion authority. | “Organize” alone does not authorize deletion. Recovery is an exact post-deletion operation, not a planning substitute for preserving dependencies. Stop before deletion when any transfer is unresolved. |
| Broad cleanup | **Supported with operational bounds** | Discover summaries first, review/authorize the exact scope, then choose 25–40 candidates per chunk. Obtain each `rev1` just in time, write one item, read it back, and discard the reference before the next item. | The final chunk may be smaller. Halt the run on the first ambiguity, stale state, pending/partial/manual-repair receipt, or failure. Refresh summaries between verified chunks. |
| Completed brief | **Supported** | `fetch_reminders` uses `status=completed` with explicit `completion_start` and `completion_end` no wider than 90 days. The renderer receives the same bounds and renders a separate Completed section. | Completed items are not mixed into active due buckets. |
| Timed due and priority display | **Supported subset** | Zoned timed input verifies the requested clock and IANA timezone; explicit floating input verifies local wall-clock fields with no fixed instant. Human priority labels map Apple/EventKit 1–4 to high, 5 to medium, and 6–9 to low while retaining the numeric value. | Zone loss is never silently accepted. Repeated zoned DST hours are rejected before writing. `0` means no priority; bare `pN` labels are not user-facing semantics. |
| Reminder List CRUD | **Supported subset / Intentional boundary** | Read lists and create-or-return one exact-name list in an exact account with `ensure_reminder_list`. Duplicate exact names return `ambiguous_scope` without an arbitrary destination, including on persisted receipt replay. | Choose an exact list ID after resolving ambiguity. Rename, delete, color, and emblem writes are not public. |
| Complete a recurring occurrence | **Intentional boundary** | Incomplete recurring reminders return `unsupported_recurring_completion` before writing. The app can advance the original ID to the next occurrence and create a separate completed record. | Complete the intended occurrence once in Reminders. Do not retry the original ID or remove recurrence to bypass the boundary. Reopening an exact completed historical item does not rewind the series. |
| Section CRUD | **Experimental, blocked initially** | Public tools remain closed, but no exact section command-schema evidence is admitted. | Use separate lists or textual title/note grouping. Rename/delete remain absent. |
| Tag CRUD | **Experimental, blocked initially** | Public tools remain closed, but no exact tag command-schema evidence is admitted. | Use a plain-text title/note label. Global unused-label deletion remains absent. |
| Attachment CRUD | **Experimental, admitted only** | Image/URL actions require exact attachment build/schema evidence; image helper paths also require CLT. | Use notes for a Core-safe URL/image reference. Raw export and repair remain absent. |
| Dates, alarms, recurrence | **Supported subset / Intentional boundary** | Typed all-day/timed due values, explicit absolute, due-anchored relative, or coordinate-backed location alarms, and one validated recurrence rule are public. The writable relative subset is a bare default-display alarm with an integral offset from `-31536000` through `0`: at most 31,536,000 seconds (365 elapsed days) before due. | Due and alarm intent remain distinct. Relative offsets require an existing or same-request due value. Unsupported trigger, offset, or action variants remain readable as `read_only:true` with bounded action metadata; messaging-alarm writes are not public. |
| Relative-alarm replacement | **Supported with a read-only boundary** | Supplying `alarms` replaces the complete alarm array. Omitting `alarms` preserves the complete existing array whenever alarm intent is unchanged and the resulting due remains non-null; `alarms:null` and `alarms:[]` explicitly clear it. Setting `due:null` while retaining a relative alarm is rejected; jointly clear alarms or provide a complete non-relative replacement. A non-empty replacement is rejected before mutation when an existing alarm is `read_only:true`, because the plugin cannot preserve that alarm's unsupported trigger, offset, or action metadata through reconstruction. | Read the exact Reminder first. Explicit clear removes every alarm, including read-only alarms; use it only when that complete removal is intended. A lossy native trigger projection cannot prove preservation, so the write remains verification-pending. |
| Due-relative verification | **Supported as one semantic unit** | If the resulting Reminder contains a relative alarm, a `due` or `alarms` change verifies both fields. A list/account move verifies the destination (`calendar_id` natively and `list_id` publicly), `due`, and `alarms`. Native verification uses a fresh identifier lookup after save; the public Module repeats dependency-expanded matching against a fresh exact read. | Unexpected provider transformations cannot produce `verified`. Only an absent initial start becoming the exact observed first-due start is normalized; existing-start and zone loss still fail verification. The app may display a relative trigger as its primary time; this does not configure its separate Early Reminder control. |
| Pagination | **Supported with snapshot drift detection** | An opaque v3 cursor binds identical filters/sort/limit plus a cursor-contained SHA-256 fingerprint of the ordered Reminder IDs and revisions. Every next-page fetch recomputes it. | The digest is not a secret or a standalone result field. Membership or revision drift fails without mutation as `concurrent_modification` / `pagination_snapshot_stale`; restart from page one. The fingerprint is not a write precondition, so each mutation still requires a just-in-time exact read. |
| Permission handling | **Supported** | On `permission_denied`, request Reminders access once and retry the original operation once. | Report a prompt only when it was directly observed. Stop after denial. |
| Pending and partial receipts | **Supported safety boundary** | `committed_verification_pending`, `partial_success`, and `failed_manual_repair_required` preserve uncertainty and return a read-only resolution step. | Never replay a write automatically or continue a destructive chain. |
| Sync and device claims | **Supported evidence boundary** | Report only the returned local/native/CloudKit evidence. | `mobile_visible_likely` is not direct iPhone confirmation or guaranteed iCloud convergence. |
| Diagnostics and maintenance | **Intentional boundary** | `diagnose_reminders` is content-free and reports Stable/Experimental tier, compiler requirement, runtime state, and exact block reason. | Diagnosis never grants a bypass. Native flags, raw SQLite, backup/repair apply, and log purge are not public. |
| Minis/iOS surface | **Intentional boundary** | The portable Minis export supports its documented list/reminder subset. | It does not inherit macOS Native Extension, Recently Deleted, section, tag, or attachment capabilities. |

## Representative workflow: top four, recover, and consolidate images

This is an explicitly requested Experimental composition, not a default cleanup
workflow. Prefer archive-list preservation and note references.

1. Diagnose `recovery` and `attachments`. Stop before any private read/write if
   either exact capability is blocked; do not reinterpret a compiler or static
   schema success as compatibility.
2. Establish the selection basis. For literal UI-relative “top,” observe the current Reminders UI, resolve every visible row to one exact ID, and record the observation time. Stop on duplicate or ambiguous row-to-ID mapping. If UI observation is unavailable, stop and obtain explicit agreement before substituting one bounded API result with an exact list/filter, supported sort, `limit=4`, returned IDs, and truncation state. Never label one basis as the other.
3. Clarify whether “one image” means multiple separate copied attachments on one destination Reminder or a newly composited bitmap; only the former is public. If the sources are already deleted, page the bounded Recently Deleted inventory with the identical account/limit, restarting from page one on snapshot drift. Inspect every chosen item again by exact ID immediately before recovery, match its `account_id` to the destination `source.id`, then recover one at a time with a fresh `del1` and unique idempotency key.
4. Read every active source and the destination. Inspect the exact source image attachment IDs and destination attachment state.
5. Copy images to the destination one at a time with `copy_image`, using fresh source and destination `rev1` references. Both input references are consumed after dispatch. Require a verified destination read-back after each copy, use the returned fresh destination reference for the next copy, and re-read the source before another copy.
6. If deletion was separately and exactly authorized, re-read every source after all copies are verified, then delete exact sources one at a time. “Organize” is not deletion authority. Stop the whole chain on the first uncertain outcome.
7. Report local UI evidence, API evidence, native recovery evidence, and device/sync evidence separately. A successful run on one Mac is product evidence for that tested environment, not a compatibility promise for every macOS release or account.

The ordering is dependency-first: preserve and verify required data before any avoidable destructive step. When deletion already occurred, recovery is an exact guarded repair path rather than a reason to weaken that ordering.

## Evidence index

- `plugins/apple-reminders/schemas/mcp-tools.json`: public tools, closed actions, selectors, bounds, and reference formats.
- `plugins/apple-reminders/mcp/v2_contract.py`: envelope, receipt, reference, and uncertainty validation.
- `plugins/apple-reminders/mcp/v2_recovery.py`: opaque `del1` lifetime, one-use semantics, and public/private data boundary.
- `plugins/apple-reminders/skills/*/SKILL.md`: agent-executed workflow policy.
- `tests/test_workflow_hardening.py`: static drift checks for this matrix and the representative journey.
- Apple documents automatic and manual Reminders list ordering, which is why an API sort cannot stand in for observed UI order: <https://support.apple.com/guide/reminders/sort-reminders-remn922d0b42/mac>.
- Apple documents the user-facing 30-day Recently Deleted window for Reminders on Mac: <https://support.apple.com/guide/reminders/delete-reminders-remna83c9566/mac>.
