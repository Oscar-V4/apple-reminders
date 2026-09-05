# Apple Reminders for Codex

[![CI](https://github.com/Oscar-V4/apple-reminders/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Oscar-V4/apple-reminders/actions/workflows/ci.yml)

Ask Codex to show what is due, add reminders from your notes, and update or
complete them in Apple Reminders on your Mac.

This is an independent, open-source community plugin. It is not an Apple or
OpenAI product or endorsed integration. Reminder operations run locally;
selected tool results return to Codex. See [Privacy](PRIVACY.md).

## Which version am I installing?

**The latest published release is v0.5.2.** The three steps below install that
exact release. Changes on the development branch are not included in it.

| Behavior | Published v0.5.2 | Unreleased development branch |
|---|---|---|
| Tools available at startup | 15 tools, including experimental tools | 9 Core and diagnostic tools by default |
| Advanced native and recovery tools | Listed, but individual capabilities can be blocked | 6 additional tools require explicit `--experimental` startup |
| A reminder's URL field | Combines EventKit storage with native URL attachment work; it can partly succeed | Default stores EventKit URL metadata only; a visible URL card is not promised |

Experimental capabilities still require a supported macOS/Reminders build and
successful verification. Enabling them does not remove those requirements.
For the design and remaining work, see [Product direction](https://github.com/Oscar-V4/apple-reminders/blob/main/docs/product-direction.md).

## Get started in three steps

You need a Mac running **macOS 14 or newer**, Apple Reminders, and Codex.
Ordinary reminder work uses a bundled, signed and notarized helper. You do not
need Xcode, Command Line Tools, or an Apple Developer membership for that work.

### 1. Install Python once

The plugin still needs **Python 3.11 or newer**. Download a macOS installer from
[python.org](https://www.python.org/downloads/macos/), open it, and follow its
installation steps. Then fully quit and reopen Codex.

If you already have a supported Python installation, keep it. A self-contained
plugin that needs no separate Python installation is planned, not released.

### 2. Install the pinned plugin release

Ask Codex to run these commands, or run them in a terminal where the `codex`
command is available:

```bash
codex plugin marketplace add Oscar-V4/apple-reminders --ref v0.5.2
codex plugin add apple-reminders@oscar-v4-reminders
```

Already installed an older version? Follow [Upgrade](#upgrade) below.

### 3. Start a new Codex task and ask naturally

Start a **new task** so Codex loads the installed plugin, then try:

```text
Show my overdue reminders and everything due today.
```

When Codex requests Reminders access, allow it in the macOS permission prompt.
You can then ask:

```text
Add "Submit expense report" to my Work list for Friday at 3 PM.
Mark the expense report reminder as complete.
```

Codex will resolve the exact list and reminder before making changes. No
separate diagnostic command is needed for normal first use.

## Everyday use

- “Turn these meeting notes into reminders in my Project list.”
- “Show what is due this week.”
- “Move the reminders for this project into my Project Archive list.”

For **v0.5.2**, ask to put links in the reminder's **notes** when you want the
ordinary EventKit path. Its separate URL field also attempts experimental native
attachment work. Sections, native tags, image attachments, and Recently Deleted
recovery have additional limits; see [Advanced setup and troubleshooting](https://github.com/Oscar-V4/apple-reminders/blob/main/docs/installation.md).

After a change, Codex checks the saved result. If it reports partial success or
pending verification, ask it to check that reminder before retrying. A verified
local change does not prove it has appeared on every device yet.

## Permissions and troubleshooting

If access was denied or later revoked, open **System Settings → Privacy &
Security → Reminders**, re-enable access for the plugin's signed helper, and
retry your request. Repeated requests do not reopen the first permission prompt.
An upgrade from the old, locally built helper can require permission again.

| What you see | What to do |
|---|---|
| Python is missing or too old | Install Python 3.11+ from python.org, then fully reopen Codex. |
| A developer-tools installer appears | Cancel it for ordinary use; install Python as in step 1. The development branch fixes the launcher probe that can cause this. |
| The plugin is missing after installation or upgrade | Start a new Codex task. |
| An advanced feature is unsupported | Continue with ordinary reminders; see the advanced guide for that feature's limits. |
| A change is unconfirmed | Ask Codex to read the exact reminder before trying the change again. |

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
they do not delete Apple Reminders data. Maintainers and advanced users can use
the [release verifier](https://github.com/Oscar-V4/apple-reminders/blob/main/docs/release-verification.md) to inspect a published artifact's provenance.

## Uninstall

```bash
codex plugin remove apple-reminders@oscar-v4-reminders
codex plugin marketplace remove oscar-v4-reminders
```

Start a new Codex task. Removal leaves your reminders intact and does not erase
local support data or revoke macOS permissions. See [Privacy: user control](PRIVACY.md#user-control)
and the [full removal guide](https://github.com/Oscar-V4/apple-reminders/blob/main/docs/installation.md#full-removal) for those optional steps.

## More information

- [Installation and advanced troubleshooting](https://github.com/Oscar-V4/apple-reminders/blob/main/docs/installation.md)
- [Supported workflows](https://github.com/Oscar-V4/apple-reminders/blob/main/docs/workflow-capability-matrix.md) and [architecture](https://github.com/Oscar-V4/apple-reminders/blob/main/docs/architecture.md)
- [Product direction](https://github.com/Oscar-V4/apple-reminders/blob/main/docs/product-direction.md) and [contribution guide](https://github.com/Oscar-V4/apple-reminders/blob/main/CONTRIBUTING.md)
- [Privacy](PRIVACY.md), [Terms](TERMS.md), [Security](SECURITY.md), [Support](SUPPORT.md), and [License](LICENSE)
