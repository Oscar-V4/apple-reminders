# Support

## Before filing an issue

Confirm that you are using the latest tagged release on macOS with Apple Reminders available. Read the installation and troubleshooting sections in the README, then retry one bounded read. Run metadata-only summary diagnosis only when the operation returns an environment, Native Extension, or Recovery failure. Opt in to Experimental toolchain diagnosis only for a CLT-required capability failure.

## What to include

- plugin version and installation source
- macOS version and Reminders build
- Codex desktop or CLI version
- the exact user goal and tool error code
- whether Core work still succeeds
- a minimal, redacted reproduction

Never attach a Reminders database, container archive, diagnostic bundle, reminder content, image attachment, account identifier, token, or absolute home-directory path. Replace exact IDs with stable placeholders while preserving whether two IDs were equal or different.

## Supported scope

The project supports local Codex use on macOS. It does not promise ChatGPT web, Codex cloud, Windows, Linux, automatic restore, direct iPhone inspection, or compatibility with every future private Reminders schema.
Exact user-directed recovery from Recently Deleted is available only within its
documented 30-day, same-account, compatible-local-build boundary; it is not an
automatic or broad backup restoration service.

Feature requests should describe the recognizable user goal and expected result. Requests for raw database mutation, silent destructive cleanup, or weakened concurrency and verification guards are outside the supported product direction.
