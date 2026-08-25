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

In the installed 1.2.3 snapshot reviewed on 2026-08-25, Google Calendar exposes
15 connector actions under five task-oriented skills. That is useful
counter-evidence to treating a smaller raw tool count as the quality goal: its
coherence comes from bounded reads, preservation rules, explicit recurring-event
scope, and purpose-level workflows above the connector.

## Apple Calendar references

### redpop: distribution and deep-module reference

[`redpop/apple-calendar-mcp`](https://github.com/redpop/apple-calendar-mcp) 0.2.1
exposes six task-level tools over a TypeScript MCP server and one lazily started,
long-lived Swift/EventKit helper. Its strongest ideas are a signed and notarized
universal helper, compiler-free install, a fixed helper path that preserves the
TCC grant across updates, explicit recurring-event scope with no destructive
default, and tests around helper lifecycle and packaging.

It is still a comparison input, not a safety contract to copy. The reviewed
release has no opt-in EventKit/TCC smoke harness, create is not idempotent, and
writes return the saved object without this project's independent exact
read-back/Receipt semantics. Its private TCC self-responsibility re-exec also
hung when rebuilt with the locally installed Xcode 26.5 SDK even though the
published SDK 14.5 binary worked. A signed/prebuilt Reminders helper remains a
valuable stable-release direction, but the private TCC mechanism and Node+Swift
runtime should not be imported without a Reminders-specific failure and
compatibility study.

### mightymattys: layout reference only

The recent
[`mightymattys/apple-productivity-mcp`](https://github.com/mightymattys/apple-productivity-mcp)
Apple Calendar/Reminders project was reviewed as a current community example.
Its canonical plugin layout, short Quick Start, changelog, and disposable
smoke-cleanup flow are useful release references.

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

Neither the six-tool Apple Calendar server nor the 15-action Google Calendar
connector implies that Apple Reminders should chase a particular count. The
current eight Core, four Native Extension, and one Diagnostics boundaries are
appropriate while each maps to a recognizable task and the failure-triggered
Diagnostics tool stays out of normal first use.

## Intentional non-parity

Unused-tag cleanup, attachment repair, backup/Snapshot, restore, log purge, flag
mutation, and native UI handoff are withheld from the public 0.3 Interface.
Their existence in an internal adapter or another plugin does not make them
necessary first-use product features.
