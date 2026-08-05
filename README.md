# Apple Reminders Codex Plugin

Apple Reminders is an experimental, local macOS Codex plugin. It provides a
bundled stdio MCP server, task-oriented skills, and narrow adapters for reading
and changing the current user's Apple Reminders data.

This is not an Apple-supported integration, an App Store component, or a claim
of stable compatibility with every macOS release. Public APIs are preferred.
Some advanced operations rely on private frameworks or the private Reminders
store and must pass build-, permission-, schema-, and read-back gates at run
time.

## Release Status

- `.codex-plugin/plugin.json` declares the bundled skills and the substantive
  local MCP configuration in `.mcp.json`.
- `mcp/server.py` is a local JSON-RPC/stdio transport. It does not provide or
  contact a remote MCP endpoint.
- Public reminder fields use EventKit or AppleScript-backed paths where the
  requested operation is supported.
- Sections, tags, URL/image attachments, repair workflows, and some full-grasp
  reads may use version-sensitive private storage or ReminderKit paths.
- A zero exit code alone is never sufficient evidence that a mutation reached
  Reminders, iCloud, another device, or a shared-list participant.

The repository also contains a separate, reduced OpenMinis export under
`minis/apple-reminders/`. It is not part of the installable macOS plugin
artifact and does not include private adapters.

## Architecture and Trust Boundary

| Layer | Role | Trust and compatibility boundary |
|---|---|---|
| Skills | Bounded reads, ambiguity handling, mutation policy, and reporting | Instructions only; they do not grant macOS permissions |
| Local MCP | Typed tools over stdio; invokes the bundled adapter | Runs with the Codex host user's filesystem and process permissions |
| EventKit helper | Public reminders access for supported fields | Requires macOS Reminders permission; runtime success is permission/account dependent |
| AppleScript | Public app automation and native deletion fallback | May require Automation permission and an available Reminders app |
| SQLite adapter | Sections, tags, URL attachments, bounded cache/audit/repair surfaces | Private schema; can break after a macOS update; writes must be schema-gated and verified |
| ReminderKit helper | Native image-attachment path | Private framework; not an App Store API; mobile-sync evidence is not an iPhone-screen confirmation |
| Doctor | Content-free metadata, schema, permission-symptom, and toolchain checks | Does not prove write semantics, iCloud convergence, or device visibility |

The MCP process can invoke tools that mutate reminders. Those changes may sync
through the user's Apple account and may affect shared lists. Review tool
arguments, target IDs, result status, warnings, and recovery semantics before
treating an operation as complete.

## Result Semantics

Mutation responses use explicit states such as:

- `unchanged`: no mutation was necessary.
- `verified`: the selected backend produced the stated local verification
  evidence.
- `committed_verification_pending`: a write may have committed, but the required
  read-back is not yet available.
- `partial_success`: only part of the requested operation was verified.
- `failed_no_mutation`: the operation failed before a known mutation.
- `failed_manual_repair_required`: a partial mutation could not be compensated
  automatically.

`verified` is scoped to the verification object in that response. It does not
implicitly mean “synced to iCloud,” “visible on iPhone,” or “observed by every
shared-list participant.” For attachments, `mobile_visible_likely` or similar
fields are sync evidence only. Actual device confirmation requires an actual
device/UI observation.

## Requirements

- macOS with Apple Reminders available for the current user.
- Python 3.10 or newer.
- Xcode command-line tools for non-linking syntax checks and locally compiled
  native helpers.
- Reminders access for EventKit and, when used, Automation access for
  AppleScript.
- Explicit acceptance of the private-interface risk before using ReminderKit
  or SQLite-backed advanced operations.

Do not assume support solely from the OS version. Run the doctor after macOS or
Reminders updates and treat an unknown schema, missing entitlement/permission,
or failed helper check as a blocked capability.

## Local Validation

These checks do not launch Reminders or access live reminder rows:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 scripts/validate_plugin.py .
PYTHONDONTWRITEBYTECODE=1 python3 scripts/validate_minis_export.py
PYTHONDONTWRITEBYTECODE=1 python3 scripts/audit_source_package.py .
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_*.py'
```

Native source checks on macOS:

```bash
clang -x objective-c -fobjc-arc -framework Foundation -framework AppKit \
  -fsyntax-only scripts/remkit_attach_image.m
clang -x objective-c -fobjc-arc -framework Foundation -framework EventKit \
  -fsyntax-only scripts/reminders_eventkit.m
