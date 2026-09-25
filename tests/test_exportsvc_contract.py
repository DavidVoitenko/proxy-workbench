"""Snapshot contract: scope, profile, policy, status, schema version.

CONTRACTS.ru.md §4.2-§4.3: what a snapshot says, what a reader may accept, and
which legacy names must keep working.
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import core, exportsvc as es


def scope(**overrides):
    base = dict(identity=core.Scope('col-1', 'prof-1', 3))
    base.update(overrides)
    return es.ExportScope(**base)


class ScopeDigestTests(unittest.TestCase):
    def test_digest_is_stable_under_key_order(self):
        left = es.ExportScope(identity=core.Scope('c', 'p', 1), protocol='socks5', countries=('de', 'NL'))
        right = es.ExportScope(identity=core.Scope('c', 'p', 1), protocol='socks5', countries=('de', 'nl'))
        self.assertEqual(left.digest(), right.digest())
        self.assertEqual(len(left.digest()), 64)

    def test_digest_changes_with_every_scope_component(self):
        base = scope()
        variants = [scope(identity=core.Scope('col-2', 'prof-1', 3)),
                    scope(identity=core.Scope('col-1', 'prof-1', 4)),
                    scope(protocol='socks5'), scope(countries=('de',)),
                    scope(exclude_hosting=True), scope(query='de'),
                    scope(quick='speed'), scope(network_id='home'),
                    scope(merge_mode='replace'), scope(source_binding='feed-7')]
        seen = {base.digest()}
        for variant in variants:
            self.assertNotIn(variant.digest(), seen, variant)
            seen.add(variant.digest())

    def test_selection_and_top_do_not_change_the_table_digest(self):
        plain = scope()
        sliced = scope(selection=('http://203.0.113.7:8080',), top=5)
        self.assertEqual(plain.digest(), sliced.digest())
        self.assertNotEqual(plain.artifact_digest('published', 'p', []),
                            sliced.artifact_digest('selection', 'p', []))

    def test_core_scope_carries_the_network_the_caller_asked_for(self):
        self.assertEqual(scope(network_id='home').core_scope.network_id, 'home')
        self.assertEqual(scope().core_scope, core.Scope('col-1', 'prof-1', 3, 'default'))

    def test_closed_value_sets_are_rejected(self):
        with self.assertRaises(es.ExportError) as caught:
            scope(protocol='quic')
        self.assertEqual(caught.exception.code, 'E_VALIDATION_FIELD')
        with self.assertRaises(es.ExportError):
            scope(merge_mode='upsert')


class StatusContractTests(unittest.TestCase):
    def build(self, **overrides):
        parameters = dict(kind='published', generation='.generation-abcdef0123456789',
                          scope=scope(), policy=core.Policy(max_age_seconds=900),
                          options=es.ExportOptions(), rows=[{'proxy': 'http://203.0.113.7:8080'}],
                          now=1_700_000_000.0)
        parameters.update(overrides)
        return es.build_status(**parameters)

    def test_legacy_field_names_survive(self):
        report = self.build().as_dict()
        for name in ('schema_version', 'profile', 'generation', 'state', 'stop_reason', 'scope',
                     'scope_candidates', 'checked', 'pending', 'passed', 'candidates',
                     'local_filtered', 'exported', 'complete', 'generated_at', 'valid_until',
                     'stale', 'empty_export', 'sort', 'min_success', 'protocol', 'max_latency',
                     'countries', 'exclude_hosting', 'query', 'quick', 'watch_minutes',
                     'selection_requested', 'selection_exported', 'selection_missing',
                     'selection_truncated', 'request_profile', 'request_profile_digest',
                     'reputation', 'anonymity', 'source_quality', 'source_health_basis',
                     'breakdown'):
            self.assertIn(name, report, name)

    def test_contract_fields_are_added(self):
        report = self.build(extra={'request_profile': 'workbench', 'request_profile_digest': 'abc',
                                   'targets': [{'name': 'one', 'url': 'http://one.invalid/'}],
                                   'reputation': {'strict': False, 'counts': {'clean': 1}},
                                   'anonymity': {'enabled': False, 'min_level': 'any', 'counts': {}},
                                   'source_quality': {}, 'source_health_basis': 'fresh_profile_checks',
                                   'breakdown': {'protocols': {'http': 1}, 'countries': {}}}).as_dict()
        for name in ('kind', 'profile_id', 'profile_revision', 'collection_id', 'scope_digest',
                     'max_age_seconds', 'expires_at', 'state_detail', 'deficit_reasons',
                     'desired', 'next_attempt_at', 'compat', 'artifact_digest', 'merge_mode',
                     'credentials', 'client_target'):
            self.assertIn(name, report, name)
        self.assertEqual(report['scope'], {'protocol': 'all', 'countries': [], 'exclude_hosting': False})
        self.assertEqual(report['targets'], [{'name': 'one', 'url': 'http://one.invalid/'}])

    def test_state_and_stop_reason_are_separate_axes(self):
        stale = self.build(rows=[], run_state={'state': core.SELECTION_STALE,
                                               'state_detail': core.DETAIL_ALL_EXPIRED}).as_dict()
        self.assertEqual(stale['state'], 'stale')
        self.assertEqual(stale['state_detail'], 'all_expired')
        self.assertEqual(stale['stop_reason'], 'expired')
        self.assertTrue(stale['stale'])
        self.assertFalse(stale['empty_export'])
        empty = self.build(rows=[], run_state={'state': core.SELECTION_EMPTY,
                                               'state_detail': core.DETAIL_NO_MATCH}).as_dict()
        self.assertEqual(empty['state'], 'empty')
        self.assertFalse(empty['stale'])
        self.assertTrue(empty['empty_export'])

    def test_round_trip_through_status_from_dict(self):
        report = self.build(kind='selection', scope=scope(query='de', selection=('http://203.0.113.7:8080',)),
                            options=es.ExportOptions(sort='speed', top=3, client_target='1.12.0')).as_dict()
        restored = es.status_from_dict(json.loads(json.dumps(report)))
        self.assertEqual(restored.generation, report['generation'])
        self.assertEqual(restored.kind, 'selection')
        self.assertEqual(restored.scope.query, 'de')
        self.assertEqual(restored.scope.digest(), report['scope_digest'])
        self.assertEqual(restored.options.client_target, '1.12.0')
        self.assertEqual(restored.as_dict()['exported'], report['exported'])
        self.assertFalse(restored.legacy)

    def test_legacy_version_one_status_is_readable_but_flagged(self):
        legacy = {'schema_version': 1, 'profile': 'fx', 'generation': '.generation-legacy0001',
                  'state': 'complete', 'stop_reason': 'complete', 'protocol': 'socks5',
                  'countries': ['de'], 'exported': 3, 'checked': 4, 'scope_candidates': 4,
                  'generated_at': 1.0, 'valid_until': 2.0, 'stale': False, 'empty_export': False}
        restored = es.status_from_dict(legacy)
        self.assertTrue(restored.legacy)
        self.assertEqual(restored.profile, 'fx')
        self.assertEqual(restored.generation, '.generation-legacy0001')
        self.assertEqual(restored.as_dict()['legacy'], True)

    def test_unknown_schema_version_is_refused(self):
        for version in (0, 3, 99, '2', None):
            with self.subTest(version=version):
                with self.assertRaises(es.ExportError) as caught:
                    es.status_from_dict({'schema_version': version, 'profile': 'fx'})
                self.assertEqual(caught.exception.code, 'E_STATE_SNAPSHOT_SCHEMA')

    def test_version_two_without_scope_digest_is_refused(self):
        with self.assertRaises(es.ExportError) as caught:
            es.status_from_dict({'schema_version': 2, 'profile': 'fx', 'generation': '.generation-1'})
        self.assertEqual(caught.exception.code, 'E_VALIDATION_SCHEMA')

    def test_kind_is_a_closed_set(self):
        with self.assertRaises(es.ExportError) as caught:
            self.build(kind='adhoc')
        self.assertEqual(caught.exception.code, 'E_VALIDATION_FIELD')

    def test_set_lifetime_is_not_the_minimum_of_row_ttls(self):
        identity = dict(collection_id='col-1', profile_id='prof-1', profile_revision=3,
                        network_id='default', access_id='acc-1', access_revision=1,
                        reliability=1.0, min_target_reliability=1.0, checked_at=1_700_000_000.0)
        rows = [dict(identity, proxy='http://203.0.113.7:8080', valid_until=1_700_000_100.0),
                dict(identity, proxy='socks5://198.51.100.9:1080', valid_until=1_700_000_900.0)]
        selection = core.select(rows, core.Scope('col-1', 'prof-1', 3), core.Access('acc-1', 1),
                                core.Policy(max_age_seconds=900), 1_700_000_000.0)
        self.assertEqual(len(selection.admitted), 2)
        report = self.build(rows=selection.admitted, selection=selection).as_dict()
        self.assertEqual(report['expires_at'], 1_700_000_900.0)
        self.assertEqual(report['valid_until'], 1_700_000_900.0)
        self.assertEqual(report['admitted'], 2)

    def test_one_expired_member_does_not_expire_the_set(self):
        identity = dict(collection_id='col-1', profile_id='prof-1', profile_revision=3,
                        network_id='default', access_id='acc-1', access_revision=1,
                        reliability=1.0, min_target_reliability=1.0)
        rows = [dict(identity, proxy='http://203.0.113.7:8080', checked_at=1_699_999_000.0,
                     valid_until=1_699_999_500.0),
                dict(identity, proxy='socks5://198.51.100.9:1080', checked_at=1_700_000_000.0,
                     valid_until=1_700_000_900.0)]
        selection = core.select(rows, core.Scope('col-1', 'prof-1', 3), core.Access('acc-1', 1),
                                core.Policy(max_age_seconds=900), 1_700_000_000.0)
        report = self.build(rows=selection.admitted, selection=selection).as_dict()
        self.assertEqual([item.reason_code for item in selection.rejected],
                         ['E_TIME_TTL_EXPIRED'])
        self.assertEqual(report['state'], 'partial')
        self.assertEqual(report['state_detail'], 'rejected')
        self.assertEqual(report['expires_at'], 1_700_000_900.0)
        self.assertEqual(report['admission_counts'], {'E_TIME_TTL_EXPIRED': 1})


class GenerationNameTests(unittest.TestCase):
    def test_names_stay_inside_the_contracted_charset(self):
        import re
        for _ in range(50):
            name = es.generation_name()
            self.assertRegex(name, re.compile(r'^\.generation-[A-Za-z0-9-]{8,}$'))
            self.assertTrue(es.valid_generation_name(name))

    def test_names_that_could_escape_the_directory_are_rejected(self):
        for name in ('.generation-a_b', '.generation-x', '../evil', '.generation-a/b',
                     '.generation-a\\b', '', '.', None, 42):
            self.assertFalse(es.valid_generation_name(name), name)


class PointerReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.home = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_broken_or_hostile_pointer_reads_as_absent(self):
        (self.home / es.POINTER_NAME).parent.mkdir(parents=True, exist_ok=True)
        for content in ('{"generation": "../elsewhere"}', '{"generation": ".generation-a_b"}',
                        'not json at all', '[]', '{"generation": ""}'):
            with self.subTest(content=content):
                (self.home / es.POINTER_NAME).write_text(content, encoding='utf-8')
                self.assertIsNone(es.read_pointer(self.home))
        (self.home / es.POINTER_NAME).unlink()
        self.assertIsNone(es.read_pointer(self.home))


if __name__ == '__main__':
    unittest.main()
