# Apple Reminders Codex Plugin

Apple Reminders is a local macOS Codex plugin being prepared for its first 0.3
public beta. It provides a bundled stdio MCP server, task-oriented skills, and
narrow adapters for reading and changing the current user's Apple Reminders
data.

This is not an Apple-supported integration, an App Store component, or a claim
of stable compatibility with every macOS release. Public APIs are preferred.
Some advanced operations rely on private frameworks or the private Reminders
store and must pass build-, permission-, schema-, and read-back gates at run
time.

## Quick Start

The commands below target the tagged 0.3.0 repo-marketplace release. Until that
tag exists, treat this repository as development source rather than a finished
installer.

```bash
codex plugin marketplace add Oscar-V4/apple-reminders --ref v0.3.0
codex plugin add apple-reminders@oscar-v4-reminders
```

Start a new Codex task so it loads the installed tools and skills, then try one
of these prompts:

```text
오늘 할 일 보여줘.
쇼핑 목록에 우유 사기를 추가해줘.
```

The first operation that needs Apple Reminders will cause macOS to request
Reminders access for the app running Codex. Approve it only if the displayed
app and requested operation are expected. A normal first task does not require
diagnostics or private-interface setup.

Supported source-beta environment:

- macOS 14 or newer with Apple Reminders available for the current user.
- Python 3.11 or newer.
- Xcode command-line tools while native helpers are distributed as source.

Native Extension operations such as image attachments and sections may rely on
version-sensitive Apple interfaces. Core Reminder work remains separate from
those capabilities.

## Upgrade

Refresh the pinned marketplace snapshot, reinstall the plugin, and start a new
Codex task:

```bash
codex plugin marketplace upgrade oscar-v4-reminders
codex plugin add apple-reminders@oscar-v4-reminders
```

Read [CHANGELOG.md](CHANGELOG.md) before crossing a minor or major version.

## Uninstall

Remove the installed plugin without changing Apple Reminders data:

```bash
codex plugin remove apple-reminders@oscar-v4-reminders
```

To remove the marketplace source as well, run
`codex plugin marketplace remove oscar-v4-reminders`. Plugin removal does not
delete optional local journals, caches, or Snapshots. Review
[PRIVACY.md](PRIVACY.md) and inspect the documented local-data directories
before choosing whether to remove them separately.

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
| ReminderKit helpers | Native image-attachment and list-section save paths | Private framework; not an App Store API; CloudKit read-back is not an iPhone-screen confirmation |
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

- macOS 14 or newer with Apple Reminders available for the current user.
- Python 3.11 or newer.
- Xcode command-line tools for non-linking syntax checks and locally compiled
  native helpers.
- Reminders access for EventKit and, when used, Automation access for
  AppleScript.
- Explicit acceptance of the private-interface risk before using ReminderKit
  or SQLite-backed advanced operations.

Do not assume Native Extension support solely from the OS version. When a
specific operation reports an environment or native-capability failure, run
targeted summary diagnosis and treat an unknown schema, missing permission, or
failed runtime probe as limited to the affected capability.

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

The default is a concise summary. Add `--detail-level full` only when
troubleshooting a warning or blocked capability. Read the emitted `privacy`,
`checks`, and `capabilities` fields. A static pass is a prerequisite, not a
guarantee that a future write is semantically safe.

### Data-free performance benchmark

The repeatable benchmark measures cold MCP initialization/tool discovery, the
actual MCP-to-doctor route in an isolated home, Python-only EventKit request
validation, fresh and cached EventKit helper builds, source audit, and
deterministic package build time. It reports median and p95 latency, evaluates
generous p95 regression budgets for the production startup paths, and records
allowlisted source/archive bytes:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 scripts/benchmark_plugin.py \
  --label local-baseline --samples 15 --warmups 3 \
  --build-samples 5 --build-warmups 1 --output benchmark.json
