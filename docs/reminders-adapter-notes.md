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

Reminder creation proof:

- A reminder row can be inserted directly with `REMCDReminder` plus a matching `REMCKCloudState` row.
- The list's reminder ordering JSON should include the new reminder UUID.
- `ZTITLE` and `ZNOTES` alone are not enough for native list rendering. Reminders also expects gzip-compressed rich text document blobs in `ZTITLEDOCUMENT` and `ZNOTESDOCUMENT`.
- Once those document blobs are present, DB-created reminders render title and notes correctly in the native Reminders UI.
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

The cache is not a source of truth. It stores only lightweight fields that can be rebuilt from Reminders: list and section IDs/names, reminder IDs/titles, completion, priority, flagged state, due/display/completion/modified timestamps, image and URL attachment counts, and notes length plus SHA-256 hash. It does not store image contents, attachment payloads, or full notes.

Unsupported writes as of this note: urgent alerts, location alerts, message-when-messaging alerts, tags, and attachment removal. These surfaces have private data shapes or higher destructive risk and should not be exposed until verified separately.

Cache searches do not search note bodies because the cache does not keep them. Use `search_reminders` when full note text must be searched from the source database.

## Adapter Rules

- Always run a schema doctor before private writes.
- Always back up the Reminders container before experiments or broad changes.
- Keep transactions narrow.
- Verify every write by reading back through the app state or database.
- Treat private-store writes as local-first until iCloud behavior is tested more deeply.
