# Contributing

Apple Reminders is a local macOS Codex plugin with real mutation capability and
version-sensitive private integration paths. Contributions must preserve
explicit trust boundaries, machine-readable failure semantics, bounded reads,
and deterministic source packaging.

## Required Checks

Run from the repository root without launching Reminders or using a live
Reminders store:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 scripts/validate_plugin.py plugins/apple-reminders
PYTHONDONTWRITEBYTECODE=1 python3 scripts/validate_minis_export.py
PYTHONDONTWRITEBYTECODE=1 python3 scripts/audit_source_package.py plugins/apple-reminders \
  --strict-worktree --verify-root-document-mirrors
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_*.py'
```

On macOS, also validate all native sources and the EventKit plist:

```bash
clang -x objective-c -fobjc-arc -framework Foundation -framework AppKit \
  -framework ImageIO \
  -fsyntax-only plugins/apple-reminders/scripts/remkit_attach_image.m
clang -x objective-c -fobjc-arc -framework Foundation -framework AppKit \
  -fsyntax-only plugins/apple-reminders/scripts/remkit_sections.m
clang -x objective-c -fobjc-arc -framework Foundation -framework EventKit \
  -fsyntax-only plugins/apple-reminders/scripts/reminders_eventkit.m
clang -x objective-c -fobjc-arc -framework Foundation \
  -fsyntax-only plugins/apple-reminders/scripts/remkit_recover.m
plutil -lint plugins/apple-reminders/scripts/eventkit_bridge_info.plist
```

Exercise request validation without compiling or accessing EventKit:

```bash
printf '%s\n' '{"schema_version":1,"operation":"capabilities"}' | \
  PYTHONDONTWRITEBYTECODE=1 python3 plugins/apple-reminders/scripts/eventkit_bridge.py --validate-only
```

CI must use synthetic fixtures and static validation. Do not add a CI step that
opens a user's Reminders database, launches Reminders, prompts for permission,
loads a private framework, or performs a write.

## Performance and opt-in live validation

The data-free benchmark measures MCP discovery, diagnosis, EventKit helper
validation/build, source audit, and deterministic packaging. Timings are local
end-to-end subprocess wall time, not cross-machine scores:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 scripts/benchmark_plugin.py \
  --label local-baseline --samples 15 --warmups 3 \
  --build-samples 5 --build-warmups 1 --output benchmark.json
```

Only after every data-free check passes, a maintainer may run the disposable
live harness. First read `list_reminder_lists` and copy the exact writable
`source.id`, then run:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 scripts/live_smoke.py \
  --bundled-runtime --confirm-live-reminders --source-id '<exact-source-id>'
