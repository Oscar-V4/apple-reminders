# Reminders Adapter Notes

These notes capture local findings from the initial macOS Reminders investigation.

## Public Surfaces

AppleScript can create and inspect lists and reminders. It exposes common fields such as title, body, due date, reminder date, completion, priority, and flagged state.

EventKit exposes accounts/calendars and public reminder fields, including typed due dates, absolute and coordinate-backed location alarms, recurrence, completion, priority, URLs, and list-to-list moves. It does not expose native image attachments, tags, or list sections.

The bundled EventKit bridge is now the default public-field path. It requires a native EventKit predicate bound by calendar IDs or the matching incomplete-due/completed-completion range; text and modified-time filters are post-filters and do not make an otherwise broad native fetch safe. Existing-reminder writes require an exact identifier and last-modified precondition.

## Private Store Surfaces

The Reminders store lives under:

`~/Library/Group Containers/group.com.apple.reminders/Container_v1/Stores/`

Attachment files live under:

`~/Library/Group Containers/group.com.apple.reminders/Container_v1/Files/<Account>/Attachments/`

Observed Core Data entities:

- `REMCDReminder`
- `REMCDBaseList`
- `REMCDBaseSection`
- `REMCDHashtagLabel`
- `REMCDImageAttachment`
- `REMCDURLAttachment`
- `REMCKCloudState`

Image attachment proof, revised after iPhone testing:

- Copying an image file into the account attachment folder, inserting a `REMCDImageAttachment` row, and inserting/updating `REMCKCloudState` can make the image render on the Mac.
- That DB-only path is not sufficient for iPhone visibility. The local row can have no `ZCKSERVERRECORDDATA`, no `ZINCLOUD=1`, and no synced local version, which leaves the attachment Mac-local in practice.
- The working mobile-visible path uses private ReminderKit classes (`REMStore`, `REMSaveRequest`, `REMReminderAttachmentContextChangeItem`, and `REMImageAttachment`) through the local `remkit_attach_image.m` helper.
- `attach_image` defaults to that ReminderKit backend, then verifies `mobile_visible_likely` by reading back CloudKit evidence from the Reminders store.
- Helper success is not enough: the adapter rejects pre-existing or ambiguous rows and reports partial failure if mobile-visible evidence does not arrive within the bounded verification window.
- `replace_attachment --image` uses the same ReminderKit backend for the new image, then soft-deletes the selected old attachment. If that second step fails, it attempts a compensating soft-delete of the new attachment and never reports full success.
- `attach_image --backend db` remains a fallback/diagnostic path only. Treat those rows as local-only unless `audit_attachments` proves otherwise.
- `repair_attachments` finds local-only image rows, locates the source file by SHA512, reattaches through ReminderKit, and soft-deletes the older local-only attachment object after a backup.

URL attachment proof from a native Reminders add/replace/delete round trip:

- Native URL replacement physically removes the old `ZREMCDOBJECT` row, keeps its `ZREMCKCLOUDSTATE` row as the sync tombstone, and gives the new URL row the old display order.
- Native URL deletion likewise removes the object row and retains/bump-syncs the cloud state. The adapter mirrors those URL-specific semantics; image attachment deletion and repair continue to use soft-delete behavior.
- The live evidence was captured on macOS 26.5.2 (25F84), Reminders 7.0,
  against schema fingerprint
  `82761d59e465cf4c90ca8c98bb51eab498c6976e81d608023535f3bf0ec63d62`.
  The public EventKit `EKReminder.url` field was considered but cannot represent
  multiple ordered URL attachment objects. The private operation therefore
  touches only the exact URL `ZREMCDOBJECT` row, its existing
  `ZREMCKCLOUDSTATE` (`ZCURRENTLOCALVERSION`, `ZLOCALVERSIONDATE`), and the
  parent reminder version inside one write transaction. It fails and rolls
  back if the cloud-state row is absent or its bumped version cannot be read
  back. Other builds remain subject to the doctor/schema gate and live
  verification; this observation is not a general macOS compatibility claim.

Section proof:

- Insert a `REMCDListSection` row.
- Link it to the list.
- Create sections and update list-section membership through `REMSaveRequest` in `remkit_sections.m` so Reminders generates its native CRDT and CloudKit state.
- Verify the section or list cloud state reaches `inCloud=1` with `latestSyncedVersion >= currentLocalVersion`; local native rendering alone is insufficient.

URL attachment proof:

- Insert a `REMCDURLAttachment` row linked to the reminder with `ZUTI='public.url'` and the target `ZURL`.
- Insert/update the related `REMCKCloudState`.
- Restart/read Reminders and verify that the URL appears in the native detail panel. Setting only the reminder-level `ZICSURL` was not enough to render in the UI.

Tag proof:

- Tag labels live in `ZREMCDHASHTAGLABEL`.
- Reminder tag assignments are `ZREMCDOBJECT` rows with `Z_ENT=32`, `ZREMINDER3` linked to the reminder, and `ZHASHTAGLABEL` linked to the label.
- `add_tag` find-or-creates the label and inserts an active assignment object. Duplicate add is idempotent.
- `remove_tag` soft-deletes only the assignment object. Label cleanup is separate and scoped through `cleanup_tags`.

Historical private reminder creation proof:

- A reminder row can be inserted directly with `REMCDReminder` plus a matching `REMCKCloudState` row.
- The list's reminder ordering JSON should include the new reminder UUID.
- `ZTITLE` and `ZNOTES` alone are not enough for native list rendering. Reminders also expects gzip-compressed rich text document blobs in `ZTITLEDOCUMENT` and `ZNOTESDOCUMENT`.
- Even when those document blobs are present, live testing found that freshly DB-created reminders can render in the native list without visible title text until the public Reminders object model rewrites the text.
- The private adapter therefore used a hybrid path: create the row, dates, ordering, and private fields through SQLite, then immediately sync title/body through AppleScript so the native Reminders UI renders the text reliably.
- Timed due/reminder dates set `ZDUEDATE`, `ZDISPLAYDATEDATE`, `ZTIMEZONE`, and `ZDISPLAYDATETIMEZONE`.
- All-day due dates set `ZALLDAY=1`, `ZDISPLAYDATEISALLDAY=1`, local-midnight `ZDISPLAYDATEDATE`, and UTC-midnight `ZDUEDATE`.

Normal public reminder creation and editing now use EventKit. The bridge keeps `due` and `alarms` separate, rejects timezone-naive timed values, supports absolute or coordinate-backed location alarms, and never treats a due date as an alert implicitly. Direct SQLite `--remind-at` writes are rejected because the previous mapping could conflate those concepts.

## Disposable Cache

The adapter has a rebuildable JSON cache under:

`~/Library/Caches/apple-reminders-codex/cache.json`

Cache commands:

- `cache_rebuild`: read the selected Reminders SQLite store and atomically rewrite the disposable cache.
- `cache_info`: report cache path, size, source database metadata, counts, and stale status when the source database still exists.
- `cache_search`: search active cached reminders by cached lightweight fields.
- `cache_query`: filter cached reminders without requiring a search term.

The cache is not a source of truth. It stores only lightweight fields that can be rebuilt from Reminders: list and section IDs/names, reminder IDs/titles, tag names/counts, completion, priority, flagged state, due/display/completion/modified timestamps, image and URL attachment counts, and notes length plus SHA-256 hash. It does not store image contents, attachment payloads, or full notes.

Supported private writes include CloudKit-verified sections and mobile-visible image attachments through ReminderKit, URL attachment objects, tag assignment writes, native-parity attachment removal/replacement, and local-only image attachment repair. Location alarms are supported only through the public EventKit bridge with explicit coordinates and enter/leave proximity. Unsupported writes as of this note include urgent alerts and message-when-messaging alerts; do not emulate them through private fields.

Every private mutation of an existing reminder requires a fresh matching
`if_version`; obtain it from `list_attachments`/`list_reminder_attachments`
immediately before the write.

Cache searches do not search note bodies because the cache does not keep them. Use `search_reminders` when full note text must be searched from the source database.

## Adapter Rules

- Always run a schema doctor before private writes.
- Use the content-free plugin doctor for first-run diagnostics; request EventKit access only through the separate explicit permission tool.
- Always back up the Reminders container before experiments or broad changes.
- Treat container archives as best-effort snapshots of a live store and verify them before relying on recovery.
- Keep transactions narrow.
- Verify every write by reading back through the app state or database.
- For public reminder fields, prefer the EventKit MCP tools. The adapter's AppleScript text-sync exists for legacy/private creation paths and is not a reason to choose DB-only writes.
- MCP `delete_reminder` uses public EventKit with a fresh last-modified precondition and no private fallback. An unresolvable identifier returns a no-write not-found result and requires a fresh read; it is not treated as proof that a previous delete succeeded because EventKit identifiers can change after a full sync. The adapter CLI retains `backend=auto`/DB soft-delete only for compatibility and diagnostic recovery-parity work; DB eligibility still requires a fresh version plus exact environment evidence.
- `cleanup_tags --apply` intentionally hard-deletes unused label rows, but only after a scoped preview digest, literal wildcard handling, a write lock, account-aware revalidation, zero-reference proof, backup, and read-back.
- Treat private-store writes as local-first until iCloud behavior is tested more deeply. For image attachments specifically, do not report success for user-facing capture unless the read-back shows CloudKit/mobile visibility evidence.
