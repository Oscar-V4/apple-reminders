# Apple Reminders Codex Plugin

Apple Reminders is a local macOS Codex plugin being prepared for its first 0.3
public beta. It bundles task-oriented skills and a local stdio MCP server for
reading and changing the current user's Apple Reminders data.

This is not an Apple-supported integration or an App Store component. Core
operations prefer EventKit. Sections, tags, and native image/URL attachments
use version-sensitive Apple interfaces behind narrower capability and
read-back gates.

## Public Interface

The public beta exposes one static 13-tool Interface. It is intentionally
smaller than the internal development surface:

| Module | Public tools |
|---|---|
| Core | `request_reminders_access`, `list_reminder_lists`, `fetch_reminders`, `read_reminder`, `create_reminder`, `change_reminder`, `delete_reminder`, `ensure_reminder_list` |
| Native Extension | `inspect_reminder_native`, `create_reminder_section`, `organize_reminder`, `change_reminder_attachment` |
| Diagnostics | `diagnose_reminders` |

Core reminder work does not depend on Native Extension availability. Unused-tag
cleanup, attachment repair, backup/Snapshot operations, privacy-log purge, flag
mutation, and native `show_reminder` handoff are withheld from the 0.3 public
Interface. Their lower-level implementations may remain for development and
compatibility, but skills and public MCP callers must not route to them.

The repository also contains a reduced OpenMinis export under
`minis/apple-reminders/`. It is not part of the installable macOS plugin and
does not contain the private adapters.

## Requirements

- macOS 14 or newer with Apple Reminders available for the current user.
- Python 3.11 or newer.
- Xcode command-line tools while native helpers are distributed as source.
- Reminders permission for Core operations.

Native Extension support cannot be inferred from the macOS version alone. A
specific Native operation can be unavailable while Core operations continue to
work.

## Quick Start

The commands below target the future tagged 0.3.0 repo-marketplace release.
Until that tag exists, treat this repository as development source rather than
a finished installer.

```bash
codex plugin marketplace add Oscar-V4/apple-reminders --ref v0.3.0
codex plugin add apple-reminders@oscar-v4-reminders
```

Start a new Codex task so that it loads the installed skills and tools, then try:

```text
오늘 할 일 보여줘.
쇼핑 목록에 우유 사기를 추가해줘.
```

## Release Status

When an operation needs Reminders permission, the plugin reports that need and
offers the explicit `request_reminders_access` step. macOS may then display a
permission prompt for the app running Codex. Normal first use does not require
Doctor or private-interface setup.

## What is preserved from real use

The smaller Interface keeps the behaviors that were added after failures were
found in actual use:

- bounded EventKit reads and exact list/reminder identity, including duplicate
  list names across accounts;
- create idempotency and guarded update, complete, reopen, move, and delete;
- URL create/change as one hybrid operation: EventKit metadata, the visible
  native URL attachment, and a final exact EventKit read;
- native ReminderKit image attachment and section saves with CloudKit evidence;
- exact-list section scope and fresh-version tag changes;
- stale-write rejection and explicit unknown-outcome handling under
  concurrency;
- normalized Receipts that distinguish verified, pending, partial, unchanged,
  and failed outcomes.

This simplification changes the public shape, not those behavioral guarantees.

## References and mutation results

`read_reminder` returns one opaque, short-lived `rev1` Reference. Existing-item
changes consume that Reference. Callers do not choose a backend or assemble
EventKit `last_modified`, private `reminder_version`, store identity, and expiry
fields themselves. A stale or already-consumed Reference returns a no-write
concurrency result and requires a new exact read.

Mutation Receipts use states such as:

- `unchanged`: no mutation was necessary;
- `verified`: the final operation passed its stated exact read-back;
- `committed_verification_pending`: a write may have committed but final
  verification is not yet available;
- `partial_success`: only part of a composed operation was verified;
- `failed_no_mutation`: the operation failed before a known mutation;
- `failed_manual_repair_required`: a partial mutation could not be compensated
  automatically.

`verified` is limited to the evidence named in the Receipt. It does not mean
that an item was visually observed on an iPhone, converged to every device, or
reached every participant in a shared list.

## Architecture and trust boundary

