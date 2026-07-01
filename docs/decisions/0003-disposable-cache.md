# 0003. Disposable Cache

## Status

Accepted.

## Decision

Use a disposable local cache for fast full-grasp reads.

Apple Reminders remains the only source of truth. Any cache created by this plugin must be safe to delete and rebuild.

## Rationale

The plugin needs a fast way to understand the user's Reminders structure across lists, sections, reminders, dates, completion state, and attachment presence.

Reading the whole store from scratch is acceptable for early development, but repeated assistant workflows benefit from a lightweight index.

The cache should not become a competing database.

## Cache Contents

The v1 cache may store:

- account IDs
- list IDs and names
- section IDs and names
- reminder IDs and titles
- completion state
- due and reminder dates
- priority and flags
- list and section membership
- attachment presence and counts
- notes hash, notes length, and possibly a short derived summary
- source modification timestamps

The v1 cache should not store:

- image file contents
- full attachment payloads
- full notes by default
- authoritative user intent or task state that cannot be rebuilt from Reminders

## Future Extension

A separate assistant memory or decision journal may be added later. It must be clearly separate from the disposable cache and must not replace Reminders as the source of truth.
