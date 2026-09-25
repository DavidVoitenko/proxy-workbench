"""Immutable publication and the atomic pointer.

Defect 7 / R04: writing an artifact and moving the active pool are two acts.
"""
import json
import os
from pathlib import Path
import stat
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import core, db, exportsvc as es

NOW = 1_700_000_000.0


def rows(count=3):
    return [dict(proxy=f'http://203.0.113.{index + 7}:8080', country='DE', latency_ms=100 + index,
                 score=90 - index, reliability=1.0, min_target_reliability=1.0, successes=3,
                 requests=3, checked_at=NOW, valid_until=NOW + 600) for index in range(count)]


def scope(**overrides):
    base = dict(identity=core.Scope('col-1', 'prof-1', 1))
    base.update(overrides)
    return es.ExportScope(**base)


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.home = Path(self.temp.name)
        self.options = es.ExportOptions(published_at=NOW)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, kind='published', **overrides):
        return es.write_snapshot(self.home, overrides.pop('rows', rows()), scope=overrides.pop('scope', scope()),
                                 options=self.options, kind=kind, now=NOW, **overrides)

    def test_writing_a_generation_does_not_touch_the_pointer(self):
        artifact = self.write()
        self.assertIsNone(es.read_pointer(self.home))
        self.assertTrue((artifact.directory / 'status.json').is_file())
        self.assertEqual(es.read_pointer(self.home / 'nothing-here'), None)

    def test_publish_moves_the_pointer_and_carries_the_contract_fields(self):
        artifact = self.write()
        pointer = es.publish(artifact, self.home, confirm=True)
        self.assertEqual(pointer.generation, artifact.generation)
        self.assertEqual(pointer.schema_version, es.SNAPSHOT_SCHEMA_VERSION)
        self.assertEqual(pointer.scope_digest, artifact.status.digest)
        self.assertEqual(pointer.profile_id, 'prof-1')
        self.assertEqual(pointer.profile_revision, 1)
        self.assertEqual(pointer.collection_id, 'col-1')
        stored = es.read_pointer(self.home)
        self.assertEqual(stored.generation, artifact.generation)
        self.assertEqual(stored.manifest['proxies.txt']['bytes'],
                         (artifact.directory / 'proxies.txt').stat().st_size)
        self.assertEqual(stored.expires_at, artifact.status.expires_at)

    def test_publish_needs_an_explicit_confirmation(self):
        artifact = self.write()
        with self.assertRaises(es.ExportError) as caught:
            es.publish(artifact, self.home)
        self.assertEqual(caught.exception.code, 'E_STATE_PUBLISH_UNCONFIRMED')
        self.assertIsNone(es.read_pointer(self.home))

    def test_a_selection_can_never_be_published(self):
        artifact = self.write(kind='selection')
        for pointer in (es.POINTER_NAME, es.DIAGNOSTIC_POINTER_NAME):
            with self.subTest(pointer=pointer):
                with self.assertRaises(es.ExportError) as caught:
                    es.publish(artifact, self.home, confirm=True, pointer_name=pointer)
                self.assertEqual(caught.exception.code, 'E_VALIDATION_FIELD')
        self.assertIsNone(es.read_pointer(self.home))

    def test_a_diagnostic_snapshot_reaches_only_the_diagnostic_pointer(self):
        artifact = self.write(kind='diagnostic')
        with self.assertRaises(es.ExportError):
            es.publish(artifact, self.home, confirm=True)
        pointer = es.publish(artifact, self.home, confirm=True,
                             pointer_name=es.DIAGNOSTIC_POINTER_NAME)
        self.assertEqual(pointer.generation, artifact.generation)
        self.assertIsNone(es.read_pointer(self.home))
        self.assertEqual(es.read_pointer(self.home, es.DIAGNOSTIC_POINTER_NAME).generation,
                         artifact.generation)

    def test_a_failed_pointer_swap_leaves_the_previous_generation_published(self):
        first = self.write()
        es.publish(first, self.home, confirm=True)
        second = self.write()
        with mock.patch.object(es, '_atomic_write', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                es.publish(second, self.home, confirm=True)
        self.assertEqual(es.read_pointer(self.home).generation, first.generation)
        loaded = es.load_snapshot(self.home)
        self.assertEqual(loaded.generation, first.generation)
        self.assertEqual([row['proxy'] for row in loaded.rows],
                         [f'http://203.0.113.{index + 7}:8080' for index in range(3)])

    def test_a_second_publication_does_not_rewrite_the_first_generation(self):
        first = self.write()
        es.publish(first, self.home, confirm=True)
        before = {path.name: (path.stat().st_mtime_ns, path.stat().st_size)
                  for path in first.directory.iterdir()}
        second = self.write()
        es.publish(second, self.home, confirm=True)
        after = {path.name: (path.stat().st_mtime_ns, path.stat().st_size)
                 for path in first.directory.iterdir()}
        self.assertEqual(before, after)
        self.assertEqual(es.read_pointer(self.home).generation, second.generation)
        self.assertTrue(first.directory.is_dir())

    def test_published_files_are_not_writable(self):
        artifact = self.write()
        for path in artifact.directory.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode) & 0o222, 0, path.name)
        if hasattr(os, 'geteuid') and os.geteuid() != 0:
            with self.assertRaises(PermissionError):
                (artifact.directory / 'proxies.txt').write_text('tampered', encoding='utf-8')

    def test_a_failed_write_removes_the_half_written_generation(self):
        with mock.patch.object(Path, 'write_text', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.write()
        self.assertEqual(list((self.home / 'generations').iterdir()), [])
        self.assertIsNone(es.read_pointer(self.home))

    def test_prune_keeps_the_current_generation(self):
        names = []
        for _ in range(5):
            artifact = self.write()
            es.publish(artifact, self.home, confirm=True)
            names.append(artifact.generation)
        self.assertEqual(es.read_pointer(self.home).generation, names[-1])
        present = sorted(path.name for path in (self.home / 'generations').iterdir())
        self.assertEqual(len(present), self.options.keep_generations)
        self.assertIn(names[-1], present)
        self.assertNotIn(names[0], present)

    def migrated_db(self):
        """A database made by the real migrator, never a schema written here."""
        db.migrate(self.home / 'proxies.sqlite3')
        connection = db.connect(self.home / 'proxies.sqlite3')
        self.addCleanup(connection.close)
        return connection

    def test_record_artifact_stores_what_the_download_must_serve(self):
        artifact = self.write(kind='selection', scope=scope(selection=('http://203.0.113.7:8080',)),
                              rows=rows(1))
        connection = self.migrated_db()
        stored = es.record_artifact(connection, artifact)
        connection.commit()
        row = tuple(connection.execute(
            'SELECT kind, collection_id, profile_id, profile_revision, generation, state, reason_code '
            'FROM export_artifact WHERE id=?', (stored,)).fetchone())
        self.assertEqual(row, ('selection', 'col-1', 'prof-1', 1, artifact.generation,
                               'complete', 'ok'))
        manifest = json.loads(connection.execute('SELECT manifest_json FROM export_artifact WHERE id=?',
                                                 (stored,)).fetchone()[0])
        self.assertEqual(manifest['ranked.json']['sha256'], artifact.manifest['ranked.json']['sha256'])

    def test_record_artifact_reports_a_missing_table_instead_of_creating_one(self):
        artifact = self.write()
        connection = sqlite3.connect(':memory:')
        with self.assertRaises(es.ExportError) as caught:
            es.record_artifact(connection, artifact)
        self.assertEqual(caught.exception.code, 'E_DATA_MIGRATION_FAILED')
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, set())

    def test_record_artifact_reports_a_reshaped_table(self):
        artifact = self.write()
        connection = sqlite3.connect(':memory:')
        connection.execute('CREATE TABLE export_artifact (id TEXT PRIMARY KEY, kind TEXT)')
        with self.assertRaises(es.ExportError) as caught:
            es.record_artifact(connection, artifact)
        self.assertEqual(caught.exception.code, 'E_DATA_MIGRATION_FAILED')
        self.assertIn('manifest_json', caught.exception.detail)


if __name__ == '__main__':
    unittest.main()
