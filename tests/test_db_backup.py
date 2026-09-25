"""F24: technical backup, restore-preview into a new data path, and data path moves.

Every restore here targets a *new* folder: the current data path is never
overwritten, and a preview never writes anything.
"""
from pathlib import Path
import json
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import db
from proxy_workbench import proxytool as proxytool_module

LEGACY_PROXY = "http://1.2.3.4:8080"


def legacy_database(path):
    conn = proxytool_module.open_db(path)
    conn.execute("INSERT INTO candidates VALUES (?)", (LEGACY_PROXY,))
    conn.execute("INSERT INTO results VALUES (?,?,?)", ("p1", LEGACY_PROXY, '{"score": 80}'))
    conn.commit()
    conn.close()
    return Path(path)


class BackupRestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.data = self.home / "data"
        self.db_path = self.data / db.DB_FILENAME

    # --- backup -------------------------------------------------------------

    def test_backup_is_a_readable_database_with_a_checksum_manifest(self):
        db.migrate(self.db_path)
        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        conn.execute("INSERT INTO candidates(proxy, endpoint_id) VALUES (?,?)",
                     (LEGACY_PROXY, db.endpoint_id(LEGACY_PROXY)))
        conn.commit()

        manifest = db.create_backup(conn, self.home / "backups", reason="manual")
        self.assertTrue(manifest.path.endswith(".bak"))
        self.assertEqual(manifest.schema_version, db.SCHEMA_VERSION)
        self.assertEqual(manifest.application_id, db.APPLICATION_ID)
        self.assertEqual(Path(manifest.source_path).resolve(), self.db_path.resolve())
        self.assertEqual(manifest.bytes, Path(manifest.path).stat().st_size)
        self.assertEqual(len(manifest.sha256), 64)
        self.assertTrue(db.verify_backup(manifest))
        copy = db.connect(manifest.path, read_only=True)
        self.addCleanup(copy.close)
        self.assertEqual(copy.execute("SELECT count(*) FROM candidates").fetchone()[0], 1)

    def test_manifest_file_is_next_to_the_backup_and_readable_back(self):
        db.migrate(self.db_path)
        manifest = db.create_backup(self.db_path, self.home / "backups", name="named.bak")
        manifest_file = db.manifest_path(manifest.path)
        self.assertEqual(manifest_file.name, "named.bak" + db.MANIFEST_SUFFIX)
        payload = json.loads(Path(manifest_file).read_text(encoding="utf-8"))
        self.assertEqual(payload["sha256"], manifest.sha256)
        self.assertEqual(db.as_manifest(manifest_file), manifest)
        self.assertEqual(db.as_manifest(manifest.path), manifest)
        self.assertEqual(db.as_manifest(manifest), manifest)

    def test_list_backups_is_newest_first_and_skips_broken_manifests(self):
        db.migrate(self.db_path)
        first = db.create_backup(self.db_path, self.home / "backups", name="one.bak", now=1000.0)
        second = db.create_backup(self.db_path, self.home / "backups", name="two.bak", now=2000.0)
        db.write_json(db.manifest_path(self.home / "backups" / "broken.bak"), {"nonsense": True})
        found = db.list_backups(self.home / "backups")
        self.assertEqual([item.path for item in found], [second.path, first.path])
        self.assertEqual(db.list_backups(self.home / "nowhere"), [])

    def test_backup_refuses_a_missing_source_and_an_open_transaction(self):
        with self.assertRaises(db.BackupError):
            db.create_backup(self.home / "missing.sqlite3", self.home / "backups")
        db.migrate(self.db_path)
        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        conn.execute("BEGIN IMMEDIATE")
        with self.assertRaises(db.BackupError):
            db.create_backup(conn, self.home / "backups")
        conn.execute("ROLLBACK")

    def test_backup_of_a_wal_database_includes_uncommitted_wal_pages(self):
        db.migrate(self.db_path)
        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        conn.execute("INSERT INTO candidates(proxy, endpoint_id) VALUES (?,?)",
                     (LEGACY_PROXY, db.endpoint_id(LEGACY_PROXY)))
        conn.commit()
        manifest = db.create_backup(conn, self.home / "backups")
        copy = db.connect(manifest.path, read_only=True)
        self.addCleanup(copy.close)
        self.assertEqual(copy.execute("SELECT count(*) FROM candidates").fetchone()[0], 1)

    # --- restore preview ----------------------------------------------------

    def test_restore_preview_writes_nothing_and_describes_the_copy(self):
        legacy_database(self.db_path)
        db.migrate(self.db_path)
        target = self.home / "restored"

        preview = db.restore_preview(self.db_path, target)
        self.assertFalse(preview.applied)
        self.assertTrue(preview.ok)
        self.assertFalse(target.exists())
        self.assertEqual(preview.files[0][0], db.DB_FILENAME)
        self.assertEqual(preview.files[0][2], db.sha256_file(self.db_path))
        self.assertEqual(preview.total_bytes, sum(size for _n, size, _d in preview.files))
        self.assertEqual(preview.to_dict()["ok"], True)

    def test_restore_without_apply_returns_the_preview_and_creates_nothing(self):
        legacy_database(self.db_path)
        db.migrate(self.db_path)
        target = self.home / "restored"
        report = db.restore(self.db_path, target)
        self.assertFalse(report.preview.applied)
        self.assertFalse(report.verified)
        self.assertIsNone(report.manifest)
        self.assertIn("preview", report.note)
        self.assertFalse(target.exists())

    def test_restore_refuses_a_non_empty_target(self):
        db.migrate(self.db_path)
        target = self.home / "restored"
        target.mkdir()
        (target / "gui-settings.json").write_text("{}", encoding="utf-8")
        preview = db.restore_preview(self.db_path, target)
        self.assertFalse(preview.ok)
        self.assertEqual(preview.conflicts, ("gui-settings.json",))
        with self.assertRaises(db.BackupError):
            db.restore(self.db_path, target, apply=True)
        self.assertEqual((target / "gui-settings.json").read_text(encoding="utf-8"), "{}")

    def test_restore_refuses_the_current_data_path(self):
        db.migrate(self.db_path)
        with self.assertRaises(db.BackupError) as caught:
            db.restore_preview(self.db_path, self.data)
        self.assertEqual(caught.exception.code, db.E_PATH_CONFLICT)
        self.assertEqual(db.probe(self.db_path)[0], db.SCHEMA_VERSION)

    def test_restore_into_a_new_path_keeps_the_current_database_intact(self):
        legacy_database(self.db_path)
        db.migrate(self.db_path)
        digest = db.sha256_file(self.db_path)
        target = self.home / "restored"

        report = db.restore(self.db_path, target, apply=True)
        self.assertTrue(report.preview.applied)
        self.assertTrue(report.verified)
        self.assertEqual(report.manifest.schema_version, db.SCHEMA_VERSION)
        self.assertTrue((target / db.DB_FILENAME).is_file())
        restored = db.connect(target / db.DB_FILENAME, read_only=True)
        self.addCleanup(restored.close)
        self.assertEqual(restored.execute("SELECT count(*) FROM candidates").fetchone()[0], 1)
        self.assertEqual(db.sha256_file(self.db_path), digest)

    def test_restore_reads_a_data_folder_or_a_file_alike(self):
        db.migrate(self.db_path)
        target = self.home / "restored"
        from_folder = db.restore(self.data, target, apply=True)
        self.assertTrue(from_folder.verified)
        self.assertEqual(from_folder.manifest.source_path, str(self.db_path))

    def test_restore_of_a_corrupt_source_is_refused_by_the_reader(self):
        broken = self.home / "broken.sqlite3"
        broken.write_bytes(b"not a database" * 40)
        preview = db.restore_preview(broken, self.home / "restored")
        self.assertEqual(preview.secrets, ())
        with self.assertRaises(db.BackupError):
            db.restore(broken, self.home / "restored", apply=True)

    # --- rollback -----------------------------------------------------------

    def test_rollback_of_a_legacy_backup_returns_the_file_as_it_was(self):
        legacy_database(self.db_path)
        report = db.migrate(self.db_path)
        target = self.home / "rolled-back"

        result = db.rollback(report.backup.path, target, apply=True)
        self.assertTrue(result.verified)
        self.assertEqual(result.preview.reason, "rollback")
        self.assertEqual(db.probe(target)[0], 0)
        conn = db.connect(target, read_only=True)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT payload FROM results").fetchone()[0], '{"score": 80}')
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'endpoints'").fetchone())
        # The migrated database is still there and still current.
        self.assertEqual(db.probe(self.db_path)[0], db.SCHEMA_VERSION)

    # --- data path migration -------------------------------------------------

    def test_migrate_data_path_previews_then_copies_with_a_backup_of_the_old_database(self):
        legacy_database(self.db_path)
        db.migrate(self.db_path)
        (self.data / "gui-settings.json").write_text('{"lang": "ru"}', encoding="utf-8")
        target = self.home / "new-location"

        preview = db.migrate_data_path(self.data, target)
        self.assertFalse(preview.preview.applied)
        self.assertFalse(target.exists())

        result = db.migrate_data_path(self.data, target, apply=True)
        self.assertTrue(result.manifest.reason, "pre-data-path-move")
        self.assertTrue((target / db.DB_FILENAME).is_file())
        self.assertTrue((target / "gui-settings.json").is_file())
        self.assertEqual(db.probe(target)[0], db.SCHEMA_VERSION)
        backups = db.list_backups(target / db.BACKUP_DIRNAME)
        self.assertEqual(len(backups), 1)
        self.assertTrue(db.verify_backup(backups[0]))
        # The old folder is left alone: deleting it stays an explicit user action.
        self.assertTrue(self.db_path.is_file())
        self.assertIn("old data path kept", result.note)

    def test_migrate_data_path_refuses_to_move_onto_itself(self):
        db.migrate(self.db_path)
        with self.assertRaises(db.BackupError) as caught:
            db.migrate_data_path(self.data, self.data, apply=True)
        self.assertEqual(caught.exception.code, db.E_PATH_CONFLICT)
        self.assertEqual(db.probe(self.db_path)[0], db.SCHEMA_VERSION)

    def test_migrate_data_path_without_a_database_is_refused(self):
        (self.home / "empty").mkdir()
        with self.assertRaises(db.BackupError):
            db.migrate_data_path(self.home / "empty", self.home / "new", apply=True)


if __name__ == '__main__':
    unittest.main()