```

For source-only Doctor output, run
`PYTHONDONTWRITEBYTECODE=1 python3 plugins/apple-reminders/scripts/reminders_doctor.py --compact`;
add `--detail-level full` only for one specific warning or blocked capability.
The default is metadata-only and starts no developer tool. Add
`--run-experimental-toolchain-check` only when explicitly testing a
CLT-required Experimental capability; it never installs developer tools.

This command writes to the maintainer's real Reminders store and must never run
in CI. It creates a uniquely named synthetic list, exercises public Core flows by default; `--experimental` explicitly adds the historical Native flows through the installable stdio server, and deletes that exact list
at the end. Output is limited to step, status, and latency. If cleanup cannot be
proved, do not retry blindly: inspect only the printed reserved list name.
Deleted synthetic content may remain in **Recently Deleted**; the harness must
not empty that recoverable system area.

Mock backend substitution is source-test-only. Construct
`mcp.server.BackendPaths(adapter=..., eventkit_bridge=..., doctor=...)` and pass
it to `mcp.server.main(backend_paths=...)` through the source-only test harness.
Do not add environment-variable path overrides: packaged runtime always resolves
its bundled adapter, EventKit bridge, and Doctor.

## Plugin and MCP Contract

- Keep `plugins/apple-reminders/.codex-plugin/plugin.json` name aligned with the plugin directory and
  use strict semantic versioning.
- Declare `mcpServers` only when `plugins/apple-reminders/.mcp.json` is substantive and all referenced
  server/schema files are packaged. If the MCP is removed, remove the manifest
  declaration, config, runtime files, documentation, and tests together.
- Keep the complete schema catalog at 15 tools: eight Core, four Native
  Extension, two Recovery, and one Diagnostics tool. ADR 0021 sets the default
  runtime to nine Core/Diagnostics tools; Experimental startup advertises 15.
- Keep MCP inputs closed-schema, bounded, semantically scoped, and exact-ID
  based. Result envelopes and Receipts must pass the centralized validator even
  though optional MCP `outputSchema` descriptors are omitted from discovery.
- Keep the MCP server a thin transport and contract boundary over the Core,
  Native Extension, and Diagnostics Modules. Backend selection, reference
  revalidation, mode-bound URL behavior, and read-back belong behind those Module
  Interfaces rather than in skills.
- Existing-item public mutations consume an opaque `rev1` Reference from an
  exact read. Do not expose raw `last_modified`/`reminder_version` selection to
  callers or add routes that let those preconditions be mixed.
- Update the MCP `SERVER_VERSION` semantic core with the plugin manifest when a
  release changes it. The validator rejects drift.
- Do not add unsupported manifest fields or unresolved placeholders.

The complete catalog is listed below. Default discovery contains only the
eight Core tools and Diagnostics. `--experimental` exposes the six Native and
Recovery tools and hybrid URL behavior, while retaining all private admission
gates. See ADR 0021; tests must cover both mode isolation and direct-call
rejection before dispatch.

The catalog is:

- Core: `request_reminders_access`, `list_reminder_lists`, `fetch_reminders`,
  `read_reminder`, `create_reminder`, `change_reminder`, `delete_reminder`, and
  `ensure_reminder_list`;
- Native Extension: `inspect_reminder_native`,
  `create_reminder_section`, `organize_reminder`, and
  `change_reminder_attachment`;
- Recovery: `inspect_recently_deleted` and `recover_deleted_reminder`;
- Diagnostics: `diagnose_reminders`.

Unused-tag cleanup, raw attachment export, attachment repair, broad
backup/Snapshot restore, log purge, flag mutation, and `show_reminder` are
withheld. Their obsolete adapter commands, together with the former direct Core
write and cache routes, were physically removed before 0.4. Do not add hidden
aliases, compatibility shims, or public-skill fallbacks for them.

## Skills and Evals

Every directory under `plugins/apple-reminders/skills/` must contain:

- `SKILL.md` with matching kebab-case frontmatter name and a specific trigger
  description;
- `agents/openai.yaml` with display name, short description, and default prompt;
- `evals/evals.json` with at least two non-empty cases.

Keep skills under 500 lines, keep local links resolvable, and add/update evals
when behavior changes. The reduced OpenMinis export has a separate schema and
must continue to pass `scripts/validate_minis_export.py`.

## Safety and Truthfulness

- Read current, bounded state before mutations.
- Resolve ambiguous names to exact stable IDs.
- Preserve fields the user did not ask to change.
- Consume and revalidate a fresh opaque Reference for every existing-item
  mutation. Reject expired, stale, replayed, or failed-revalidation tokens
  before dispatch; consume a token after a possible post-dispatch mutation.
- Issue a `del1` recovery Reference only from an exact Recently Deleted item
  read. Keep private store/revision/attachment guards server-side, consume the
  token once, and never turn list discovery into write authority.
- Cross-Reminder image copy revalidates and consumes distinct source and
  destination `rev1` References, keeps backing paths private, preserves the
  source, and requires exact destination byte-identity evidence.
- Pagination continuations remain bound to the ordered identifier/revision
  snapshot that issued them and fail read-only when that snapshot changes.
- Keep any internal destructive/bulk operation previewable, bounded, and
  recoverable where possible. Its internal safeguards do not make it a public
  capability.
- Prefer EventKit for fields and deletion that its public API supports. Do not
  reintroduce an AppleScript mutation fallback or expose UI handoff until exact
  selection is observable.
- Gate private SQLite writes on explicit schema requirements, a fresh matching
  internal reminder version for existing-item mutations, transactions,
  read-back, and truthful recovery semantics. The public MCP obtains that
  version by unwrapping/revalidating the Reference. Its delete path stays in
  EventKit; there is no adapter DB-delete fallback.
- Treat ReminderKit and private store behavior as version-sensitive. Never turn
  a failed private path into an unreported fallback.
- Never hard-delete reminder rows. A URL attachment object row may be removed
  only under the documented native-parity contract: exact private-schema gate,
  fresh reminder version, one transaction, retained-and-bumped cloud-state
  tombstone, and read-back. ADR 0009's unused-label cleanup was historical and
  its implementation was removed by ADR 0014.
- Never infer success from process exit status alone.
- `verified` must name its evidence. Do not translate local/CloudKit evidence
  into “confirmed on iPhone” or guaranteed iCloud/shared-list delivery.
- Preserve `committed_verification_pending`, `partial_success`,
  `failed_no_mutation`, and `failed_manual_repair_required` distinctions.
- Verify existing-Reminder mutations at both native and public seams with the
  canonical requested-delta-plus-stable-user-state projection. Preserve alarm
  duplicate counts while ignoring order; exclude only the documented
  provider-owned or derived fields. Any mismatch must issue no fresh `rev1`.
- Preserve the real-use regressions behind the smaller Interface: visible URL
  final read, ReminderKit image/section saves, exact-list section scope,
  fresh-revision tag changes, create idempotency, concurrency rejection, and
  normalized Receipts.
- Run normal bounded operations without Doctor preflight. Route to the
  content-free `diagnose_reminders` only after a relevant permission,
  environment, build, schema, or native-capability failure.

## Privacy and Fixtures

Use only synthetic fixtures. Never commit, package, or attach:

- real reminder titles, notes, URLs, account/list/section/tag names, IDs, or
  attachment contents;
- Reminders SQLite files or WAL/SHM files;
- journals, caches, idempotency/capability records, schema dumps tied to a user,
  or container backups;
- `.DS_Store`, bytecode, screenshots/UI captures, archives, recovery copies, or
  compiled helpers other than the exact reviewed components described below.

The EventKit compiled-artifact exception is
`plugins/apple-reminders/native/AppleRemindersEventKitHelper.app` together with
`native/eventkit-helper-build.json`. It must come from the protected signing
workflow, retain the exact allowlisted file tree, pass Developer ID,
notarization, Gatekeeper, source-hash, mode, and artifact-attestation
verification, and enter the repository through normal review. Never commit a
local or ad-hoc build.

The only other binary exceptions are the two signed Python capsules and their
three metadata files under `plugins/apple-reminders/runtime/`, described below.
This does not permit arbitrary archives, downloaded tools, or locally built
executables in the package.

Do not inspect the contents of a suspected screenshot, archive, backup, or user
data file merely to decide whether it belongs in the package. Classify it by
path and reject it. Redact diagnostics before sharing them.

## Deterministic Release Artifact

### Bundled Python runtime

The runtime build is separate from the EventKit helper. Its pinned upstream
inputs and licenses are described by `scripts/python-runtime-lock.json` and
ADR 0022. Build only through `scripts/build_python_runtime.py`; the two
architecture-specific runtime apps are never compiled on an end user's Mac.

The trusted `prepare-signed-runtime-source.yml` workflow must land on main
before dispatch. It builds an exact reviewed source commit, signs without
checking out or executing that source in the credentials job, verifies both
architectures on native hosted runners, and attests the exact five candidate
subjects. Only its reviewed ZIPs, manifests, and `SHA256SUMS` may enter
`plugins/apple-reminders/runtime/`. Keep runtime artifacts outside `native/`,
which the existing EventKit-helper preparation workflow replaces as a unit.

The outer source package stores these already-compressed capsules unchanged.
`verify_python_runtime.py` checks their identities, inventories, licenses,
signatures, and executable behavior. `verify_runtime_provenance.py` additionally
proves ancestry, historical build inputs, and the exact workflow attestation.
Runtime C/plist/lock changes require a fresh runtime; an unrelated plugin
version or CI-tooling edit does not change the identity of an existing runtime.

The ordinary `.mcp.json` uses `scripts/launch_bundled_mcp.sh`. Both signed
capsules and the matching plugin-version EventKit helper must remain assembled
and verified together. The bundled launcher has no external Python or download
fallback. Its fixed `--render-daily-brief` entrypoint also runs the skill's
renderer without requiring a host Python installation.

### Plugin package

Build only through the allowlisted packager:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 scripts/build_source_package.py \
  plugins/apple-reminders \
  --output-directory dist
```

