# CalibReader Sync — Calibre Desktop Plugin

A small, focused [Calibre](https://calibre-ebook.com/) plugin that brings what
you do in the **CalibReader** app for iPhone and iPad back into your
desktop Calibre library. Get the app: [calibreader.com](https://calibreader.com)
(on the App Store: https://apps.apple.com/app/id6777391511).

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
| **Version** | 1.6.0 (tester preview) |
| **Author** | CalibReader |
| **Platforms** | Windows, macOS, Linux |
| **Minimum Calibre** | 9.0.0 |
| **License** | GPL v3 (see `LICENSE`) |
| **Dependencies** | None beyond Calibre itself (stdlib `json` / `os` / `datetime` / `re` / `math` / `hashlib` only) |
| **Network access** | None by default: it reads local files only. With the Kobo tester preview's "Also send CalibReader's place to the Kobo" ticked (1.5.0), requests go to your own Calibre-Web only (the address in Settings), from calibre or, when ticked, from the background job (1.6.0). |

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
- **Stamps** (1.3.0): it adds `applied_value` and `applied_at` to each entry
  it writes into Calibre, and once to each entry Calibre already agrees with,
  so the app can tell a later Calibre edit from the plugin's own write.

`CalibReader_positions.json` is written only by the Kobo import (1.4.0 tester
preview, off until you set it up). Sending CalibReader's place to the Kobo
(1.5.0) only reads it.

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

`applied_value` / `applied_at` (1.3.0) are added by the plugin to an object
entry, only while the file still holds that same entry; every other key is
kept. They mean "Calibre held `applied_value` at `applied_at`":

- an entry the plugin writes into Calibre: the value it wrote, and the book's
  Calibre `last_modified` right after the write;
- an entry Calibre already agrees with (nothing written), and the plugin's own
  write-back entries: the value, and the book's current `last_modified`. These
  are stamped once: an entry that already carries `applied_value` equal to its
  `value` is left alone, and a sync that changes nothing doesn't rewrite the
  file.

`applied_at` is rounded up to the millisecond, same format as `updated_at`. So
if the app later finds Calibre's value different from the entry's, with
`last_modified` newer than `applied_at`, the read status was edited in Calibre
afterwards. Readers ignore keys they don't know: older apps and plugins read
`value` and `updated_at` only. A legacy bare bool is never converted.

### Read status column (1.3.0)

Open **Settings** (the arrow next to the toolbar button, or Preferences →
Plugins → CalibReader Sync → Customize) and choose the **Read status column**.
It lists the library's Yes/No columns. Until you choose, it shows
`#read_status`, and **nothing else is ever pre-selected** (1.3.1): a column of
your own that is explicitly a read flag (lookup name `#read`, `#have_read`,
`#is_read`, `#finished`, KOReader Sync's `#ko_statusbool`, or a heading of
exactly "Read", "Have read", "Is read", "Read status" or "Finished") is named
underneath as a hint. Columns that only contain one of those words
(`#abandoned`, "Did not finish", `#unfinished`, `#proofread`, `#ready`…) are
never named. Clicking OK saves a read column only when you picked one (or chose
one before). **Create column…** opens Calibre's own column dialog, filled in
with `#read_status` (Yes/No); Calibre must restart before the new column can
be used.

The choice is saved in the library with the other settings (`read_column`).
The CalibReader app for iPhone and iPad reads the same column (app 1.0.1 or
later; an older app reads `#read_status` only). **The Android app reads
`#read_status` only**: with another column chosen, its marks still reach that
column through the plugin, but it does not see read status changed in Calibre.

Only a Yes/No column is used. **If the column you chose is missing** (deleted,
renamed, or created and Calibre not restarted yet) **or is not Yes/No, read
status is not synced at all** (1.3.1): it never goes into `#read_status`
instead, and the sync says why. % read and last read still sync. With no
choice saved, `#read_status` is used, as in 1.2.x.

**The first sync into a column you chose asks first** (1.3.1). Before it
writes anything into that column, the Sync Read Status button shows how many
empty cells it will fill with the app's marks (and how many filled cells it
will change); nothing is written until you click Yes. An automatic run only
leaves a status-bar note until then, and everything else keeps syncing. Your
Yes is remembered for that column in that library; a run that had nothing to
write does not count as one. If you chose a column with 1.3.0, you are asked
once, the next time the plugin has something to write into it. After
you click OK in Settings with a newly picked column, the plugin runs the
button's sync for you, so the question comes up straight away.

The plugin's per-book memory (see **Conflict resolution**) is kept per column,
so switching columns starts the new one fresh, and the old column's memory is
kept for a switch back. Switching to a column you already fill in yourself:
its empty cells take the app's marks, and its filled cells keep their values
unless the app's mark is newer than every edit of the book. When a filled cell
disagrees with an older mark, Calibre's value is kept and **nothing is passed
back to the app** (1.3.1): Calibre keeps no edit time per column, so a later
edit of the book (a rating, a tag) is no proof the cell is newer.

### Change log (1.3.1)

Calibre has no undo for these writes, so every value the plugin writes into a
library (read status, % read, last read) is recorded with the value it
replaced: one JSON line per value, with the time, book id, title, UUID,
column, old value, new value, and the stamp of the app entry that won. The log
is in Calibre's configuration folder,
`plugins/calibreader_sync_changes/<library>.log`, one file per library, and
keeps the newest 10,000 lines. The log of a library the plugin no longer
remembers (see **Conflict resolution**: the 8 most recent) is removed. Open it
from the toolbar button's menu: **Change log…**.

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
`applied_pos` for reading progress). From 1.3.1 these are kept **per
library** (under `libraries`, keyed by Calibre's library id and the library's
folder, so two copies of one library don't mix), for the 8 most recently
synced libraries. On the upgrade from 1.3.0 or 1.2.x, each library gets a
copy of the old memory, filtered to its own books, the first time it syncs;
the old keys are left in place (unused) for a downgrade. The record holds the stamp of the
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

**A bare `true` / `false` entry is applied once** (1.3.1). Such an entry (the
form the Android app writes) has no time of its own: it carries the file's
`updated_at`, which the apps move on every upload. The plugin remembers the
value it last applied for each book, and a bare entry with that same value is
never applied again, however the file's stamp moves, so a cell you clear or
change in Calibre stays as you left it. A bare entry with the other value is a
new mark.

**First contact with a filled cell** (1.3.1): the first sync of a book into a
column (a new install, or a switch to another read column) never passes
Calibre's value back to the app on the strength of the book's
`last_modified`, which any edit moves. If the app's mark is newer than the
book's `last_modified` it is applied; otherwise Calibre's value is kept, the
mark is recorded as handled, and only a later mark (or an edit of that cell in
Calibre, seen by its value from then on) decides.

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

### Kobo reading places from Calibre-Web (1.4.0, TESTER PREVIEW)

**Tester preview only, until the CalibReader app 1.0.4 is on the App Store.**
Do not publish this build. The feature is off unless you set Calibre-Web's
`app.db` path in **Settings → Kobo via Calibre-Web**. Use it only with the
CalibReader test build on every iPhone or iPad you read on: that build asks
before a Kobo place moves you back; older apps do not.

If your Kobo syncs with **Calibre-Web** (its Kobo sync), the plugin hands the
Kobo's place in each book to CalibReader:

- `app.db` is opened **read-only**; nothing is written to it or to `metadata.db`.
- Each Kobo place is mapped to CalibReader's chapter + text position (the text
  counted as the app counts it) and written into `CalibReader_positions.json`
  (`deviceId` `kobo-calibre-web`, plus a `kobo` block with the raw location
  and the precision). The file keeps its permissions; the plugin re-reads it
  just before writing, so an app upload meanwhile is kept.
- **Newer wins**, as between devices; the app 1.0.4 never moves you BACK to a
  Kobo place without asking. The Kobo's stamp is Calibre-Web's server time;
  stamps more than 10 minutes in the future are refused (wrong clock).
- **Precision.** With KEPUBs (Calibre-Web with kepubify) the place is the
  Kobo's sentence; otherwise the same chapter, within a page or two.
- Not imported: a Kobo location not found in the book (wrong library: if
  most are, nothing is imported), an unchanged location under a new stamp,
  status-only updates (0 % / 100 %), a location far from the Kobo's own %.
- With several Calibre-Web users, name whose Kobo to follow.
- At most 25 books are mapped per run; the rest follow a few seconds later.
- CalibReader to the Kobo: see the next section (1.5.0, off by default).

### CalibReader's place to the Kobo (1.5.0, TESTER PREVIEW)

**Off by default.** Tick **Also send CalibReader's place to the Kobo (tester
preview)** in **Settings → Kobo via Calibre-Web** (the `app.db` path must be
set too). Nothing is ever sent while it is unticked, and only places you read
**after ticking it** are sent (ticking it sends nothing old).

