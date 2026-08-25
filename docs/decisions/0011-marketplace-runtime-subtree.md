# Install from a canonical runtime subtree

The repo marketplace will point to `./plugins/apple-reminders`, which will contain the complete canonical runtime plugin, while repository tests, architecture documents, workflows, build tools, and release output remain outside that subtree. Codex 0.149.0 accepts a root `./` source, but its local installer recursively copies the entire source directory instead of applying this project's runtime allowlist; a clean-clone prototype copied about 1.78 MB for a roughly 788 KB runtime and would also include local development files from a worktree.

## Considered options

- Keep the plugin at repository root: simplest source layout, but the installed cache includes development and release files that are not part of the reviewed runtime.
- Publish a separate runtime-only repository or branch: produces a clean install, but creates a synchronization and release-integrity problem.
- Generate a duplicate plugin subtree: keeps root development paths, but makes runtime code exist in two places.

## Consequences

Runtime code and resources move once and remain single-source under `plugins/apple-reminders`; repository tools and tests address that root explicitly. Runtime files must not reach back to the parent repository. Source-only test dependency injection replaces the current `PLUGIN_ROOT/tests/test_mcp_server.py` presence check before the move. The marketplace can then be installed with a sparse checkout of only `.agents/plugins` and `plugins/apple-reminders`.

GitHub-facing `README`, changelog, license, privacy, security, support, and
terms documents remain canonical at the repository root so repository hosting
and policy links keep working. Byte-identical copies live in the runtime
subtree for offline installs; CI compares their SHA-256 digests and fails on
drift. Executable code, schemas, skills, and assets are never duplicated.
