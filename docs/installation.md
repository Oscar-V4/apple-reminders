# Installation and advanced troubleshooting

For ordinary first use, follow the [quick setup](../README.md#install).
This guide describes **v0.8.0**. Before installing, verify the
[v0.8.0 public beta release](https://github.com/Oscar-V4/apple-reminders/releases/tag/v0.8.0)
and its versioned verification results, then use the README's exact commands.
Publication status and release checks belong in the
[signoff](release-evidence/claude-support-0.8.0.md).
The [historical v0.7.0 evidence](release-evidence/public-beta-0.7.0.md) applies
only to that earlier version.

## Choose a client

Use the [client-specific installation steps](../README.md#install).
Codex and Claude Code install the same five skills alongside MCP tools.
Claude Desktop installs the local tools through an MCPB extension and receives
MCP server guidance; it does not load the Code plugin's skill files.
This is local Mac integration. Browser-only Claude, remote Linux sessions,
and Codex cloud cannot access a Mac's Reminders through this stdio server.

### Claude Code skill discovery

Run `/apple-reminders:apple-reminders` to load the primary workflow explicitly,
or ask naturally. The other skills cover briefs, quick capture, organization,
and attachments. If the plugin is missing, check `/plugin` and restart the
session. In `/mcp`, the local server is namespaced under the plugin.
Install through the plugin marketplace so both tools and skills are included.

### Claude Desktop extension

The `.mcpb` contains both CPU runtimes and the signed helpers. Use
**Settings → Extensions → Advanced settings → Install Extension…** and select
the exact versioned file from GitHub Releases. Ensure it is enabled, then
start a fresh conversation. The extension uses the bundled runtime even when
Desktop is launched from Finder; no `npx`, external Python, or configuration
file edits are required. A custom extension may be restricted by workspace
policy. Tool approval in Claude and macOS Reminders permission are separate.

### Other local MCP clients

A client that supports local stdio MCP can run an extracted, reviewed package
using `/bin/sh` as the command and an absolute path to
`scripts/launch_bundled_mcp.sh` as its first argument. Keep the whole plugin
directory intact. No arguments are needed for the default profile. This route
provides tools, not automatic skill installation, and has not been qualified
for every MCP client. Put diagnostics on stderr and leave stdout for MCP.

### Installation format references

Client packaging follows the [OpenAI plugin documentation](https://developers.openai.com/plugins/build/plugins),
[Claude Code plugin reference](https://code.claude.com/docs/en/plugins-reference),
[Claude marketplace guide](https://code.claude.com/docs/en/plugin-marketplaces),
and [Desktop extension installation guide](https://support.claude.com/en/articles/10949351-getting-started-with-local-mcp-servers-on-claude-desktop).
The bundle manifest follows [MCPB 0.3](https://github.com/modelcontextprotocol/mcpb/blob/main/MANIFEST.md).

## Version contract

| Area | v0.8.0 contract |
|---|---|
| Tool discovery | 15 tools by default: Core 8, diagnosis 1, Native/Recovery 6 |
| Core-only dispatch | `--core-only` exposes 9 Core and diagnostic tools; Native calls are rejected before dispatch |
| Legacy URL mode | `--experimental` is only a legacy hybrid URL opt-in, with the same 15-tool inventory |
| Core `url` create/change | EventKit URL metadata only by default; use an explicit attachment action for a native card |
| Python | Bundled signed Python runtime; no separate Python installation |
| EventKit helper | Verified v0.8.0 signed, notarized, and stapled bundle |
| Early Reminder | Calendar unit/count through Native inspection and set/clear; exact build/schema admission |
| Native helper | Signed universal bundle; maintainer-host image checks are bounded evidence; clean-user acceptance is not established by these checks |

## Bundled runtime and Finder-launched Codex

This version includes Python 3.13.15 for Apple silicon and Intel Macs. You do
not need Python, Homebrew, Xcode, or Command Line Tools for ordinary Core work.
Codex and Claude Desktop launched from Finder use the same packaged runtime
as the terminal clients; startup does not search `PATH`, activate a virtual environment,
or run Apple's developer-tool Python shim.

On first start, the plugin verifies its selected runtime capsule and prepares a
private copy under `~/Library/Caches/apple-reminders-codex/python-runtime/`.
Later starts use that verified copy. No interpreter is downloaded at runtime.
The cache contains runtime code, not reminder content.

If the runtime or its signature is missing or invalid, reinstall the same
reviewed release and start a fresh conversation in your assistant. Do not install a different Python
to repair the packaged runtime. Startup stops rather than choosing a different
interpreter, compiling a helper, or changing Gatekeeper settings.

If the error specifically identifies the **runtime cache**, reinstalling alone
does not replace that cached copy. Fully quit every Codex and Claude client using the plugin. In Finder, choose
**Go → Go to Folder…** and open
`~/Library/Caches/apple-reminders-codex/python-runtime/`. Move only that
`python-runtime` folder to Trash, then reopen your assistant and start a fresh conversation. The
plugin recreates it from the signed capsule. This removes executable cache
files only; it does not change reminders or operation records.

## Reminders permission

For first use, ask for a bounded read such as today's reminders. If access has
not been decided, the assistant can call `request_reminders_access` once and retry the
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

All six Native Extension and Recovery tools are discoverable in this version's
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
messages on standard input. All three client configurations use the default command.
Codex reads `.mcp.json`; Claude Code uses the inline `mcpServers` entry in
`.claude-plugin/plugin.json` with `${CLAUDE_PLUGIN_ROOT}`, and Desktop reads `manifest.json` with `${__dirname}`.
The launcher resolves its own directory and does not depend on Claude's working directory.

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
`failed_manual_repair_required`. Ask the assistant to inspect the exact reminder after
a pending or partial result; a blind retry can repeat a change that was already
saved. A stale or consumed Reference needs a fresh `read_reminder` before a new
change. `verified` covers the named local read-back evidence, not every device.

If the bundled EventKit helper is missing or invalid, reinstall the same
reviewed release and use targeted diagnosis if needed. Core does not download
or compile a replacement automatically. Maintainers can inspect the
[release verification procedure](release-verification.md) and
[signed-helper design](decisions/0019-prebuilt-signed-eventkit-core-helper.md).

## Upgrade

Installations are pinned to a release. Read [CHANGELOG.md](../CHANGELOG.md), then
replace `vX.Y.Z` below with the exact published release you want.

**Codex:**

```bash
codex plugin remove apple-reminders@oscar-v4-reminders
codex plugin marketplace remove oscar-v4-reminders
codex plugin marketplace add Oscar-V4/apple-reminders --ref vX.Y.Z
codex plugin add apple-reminders@oscar-v4-reminders
```

**Claude Code:**

```text
/plugin uninstall apple-reminders@oscar-v4-reminders
/plugin marketplace remove oscar-v4-reminders
/plugin marketplace add https://github.com/Oscar-V4/apple-reminders.git#vX.Y.Z
/plugin install apple-reminders@oscar-v4-reminders
```

**Claude Desktop:** download the new version's `.mcpb` from its release and
install it through the same Extensions screen. Custom release files need a
manual upgrade; they are not an automatically updated directory listing.

Start a fresh conversation/session afterward. These operations change the
plugin installation and leave Apple Reminders data intact. Using Codex and
Claude on the same Mac shares the plugin's existing local runtime cache and
operation records, including the legacy `apple-reminders-codex` folder names.

## Uninstall

**Codex:**

```bash
codex plugin remove apple-reminders@oscar-v4-reminders
codex plugin marketplace remove oscar-v4-reminders
```

**Claude Code:**

```text
/plugin uninstall apple-reminders@oscar-v4-reminders
/plugin marketplace remove oscar-v4-reminders
```

**Claude Desktop:** remove the extension in **Settings → Extensions**.

Start a fresh conversation/session. Removal leaves reminders intact and does
not erase shared local support data or revoke macOS permissions. For optional
cleanup, see [Privacy: user control](../PRIVACY.md#user-control) and the
[full removal guide](#full-removal).
Stop every client using the plugin before removing shared support data.

## Full removal

First follow [Uninstall](#uninstall). Plugin removal does not delete
reminders, undo iCloud changes, revoke macOS permission, or erase support data.

If you also want to remove local support data, stop the plugin in every
Codex and Claude client and make sure no Reminders operation is running.
These folders are shared across clients; removing them affects every installation. In Finder, use
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
