# Apple Reminders for Codex and Claude

Manage your Apple Reminders by talking to Codex or Claude.
Turn notes into tasks, check what's due, and update or complete reminders.

**macOS 14+ · Apple silicon & Intel · [v0.8.0 beta](https://github.com/Oscar-V4/apple-reminders/releases/tag/v0.8.0)**

[한국어](https://github.com/Oscar-V4/apple-reminders/blob/main/docs/installation.ko.md)

## Install

Choose your app. Everything needed to run the plugin is bundled.

<details>
<summary><strong>Codex</strong></summary>

Ask Codex to run these commands, or run them in a terminal with `codex` available:

```bash
codex plugin marketplace add Oscar-V4/apple-reminders --ref v0.8.0
codex plugin add apple-reminders@oscar-v4-reminders
```

Start a **new Codex task** after installation.

</details>

<details>
<summary><strong>Claude Code</strong></summary>

Run these in your Claude Code conversation:

```text
/plugin marketplace add https://github.com/Oscar-V4/apple-reminders.git#v0.8.0
/plugin install apple-reminders@oscar-v4-reminders
```

Restart your Claude Code session after installation.

</details>

<details>
<summary><strong>Claude Desktop</strong></summary>

1. [Download the extension](https://github.com/Oscar-V4/apple-reminders/releases/download/v0.8.0/apple-reminders-0.8.0.mcpb).
2. Open **Settings → Extensions → Advanced settings → Install Extension…** and select the file.
3. Enable the extension and start a new conversation.

</details>

## Try it

- “Show my overdue reminders and everything due today.”
- “Add ‘Submit expense report’ to my Work list for Friday at 3 PM.”
- “Move it to Monday, keeping my notes and alert.”

Allow Reminders access when the macOS permission prompt appears.

[Help, updates & removal](https://github.com/Oscar-V4/apple-reminders/blob/main/docs/installation.md) · [Supported features](https://github.com/Oscar-V4/apple-reminders/blob/main/docs/workflow-capability-matrix.md)

---

Independent, open-source community plugin. Runs locally on your Mac;
selected tool results are sent to your assistant. [Privacy](PRIVACY.md) · [License](LICENSE)
