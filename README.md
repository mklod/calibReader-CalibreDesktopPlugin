# CalibReader Sync — Calibre Desktop Plugin

A small, focused [Calibre](https://calibre-ebook.com/) plugin that brings what
you do in the **CalibReader** app for iPhone and iPad back into your
desktop Calibre library. Get the app: [calibreader.com](https://calibreader.com)
(public beta on TestFlight: https://testflight.apple.com/join/PVW2VU9c).

- **Read status**: read / unread, into a Yes/No column you choose (new in
  1.3.0; `#read_status` unless you pick another). It goes both ways.
- **Reading progress**: **% read** and the **last-read date**, into columns
  you choose.

The app keeps two small files in your library folder,
`CalibReader_sync.json` and `CalibReader_positions.json`. This plugin reads
them and updates `metadata.db`.

| | |
|---|---|
| **Name** | CalibReader Sync |
| **Version** | 1.3.0 |
| **Author** | CalibReader |
| **Platforms** | Windows, macOS, Linux |
| **Minimum Calibre** | 9.0.0 |
| **License** | GPL v3 (see `LICENSE`) |
| **Dependencies** | None beyond Calibre itself (stdlib `json` / `os` / `datetime` / `re` / `math` only) |
| **Network access** | None. It reads local files only. |

---

## How the sync loop works

```
   ┌─────────────────────┐  marks read, reads     ┌───────────────────────────┐
   │  CalibReader mobile  │ ─────────────────────► │ CalibReader_sync.json      │
   │  (iPhone / iPad)     │  writes by book UUID    │ CalibReader_positions.json │
   └─────────────────────┘                          └─────────────┬─────────────┘
                                                                  │ Dropbox / Google Drive /
                                                                  │ OneDrive / network share
                                                                  ▼
   ┌─────────────────────┐  "Sync Read Status"     ┌───────────────────────────┐
   │  Calibre desktop     │ ◄───── reads files ──── │  Local library folder      │
   │  + this plugin       │  updates the columns    │  (metadata.db + json)      │
   └─────────────────────┘                          └───────────────────────────┘
```

1. **Mobile → library folder.** When you mark a book read or unread, the app
   records it in `CalibReader_sync.json`. When you leave a book, it records
   your position, including `progress` (0-1), in `CalibReader_positions.json`.
   Both files sit next to `metadata.db`.
2. **Folder → desktop.** Your cloud client (or the network share) brings those
   files to the computer that runs Calibre.
3. **Plugin → metadata.db.** The plugin syncs by itself (1.2.5) a few seconds
   after Calibre starts or a library opens, and whenever the app's files change
   (checked every 5 minutes); **Sync Read Status** syncs on demand. It sets
   your read status column to Yes or No, and writes % read and last read into
   the columns you picked in **Settings**.

The plugin never touches the internet. In Calibre it changes only the read
status column and the progress columns you chose. It writes to
`CalibReader_sync.json` in two cases:

- **A Calibre edit wins** (1.2.4): when you changed a book's read status in
  Calibre after the app's change, it records Calibre's value there, stamped
  with the time of your edit, so the app follows it on its next sync.
- **An entry was applied** (1.3.0): when it writes an entry's read status into
  Calibre, it adds `applied_value` and `applied_at` to that entry, so the app
  can tell a later Calibre edit from the plugin's own write.

`CalibReader_positions.json` is never written.

### File formats

`CalibReader_sync.json`:

```json
{
  "schema": 1,
  "updated_at": "2026-03-11T23:47:35.089553Z",
  "books": {
    "0f5c1e2a-....-uuid": true,
    "9a3b77de-....-uuid": { "value": false, "updated_at": "2026-11-02T18:31:12.482Z" },
    "5d2e0c41-....-uuid": { "value": true, "updated_at": "2026-11-02T18:31:12.482Z",
                            "applied_value": true, "applied_at": "2026-11-02T18:40:03.117Z" }
  }
}
```

`CalibReader_positions.json` (only `progress` and `updated_at` matter here):

```json
{
  "schema": 1,
  "updated_at": "2026-10-01T08:14:55Z",
  "positions": {
    "c45477b9-....-uuid": { "progress": 0.474, "updated_at": "2026-09-30T21:06:29.447Z",
                            "chapter": 21, "page": 3, "scrollY": 1399, "deviceId": "..." }
  }
}
```

Books are keyed by their Calibre **UUID**, so matching survives differing book
IDs across devices and libraries. Matching is case-insensitive.

A read-status entry is **either** a legacy bare boolean **or** an object with
its own `value` + `updated_at`. `schema` stays `1`: the two forms may mix in
one file, because a device still on an older build keeps writing bare bools.
A bare bool carries the **file-level** `updated_at`; an object carries its own.
Fractional seconds are allowed (milliseconds or microseconds).
Both apps write `progress` for EPUB and PDF (a PDF's is `(page + 1) / pages`).

`applied_value` / `applied_at` (1.3.0) are added by the plugin, only to an
object entry it has just written into Calibre, and only while the file still
holds that same entry; every other key is kept. `applied_value` is the value it
wrote; `applied_at` is the book's Calibre `last_modified` right after the write
(rounded up to the millisecond, same format as `updated_at`). So if the app
later finds Calibre's value different from the entry's, with `last_modified`
newer than `applied_at`, the read status was edited in Calibre afterwards.
Readers ignore keys they don't know: older apps and plugins read `value` and
`updated_at` only. A legacy bare bool is never converted.

### Read status column (1.3.0)

Open **Settings** (the arrow next to the toolbar button, or Preferences →
Plugins → CalibReader Sync → Customize) and choose the **Read status column**.
It lists the library's Yes/No columns. Until you choose, it offers
`#read_status` if the library has it, otherwise a Yes/No column that already
looks like a read flag (`#read`, "Have read", `#finished`, `#done`, KOReader
Sync's `#ko_statusbool`…). Nothing changes until you click OK. **Create
column…** opens Calibre's own column dialog, filled in with `#read_status`
(Yes/No); Calibre must restart before the new column can be used.

The choice is saved in the library with the other settings (`read_column`), and
the CalibReader app reads the same column (app 1.0.1 or later; an older app
reads `#read_status` only). Only a Yes/No column is used: if the chosen column
is missing or of another type, read status goes into `#read_status` instead,
as in the app, and the sync says so.

The plugin's per-book memory (see **Conflict resolution**) is kept per column,
so switching columns starts the new one fresh, and the old column's memory is
kept for a switch back. Switching to a column you already fill in yourself:
its empty cells take the app's marks, and its filled cells keep their values
unless the app's mark is newer (where they disagree with an older mark,
Calibre's value goes back to the app).

### Reading progress (1.2.0)

In **Settings**, choose:

| Setting | Column type | What it gets |
|---|---|---|
| **% read column** | Integers, or Floating point | How far you have read. An Integer column gets 0-100 (`47`). A Floating point column shown as a percentage, with a format such as `{:.0%}`, gets 0-1 (`0.474`); any other Floating point column gets 0-100 with one decimal. |
| **Last read column** | Date | When the app last saved your position in that book. |
| **Skip books under** | – | Default **1 %**. A book under this is left alone, so opening a book to look at it does not make it "in progress". |

The dialog suggests columns that already mean the same thing, such as Kobo
Utilities' "Percent Read" / "Last Read Timestamp" or KOReader Sync's
`#ko_progint`. It never suggests a "Date Read" or "Finished" column for last
read, because those mean something else. Nothing is written until you click OK.

**Create column…** opens Calibre's own column dialog, filled in with
`#read_percent` (Integers, shown as `47%`) or `#last_read` (Date). You confirm
it there. Calibre must restart before a new column can be used; until then the
sync skips it and says so.

**The settings are saved in the library** (`metadata.db` preferences, key
`calibreader_sync`), so they follow the library to every computer that opens
it:

```json
{"version": 1, "read_column": "#read_status", "percent_column": "#read_percent",
 "last_read_column": "#last_read", "progress_threshold": 1, "auto_sync": true}
```

`""` means "don't sync" (progress columns only). `read_column` (1.3.0) is a
lookup name with `#`; a missing or malformed value means `#read_status`.

The read flag and % read stay separate on purpose: 100 % is not the same as
finished (some books end with previews), and you may mark a book read at 60 %.

### Conflict resolution

The plugin keeps its own per-book record of what it last applied, in the
plugin's preferences (`plugins/calibreader_sync.json`: `applied` for read
status in `#read_status`, `applied_cols` for any other read column (1.3.0),
`applied_pos` for reading progress). The record holds the stamp of the
entry it last applied and that book's `last_modified` right after the write.
**No column is added to your library for this.** For each entry, in order:

1. **Undated entry** (legacy file with no usable stamp anywhere): applied only
   as a bootstrap, when the plugin has no record for that book yet. Once a
   record exists, an undated source never overrides it.
2. **Stale**: a stamp older than or equal to the one already applied is
   ignored. This retires an old `false` permanently.
3. **Desktop edit wins**: an entry that isn't strictly newer than a Calibre
   edit of the read flag is ignored. From 1.2.3 the plugin also remembers the
   read value it last saw for every book, and a Calibre edit of the read flag
   means that value **changed** (the edit's time is taken as the book's
   `last_modified` when the change was first seen). Other metadata edits (a
   rating, a tag) don't count. For a book with no such record yet (the first
   sync, or a record from 1.2.2 or earlier), the old rule applies once: if the
   book changed in Calibre since the last apply (or was never synced), an entry
   whose stamp isn't strictly newer than the book's `last_modified` is ignored.
4. Otherwise the entry wins and the column is set.

**An empty read status cell takes the app's mark** (1.3.0), however old the
mark is: rule 3 doesn't apply to an empty cell. Rule 2 still does, so a cell
you clear in Calibre is not refilled by the entry the plugin already applied;
the app's next mark fills it. A filled cell follows rules 1-4.

Reading progress follows the same rules, with two additions in 1.2.0:

- **An empty column takes the first value.** When the plugin has never written
  % read or last read for a book and those columns are empty, there is no
  desktop edit to protect, so rule 3 doesn't apply. Without this, a book you
  finished in September would never get its 100 % if you added the column in
  October.
- **The plugin's own writes don't count as desktop edits.** Writing % read
  bumps the same `last_modified` the read-status record compares against, and
  vice versa. The other record is moved on with it, unless the book was also
  edited in Calibre in between.

For reading progress, rule 3 still keys off `last_modified`, so **any**
metadata edit in Calibre can make a same-vintage position lose. Reading on
produces a newer stamp and syncs normally. Read status no longer has this
limitation (1.2.3, see rule 3).

### Testing

The rules live in two modules that import nothing from Calibre:

```bash
python3 sidecar_merge.py      # 38 tests: parsing, timestamps, the conflict rules
python3 sync_core.py          # 52 tests: whole sync runs against an in-memory library
```

---

## Installation

### For users

1. Get `CalibReaderSync.zip` (shipped in this folder). To rebuild it, zip these
   items **at the root of the archive** (not inside a subfolder):
   - `__init__.py`
   - `ui.py`
   - `config.py`
   - `sync_core.py`
   - `sidecar_merge.py`
   - `plugin-import-name-calibreader_sync.txt`
   - `images/icon.png`
   - `LICENSE`
2. In Calibre: **Preferences → Plugins → Load plugin from file** → choose the zip.
   Installing over an older version works without uninstalling it first.
3. **Restart Calibre.**
4. Optional: **Preferences → Toolbar** → add **Sync Read Status** to the main
   toolbar so it's one click away.

### For developers

```bash
calibre-customize --add /path/to/CalibReaderSync.zip
```

Requires **Calibre 9.0.0 or later**.

---

## One-time setup

### Read status: a Yes/No column

Read status goes into a custom **Yes/No** column. **Already track read status
in your own column** (for example `#read` or `#finished`, Yes/No)? Pick it in
**Settings** (see **Read status column** above). Otherwise create one there
with **Create column…**, or by hand:

**Calibre → Preferences → Add your own columns → (+) Add custom column**

| Field | Value |
|---|---|
| Lookup name | `read_status` |
| Column heading | `Read Status` |
| Column type | `Yes/No` |

(The lookup name `read_status` becomes `#read_status` internally.) Restart
Calibre after creating a column. If there is no usable column, the sync says so
and leaves read status alone; reading progress still syncs.

### Reading progress

Choose the columns in **Settings** (see **Reading progress** above). Without
them, only read status syncs, and the summary reminds you where to set them up.

Also make sure:

- Your Calibre library folder is the **same folder** your cloud client syncs
  and the **same library** the app reads.
- Your cloud client has finished syncing, so the two files are present.

---

## Using it

**Automatic sync (1.2.5, on by default):** a few seconds after Calibre starts,
after you open a library, and whenever `CalibReader_sync.json` or
`CalibReader_positions.json` changes (checked every 5 minutes). An automatic
run never pops anything up: it leaves one short note in Calibre's status bar
("CalibReader Sync: updated 2 read status.") and says nothing when nothing
changed. A library with neither file is left alone. Turn it off in **Settings**
(**Sync automatically**); the switch is saved with the library.

**On demand:** click **Sync Read Status**. Its arrow opens a menu with
**Sync now** and **Settings…**. Either way, the plugin will:

1. Look for `CalibReader_sync.json` and `CalibReader_positions.json` in the
   current library folder (`gui.library_path`).
2. Parse them and match books by UUID.
3. Decide, per book, whether each entry is newer than what this library
   already has (see **Conflict resolution** above).
4. Batch-set the columns with `db.set_field(…)` and refresh the library view.
5. Report how many books were updated, how many entries were left alone, how
   many books in the files are **not** in this library (they may belong to
   another library), and anything that needs your attention.

---

## Files in this folder

| File | Purpose |
|---|---|
| `__init__.py` | The Calibre-facing wrapper, `CalibReaderSyncPlugin` (`InterfaceActionBase`): name, version, and the settings. It imports no GUI code, so non-GUI Calibre processes don't load Qt because of this plugin. |
| `ui.py` | `CalibReaderSyncAction`, the toolbar button, its menu and the summary dialog. Calibre imports it only in its main window. |
| `config.py` | The settings dialog: column pickers with suggestions, **Create column…** through Calibre's API, the threshold. Saves to the library's preferences. |
| `sync_core.py` | One sync run (`run_sync`), the settings schema and the column suggestions. No Calibre imports; `python3 sync_core.py` runs its tests. |
| `sidecar_merge.py` | Parsing both files, timestamps, the per-book conflict rules and the bookkeeping. No Calibre imports; `python3 sidecar_merge.py` runs its tests. |
| `plugin-import-name-calibreader_sync.txt` | Required by Calibre's loader; declares the import name `calibreader_sync`. |
| `images/icon.png` | Toolbar icon (blue circular sync arrows), loaded at runtime via `get_icons('images/icon.png')`. |
| `LICENSE` | GPL v3. |
| `make_icon.py` | Standalone script that generates `images/icon.png`. Not part of the shipped plugin. |
| `sync.png`, `sync.svg` | Reference icon source art. Not shipped. |
| `CalibReaderSync.zip` | The packaged, installable plugin. |

---

## Error handling

Every failure path shows a Calibre dialog rather than crashing:

- **Neither file found**: reminds you to let the cloud client finish syncing
  and to check that this is the library the app opens.
- **A file can't be read** (a cloud client may leave it half-written): that
  file is skipped for this run and the other one still syncs.
- **No usable read status column**: read status is skipped, and the message
  says to pick or create a Yes/No column in Settings, naming any column that
  already looks like a read flag (`#read`, `#finished`…). A chosen column that
  is missing or not Yes/No falls back to `#read_status`, with a note.
- **A chosen progress column is missing or the wrong type**: skipped, with a
  note (usually "restart calibre" after creating it).
- **Books not in this library**: informational only.

---

## Scope & limitations (1.3.x)

- **Read status goes both ways; progress goes one way.** The app reads the
  read status column from your library when it syncs. When both sides changed
  a book, the newer change wins, and the plugin passes a newer Calibre change
  back through `CalibReader_sync.json` (1.2.4). % read and last read go from
  the app to Calibre only.
- **The app must follow the column choice.** CalibReader 1.0.1 and later read
  `read_column`; an older app keeps reading `#read_status`, so update the app
  before choosing another column.
- **Automatic sync polls.** Changes from the app are checked every 5 minutes
  while Calibre is open (cloud folders don't reliably report file changes).
- **Current library only.** Operates on whatever library is open.
- **Sync bookkeeping is per installation.** The applied-stamp records live in
  this Calibre install's plugin prefs, not in the library, so a second desktop
  starts from scratch (its first sync follows the rules above).

---

## Roadmap

Decided 2026-10-12 (`docs/PLUGIN_READ_STATUS_DISCUSSION.md` in the CalibReader
project):

- A "Read" tag as an alternative to a Yes/No column.
- Highlights & annotations, later.

Done: two-way read status (1.2.4), automatic sync (1.2.5), your own read
column (1.3.0).

---

## Changelog

### 1.3.0 — 2026-10-22

- Pick your own read status column: Settings lists the library's Yes/No
  columns (`#read`, `#finished`…) and has Create column…; the choice is
  saved in the library, and the CalibReader app (1.0.1 or later) follows it.
  A missing or non-Yes/No choice falls back to `#read_status`, with a note.
  This replaces the 1.2.2 "copy your column over" steps.
- An empty read status cell takes the app's mark, however old; a filled cell
  keeps its value unless the app's mark is newer. The per-book memory is kept
  per column, so switching to your own column fills its empty cells and keeps
  the rest.
- Each read status entry the plugin applies gets `applied_value` and
  `applied_at` in `CalibReader_sync.json`, so the app can tell a later Calibre
  edit from the plugin's own write. Older apps ignore the extra keys.

### 1.2.5 — 2026-10-21

- **Automatic sync.** The plugin syncs by itself a few seconds after Calibre
  starts or a library opens, and when the app's files in the library folder
  change (checked every 5 minutes). It leaves a short note in the status bar
  and never pops up a dialog; the button still syncs on demand. On by default;
  **Settings → Sync automatically** turns it off for a library.

### 1.2.4 — 2026-10-21

- **Read status stays in step both ways.** If you change a book's read status
  in Calibre after the plugin applied a change from the app, but before the app
  synced again, the two could disagree for good. The plugin now writes
  Calibre's value into `CalibReader_sync.json`, stamped with the time of your
  edit, and the app follows it. It uses the apps' own safe-write rules: it
  writes only when the file reads cleanly, changes only those books' entries,
  never removes any, and swaps the file in atomically.
- A book's `last_modified` in the future (a computer clock running ahead) is
  treated as now.

### 1.2.3 — 2026-10-21

- **Read status: an unrelated Calibre edit no longer drops a phone change.**
  Calibre updates a book's `last_modified` on every metadata edit (a rating,
  a tag, a cover), and the plugin used to treat any such edit as a change of the
  read flag, so a newer read mark from the app was skipped. The plugin now
  remembers the read value it last saw for each book, and only a change of
  that value counts as a Calibre edit of the read flag. Newer still wins in both
  directions. The sync files are unchanged.
- Wording: "an enumeration column".

### 1.2.2 — 2026-10-21

- If the library has no `#read_status` but already has a Yes/No column that
  looks like a read flag (`#read`, `#finished`, KOReader Sync's
  `#ko_statusbool`…), the sync summary and Settings name it and explain how to
  copy it into `#read_status` once with Calibre's bulk edit. Nothing is
  written to your column.

### 1.2.1 — 2026-10-21

- The toolbar action moved into `ui.py` (Calibre's two-class plugin pattern).
  Calibre's command-line tools and server (`calibredb`, `calibre-server`,
  `ebook-convert`) no longer import Qt because this plugin is installed.
  No change to what a sync does.
- Tested on Calibre 9.16.

### 1.2.0 — 2026-10-12

- **Reading progress.** % read and last read go into columns you choose in the
  new **Settings** dialog, from `CalibReader_positions.json`. Integer and
  Floating point % columns are both supported. Books under a threshold
  (default 1 %) are skipped. One way only.
- **Settings live in the library** (`metadata.db` preferences), so they follow
  it to other computers. **Create column…** uses Calibre's own column dialog,
  with your confirmation.
- The toolbar button gets a menu: **Sync now** and **Settings…**.
- `#read_status` must now be Yes/No; a wrong-type column is refused instead of
  written. A missing one no longer stops reading progress from syncing.
- Fixed: the bookkeeping stores `last_modified` to the millisecond while
  Calibre reports microseconds, so an untouched book could look edited in
  Calibre. A late-arriving entry older than the plugin's own last write was
  then skipped.
- Fixed: installing over 1.1.x in a running Calibre failed ("cannot import
  name"), because Calibre reloads only the plugin's top module.
- Split into `sync_core.py` (one run, 20 tests) and `config.py`; 32 tests in
  `sidecar_merge.py`.

### 1.1.1 — 2026-10-09

- GPL v3 licence (`LICENSE` in the zip); minimum Calibre raised to 9.0.

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
