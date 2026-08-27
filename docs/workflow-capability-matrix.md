# Apple Reminders v0.3.1 workflow capability matrix

This matrix audits the released `v0.3.1` product surface as a multi-turn workflow system rather than as thirteen isolated tool calls.

- **Audit baseline:** tag `v0.3.1` and `main` commit `d5f401b8cd346e25d3d2c8b6172607892e60470b`.
- **Verification environment:** repository CI with synthetic fixtures and static validation only. No Reminders database, EventKit permission prompt, native UI, iCloud account, iPhone, or Recently Deleted state was observed.
- **Public surface:** eight Core tools, four Native Extension tools, and one Diagnostics tool, as defined by `CONTRIBUTING.md` and `plugins/apple-reminders/schemas/mcp-tools.json`.

## Classification

| Class | Meaning |
| --- | --- |
| **Bug** | Released instructions make a claim or prescribe an order that the public interface cannot verify safely. |
| **Unsafe gap** | Individual tools are guarded, but a natural composed journey can lose required state or imply unsupported capability. |
| **Intentional boundary** | The capability is deliberately absent; the correct behavior is to fail closed and state the boundary. |
| **macOS-only follow-up** | Native/UI behavior may exist, but implementation and evidence require a local macOS worker and are not part of this CI-verifiable change. |
| **Supported** | The current interface has an exact selector, bounded contract, receipt semantics, and synthetic regression coverage. |

## Capability and workflow matrix

| Journey or capability | v0.3.1 evidence | Classification | Hardened workflow contract | Remaining follow-up |
| --- | --- | --- | --- | --- |
| Create → exact read | `create_reminder` requires exact `list_id` and idempotency key; verified results may return a fresh `rev1` reference. | Supported | Preserve idempotency and final exact read. Never infer remote convergence from local verification. | None for the platform-independent contract. |
| Read → change → read | Existing-item changes require an opaque reference from `read_reminder`; omitted fields remain unchanged. | Supported | Re-read before every mutation and stop after stale, pending, partial, or manual-repair status. | None. |
| Delete → undo/recover | The released primary skill said Recently Deleted was “expected,” while the public interface exposes only `delete_reminder` and explicitly withholds restore. | **Bug** | Treat public deletion as terminal. Require a fresh reference and local-absence evidence, but never promise recovery or claim Recently Deleted state. | **macOS-only follow-up:** validate native Recently Deleted enumeration, restore, identity mapping, and receipts locally before exposing tools. |
| “Delete the top four, then recover them and consolidate images” | “Top” has no exact UI selector; restore is absent; attachment actions accept local files but cannot export existing attachment bytes. | **Unsafe gap** | Resolve an explicit API scope/sort to exact IDs; inspect all sources and the destination; attach from exact local files and verify destination first; delete sources last, one by one. Stop before deletion when any dependency is unavailable. | Native UI-order selection, Recently Deleted recovery, and attachment export/copy require separate local validation. |
| Relative selection and sorting | `fetch_reminders.sort` supports only `due`, `modified`, and `title`; Apple documents additional native UI modes including manual ordering. | **Intentional boundary** | Report API scope, sort, limit, truncation, and IDs. Do not call API order the current UI order. | **macOS-only follow-up:** define an observable UI selection/order contract or keep it withheld. |
| Pagination | Cursors are opaque and bound to original filters, sort, and page size. | Supported | Reuse a cursor only with identical arguments; never mutate unseen or differently sorted pages as one reviewed batch. | Add journey evals for multi-page broad proposals if limits expand. |
| Reminder-list CRUD | Public tools list reminder lists and ensure an exact list; rename/delete/color/emblem writes are absent. | **Intentional boundary** | Resolve exact `source_id` and `list_id`; create-or-return only through `ensure_reminder_list`. | Consider separately reviewed exact rename/delete contracts. |
| Section operations | Exact-list section inspection, create, and reminder move are exposed. Section rename/delete is absent. | Supported plus **Intentional boundary** | Treat section names as list-scoped; preserve exact IDs; do not invent rename/delete operations. | Native rename/delete only after schema, concurrency, and recovery evidence. |
| Tag operations | Exact reminder tag add/remove and bounded tag inventory are exposed; unused-label row cleanup is withheld. | Supported plus **Intentional boundary** | Change assignments only. Offer read-only inventory for global cleanup requests. | Keep private row cleanup separate from public workflow. |
| Image attachment attach/replace/delete | Public mutations require an exact reminder reference and local PNG/JPEG path; inspection returns attachment metadata. | Supported | Validate the local file, use idempotency for image writes, and trust only final receipts/read-back. | None for single-reminder local-file operations. |
| Cross-reminder attachment consolidation | No public action downloads, exports, or copies existing image bytes from an attachment object. | **Unsafe gap** and **Intentional boundary** | Consolidate only from exact available local source files. Attach and verify destination content before any source deletion. | **macOS-only follow-up:** research a native export/copy contract without exposing private paths or user data. |
| URL attachment semantics | Core URL create/change is a composed metadata plus visible-attachment operation; additional URL objects use Native Extension actions. | Supported | Do not duplicate a URL after a verified Core write; clearing Core URL metadata does not imply attachment-object deletion. | Preserve composed-operation regression coverage. |
| Dates, alarms, recurrence | Typed all-day/timed due values, explicit alarms, and one validated recurrence rule are exposed; recurrence requires a due date. | Supported plus **Intentional boundary** | Keep due and alarm intent separate. Do not invent relative or messaging alarms. | Expand only through a separately versioned schema decision. |
| Permissions | Normal operations can return `permission_denied`; `request_reminders_access` is explicit. | Supported | Request once, retry the original operation once, then stop after denial. Do not claim a system prompt was observed unless directly observable. | Local UI evidence remains outside CI. |
| Pending/partial receipts | `committed_verification_pending`, `partial_success`, and manual-repair states are distinct. | Supported | Perform only the receipt-directed fresh read. Never automatically replay a mutation or continue a destructive chain. | Continue focused failure-injection coverage. |
| Sync and device claims | Native results may expose CloudKit/mobile evidence, but not direct iPhone observation or guaranteed convergence. | Supported boundary | Say which evidence was observed. Never translate it into “confirmed on iPhone,” guaranteed iCloud convergence, or shared-list delivery. | Device/UI confirmation is an external observation, not a tool inference. |
| Diagnostics and error recovery | `diagnose_reminders` is content-free and read-only; backup/Snapshot apply, restore, repair apply, and log purge are withheld. | **Intentional boundary** | Diagnose only after a relevant failure and keep recovery read-only unless an exposed mutation has a complete receipt contract. | Native recovery tooling needs separate local implementation and review. |
| Minis/iOS symmetry | The Minis export supports list/create/update/complete/delete but not sections, tags, or attachments. | **Intentional boundary** | Explain the narrower surface rather than inventing parity. | Propose upstream Minis capabilities separately. |

