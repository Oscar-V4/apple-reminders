# Apple Reminders Codex Plugin

[![CI](https://github.com/Oscar-V4/apple-reminders/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Oscar-V4/apple-reminders/actions/workflows/ci.yml)

Plan, capture, organize, and safely update Apple Reminders from Codex on your
Mac. The plugin can brief due work, add reminders to an exact list, and make
guarded changes with explicit read-back results.

Try prompts such as:

- `오늘 마감이거나 기한이 지난 리마인더를 보여줘.`
- `쇼핑 목록에 우유 사기를 추가해줘.`
- `이 리마인더를 완료하고 다시 읽어서 확인해줘.`
- `최근 삭제된 '치과 예약'을 정확히 찾아 원래 계정의 쇼핑 목록으로 복구해줘.`
- `이 네 미리알림의 사진을 하나의 미리알림에 각각 복사해줘.`

This repository hosts the Apple Reminders community plugin. The MCP and adapters run locally,
but results are returned to Codex as described in [PRIVACY.md](PRIVACY.md).
This is an independent community project, not an Apple or OpenAI product or
endorsed integration.

See the [workflow capability matrix](https://github.com/Oscar-V4/apple-reminders/blob/main/docs/workflow-capability-matrix.md)
for supported journeys, explicit evidence boundaries, and deliberately
withheld operations.

## Requirements

- macOS 14 or newer with Apple Reminders available for the current user.
- Python 3.11 or newer.
- Xcode command-line tools while native helpers are distributed as source.
- Reminders permission for Core operations.

Finder-launched Codex checks `PATH` plus standard Homebrew and python.org
locations. Native availability is capability-specific; Core can remain usable
when one Native operation is unavailable.

## 60-second preflight

Before the first install, confirm the two source-runtime prerequisites:

```bash
python3 -c 'import sys; assert sys.version_info >= (3, 11), sys.version'
xcode-select -p
```

If the second command fails, run `xcode-select --install`, finish installation,
and restart Codex.

## Quick Start

Install the pinned 0.4.0 repo-marketplace release:

```bash
codex plugin marketplace add Oscar-V4/apple-reminders --ref v0.4.0
codex plugin add apple-reminders@oscar-v4-reminders
```

Start a new Codex task so that it loads the installed skills and tools, then try:

```text
오늘 할 일 보여줘.
쇼핑 목록에 우유 사기를 추가해줘.
```

Current-Mac Reminders and iCloud attachment checks pass. Fresh-profile
permission behavior remains follow-up validation; report unexpected prompts.

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
version-sensitive Native and Recovery paths remain behind exact read-back gates.

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
- **Native/build failure:** run targeted diagnosis; for a missing compiler run
  `xcode-select --install`, finish installation, and restart Codex. Core may
  remain usable.
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
or legacy cache/backup artifacts. The 0.4 runtime no longer creates metadata
caches or backup archives; Recently Deleted recovery is exact and user-directed.
See [PRIVACY.md](PRIVACY.md#user-control) before inspecting or removing them.

To remove local support data safely, first remove or stop the plugin, start a
new Codex task, and confirm that no Reminders operation is running. In Finder,
use **Go → Go to Folder…** for each exact path, then move only its
`apple-reminders-codex` folder to Trash—never a parent directory. This clears
local support data but does not undo Reminders or iCloud changes.
For a full uninstall, also revoke Reminders and Automation access in **System
Settings → Privacy & Security**, and separately inspect any custom external
legacy backup directory that you explicitly configured.

## License and contributions

See [LICENSE](LICENSE), [PRIVACY.md](PRIVACY.md), [TERMS.md](TERMS.md),
[SECURITY.md](SECURITY.md), [SUPPORT.md](SUPPORT.md), and the
[contribution guide](https://github.com/Oscar-V4/apple-reminders/blob/main/CONTRIBUTING.md).
Never include real reminder data,
screenshots, databases, archives, backups, journals, or caches in issues,
fixtures, commits, or release artifacts.
