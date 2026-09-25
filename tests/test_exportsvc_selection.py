"""Export of a selection, a top-N slice and a search result.

Defect 7 / R04: these are their own artifacts.  They do not move the active
pool and they do not narrow the table.  F27, merge/replace side: the artifact
records the binding and the merge mode it was cut from, and no export path
writes membership at all, so a failed refresh cannot clear a collection.
"""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import core, db, exportsvc as es

NOW = 1_700_000_000.0
POOL = [dict(proxy=f'http://203.0.113.{index + 7}:8080', country='DE', latency_ms=100 + index,
             score=90 - index, reliability=1.0, min_target_reliability=1.0, successes=3,
             requests=3, checked_at=NOW, valid_until=NOW + 600) for index in range(10)]
PROXIES = [row['proxy'] for row in POOL]


def scope(**overrides):
    base = dict(identity=core.Scope('col-1', 'prof-1', 1))
    base.update(overrides)
    return es.ExportScope(**base)


class SelectionArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.home = Path(self.temp.name)
        self.published = es.write_snapshot(self.home, POOL, scope=scope(),
                                           options=es.ExportOptions(published_at=NOW), now=NOW)
        es.publish(self.published, self.home, confirm=True)

    def tearDown(self):
        self.temp.cleanup()

    def active(self):
        return es.load_snapshot(self.home)

    def test_a_selection_does_not_move_the_active_pool(self):
        chosen = PROXIES[2:5]
        selection = es.write_snapshot(self.home, [row for row in POOL if row['proxy'] in chosen],
                                      scope=scope(selection=tuple(chosen)),
                                      options=es.ExportOptions(published_at=NOW), kind='selection',
                                      now=NOW)
        self.assertEqual(es.read_pointer(self.home).generation, self.published.generation)
        self.assertEqual(len(self.active().rows), len(POOL))
        self.assertEqual(selection.kind, 'selection')
        self.assertEqual(selection.status.as_dict()['selection_requested'], 3)
        self.assertEqual(selection.status.as_dict()['selection_exported'], 3)

    def test_a_top_n_slice_does_not_move_the_active_pool(self):
        options = es.ExportOptions(published_at=NOW, top=3)
        artifact = es.write_snapshot(self.home, POOL, scope=scope(), options=options,
                                     kind='selection', now=NOW)
        self.assertEqual(es.read_pointer(self.home).generation, self.published.generation)
        self.assertEqual(len(self.active().rows), len(POOL))
        report = json.loads((artifact.directory / 'status.json').read_text(encoding='utf-8'))
        self.assertEqual(report['top'], 3)
        self.assertEqual(report['exported'], 3)
        self.assertEqual((artifact.directory / 'proxies.txt').read_text(encoding='utf-8').split(),
                         PROXIES[:3])

    def test_a_search_result_does_not_move_the_active_pool(self):
        found = es.ExportScope(identity=core.Scope('col-1', 'prof-1', 1), query='203.0.113.1')
        matching = [row for row in POOL if found.query in row['proxy']]
        artifact = es.write_snapshot(self.home, matching, scope=found,
                                     options=es.ExportOptions(published_at=NOW), kind='selection',
                                     now=NOW)
        self.assertEqual(es.read_pointer(self.home).generation, self.published.generation)
        self.assertEqual(len(self.active().rows), len(POOL))
        report = json.loads((artifact.directory / 'status.json').read_text(encoding='utf-8'))
        self.assertEqual(report['query'], '203.0.113.1')
        self.assertEqual(report['scope']['protocol'], 'all')

    def test_a_selection_that_cannot_be_fully_served_says_which_part_is_missing(self):
        wanted = (PROXIES[0], 'http://198.51.100.1:8080')
        artifact = es.write_snapshot(self.home, [row for row in POOL if row['proxy'] == PROXIES[0]],
                                     scope=scope(selection=wanted),
                                     options=es.ExportOptions(published_at=NOW), kind='selection',
                                     now=NOW)
        report = json.loads((artifact.directory / 'status.json').read_text(encoding='utf-8'))
        self.assertEqual(report['selection_missing'], ['http://198.51.100.1:8080'])
        self.assertEqual(report['selection_exported'], 1)

    def test_the_table_scope_digest_is_the_same_for_the_slice_and_the_table(self):
        artifact = es.write_snapshot(self.home, POOL[:2], scope=scope(selection=tuple(PROXIES[:2])),
                                     options=es.ExportOptions(published_at=NOW), kind='selection',
                                     now=NOW)
        self.assertEqual(artifact.status.digest, self.published.status.digest)
        self.assertNotEqual(artifact.status.artifact_digest, self.published.status.artifact_digest)
        self.assertEqual(artifact.status.as_dict()['kind'], 'selection')
        self.assertEqual(self.published.status.as_dict()['kind'], 'published')

    def test_a_selection_keeps_its_own_generation_and_manifest(self):
        artifact = es.write_snapshot(self.home, POOL[:2], scope=scope(selection=tuple(PROXIES[:2])),
                                     options=es.ExportOptions(published_at=NOW), kind='selection',
                                     now=NOW)
        self.assertNotEqual(artifact.generation, self.published.generation)
        loaded = es.load_snapshot(self.home, artifact.generation)
        self.assertEqual(loaded.kind, 'selection')
        self.assertEqual(len(loaded.rows), 2)
        self.assertEqual(loaded.status.kind, 'selection')
        self.assertEqual(es.read_pointer(self.home).generation, self.published.generation)


class MergeReplaceContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.home = Path(self.temp.name)
        # A database made by the real migrator, never a schema declared here.
        db.migrate(self.home / 'proxies.sqlite3')
        self.db = db.connect(self.home / 'proxies.sqlite3')
        self.addCleanup(self.db.close)
        self.collection = db.create_collection(self.db, 'Свои', kind='private',
                                              collection_id='col-1', now=NOW)
        for index in range(5):
            endpoint = db.upsert_endpoint(self.db, f'http://192.0.2.{index + 1}:8080')
            db.add_member(self.db, self.collection, endpoint, origin='manual', now=NOW)
        self.db.commit()

    def tearDown(self):
        self.temp.cleanup()

    def membership(self):
        return [row[0] for row in self.db.execute(
            'SELECT endpoint_id FROM membership WHERE collection_id=? ORDER BY endpoint_id',
            (self.collection,))]

    def test_publishing_never_touches_collection_membership(self):
        before = self.membership()
        artifact = es.write_snapshot(self.home, POOL, scope=scope(merge_mode='replace',
                                                                  source_binding='feed-9'),
                                     options=es.ExportOptions(published_at=NOW), now=NOW)
        es.publish(artifact, self.home, confirm=True)
        es.record_artifact(self.db, artifact)
        self.db.commit()
        self.assertEqual(self.membership(), before)

    def test_a_failing_generation_leaves_the_collection_and_the_pointer_alone(self):
        good = es.write_snapshot(self.home, POOL, scope=scope(merge_mode='merge',
                                                             source_binding='feed-9'),
                                 options=es.ExportOptions(published_at=NOW), now=NOW)
        es.publish(good, self.home, confirm=True)
        before = self.membership()
        with self.assertRaises(es.ExportError):
            es.write_snapshot(self.home, [], scope=scope(merge_mode='replace', source_binding='feed-9'),
                              options=es.ExportOptions(published_at=NOW, empty_policy='error'), now=NOW)
        with self.assertRaises(es.ExportError):
            es.write_snapshot(self.home, POOL, scope=scope(),
                              options=es.ExportOptions(published_at=NOW, client_target='nightly'), now=NOW)
        self.assertEqual(self.membership(), before)
        self.assertEqual(es.read_pointer(self.home).generation, good.generation)
        self.assertEqual(len(es.load_snapshot(self.home).rows), len(POOL))

    def test_merge_and_replace_are_recorded_on_the_artifact(self):
        for mode in ('merge', 'replace', 'none'):
            with self.subTest(mode=mode):
                artifact = es.write_snapshot(self.home, POOL, scope=scope(merge_mode=mode,
                                                                          source_binding='feed-9'),
                                             options=es.ExportOptions(published_at=NOW), now=NOW)
                report = artifact.status.as_dict()
                self.assertEqual(report['merge_mode'], mode)
                self.assertEqual(report['source_binding'], 'feed-9')
                self.assertEqual(report['collection_id'], 'col-1')
                self.assertNotEqual(artifact.status.digest, scope().digest())
                self.assertEqual(es.status_from_dict(report).scope.merge_mode, mode)

    def test_the_recorded_artifact_serves_the_selection_it_was_cut_from(self):
        chosen = PROXIES[:2]
        artifact = es.write_snapshot(self.home, POOL[:2], scope=scope(selection=tuple(chosen),
                                                                      source_binding='feed-9'),
                                     options=es.ExportOptions(published_at=NOW), kind='selection',
                                     now=NOW)
        es.record_artifact(self.db, artifact)
        self.db.commit()
        row = self.db.execute('SELECT kind, collection_id, generation, manifest_json FROM export_artifact '
                              'WHERE id=?', (artifact.id,)).fetchone()
        self.assertEqual(row[0], 'selection')
        self.assertEqual(row[1], 'col-1')
        self.assertEqual(row[2], artifact.generation)
        self.assertEqual(json.loads(row[3])['proxies.txt']['sha256'],
                         artifact.manifest['proxies.txt']['sha256'])
        served = es.load_snapshot(self.home, row[2])
        self.assertEqual([row['proxy'] for row in served.rows], chosen)


if __name__ == '__main__':
    unittest.main()