| Layer | Role | Boundary |
|---|---|---|
| Skills | Bounded reads, ambiguity handling, and reporting policy | Instructions only; they do not grant macOS permission |
| Local MCP | Closed input schemas, routing, concise text, and validated structured results | Runs with the Codex host user's local process permissions |
| Core Module | EventKit-backed list/reminder operations and opaque References | Public API behavior is still account-, permission-, and sync-dependent |
| Native Extension Module | Sections, tags, and native image/URL attachments | Uses version-sensitive private interfaces and exact read-back gates |
| Diagnostics Module | Content-free targeted diagnosis | Does not prove write semantics, iCloud convergence, or device visibility |

The MCP can make changes that sync through the user's Apple account and affect
shared lists. A process exit code alone is never proof that a mutation reached
Reminders or another device.

## Diagnosis and troubleshooting

Run normal bounded work first. Use `diagnose_reminders` only after a relevant
permission, environment, build, schema, or native-capability failure. It
defaults to a small content-free Core summary and can target the affected area.
It does not read reminder titles, notes, list/section/tag names, attachment
contents, journals, caches, or backup contents.

Common cases:

- **Unsupported Python runtime:** run `python3 --version` in the environment
  that launches Codex. Ensure it resolves to Python 3.11 or newer, then restart
  Codex before retrying; the plugin rejects the call before any Reminder write.
- **Permission required:** allow the explicit `request_reminders_access` step,
  review the macOS prompt, then retry once.
- **Reference stale or consumed:** call `read_reminder` again and retry with the
  new Reference. Do not replay the old token.
- **Verification pending:** read the exact reminder before deciding whether to
  retry. Blind retry can duplicate a change whose first result was unknown.
- **Native Extension unavailable:** run targeted diagnosis for the failed
  capability. Continue using Core operations; do not infer that all Reminders
  access is blocked.
- **Missing private-framework path:** this is inconclusive on systems where dyld
  can load a framework from the shared cache. A runtime probe and the operation's
  read-back decide availability.
- **Plugin changes are not visible:** start a new Codex task after install,
  upgrade, removal, or re-addition so tool discovery is refreshed.

Repository developers can run the underlying compact Doctor directly for
source troubleshooting:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 plugins/apple-reminders/scripts/reminders_doctor.py --compact
```

Add `--detail-level full` only for a specific warning or blocked capability.

## Upgrade

```bash
codex plugin marketplace upgrade oscar-v4-reminders
codex plugin add apple-reminders@oscar-v4-reminders
```

Start a new Codex task after upgrading. Read [CHANGELOG.md](CHANGELOG.md) before
crossing a minor or major version.

## Temporarily disable

The current Codex plugin CLI has no separate disable command. To stop loading
the plugin without changing Apple Reminders data, remove it and add it again
when needed:

```bash
codex plugin remove apple-reminders@oscar-v4-reminders
# Later:
codex plugin add apple-reminders@oscar-v4-reminders
```

Start a new Codex task after either command. Removal does not delete reminders
or automatically erase plugin-created local support data.

## Uninstall

```bash
codex plugin remove apple-reminders@oscar-v4-reminders
codex plugin marketplace remove oscar-v4-reminders
```

Review [PRIVACY.md](PRIVACY.md) before separately deleting local support data.
Removing the plugin itself does not modify Apple Reminders.

## Public and internal surfaces

The public contract is `plugins/apple-reminders/schemas/mcp-tools.json`. Every public input is closed
and bounded. Result shapes and Receipts are enforced centrally even though MCP
`outputSchema` is intentionally not duplicated into `tools/list`; compact tool
discovery remains below the release budget.

`plugins/apple-reminders/scripts/reminders_adapter.py` is a lower-level implementation seam, not a
second public API. Its old direct write, Maintenance, backup, repair, log-purge,
flag, and UI-handoff commands are deprecated or internal. They may support
tests, compatibility analysis, and recovery research, but public skills must
not fall back to them.

## Local validation

These checks do not launch Reminders or read live reminder rows:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 scripts/validate_plugin.py plugins/apple-reminders
PYTHONDONTWRITEBYTECODE=1 python3 scripts/validate_minis_export.py
PYTHONDONTWRITEBYTECODE=1 python3 scripts/audit_source_package.py plugins/apple-reminders \
  --strict-worktree --verify-root-document-mirrors
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_*.py'
```

Native source checks on macOS:

```bash
clang -x objective-c -fobjc-arc -framework Foundation -framework AppKit \
  -fsyntax-only plugins/apple-reminders/scripts/remkit_attach_image.m
clang -x objective-c -fobjc-arc -framework Foundation -framework EventKit \
  -fsyntax-only plugins/apple-reminders/scripts/reminders_eventkit.m
plutil -lint plugins/apple-reminders/scripts/eventkit_bridge_info.plist
```

The data-free performance benchmark measures MCP initialization/tool discovery,
targeted diagnosis, EventKit request validation/helper build, package audit, and
deterministic build time:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 scripts/benchmark_plugin.py \
  --label local-baseline --samples 15 --warmups 3 \
  --build-samples 5 --build-warmups 1 --output benchmark.json
```

Timing is end-to-end subprocess wall time and is not a cross-machine score.

### Opt-in live validation

Maintainers can run one destructive-but-disposable end-to-end smoke test after
the data-free suite passes. From a repository checkout, first use
`list_reminder_lists` to choose the exact `source.id` of a writable account. The
maintainer harness is not included in the runtime ZIP. It creates one uniquely
named synthetic list, exercises Core and Native Extension flows through the
installable runtime's stdio MCP server, verifies list/create idempotent replay,
a five-item bounded fetch, actual stale-revision rejection from parallel exact
reads, URL and image visibility plus sync evidence, section placement,
completion/reopen, and exact deletion. It finally deletes that exact list by
matching both its name and AppleScript identity.

```bash
PYTHONDONTWRITEBYTECODE=1 python3 scripts/live_smoke.py \
  --confirm-live-reminders \
  --source-id '<exact-source-id>'
```

This command changes live Reminders data and must never run in CI. Its output is
redacted to step, status, and latency. If exact cleanup cannot be proven, it
prints only the reserved synthetic list name that must be inspected manually;
do not retry blindly. Reminders may retain the deleted synthetic content in
**Recently Deleted**; the harness deliberately does not empty that recoverable
system area.

## Deterministic source package

The allowlisted release ZIP includes runtime files only. It excludes tests,
contributor docs, workflows, OpenMinis files, screenshots, bytecode, databases,
journals, caches, backups, and pre-existing archives.

Production resolves only bundled backend paths. Tests inject an explicit
`BackendPaths` value into `mcp.server.main(...)` through the source-only harness;
environment variables do not override production backend paths.

```bash
PYTHONDONTWRITEBYTECODE=1 python3 scripts/build_source_package.py \
  plugins/apple-reminders \
  --output-directory dist
```

The builder normalizes ZIP metadata, audits members, checks version/filename
agreement, and prints the archive path and SHA-256.

## Local files and privacy

Internal or development operations can create support data under:

- `~/Library/Application Support/apple-reminders-codex/`
- `~/Library/Caches/apple-reminders-codex/`

These locations may contain sensitive identifiers, cached metadata, operation
records, compiled helpers, capability records, or recovery snapshots. Backup,
restore, repair, and log-purge operations are not public 0.3 tools; no automatic
restore is promised. See [PRIVACY.md](PRIVACY.md#user-control) before inspecting,
sharing, or removing support data.

To remove local support data safely, first remove or stop the plugin, start a
new Codex task, and confirm that no Reminders operation is running. In Finder,
use **Go → Go to Folder…** to inspect each exact path above, then move only the
`apple-reminders-codex` folder at those two locations to Trash. Do not remove a
parent `Application Support` or `Caches` directory. This clears local caches,
compiled helpers, journals, idempotency metadata, and any plugin-managed backup
files that still exist; it does not undo Reminders or iCloud changes. Empty
Trash only after deciding that any old recovery artifacts are no longer needed.
For a full uninstall, also revoke Reminders and Automation access in **System
Settings → Privacy & Security**, and separately inspect any custom external
backup directory that you explicitly configured.

## License and contributions

See [LICENSE](LICENSE), [PRIVACY.md](PRIVACY.md), [TERMS.md](TERMS.md),
[SECURITY.md](SECURITY.md), [SUPPORT.md](SUPPORT.md), and the
[contribution guide](https://github.com/Oscar-V4/apple-reminders/blob/main/CONTRIBUTING.md).
Never include real reminder data,
screenshots, databases, archives, backups, journals, or caches in issues,
fixtures, commits, or release artifacts.