plutil -lint scripts/eventkit_bridge_info.plist
```

The EventKit request validator can be exercised without compiling a helper,
requesting permission, or reading Reminders:

```bash
printf '%s\n' '{"schema_version":1,"operation":"capabilities"}' | \
  PYTHONDONTWRITEBYTECODE=1 python3 scripts/eventkit_bridge.py --validate-only
```

The onboarding doctor is a separate local diagnostic. It intentionally reads
filesystem metadata and the Reminders database schema, but not reminder rows,
titles, notes, list/section/tag names, journal contents, cache contents, or
backup contents; it does not write, launch Reminders, or request permission:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 scripts/reminders_doctor.py --compact
```

Read the emitted `privacy`, `checks`, and `capabilities` fields. A static pass
is a prerequisite, not a guarantee that a future write is semantically safe.

### Data-free performance benchmark

The repeatable benchmark measures cold MCP initialization/tool discovery,
Python-only EventKit request validation, an isolated-home doctor run, source
audit, and deterministic package build time. It reports median and p95 latency
plus allowlisted source/archive bytes:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 scripts/benchmark_plugin.py \
  --label local-baseline --samples 15 --warmups 3 \
  --build-samples 5 --build-warmups 1 --output benchmark.json
```

It does not load EventKit, request TCC access, open Reminders, or read reminder
rows. Use `--plugin-root` to compare another clean checkout on the same machine.
Timing results are advisory because host load varies; the deterministic release
archive has a CI-tested 800,000-byte budget.

## Deterministic Source Package

The release ZIP is built from a runtime-only allowlist. It contains the plugin
manifest, MCP config/server/schema, reviewed brand assets, runtime skills, root
license/privacy/readme files, and the adapter/native-helper sources required by
those components. It intentionally excludes tests, contributor docs, GitHub
workflows, the OpenMinis export, reverse-engineering screenshots, bytecode,
databases, journals, caches, backups, and pre-existing archives.

Build and audit without contacting Reminders:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 scripts/build_source_package.py \
  --output-directory dist
```

The builder normalizes ZIP timestamps, permissions, member ordering, and
compression mode, audits the resulting versioned archive, and prints its path
and SHA-256. It refuses version/filename drift and unexpected package members.
Ignored local artifacts are reported by name and excluded without opening
screenshot, database, backup, or archive contents. Use `--strict-worktree`
when a completely clean checkout is required.

## MCP and Adapter Surfaces

The MCP tool contract lives in `schemas/mcp-tools.json`; the adapter CLI is
`scripts/reminders_adapter.py`. Inspect the current schema or run `--help`
instead of relying on an old command list. Broadly, the implementation covers:

- bounded account/list/section/reminder/tag and attachment reads;
- exact-ID EventKit reminder create/update/complete/reopen/move operations and
  exact-ID native deletion;
- list creation by exact name with optional color and emblem, plus section and
  tag operations;
- first-class MCP tools for URL/image attachment, exact attachment replacement
  and deletion, and digest-bound preview/apply repair workflows where the
  current private-interface capability gate permits them;
- local diagnostics, backup, cache, and privacy-log maintenance.

Unsupported, ambiguous, permission-blocked, schema-unknown, or
verification-pending requests must be reported as such. The plugin must not
invent support or silently drop fields.

## Local Files and Recovery

Depending on the commands used, the adapter can create data under:

- `~/Library/Application Support/apple-reminders-codex/`
- `~/Library/Caches/apple-reminders-codex/`

These locations can contain sensitive identifiers, cached titles/list names,
operation metadata, compiled helpers, capability records, and optional full
Reminders-container backups. Backups are best-effort snapshots of a live
container, not guaranteed transactionally consistent recovery points. See
[PRIVACY.md](PRIVACY.md) before enabling advanced writes or sharing diagnostic
output.

## OpenMinis Boundary

Do not submit this full macOS plugin or its private adapter to OpenMinis. The
allowlisted export is only:

```text
minis/apple-reminders/
├── SKILL.md
└── evals/
    └── evals.json
```

Validate that export with `python3 scripts/validate_minis_export.py`. It targets
only the built-in `apple-reminders` command and omits MCP, macOS adapters,
private frameworks/storage, caches, backups, attachments, repair flows, and
local evidence.

## License and Contributions

See [LICENSE](LICENSE), [PRIVACY.md](PRIVACY.md), and the upstream
[contribution guide](https://github.com/Oscar-V4/apple-reminders/blob/main/CONTRIBUTING.md).
Do not include real reminder data, screenshots, databases, archives, backups,
journals, or caches in issues, fixtures, commits, or release artifacts.