- When you have read further in CalibReader than on the Kobo, the plugin sends
  that place to **Calibre-Web**, the way a Kobo does (`PUT
  /kobo/<token>/v1/library/<book uuid>/state`); Calibre-Web hands it to the
  Kobo at the Kobo's next sync. The address is **Calibre-Web address** in
  Settings (default `http://127.0.0.1:8083`, Calibre-Web on the same
  computer). The Kobo token is read from `app.db` (still read-only) and never
  shown or logged.
- **Forward only.** A place is sent only when CalibReader's entry is newer
  than the Kobo's AND ahead of it, and then once more just before sending, in
  the Kobo's own terms: its sentence-span against ours (`kobo.P.S`, or a later
  chapter), otherwise the % the Kobo will get against the Kobo's own %. What
  cannot be compared is not sent. Never a place behind the Kobo's: the Kobo
  cannot ask before moving you back. If the Kobo syncs while calibre is
  sending, that book waits for the next sync.
- **Precision.** With the book's KEPUB (Calibre-Web with kepubify; found next
  to the EPUB even before calibre lists it): the Kobo's sentence-span the app's
  place is in. Without: the percentages only (Calibre-Web passes no chapter to
  the Kobo then).
- Only books Calibre-Web has synced to that user's Kobo; read status and
  reading statistics in Calibre-Web are left alone.
