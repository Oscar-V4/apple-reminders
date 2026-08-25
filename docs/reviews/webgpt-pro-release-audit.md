# Independent Pro Release Audit — Apple Reminders 0.3 Public Beta

**Review target:** [`70692312c2abb2a8b10c5c2a9d0df5cb1f620835`](https://github.com/Oscar-V4/apple-reminders/commit/70692312c2abb2a8b10c5c2a9d0df5cb1f620835) on `codex/public-beta-0.3`  
**Comparison base:** [`07c60604c27a939708ab1966fea416b9a3105a75`](https://github.com/Oscar-V4/apple-reminders/commit/07c60604c27a939708ab1966fea416b9a3105a75)  
**Evidence inputs:** [PR #3](https://github.com/Oscar-V4/apple-reminders/pull/3), [issue #2](https://github.com/Oscar-V4/apple-reminders/issues/2), exact-head repository contents, exact-head GitHub Actions run [32855582138](https://github.com/Oscar-V4/apple-reminders/actions/runs/32855582138), and the primary/first-party comparison sources listed below  
**Review date:** 2026-08-25  
**Scope:** independent release/product/architecture review only; no runtime product code was changed

## Decision

**NO-GO for tagging or publishing the public beta in its present release state. Conditional GO after the three release blockers below are closed with exact-artifact evidence.**

This is not a finding that the 13-tool runtime is broadly unsafe. The exact-head synthetic suite and package audit are unusually strong, and I found no confirmed runtime data-loss defect in the reviewed code paths. The no-go is narrower: the repository has not yet proven the actual first-user launch path, the exact packaged artifact has no durable live TCC/Reminders evidence, and the release identity is not assembled from a merged commit.

The 13-tool surface is defensible, but it should not be treated as axiomatic. My independent minimum for routine public Reminders work is the eight Core tools plus targeted Diagnostics. The four Native Extension tools are differentiating rather than essential. Once they are statically advertised, however, they become part of the release contract and require live evidence on the declared supported system combination.

### Release classification

| Class | Count | Release effect |
|---|---:|---|
| Release blockers | 3 | Must close before tag/publication |
| Pre-release improvements | 5 | Resolve before publication or document an explicit, time-bounded waiver |
| Later work | 7 | Do not hold the first beta, but complete before a stable release where noted |

## Review method

I applied the current Matt Pocock engineering workflows at pinned upstream commit [`6654f6b`](https://github.com/mattpocock/skills/tree/6654f6b60cd9d5be8b54c6fafe44346dabeb3b76):

- **`mp-code-review`:** I kept two independent evidence ledgers—**Standards** and **Spec**—against the fixed point and only aggregated them after both passes. The sections remain separate below rather than merging or reranking their findings. See [`code-review/SKILL.md`](https://github.com/mattpocock/skills/blob/6654f6b60cd9d5be8b54c6fafe44346dabeb3b76/skills/engineering/code-review/SKILL.md).
- **`mp-codebase-design`:** I reviewed module ownership, vocabulary translation, seam depth, locality, test surfaces, and the deletion test. See [`codebase-design/SKILL.md`](https://github.com/mattpocock/skills/blob/6654f6b60cd9d5be8b54c6fafe44346dabeb3b76/skills/engineering/codebase-design/SKILL.md).
- **`mp-diagnosing-bugs`:** correctness and performance claims are classified as confirmed, evidence gap, or inference. I did not convert a plausible hypothesis into a defect without a deterministic source/test contradiction. See [`diagnosing-bugs/SKILL.md`](https://github.com/mattpocock/skills/blob/6654f6b60cd9d5be8b54c6fafe44346dabeb3b76/skills/engineering/diagnosing-bugs/SKILL.md).
- **`mp-research`:** comparisons use repository-owner sources, the currently connected first-party Google Calendar action schema, official OpenAI app/data-control documentation, and Google Calendar API documentation. Backend details that are not public are explicitly marked unavailable. See [`research/SKILL.md`](https://github.com/mattpocock/skills/blob/6654f6b60cd9d5be8b54c6fafe44346dabeb3b76/skills/engineering/research/SKILL.md).

The isolated WebGPT execution backend failed before creating a worktree and also failed on read-only repository access. I therefore performed the inspection through the authorized GitHub connector at the immutable SHA. I did **not** mutate Apple Reminders, PR #3, issue #2, tags, releases, workflows, CI, or runtime files. I also did not rely on any earlier audit report; no `docs/reviews` directory existed at the reviewed SHA. A fresh local test run was unavailable in the fallback environment, so execution claims below distinguish exact-head CI evidence from unverified PR prose.

## Release blockers

### RB-1 — The real marketplace launch path has not proved its Python and native-toolchain environment

**Finding.** The shipped MCP manifest launches ambient `python3`, while the runtime hard-requires Python 3.11 or later and Native helpers require the Xcode command-line toolchain. The package tests initialize extracted copies with the test runner's `sys.executable`, not the manifest's `python3`. They therefore prove archive completeness and server behavior, but not the interpreter that Codex will resolve on a clean Mac.

**Evidence.**

- [`plugins/apple-reminders/.mcp.json:15-18`](https://github.com/Oscar-V4/apple-reminders/blob/70692312c2abb2a8b10c5c2a9d0df5cb1f620835/plugins/apple-reminders/.mcp.json#L15-L18) launches `python3 ./mcp/server.py`.
- [`plugins/apple-reminders/mcp/server.py`](https://github.com/Oscar-V4/apple-reminders/blob/70692312c2abb2a8b10c5c2a9d0df5cb1f620835/plugins/apple-reminders/mcp/server.py) declares a Python 3.11 minimum and returns `unsupported_python_runtime` before façade dispatch on older interpreters.
- [`plugins/apple-reminders/README.md:33-40`](https://github.com/Oscar-V4/apple-reminders/blob/70692312c2abb2a8b10c5c2a9d0df5cb1f620835/plugins/apple-reminders/README.md#L33-L40) requires macOS 14+, Python 3.11+, and Xcode command-line tools.
- `tests/test_package_source.py::test_extracted_package_initializes_and_lists_exact_public_tools` and `::test_recursive_marketplace_source_copy_initializes_with_exact_public_tools` invoke `sys.executable`; they do not execute the command declared in `.mcp.json`.
- `tests/test_mcp_server.py::test_unsupported_python_fails_explicitly_before_facade_dispatch` proves safe failure, not successful clean-install resolution.
- The formal candidate spec requires a clean packaged-artifact initialization path in [`docs/public-beta-0.3.md`](https://github.com/Oscar-V4/apple-reminders/blob/70692312c2abb2a8b10c5c2a9d0df5cb1f620835/docs/public-beta-0.3.md).

**Why this blocks release.** A first-time user can install a structurally correct package and still receive an unusable plugin because Codex resolves an older system Python or the helper toolchain is absent. Safe failure prevents data damage but does not satisfy the promised first-use outcome.

**Required exit evidence.** On a clean supported Mac, install the exact release candidate through the marketplace path, start a new Codex task, and record a redacted receipt containing the package SHA-256, commit SHA, `sys.executable`, `platform.python_version()`, macOS version, Reminders build, `clang` availability, helper build/cache result, MCP initialize result, and the exact 13 names returned by `tools/list`. The probe must launch through the manifest, not a test-selected interpreter.

### RB-2 — The exact packaged artifact has no durable live TCC, Core, and Native Extension receipt

**Finding.** Exact-head CI proves synthetic contracts, syntax, deterministic packaging, and package initialization. It does not run a live Reminders workflow. PR #3 describes live checks, but the reviewed evidence does not contain a durable, redacted artifact tying those checks to the exact candidate ZIP and supported macOS/Reminders/Python combination.

**Evidence.**

- Exact-head CI run [32855582138](https://github.com/Oscar-V4/apple-reminders/actions/runs/32855582138) passed both macOS 14 jobs for Python 3.11 and 3.12.
- [`.github/workflows/ci.yml`](https://github.com/Oscar-V4/apple-reminders/blob/70692312c2abb2a8b10c5c2a9d0df5cb1f620835/.github/workflows/ci.yml) runs validators, synthetic unit/package tests, Objective-C syntax checks, plist validation, and deterministic package audits. It contains no live TCC/Reminders step.
- [`docs/public-beta-0.3.md`](https://github.com/Oscar-V4/apple-reminders/blob/70692312c2abb2a8b10c5c2a9d0df5cb1f620835/docs/public-beta-0.3.md) explicitly requires a disposable live workflow covering list/read/create/patch/complete/reopen/delete, URL, image, section, and concurrency rejection with cleanup.
- `tests/test_mcp_v2_core.py`, `tests/test_mcp_v2_native.py`, `tests/test_mcp_server.py`, and adapter tests provide deterministic substitutes, but their EventKit/private-store ports are mocked or fixture-backed.
- The static 13-tool discovery contract always advertises four Native Extension tools, although the README correctly says Native availability cannot be inferred from macOS version alone.

**Why this blocks release.** EventKit permissions and version-sensitive private ReminderKit/CloudKit behavior are precisely the parts synthetic fixtures cannot prove. A public beta may tolerate a capability being unavailable, but it must first establish that the advertised supported combination can complete the promised flows and fail with the documented receipts when it cannot.

**Required exit evidence.** Run the exact audited ZIP on a disposable list after first-use TCC authorization. Persist a content-free/redacted run manifest tied to commit and package hash that covers:

1. permission-required result, explicit access request, and one retry;
2. list/read/create/patch/complete/reopen/delete with exact final reads and cleanup;
3. create idempotency replay and conflicting-key rejection;
4. visible URL success plus a forced/observed partial-success path;
5. image attach/replace/delete with PNG/JPEG validation and cleanup;
6. exact-list section create/move and tag add/remove;
7. stale/consumed reference rejection and a concurrent-modification rejection;
8. no residual disposable reminders, sections, attachments, or plugin-generated test files.

No user reminder content should enter the artifact. If the Native Extension cannot be validated on the declared combination, the alternative is a separately reviewed Core-plus-Diagnostics release surface; silently shipping four unvalidated Native tools is not acceptable.

### RB-3 — The publishable release identity does not yet exist

**Finding.** The candidate is a draft PR branch, the repository has no GitHub release, and the README intentionally describes `v0.3.0` as a future tag. The deterministic package machinery is ready, but the artifact/tag/checksum/install identity has not been assembled from a merged commit.

**Evidence.**

- [`plugins/apple-reminders/README.md:46-52`](https://github.com/Oscar-V4/apple-reminders/blob/70692312c2abb2a8b10c5c2a9d0df5cb1f620835/plugins/apple-reminders/README.md#L46-L52) says the commands target the future tagged release and that the repository remains development source until the tag exists.
- PR #3 remains open and draft at the reviewed head.
- The repository's releases endpoint returned an empty list on 2026-08-25.
- `tests/test_package_source.py::test_two_source_packages_are_byte_for_byte_deterministic`, `::test_archive_contains_only_runtime_allowlist`, and `::test_release_archive_stays_within_size_budget` prove the build mechanism, not publication from the final merged SHA.
- [`docs/public-beta-0.3.md`](https://github.com/Oscar-V4/apple-reminders/blob/70692312c2abb2a8b10c5c2a9d0df5cb1f620835/docs/public-beta-0.3.md) requires marketplace metadata, manifest, changelog, tag, ZIP, checksum, and release version to agree.

**Why this blocks release.** A release cannot be audited or reproduced when the install command points to a tag that does not exist and no immutable artifact/checksum is tied to the reviewed code.

**Required exit evidence.** After review findings are resolved, merge the intended candidate; build the source ZIP twice from the merged SHA; compare byte-for-byte; run the archive audit; publish the checksum; create `v0.3.0` at that SHA; attach or identify the exact audited artifact; publish release notes that disclose private-interface/toolchain limitations; then install from the tag in a new Codex task and verify exactly 13 tools. Do not reuse an artifact built from the pre-merge PR head unless the merged SHA is byte-identical and documented.

## Pre-release improvements

### PI-1 — `diagnose_reminders(detail_level="full")` is over-promised and currently loses the Doctor's details

**Confirmed defect.** Doctor check objects store detailed values under `details`, while the public Diagnostics façade emits full facts only when a check contains a mapping named `facts`. The public `full` mode will therefore usually add no detailed facts even though the CLI full report contains them.

**Evidence.**

- [`reminders_doctor.py:124-145`](https://github.com/Oscar-V4/apple-reminders/blob/70692312c2abb2a8b10c5c2a9d0df5cb1f620835/plugins/apple-reminders/scripts/reminders_doctor.py#L124-L145) defines check results with `details` and `errors`.
- [`reminders_doctor.py:1188-1305`](https://github.com/Oscar-V4/apple-reminders/blob/70692312c2abb2a8b10c5c2a9d0df5cb1f620835/plugins/apple-reminders/scripts/reminders_doctor.py#L1188-L1305) returns full checks unchanged for `detail_level=full` and strips them only for summary mode.
- [`mcp/v2_native.py:1414-1530`](https://github.com/Oscar-V4/apple-reminders/blob/70692312c2abb2a8b10c5c2a9d0df5cb1f620835/plugins/apple-reminders/mcp/v2_native.py#L1414-L1530) reads only `candidate["facts"]` when public detail is full.
- `tests/test_doctor_summary_contract.py` tests CLI summary/full behavior; Native/MCP tests cover summary routing but do not protect an end-to-end public full-detail projection.

**Action.** For 0.3, either remove `full` from the public schema/docs or map an explicit allowlist of scalar, content-free `details` into bounded `facts`. Add an end-to-end `diagnose_reminders(scope=..., detail_level="full")` test that proves useful additional information without paths, names, rows, or content. This is a real UX/contract defect, but it does not currently threaten reminder data, so it is not a release blocker.

### PI-2 — Performance claims need an exact-head artifact, not only a safe harness and mocked gate tests

The benchmark design is sound and data-free. It defines p95 budgets for initialize/tool discovery, Doctor, fresh EventKit helper build, and cached EventKit requests. `tests/test_benchmark_plugin.py` primarily tests benchmark mechanics and threshold handling with controlled timings. Exact-head CI does not run `scripts/benchmark_plugin.py`, and no raw benchmark JSON was available in the reviewed workflow record.

**Action.** Run the benchmark against the extracted release ZIP on the clean-install machine, attach the JSON and environment fingerprint to the release evidence, and preserve it as a CI/release artifact. Do not claim cross-machine comparability; use it to detect gross candidate regressions and to substantiate the PR's p95 statement.

### PI-3 — Make the clean-install/live receipt a first-class release artifact

The present release checklist is prose. Convert the RB-1/RB-2 exit evidence into a small machine-readable schema, with content fields forbidden by construction. Include candidate SHA, ZIP hash, OS/app/runtime versions, tool list digest, operation/status sequence, cleanup result, and timings. This improves audit durability without shipping test fixtures or user data in the plugin.

### PI-4 — State Native Extension support and fallback at the install boundary, not only in deep documentation

The README is accurate, but the static tool list can still lead users to infer that every Native operation is supported on every macOS 14+ machine. Add a compact release-note/onboarding statement: Core is the compatibility floor; Native uses version-sensitive private interfaces; an unavailable Native action does not imply Core failure; `diagnose_reminders` is failure-triggered rather than a mandatory preflight.

### PI-5 — Freeze release-facing evidence to the tag where practical

Manifest documentation URLs currently point at `main`. That is conventional, but release auditability is stronger when the release notes include immutable links to the tagged privacy, security, support, terms, changelog, and architecture documents. Keep the user-facing canonical URLs if required, but add tagged source links and hashes to the release record.

## Later work

1. **Split deprecated direct-write and maintenance code out of the installable runtime before a stable release.** The closed MCP dispatch prevents public access today, and the main skill explicitly forbids fallback. Nevertheless, the packaged `reminders_adapter.py` still exposes deprecated direct database writes and maintenance operations. Move those into a repository-only maintenance package once the migration seam is no longer needed.
2. **Ship signed or prebuilt native helpers, or a verified installer-managed toolchain.** Requiring Xcode command-line tools is acceptable for a developer beta but is a poor stable consumer experience.
3. **Validate a second macOS/Reminders build before stable.** Private-interface behavior is build-sensitive; one supported combination is enough for beta only if clearly disclosed.
4. **Reduce `server.py` legacy orchestration after behavior is protected at module interfaces.** Do not flatten Core/Native/Diagnostics. The safe deletion test is that removing inaccessible legacy route plumbing changes neither the 13-tool discovery contract nor façade tests.
5. **Keep backup/repair/cleanup withheld until preview/apply and restoration verification are complete.** Do not expose automatic restore merely because backup primitives exist.
6. **Consider a capability-negotiated or Core-only distribution if Native support proves too volatile.** This requires a new interface review; it is not a documentation-only switch.
7. **Add optional output schemas only when they earn their discovery cost.** Central result validation currently provides stronger value than duplicating large schemas into `tools/list`.

## Standards review

This section preserves the `mp-code-review` Standards axis independently from the product specification.

### S-1 — The public surface is closed, bounded, and enforced at load and dispatch boundaries

**Pass.** `mcp/v2_contract.py` defines six read tools and seven mutation tools. `server.py` rejects a schema whose names differ from that contract, validates arguments before façade dispatch, and returns a protocol error for unknown legacy tools. `schemas/mcp-tools.json` closes object schemas and bounds strings/arrays. Tests verify exact discovery under 32 KiB, all 13 routes, no eager façade import during discovery, and concise content-free MCP text summaries.

The absence of MCP `outputSchema` is a deliberate tradeoff, not a blocker: centralized result-envelope validation and boundary tests are the actual safety mechanism. Adding duplicate discovery schemas would increase token and drift cost without replacing those validators.

### S-2 — Core, Native, and Diagnostics are deep modules with appropriate vocabulary ownership

**Pass with later refactor debt.**

- **Core** owns EventKit-facing list/reminder semantics, exact read projections, cursor fingerprints, opaque one-use references, public revision checks, idempotent create/list behavior, and EventKit mutation receipts.
- **Native Extension** accepts only a Core-issued guard, revalidates public identity/revision, reacquires the private reminder version immediately before dispatch, and rotates or consumes references according to outcome. It does not become a second reference authority.
- **Diagnostics** owns content-free environment checks and scope filtering. It does not become a capability preflight dependency for Core.
- **Server** is the composition root and transport adapter. It is large and still contains legacy route plumbing, but public callers cannot address those routes.

This is the right seam: public vocabulary stays in Core/Native modules, while EventKit calendar terminology, SQLite rows, private versions, CloudKit evidence, and helper command lines remain adapter vocabulary.

### S-3 — Failure semantics are conservative and materially safer than exit-code success

**Pass.** The receipt model distinguishes `unchanged`, `verified`, `committed_verification_pending`, `partial_success`, `failed_no_mutation`, and `failed_manual_repair_required`. Post-dispatch transport exceptions are treated as unknown outcomes, not clean failures. Terminal success requires the documented final read. Native manual-repair state is not downgraded. This is especially important for local native helpers, where process termination cannot prove whether a write committed.

### S-4 — Package composition and privacy controls are strong

**Pass.** The source package is allowlisted, deterministic, below a 1 MiB ceiling, and audited for tests, development docs, workflows, screenshots, databases, journals, backups, bytecode, and private images. Extracted and recursively copied packages initialize with the exact public tools. Production paths ignore legacy environment overrides. Privacy documentation explains local stdio, possible Codex-host transmission, local caches/journals/backups, shared-list effects, and verification limits.

Doctor is content-free by construction: it reads platform/app metadata, schema and aggregate counts, toolchain state, permissions symptoms, and artifact metadata; it does not read reminder rows, titles, list/section/tag names, attachment contents, or backup/log contents. Adapter journal tests verify recursive sensitive-field redaction and bounded retention controls.

### S-5 — The test architecture mostly targets interfaces rather than implementation details

**Pass, with one caution.** Core and Native façade tests use explicit ports and test state transitions at module boundaries. MCP tests verify transport/schema/dispatch. Package tests verify the installable artifact. Adapter tests cover private schema/mutation contracts. This is a good layered test strategy.

The caution is `tests/test_golden_regressions.py`: it checks that regression-contract prose contains terms. It is not execution evidence. The real regression protection lives in the Core, Native, server, EventKit bridge, adapter, image-input, recovery, and package tests. Release claims should cite those executable tests rather than treating the golden document check as proof.

### S-6 — The installable runtime contains more trust surface than the public interface requires

**Later-work finding.** Closed dispatch means the deprecated adapter writes are not a current public route. Still, shipping direct-DB mutation and maintenance implementation in the same archive increases reviewer burden and consequences if a future routing mistake occurs. This is overbuilt for the beta's normal-operation interface, but deleting it immediately could break URL/image/section/tag compatibility seams and recovery behavior. Split it only after the interface tests prove the installed server no longer imports or invokes those commands.

## Spec review

This section preserves the `mp-code-review` Spec axis independently from Standards.

### SP-1 — Routine Core behavior is substantially complete

**Pass in synthetic evidence.** The eight Core tools cover access, exact list identity, bounded fetch, exact read, create, consolidated change, delete, and exact-source list creation. Fetch rules prevent unbounded completed/incomplete scans; cursor fingerprints bind filters and sort; exact reads issue opaque references; patch omission preserves unrelated fields; due, alarm, and recurrence remain distinct.

`change_reminder` is the correct compression point. Splitting patch/completion/reopen/move into separate public tools would add discovery cost without adding a distinct safety boundary. `delete_reminder` merits its own tool because it has different authorization, final-absence, and user-confirmation semantics.

### SP-2 — Native Extension behavior is cohesive but not required for the minimum first release

**Conditional pass.** `inspect_reminder_native`, `create_reminder_section`, `organize_reminder`, and `change_reminder_attachment` form a coherent capability module. `organize_reminder` correctly consolidates section move and tag assignment actions; `change_reminder_attachment` consolidates image/URL attach, replace, and delete actions. Splitting by attachment type or tag operation would recreate a broad implementation-shaped surface.

These four tools are not necessary for a useful first public Reminders release. They are justified as differentiators only if RB-2 live evidence is produced. Flags, native UI selection, unused-tag row cleanup, attachment repair, backup, and log purge are correctly withheld.

### SP-3 — Permission and diagnosis UX follows failure-triggered escalation

**Pass except PI-1.** Skills start with the requested bounded Core operation, request permission only after `permission_denied`, retry once, and invoke Doctor only after a relevant environment/Native failure. This is better first-use UX than unconditional preflight. The content-free Doctor and scoped result are appropriate. Public full-detail projection is the one confirmed gap.

### SP-4 — Backup and recovery are correctly separated from normal Reminder work

**Pass.** Normal EventKit/native writes rely on exact reads and receipts, not a misleading global backup promise. Internal recovery primitives create mode-0600 SQLite online backups and best-effort container archives; retention deletes only managed backups and protects the newly created copy. Automatic restore remains a non-goal, and backup/repair commands are withheld from the public surface. That is the correct release posture until restoration can be verified end to end.

### SP-5 — Onboarding content is complete; onboarding execution is not

**Partial.** The README includes requirements, install, permission behavior, first prompts, upgrade, temporary disable, uninstall, local-data cleanup, troubleshooting, receipt semantics, private-interface disclosure, and new-task refresh guidance. The missing element is evidence that these steps work from the final tag on a clean machine. RB-1 and RB-3 close that gap.

### SP-6 — GitHub release readiness is mechanically prepared but operationally incomplete

**Partial.** Marketplace metadata points at the canonical plugin subtree, plugin/version metadata is internally aligned at 0.3.0, public legal/support/security documents are packaged, exact-head CI is green, and deterministic package tests pass. Merge, immutable tag, checksum, release record, clean tag install, and final tool-list verification remain outstanding.

## Exact public tool-surface audit

| Tool | Release judgment | Why it exists / concern |
|---|---|---|
| `request_reminders_access` | Required | TCC authorization is an explicit side effect and should not be hidden inside reads/writes. |
| `list_reminder_lists` | Required | Establishes exact `source_id`/`list_id`; duplicate display names are not identity. |
| `fetch_reminders` | Required | Bounded collection read with filter-bound cursor. |
| `read_reminder` | Required | Exact projection and sole entry to a fresh opaque mutation reference. |
| `create_reminder` | Required | Distinct idempotent creation contract and hybrid visible-URL behavior. |
| `change_reminder` | Required | Correctly consolidates patch, completion/reopen, and move under one discriminated action. |
| `delete_reminder` | Required | Destructive operation with final-absence semantics; should remain separate. |
| `ensure_reminder_list` | Required for practical use | Exact source/name selection and idempotent ensure semantics. |
| `inspect_reminder_native` | Optional differentiator | One polymorphic bounded read is preferable to separate section/tag/attachment/sync tools. |
| `create_reminder_section` | Optional differentiator | List-level operation cannot naturally fit reminder mutation reference semantics. |
| `organize_reminder` | Optional differentiator | Correctly groups section moves and tag assignment changes. |
| `change_reminder_attachment` | Optional differentiator | Correctly groups image/URL attach, replace, and delete while retaining type-specific validation. |
| `diagnose_reminders` | Required support surface | Failure-triggered, content-free, and scoped; fix/remove ineffective `full` projection. |

**Conclusion on tool count:** 13 is not intrinsically overbuilt. The recent Apple Calendar comparator exposes ten Calendar operations before counting its Reminders operations, while the current mature Google Calendar connector exposes fifteen actions. What matters is whether each tool corresponds to a user-recognizable capability and a distinct safety contract. This candidate generally does. The overbuilt material is behind the public surface: legacy adapter routes, deprecated direct writes, and maintenance/recovery plumbing bundled for compatibility.

## Regression-preservation audit: what must not be simplified away

| Failure class | Unsafe simplification | Current protection and executable evidence | Judgment |
|---|---|---|---|
| Visible URL | Treat EventKit metadata save as complete; attach URL later without a final exact read; blindly retry after helper failure | Hybrid URL flow performs EventKit save, guarded native URL attachment, and final exact EventKit read. `tests/test_mcp_server.py::test_hybrid_url_attachment_failure_reaches_public_partial_receipt`; Core tests ensure partial URL success issues no writable reference. | **Must preserve.** Removing the final read recreates stale `after`/reference state; blind retry can duplicate an attachment. |
| Image attachment | Accept arbitrary paths or bytes; skip decode/dimension checks; reuse a stale private version | Absolute regular non-symlink input, PNG/JPEG decode, 25 MiB, 16,384-per-dimension, and 40M-pixel bounds; guarded Native dispatch; attachment read-back and compensation/recovery tests. | **Must preserve.** This is both correctness and resource-exhaustion protection. |
| Tags | Resolve by display name globally; apply `limit` before account/list filtering; reuse pre-inspection private version | Exact reminder/list/account identity, bounded projections, fresh private revision immediately before mutation, adapter fixture tests for tag assignments and bounded reads. | **Must preserve.** Name-first simplification can mutate the wrong account or hide the intended tag behind an early limit. |
| Sections | Treat section name as global or derive list from a duplicate title | `list_id` is required; section reads/writes are exact-list scoped; Native tests verify exact scope and reference rotation. | **Must preserve.** Duplicate list names are a demonstrated architectural constraint. |
| Stale reference | Expose raw `last_modified`/private version fields or allow reusable IDs for writes | Opaque one-use `rev1` binds reminder/store/public revision/expiry; revalidation precedes Native private-version acquisition; stale/replayed refs fail before mutation. Core/Native tests cover consumption, rotation, stale rejection, and no fresh ref on unknown outcomes. | **Must preserve.** This is the central concurrency boundary. |
| Idempotency | Use only in-memory dedupe, store raw user text/key, or automatically replay unknown writes | Durable hashed idempotency store, input fingerprint conflict, no user text/key at rest, pending-state handling, and exact-read recovery. `tests/test_adapter_contract.py::test_repeated_key_replays_once_and_store_contains_no_user_text` and `::test_reusing_key_with_different_input_fails`; façade tests cover ensure/create replay and pending outcomes. | **Must preserve.** It prevents duplicates without turning an unknown commit into a blind retry. |
| Recovery/receipts | Map helper exception to `failed_no_mutation`; collapse manual repair into pending; call process exit `verified` | Unknown post-dispatch results become `committed_verification_pending`; partial composition remains `partial_success`; uncompensated writes remain `failed_manual_repair_required`; final absence/read-back required for terminal states. | **Must preserve.** Simplification would produce false success/failure and unsafe retries. |

A safe simplification removes inaccessible compatibility code **behind** the same ports and executable tests. An unsafe simplification removes the ports, exact identity, final reads, outcome states, or durable idempotency merely to reduce file count.

## Doctor audit

### What is good

- Content-free by explicit privacy flags.
- No permission prompt, app launch, private-framework load, or write.
- Static schema inspection uses read-only SQLite and anonymous counts, not reminder rows or names.
- Helper check is non-linking `clang -fsyntax-only`; it does not leave an executable.
- Missing canonical private-framework paths are treated as inconclusive when dyld shared-cache loading may still work.
- Scope filtering prevents an all-environment dump for a narrow failure.

### What needs correction

- Public `full` projection mismatch described in PI-1.
- Doctor cannot prove future writes, iCloud convergence, or device visibility; docs state this correctly and must continue to do so.
- Doctor should remain failure-triggered. Making it a startup preflight would add latency and permissions anxiety without proving the requested Core operation.

## Backup and recovery audit

The beta makes the correct distinction between **mutation recovery semantics** and **filesystem backup utilities**.

- Public mutation safety comes from exact identity, guard revalidation, one-use references, idempotency, final reads, and conservative receipts.
- Internal SQLite backup uses the SQLite online backup API and owner-only permissions; container backup is labeled best-effort live-container rather than transactionally consistent.
- Retention targets only managed filename patterns and protects the newly created backup.
- Automatic restore is absent. That is correct: a backup without a verified restore path is not a user-facing recovery guarantee.
- The recovery module remains packaged because deprecated maintenance seams remain. Once those seams are split out, the normal runtime should not carry backup/restore code it cannot expose safely.

## Deprecated-write audit

The public route is closed: unknown tools fail, skills specify MCP-only operation, production backend paths ignore legacy environment overrides, and the 13 public names are verified at load time. Therefore the deprecated direct-write CLI is **not a release blocker for this beta**.

It is still material trust debt. `tests/test_legacy_cli_deprecation.py` proves that direct write commands remain executable and are merely marked deprecated. `reminders_adapter.py` also contains maintenance and private-store mutation commands. Bundling them enlarges the installed attack/reviewer surface and makes future routing regressions more consequential. Split them before stable, but do not delete compatibility code until URL/image/tag/section/recovery behavior is protected at the replacement interfaces.

## Startup and request-latency audit

### Confirmed

- Tool discovery is compact and bounded; package tests verify exact discovery below 32 KiB.
- `server.py` lazily imports Core/Native façade backends, and `tests/test_mcp_server.py::test_tool_discovery_does_not_eagerly_import_facade_backends` protects startup from eager heavy imports.
- The benchmark harness is content-free and includes process startup in its measurements.
- Cached/fresh native helper paths are distinguished.

### Not yet proven for release

- Actual p95 values for the exact extracted candidate on the clean supported machine.
- The first Native request cost when helper compilation is required.
- The manifest's ambient interpreter startup, which package tests bypass with `sys.executable`.
- Cross-machine comparability; the benchmark itself correctly warns against it.

No performance regression is asserted without measurements. The release should attach benchmark JSON and use it as an environment-specific regression signal, not a universal SLA.

## Packaging audit

**Strong, subject to RB-1/RB-3.**

- Canonical marketplace source is the runtime subtree.
- Deterministic allowlist excludes tests, development docs, workflows, screenshots, `dist`, databases, journals, backups, caches, bytecode, and private media.
- Required release/legal/support documents are packaged.
- Runtime modules, helper sources, schemas, assets, and skills are included.
- Two builds are byte-identical in tests; archive audit and 1 MiB ceiling are enforced.
- Extracted ZIP and recursive marketplace copy initialize and list exactly 13 tools.
- Package runtime ignores backend override environment variables.

The package is compositionally ready. It is not yet operationally released because the real manifest interpreter/toolchain path and final tag artifact have not been exercised.

## Privacy audit

**Release posture is appropriate.**

- The server is local stdio and declares no public-web/open-world access.
- Structured results may contain requested reminder content; concise MCP text summaries avoid duplicating it. The privacy document correctly warns that the Codex host may transmit content according to its own settings and service behavior.
- Local journals/idempotency/cache data use redaction/hashing and owner-only permissions; package audits exclude local artifacts.
- Doctor is content-free and does not read journal/cache/backup contents.
- Shared-list writes can affect other participants; `verified` is limited to the named local read-back and never claims iPhone or full iCloud convergence.
- Image paths and attachment content remain local inputs but are sensitive; path/symlink/type/size/dimension checks should remain exact.
- Private APIs are disclosed. Release notes should repeat that disclosure because it materially affects compatibility and trust.

## Onboarding audit

The documentation covers the required topics and uses the correct operational order:

1. requirements and compatibility floor;
2. marketplace install and new-task refresh;
3. first prompts;
4. permission only after a Core call requires it;
5. Doctor only after a relevant failure;
6. upgrade, disable/remove/re-add, uninstall, and local-data cleanup;
7. troubleshooting for unsupported Python, stale references, pending verification, Native unavailability, and missing private-framework paths.

The remaining onboarding defect is evidence, not prose: the future-tag instructions have not been executed from a clean machine and immutable release artifact.

## GitHub release-readiness audit

| Area | Status | Evidence / action |
|---|---|---|
| PR review fixed point | Ready for audit | Exact head and base are known; PR remains draft/open. |
| CI | Green synthetic | Exact-head run passed macOS 14 / Python 3.11 and 3.12. |
| Package determinism/audit | Green in tests | Build, compare, allowlist, archive, ceiling, extracted initialization. |
| Marketplace/manifest/version | Internally aligned | Runtime subtree and 0.3.0 metadata are present. |
| Privacy/security/support/terms/changelog | Present and packaged | Add immutable tagged links to release record. |
| Clean marketplace install | **Missing — blocker** | RB-1. |
| Live disposable workflow | **Missing durable artifact — blocker** | RB-2. |
| Merge/tag/release/checksum | **Missing — blocker** | RB-3. |
| Exact release benchmark | Missing | PI-2. |
| Stable helper distribution | Deferred | Later work. |

## Primary-source comparison

### Recent Codex Apple Calendar plugin

Comparator: [`mightymattys/apple-productivity-mcp`](https://github.com/mightymattys/apple-productivity-mcp) at current main commit [`6caa7f896431a853cadad7a2564f9e10e838e332`](https://github.com/mightymattys/apple-productivity-mcp/commit/6caa7f896431a853cadad7a2564f9e10e838e332), released as [`v0.1.1`](https://github.com/mightymattys/apple-productivity-mcp/releases/tag/v0.1.1) on 2026-03-27.

First-party repository evidence shows:

- a user-facing Apple Calendar plugin with a Python CLI and Swift/EventKit backend;
- ten Calendar MCP operations: list calendars/events, find, add, update, delete, set/clear reminders, ICS export/import;
- auto-compilation of the Swift backend on first use;
- manual macOS permission setup;
- a shared Calendar/Reminders MCP layer and repository installer;
- smoke tests that create temporary artifacts and clean them up.

Useful lessons for this candidate are concise goal-oriented onboarding, explicit first-use compilation, and a disposable smoke test. The comparator is not a mature safety/recovery benchmark: its current shared server eagerly loads wrappers, exposes less bounded schemas, uses ordinary IDs/title-day matching rather than one-use revision references, and does not expose this candidate's structured receipt/Doctor/recovery model. Its checked-in shared server also contains a machine-specific root path, despite installer placeholder documentation. Apple Reminders should not copy that portability weakness.

### Mature Google Calendar connector

Observable first-party interface on 2026-08-25: fifteen connected actions—calendar/profile/color reads; bounded event search/read/batch read; availability; create/update/delete; invitation response; label list/update. The schema emphasizes explicit calendar IDs, bounded RFC 3339 windows, pagination, exact event IDs, read-before-update guidance for recurrence/attendees, recurring update scope, and ETag-protected silent label changes.

Official sources:

- [Apps in ChatGPT](https://help.openai.com/en/articles/11487775-connectors-in-chatgpt) describes plugin/app discovery, connection/authentication, and workspace permission administration.
- [Google App for ChatGPT — Data Controls FAQ](https://help.openai.com/en/articles/10408842) documents Google Calendar OAuth scopes and connected-data controls.
- [Google Calendar Events API](https://developers.google.com/workspace/calendar/api/v3/reference/events) documents event IDs, patch/update semantics, and ETag-based atomicity guidance.

The connector's backend implementation, internal module boundaries, latency data, recovery design, and test suite are not public through the connected action schema; no claims are made about them. The fair comparison is interface and UX only.

### Comparative judgment

| Dimension | Apple Reminders candidate | Recent Apple Calendar plugin | Mature Google Calendar connector |
|---|---|---|---|
| Public action count | 13 | 10 Calendar actions in shared server | 15 observable actions |
| Identity/concurrency | Exact IDs plus opaque one-use revision reference; fresh private version at dispatch | IDs or title/day matching; no comparable public revision token observed | Exact event IDs; ETag/concurrency behavior observable for specific actions/API guidance |
| Read bounds | Closed schemas, semantic date/list bounds, filter-bound cursor | Less explicit schema bounds | Explicit time windows, result limits, pagination |
| Mutation result | Structured receipts with final-read/unknown/partial/manual-repair states | Ordinary payload/error surface observed | Action results; backend recovery internals unavailable |
| Permission UX | Explicit TCC access tool; failure-triggered Doctor | Manual macOS permission setup | Managed OAuth connection and workspace/admin controls |
| Install UX | Local source package; ambient Python 3.11+; native compiler | Repository installer; `/usr/bin/python3`; first-use Swift compile | Managed connected app; no local compiler/runtime onboarding |
| Privacy model | Local stdio plus host-transmission disclosure; content-free Doctor; local artifacts | Local EventKit/plugin model | Connected Google data/OAuth controls; indexed/synced data behavior documented by OpenAI |
| Packaging | Deterministic audited ZIP; final tag not published | Source release and installer; no comparable audited artifact evidence reviewed | Managed service; implementation/package unavailable |

**Implication.** Tool count does not justify cutting the candidate below 13 by itself. The mature Google interface is not smaller, and the Apple Calendar plugin already uses ten Calendar operations. The candidate should compete by preserving exact identity, bounds, conservative receipts, and honest local compatibility—not by imitating managed-OAuth convenience it cannot provide. Its first-release burden is therefore a clean local install receipt and truthful Native capability evidence.

## Final release plan

### Required before public beta

1. Correct or remove public Doctor full-detail mode.
2. Produce RB-1 clean manifest-launch evidence from the exact package.
3. Produce RB-2 redacted live disposable Core/Native/TCC receipt from the exact package.
4. Run and attach exact-package benchmark JSON.
5. Merge the intended candidate, rebuild twice, audit, checksum, tag `v0.3.0`, publish release notes/artifact, install from tag, and verify exactly 13 tools in a new Codex task.

### Explicitly not required for the first beta

- large Core/Native/server refactoring;
- exposing backup, restore, repair, cleanup, log purge, flags, or UI selection;
- a companion macOS app;
- universal directory submission;
- guaranteed iCloud convergence or direct iPhone observation;
- prebuilt/signed helpers, provided the source-build requirement is clearly disclosed and clean-install evidence passes.

## Bottom line

The candidate's central architecture is sound: a small closed interface fronts deep Core, Native, and Diagnostics modules; exact identity and one-use revisions prevent stale writes; hybrid URL and private attachment flows preserve partial/unknown outcomes; package and privacy controls are stronger than typical local plugins. Simplifying those boundaries would recreate real regressions.

The release is not blocked by the existence of 13 tools or by a confirmed destructive runtime bug. It is blocked because the actual distributable has not yet been proven through its real interpreter/toolchain/TCC path, its version-sensitive Native behavior lacks a durable exact-artifact live receipt, and there is no immutable merged/tagged release identity. Close those three gates, fix the Doctor full-detail mismatch, attach benchmark evidence, and the candidate is suitable for a first public beta.
