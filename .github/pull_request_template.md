## Summary

<!-- Describe the user-visible outcome and why it belongs in this plugin. -->

## Scope

- Public Interface impact:
- Core / Native Extension / Diagnostics / internal boundary:
- Migration or compatibility impact:

## Verification

- [ ] Added or updated behavior-level tests at the public Module or MCP boundary.
- [ ] Ran the full synthetic test suite.
- [ ] Ran plugin and strict source-package validation.
- [ ] Verified deterministic packaging when runtime files changed.
- [ ] Recorded any live Reminders check using disposable data and exact cleanup.

## Safety and privacy

- [ ] No real Reminder titles, notes, URLs, identifiers, screenshots, databases, journals, caches, backups, or local paths are included.
- [ ] Possible-write failures preserve honest pending/partial/manual-repair semantics and do not recommend blind retry.
- [ ] Existing-item writes use an exact fresh Reference and read-back appropriate to the operation.
- [ ] Public tool, skill, documentation, and lifecycle changes remain consistent.

## Release notes

<!-- State whether CHANGELOG, version, marketplace metadata, or release artifacts need an update. -->