- The plugin remembers what it sent (calibre's config folder, this computer),
  so the Kobo import does not read its own push back as a Kobo move.
- Sending runs in the background: calibre never waits for Calibre-Web, and the
  status bar shows the result. Calibre-Web not running, a wrong address or a
  refused token: one note, nothing retried; automatic sends pause for 5
  minutes, the Sync Read Status button tries at once.
- **Open question:** whether a real Kobo moves to a place sent from the server.
  That is what this tester preview finds out.
- Outside calibre: `python3 kobo_import.py --library … --app-db … --user …
  --push [--calibre-web http://…] [--state file] [--since ISO-time]` (sending
  starts at the first `--push` run unless `--since` says otherwise).

### Keep syncing when calibre is closed (1.6.0, macOS, TESTER PREVIEW)

**Off by default.** Tick **Keep syncing when calibre is closed (macOS, tester
preview)** in **Settings → Kobo via Calibre-Web** and click OK. A small
background job then does exactly the Kobo work above (the Kobo's place into
`CalibReader_positions.json`, and CalibReader's place to the Kobo if that box
is ticked too) with calibre closed: every 5 minutes, and within about a minute
of Calibre-Web recording a Kobo sync (its `app.db` changing). Calibre-Web must
keep running; calibre need not.

- **What OK does.** Copies the plugin's four Kobo modules to
  `~/Library/Application Support/CalibReaderSync/service/`, writes
  `~/Library/LaunchAgents/com.calibreader.sync.kobo.<12 letters>.plist` (one
  per library) and loads it with `launchctl bootstrap gui/<your user id>`. No
  password, no admin rights. It also creates the job's log and support folders
  first. It runs calibre's own Python (`calibre-debug -e`), so nothing else
  needs installing; it refuses (with "Move calibre to Applications first …")
  when calibre runs from its disk image or from a temporary copy macOS made of
  a downloaded app. Unticking it (or clearing the app.db path) unloads the job
  and removes the plist. OK with it ticked after a plugin update refreshes the
  copy (calibre also refreshes it when it starts).
- **The same settings.** The job reads this library's Kobo settings from
  calibre's own settings file at every run, and again before every send:
  untick "Also send…" and it stops sending from the next request on. It never
  changes calibre's settings, and it never runs while that box is unticked, the
  plugin is removed from calibre, or calibre's CalibReader Sync is older than
  1.6.0 (untick before installing an older build).
- **One at a time.** The job, calibre and `kobo_import.py` share one lock per
  library: whoever finds it taken skips that run (one line says so) and never
  waits. While the job is set up and healthy (a good run in the last 15
  minutes), calibre's own automatic Kobo runs leave the Kobo to it; if it
  cannot start or keeps failing, calibre does the Kobo work itself again.
  **Sync Read Status** always does the Kobo part (under the same lock). The
  lock is per computer: a second computer's calibre on the same library is not
  kept apart from this one's job.
  They share one memory of what was sent (calibre's
  `plugins/calibreader_sync_kobo_pushed.json`), so neither reads the other's
  send back as a Kobo move.
- **Status and log.** Settings shows whether the job is on, starting or not
  running (and why: launchd's exit code, or the last run's error, which stays
  until a run succeeds), and its last run. Below it, every CalibReader
  background job on this Mac, with Remove, including jobs for libraries calibre
  no longer shows. The log is `~/Library/Logs/CalibReaderSync/kobo.log`: one
  line per run (what was imported and sent, "no change", "failed: …",
  "nothing done: …"), notes, and a book's reason for not sending only when it
  changes; rotated at 256 KB (3 old files kept); never the Kobo token; the home
  folder shown as `~`.
- After Calibre-Web failed, the job waits 5 minutes before sending again (the
  import goes on).
- macOS only for now (Windows Task Scheduler and Linux systemd timers are
  sketched in the design notes). Not yet checked on a real protected folder:
  a library or app.db in a folder macOS guards (Documents, Desktop, Downloads,
  iCloud Drive, an external drive or a network share) may need macOS's
  permission for the job. The job then says "macOS did not let the background
  job read <folder>. In System Settings > Privacy & Security > Full Disk
  Access, turn on calibre …".

### Testing

The rules live in two modules that import nothing from Calibre, each with its
own tests (the in-memory library returns local-time dates, as Calibre does):

```bash
python3 sidecar_merge.py      # 39 tests: parsing, timestamps, the conflict rules
python3 sync_core.py          # 81 tests: whole sync runs against an in-memory library
```

All tests, with a count per source:

```bash
python3 tests/run_tests.py    # both modules; the real-Calibre tests are skipped
tests/run_calibre.sh          # the same under Calibre's own python, real-Calibre tests included
# run_tests.py also runs tests/test_kobo_position.py + tests/test_kobo_import.py (1.4.0)
# and tests/test_kobo_push.py (1.5.0; a stand-in Calibre-Web on 127.0.0.1)
# and tests/test_kobo_service.py (1.6.0; the background job, its lock, log and launchd set-up)
```

`tests/test_real_calibre.py` runs against real Calibre (`LibraryDatabase`,
`set_field`, `metadata.db`, the plugin's `JSONConfig`, the Settings widget, the
button with its dialogs stubbed): the main sync path, an upgrade from 1.2.5
(its code is in `tests/fixtures/plugin_1_2_5`), Settings OK, column delete and
switch, bare-bool churn, the change log. Each test works on a throwaway copy of
a local copy of the SAFE library's `metadata.db` (`CR_SAFE_LIBRARY`, default
`~/builds/plugin13/safe`), with a throwaway Calibre config folder and
`TZ=America/Los_Angeles`. The runner exits 1 on a failure and 2 (VOID) when a
source executed nothing or, under Calibre, when the real-Calibre tests were
skipped. Note: `calibre-debug -e sync_core.py` runs **0** tests (the tests sit
in the `__main__` block, which `unittest.main()` can't find under
`calibre-debug`); use `tests/run_calibre.sh`.

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
   - `kobo_position.py`
   - `kobo_import.py`
   - `kobo_push.py`
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
**Sync now**, **Settings…** and **Change log…** (1.3.1). Either way, the plugin will:

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
| `kobo_position.py` | (1.4.0, tester preview) A Kobo place (KoboSpan / EPUB CFI / percentages) to CalibReader's chapter + text position. No Calibre imports. |
| `kobo_import.py` | (1.4.0, tester preview) Reads Calibre-Web's app.db read-only and merges Kobo places into `CalibReader_positions.json`; `python3 kobo_import.py --library … --app-db … --user …` runs one import (`--push` also sends CalibReader's places, 1.5.0). |
| `kobo_push.py` | (1.5.0, tester preview) CalibReader's place to the Kobo through Calibre-Web's Kobo API, forward only, off by default. No Calibre imports. |
| `kobo_service.py` | (1.6.0, tester preview) The Kobo sync without calibre running: the background job's run (`calibre-debug -e kobo_service.py -- run --library …`), the per-library lock shared with calibre and the CLI, and the macOS launchd set-up. No Calibre imports. |
| `plugin-import-name-calibreader_sync.txt` | Required by Calibre's loader; declares the import name `calibreader_sync`. |
| `images/icon.png` | Toolbar icon (blue circular sync arrows), loaded at runtime via `get_icons('images/icon.png')`. |
| `LICENSE` | GPL v3. |
| `make_icon.py` | Standalone script that generates `images/icon.png`. Not part of the shipped plugin. |
| `sync.png`, `sync.svg` | Reference icon source art. Not shipped. |
| `CalibReaderSync.zip` | The packaged, installable plugin (12 files at the zip root). |
| `tests/` | `run_tests.py` (all tests, counts per source, VOID on 0), `run_calibre.sh` (the same under Calibre's python), `test_real_calibre.py`, and the 1.2.5 code for the upgrade test. Not shipped. |

---

## Error handling

Every failure path shows a Calibre dialog rather than crashing:

- **Neither file found**: reminds you to let the cloud client finish syncing
  and to check that this is the library the app opens.
- **A file can't be read** (a cloud client may leave it half-written): that
  file is skipped for this run and the other one still syncs.
- **No usable read status column**: read status is skipped, and the message
  says to pick or create a Yes/No column in Settings, naming any column that
  is explicitly a read flag (`#read`, `#finished`…). A chosen column that is
  missing or not Yes/No means read status is not synced (1.3.1), with a note;
  it never falls back to `#read_status`.
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
- **The app must follow the column choice.** CalibReader for iPhone and iPad
  1.0.1 and later read `read_column`; an older app keeps reading
  `#read_status`, so update the app before choosing another column. **The
  Android app reads `#read_status` only.**
- **A column re-created with the same lookup name is not backfilled** (review
  L2). Delete `#read` and create a new `#read`: the plugin's memory says the
  app's entries were applied already, so the new column's empty cells stay
  empty until the app's next mark for each book. Nothing is lost (the app
  treats an empty cell as no information). Workaround: pick a column with a
  new lookup name.
