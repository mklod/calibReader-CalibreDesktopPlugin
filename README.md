# CalibReader Sync — Calibre Desktop Plugin

A small, focused [Calibre](https://calibre-ebook.com/) plugin that pulls the
**read / unread status** you set in the **CalibReader** mobile apps (iOS &
Android) back into your desktop Calibre library.

It is the desktop half of CalibReader's read-status sync loop. The mobile app
writes a lightweight `CalibReader_sync.json` sidecar into your cloud library
folder; this plugin reads that file and updates a **`#read_status`** custom
column in Calibre's `metadata.db`.

| | |
|---|---|
| **Name** | CalibReader Sync |
| **Version** | 1.1.1 |
| **Author** | CalibReader |
| **Platforms** | Windows, macOS, Linux |
| **Minimum Calibre** | 9.0.0 |
| **License** | GPL v3 (see `LICENSE`) |
| **Dependencies** | None beyond Calibre itself (stdlib `json` / `os` / `datetime` / `re` only) |
| **Network access** | None — reads a local file only |

---

## How the sync loop works

```
   ┌─────────────────────┐   marks book "read"    ┌──────────────────────┐
   │  CalibReader mobile  │ ─────────────────────► │  CalibReader_sync.json│
   │  (iOS / Android)     │   writes book UUID      │  in cloud library     │
   └─────────────────────┘                          └──────────┬───────────┘
                                                                │ Dropbox / Google
                                                                │ Drive / OneDrive
                                                                │ desktop sync client
                                                                ▼
   ┌─────────────────────┐   "Sync Read Status"    ┌──────────────────────┐
   │  Calibre desktop     │ ◄───── reads file ───── │  Local library folder │
   │  + this plugin       │   updates #read_status  │  (metadata.db + json) │
   └─────────────────────┘                          └──────────────────────┘
```

1. **Mobile → cloud.** When you mark a book read in the CalibReader app, the
   book's UUID is recorded in `CalibReader_sync.json` in your cloud library
   folder (the same folder that holds `metadata.db`).
2. **Cloud → desktop.** Your cloud client (Dropbox / Google Drive / OneDrive
   desktop app) syncs that file down into the local Calibre library folder.
3. **Plugin → metadata.db.** You click **Sync Read Status** in Calibre; the
   plugin reads the JSON and flips the `#read_status` column to *yes* for every
   matching book.

The plugin is intentionally **read-only and one-directional**: it never writes
to the JSON, never touches the internet, and never modifies any column other
than `#read_status`.

### Sidecar file format

```json
{
  "schema": 1,
  "updated_at": "2026-03-11T23:47:35.089553Z",
  "books": {
    "0f5c1e2a-....-uuid": true,
    "9a3b77de-....-uuid": { "value": false, "updated_at": "2026-11-02T18:31:12.482Z" }
  }
}
```

Books are keyed by their Calibre **UUID**, so matching survives differing book
IDs across devices/libraries. Matching is case-insensitive.

An entry is **either** a legacy bare boolean **or** an object with its own
`value` + `updated_at`. `schema` stays `1` — the two forms are allowed to mix in
one file, because a device still on an older build keeps writing bare bools.
A bare bool carries the **file-level** `updated_at`; an object carries its own.
Fractional seconds are allowed (Android writes them).

### Conflict resolution (v1.1.0)

Before v1.1.0 the file carried a single file-level timestamp and this plugin
applied **every** entry, **every** time. That meant a `false` written in
September would keep re-marking a book unread months after you finished it, and
flipping the flag in Calibre was undone by the next sync.

Now the plugin keeps its own per-book record — the stamp of the entry it last
applied, plus that book's `last_modified` immediately after that write — in the
plugin's preferences (`plugins/calibreader_sync.json`). **No column is added to
your library.** For each entry, in order:

1. **Undated entry** (legacy file with no usable stamp anywhere) — applied only
   as a bootstrap, when the plugin has no record for that book yet. Once a
   record exists, an undated source never overrides it.
2. **Stale** — stamp older than or equal to the one already applied → ignored.
   This is what retires that September `false` permanently.
3. **Desktop edit wins** — if the book changed in Calibre since the last apply
   (or was never synced), an entry whose stamp isn't strictly newer than the
   book's `last_modified` is ignored.
4. Otherwise the entry wins and `#read_status` is set.

Because step 3 keys off `last_modified`, **any** metadata edit in Calibre (not
just the read flag) can make a same-vintage sidecar entry lose. Re-toggling the
book in the app produces a newer stamp and syncs normally.

The plugin still **never writes** the sidecar. If read-status writeback is added
later it must emit the object form via `sidecar_merge.build_entry_object()`.

### Testing the merge rules

All of the above lives in `sidecar_merge.py`, which imports nothing from
Calibre:

```bash
python3 sidecar_merge.py      # 22 unit tests, no Calibre required
```

---

## Installation

### For users

1. Get `CalibReaderSync.zip` (shipped in this folder). If you need to rebuild
   it, zip these items **at the root of the archive** (not inside a subfolder):
   - `__init__.py`
   - `sidecar_merge.py`
   - `plugin-import-name-calibreader_sync.txt`
   - `images/icon.png`
2. In Calibre: **Preferences → Plugins → Load plugin from file** → choose the zip.
3. **Restart Calibre.**
4. Optional: **Preferences → Toolbar** → add **Sync Read Status** to the main
   toolbar so it's one click away.

### For developers

```bash
calibre-customize --add /path/to/CalibReaderSync.zip
```

Requires **Calibre 5.0.0 or later** (ships with Python 3).

---

## One-time setup: the `#read_status` column

The plugin updates a custom **Yes/No** column. Create it once before first use:

**Calibre → Preferences → Add your own columns → (+) Add custom column**

| Field | Value |
|---|---|
| Lookup name | `read_status` |
| Column heading | `Read Status` |
| Column type | `Yes/No` |

(The lookup name `read_status` becomes `#read_status` internally — that is what
the plugin writes to.) If the column is missing or the wrong type, the plugin
shows a dialog with these exact instructions instead of failing silently.

Also make sure:

- Your Calibre library folder is the **same folder** your cloud client syncs and
  the **same library** the mobile app reads.
- Your cloud client has finished syncing so `CalibReader_sync.json` is present
  in the library folder.
- At least one book has been marked read in the app (otherwise the sidecar file
  won't exist yet).

---

## Using it

Click **Sync Read Status** (toolbar or plugin menu). The plugin will:

1. Look for `CalibReader_sync.json` in the current library folder
   (`gui.library_path`).
2. Parse it and build a `uuid → book_id` map from your library.
3. Decide, per book, whether the sidecar entry is actually newer than what this
   library already has (see **Conflict resolution** above).
4. Batch-set `#read_status` for every winning book via
   `db.set_field('#read_status', …)` and refresh the library view.
5. Report how many books were updated, how many entries were skipped as stale,
   and how many UUIDs in the file did **not** match this library (e.g. they
   belong to a different library).

There is **no configuration dialog** — `is_customizable()` returns `False`. The
behaviour is fixed and dependency-free.

---

## Files in this folder

| File | Purpose |
|---|---|
| `__init__.py` | The Calibre-facing half: `CalibReaderSyncPlugin` (the `InterfaceActionBase` wrapper) + `CalibReaderSyncAction` (the `InterfaceAction` that reads the file, talks to `metadata.db`, and shows the dialogs). |
| `sidecar_merge.py` | Pure merge/decision logic — sidecar parsing, timestamp handling, per-book conflict rules, prefs bookkeeping. No Calibre imports; `python3 sidecar_merge.py` runs its unit tests. |
| `plugin-import-name-calibreader_sync.txt` | Required by Calibre's loader; declares the import name `calibreader_sync`. |
| `images/icon.png` | Toolbar icon (blue circular sync arrows), loaded at runtime via `get_icons('images/icon.png')`. |
| `make_icon.py` | Standalone script that procedurally generates `images/icon.png`. Not part of the shipped plugin. |
| `sync.png`, `sync.svg` | Reference/legacy icon source art. Not shipped. |
| `CalibReaderSync.zip` | The packaged, installable plugin. |

---

## Error handling

Every failure path shows an actionable Calibre dialog rather than crashing:

- **Sync file not found** — reminds you to let the cloud client finish syncing
  and to mark at least one book read in the app.
- **Failed to read sync file** — surfaces the JSON/parse exception text.
- **Missing/incorrect `#read_status` column** — prints the exact column setup.
- **Unmatched UUIDs** — informational only; lists how many book UUIDs in the
  file aren't in this library.

---

## Scope & limitations (v1.1.0)

- **Read-status only.** Reading *position* is synced separately by the mobile
  app via `CalibReader_positions.json` — this plugin does not touch it.
- **One-directional.** Marking a book read in *desktop* Calibre does not write
  back to the sidecar; only mobile → desktop is supported. A desktop edit is now
  *protected* from being reverted, but it still doesn't reach your phone.
- **Current library only.** Operates on whatever library is open
  (`gui.library_path`).
- **Highlights/annotations** sync is planned for a later version (see the PRD).
- **Sync bookkeeping is per-installation.** The applied-stamp record lives in
  this Calibre install's plugin prefs, not in the library, so a second desktop
  starts from scratch (its first sync is governed by rule 3 above).

---

## Roadmap

- Read-status **writeback** (desktop → mobile) so marking read in Calibre flows
  back to the app.
- Highlights & annotations sync (PRD v1.1).

---

## Changelog

### 1.1.0 — 2026-09-15

- **Per-entry timestamps.** `books[uuid]` may now be
  `{"value": <bool>, "updated_at": "<ISO-8601 UTC>"}`. Legacy bare bools are
  still read and inherit the file-level `updated_at`; `schema` stays `1`.
- **Stops stale entries re-applying forever.** The plugin records, per book, the
  stamp it last applied and the book's `last_modified` right after that write,
  and skips anything not strictly newer. A `false` written months ago can no
  longer keep un-reading a finished book.
- **Protects desktop edits.** Flipping `#read_status` in Calibre after the
  sidecar entry was written is no longer reverted by the next sync.
- Merge logic split into `sidecar_merge.py` (no Calibre imports) with 22 unit
  tests: `python3 sidecar_merge.py`.
- The sync dialog now reports skipped entries alongside updated and unmatched.

### 1.0.0

- Initial release: reads `CalibReader_sync.json` and sets `#read_status`.
