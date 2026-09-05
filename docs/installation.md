# Installation and advanced troubleshooting

For ordinary first use, follow the [README's three steps](../README.md#get-started-in-three-steps).
This guide covers environment problems, experimental capabilities, development
launches, and removal. You do not need to complete every section before using
the plugin.

## Published release versus development

The pinned installation in the README installs **v0.5.2**, the latest published
release at the time of this change. It does not install the unreleased changes
described in [ADR 0021](decisions/0021-core-default-experience.md).

| Contract | Published v0.5.2 | Unreleased development branch |
|---|---|---|
| Tool discovery | Static 15-tool interface | Static 9-tool default; 15 with `--experimental` |
| Experimental dispatch | Tools are listed; each operation still needs its admission and verification requirements | Disabled tools are rejected unless the runtime started with `--experimental`; existing operation gates still apply |
| Core `url` create/change | EventKit plus native URL attachment composition | EventKit metadata by default; legacy hybrid composition with `--experimental` |
| Python | Requires Python 3.11+ | Still requires Python 3.11+; avoids probing Apple's developer-tool Python shim and removes unsupported-interpreter fallback |
| EventKit helper | Bundled signed, notarized helper | Same reviewed helper; no native rebuild is needed for these changes |

Use [the v0.5.2 documentation](https://github.com/Oscar-V4/apple-reminders/tree/v0.5.2/docs)
when inspecting the exact published contract. Documentation on a development
branch is not evidence that an installed release has changed.

## Python and Finder-launched Codex

Install Python 3.11 or newer using a macOS installer from
[python.org](https://www.python.org/downloads/macos/), then fully quit and reopen
Codex. The runtime has no bundled Python interpreter yet. Existing supported
Homebrew or python.org installations can be used; Homebrew is not required.

A GUI-launched app may see a different `PATH` from Terminal. The launcher checks
`PATH` and standard Homebrew and python.org locations without sourcing shell
startup files. A Python that works in an activated virtual environment in
Terminal may therefore be unavailable to Codex.

The published launcher can probe `/usr/bin/python3`, which belongs to Apple's
developer tools and may open their installer. Ordinary Core operations do not
need those tools. Cancel that installer and check the Python installation
above. The development launcher excludes the system shim, including resolved
aliases, before testing interpreter versions. **That fix is not in v0.5.2.**
If a published installation still prompts after installing Python and reopening
Codex, report the launcher issue through [Support](../SUPPORT.md); do not assume
that installing Xcode establishes a plugin requirement or compatibility.

If no supported interpreter can be found, the development launcher exits with
an actionable error. It does not install Python, compile a helper, or silently
try an unsupported interpreter. A self-contained signed runtime is a future
distribution priority, not a completed part of this change.

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

## Experimental capabilities

The following paths are outside ordinary EventKit Core. In the development
runtime, all six Native Extension and Recovery tools require explicit
experimental startup. In v0.5.2 those tools are already listed. In both cases,
listing a tool does not mean the current Mac supports the requested operation.

| Capability | Local compiler requirement |
|---|---|
| Section creation or moves | Required |
| Image attachment changes | Required |
| Exact Recently Deleted inspection or recovery | Required |
| Native tag assignment and URL-only attachment changes | No compiler; still guarded private-store operations |
| Bounded native metadata inspection | No compiler; a successful read does not establish write compatibility |
| Recently Deleted inventory | No compiler; requires the admitted recovery build/schema |

Private operations require the exact reviewed macOS version/build, Reminders
version/build, and relevant schema evidence. A successful compiler check,
matching selector, or experimental launch flag does not bypass admission. See
the [capability matrix](workflow-capability-matrix.md) and
[runtime-gate decision](decisions/0020-fail-closed-experimental-runtime-gate.md).

After a relevant failure, start with targeted, content-free
`diagnose_reminders` using `execution_mode=metadata_only`. It does not run
`xcode-select` or `clang`. Only an explicitly requested Experimental toolchain
diagnosis uses `execution_mode=experimental_toolchain`. If that reports
`compiler_required` and you choose to develop or test a compiler-backed feature,
install Apple's Command Line Tools separately. Core does not require this step.
An unsupported build remains unsupported after installing a compiler.

## Testing the development runtime

These commands are for a developer working from the repository root, not a
change to a pinned marketplace installation:

```bash
# Default Core and diagnostic MCP server: 9 tools.
/bin/sh plugins/apple-reminders/scripts/launch_mcp.sh

# Opt-in development MCP server: 15 tools, with existing private-operation gates.
/bin/sh plugins/apple-reminders/scripts/launch_mcp.sh --experimental
```

Each starts an MCP server that expects protocol messages on standard input;
it is not an interactive Reminders command line. Use the chosen launch command
in an isolated MCP test client. Tool exposure is fixed for that runtime's
lifetime; changing the selection requires a new process and fresh discovery.

The packaged `.mcp.json` uses the default launch command. There is no automatic
switch to experimental mode after a failure. Do not edit installed cache files
or global Codex settings to make a release appear to contain development code.
Changes should be tested from the checkout, reviewed, and distributed through a
new release when ready.

Default Core URL writes verify only the EventKit URL field. Experimental startup
retains native URL attachment composition; pending or partial outcomes need an
exact read before another write. An opt-in is not a promise of a visible card,
successful private mutation, or iCloud convergence.

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
Privacy & Security → Reminders**. The v0.5 runtime does not use macOS Automation
or Apple Events, and it does not create metadata caches or backup archives.
