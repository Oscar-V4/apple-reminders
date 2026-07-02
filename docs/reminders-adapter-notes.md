# Reminders Adapter Notes

These notes capture local findings from the initial macOS Reminders investigation.

## Public Surfaces

AppleScript can create and inspect lists and reminders. It exposes common fields such as title, body, due date, reminder date, completion, priority, and flagged state.

EventKit exposes reminders and calendars but does not expose native image attachments or list sections.

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

Image attachment proof:

- Copy image file into the account attachment folder using its SHA512 digest as filename.
- Insert a `REMCDImageAttachment` row linked to the reminder.
- Insert/update related `REMCKCloudState`.
- Restart/read Reminders and verify native thumbnail display.

Section proof:

- Insert a `REMCDListSection` row.
- Link it to the list.
- Update list section membership JSON so reminder UUIDs map to section UUIDs.
- Restart/read Reminders and verify native section rendering.

URL attachment proof:

- Insert a `REMCDURLAttachment` row linked to the reminder with `ZUTI='public.url'` and the target `ZURL`.
- Insert/update the related `REMCKCloudState`.
- Restart/read Reminders and verify that the URL appears in the native detail panel. Setting only the reminder-level `ZICSURL` was not enough to render in the UI.

Tag proof:

- Tag labels live in `ZREMCDHASHTAGLABEL`.
- Reminder tag assignments are `ZREMCDOBJECT` rows with `Z_ENT=32`, `ZREMINDER3` linked to the reminder, and `ZHASHTAGLABEL` linked to the label.
- `add_tag` find-or-creates the label and inserts an active assignment object. Duplicate add is idempotent.
- `remove_tag` soft-deletes only the assignment object. Label cleanup is separate and scoped through `cleanup_tags`.

Reminder creation proof:

- A reminder row can be inserted directly with `REMCDReminder` plus a matching `REMCKCloudState` row.
- The list's reminder ordering JSON should include the new reminder UUID.
- `ZTITLE` and `ZNOTES` alone are not enough for native list rendering. Reminders also expects gzip-compressed rich text document blobs in `ZTITLEDOCUMENT` and `ZNOTESDOCUMENT`.
- Even when those document blobs are present, live testing found that freshly DB-created reminders can render in the native list without visible title text until the public Reminders object model rewrites the text.
- The adapter therefore uses a hybrid path: create the row, dates, ordering, and private fields through SQLite, then immediately sync title/body through AppleScript so the native Reminders UI renders the text reliably.
- Timed due/reminder dates set `ZDUEDATE`, `ZDISPLAYDATEDATE`, `ZTIMEZONE`, and `ZDISPLAYDATETIMEZONE`.
- All-day due dates set `ZALLDAY=1`, `ZDISPLAYDATEISALLDAY=1`, local-midnight `ZDISPLAYDATEDATE`, and UTC-midnight `ZDUEDATE`.

## Disposable Cache

The adapter has a rebuildable JSON cache under:

`~/Library/Caches/apple-reminders-codex/cache.json`

Cache commands:

- `cache_rebuild`: read the selected Reminders SQLite store and atomically rewrite the disposable cache.
- `cache_info`: report cache path, size, source database metadata, counts, and stale status when the source database still exists.
- `cache_search`: search active cached reminders by cached lightweight fields.
- `cache_query`: filter cached reminders without requiring a search term.

The cache is not a source of truth. It stores only lightweight fields that can be rebuilt from Reminders: list and section IDs/names, reminder IDs/titles, tag names/counts, completion, priority, flagged state, due/display/completion/modified timestamps, image and URL attachment counts, and notes length plus SHA-256 hash. It does not store image contents, attachment payloads, or full notes.

Supported safe v1 private writes include sections, image attachments, URL attachments, tag assignment writes, and attachment soft-delete/replacement. Unsupported writes as of this note: urgent alerts, location alerts, and message-when-messaging alerts. These surfaces have private data shapes and should not be exposed until verified separately.

Cache searches do not search note bodies because the cache does not keep them. Use `search_reminders` when full note text must be searched from the source database.

## Adapter Rules

- Always run a schema doctor before private writes.
- Always back up the Reminders container before experiments or broad changes.
- Keep transactions narrow.
- Verify every write by reading back through the app state or database.
- For reminder title/body writes, prefer AppleScript or the adapter's AppleScript text-sync post-write, not a DB-only write.
- Treat private-store writes as local-first until iCloud behavior is tested more deeply.
