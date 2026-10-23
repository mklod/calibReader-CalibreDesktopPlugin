# CalibReader Sync plugin — TODO

_Last modified: 2026-10-23--1516_

- [ ] **Default sort parity with the app (user, 2026-10-23).** The desktop library sorts by a custom column "date downloaded" that calibre leaves empty on add (typed by hand), while the iOS app sorts by Date added, so "newest first" differs (desktop's newest = Roadside Picnic). Options to examine: (a) the plugin fills "date downloaded" automatically when a book is added (and offers a one-time backfill from `timestamp`); (b) the plugin writes the desktop's current sort column + direction into the `calibreader_sync` prefs so the app can default to the same order (the app side reads it, like `read_column`). Decide with the user; not before the 1.0.1 Duo items.
