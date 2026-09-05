# Apple Reminders for Codex

[![CI](https://github.com/Oscar-V4/apple-reminders/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Oscar-V4/apple-reminders/actions/workflows/ci.yml)

Ask Codex to show what is due, turn notes into reminders, and update or complete
them in Apple Reminders on your Mac.

This is an independent, open-source community plugin. It is not an Apple or
OpenAI product or endorsed integration. Reminder operations run locally;
selected tool results return to Codex. See [Privacy](PRIVACY.md).

## This version

This guide describes **v0.6.0**. Use the matching
[release](https://github.com/Oscar-V4/apple-reminders/releases/tag/v0.6.0) for the
versioned package and verification results.

The plugin includes a signed Python runtime, so there is **no separate Python
installation**. Ordinary startup offers **9 Core and diagnostic tools**.
The **6 additional experimental tools** require explicit `--experimental`
startup and their existing compatibility checks. Default URL writes store
**EventKit URL metadata only**; they do not promise a visible URL card.

## Get started in three steps

You need a Mac running **macOS 14 or newer**, Apple Reminders, and Codex.
Ordinary reminder work uses bundled, signed and notarized components. You do not
need Xcode, Command Line Tools, Homebrew, or an Apple Developer membership.

### 1. Install the pinned plugin release

Ask Codex to run these commands, or run them in a terminal where the `codex`
command is available:

```bash
codex plugin marketplace add Oscar-V4/apple-reminders --ref v0.6.0
codex plugin add apple-reminders@oscar-v4-reminders
```

Already installed an older version? Follow [Upgrade](#upgrade) below.

### 2. Start a new Codex task and allow Reminders access

Start a **new Codex task** so Codex loads the installed plugin, then ask:

```text
Show my overdue reminders and everything due today.
```

When Codex requests Reminders access, allow it in the macOS permission prompt.
No separate diagnostic command is needed for normal first use.

### 3. Ask naturally

```text
Add "Submit expense report" to my Work list for Friday at 3 PM.
Mark the expense report reminder as complete.
```

Codex resolves the exact list and reminder before making changes, then checks
the saved result.

## Everyday use

- “Turn these meeting notes into reminders in my Project list.”
- “Show what is due this week.”
- “Add this link to the reminder.”
- “Move the reminders for this project into my Project Archive list.”

Sections, native tags, image attachments, and Recently Deleted recovery are
experimental and off by default. See [Advanced setup and troubleshooting](https://github.com/Oscar-V4/apple-reminders/blob/main/docs/installation.md)
for opting in and understanding their limits.

If Codex reports partial success or pending verification, ask it to check the
exact reminder before retrying. A verified local change does not prove it has
appeared on every device yet.

## Permissions and troubleshooting

If access was denied or later revoked, open **System Settings → Privacy &
Security → Reminders**, re-enable access for the plugin's signed helper, and
retry. Repeated requests do not reopen the first permission prompt.

| What you see | What to do |
|---|---|
| A bundled runtime file is missing or invalid | Reinstall the same reviewed release, then start a new Codex task. |
| A developer-tools installer appears during ordinary use | Cancel it and check the installed plugin version; this version's ordinary startup uses its bundled runtime. |
| The plugin is missing after installation or upgrade | Start a new Codex task. |
| An advanced feature is unsupported | Continue with ordinary reminders and check that feature's limits in the advanced guide. |
| A change is unconfirmed | Ask Codex to read the exact reminder before trying the change again. |

Fresh-user Reminders permission flows and end-to-end execution on the minimum
macOS 14 version still need external acceptance testing. The build target and
automated tests do not establish those results.

For other failures, ask Codex to diagnose the specific problem. See
[Support](SUPPORT.md) before opening an issue; omit real reminder contents,
screenshots, databases, and private logs.

## Upgrade

Installation is pinned to a release. Refreshing the marketplace keeps that tag.
Read [CHANGELOG.md](CHANGELOG.md), then replace `vX.Y.Z` with the exact published
release you want:

```bash
codex plugin remove apple-reminders@oscar-v4-reminders
codex plugin marketplace remove oscar-v4-reminders
codex plugin marketplace add Oscar-V4/apple-reminders --ref vX.Y.Z
codex plugin add apple-reminders@oscar-v4-reminders
```

Start a new Codex task afterward. These commands change the plugin installation;
they do not delete Apple Reminders data. This version uses its bundled Python
even if an older installation used a separate interpreter.

Maintainers and advanced users can use the [release verifier](https://github.com/Oscar-V4/apple-reminders/blob/main/docs/release-verification.md)
to inspect a published artifact's provenance.

## Uninstall

```bash
codex plugin remove apple-reminders@oscar-v4-reminders
codex plugin marketplace remove oscar-v4-reminders
```

Start a new Codex task. Removal leaves your reminders intact and does not erase
local support data or revoke macOS permissions. See [Privacy: user control](PRIVACY.md#user-control)
and the [full removal guide](https://github.com/Oscar-V4/apple-reminders/blob/main/docs/installation.md#full-removal)
for those optional steps.

## More information

- [Installation and advanced troubleshooting](https://github.com/Oscar-V4/apple-reminders/blob/main/docs/installation.md)
- [Supported workflows](https://github.com/Oscar-V4/apple-reminders/blob/main/docs/workflow-capability-matrix.md) and [architecture](https://github.com/Oscar-V4/apple-reminders/blob/main/docs/architecture.md)
- [Product direction](https://github.com/Oscar-V4/apple-reminders/blob/main/docs/product-direction.md) and [contribution guide](https://github.com/Oscar-V4/apple-reminders/blob/main/CONTRIBUTING.md)
- [Privacy](PRIVACY.md), [Terms](TERMS.md), [Security](SECURITY.md), [Support](SUPPORT.md), and [License](LICENSE)
