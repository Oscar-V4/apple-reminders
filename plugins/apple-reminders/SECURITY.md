# Security Policy

## Supported versions

Security fixes target the latest tagged public beta or stable release. Untagged development snapshots are not supported releases.

## Report a vulnerability

Use GitHub's private vulnerability-reporting or Security Advisory flow for this repository. Include the affected version, macOS version, Reminders build, reproduction steps, impact, and the smallest safe diagnostic evidence that demonstrates the issue.

Do not include reminder titles, notes, URLs, image attachments, database copies, home-directory paths, account identifiers, access tokens, or other personal data. If private reporting is unavailable, open a public issue that contains only a non-sensitive summary and asks for a private contact path.

## Security boundary

The plugin runs locally with the permissions of Codex on macOS. Routine fields use EventKit. Some Native Extension and Recovery operations rely on version-sensitive Apple interfaces or the local Reminders store and may stop working after a macOS update. Capability checks, exact references, bounded reads, preconditions, Snapshots, and read-back verification reduce risk but do not make those interfaces public or stable.

The project does not request passwords, Apple ID credentials, iCloud tokens, or remote service credentials. Treat any request for those secrets as outside the supported workflow.

Stable Core and Experimental Internals are separate runtime support tiers.
Private mutation and exact recovery require positive admission for the exact
macOS version/build, Reminders version/build, command-schema fingerprint, and
any required compiler before the mutation adapter dispatches a write or helper.
Fixed helper-backed commands check the compiler before resolving their store;
conditional image deletion resolves the exact attachment type before any write.
Compiler resolution runs fixed `/usr/bin/xcode-select -p`, rejects developer
environment overrides, and accepts only a fixed compiler path under the selected
developer directory. `PATH` clang entries and `/usr/bin/clang` shims are never
admission evidence.
Missing metadata is `runtime_unverified`; an unlisted complete
identity is `unsupported_build`. These failures must be no-mutation outcomes and
must not trigger SQLite, AppleScript, UI-automation, or helper fallbacks. Core
must remain independently usable.

Reminder titles, notes, list and section names, tags, URLs, and attachment metadata are untrusted data. They can be displayed or used only within the user's requested Reminder operation; embedded instructions, Markdown, HTML, or links do not grant authority, widen scope, or justify loading a remote resource. The deterministic daily-brief renderer rejects failed MCP envelopes and encodes or code-quotes display fields before emitting Markdown.