- **Syncs run on Calibre's main thread** (review L4). Quick on a local disk; on
  a slow network or cloud-mounted library Calibre can pause for the length of
  a run.
- **"(conflicted copy)" files** (review L5). Each apply or first stamp rewrites
  `CalibReader_sync.json` (a 1.2.5 library gets one full rewrite on the first
  1.3.x sync). If the phone uploads the same file within those milliseconds,
  the cloud client keeps both versions; the app's is the one that counts, and
  the plugin stamps again on a later run. No read status is lost.
- **Automatic sync polls.** Changes from the app are checked every 5 minutes
  while Calibre is open (cloud folders don't reliably report file changes).
- **Current library only.** Operates on whatever library is open.
- **Sync bookkeeping is per installation, per library.** The applied-stamp
  records live in this Calibre install's plugin prefs, not in the library, so
  a second desktop starts from scratch (its first sync follows the rules
  above). Moving a library to another folder starts its memory afresh too.

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

### 1.6.0 — 2026-11-05 (tester preview, not published)

- **Keep syncing when calibre is closed** (macOS, off by default: Settings →
  Kobo): a launchd job runs the Kobo import and, when ticked, the send to the
  Kobo with calibre closed, every 5 minutes and when Calibre-Web's app.db
  changes, using calibre's own Python. Status line in Settings; log in
  `~/Library/Logs/CalibReaderSync/kobo.log`.
- One lock per library for the job, calibre and the command line; calibre's
  automatic Kobo runs stand down while the job is set up and healthy (the
  button still works).
- Review round 1: the log and support folders are created before launchd
  starts the job; no set-up from a disk image or a translocated calibre; a
  failed run keeps its error in the status line; one plain message for macOS
  privacy blocks; the push memory is locked across processes; the job stops
  for an older calibre plugin; Settings lists every job with Remove.
- One memory of what was sent, shared by calibre, the job and the command line
  (calibre's journal, re-read at every run; the command line's default
  `--state`), so none of them reads another's send back as a Kobo move.
- A place sent without a sentence (no KEPUB) leaves Calibre-Web no chapter
  path, so later Kobo moves by % only still come back to CalibReader.
- Tests: `tests/test_kobo_service.py`, more real-Calibre tests.

### 1.5.0 — 2026-11-05 (tester preview, not published)

- **CalibReader → Kobo** (off by default: Settings → Kobo, "Also send
  CalibReader's place to the Kobo"; the CLI's `--push`): the app's place goes
  to Calibre-Web's own Kobo API, only when newer than and ahead of the Kobo's
  place. The KoboSpan of the app's sentence when the library holds the KEPUB,
  else the chapter and %. app.db stays read-only; the token is never shown.
  The Kobo import skips the plugin's own push coming back. A Calibre-Web that
  is down or refuses: one note, no retry.
- New setting: Calibre-Web address (default `http://127.0.0.1:8083`).
- Review round 1: only places read after ticking the box; forward-only checked
  again in the Kobo's own terms just before each send; the Kobo reporting our
  place in its own terms is not imported back; a KEPUB Calibre-Web added while
  calibre runs is found; sends run off calibre's GUI thread with a time limit.
- Tests: `tests/test_kobo_push.py`; `tests/e2e/` (a Kobo simulator for a local
  Calibre-Web). The two POSIX file-mode tests are skipped on Windows.

### 1.4.0 — 2026-11-02 (tester preview, not published)

- **Kobo → CalibReader** (off unless set up): Kobo reading places from
  Calibre-Web's app.db (read-only) go into `CalibReader_positions.json`.
  KoboSpan via the KEPUB, EPUB CFI, or the chapter/book percentage. Automatic
  sync also runs when app.db changes. A Kobo failure never stops the
  read-status sync. Built on 1.3.1.

### 1.3.1 — 2026-11-02

Fixes from the 1.3.0 review (`docs/handoffs/REVIEW-plugin-1.3.md` in the
CalibReader project):

- **No read column is picked for you.** Only columns that are explicitly a
  read flag are named, and only as a hint; Settings no longer pre-selects one,
  and OK saves a read column only when you picked one. (1.3.0 matched parts of
  words, so `#abandoned`, "Did not finish" or `#incomplete` could be offered,
  and OK for another reason saved it.)
- **The first sync into a column you chose asks first**, with the number of
  empty cells it will fill and filled cells it will change.
- **A chosen column that is gone is never replaced by `#read_status`.** If it
  was deleted, renamed, or not created yet (Calibre not restarted), read
  status is not synced and the sync says why; everything else still syncs.
- **Change log**: every value written into a library is recorded with the
  value it replaced (toolbar menu: Change log…).
- **A bare `true` from the Android app is applied once**: a cell you cleared or
  changed in Calibre is no longer refilled every time the phone uploads.
- **Switching columns no longer sends Calibre's old value back to the phone**
  because of an unrelated edit (a rating) made after the phone's mark.
- The plugin's memory is kept **per library** (copies of one library no longer
  mix), for the 8 most recent libraries.
- Settings: says that the Android app reads `#read_status` only; after OK, a
  sync runs.
- Tests: the real-Calibre tests are in the repo (`tests/`), with a runner that
  refuses a run that executed nothing.
- Known, one-time on the upgrade (Android users): the "applied once" memory of
  a bare `true`/`false` is new in 1.3.1. A 1.3.0 record has none, and it can
  only be inferred when the phone has not uploaded since 1.3.0's last run. So
  on the first 1.3.1 run, a cell you cleared or changed in Calibre after 1.3.0
  applied a bare mark can be set back once (as 1.3.0 itself would do); from
  then on it holds. It is not inferred from Calibre's value because a change
  in Calibre and a new mark from the phone look the same in a 1.3.0 record.

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
- Read status entries in `CalibReader_sync.json` get `applied_value` and
  `applied_at` (once, when the plugin applies them or finds Calibre already
  agrees), so the app can tell a later Calibre edit from the plugin's own
  write. Older apps ignore the extra keys.

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
