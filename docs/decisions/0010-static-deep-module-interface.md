# Use a static deep-module interface for the 0.3 public beta

The 0.3 public beta will expose one static 15-tool Interface: seven Core tools, five Native Extension tools, and three Maintenance tools. This keeps first installation predictable in current Codex clients while moving revision selection, backend choice, URL composition, native read-back, and snapshot planning behind three deep Modules. We rejected both the 32-tool surface, which leaks ordering and backend knowledge to callers, and same-turn dynamic tool activation, which did not make a newly announced tool callable in a Codex 0.149.0 acceptance test.

## Considered options

- Keep roughly 24 purpose-specific tools: strongest name compatibility, but too much caller knowledge and duplicated orchestration remain.
- Collapse to eight union-based tools: smallest surface, but large discriminated unions reduce discoverability and create a new shallow Interface.
- Dynamically expose capability families: attractive in theory, but not reliable enough for a first public release.

## Consequences

The public tool names change before the first tagged release, while every user-validated behavior must move to the new Interface before its old route is removed. Maintenance remains in the same artifact for one-step installation, but its three explicitly named tools are used only for diagnosis, preview, and plan-token apply flows.
