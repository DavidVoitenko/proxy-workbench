"""Collection binding and persistence for user sources (F27).

The three acceptance scenarios of F27 live here, on a real SQLite database
built from the schema this module asks ``db.migrate()`` for:
a failed update does not clear a working collection, a credential change
invalidates the admissions it must, and an overlap never deletes a membership
belonging to another source.
"""
import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import sourcedesk as sd

A = 'http://198.51.100.7:8080'
B = 'socks5://198.51.100.11:1080'
C = 'http://198.51.100.21:3128'
SHARED = 'http://198.51.100.33:8080'
T0 = 1_700_000_000.0

CANARY = 'CANARY-9a41d7e0b3c2'


def put(name, value):
    return sd.make_ref('href', 'store', name)


def feed_document(*endpoints):
    """A Clash document that lists exactly these canonical endpoints."""
    entries = []
    for index, value in enumerate(endpoints):
        scheme, _, authority = value.partition('://')
        host, _, port = authority.rpartition(':')
        entries.append({'name': f'n{index}', 'type': 'http' if scheme == 'http' else 'socks5',
                        'server': host, 'port': int(port)})
    return json.dumps({'proxies': entries})


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        # The schema comes only from REQUESTED_DDL: if the module's DML and
        # the schema it asks for ever disagree, this fails here.
        self.db = sqlite3.connect(':memory:')
        for statement in sd.REQUESTED_DDL:
            self.db.execute(statement)
        self.desk = sd.SourceDesk(self.db)
        self.source = sd.user_source(binding_id='b1', url='https://sub.invalid/v1/list',
                                     source_format='clash')

    def tearDown(self):
        self.db.close()

    def bind(self, collection_id='c1', mode='replace', url='https://sub.invalid/v1/list'):
        source = sd.user_source(binding_id='b1', url=url, source_format='clash')
        self.desk.bind(source, collection_id=collection_id, mode=mode)
        return source

    def rows(self, table):
        return [str(row) for row in self.db.execute(f'SELECT * FROM {table}')]


class BindingTests(StoreTestCase):
    def test_bind_persists_a_readable_binding(self):
        bound = self.bind('c1', mode='merge')
        state = self.desk.get(bound.id, 'c1')
        self.assertEqual(state.source_id, self.source.id)
        self.assertEqual(state.collection_id, 'c1')
        self.assertEqual(state.mode, 'merge')
        row = self.desk.feed_row(self.source.id, 'c1')
        self.assertEqual(row['source_format'], 'clash')
        self.assertEqual(row['public_url'], 'https://sub.invalid/v1/list')

    def test_listing_is_scoped_by_collection(self):
        self.bind('c1')
        self.bind('c2', url='https://other.invalid/list')
        self.assertEqual(len(self.desk.list_feeds()), 2)
        self.assertEqual(len(self.desk.list_feeds(collection_id='c2')), 1)
        self.assertIsNone(self.desk.get(self.source.id, 'c3'))

    def test_invalid_bindings_are_refused(self):
        for kwargs in (dict(collection_id=''), dict(collection_id='c1', mode='upsert')):
            with self.subTest(kwargs=kwargs), self.assertRaises(sd.SourceDeskError):
                self.desk.bind(self.source, **kwargs)
        with self.assertRaises(sd.SourceDeskError):
            self.desk.bind({'id': 'x'}, collection_id='c1')
        with self.assertRaises(sd.SourceDeskError):
            sd.SourceDesk('not a connection')

    def test_corrupt_json_column_is_reported_not_ignored(self):
        self.bind('c1')
        self.db.execute("UPDATE source_feed SET active_json='{'")
        with self.assertRaises(sd.SourceDeskError) as caught:
            self.desk.get(self.source.id, 'c1')
        self.assertEqual(caught.exception.code, 'E_DATA_MIGRATION_FAILED')


