"""Reading a pinned generation.

Defect 3 (one expired member must not empty a set) and defect 8 (a consumer
stays on the generation it bound to).  The reader refuses what it cannot trust
instead of answering with an empty list.
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import core, exportsvc as es
from tests.fixtures import admission

NOW = 1_700_000_000.0
ROWS = [dict(proxy=f'http://203.0.113.{index + 7}:8080', country='DE', latency_ms=100 + index,
             score=90 - index, reliability=1.0, min_target_reliability=1.0, successes=3,
             requests=3, checked_at=NOW - 10, valid_until=NOW + 600) for index in range(4)]


def scope(**overrides):
    base = dict(identity=core.Scope('col-1', 'prof-1', 1))
    base.update(overrides)
    return es.ExportScope(**base)


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.home = Path(self.temp.name)
        self.first = es.write_snapshot(self.home, ROWS, scope=scope(),
                                       options=es.ExportOptions(published_at=NOW), now=NOW)
        es.publish(self.first, self.home, confirm=True)
        self.second = es.write_snapshot(self.home, ROWS[:2], scope=scope(top=2),
                                        options=es.ExportOptions(published_at=NOW, top=2), now=NOW)
        es.publish(self.second, self.home, confirm=True)

    def tearDown(self):
        self.temp.cleanup()

    def test_a_consumer_keeps_the_generation_it_bound_to(self):
        bound = es.load_snapshot(self.home, self.first.generation)
        self.assertEqual(bound.generation, self.first.generation)
        self.assertEqual(len(bound.rows), 4)
        self.assertEqual(es.read_pointer(self.home).generation, self.second.generation)
        self.assertEqual(len(es.load_snapshot(self.home).rows), 2)

    def test_pinning_goes_through_the_shared_core_contract(self):
        available = [core.Generation(name=self.first.generation, schema_version=es.SNAPSHOT_SCHEMA_VERSION),
                     core.Generation(name=self.second.generation, schema_version=es.SNAPSHOT_SCHEMA_VERSION)]
        pinned = core.pin_generation(available, self.first.generation, es.SUPPORTED_SCHEMA_VERSIONS)
        self.assertEqual(pinned.name, self.first.generation)
        with self.assertRaises(core.AdmissionError) as caught:
            core.pin_generation(available, '.generation-missing0000', es.SUPPORTED_SCHEMA_VERSIONS)
        self.assertEqual(caught.exception.code, 'E_STATE_NO_SNAPSHOT')

    def test_an_unknown_generation_is_a_refusal_not_an_empty_list(self):
        for name in ('.generation-missing0000', '.generation-a_b', '../escape', ''):
            with self.subTest(name=name):
                with self.assertRaises(es.ExportError) as caught:
                    es.load_snapshot(self.home, name)
                self.assertIn(caught.exception.code, ('E_STATE_NO_SNAPSHOT', 'E_VALIDATION_FIELD'))

    def test_a_missing_pointer_is_a_refusal(self):
        with self.assertRaises(es.ExportError) as caught:
            es.load_snapshot(self.home / 'empty-dir')
        self.assertEqual(caught.exception.code, 'E_STATE_NO_SNAPSHOT')

    def test_one_expired_member_does_not_empty_the_set(self):
        # A mixed-age generation, as written by a run that found some members
        # already past their deadline: the file is consistent, no tampering.
        mixed = [dict(ROWS[0], valid_until=NOW - 1), dict(ROWS[1], valid_until=NOW + 5_000)]
        mixed += [dict(row, valid_until=NOW + 600) for row in ROWS[2:]]
        artifact = es.write_snapshot(self.home, mixed, scope=scope(),
                                     options=es.ExportOptions(published_at=NOW), now=NOW)
        loaded = es.load_snapshot(self.home, artifact.generation, now=NOW)
        self.assertEqual(len(loaded.rows), 4)
        self.assertEqual(len(loaded.fresh_rows), 3)
        self.assertEqual([row['proxy'] for row in loaded.expired_rows], [ROWS[0]['proxy']])
        self.assertEqual(loaded.status.state, 'complete')
        # the set lives on its own deadline, not on the oldest row it still holds
        self.assertEqual(loaded.status.expires_at, NOW + 900)
        self.assertGreater(loaded.status.expires_at, mixed[0]['valid_until'])
        self.assertFalse(loaded.expired)

    def test_the_set_expires_on_its_own_deadline(self):
        loaded = es.load_snapshot(self.home, self.first.generation, now=NOW + 10_000)
        self.assertTrue(loaded.expired)
        self.assertEqual(loaded.status.state, 'complete')
        self.assertEqual(len(loaded.fresh_rows), 0)

    def test_a_tampered_file_is_caught_by_the_manifest(self):
        path = self.first.directory / 'proxies.txt'
        path.chmod(0o644)
        path.write_text('http://203.0.113.200:8080\n', encoding='utf-8')
        with self.assertRaises(es.ExportError) as caught:
            es.load_snapshot(self.home, self.first.generation)
        self.assertEqual(caught.exception.code, 'E_STATE_SNAPSHOT_MANIFEST')
        loaded = es.load_snapshot(self.home, self.first.generation, verify=False)
        self.assertEqual(len(loaded.rows), 4)

    def test_rows_from_another_generation_are_refused(self):
        status = json.loads((self.first.directory / 'status.json').read_text(encoding='utf-8'))
        status['generation'] = self.second.generation
        (self.first.directory / 'status.json').chmod(0o644)
        (self.first.directory / 'status.json').write_text(json.dumps(status), encoding='utf-8')
        with self.assertRaises(es.ExportError) as caught:
            es.load_snapshot(self.home, self.first.generation)
        self.assertEqual(caught.exception.code, 'E_STATE_SNAPSHOT_MIXED')

    def test_a_status_of_an_unknown_version_is_refused(self):
        status = json.loads((self.first.directory / 'status.json').read_text(encoding='utf-8'))
        status['schema_version'] = 99
        (self.first.directory / 'status.json').chmod(0o644)
        (self.first.directory / 'status.json').write_text(json.dumps(status), encoding='utf-8')
        with self.assertRaises(es.ExportError) as caught:
            es.load_snapshot(self.home, self.first.generation)
        self.assertEqual(caught.exception.code, 'E_STATE_SNAPSHOT_SCHEMA')

    def test_a_broken_ranked_json_is_a_refusal(self):
        path = self.first.directory / 'ranked.json'
        path.chmod(0o644)
        path.write_text('{"not": "a list"}', encoding='utf-8')
        with self.assertRaises(es.ExportError) as caught:
            es.load_snapshot(self.home, self.first.generation, verify=False)
        self.assertEqual(caught.exception.code, 'E_VALIDATION_SCHEMA')

    def test_public_rows_carry_the_age_and_never_invent_a_reason(self):
        loaded = es.load_snapshot(self.home, self.first.generation, now=NOW + 60)
        public = loaded.public_rows()
        self.assertEqual(len(public), 4)
        for row in public:
            self.assertAlmostEqual(row['age_seconds'], 70.0, places=3)
            self.assertFalse(row['stale'])
            self.assertIsNone(row['admission_reason'])
            self.assertNotIn('password', row)

    def test_a_reader_that_uses_core_admission_gets_its_reason_into_the_rows(self):
        identity = dict(collection_id='col-1', profile_id='prof-1', profile_revision=1,
                        network_id='default', access_id='acc-1', access_revision=1,
                        reliability=1.0, min_target_reliability=1.0, checked_at=NOW,
                        valid_until=NOW + 600)
        rows = [dict(identity, proxy=row['proxy']) for row in ROWS]
        selection = core.select(rows, core.Scope('col-1', 'prof-1', 1), core.Access('acc-1', 1),
                                core.Policy(max_age_seconds=900), NOW)
        artifact = es.write_snapshot(self.home, selection.admitted, scope=scope(),
                                     options=es.ExportOptions(published_at=NOW), now=NOW,
                                     selection=selection)
        loaded = es.load_snapshot(self.home, artifact.generation, now=NOW)
        self.assertEqual({row['admission_reason'] for row in loaded.rows}, {'OK'})
        self.assertEqual({row['time_state'] for row in loaded.rows}, {core.TIME_OK})
        report = json.loads((artifact.directory / 'status.json').read_text(encoding='utf-8'))
        self.assertEqual(report['admitted'], 4)
        self.assertEqual(report['admission_counts'], {})


class SharedFixtureTests(unittest.TestCase):
    """The snapshot contract on the rows every other module tests against.

    The fixture is the agreed vocabulary (HANDOFF §2.1), so a divergence between
    this module and ``core`` shows up here rather than in an integration run.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.home = Path(self.temp.name)
        self.identity = core.Scope('col-fixture', 'fixture-profile', 1)
        self.policy = core.Policy(max_age_seconds=2 * admission.HOUR)

    def tearDown(self):
        self.temp.cleanup()

    def payloads(self, *names):
        return [admission.admission_row(name) for name in names]

    def test_the_mixed_age_snapshot_keeps_the_fresh_member(self):
        cases = self.payloads('fresh_ok', 'still_valid', 'expired_ttl')
        artifact = es.write_snapshot(self.home, [row['payload'] for row in cases],
                                     scope=scope(identity=self.identity),
                                     options=es.ExportOptions(published_at=admission.NOW,
                                                              client_target='1.12.0'),
                                     policy=self.policy, now=admission.NOW)
        loaded = es.load_snapshot(self.home, artifact.generation, now=admission.NOW)
        self.assertEqual(len(loaded.rows), 3)
        self.assertEqual(len(loaded.fresh_rows), 2)
        self.assertEqual([row['name'] for row in loaded.expired_rows], ['expired_ttl'])
        self.assertEqual(loaded.status.state, 'complete')
        self.assertEqual(loaded.status.state_detail, core.DETAIL_OK)
        # the set deadline is the newest member's, not the oldest one it still holds
        self.assertEqual(loaded.status.expires_at, admission.NOW + 2 * admission.HOUR)

    def test_every_time_state_of_the_fixture_reaches_the_artifact_unchanged(self):
        cases = admission.rows()
        artifact = es.write_snapshot(self.home, [row['payload'] for row in cases],
                                     scope=scope(identity=self.identity),
                                     options=es.ExportOptions(published_at=admission.NOW),
                                     policy=self.policy, now=admission.NOW)
        loaded = es.load_snapshot(self.home, artifact.generation, now=admission.NOW)
        by_name = {row['name']: row for row in loaded.rows}
        self.assertEqual(set(by_name), {row['case']['name'] for row in cases})
        for row in cases:
            with self.subTest(case=row['case']['name']):
                self.assertEqual(by_name[row['case']['name']]['checked_at'], row['checked_at'])
                self.assertEqual(by_name[row['case']['name']]['valid_until'], row['valid_until'])
        # one member is past its deadline, one never recorded a deadline, and a row
        # measured in the future is not "very fresh" either: only core decides that
        self.assertEqual([row['name'] for row in loaded.expired_rows], ['expired_ttl'])
        self.assertEqual([row['name'] for row in loaded.unknown_rows], ['unknown_time'])
        self.assertEqual(len(loaded.rows) - len(loaded.expired_rows) - len(loaded.unknown_rows), 5)
        self.assertEqual(loaded.status.state, 'complete')

    def test_a_row_without_a_recorded_time_is_not_infinitely_fresh(self):
        cases = self.payloads('unknown_time', 'fresh_ok')
        artifact = es.write_snapshot(self.home, [row['payload'] for row in cases],
                                     scope=scope(identity=self.identity),
                                     options=es.ExportOptions(published_at=admission.NOW),
                                     policy=self.policy, now=admission.NOW)
        loaded = es.load_snapshot(self.home, artifact.generation, now=admission.NOW)
        public = {row['name']: row for row in loaded.public_rows()}
        unknown = public['unknown_time']
        self.assertIsNone(unknown['checked_at'])
        self.assertIsNone(unknown['valid_until'])
        self.assertEqual(unknown['freshness'], 'unknown')
        self.assertFalse(unknown['stale'])
        self.assertIsNone(unknown['age_seconds'])
        self.assertEqual(public['fresh_ok']['freshness'], 'fresh')
        self.assertEqual([row['name'] for row in loaded.unknown_rows], ['unknown_time'])

    def test_the_denied_hostname_row_is_reported_not_silently_dropped(self):
        cases = self.payloads('blocked', 'fresh_ok')
        artifact = es.write_snapshot(self.home, [row['payload'] for row in cases],
                                     scope=scope(identity=self.identity),
                                     options=es.ExportOptions(published_at=admission.NOW),
                                     policy=self.policy, now=admission.NOW)
        status = json.loads((artifact.directory / 'status.json').read_text(encoding='utf-8'))
        reasons = {item['proxy']: item['reasons'] for item in status['compat']['unsupported']}
        self.assertEqual(reasons[f'http://{admission.BLOCKED_HOST}:8080'],
                         ['E_EXPORT_HOSTNAME_UNSUPPORTED'])
        # the IP-only list drops the hostname; a client that takes a hostname keeps it
        self.assertEqual(status['compat']['files']['proxies.txt']['supported'], 1)
        self.assertEqual(status['compat']['files']['clash.yaml']['supported'], 2)
        self.assertEqual(status['exported'], 2)

    def test_the_fixture_digest_can_be_carried_in_the_status(self):
        artifact = es.write_snapshot(self.home, [row['payload'] for row in admission.rows()],
                                     scope=scope(identity=self.identity),
                                     options=es.ExportOptions(published_at=admission.NOW),
                                     policy=self.policy, now=admission.NOW,
                                     extra={'fixture_digest': admission.FIXTURE_DIGEST})
        report = json.loads((artifact.directory / 'status.json').read_text(encoding='utf-8'))
        self.assertEqual(report['fixture_digest'], admission.FIXTURE_DIGEST)
        self.assertEqual(es.status_from_dict(report).extra['fixture_digest'],
                         admission.FIXTURE_DIGEST)


if __name__ == '__main__':
    unittest.main()
