# Apple Reminders for Codex

[![CI](https://github.com/Oscar-V4/apple-reminders/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Oscar-V4/apple-reminders/actions/workflows/ci.yml)

Give Codex the messy input—meeting notes, screenshots, or an everyday plan—and
get organized Apple Reminders back on your Mac.

With this plugin, Codex can extract action items and dates, create reminders in
the right lists, brief upcoming work, and safely organize or update reminders
with verified read-backs.

Try prompts like:

- `Turn these meeting notes into one reminder per action item, preserving owners and deadlines, then add them to my Project list.`
- `Create one reminder per screenshot in my Job Search list. Keep any dates and useful details you find.`
- `Show me everything overdue, due today, and coming up this week.`
- `Organize my Inbox reminders into sensible sections. Show me the plan before changing anything.`

The first three prompts use Core. Section organization is an optional Native
Extension feature and requires Xcode Command Line Tools.

This repository hosts an independent, open-source community plugin for Apple
Reminders. Its MCP server and macOS adapters run locally, while tool results
return to Codex as described in [PRIVACY.md](PRIVACY.md). This is not an Apple
or OpenAI product or endorsed integration.

See the [workflow capability matrix](https://github.com/Oscar-V4/apple-reminders/blob/main/docs/workflow-capability-matrix.md)
for supported journeys, explicit evidence boundaries, and deliberately
withheld operations.

## Requirements

- macOS 14 or newer with Apple Reminders available for the current user.
- Python 3.11 or newer.
- Reminders permission for Core operations.

Ordinary Core use does **not** require Xcode or Xcode Command Line Tools. The
plugin includes a universal EventKit helper that is Developer ID signed,
notarized by Apple, and stapled for offline Gatekeeper verification. Tag
assignments and native URL attachment operations also avoid runtime
compilation, but remain version-sensitive guarded private-store paths. Section
creation or moves, image-attachment changes, and exact Recently Deleted
inspection or recovery compile three private Objective-C helpers locally and
therefore require Xcode Command Line Tools.

End users do not need an Apple Developer Program membership. Maintainers use it
only to sign and notarize the bundled Core release helper.

For Python, Finder-launched Codex checks `PATH` plus standard Homebrew and
python.org locations. Availability is capability-specific, so Core remains
usable when an advanced Native or Recovery capability is unavailable.

## 60-second setup check

Before the first install, confirm Python:

```bash
python3 -c 'import sys; assert sys.version_info >= (3, 11), sys.version'
```

If you plan to create or move sections, change image attachments, or inspect or
recover one exact Recently Deleted item, also run `xcode-select -p`. If that
command fails, run `xcode-select --install`, finish installation, and restart
Codex. Core, tag assignments, native URL attachment operations, and read-only
section or attachment inspection do not invoke `clang`.

## Quick Start

Install the pinned 0.5.2 repo-marketplace release:

```bash
codex plugin marketplace add Oscar-V4/apple-reminders --ref v0.5.2
codex plugin add apple-reminders@oscar-v4-reminders
```

Start a new Codex task so that it loads the installed skills and tools, then try:

```text
Show me everything overdue, due today, and coming up this week.
Add "Submit expense report" to my Work list for Friday at 3 PM.
```

Starting with 0.5.0, Core uses the stable signed helper above instead of a
locally compiled ad-hoc helper. macOS may ask for Reminders access again after
this upgrade because the helper's code-signing identity changed.

## First permission

When requested, run `request_reminders_access` and answer the macOS prompt.
The tool can report its request but cannot observe the prompt itself; denial
does not trigger an automatic prompt loop. Normal first use needs no Doctor.

## Public Interface

The plugin exposes one static 15-tool Interface. It is intentionally
smaller than the internal development surface:

| Module | Public tools |
|---|---|
| Core | `request_reminders_access`, `list_reminder_lists`, `fetch_reminders`, `read_reminder`, `create_reminder`, `change_reminder`, `delete_reminder`, `ensure_reminder_list` |
| Native Extension | `inspect_reminder_native`, `create_reminder_section`, `organize_reminder`, `change_reminder_attachment` |
| Recovery | `inspect_recently_deleted`, `recover_deleted_reminder` |
| Diagnostics | `diagnose_reminders` |

Core reads and non-URL writes remain usable if a Native capability is missing.
URL writes are hybrid and may report partial success. See the
[capability matrix](https://github.com/Oscar-V4/apple-reminders/blob/main/docs/workflow-capability-matrix.md)
for exact evidence and intentionally withheld operations.

## References and mutation results

`read_reminder` returns one short-lived `rev1` Reference; a change consumes it,
and stale reuse requires a fresh exact read. Receipts distinguish `unchanged`,
`verified`, `committed_verification_pending`, `partial_success`,
`failed_no_mutation`, and `failed_manual_repair_required`. Pending, partial, or
manual-repair results must not be retried blindly. `verified` covers only the
named read-back evidence, not convergence to every device or shared-list member.

## Architecture and trust boundary

Skills grant no macOS permission. The local MCP validates inputs and Receipts;
Core launches only the reviewed helper bundled in the installed plugin and does
not download or automatically compile a fallback. A missing or invalid bundle
fails before mutation. Version-sensitive private paths remain behind exact
read-back gates. Only section writes, image-attachment changes, and exact
Recently Deleted inspection or recovery retain a separate local-build
dependency.

## Diagnosis and troubleshooting

Use `diagnose_reminders` only after a relevant failure. Its bounded default is
content-free and can target the affected capability.

Common cases:

- **Unsupported Python:** install Python 3.11+ via Homebrew or python.org, then
  restart Codex.
- **Permission required:** allow the explicit `request_reminders_access` step,
  review the macOS prompt, then retry once.
- **Reference stale or consumed:** call `read_reminder` again and retry with the
  new Reference. Do not replay the old token.
- **Verification pending:** read the exact reminder before deciding whether to
  retry. Blind retry can duplicate a change whose first result was unknown.
- **Bundled Core helper unavailable:** reinstall the same reviewed release tag,
  then run targeted diagnosis if the failure persists. Core does not silently
  compile or download a replacement.
- **Section/image/Recovery helper build failure:** run targeted diagnosis; for a
  missing compiler run `xcode-select --install`, finish installation, and
  restart Codex. Core remains independently usable.
- **Plugin changes are not visible:** start a new Codex task after install,
  upgrade, removal, or re-addition so tool discovery is refreshed.

## Upgrade

The Quick Start pins one release tag. Refreshing that marketplace keeps the
configured tag; it does not select a newer release. To move to another release,
replace `vX.Y.Z` below with the exact tag you reviewed:

```bash
codex plugin remove apple-reminders@oscar-v4-reminders
codex plugin marketplace remove oscar-v4-reminders
codex plugin marketplace add Oscar-V4/apple-reminders --ref vX.Y.Z
codex plugin add apple-reminders@oscar-v4-reminders
```

This changes only the Codex plugin installation; it does not delete Apple
Reminders data. Start a new Codex task after upgrading. Read
[CHANGELOG.md](CHANGELOG.md) before crossing a minor or major version.

## Uninstall

The CLI has no separate disable command. Removing the plugin never deletes
Reminders data; add it again later to re-enable it:

```bash
codex plugin remove apple-reminders@oscar-v4-reminders
# Later:
codex plugin add apple-reminders@oscar-v4-reminders
```

Start a new Codex task after either command. Removal does not delete reminders
or automatically erase plugin-created local support data.
For a full uninstall, also run
`codex plugin marketplace remove oscar-v4-reminders`. Review
[PRIVACY.md](PRIVACY.md) before separately deleting local support data.

## Internals and contribution

The closed contract is `plugins/apple-reminders/schemas/mcp-tools.json`.
See the [architecture guide](https://github.com/Oscar-V4/apple-reminders/blob/main/docs/architecture.md)
and [contribution checks](https://github.com/Oscar-V4/apple-reminders/blob/main/CONTRIBUTING.md).

## Local files and privacy

Internal or development operations can create support data under:

- `~/Library/Application Support/apple-reminders-codex/`
- `~/Library/Caches/apple-reminders-codex/`

These folders may contain sensitive identifiers, operation records, helpers,
or legacy cache/backup artifacts. The 0.5 runtime does not create metadata
caches or backup archives; Recently Deleted recovery is exact and user-directed.
See [PRIVACY.md](PRIVACY.md#user-control) before inspecting or removing them.

To remove local support data safely, first remove or stop the plugin, start a
new Codex task, and confirm that no Reminders operation is running. In Finder,
use **Go → Go to Folder…** for each exact path, then move only its
`apple-reminders-codex` folder to Trash—never a parent directory. This clears
local support data but does not undo Reminders or iCloud changes.
For a full uninstall, also revoke Reminders access in **System Settings →
Privacy & Security**, and separately inspect any custom external legacy backup
directory that you explicitly configured. The 0.5 runtime does not use macOS
Automation or Apple Events.

## License and contributions

See [LICENSE](LICENSE), [PRIVACY.md](PRIVACY.md), [TERMS.md](TERMS.md),
[SECURITY.md](SECURITY.md), [SUPPORT.md](SUPPORT.md), and the
[contribution guide](https://github.com/Oscar-V4/apple-reminders/blob/main/CONTRIBUTING.md).
Never include real reminder data,
screenshots, databases, archives, backups, journals, or caches in issues,
fixtures, commits, or release artifacts.