class AcceptanceFailedUpdateTests(StoreTestCase):
    """A failed update must not empty a collection that is working."""

    def setUp(self):
        super().setUp()
        self.source = self.bind('c1', mode='replace')
        self.desk.apply_import(sd.ImportResult(outcome='ok', endpoints=(
            sd.ImportedEndpoint(A), sd.ImportedEndpoint(B))), source_id=self.source.id,
            collection_id='c1', mode='replace', now=T0)
        self.before = self.desk.get(self.source.id, 'c1')

    def test_the_working_set_is_there_to_begin_with(self):
        self.assertEqual(self.desk.membership(self.source.id, 'c1'), (A, B))
        self.assertEqual(self.before.last_good, (A, B))
        self.assertIsNotNone(self.before.expires_at)

    def test_a_broken_document_keeps_the_collection(self):
        plan = self.desk.apply_import(
            sd.ImportResult(outcome='invalid', rejected=(('E_IMPORT_FORMAT', 'html page'),)),
            source_id=self.source.id, collection_id='c1', mode='replace', now=T0 + 10)
        self.assertTrue(plan.blocked)
        self.assertEqual(self.desk.membership(self.source.id, 'c1'), (A, B))
        self.assertEqual(self.desk.get(self.source.id, 'c1').last_good, (A, B))
        self.assertEqual(self.desk.get(self.source.id, 'c1').expires_at, self.before.expires_at)

    def test_an_empty_document_keeps_the_collection(self):
        plan = self.desk.apply_import(sd.ImportResult(outcome='empty'), source_id=self.source.id,
                                      collection_id='c1', mode='replace', now=T0 + 10)
        self.assertEqual(plan.removed, ())
        self.assertEqual(self.desk.membership(self.source.id, 'c1'), (A, B))

    def test_an_oversized_document_keeps_the_collection(self):
        plan = self.desk.apply_import(
            sd.ImportResult(outcome='limit_exceeded', rejected=(('E_LIMIT_BODY', 'too big'),)),
            source_id=self.source.id, collection_id='c1', mode='replace', now=T0 + 10)
        self.assertEqual(plan.reason_code, 'limit_exceeded')
        self.assertEqual(self.desk.membership(self.source.id, 'c1'), (A, B))

    def test_rate_limiting_keeps_the_collection_and_records_the_wait(self):
        plan = sd.plan_refresh(self.desk.get(self.source.id, 'c1'),
                               sd.FeedResult(outcome='rate_limited', fetched_at=T0 + 10,
                                             retry_after=T0 + 900, error='429'),
                               policy=sd.FeedPolicy(mode='replace'), now=T0 + 10)
        self.desk.apply_plan(plan, now=T0 + 10)
        self.assertEqual(self.desk.membership(self.source.id, 'c1'), (A, B))
        self.assertEqual(self.desk.get(self.source.id, 'c1').next_attempt_at, T0 + 900)

    def test_a_recovery_promotes_the_new_generation(self):
        self.desk.apply_import(sd.ImportResult(outcome='ok', endpoints=(sd.ImportedEndpoint(A),
                                                                        sd.ImportedEndpoint(C))),
                               source_id=self.source.id, collection_id='c1', mode='replace', now=T0 + 20)
        self.assertEqual(self.desk.membership(self.source.id, 'c1'), tuple(sorted((A, C))))
        self.assertEqual(self.desk.get(self.source.id, 'c1').last_good, tuple(sorted((A, C))))


class AcceptanceOverlapTests(StoreTestCase):
    """An overlap must not delete a membership owned by another source."""

    def setUp(self):
        super().setUp()
        self.mine = self.bind('c1', mode='replace')
        self.other = sd.user_source(binding_id='b2', url='https://other.invalid/list',
                                    source_format='clash')
        self.desk.bind(self.other, collection_id='c1', mode='merge')
        self.desk.apply_import(
            sd.ImportResult(outcome='ok', endpoints=(sd.ImportedEndpoint(A), sd.ImportedEndpoint(B))),
            source_id=self.mine.id, collection_id='c1', mode='replace', now=T0)
        self.desk.add_membership('c1', self.other.id, (B, SHARED), now=T0)

    def test_both_sources_see_the_shared_endpoint(self):
        self.assertEqual(self.desk.contributors(B, 'c1'), tuple(sorted((self.mine.id, self.other.id))))

    def test_a_replacing_refresh_keeps_the_other_sources_membership(self):
        plan = self.desk.apply_import(
            sd.ImportResult(outcome='ok', endpoints=(sd.ImportedEndpoint(A), sd.ImportedEndpoint(C))),
            source_id=self.mine.id, collection_id='c1', mode='replace', now=T0 + 10)
        self.assertEqual(plan.removed, (B,))
        self.assertEqual(plan.retained_shared, (B,))
        self.assertNotIn(self.mine.id, self.desk.contributors(B, 'c1'))
        self.assertEqual(self.desk.contributors(B, 'c1'), (self.other.id,))
        self.assertEqual(self.desk.membership(self.other.id, 'c1'), tuple(sorted((B, SHARED))))

    def test_removing_the_other_source_keeps_our_row(self):
        self.db.execute('DELETE FROM membership_source WHERE source_id=?', (self.other.id,))
        self.assertEqual(self.desk.membership(self.mine.id, 'c1'), (A, B))

    def test_a_merge_refresh_never_removes(self):
        self.desk.apply_import(
            sd.ImportResult(outcome='ok', endpoints=(sd.ImportedEndpoint(C),)),
            source_id=self.mine.id, collection_id='c1', mode='merge', now=T0 + 10)
        self.assertEqual(self.desk.membership(self.mine.id, 'c1'), tuple(sorted((A, B, C))))
        self.assertEqual(self.desk.membership(self.other.id, 'c1'), tuple(sorted((B, SHARED))))

    def test_foreign_membership_query_ignores_the_source_itself(self):
        self.assertEqual(self.desk.foreign_membership(self.mine.id, 'c1', (A, B, SHARED)),
                         frozenset({B, SHARED}))
        self.assertEqual(self.desk.foreign_membership(self.mine.id, 'c1', ()), frozenset())


