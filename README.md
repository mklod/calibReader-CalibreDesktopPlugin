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
| **Version** | 1.0.0 |
| **Author** | CalibReader |
| **Platforms** | Windows, macOS, Linux |
| **Minimum Calibre** | 5.0.0 |
| **Dependencies** | None beyond Calibre itself (uses only `json` + `os` from the stdlib) |
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
    "9a3b77de-....-uuid": true
  }
}
```

Books are keyed by their Calibre **UUID**, so matching survives differing book
IDs across devices/libraries. Matching is case-insensitive.

---

## Installation

### For users

1. Get `CalibReaderSync.zip` (shipped in this folder). If you need to rebuild
   it, zip these items **at the root of the archive** (not inside a subfolder):
   - `__init__.py`
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
3. Batch-set `#read_status = yes` for every matched book via
   `db.set_field('#read_status', …)` and refresh the library view.
4. Report how many books were updated, and how many UUIDs in the file did **not**
   match this library (e.g. they belong to a different library).

There is **no configuration dialog** — `is_customizable()` returns `False`. The
behaviour is fixed and dependency-free.

---

## Files in this folder

| File | Purpose |
|---|---|
| `__init__.py` | The entire plugin: `CalibReaderSyncPlugin` (the `InterfaceActionBase` wrapper) + `CalibReaderSyncAction` (the `InterfaceAction` that does the work). |
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

## Scope & limitations (v1.0.0)

- **Read-status only.** Reading *position* is synced separately by the mobile
  app via `CalibReader_positions.json` — this plugin does not touch it.
- **One-directional.** Marking a book read in *desktop* Calibre does not write
  back to the sidecar; only mobile → desktop is supported in v1.0.
- **Current library only.** Operates on whatever library is open
  (`gui.library_path`).
- **Highlights/annotations** sync is planned for a later version (see the PRD),
  not in v1.0.

---

## Roadmap

- Read-status **writeback** (desktop → mobile) so marking read in Calibre flows
  back to the app.
- Highlights & annotations sync (PRD v1.1).
