# Calendar Early Reminder

Status: implemented; exact-host Native admission only.

## Problem and evidence

An EventKit relative alarm can save and verify while the Reminders Early
Reminder control remains None. The public EKAlarm API provides absolute dates
and elapsed-second relative offsets; it has no calendar-month Early Reminder
property. Apple documents the separate app control in
[Add or change reminders](https://support.apple.com/guide/reminders/remndc729e28/mac).

On macOS 26.5.2 (25F84), Reminders 7.0 (3976), a disposable all-day annual
Reminder with a relative alarm reproduced this mismatch. Selecting one month
before in the app produced a REMDueDateDeltaAlert with REMDueDateDeltaInterval
unit 4, count -1. The existing ordinary REMAlarmTimeIntervalTrigger survived.
REMStore's default fetch and EventKit omit the delta context. The explicit
REMReminderFetchOptions.includeDueDateDeltaAlerts fetch returned it.

## Interface

Keep 15 public tools and existing Core alarm replacement semantics.
`organize_reminder.set_early_reminder` accepts `early_reminder:null`, or a closed
`{unit, value}` interval: minute/hour/day/week/month and integer 1–200. Units
map to native enum 0–4 and a negative count. A month is never converted to
30 days or seconds. Inspect via `inspect_reminder_native` with
`include:["early_reminder"]`; diagnose the `early_reminder` scope.

Create the Core due/recurrence first, then configure the exact created item.
This deliberately avoids a two-backend create pretending to be atomic.
Omission of an Early Reminder action preserves it. Null clears only its
context; ordinary alarms are separate. Multiple or unsupported native delta
alerts are read-only and cannot be silently collapsed or cleared.

## Runtime and verification

The existing signed sections executable gains `early-read` and `early-set`
internal commands. Ordinary use requires the full verified signed Native
bundle; explicit contributor source builds retain the existing opt-in.
Neither path bypasses exact runtime/schema admission. No SQLite writes are
introduced. The read-only schema gate binds ZREMCDREMINDER and
ZREMCDDUEDATEDELTAALERT, including the delta unit/count and reference columns.
The reviewed command fingerprint is
`644afc465f44355207ce5dc511f85e44328c1fd709bdd037bb56d8223c3133c7`.
Unknown builds, schemas, or missing runtime evidence stay blocked.

The mutation revalidates the opaque Core revision, exact private version and
native storage digest. A separate native fetch immediately before save checks
the same storage digest. REMSaveRequest.updateReminder supplies the
REMReminderDueDateDeltaAlertContextChangeItem. Its
removeAllFetchedDueDateDeltaAlerts and addDueDateDeltaAlertWithDueDateDelta
methods stage the requested change; saveSynchronouslyWithError commits with
syncToCloudKit enabled. A pre-save race is rejected, and native save errors
retain possible-write semantics. The private API cannot provide an atomic
cross-process compare-and-swap; a race after the last read is still possible
and final preservation checks detect observable drift.

A fresh native snapshot verifies the exact interval/count and hashed complete
user state: identity/list/parent, rich and plain title/notes, URL/user activity,
priority/completion/flag, due/start/all-day/time zone, recurrence, complete
ordinary alarm multiset, attachments, hashtags, assignments, contacts and
urgent state. Derived display and provider revision metadata are excluded.
The adapter performs another native read, then the Native backend checks all
stable Core fields through fresh EventKit. The facade issues a fresh reference
only after its final exact read and interval projection agree. Lost final reads
remain pending; failed reads never become an absent Early Reminder.

## Calendar behavior and limits

The native interval is retained for each recurrence; the plugin does not
precompute a future series into absolute dates. The opt-in data-free native
calendar checker exercises REMDueDateDeltaInterval.addedTo: for month ends,
leap and non-leap February, and US/European DST. On this exact build, a lead
landing in the US spring gap advances 02:30 to 03:30; an autumn 01:30 fold
selects standard time. These are native calendar results, not a plugin-defined
conversion policy.
Live acceptance also patches due dates and ordinary alarms while checking that
the native month value survives. Existing recurring-completion restrictions
remain in force.

An all-day lead names a calendar day. The app's all-day notification time and
actual alert delivery remain controlled by Apple. Local native readback and
Mac UI evidence do not prove future delivery, iCloud convergence or iPhone UI.

## Distribution

Use the numeric 0.8.0 release version to invalidate installed caches. This
repository binds numeric app versions, exact source hashes, signatures and
attestations together; a local `+codex` manifest suffix is not an installable
signed release. Both helpers must be rebuilt by the existing main-owned
protected signing workflows before normal installation. Do not edit cached
plugin files or relax signature/provenance checks to make a source build look
like a release.
