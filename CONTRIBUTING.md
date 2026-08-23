# Contributing

Apple Reminders is a local macOS Codex plugin with real mutation capability and
version-sensitive private integration paths. Contributions must preserve
explicit trust boundaries, machine-readable failure semantics, bounded reads,
and deterministic source packaging.

## Required Checks

Run from the repository root without launching Reminders or using a live
Reminders store:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 scripts/validate_plugin.py .
PYTHONDONTWRITEBYTECODE=1 python3 scripts/validate_minis_export.py
PYTHONDONTWRITEBYTECODE=1 python3 scripts/audit_source_package.py .
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_*.py'
```

On macOS, also validate both native sources and the EventKit plist:

```bash
clang -x objective-c -fobjc-arc -framework Foundation -framework AppKit \
  -fsyntax-only scripts/remkit_attach_image.m
clang -x objective-c -fobjc-arc -framework Foundation -framework EventKit \
  -fsyntax-only scripts/reminders_eventkit.m
plutil -lint scripts/eventkit_bridge_info.plist
```

Exercise request validation without compiling or accessing EventKit:

```bash
printf '%s\n' '{"schema_version":1,"operation":"capabilities"}' | \
  PYTHONDONTWRITEBYTECODE=1 python3 scripts/eventkit_bridge.py --validate-only
```

CI must use synthetic fixtures and static validation. Do not add a CI step that
opens a user's Reminders database, launches Reminders, prompts for permission,
loads a private framework, or performs a write.

Mock backend substitution is source-test-only: set
`APPLE_REMINDERS_MCP_TEST_MODE=1` only in tests while
`tests/test_mcp_server.py` exists. Packaged runtime intentionally ignores
adapter, EventKit bridge, and doctor path overrides.

## Plugin and MCP Contract

- Keep `.codex-plugin/plugin.json` name aligned with the plugin directory and
  use strict semantic versioning.
- Declare `mcpServers` only when `.mcp.json` is substantive and all referenced
  server/schema files are packaged. If the MCP is removed, remove the manifest
  declaration, config, runtime files, documentation, and tests together.
- Keep MCP tools closed-schema, bounded, and exact-ID based for mutations.
- Keep the MCP server a thin transport over the adapter. Business logic and
  safety policy belong in the adapter/skills.
- Update the MCP `SERVER_VERSION` semantic core with the plugin manifest when a
  release changes it. The validator rejects drift.
- Do not add unsupported manifest fields or unresolved placeholders.

## Skills and Evals

Every directory under `skills/` must contain:

- `SKILL.md` with matching kebab-case frontmatter name and a specific trigger
  description;
- `agents/openai.yaml` with display name, short description, and default prompt;
- `evals/evals.json` with at least two non-empty cases.

Keep skills under 500 lines, keep local links resolvable, and add/update evals
when behavior changes. The reduced OpenMinis export has a separate schema and
must continue to pass `scripts/validate_minis_export.py`.

## Safety and Truthfulness

- Read current, bounded state before mutations.
- Resolve ambiguous names to exact stable IDs.
- Preserve fields the user did not ask to change.
- Keep destructive/bulk operations previewable, bounded, and recoverable where
  possible.
- Prefer EventKit for fields and deletion that its public API supports; keep AppleScript for narrow compatibility/UI handoff cases.
- Gate private SQLite writes on explicit schema requirements, a fresh matching
  reminder version for existing-item mutations, transactions, read-back, and
  truthful recovery semantics. The MCP delete path stays in EventKit; the adapter's DB delete is diagnostic-only and remains version/capability gated.
- Treat ReminderKit and private store behavior as version-sensitive. Never turn
  a failed private path into an unreported fallback.
- Never hard-delete reminder rows. A URL attachment object row may be removed
  only under the documented native-parity contract: exact private-schema gate,
  fresh reminder version, one transaction, retained-and-bumped cloud-state
  tombstone, and read-back. Unused-label cleanup is the separate digest-gated
  maintenance exception documented in ADR 0009.
- Never infer success from process exit status alone.
- `verified` must name its evidence. Do not translate local/CloudKit evidence
  into “confirmed on iPhone” or guaranteed iCloud/shared-list delivery.
- Preserve `committed_verification_pending`, `partial_success`,
  `failed_no_mutation`, and `failed_manual_repair_required` distinctions.

## Privacy and Fixtures

Use only synthetic fixtures. Never commit, package, or attach:

- real reminder titles, notes, URLs, account/list/section/tag names, IDs, or
  attachment contents;
- Reminders SQLite files or WAL/SHM files;
- journals, caches, idempotency/capability records, schema dumps tied to a user,
  or container backups;
- `.DS_Store`, bytecode, screenshots/UI captures, archives, recovery copies, or
  compiled helpers.

Do not inspect the contents of a suspected screenshot, archive, backup, or user
data file merely to decide whether it belongs in the package. Classify it by
path and reject it. Redact diagnostics before sharing them.

## Deterministic Release Artifact

Build only through the allowlisted packager:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 scripts/build_source_package.py \
  --output-directory dist
```

The package intentionally excludes `.github/`, `tests/`, `docs/`,
`CONTRIBUTING.md`, `minis/`, and the packaging scripts themselves. Adding a new
runtime file requires an intentional allowlist update plus a packaging test.
Run the build twice in separate empty directories and compare SHA-256 values
before a release. Do not hand-assemble a ZIP from the working tree.

## Private API Review

Changes involving private ReminderKit or the Reminders database must document:

- the exact macOS/Reminders build and schema fingerprint used for evidence;
- public alternatives considered;
- fields/tables/selectors touched and transaction boundaries;
- preconditions, read-back evidence, partial-failure behavior, and recovery;
- why the feature remains blocked when any prerequisite is unknown.

Keep this private integration out of OpenMinis contributions unless that
project explicitly accepts the dependency. Use only `minis/apple-reminders/`
for the reduced public command surface.
