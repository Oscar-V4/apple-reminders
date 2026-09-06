# Installation and advanced troubleshooting

For ordinary first use, follow the [README's three steps](../README.md#get-started-in-three-steps).
This guide describes the **published public beta** for **v0.7.0**.
The immutable release and canonical verifier passed; see the
[publication evidence](release-evidence/public-beta-0.7.0.md).
Use the README's exact v0.7.0 installation commands. Public beta status is
separate from general availability or completed clean-user acceptance. The
historical [v0.6.1 release](https://github.com/Oscar-V4/apple-reminders/releases/tag/v0.6.1)
has different startup behavior documented at its own tag.

## Public beta contract

| Area | v0.7.0 public beta contract |
|---|---|
| Tool discovery | 15 tools by default: Core 8, diagnosis 1, Native/Recovery 6 |
| Core-only dispatch | `--core-only` exposes 9 Core and diagnostic tools; Native calls are rejected before dispatch |
| Legacy URL mode | `--experimental` is only a legacy hybrid URL opt-in, with the same 15-tool inventory |
| Core `url` create/change | EventKit URL metadata only by default; use an explicit attachment action for a native card |
| Python | Bundled signed Python runtime; no separate Python installation |
| EventKit helper | Bundled signed, notarized helper with its existing permission identity |
| Native helper | Prebuilt signed universal bundle verified; clean-user acceptance remains pending |

## Bundled runtime and Finder-launched Codex

This version includes Python 3.13.15 for Apple silicon and Intel Macs. You do
not need Python, Homebrew, Xcode, or Command Line Tools for ordinary Core work.
Codex launched from Finder uses the same packaged runtime as Codex launched
from a terminal; startup does not search `PATH`, activate a virtual environment,
or run Apple's developer-tool Python shim.

On first start, the plugin verifies its selected runtime capsule and prepares a
private copy under `~/Library/Caches/apple-reminders-codex/python-runtime/`.
Later starts use that verified copy. No interpreter is downloaded at runtime.
The cache contains runtime code, not reminder content.

If the runtime or its signature is missing or invalid, reinstall the same
reviewed release and start a new Codex task. Do not install a different Python
to repair the packaged runtime. Startup stops rather than choosing a different
interpreter, compiling a helper, or changing Gatekeeper settings.

If the error specifically identifies the **runtime cache**, reinstalling alone
does not replace that cached copy. Fully quit Codex. In Finder, choose
**Go → Go to Folder…** and open
`~/Library/Caches/apple-reminders-codex/python-runtime/`. Move only that
`python-runtime` folder to Trash, then reopen Codex and start a new task. The
plugin recreates it from the signed capsule. This removes executable cache
files only; it does not change reminders or operation records.

## Reminders permission

For first use, ask for a bounded read such as today's reminders. If access has
not been decided, Codex can call `request_reminders_access` once and retry the
original request after the macOS prompt. The tool reports authorization state;
it cannot claim to have observed the prompt itself.

If access was denied or revoked, use **System Settings → Privacy & Security →
Reminders** to re-enable the signed helper, then retry. Repeated access-tool
calls do not reopen the first-time prompt. Do not reset the macOS privacy
database as a troubleshooting step.

The signed helper introduced in v0.5.0 has a different identity from the older
locally built helper, so that upgrade may require a new permission decision.
First grant, denial, revocation, and permission continuity across update paths
still need acceptance testing on fresh nondeveloper Macs; an existing developer
profile is not sufficient evidence.

## Native availability

All six Native Extension and Recovery tools are discoverable in the public beta's
default session. Listing a tool does not mean the current Mac supports its
operation. The private implementation tier remains `experimental_internals`.

Image, section, and exact Recently Deleted operations use a verified prebuilt
signed universal Native bundle in the intended distribution. Ordinary users
need no Xcode or Command Line Tools. Tag assignment, URL-only attachment
changes, bounded metadata inspection, and deleted-item inventory remain guarded
private-store operations. A successful read does not establish write support.

Private operations require the exact reviewed macOS version/build, Reminders
version/build, and relevant schema evidence, plus final exact read-back. The
signed bundle does not override those checks. Sections and tags do not yet have
acceptance evidence; discovery alone must not be presented as functional support.
See [ADR 0023](decisions/0023-native-default-minimal-dependencies.md), the
[dependency/capability matrix](workflow-capability-matrix.md), and the
[signoff record](release-evidence/release-candidate-signoff.md).

Use targeted, content-free `diagnose_reminders` with
`execution_mode=metadata_only` before a requested private mutation or after a
relevant failure. Explain the returned availability and precise block reason.
Missing or unavailable Native support leaves healthy Core usable.
A missing or invalid bundle needs a reviewed package repair; an unadmitted build
needs new compatibility evidence. Neither is repaired by asking an ordinary
user to install a compiler, change flags, or weaken an allowlist.

For contributors only, explicit `APPLE_REMINDERS_NATIVE_ALLOW_SOURCE_BUILD=1`
permits the source-build fallback. Developer toolchain diagnosis uses
`execution_mode=experimental_toolchain`; ordinary metadata diagnosis does not
run `xcode-select` or `clang`. Source compilation cannot grant OS/app/schema
admission. This is a development facility, not an installation prerequisite.

The prior v0.6.1 SDK/compiler and attachment fingerprint defects are described
in the [preflight correction evidence](release-evidence/attachment-preflight-fix.md).
They are plugin defects, not a reason to reinstall Xcode.

## Startup choices for contributors

These commands run an MCP server from a complete checkout and expect protocol
messages on standard input. The packaged `.mcp.json` uses the default command.

```bash
# Default: 15 tools, exact private-operation admission remains required.
/bin/sh plugins/apple-reminders/scripts/launch_bundled_mcp.sh

# Explicit restricted runtime: 9 tools; Native dispatch is rejected.
/bin/sh plugins/apple-reminders/scripts/launch_bundled_mcp.sh --core-only

# Legacy hybrid URL opt-in only: 15 tools.
/bin/sh plugins/apple-reminders/scripts/launch_bundled_mcp.sh --experimental
```

Tool exposure is fixed for the process lifetime. Use an isolated test client;
installed cache files and global settings are not development deployment paths.

Default Core URL writes verify only the EventKit URL field. Use explicit
`change_reminder_attachment` actions for native cards. Only legacy
`--experimental` startup retains automatic hybrid composition on string URLs.
Clearing URL metadata preserves cards. Pending or partial outcomes need an
exact read before another write; neither startup mode proves iCloud convergence.

## Understanding an uncertain result

Receipts distinguish `unchanged`, `verified`,
`committed_verification_pending`, `partial_success`, `failed_no_mutation`, and
`failed_manual_repair_required`. Ask Codex to inspect the exact reminder after
a pending or partial result; a blind retry can repeat a change that was already
saved. A stale or consumed Reference needs a fresh `read_reminder` before a new
change. `verified` covers the named local read-back evidence, not every device.

If the bundled EventKit helper is missing or invalid, reinstall the same
reviewed release and use targeted diagnosis if needed. Core does not download
or compile a replacement automatically. Maintainers can inspect the
[release verification procedure](release-verification.md) and
[signed-helper design](decisions/0019-prebuilt-signed-eventkit-core-helper.md).

## Full removal

First follow [Uninstall](../README.md#uninstall). Plugin removal does not delete
reminders, undo iCloud changes, revoke macOS permission, or erase support data.

If you also want to remove local support data, stop the plugin, start a new
Codex task, and make sure no Reminders operation is running. In Finder, use
**Go → Go to Folder…** to inspect these exact locations:

- `~/Library/Application Support/apple-reminders-codex/`
- `~/Library/Caches/apple-reminders-codex/`

Move only the `apple-reminders-codex` folders to Trash, never their parent
directories. They may contain sensitive identifiers, operation records,
helpers, or legacy artifacts. Read [Privacy: user control](../PRIVACY.md#user-control)
before removing anything, and separately inspect any custom external legacy
backup directory that you explicitly configured.

You can also revoke the helper's Reminders access in **System Settings →
Privacy & Security → Reminders**. The current runtime does not use macOS Automation
or Apple Events, and it does not create metadata caches or backup archives.
