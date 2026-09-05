# Installation and advanced troubleshooting

For ordinary first use, follow the [README's three steps](../README.md#get-started-in-three-steps).
This guide describes v0.6.0. The matching
[release](https://github.com/Oscar-V4/apple-reminders/releases/tag/v0.6.0) contains
the versioned package and verification results.

## This version's contract

| Area | v0.6.0 contract |
|---|---|
| Tool discovery | 9 Core and diagnostic tools by default; 6 additional experimental tools require `--experimental` startup |
| Experimental dispatch | Disabled tools are rejected unless the runtime started with `--experimental`; existing operation gates still apply |
| Core `url` create/change | EventKit URL metadata only by default; native visible-card composition is an experimental operation |
| Python | Bundled signed Python runtime; no separate Python installation |
| EventKit helper | Bundled signed, notarized helper with its existing permission identity |

Older releases have different startup and URL behavior. Use the documentation
at the exact installed tag when diagnosing an older installation.

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

The following paths are outside ordinary EventKit Core. All six Native
Extension and Recovery tools require explicit experimental startup. Listing a
tool does not mean the current Mac supports the requested operation.

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

## Opting in to experimental tools

These commands show the two startup modes from a complete release checkout.
They are for an isolated MCP client; the packaged Codex configuration uses the
default mode:

```bash
# Default Core and diagnostic MCP server: 9 tools.
/bin/sh plugins/apple-reminders/scripts/launch_bundled_mcp.sh

# Opt-in MCP server: 15 tools, with existing private-operation gates.
/bin/sh plugins/apple-reminders/scripts/launch_bundled_mcp.sh --experimental
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
Privacy & Security → Reminders**. The current runtime does not use macOS Automation
or Apple Events, and it does not create metadata caches or backup archives.
