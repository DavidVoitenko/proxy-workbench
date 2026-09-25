"""Shared fixtures for the pools tests.

The pool tables belong to ``db.py`` (CONTRACTS §3.3 migration 7), and the
contract says a module reaches its schema through ``db.migrate()`` instead of
declaring DDL, so every test here migrates a temporary database with the real
migrator and works with what it created.

Nothing in this file talks to the network, and no secret of any kind is
involved: the addresses come from the documentation ranges (RFC 5737) and
RFC 6598, and they are only ever stored, never dialled.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from proxy_workbench import db, pools


class FakeSource:
    """A local, deterministic stand-in for the candidate tiers of a pool.

    ``tiers`` maps a tier name to the candidates it offers. Every call is
    recorded, so a test can assert which tiers the pool asked for and with
    what budget. With ``error`` the source raises; ``error_after`` lets the
    first N calls succeed first.
    """

    def __init__(self, tiers=None, *, error: Exception | None = None, error_after: int | None = None):
        self.tiers = {kind: list(items) for kind, items in (tiers or {}).items()}
        self.error = error
        self.error_after = error_after
        self.calls = []

    def __call__(self, spec, kind, budget, now):
        self.calls.append((kind, budget, now))
        if self.error is not None and (self.error_after is None or len(self.calls) > self.error_after):
            raise self.error
        return list(self.tiers.get(kind, ())[:max(0, int(budget))])

    @property
    def asked(self) -> list:
        return [kind for kind, _budget, _now in self.calls]


class ManualClock(pools.Clock):
    """A clock a test drives by hand, so `watch` never sleeps."""

    def __init__(self, start: float = 1_000.0, step: float = 0.0):
        self.value = float(start)
        self.step = float(step)
        self.slept = []

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.value += max(seconds, self.step)


class PoolTestCase(unittest.TestCase):
    """Base case: a migrated temporary database with one public collection."""

    collection_id = db.PUBLIC_COLLECTION_ID
    collection_kind = 'public'
    now = 1_000_000.0

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.path = Path(self._directory.name) / db.DB_FILENAME
        self.store = pools.PoolStore.open(self.path, migrate=db.migrate)
        self.addCleanup(self.store.close)
        self.collection_kind = self.store.collection_kind(self.collection_id)

    def add_collection(self, collection_id: str, kind: str) -> None:
        with pools.transaction(self.store.conn):
            db.create_collection(self.store.conn, collection_id, kind=kind, collection_id=collection_id,
                                 now=self.now)

    def candidate(self, index: int, **overrides) -> pools.Candidate:
        """One local candidate. The address is documentation space; nothing is dialled."""
        values = {
            'endpoint_id': f'ep-{index:02d}',
            'canonical': f'http://198.51.100.{index}:8080',
            'collection_id': self.collection_id,
            'origin_domain': pools.DOMAIN_PUBLIC,
            'protocol': 'http',
            'country': 'DE',
            'asn': 64500 + index,
            'exit_ip': f'203.0.113.{index}',
            'checked_at': self.now - 60,
            'valid_until': self.now + 3600,
            'allowed': True,
            'score': 100.0 - index,
        }
        values.update(overrides)
        return pools.Candidate(**values)

    def candidates(self, count: int, *, start: int = 1, **overrides) -> list:
        """``count`` distinct candidates, numbered from ``start`` so tiers stay disjoint."""
        return [self.candidate(index, **overrides) for index in range(start, start + count)]

    def create_pool(self, pool_id: str = 'main', *, collection_id=None, desired=5, minimum=0, reserve=2,
                    policy=None) -> pools.PoolSpec:
        values = {'max_age_seconds': 7200, 'cooldown_seconds': 300,
                  'retry_interval_seconds': 60, 'interval_seconds': 300, 'refill_budget': 20}
        values.update(policy or {})
        return self.store.create(pool_id, collection_id=collection_id or self.collection_id,
                                 profile_id='profile-1', profile_revision=2,
                                 desired=desired, minimum=minimum, reserve=reserve, policy=values)

    def states(self, pool_id: str = 'main') -> dict:
        counts = {phase: 0 for phase in pools.MEMBER_STATES}
        for member in self.store.members(pool_id):
            counts[member.state] += 1
        return counts

    def member_states(self, pool_id: str = 'main') -> dict:
        return {member.endpoint_id: member.state for member in self.store.members(pool_id)}