The package intentionally excludes `.github/`, `tests/`, `docs/`,
`CONTRIBUTING.md`, `minis/`, and the packaging scripts themselves. Adding a new
runtime file requires an intentional allowlist update plus a packaging test.
Run the build twice in separate empty directories and compare SHA-256 values
before a release. Do not hand-assemble a ZIP from the working tree.

The EventKit helper has a separate trusted-workflow release boundary. The
definition of `prepare-signed-helper-source.yml` must already be merged on the
protected default branch before it can prepare another branch's source. The
repository owner dispatches that default-branch workflow with one exact
`source_ref` and `source_commit`. Credentials-free jobs prove the remote ref
still resolves to that commit, then build and test the target source. The
protected signing job checks out and executes no repository code. Post-signing
execution runs without OIDC or attestation permissions; a final no-checkout
job revalidates the immutable subject inventory and digests immediately before
attestation. The manifest records both the target `source_commit` and the
trusted default-branch `workflow_commit`. If the trusted workflow itself must
change, merge and review that infrastructure change first, then dispatch it to
prepare the source-and-artifact pull request. The later tag workflow remains
secrets-free and verifies the workflow attestation plus target-source ancestry;
mutable branch-name labels are not trusted. Signing keys, API keys,
certificate archives, and their passwords must never enter the worktree,
logs, artifacts, pull requests, or issues. See
[`0019-prebuilt-signed-eventkit-core-helper.md`](docs/decisions/0019-prebuilt-signed-eventkit-core-helper.md)
for the trust model.

