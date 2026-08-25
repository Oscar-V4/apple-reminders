# Calendar Plugin Reference Notes

Calendar integrations are comparison inputs for Apple Reminders, not templates
whose structure or quality claims should be copied wholesale.

## Google Calendar: product-discipline reference

The useful Google Calendar pattern is its operating discipline:

- the skill is an operating contract rather than a command manual;
- state is read before reasoning or mutation;
- reads have a concrete semantic scope as well as a numeric limit;
- ambiguity is resolved from existing data when possible;
- writes preserve omitted fields;
- bulk changes identify the exact qualifying set first;
- output emphasizes decisions, changes, and uncertainty rather than raw payloads.

Google Calendar uses a hosted connector. Apple Reminders has no equivalent
connector for the current user's native local store, so matching its tool count,
authentication model, or package shape would be artificial.

## Apple Calendar plugin: implementation reference only

The recent `mightymattys/apple-productivity-mcp` Apple Calendar/Reminders
project was reviewed as a current community example. Its canonical plugin
layout, short Quick Start, changelog, and disposable smoke-cleanup flow are
useful release references.

It is not this project's quality bar. Its surface and safety trade-offs serve a
different design, and do not justify loose schemas, unbounded results, ambiguous
selectors, or weaker concurrency and Receipt contracts here. We copy evidence
that improves installation and test ergonomics, not the reference repository's
entire architecture.

## Apple Reminders translation

The 0.3 design is:

- **Core:** EventKit-backed access, exact list identity, bounded fetch, exact
  read, create/change/delete, and idempotent list creation;
- **Native Extension:** exact sections, tags, and native image/URL attachment
  behavior behind guarded private interfaces;
- **Diagnostics:** one content-free targeted tool used after a relevant failure;
- **local MCP:** closed input discovery, exact dispatch, concise text, and
  centrally validated structured results and Receipts.

An exact read returns an opaque `rev1` Reference. This preserves the calendar
principle of guarded writes without making callers choose between EventKit
`last_modified` and private `reminder_version` fields.

## Product-parity test

An improvement counts as calendar-grade product discipline only when it:

- reads relevant state before a guarded write;
- requires semantic scope in addition to a numeric limit;
- binds continuation cursors to the original filters;
- distinguishes all-day dates, timed due dates, and alarms;
- uses exact account/list/reminder identity instead of display names;
- preserves omitted fields and rejects stale References before mutation;
- returns verified, pending, partial, unchanged, and failed outcomes without
  optimistic wording;
- preserves visible URL, native image/section, tag, concurrency, idempotency,
  and Receipt behavior already proven through real use;
- packages task-oriented skills above the raw tools.

## Intentional non-parity

Unused-tag cleanup, attachment repair, backup/Snapshot, restore, log purge, flag
mutation, and native UI handoff are withheld from the public 0.3 Interface.
Their existence in an internal adapter or another plugin does not make them
necessary first-use product features.