```

It compiles but never runs the EventKit helper, does not request TCC access, open
Reminders, or read reminder rows. Use `--plugin-root` to compare another clean
checkout on the same machine. A p95 budget failure exits with status 2; use
`--no-enforce-performance-gates` only for an advisory diagnostic run. Timing
is end-to-end subprocess wall time, including Python/process startup. It varies
with host load and is not a cross-machine score. The deterministic allowlist is
the package-content boundary, benchmarks expose exact source/archive bytes, and
CI applies a 1 MiB hard ceiling against gross growth.

## Deterministic Source Package

The release ZIP is built from a runtime-only allowlist. It contains the plugin
manifest, MCP config/server/schema, reviewed brand assets, runtime skills, root
license/privacy/readme files, and the adapter/native-helper sources required by
those components. It intentionally excludes tests, contributor docs, GitHub
workflows, the OpenMinis export, reverse-engineering screenshots, bytecode,
databases, journals, caches, backups, and pre-existing archives.

The packaged MCP always resolves its bundled adapter, EventKit bridge, and
doctor. Backend path overrides work only in an explicit source-test mode while
the source-only `tests/test_mcp_server.py` gate exists; release archives omit
that gate and ignore all three overrides.

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
- exact-ID EventKit reminder create/update/complete/reopen/move/delete operations;
- hybrid URL create/update that preserves EventKit metadata and verifies the matching user-visible native URL attachment before reporting full success;
- list creation by exact name with optional color and emblem, native ReminderKit
  section creation/membership with CloudKit version read-back, plus tag operations;
- first-class MCP tools for URL/image attachment, exact attachment replacement
  and deletion, and digest-bound preview/apply repair workflows where the
  current private-interface capability gate permits them;
- local diagnostics, backup, cache, and privacy-log maintenance.

Private existing-reminder mutations require a fresh `reminder_version` from
`list_reminder_attachments` as `if_version`. `delete_reminder` is public
EventKit instead: read the exact reminder first and pass its fresh
`last_modified` as `expected_last_modified`. The adapter's DB delete remains a
capability-gated diagnostic CLI path, not an MCP fallback.

Direct adapter `create_reminder`, `update_reminder`, `complete_reminder`,
`reopen_reminder`, and `delete_reminder` commands remain compatible but are
deprecated throughout 0.2.x. Normal callers must use the typed MCP/EventKit
tools. Removal is reserved for a separately reviewed 0.3.0 breaking release.

Unsupported, ambiguous, permission-blocked, schema-unknown, or
verification-pending requests must be reported as such. The plugin must not
invent support or silently drop fields.

## Local Files and Recovery

Depending on the commands used, the adapter can create data under:

- `~/Library/Application Support/apple-reminders-codex/`
- `~/Library/Caches/apple-reminders-codex/`

These locations can contain sensitive identifiers, cached titles/list names,
operation metadata, compiled helpers, capability records, and optional
recovery snapshots. Multi-label tag cleanup uses a single-database SQLite
online Snapshot. Cross-store attachment repair keeps the broader best-effort
container archive. Managed database Snapshots target five/100 MB and managed
container archives target two/300 MB by pruning older managed files. The newest
protected file is currently retained even when it alone exceeds that budget,
so these values are not hard storage caps. Explicit user-selected paths are not
auto-pruned. See [PRIVACY.md](PRIVACY.md) before enabling Maintenance writes or
sharing diagnostic output.

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

See [LICENSE](LICENSE), [PRIVACY.md](PRIVACY.md), [TERMS.md](TERMS.md),
[SECURITY.md](SECURITY.md), [SUPPORT.md](SUPPORT.md), and the upstream
[contribution guide](https://github.com/Oscar-V4/apple-reminders/blob/main/CONTRIBUTING.md).
Do not include real reminder data, screenshots, databases, archives, backups,
journals, or caches in issues, fixtures, commits, or release artifacts.
