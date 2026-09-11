# Apple Reminders for Codex and Claude

[![CI](https://github.com/Oscar-V4/apple-reminders/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Oscar-V4/apple-reminders/actions/workflows/ci.yml)

Ask Codex or Claude to show what is due, turn notes into reminders, and update
or complete them in Apple Reminders on your Mac.

**[한국어 설치 안내](https://github.com/Oscar-V4/apple-reminders/blob/main/docs/installation.ko.md)**

An independent, open-source community plugin. It is not an Apple, OpenAI, or
Anthropic product or endorsed integration. Operations run locally; selected
tool results return to the assistant you use. See [Privacy](PRIVACY.md).

## This version

This guide describes **v0.8.0**. Before installing, verify the
[v0.8.0 public beta release](https://github.com/Oscar-V4/apple-reminders/releases/tag/v0.8.0)
and its versioned verification results in the
[signoff](https://github.com/Oscar-V4/apple-reminders/blob/main/docs/release-evidence/claude-support-0.8.0.md).
Installation commands select that exact version.

This release adds Claude Code and Claude Desktop installation using the same
local MCP server and signed macOS components as Codex.

The plugin includes a signed Python runtime, so there is **no separate Python
installation**. Ordinary reminder work uses bundled components. You do not
need Xcode, Command Line Tools, Node.js, npm, or Homebrew.

## Get started in three steps

You need **macOS 14 or newer**, Apple Reminders, and one of the following
local clients. Apple silicon and Intel runtime packages are included.

### 1. Install for your assistant

**Codex app or CLI** — ask Codex to run these commands, or use a terminal with
`codex` available:

```bash
codex plugin marketplace add Oscar-V4/apple-reminders --ref v0.8.0
codex plugin add apple-reminders@oscar-v4-reminders
```

**Claude Code** — run these commands inside Claude Code:

```text
/plugin marketplace add https://github.com/Oscar-V4/apple-reminders.git#v0.8.0
/plugin install apple-reminders@oscar-v4-reminders
```

Prefer a terminal? Use `claude plugin marketplace add` and
`claude plugin install` with the same arguments.

**Claude Desktop** — download
[apple-reminders-0.8.0.mcpb](https://github.com/Oscar-V4/apple-reminders/releases/download/v0.8.0/apple-reminders-0.8.0.mcpb).
Open **Settings → Extensions → Advanced settings → Install Extension…**, select
the file, and follow the installation prompts. No JSON editing is needed.
This custom extension is distributed through this repository's releases.

| Client | What is installed |
|---|---|
| Codex | Local MCP tools and five workflow skills |
| Claude Code | The same local MCP tools and five workflow skills |
| Claude Desktop | Local MCP tools and server guidance; Code plugin skills are not loaded by a Desktop extension |

Already installed an older version? Follow [Upgrade](#upgrade).

### 2. Start a fresh conversation and allow Reminders access

Start a **new Codex task**, restart your Claude Code session, or start a new
Claude Desktop conversation after enabling the extension. Ask:

```text
Show my overdue reminders and everything due today.
```

Allow Reminders access in the macOS permission prompt for the plugin's signed helper.
Normal first use does not require a diagnostic command. If your organization
restricts local plugins or extensions, its installation policy still applies.

### 3. Ask naturally

```text
Add "Submit expense report" to my Work list for Friday at 3 PM.
Change its deadline to Monday, keeping my notes and alert.
Mark the expense report reminder as complete.
```

The assistant resolves the exact list and reminder before making changes,
then checks the saved result.

## Everyday use

- “Turn these meeting notes into reminders in my Project list.”
- “Show what is due this week.”
- “Remind me one calendar month before this annual renewal.”
- “Add this link to the reminder.”
- “Move these project reminders into my Project Archive list.”

The server exposes **15 tools** by default: eight Core tools, diagnosis, and
six Native/Recovery tools. An explicit `--core-only` startup exposes **9 Core
and diagnostic tools** and rejects Native dispatch. `--experimental` is only
a legacy hybrid URL opt-in. Default URL fields store EventKit URL metadata only;
an explicit attachment action adds or changes a native URL card.

Ordinary reminder fields use Apple's public EventKit API. Sections, native
tags, attachments, and recovery use version-sensitive private interfaces with
exact OS/app/schema admission. Their support tier remains
`experimental_internals`; tool discovery does not establish availability.
See [supported workflows](https://github.com/Oscar-V4/apple-reminders/blob/main/docs/workflow-capability-matrix.md).
This release also includes the app's calendar Early Reminder control, including
one calendar month before an annual renewal. It preserves ordinary alarms and
requires its recorded build/schema admission. See the
[Early Reminder design](https://github.com/Oscar-V4/apple-reminders/blob/main/docs/decisions/0024-calendar-early-reminder.md).
Client compatibility adds no further private capability admission.

A pending or partially verified change needs an exact read before retrying.
Verified local changes do not prove delivery or display on every device.

## Why no npm install?

npm is useful for distributing Node.js software, but this plugin already
contains its Python runtime and signed macOS helpers. Native plugin installation
also brings the workflow skills into Codex and Claude Code; a bare MCP command
would not do that. Desktop's `.mcpb` bundles the local server for file-based
installation. There is no npm package to install for this release.

## Permissions and troubleshooting

If access was denied or revoked, open **System Settings → Privacy & Security →
Reminders**, re-enable the plugin's signed helper, and retry. Repeated access
requests do not reopen the first permission prompt.

| What you see | What to do |
|---|---|
| Plugin missing after installation | Start a new Codex task or restart Claude Code; in Desktop, check that the extension is enabled. |
| Bundled runtime missing or invalid | Reinstall the same reviewed release. |
| Runtime cache error | Follow the [cache recovery steps](https://github.com/Oscar-V4/apple-reminders/blob/main/docs/installation.md#bundled-runtime-and-finder-launched-codex). |
| A developer-tools installer appears | Cancel it and check the installed version; ordinary startup uses the bundled runtime. |
| An advanced feature is unavailable | Use ordinary reminders and read the exact capability reason. |
| A change is unconfirmed | Ask the assistant to read that exact reminder before another change. |

Fresh-user permission flows and end-to-end execution on minimum macOS 14 still
need external acceptance testing. Build targets and maintainer-host tests do
not establish those results. See [Support](SUPPORT.md); omit real reminder
contents, screenshots, databases, and private logs from issue reports.

## Upgrade

Installations are pinned to a release. Read [CHANGELOG.md](CHANGELOG.md), then
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
cleanup, see [Privacy: user control](PRIVACY.md#user-control) and the
[full removal guide](https://github.com/Oscar-V4/apple-reminders/blob/main/docs/installation.md#full-removal).
Stop every client using the plugin before removing shared support data.

## More information

- [Installation and advanced troubleshooting](https://github.com/Oscar-V4/apple-reminders/blob/main/docs/installation.md)
- [Architecture](https://github.com/Oscar-V4/apple-reminders/blob/main/docs/architecture.md) and [contribution guide](https://github.com/Oscar-V4/apple-reminders/blob/main/CONTRIBUTING.md)
- [Release verification](https://github.com/Oscar-V4/apple-reminders/blob/main/docs/release-verification.md)
- [Privacy](PRIVACY.md), [Terms](TERMS.md), [Security](SECURITY.md), [Support](SUPPORT.md), and [License](LICENSE)
