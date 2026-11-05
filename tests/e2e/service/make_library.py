# make_library.py <library> <epub>... : a new throwaway calibre library with these EPUBs, through
# calibre's own db API (calibre-debug -e; calibredb refuses while another session's calibre-server
# runs). Not shipped.
import os
import sys

from calibre.db.legacy import LibraryDatabase
from calibre.ebooks.metadata.meta import get_metadata

library, epubs = sys.argv[1], sys.argv[2:]
os.makedirs(library, exist_ok=True)
db = LibraryDatabase(library)
try:
    for path in epubs:
        with open(path, 'rb') as f:
            mi = get_metadata(f, 'epub')
        ids, dups = db.new_api.add_books([(mi, {'EPUB': path})])
        print('added', ids, mi.title)
    api = db.new_api
    for book_id in sorted(api.all_book_ids()):
        print(book_id, api.field_for('title', book_id), api.field_for('uuid', book_id), api.formats(book_id))
finally:
    db.close()
