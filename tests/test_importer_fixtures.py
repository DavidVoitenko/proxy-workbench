"""Shared setup for the importer tests.

The schema is the one `db.migrate()` creates -- the importer writes no DDL of
its own, and its tests prove behaviour against the real migrator rather than
against a private copy of the contract (HANDOFF/README.ru.md §2.2).
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from proxy_workbench import db
from proxy_workbench import importer as imp

#: the four tables this module reads and writes (CONTRACTS.ru.md §3.3, 1, 2, 12)
TABLES = ('endpoints', 'collections', 'membership', 'import_batch')


class Schema:
    """A migrated database in a temporary folder, closed and removed on cleanup."""

    def __init__(self, folder=None):
        self._own_folder = folder is None
        self._folder = folder or tempfile.TemporaryDirectory()
        self.path = Path(self._folder.name) / 'workbench.sqlite3'
        self.report = db.migrate(self.path)
        self.conn = db.connect(self.path)
        db.assert_current(self.conn)

    def close(self):
        self.conn.close()
        if self._own_folder:
            self._folder.cleanup()


def dump(conn):
    """Everything the importer can see, as comparable text."""
    data = {}
    for table in TABLES:
        data[table] = [tuple(row) for row in conn.execute(f'SELECT * FROM {table} ORDER BY 1')]
    return data


def members(conn, collection_id):
    return sorted(row[0] for row in conn.execute(
        'SELECT e.canonical FROM membership m JOIN endpoints e ON e.id = m.endpoint_id '
        'WHERE m.collection_id = ?', (collection_id,)))


def source(text, name='list.txt', channel='clipboard'):
    return imp.ImportSource.from_text(text, name=name, channel=channel)


def texts_of(conn):
    """Every stored string of every table, for "the secret is nowhere" checks."""
    chunks = []
    for table in TABLES:
        for row in conn.execute(f'SELECT * FROM {table}'):
            chunks.extend(str(value) for value in row)
    return '\n'.join(chunks)
