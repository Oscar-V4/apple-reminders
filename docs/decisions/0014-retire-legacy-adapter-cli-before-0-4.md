# Retire the legacy adapter CLI before the 0.4 release

## Status

Accepted. Implemented by the deletion-focused follow-up stacked on the 0.4
workflow-hardening head.

The resulting deterministic archive is about 1.16 MiB, so the package hard
ceiling returns from the temporary 1.28 MiB allowance to 1.20 MiB. The recovered
space is retained as a growth guard rather than immediately spent on another
abstraction; exact bytes and SHA-256 remain build artifacts rather than an ADR
invariant.

Implementation removed all 20 obsolete parser routes, 64 command-owned
top-level functions, the 245-line recovery-backup module, and their dedicated
tests. Every retained adapter function and class remains structurally identical
to the stabilized hardening head; only `build_parser` changed among surviving
functions.

ADR 0009 retained direct adapter writes only through the 0.2 series and called
for a separate removal review at 0.3. The repository is now 0.4.0, the public
MCP surface is closed at 15 tools, and `tests/test_legacy_cli_deprecation.py`
has accidentally turned that temporary migration policy into a permanent
invariant.

Static reachability from the packaged server shows that the runtime needs 16
adapter commands plus the in-process `AdapterError` and `execute_idempotent`
library seams.

## Decision

Retain exactly these adapter CLI commands:

```text
read_reminder
list_sections
list_tags
add_tag
remove_tag
create_section
move_to_section
attach_image
copy_image_attachment
attach_url
list_attachments
delete_attachment
replace_attachment
list_deleted_reminders
read_deleted_reminder
recover_deleted_reminder
```

Remove these 20 obsolete commands before publishing 0.4:

```text
doctor
backup_store
purge_logs
cache_rebuild
cache_info
cache_search
cache_query
list_lists
snapshot
search_reminders
show_reminder
cleanup_tags
create_list
create_reminder
update_reminder
complete_reminder
reopen_reminder
delete_reminder
audit_attachments
repair_attachments
```

The five direct Core writes are the highest-priority removal because they keep
a second write interface beside EventKit Core. Maintenance, backup, repair,
log-purge, cache, and UI-handoff routes also enlarge the mutation, privacy, and
support surface without serving a public Module.

## Delivery boundary and implementation

This deletion was a v0.4 release blocker, but it was kept out of the workflow
hardening patch. Removing thousands of command-owned lines while changing MCP
runtime ownership and dispatch evidence would have made regressions hard to
localize and the review boundary too broad. It was therefore delivered as a
deletion-focused follow-up based on the stabilized hardening head.

The follow-up:

- inverts `tests/test_legacy_cli_deprecation.py` so the parser command set equals
  the 16-command allowlist and removed commands are rejected;
- removes command-owned functions by symbol rather than deleting broad source
  ranges that contain shared receipt, database, attachment, or idempotency
  helpers;
- removes the now-unreferenced `scripts/reminders_recovery.py` and its legacy
  backup/repair policy tests;
- shrinks runtime and diagnostic command-schema contracts accordingly;
- updates the package allowlist and public/internal-surface documentation;
- preserves the on-disk idempotency format and all public 15-tool results;
- passes the complete suite, deterministic package audit, and live public MCP
  smoke. Tagging remains a separate publication decision.

## Rejected alternatives

- Keeping the commands indefinitely contradicts the recorded migration policy
  and leaves a real second write surface in the shipped package.
- Hiding only parser entries leaves dead safety-sensitive code and privacy
  surface in the artifact.
- Removing them inside the current patch saves review time superficially but
  makes failures across two independent architectural changes difficult to
  attribute and revert.
