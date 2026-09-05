# Privacy-safe external tester workflow

This workflow gathers narrow compatibility evidence for Apple Reminders for
Codex without collecting a person's Reminder content or machine-local
artifacts. Each receipt covers one exact package, environment, and scenario.

It connects the fresh TCC work in
[#28](https://github.com/Oscar-V4/apple-reminders/issues/28), the synthetic
demo and social-preview work in
[#29](https://github.com/Oscar-V4/apple-reminders/issues/29), the external Mac
matrix in [#30](https://github.com/Oscar-V4/apple-reminders/issues/30), and the
private-interface boundary in
[#41](https://github.com/Oscar-V4/apple-reminders/issues/41).

## Start only from a verified release

The checked-in `v0.6.0` commands are release-candidate instructions, not proof
that the tag or assets already exist. Do not recruit testers until the exact
tag is published as an immutable two-asset GitHub Release and the canonical
verifier succeeds from a clean tag checkout:

```bash
python3 scripts/verify_release_assets.py v0.6.0
```

That command must re-download the deterministic ZIP and `SHA256SUMS`, verify
the immutable release and shared two-subject SLSA provenance, bind the exact
tag to canonical GitHub main, rebuild the ZIP twice, audit source, and verify
the signed EventKit and Python manifests without accessing Apple Reminders data.

Install only the verified exact ref:

```bash
codex plugin marketplace add Oscar-V4/apple-reminders --ref v0.6.0
codex plugin add apple-reminders@oscar-v4-reminders
```

Start a new Codex task. Do not test a moving branch, substitute an unverified
ZIP, or silently change the tag during a scenario. Refreshing a marketplace
entry does not move its pinned ref.

## Product and privacy boundary

- Stable Core uses documented EventKit and targets macOS 14+. A bundled Python
  runtime needs no separate Python installation; Reminders permission is still
  required. Core does not need Xcode or Xcode Command Line Tools.
- Default discovery contains 9 Core and diagnostic tools. The 6 additional
  experimental tools require `--experimental` startup. Default URL writes store
  EventKit URL metadata only.
- Experimental Internals are private and version-sensitive. The
  compiler-free private paths and CLT-required private paths remain separate.
- Every private mutation or exact recovery requires the exact macOS
  version/build, Reminders version/build, and command-schema fingerprint in the
  reviewed allowlist. `runtime_unverified`, `unsupported_build`,
  `schema_unverified`, or `schema_fingerprint_mismatch` stops before mutation.
- CLT-required preflight uses fixed `/usr/bin/xcode-select -p`, rejects
  developer environment overrides, ignores `PATH` clang and the
  `/usr/bin/clang` installer shim, and accepts only the fixed compiler under the
  selected developer directory. Compiler presence is not compatibility proof.
- The plugin-owned MCP and adapters run locally and have no plugin-owned remote
  backend, but tool results return to Codex under the tester's Codex product,
  account, and privacy terms.
- Use only a newly created disposable Reminder List, synthetic Reminder values,
  and—when an admitted image test is intentionally selected—a generated
  synthetic image. Delete the exact synthetic items and list when possible.
- Do not reset TCC or change a person's normal privacy settings merely to make
  a scenario available. Use a new macOS user, disposable VM, or separate Mac.

Never submit Reminder or list titles, notes, IDs, account data, URLs,
attachments, screenshots, logs, Doctor output, hashes, database contents,
build strings, local paths, prompt wording, or free-form machine details. The
receipt records only exact plugin/Codex versions, hardware class, macOS and
bundled Python version/source, optional external Python presence, Xcode/Command
Line Tools state,
bounded scenario outcomes, stable error categories, and cleanup state.

## Receipt format

Copy
[`examples/external-tester-receipt.example.json`](examples/external-tester-receipt.example.json)
and select exactly one scenario. The closed
[`external-tester-receipt.schema.json`](external-tester-receipt.schema.json)
has no field for personal content, identifiers, screenshots, logs, hashes, or
paths. The checked-in example is synthetic documentation, not external
evidence. Older receipt source categories remain valid for older versions. The
optional `external_python` field records `absent`, `installed`, or `not_checked`;
only `absent` supports the specific no-external-Python environment claim.

Validate before sharing:

```bash
python3 scripts/validate_external_tester_receipt.py receipt.json
```

The validator reports only `valid` or a stable validation category and does not
echo receipt values or the submitted path. A valid receipt proves only that the
closed format and scenario invariants passed; it does not prove the test ran.

Allowed stable error categories include Core failures plus the exact
Experimental gate reasons `runtime_unverified`, `unsupported_build`,
`compiler_required`, `schema_unverified`, and
`schema_fingerprint_mismatch`, and the public codes `unsupported_capability`
and `schema_mismatch`. Do not paste raw tool errors or diagnostic prose.

Each scenario includes `release_verification`. Core mutation scenarios also
include `core_canonical_alarm`, which verifies through public behavior that a
synthetic absolute, location, writable-relative, or read-only alarm is not
silently lost or transformed by an unrelated title, completion/reopen, or list
change. Record only the bounded outcome, never the synthetic content.

## Scenario checklists

### `fresh_core_allow`

Use a fresh TCC subject with external Python, full Xcode and Command Line Tools
all absent. Record `python.source: bundled` and `external_python: absent`; a
bundled interpreter alone does not prove that external Python is absent.

- Confirm only `xcode: absent` and `command_line_tools: absent`; do not submit
  command output or a tool path.
- Verify the release, install the exact tag, start a new Codex task, and run one
  bounded Stable Core read.
- Request access once, grant the native prompt, and retry the same read once.
- Create, exactly read, change, complete/reopen, and delete synthetic Reminders.
- Exercise one synthetic canonical alarm preservation path and record only its
  pass/fail category.
- Verify exact active-list cleanup, then delete the exact empty list in the
  Reminders app.
- Record `tcc_precondition: not_determined` and
  `tcc_result: granted_after_prompt` only when those observations are true.

### `fresh_core_deny`

Use a separate fresh TCC subject with external Python, full Xcode and Command
Line Tools absent. Record `external_python: absent` only after checking that
precondition; leave it `not_checked` when it has not been checked.

- Verify the release and install the exact tag.
- Request access once and deny the native prompt.
- Confirm there is no prompt loop or blind automatic retry.
- Do not attempt a synthetic mutation after denial.
- Record only `permission_denied`; mark cleanup `not_run` when nothing was
  created.

### `intel_core`

Use a real Intel Mac. An `x86_64` slice in the package is not execution
evidence.

- Verify and install the exact release.
- Exercise bounded Stable Core read, synthetic CRUD, canonical alarm
  preservation, and exact cleanup without resetting TCC.
- Record only `hardware: intel`; do not submit model or serial details.

### `minimum_macos_core`

Use an actual macOS 14.x subject rather than deployment-target metadata.

- Keep the subject on its existing supported 14.x patch; do not downgrade a
  person's primary Mac.
- Verify and install the exact release, then exercise the same Stable Core CRUD
  and canonical alarm path.
- Record the numeric macOS version only, without build strings or host names.

### `upgrade_identity`

The current release-candidate transition is `v0.5.2` to `v0.6.0`.

- On a disposable subject with Reminders permission granted to the `v0.5.2`
  signed helper, create one synthetic Reminder with an alarm and read it back.
- Verify `v0.6.0`, remove the plugin and repo marketplace entry, add the repo at
  `v0.6.0`, re-add the plugin, and start a new Codex task.
- Run a bounded read and one unrelated synthetic change, confirming canonical
  alarm state through a fresh exact read-back.
- Record only `granted_without_prompt` or `granted_after_prompt`; do not submit
  process wording, screenshots, TCC database data, or signing output.
- Perform exact synthetic cleanup.

### `clt_only_experimental`

Use a disposable subject with full Xcode absent and Command Line Tools already
installed intentionally.

- Verify and install the exact release, then prove Stable Core remains usable.
- Record only the state classification; do not submit `xcode-select` output or
  the selected path.
- Run metadata-only targeted diagnosis first. Use Experimental toolchain mode
  only for the requested CLT-required capability.
- Select an allowlisted CLT-required path, such as synthetic image-attachment
  mutation, only if exact runtime/schema admission succeeds. Section mutation
  currently has no admitted command-schema evidence and must remain blocked.
- If admission reports `runtime_unverified`, `unsupported_build`,
  `compiler_required`, `schema_unverified`, or
  `schema_fingerprint_mismatch`, stop before mutation and record the bounded
  category. A fail-closed receipt is valid evidence for that environment.
- If an admitted mutation runs, perform its exact read-back and delete the
  synthetic Reminder, generated image, and disposable list. Never test a
  person's real sections, attachments, or Recently Deleted content.

## Sharing a receipt or failure

1. Run the validator locally.
2. Read the JSON once and confirm that every value is from the closed enums or
   version fields. Do not add explanatory keys.
3. Add one validated receipt to issue #30. A fresh TCC receipt may also be
   linked from issue #28; a CLT-only Experimental receipt may be linked from
   issue #41.
4. For a failure, open a separate bug through the issue chooser and include
   only the stable error category plus a minimal synthetic reproduction.
5. Treat one receipt as evidence for that exact environment, never for every
   Intel Mac, every macOS 14 build, or every future Reminders version.

The 60–90 second recording and GitHub social-preview setting remain manual work
in issue #29. Do not attach a recording to a tester receipt, change repository
settings, or post to a social channel as part of this workflow.