## Representative workflow: dependency-first replacement

The observed request is unsafe when interpreted literally:

> Delete the top four reminders, then recover them and consolidate their image attachments into one reminder.

The CI-verifiable replacement workflow is:

1. **Resolve order explicitly.** Choose an exact list or bounded date scope, `status`, one supported API sort (`due`, `modified`, or `title`), and `limit=4`. Return the four exact IDs. If “top” means visible/manual UI order, stop and record a macOS-only selection follow-up.
2. **Read before mutation.** Obtain exact Core reads for all four sources and the destination. Inspect native attachments for every source and the destination.
3. **Check transfer feasibility.** Existing attachment metadata is not an image export. Resolve every intended image to an exact available local PNG/JPEG file. If any image lacks a safe local source, stop before deletion.
4. **Consolidate first.** Attach each local source image to the destination with a unique idempotency key. Follow receipt-directed fresh reads and require `verified` destination evidence.
5. **Delete last.** Obtain fresh references after attachment work and delete exact source reminders one at a time. Stop at the first non-verified outcome.
6. **No public retroactive recovery.** If the sources were already deleted, the public interface cannot enumerate or restore them. Do not use hidden adapter, SQLite, AppleScript, or UI automation paths and do not claim native Recently Deleted state.

This ordering converts recovery from an assumed safety net into an explicit dependency check before data destruction.

## Comparison with official product patterns

Primary official sources are comparison evidence, not proof of this plugin's runtime behavior.

- Apple documents multiple Reminders for Mac sort modes, including manual ordering. Therefore a phrase such as “top four” is UI-state-dependent and cannot be equated with this plugin's three API sort values without direct UI observation: <https://support.apple.com/guide/reminders/sort-reminders-remn922d0b42/mac>
- Apple documents native Recently Deleted recovery in Reminders for Mac. The plugin does not expose or remotely validate that surface, so it is a macOS-only follow-up rather than a current capability claim: <https://support.apple.com/guide/reminders/delete-reminders-remna83c9566/mac>
- Apple documents native reminder fields and attachment affordances, but account and platform behavior can vary. The plugin must continue to report only its own receipt/read-back evidence: <https://support.apple.com/guide/reminders/add-or-change-reminders-remndc729e28/mac>
- Gmail exposes recoverable `trash`/`untrash` operations separately from permanent `delete`. That separation is a strong safety pattern: recoverability should be an explicit capability, not an assumption attached to destructive deletion: <https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages> and <https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/delete>
- Gmail list pagination uses explicit page tokens, reinforcing that a reviewed page and its order must stay bound to the original query: <https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list>
- Google Tasks exposes explicit task/task-list operations and moves by stable resource identity, reinforcing exact-ID mutation rather than display-relative selection: <https://developers.google.com/workspace/tasks/reference/rest>

## Evidence index

Repository evidence:

- `CONTRIBUTING.md`: public tool count, safety boundaries, required checks, receipt truthfulness, and CI restrictions.
- `plugins/apple-reminders/schemas/mcp-tools.json`: exact inputs, supported sort values, limits, and cursor contract.
- `plugins/apple-reminders/skills/apple-reminders/references/public-interface.md`: public operations and withheld capabilities.
- `plugins/apple-reminders/mcp/v2_core.py`: opaque references, public fields, pagination fingerprint, and normalized error/receipt behavior.
- `plugins/apple-reminders/skills/*/SKILL.md`: user-journey policy executed by the agent.
- `tests/test_workflow_hardening.py`: static regression contract for this matrix and dependency-first workflow.

## macOS-only follow-up backlog

These items are intentionally not implemented by this change:

1. Observe and model exact current Reminders UI order/selection without guessing or relying on inaccessible state.
2. Enumerate Recently Deleted, preserve exact identity, restore one reminder, and return truthful receipts after local read-back.
3. Validate whether native attachment objects can be exported or copied without leaking private storage paths or bypassing concurrency controls.
4. Prove failure behavior, partial recovery, permissions, and version-sensitive compatibility on supported macOS/Reminders builds.
5. Add tools only after the local evidence supports closed schemas, bounded operations, exact selectors, and regression tests.
