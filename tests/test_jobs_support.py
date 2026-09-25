"""Shared fixtures for the `jobs.py` tests.

Everything here is local: a temporary SQLite file, an injected clock and a
`measure()` helper that reproduces the one way a worker is allowed to finish an
item (record the observation, then judge it).  No network, no services.
"""
from __future__ import annotations

from pathlib import Path
import secrets
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import jobs  # noqa: E402

try:  # the schema of these four tables is migration 6, shared with db.py
    from proxy_workbench import db
except ImportError:  # pragma: no cover - only while db.py is not in the tree
    db = None

START = 1_700_000_000.0


class FakeClock:
    """A clock the tests move by hand, so deadlines and sleep are exact."""

    def __init__(self, now: float = START):
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += float(seconds)
        return self.now


class JobFixture(unittest.TestCase):
    """A store on a temporary database file, as a worker would open it.

    The schema comes from `db.migrate()` when the migrator is in the tree, so
    these tests run against the real migration 6 and not against a private
    copy of it (HANDOFF §2.2).  `jobs.install_schema` is the fallback and the
    second source of the same four tables.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'workbench.sqlite3'
        self.clock = FakeClock()
        if db is not None and hasattr(db, 'migrate'):
            db.migrate(self.path)
            self.migrated_by = 'db.migrate'
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.execute('PRAGMA journal_mode=WAL')
        self.conn.execute('PRAGMA foreign_keys=ON')
        if not hasattr(self, 'migrated_by'):
            jobs.install_schema(self.conn)
            self.migrated_by = 'jobs.install_schema'
        self.store = jobs.JobStore(self.conn, clock=self.clock)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    # -- builders ---------------------------------------------------------

    def scope(self, *, collection_id='col-public', profile_id='prof-1', profile_revision=1,
              **budgets) -> jobs.Scope:
        return jobs.Scope(collection_id, profile_id, profile_revision,
                          profile_digest='d' * 20, filters={'protocol': 'http'},
                          budgets=({'timeout_s': 300.0} | budgets))

    def items(self, *endpoint_ids) -> list[jobs.QueueItem]:
        return [jobs.QueueItem(endpoint_id) for endpoint_id in endpoint_ids]

    def submit(self, *endpoint_ids, kind='scan', scope=None, items=None,
               idempotency_key=None, **scope_kwargs) -> jobs.Job:
        return self.store.submit(kind, scope or self.scope(**scope_kwargs),
                                 self.items(*endpoint_ids) if items is None else items,
                                 idempotency_key=idempotency_key)

    def measure(self, job_id: str, item_id: str, *, state='done', verdict=None,
                error_code=None, observation_id=None) -> jobs.JobItem:
        """Finish one item the way a worker must: observation first, verdict second."""
        observation_id = observation_id or f'obs-{item_id}'
        self.store.record_observation(job_id, item_id, observation_id)
        return self.store.finish_item(job_id, item_id, state, observation_id=observation_id,
                                      error_code=error_code,
                                      verdict={'reliability': 1.0} if verdict is None else verdict)

    def work(self, job_id: str, outcomes: dict, *, verdict=None) -> list[jobs.JobItem]:
        """Claim and finish items in queue order, one measured event per item."""
        done = []
        for item_id, state in outcomes.items():
            claimed = self.store.claim(job_id, item_id=item_id)
            self.assertIsNotNone(claimed, f'{item_id} was not claimable')
            done.append(self.measure(job_id, item_id, state=state, verdict=verdict))
        return done

    # -- introspection ----------------------------------------------------

    def db_files(self):
        return [candidate for candidate in self.path.parent.iterdir() if candidate.is_file()]

    def db_bytes(self) -> bytes:
        """Everything the database left on disk, WAL included."""
        self.conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        return b''.join(path.read_bytes() for path in self.db_files())

    def canary(self) -> str:
        """A secret generated per test: it exists only in memory while the test runs."""
        return 'canary-' + secrets.token_hex(8)


if __name__ == '__main__':
    unittest.main()
