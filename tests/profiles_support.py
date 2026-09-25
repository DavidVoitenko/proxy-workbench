"""Shared fixtures for the `profiles.py` tests.

Everything is local: a temporary SQLite file opened through `db.migrate()` (the
owner of the schema, §3.3 migration 9), fixed timestamps instead of a clock, and
target identifiers that name no real service.  No network, no public proxy, no
credentials anywhere.
"""
from __future__ import annotations

from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import db  # noqa: E402
from proxy_workbench import profiles  # noqa: E402

#: Fixed moment for every store write, so the tests never depend on the clock.
START = 1_700_000_000.0

#: A fake target, the way a catalogue entry would be referenced by a profile.
BASIC = 'http-basic'
SEARCH = 'http-search'
VIDEO = 'video-optional'
CHAT = 'chat-optional'


def target(target_id, kind='required', *, enabled=True, min_success=1.0, max_latency_ms=None):
    """One target rule as a plain mapping, the shape every surface sends."""
    return {'id': target_id, 'kind': kind, 'enabled': enabled,
            'min_success': min_success, 'max_latency_ms': max_latency_ms}


def spec(*, optional_rule='none', k=None, attempts=1, budget=None, targets=None,
         description=''):
    """A validated specification built from the mapping form."""
    if targets is None:
        targets = [target(BASIC), target(SEARCH)]
    return profiles.ProfileSpec.create(targets=targets, optional_rule=optional_rule, k=k,
                                       attempts=attempts, budget=budget, description=description)


def ok(spec_, target_id, *, attempts=1, latency_ms=None):
    """Evidence of a target that worked on every attempt."""
    return spec_.evidence(target_id, ok=attempts, attempts=attempts, latency_ms=latency_ms)


def bad(spec_, target_id, *, attempts=1, latency_ms=None):
    """Evidence of a target that failed on every attempt."""
    return spec_.evidence(target_id, ok=0, attempts=attempts, latency_ms=latency_ms)


def mixed(spec_, target_id, ok_count, attempts, *, latency_ms=None):
    """Evidence of a target that worked on part of its attempts."""
    return spec_.evidence(target_id, ok=ok_count, attempts=attempts, latency_ms=latency_ms)


def skipped(spec_, target_id):
    """Evidence of a target that was never measured: fail-fast or the budget cut it."""
    return spec_.evidence(target_id, ok=0, attempts=0)


def unknown(spec_, target_id):
    """Evidence of a target whose measurement is unknown, not failed."""
    return spec_.evidence(target_id, ok=0, attempts=0, unknown=True)


class StoreFixture(unittest.TestCase):
    """A store on a temporary database, opened the way the CLI would open it."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'workbench.sqlite3'
        db.migrate(self.path)
        self.conn = db.connect(self.path)
        self.store = profiles.ProfileStore(self.conn)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def rows(self, table='profiles'):
        return self.conn.execute(f'SELECT * FROM {table}').fetchall()

    def row(self, ref):
        return self.conn.execute('SELECT * FROM profiles WHERE id = ?', (ref.row_id,)).fetchone()


def legacy_database(path, *, profile_id='profile0001', config=None):
    """A database written by the unversioned `proxytool.open_db`, with real rows."""
    from proxy_workbench import proxytool

    conn = proxytool.open_db(Path(path))
    conn.execute('INSERT INTO profiles VALUES (?, ?)',
                 (profile_id, config if config is not None else LEGACY_CONFIG))
    conn.commit()
    conn.close()
    return Path(path)


#: A config as the pre-migration code wrote it: content-addressed id, no name,
#: one global ``min_success`` in ``fail_fast`` and full target definitions.
LEGACY_CONFIG = (
    '{"version": 2, "attempts": 2, "timeout": 10.0, "max_bytes": 4096,'
    ' "targets": [{"url": "http://service.invalid/", "method": "GET", "statuses": [200],'
    '   "headers": {}, "contains": null, "sha256": null},'
    '  {"url": "http://second.invalid/", "method": "GET", "statuses": [200],'
    '   "headers": {}, "contains": null, "sha256": null}],'
    ' "fail_fast": {"min_success": 0.6666666666666666}}'
)


def sqlite3_connection(path):
    """A plain connection without `db`'s settings, for the negative cases."""
    return sqlite3.connect(path)