## Private API Review

Changes involving private ReminderKit or the Reminders database must document:

- the exact macOS/Reminders build and schema fingerprint used for evidence;
- public alternatives considered;
- fields/tables/selectors touched and transaction boundaries;
- preconditions, read-back evidence, partial-failure behavior, and recovery;
- why the feature remains blocked when any prerequisite is unknown.

Compatibility is positive admission, not a range inference. A private mutation
may be added to `experimental_capabilities.py` only when one reviewed evidence
row binds the exact macOS version/build, Reminders version/build, command-schema
fingerprint, semantic mutation, and read-back. Missing evidence leaves the
capability disabled. Do not add environment-variable bypasses, minimum-version
wildcards, compiler-only approval, selector-only approval, or automatic fallback
writes. Command Line Tools are a dependency, not compatibility evidence.
Resolve an Experimental helper compiler only through fixed
`/usr/bin/xcode-select -p` and a fixed compiler-relative path beneath its
selected developer directory. Never trust `PATH`, a `/usr/bin/clang` installer
shim, `DEVELOPER_DIR`/`TOOLCHAINS` overrides, or a caller-supplied compiler.

Synthetic tests must prove that unknown builds, incomplete runtime metadata,
schema drift, and missing required compilers fail before store resolution or
mutation dispatch. Separate tests must preserve conservative pending/manual
classification after partial native failure and reject recovery guard mismatch
before save.

Keep this private integration out of OpenMinis contributions unless that
project explicitly accepts the dependency. Use only `minis/apple-reminders/`
for the reduced public command surface.
