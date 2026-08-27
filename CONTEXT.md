# Apple Reminders Plugin

The plugin presents Apple Reminders as safe, bounded user goals while preserving the native app's identity, synchronization, and visible behavior.

## Language

**Reminder**:
A user-managed task stored in Apple Reminders.
_Avoid_: Task row, record

**Reminder List**:
A collection with a stable identity that contains Reminders. Its display name is not its identity.
_Avoid_: Folder, named list

**Core**:
The routine Reminder work a first-time user expects to perform without diagnostics or repair preparation.
_Avoid_: Basic mode, simple mode

**Native Extension**:
A capability that preserves behavior visible in Apple Reminders or synchronized through iCloud when Core fields alone are insufficient.
_Avoid_: Private feature, optional hack

**Maintenance**:
An explicitly requested diagnosis, migration, repair, cleanup, or managed data-protection operation outside routine Reminder work.
_Avoid_: Advanced mode, normal cleanup

**Capability**:
An operation the current Mac, Reminders build, permissions, and available native paths can perform with known confidence.
_Avoid_: Feature flag, framework presence

**Revision**:
An opaque, fresh concurrency token returned by an exact read and required when a change could overwrite newer state.
_Avoid_: Timestamp, private version

**Receipt**:
The structured outcome of one requested change, distinguishing verified success, no change, pending verification, and partial success.
_Avoid_: Raw backend response, success boolean

**Snapshot**:
A managed local copy captured before a risky Maintenance change. It is not a promise of automatic restoration.
_Avoid_: Recovery, restore point
