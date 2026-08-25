# Security Policy

## Supported versions

Security fixes target the latest tagged public beta or stable release. Untagged development snapshots are not supported releases.

## Report a vulnerability

Use GitHub's private vulnerability-reporting or Security Advisory flow for this repository. Include the affected version, macOS version, Reminders build, reproduction steps, impact, and the smallest safe diagnostic evidence that demonstrates the issue.

Do not include reminder titles, notes, URLs, image attachments, database copies, home-directory paths, account identifiers, access tokens, or other personal data. If private reporting is unavailable, open a public issue that contains only a non-sensitive summary and asks for a private contact path.

## Security boundary

The plugin runs locally with the permissions of Codex on macOS. Routine fields use EventKit. Some Native Extension and Maintenance operations rely on version-sensitive Apple interfaces or the local Reminders store and may stop working after a macOS update. Capability checks, exact references, bounded reads, preconditions, Snapshots, and read-back verification reduce risk but do not make those interfaces public or stable.

The project does not request passwords, Apple ID credentials, iCloud tokens, or remote service credentials. Treat any request for those secrets as outside the supported workflow.
