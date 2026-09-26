"""The store contract: schema, target state, validation and the published status.

The tables are created by the real migrator (``db.migrate``), which is also what
proves this module never declares DDL of its own.
"""
from __future__ import annotations

import json
from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path

from proxy_workbench import db, pools

from tests.pools_support import FakeSource, PoolTestCase

# A value that must never be treated as a credential: it is only here to prove the
# pool does not invent one. No real secret of any kind appears in this repository.
CANARY = 'canary-not-a-secret'


class SchemaTest(PoolTestCase):
    def test_the_store_uses_the_tables_of_the_contract(self):
        tables = {row[0] for row in self.store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertIn('pools', tables)
        self.assertIn('pool_member', tables)

    def test_a_store_without_the_migrated_schema_refuses_to_open(self):
        with tempfile.TemporaryDirectory() as directory:
            bare = Path(directory) / 'bare.sqlite3'
            connection = sqlite3.connect(str(bare))
            connection.execute('CREATE TABLE pools (id TEXT PRIMARY KEY)')
            connection.commit()
            connection.close()

            with self.assertRaises(pools.PoolSchemaError) as caught:
                pools.PoolStore.open(bare)

            self.assertEqual(caught.exception.code, 'E_DATA_MIGRATION_FAILED')
            self.assertIn('pool_member', str(caught.exception))
            self.assertIn('collections', str(caught.exception))

    def test_columns_the_module_uses_exist(self):
        for table, columns in pools.REQUIRED_COLUMNS.items():
            present = {row[1] for row in self.store.conn.execute(f'PRAGMA table_info({table})')}
            self.assertTrue(set(columns) <= present, f'{table} misses {set(columns) - present}')

    def test_a_refill_commits_on_a_connection_from_db_connect(self):
        # db.connect opens with isolation_level=None, where a bare `with conn` commits
        # nothing; the store has to work on that connection too, and the refill has to be
        # visible to a reader that took no part in it.
        self.store.close()
        conn = db.connect(self.path)
        self.addCleanup(conn.close)
        store = pools.PoolStore(conn)
        store.create('shared', collection_id=db.PUBLIC_COLLECTION_ID, profile_id='p',
                     profile_revision=1, desired=2, minimum=1, reserve=0)
        source = FakeSource({pools.SOURCE_SOURCES: self.candidates(2)})

        status = pools.refill(store, 'shared', source, now=self.now)

        self.assertEqual(status.served, 2)
        reader = db.connect(self.path)
        self.addCleanup(reader.close)
        rows = reader.execute(
            'SELECT endpoint_id FROM pool_member WHERE pool_id = ? ORDER BY endpoint_id',
            ('shared',)).fetchall()
        self.assertEqual([row[0] for row in rows], ['ep-01', 'ep-02'])


class TargetStateTest(PoolTestCase):
    def test_a_new_pool_is_usable_and_binds_its_scope(self):
        spec = self.create_pool(desired=5, minimum=2, reserve=1)

        self.assertEqual(spec.id, 'main')
        self.assertEqual((spec.collection_id, spec.profile_id, spec.profile_revision),
                         (db.PUBLIC_COLLECTION_ID, 'profile-1', 2))
        self.assertEqual((spec.desired, spec.minimum, spec.reserve), (5, 2, 1))
        self.assertEqual(spec.state, pools.STATE_EMPTY)
        self.assertEqual(spec.policy.cooldown_seconds, 300)

    def test_the_target_survives_a_reopen(self):
        self.create_pool(desired=4, minimum=2, reserve=3)
        self.store.close()
        self.store = pools.PoolStore.open(self.path)
        self.addCleanup(self.store.close)

        spec = self.store.get('main')
        self.assertEqual((spec.desired, spec.minimum, spec.reserve), (4, 2, 3))
        self.assertEqual(spec.policy.reserve if hasattr(spec.policy, 'reserve') else spec.reserve, 3)

    def test_the_policy_is_stored_as_readable_json(self):
        self.create_pool(desired=2, reserve=0, policy={'quota': {'exit_ip': 1}, 'countries': ['de']})
        row = self.store.conn.execute('SELECT policy_json FROM pools WHERE id = ?', ('main',)).fetchone()
        stored = json.loads(row[0])

        self.assertEqual(stored['quota'], {'exit_ip': 1})
        self.assertEqual(stored['countries'], ['DE'])

    def test_changing_the_target_keeps_the_pool_intact(self):
        self.create_pool(desired=4, reserve=1)
        source = FakeSource({pools.SOURCE_SOURCES: self.candidates(4)})
        pools.refill(self.store, 'main', source, now=self.now)

        self.store.set_target('main', desired=2, minimum=1)

        spec = self.store.get('main')
        self.assertEqual((spec.desired, spec.minimum, spec.reserve), (2, 1, 1))
        self.assertEqual(len(self.store.members('main')), 4)

    def test_a_minimum_above_desired_is_refused(self):
        with self.assertRaises(pools.PoolError) as caught:
            self.store.create('bad', collection_id=db.PUBLIC_COLLECTION_ID, profile_id='p',
                              desired=2, minimum=3)
        self.assertEqual(caught.exception.code, 'E_VALIDATION_FIELD')

    def test_a_duplicate_name_is_refused(self):
        self.create_pool('main', desired=1, reserve=0)
        with self.assertRaises(pools.PoolError) as caught:
            self.create_pool('main', desired=1, reserve=0)
        self.assertEqual(caught.exception.code, 'E_CONFLICT_IDEMPOTENCY')

    def test_an_unknown_collection_is_refused_at_creation(self):
        with self.assertRaises(pools.PoolError) as caught:
            self.store.create('ghost', collection_id='col-nope', profile_id='p', desired=1)
        self.assertEqual(caught.exception.code, pools.REASON_UNKNOWN_COLLECTION)

    def test_a_name_that_is_not_a_name_is_refused(self):
        for name in ('', 'has space', 'sl/ash', 'x' * 65):
            with self.subTest(name=name):
                with self.assertRaises(pools.PoolError) as caught:
                    self.store.create(name, collection_id=db.PUBLIC_COLLECTION_ID,
                                      profile_id='p', desired=1)
                self.assertEqual(caught.exception.code, 'E_VALIDATION_FIELD')

    def test_an_unknown_policy_field_is_refused_instead_of_ignored(self):
        with self.assertRaises(pools.PoolError) as caught:
            self.create_pool('main', desired=1, reserve=0, policy={'desired_size': 5})
        self.assertEqual(caught.exception.code, 'E_VALIDATION_UNKNOWN_FIELD')

    def test_a_quota_on_an_unknown_dimension_is_refused(self):
        with self.assertRaises(pools.PoolError) as caught:
            self.create_pool('main', desired=1, reserve=0, policy={'quota': {'hostname': 1}})
        self.assertEqual(caught.exception.code, 'E_VALIDATION_FIELD')

    def test_a_negative_limit_is_refused(self):
        with self.assertRaises(pools.PoolError) as caught:
            self.create_pool('main', desired=1, reserve=0, policy={'cooldown_seconds': -1})
        self.assertEqual(caught.exception.code, 'E_VALIDATION_FIELD')

    def test_the_default_policy_round_trips(self):
        policy = pools.Policy.from_dict(None)
        self.assertEqual(pools.Policy.from_dict(policy.to_dict()), policy)


class StatusTest(PoolTestCase):
    def test_an_empty_pool_is_not_ready_for_clients_and_says_why(self):
        self.create_pool('empty', desired=0, minimum=0, reserve=0)
        status = self.store.status('empty', now=self.now)

        self.assertEqual(status.state, pools.STATE_EMPTY)
        self.assertEqual(status.served, 0)
        self.assertFalse(status.ready_for_clients,
                         'an empty pool never becomes a direct connection')
        self.assertFalse(status.below_minimum)

    def test_below_minimum_is_reported_separately_from_the_deficit(self):
        self.create_pool('main', desired=4, minimum=3, reserve=0)
        status = pools.refill(self.store, 'main', FakeSource(), now=self.now)

        self.assertEqual(status.served, 0)
        self.assertTrue(status.below_minimum)
        self.assertFalse(status.ready_for_clients)
        self.assertEqual(status.shortfall, 4)

    def test_the_status_is_json_serializable_and_carries_the_binding(self):
        self.create_pool('main', desired=2, minimum=1, reserve=1)
        status = pools.refill(self.store, 'main',
                              FakeSource({pools.SOURCE_SOURCES: self.candidates(2)}), now=self.now)
        payload = json.dumps(status.as_dict())

        self.assertIn('"desired": 2', payload)
        self.assertIn('"profile_revision": 2', payload)
        self.assertIn('"collection_kind": "public"', payload)
        self.assertIn('"next_attempt_at"', payload)
        self.assertIn('"deficit_reasons"', payload)

    def test_the_status_written_by_a_refill_is_readable_after_a_reopen(self):
        self.create_pool('main', desired=2, reserve=0)
        pools.refill(self.store, 'main', FakeSource(), now=self.now)
        self.store.close()
        self.store = pools.PoolStore.open(self.path)
        self.addCleanup(self.store.close)

        spec = self.store.get('main')
        self.assertEqual(spec.state, pools.STATE_EMPTY)
        self.assertEqual(spec.deficit_reason, pools.REASON_NO_CANDIDATES)
        self.assertEqual(spec.next_attempt_at, self.now + 60)
        self.assertEqual(self.store.status('main', now=self.now + 1).deficit_reason,
                         pools.REASON_NO_CANDIDATES)

    def test_nothing_secret_reaches_the_database(self):
        self.create_pool('main', desired=2, reserve=0)
        source = FakeSource({pools.SOURCE_SOURCES: [
            self.candidate(1, canonical=f'http://198.51.100.1:8080/{CANARY}'),
            self.candidate(2)]})
        pools.refill(self.store, 'main', source, now=self.now)
        self.store.close()

        blob = Path(self.path).read_bytes()
        self.assertNotIn(CANARY.encode(), blob,
                         'a pool stores an endpoint id, never a URL with anything after it')
        with closing(sqlite3.connect(str(self.path))) as check:
            rows = check.execute('SELECT policy_json FROM pools').fetchall()
        self.assertNotIn(CANARY, rows[0][0])


class VocabularyTest(unittest.TestCase):
    def test_every_reason_code_follows_the_documented_shape(self):
        for code in (pools.REASON_SOURCE_ERROR, pools.REASON_NO_CANDIDATES, pools.REASON_BUDGET,
                     pools.REASON_QUOTA[0], pools.REASON_QUOTA_UNKNOWN[0], pools.REASON_DENIED,
                     pools.REASON_TIME_EXPIRED, pools.REASON_SCOPE_COLLECTION):
            with self.subTest(code=code):
                self.assertRegex(code, r'^E_[A-Z]+(_[A-Z0-9]+)+$')

    def test_the_time_and_scope_codes_are_the_ones_core_publishes(self):
        for code in (pools.REASON_TIME_UNKNOWN, pools.REASON_TIME_FUTURE, pools.REASON_TIME_EXPIRED,
                     pools.REASON_TIME_MISSING, pools.REASON_SCOPE_COLLECTION, pools.REASON_COUNTRY_FILTER):
            with self.subTest(code=code):
                self.assertIn(code, db.REASON_CODES if hasattr(db, 'REASON_CODES') else __import__(
                    'proxy_workbench.core', fromlist=['REASON_CODES']).REASON_CODES)

    def test_the_module_publishes_the_vocabulary_the_consumers_need(self):
        self.assertEqual(pools.SOURCE_ORDER, ('reserve', 'known', 'sources'))
        self.assertEqual(pools.MEMBER_STATES, ('active', 'reserve', 'probation', 'cooldown'))
        self.assertEqual(pools.POOL_STATES, ('complete', 'degraded', 'empty', 'error'))
        self.assertEqual(pools.QUOTA_DIMENSIONS, ('country', 'protocol', 'asn', 'exit_ip'))


if __name__ == '__main__':
    unittest.main()