class AcceptanceRotationTests(StoreTestCase):
    """A credential change must invalidate the admissions it made obsolete."""

    def setUp(self):
        super().setUp()
        self.access_ref = sd.make_ref('auth', 'b1', 'token-v1')
        self.source = sd.user_source(binding_id='b1', url='https://sub.invalid/v1/list',
                                     source_format='clash', access_ref=self.access_ref,
                                     access_id='access-1')
        self.desk.bind(self.source, collection_id='c1', mode='replace')
        self.desk.apply_import(
            sd.ImportResult(outcome='ok', endpoints=(sd.ImportedEndpoint(A),)),
            source_id=self.source.id, collection_id='c1', mode='replace', now=T0)

    def test_the_binding_starts_at_revision_one(self):
        self.assertEqual(self.desk.feed_row(self.source.id, 'c1')['access_revision'], 1)
        self.assertEqual(self.desk.get(self.source.id, 'c1').access_revision, 1)

    def test_rotation_persists_the_new_revision_and_names_the_old_one(self):
        state = self.desk.get(self.source.id, 'c1')
        plan, after, _ = sd.plan_rotation(state, access_id='access-1',
                                          new_access_ref=sd.make_ref('auth', 'b1', 'token-v2'), now=T0 + 10)
        invalidates = self.desk.apply_rotation(plan, after, now=T0 + 10)
        stored = self.desk.get(self.source.id, 'c1')
        self.assertEqual(stored.access_revision, 2)
        self.assertEqual(invalidates, (('access-1', 1),))
        self.assertEqual(self.desk.membership(self.source.id, 'c1'), (A,))

    def test_a_second_rotation_invalidates_only_its_own_revision(self):
        state = self.desk.get(self.source.id, 'c1')
        plan, after, _ = sd.plan_rotation(state, access_id='access-1',
                                          new_access_ref=sd.make_ref('auth', 'b1', 'token-v2'), now=T0)
        self.desk.apply_rotation(plan, after, now=T0)
        second, after2, _ = sd.plan_rotation(self.desk.get(self.source.id, 'c1'), access_id='access-1',
                                             new_access_ref=sd.make_ref('auth', 'b1', 'token-v3'),
                                             now=T0 + 10)
        self.assertEqual(second.invalidates, (('access-1', 2),))
        self.assertEqual(self.desk.apply_rotation(second, after2, now=T0 + 10), (('access-1', 2),))

    def test_rotation_does_not_disturb_the_collection(self):
        before = self.desk.get(self.source.id, 'c1')
        plan, after, _ = sd.plan_rotation(before, access_id='access-1',
                                          new_access_ref=sd.make_ref('auth', 'b1', 'token-v2'), now=T0)
        self.desk.apply_rotation(plan, after, now=T0)
        after_row = self.desk.get(self.source.id, 'c1')
        self.assertEqual(after_row.active, before.active)
        self.assertEqual(after_row.last_good, before.last_good)
        self.assertEqual(self.desk.membership(self.source.id, 'c1'), (A,))


