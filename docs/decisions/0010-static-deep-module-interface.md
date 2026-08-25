# Use a static deep-module interface for the 0.3 public beta

The 0.3 public beta will expose one static 13-tool Interface: eight Core tools, four Native Extension tools, and one content-free diagnostic tool. This keeps first installation predictable in current Codex clients while moving revision selection, backend choice, URL composition, and native read-back behind deep Modules. We rejected both the 32-tool surface, which leaks ordering and backend knowledge to callers, and same-turn dynamic tool activation, which did not make a newly announced tool callable in a Codex 0.149.0 acceptance test.

## Considered options

- Keep roughly 24 purpose-specific tools: strongest name compatibility, but too much caller knowledge and duplicated orchestration remain.
- Collapse to eight union-based tools: smallest surface, but large discriminated unions reduce discoverability and create a new shallow Interface.
- Dynamically expose capability families: attractive in theory, but not reliable enough for a first public release.

## Consequences

The public tool names change before the first tagged release, while every user-validated behavior must move to the new Interface before its old route is removed. Doctor remains available only as a content-free diagnostic after a relevant failure. Unused-tag cleanup, attachment repair, backup/Snapshot handling, and log purge remain internal until their user value justifies a complete public preview/apply and restoration contract.

MCP `outputSchema` is optional and is not advertised in the default `tools/list`. Duplicating full Reminder and Receipt schemas across 13 descriptors grew the compact discovery payload to 87,200 bytes; input-only discovery is about 19 KiB. Result shapes remain versioned and enforced by centralized Module validators and boundary fixtures, while clients receive both concise text and structured content.