class TransactionTests(StoreTestCase):
    def test_a_failure_midway_rolls_the_whole_plan_back(self):
        source = self.bind('c1', mode='replace')
        self.desk.apply_import(sd.ImportResult(outcome='ok', endpoints=(sd.ImportedEndpoint(A),)),
                               source_id=source.id, collection_id='c1', mode='replace', now=T0)
        before = self.desk.membership(source.id, 'c1')
        self.db.execute("CREATE TRIGGER refuse_delete BEFORE DELETE ON membership_source "
                        "BEGIN SELECT RAISE(ABORT, 'refused'); END")
        plan = sd.plan_refresh(sd.FeedState(source.id, 'c1', mode='replace', active=(A,),
                                            last_good=(A,), expires_at=T0 + 100),
                               sd.FeedResult(outcome='ok', fetched_at=T0 + 10,
                                             entries=(sd.ImportedEndpoint(B),)),
                               policy=sd.FeedPolicy(mode='replace'), now=T0 + 10)
        self.assertTrue(plan.applied_removals)
        with self.assertRaises(sd.SourceDeskError) as caught:
            self.desk.apply_plan(plan, now=T0 + 10)
        self.assertEqual(caught.exception.code, 'E_DATA_MIGRATION_FAILED')
        self.assertEqual(self.desk.membership(source.id, 'c1'), before)
        self.db.execute('DROP TRIGGER refuse_delete')

    def test_a_plan_for_a_foreign_collection_is_rejected_by_argument_type(self):
        with self.assertRaises(sd.SourceDeskError):
            self.desk.apply_plan({'added': ()}, now=T0)


class SubscriptionRoundTripTests(StoreTestCase):
    def test_a_clash_subscription_lands_in_the_collection(self):
        source = self.bind('c1', mode='replace')
        document = json.dumps({'proxies': [
            {'name': 'a', 'type': 'http', 'server': '198.51.100.7', 'port': 8080},
            {'name': 'b', 'type': 'socks5', 'server': '198.51.100.11', 'port': 1080},
        ], 'rules': ['MATCH,DIRECT']})
        result = sd.import_subscription(document, 'clash')
        plan = self.desk.apply_import(result, source_id=source.id, collection_id='c1',
                                      mode='replace', now=T0)
        self.assertEqual(plan.added, (A, B))
        self.assertEqual(self.desk.membership(source.id, 'c1'), (A, B))

    def test_a_second_import_is_a_delta_not_a_rebuild(self):
        source = self.bind('c1', mode='replace')
        first = sd.import_subscription(feed_document(A, B), 'clash')
        self.desk.apply_import(first, source_id=source.id, collection_id='c1', mode='replace', now=T0)
        second = sd.import_subscription(feed_document(A, C), 'clash')
        plan = self.desk.apply_import(second, source_id=source.id, collection_id='c1',
                                      mode='replace', now=T0 + 10)
        self.assertEqual(plan.added, (C,))
        self.assertEqual(plan.removed, (B,))
        self.assertEqual(plan.delta, frozenset({B, C}))

    def test_merge_mode_accumulates(self):
        source = self.bind('c1', mode='merge')
        self.desk.apply_import(sd.import_subscription(feed_document(A), 'clash'), source_id=source.id,
                               collection_id='c1', mode='merge', now=T0)
        plan = self.desk.apply_import(sd.import_subscription(feed_document(B), 'clash'),
                                      source_id=source.id, collection_id='c1', mode='merge', now=T0 + 10)
        self.assertEqual(plan.removed, ())
        self.assertEqual(self.desk.membership(source.id, 'c1'), (A, B))

    def test_canary_secret_never_reaches_the_database(self):
        secret = f'https://sub.invalid/v1/{CANARY}/list?token={CANARY}'
        source = sd.user_source(binding_id='b1', url=secret, source_format='clash',
                                headers={'Authorization': f'Bearer {CANARY}'}, header_put=put,
                                access_ref=sd.make_ref('auth', 'b1', CANARY), access_id='access-1')
        self.desk.bind(source, collection_id='c1', mode='replace')
        self.desk.apply_import(sd.import_subscription(feed_document(A), 'clash'),
                               source_id=source.id, collection_id='c1', mode='replace', now=T0)
        everything = self.rows('source_feed') + self.rows('membership_source')
        self.assertEqual(sd.find_secret_leaks(everything, [CANARY]), [])
        self.assertEqual(sd.find_secret_leaks(self.desk.list_feeds(), [CANARY]), [])
        self.assertEqual(json.dumps(self.desk.feed_row(source.id, 'c1'), ensure_ascii=False)
                         .count(CANARY), 0)


if __name__ == '__main__':
    unittest.main()
