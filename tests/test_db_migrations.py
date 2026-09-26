"""Migrations of proxy_workbench/db.py: versioning, the legacy scenario, rollback."""
from pathlib import Path
import json
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import db
from tests.workbench_support import legacy_database as legacy_file  # noqa: E402

#: Every table CONTRACTS §3.3 declares, including the five pre-versioning ones.
CONTRACT_TABLES = (
    "candidates", "candidate_meta", "candidate_seen", "results", "profiles",
    "schema_migrations", "endpoints", "collections", "membership", "accesses",
    "observations", "job", "job_item", "job_event", "checkpoint", "pools",
    "pool_member", "schedules", "schedule_run", "api_keys", "audit_log",
    "export_artifact", "import_batch",
)

LEGACY_ROWS = (
    "http://1.2.3.4:8080",
    "socks5://5.6.7.8:1080",
    "https://9.9.9.9:3128",
)


def legacy_database(path, *, results=True):
    """A database written by the unversioned engine, with real rows.

    The engine no longer creates this shape itself -- it migrates instead -- so
    the fixture builds the pre-versioning file directly.
    """
    conn = legacy_file(Path(path))
    for proxy in LEGACY_ROWS:
        conn.execute("INSERT INTO candidates VALUES (?)", (proxy,))
        conn.execute("INSERT INTO candidate_seen VALUES (?, ?)", (proxy, "list-1"))
    conn.execute("INSERT INTO candidate_meta(proxy, country, source)"
                 " VALUES (?,?,?)", (LEGACY_ROWS[0], "DE", "list-1"))
    conn.execute("INSERT INTO profiles VALUES (?,?)", ("profile0001", '{"targets": 1}'))
    if results:
        conn.execute("INSERT INTO results VALUES (?,?,?)",
                     ("profile0001", LEGACY_ROWS[0], '{"score": 80}'))
        conn.execute("INSERT INTO results VALUES (?,?,?)",
                     ("profile0001", LEGACY_ROWS[1], '{"score": 40}'))
    conn.commit()
    conn.close()
    return Path(path)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.db_path = self.home / "data" / db.DB_FILENAME
        self.addCleanup(self.temp.cleanup)

    # --- 1. a fresh database lands exactly on the contract version -----------

    def test_new_database_lands_on_the_contract_version(self):
        report = db.migrate(self.db_path, app_version="test")
        self.assertEqual(report.status, "created")
        self.assertEqual(report.schema_version, db.SCHEMA_VERSION)
        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        self.assertEqual(db.read_header(conn), (db.SCHEMA_VERSION, db.APPLICATION_ID))
        self.assertEqual(sorted(db.tables(conn)), sorted(CONTRACT_TABLES))
        applied = [row[0] for row in conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version")]
        self.assertEqual(applied, list(range(db.SCHEMA_VERSION + 1)))
        self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)

    def test_migration_list_is_ordered_and_complete(self):
        self.assertEqual([m.version for m in db.MIGRATIONS],
                         list(range(db.SCHEMA_VERSION + 1)))
        self.assertEqual(len({m.name for m in db.MIGRATIONS}), len(db.MIGRATIONS))

    # --- 2. the real scenario: a legacy file opens and migrates -------------

    def test_legacy_database_without_version_opens_and_migrates(self):
        legacy_database(self.db_path)
        self.assertEqual(db.probe(self.db_path), (0, 0))

        report = db.migrate(self.db_path, app_version="test")
        self.assertEqual(report.status, "migrated")
        self.assertEqual(report.previous_version, 0)
        self.assertEqual([item[0] for item in report.applied], list(range(db.SCHEMA_VERSION + 1)))
        self.assertEqual(report.legacy_candidates, len(LEGACY_ROWS))

        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        self.assertEqual(db.primary_key(conn, "results"),
                         list(db.RESULTS_KEY))
        # Legacy data survives: candidates, results and profiles are still there.
        self.assertEqual(conn.execute("SELECT count(*) FROM candidates").fetchone()[0],
                         len(LEGACY_ROWS))
        self.assertEqual(conn.execute("SELECT count(*) FROM results").fetchone()[0], 2)
        self.assertEqual(conn.execute("SELECT name FROM profiles").fetchone()[0], None)
        self.assertEqual(conn.execute("SELECT digest FROM profiles").fetchone()[0],
                         "profile0001")
        # Every legacy address became an endpoint with an honest origin.
        members = db.collection_members(conn, db.LEGACY_COLLECTION_ID)
        self.assertEqual([row["canonical"] for row in members], sorted(LEGACY_ROWS))
        self.assertEqual({row["origin"] for row in members}, {"legacy"})
        self.assertEqual(db.legacy_summary(conn)["endpoints"], len(LEGACY_ROWS))
        # The measured row kept its payload and gained a real endpoint reference.
        row = conn.execute("SELECT * FROM results WHERE profile = ?", ("profile0001",)).fetchone()
        self.assertEqual(row["profile_id"], "profile0001")
        self.assertEqual(row["endpoint_id"], db.endpoint_id(LEGACY_ROWS[0]))
        # The recovered identity reached the payload, which is the only thing
        # `export()` and the GUI table read.  Without this the row failed admission
        # on a collection it was never measured in, and every historic result was
        # lost from the export until a full recheck (F02, F24).
        payload = json.loads(row["payload"])
        self.assertEqual(payload["score"], 80)
        self.assertEqual(payload["proxy"], LEGACY_ROWS[0])
        self.assertEqual(payload["profile_id"], "profile0001")
        self.assertEqual(payload["profile_revision"], 1)
        self.assertEqual(payload["access_id"], db.PUBLIC_ACCESS_ID)
        self.assertEqual(payload["access_revision"], 1)
        self.assertEqual(payload["collection_id"], db.LEGACY_COLLECTION_ID)
        # What a 2.x file never recorded stays absent: no invented network, no
        # invented lifetime.
        self.assertNotIn("network_id", payload)
        self.assertIsNone(row["valid_until"])
        self.assertIn(row["endpoint_id"],
                      [endpoint[0] for endpoint in conn.execute("SELECT id FROM endpoints")])

    def test_legacy_candidates_land_in_their_own_collection_not_in_the_public_base(self):
        legacy_database(self.db_path)
        db.migrate(self.db_path)
        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        self.assertEqual(db.collection_members(conn, db.PUBLIC_COLLECTION_ID), [])
        self.assertEqual(db.get_collection(conn, db.LEGACY_COLLECTION_ID)["kind"], "public")

    # --- 3. migrating again changes nothing ---------------------------------

    def test_repeated_migration_changes_nothing(self):
        db.migrate(self.db_path)
        conn = db.connect(self.db_path)
        before_schema = db.describe(conn)
        before_rows = conn.execute("SELECT count(*) FROM results").fetchone()[0]
        conn.close()
        digest = db.sha256_file(self.db_path)
        backups_before = sorted(item.name for item in (self.home / "data" / db.BACKUP_DIRNAME)
                                .iterdir()) if (self.home / "data" / db.BACKUP_DIRNAME).is_dir() else []

        report = db.migrate(self.db_path)
        self.assertEqual(report.status, "current")
        self.assertEqual(report.applied, ())
        self.assertIsNone(report.backup)
        self.assertEqual(db.sha256_file(self.db_path), digest)

        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        self.assertEqual(db.describe(conn), before_schema)
        self.assertEqual(conn.execute("SELECT count(*) FROM results").fetchone()[0], before_rows)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
        backup_dir = self.home / "data" / db.BACKUP_DIRNAME
        after = sorted(item.name for item in backup_dir.iterdir()) if backup_dir.is_dir() else []
        self.assertEqual(after, backups_before)

    def test_second_migration_of_a_legacy_file_takes_no_second_backup(self):
        legacy_database(self.db_path)
        first = db.migrate(self.db_path)
        self.assertIsNotNone(first.backup)
        backups = db.list_backups(self.home / "data" / db.BACKUP_DIRNAME)
        self.assertEqual(len(backups), 1)
        self.assertEqual(db.migrate(self.db_path).status, "current")
        self.assertEqual(len(db.list_backups(self.home / "data" / db.BACKUP_DIRNAME)), 1)

    # --- 4. a database from the future is refused without a single write ----

    def test_database_from_the_future_is_refused_and_never_written(self):
        db.migrate(self.db_path)
        conn = db.connect(self.db_path)
        conn.execute(f"PRAGMA user_version={db.SCHEMA_VERSION + 1}")
        conn.close()
        digest = db.sha256_file(self.db_path)

        with self.assertRaises(db.DbVersionError) as caught:
            db.migrate(self.db_path)
        self.assertEqual(caught.exception.code, db.E_VERSION_AHEAD)
        self.assertEqual(db.sha256_file(self.db_path), digest)
        self.assertEqual(db.probe(self.db_path)[0], db.SCHEMA_VERSION + 1)
        self.assertEqual(db.list_backups(self.home / "data" / db.BACKUP_DIRNAME), [])

    def test_assert_current_refuses_an_unmigrated_or_a_newer_file(self):
        legacy_database(self.db_path)
        conn = db.connect(self.db_path, read_only=True)
        with self.assertRaises(db.DbVersionError) as caught:
            db.assert_current(conn)
        self.assertEqual(caught.exception.code, db.E_MIGRATION_FAILED)
        conn.close()
        db.migrate(self.db_path)
        conn = db.connect(self.db_path)
        conn.execute(f"PRAGMA user_version={db.SCHEMA_VERSION + 3}")
        with self.assertRaises(db.DbVersionError) as caught:
            db.assert_current(conn)
        self.assertEqual(caught.exception.code, db.E_VERSION_AHEAD)
        conn.close()

    # --- 5. a foreign application_id is refused ------------------------------

    def test_foreign_application_id_is_refused_and_never_written(self):
        db.migrate(self.db_path)
        conn = db.connect(self.db_path)
        conn.execute("PRAGMA application_id=123456789")
        conn.close()
        digest = db.sha256_file(self.db_path)

        with self.assertRaises(db.DbForeignError) as caught:
            db.migrate(self.db_path)
        self.assertEqual(caught.exception.code, db.E_FOREIGN_DB)
        self.assertEqual(db.sha256_file(self.db_path), digest)
        self.assertEqual(db.probe(self.db_path)[1], 123456789)

    def test_magic_application_id_without_version_is_recognised_as_ours(self):
        legacy_database(self.db_path)
        conn = db.connect(self.db_path)
        conn.execute(f"PRAGMA application_id={db.APPLICATION_ID}")
        conn.close()
        report = db.migrate(self.db_path)
        self.assertEqual(report.status, "migrated")
        self.assertEqual(report.previous_version, 0)

    def test_corrupt_file_is_refused_instead_of_recreated(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path.write_bytes(b"definitely not a sqlite database" * 64)
        digest = db.sha256_file(self.db_path)
        with self.assertRaises(db.DbCorruptError) as caught:
            db.migrate(self.db_path)
        self.assertEqual(caught.exception.code, db.E_DB_CORRUPT)
        self.assertEqual(db.sha256_file(self.db_path), digest)
        with self.assertRaises(db.DbCorruptError):
            db.probe(self.db_path)

    # --- 6. the pre-migration backup and the rollback -----------------------

    def test_pre_migration_backup_and_manifest_are_verifiable(self):
        legacy_database(self.db_path)
        report = db.migrate(self.db_path)
        manifest = report.backup
        self.assertIsNotNone(manifest)
        self.assertTrue(Path(manifest.path).is_file())
        self.assertEqual(manifest.sha256, db.sha256_file(manifest.path))
        self.assertEqual(manifest.bytes, Path(manifest.path).stat().st_size)
        self.assertEqual(manifest.schema_version, 0)
        self.assertEqual(manifest.reason, "pre-migration")
        self.assertIn(manifest.tool, ("VACUUM INTO", "backup API"))
        self.assertTrue(db.verify_backup(manifest))
        on_disk = db.BackupManifest.from_dict(db.read_json(db.manifest_path(manifest.path)))
        self.assertEqual(on_disk, manifest)
        # The backup is a readable legacy database, not a copy of a WAL file.
        self.assertEqual(db.probe(manifest.path), (0, 0))
        backup_conn = sqlite3.connect(manifest.path)
        self.addCleanup(backup_conn.close)
        self.assertEqual(backup_conn.execute("SELECT count(*) FROM candidates").fetchone()[0],
                         len(LEGACY_ROWS))
        # And the journal records which backup protected this migration.
        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        row = conn.execute("SELECT backup_path FROM schema_migrations WHERE version=0").fetchone()
        self.assertEqual(row[0], manifest.path)

    def test_backup_refuses_to_overwrite_an_existing_artifact(self):
        legacy_database(self.db_path)
        target = self.home / "backups" / "fixed.bak"
        first = db.create_backup(self.db_path, self.home / "backups", name="fixed.bak")
        self.assertEqual(Path(first.path), target)
        with self.assertRaises(db.BackupError) as caught:
            db.create_backup(self.db_path, self.home / "backups", name="fixed.bak")
        self.assertEqual(caught.exception.code, db.E_BACKUP_FAILED)

    def test_tampered_backup_fails_verification(self):
        legacy_database(self.db_path)
        manifest = db.create_backup(self.db_path, self.home / "backups", name="tamper.bak")
        with Path(manifest.path).open("ab") as handle:
            handle.write(b"tampered")
        self.assertFalse(db.verify_backup(manifest))
        with self.assertRaises(db.BackupError):
            db.rollback(manifest, self.home / "restored", apply=True)

    def test_rollback_restores_the_pre_migration_backup_into_a_new_data_path(self):
        legacy_database(self.db_path)
        report = db.migrate(self.db_path)
        migrated_digest = db.sha256_file(self.db_path)

        target = self.home / "rollback-data"
        restored = db.rollback(report.backup, target, apply=True)
        self.assertTrue(restored.verified)
        self.assertTrue((target / db.DB_FILENAME).is_file())
        self.assertEqual(db.probe(target)[0], 0)
        restored_conn = db.connect(target / db.DB_FILENAME, read_only=True)
        self.addCleanup(restored_conn.close)
        self.assertEqual(restored_conn.execute("SELECT count(*) FROM candidates").fetchone()[0],
                         len(LEGACY_ROWS))
        self.assertEqual(restored_conn.execute("SELECT payload FROM results WHERE proxy = ?",
                                               (LEGACY_ROWS[0],)).fetchone()[0], '{"score": 80}')
        # The migrated database is kept, not overwritten by the rollback.
        self.assertEqual(db.probe(self.db_path)[0], db.SCHEMA_VERSION)
        self.assertEqual(db.sha256_file(self.db_path), migrated_digest)

    def test_rollback_without_apply_writes_nothing(self):
        legacy_database(self.db_path)
        report = db.migrate(self.db_path)
        target = self.home / "rollback-preview"
        result = db.rollback(report.backup, target)
        self.assertFalse(result.preview.applied)
        self.assertFalse(target.exists())
        self.assertEqual(result.preview.files[0][0], db.DB_FILENAME)
        self.assertTrue(db.verify_backup(report.backup))

    # --- 7. the rebuilt key keeps two access revisions apart ---------------

    def test_rebuilt_key_separates_two_access_revisions_of_one_address(self):
        db.migrate(self.db_path)
        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        endpoint = db.upsert_endpoint(conn, "http://1.2.3.4:8080")
        conn.execute("INSERT INTO accesses(id, endpoint_id, mode, access_revision, created_at)"
                     " VALUES (?,?,?,?,?)", ("acc-1", endpoint, "private", 1, 1.0))
        for revision in (1, 2):
            conn.execute(
                "INSERT INTO results(profile, proxy, payload, endpoint_id, access_id,"
                " access_revision, profile_id, profile_revision, job_id, checked_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("p", "http://1.2.3.4:8080", f"{{\"rev\": {revision}}}", endpoint,
                 "acc-1", revision, "p", 1, "job-1", 1000.0 + revision))
        rows = conn.execute("SELECT access_revision, payload FROM results"
                            " WHERE endpoint_id = ? ORDER BY access_revision", (endpoint,)).fetchall()
        self.assertEqual([row["access_revision"] for row in rows], [1, 2])
        self.assertEqual([json_payload(row) for row in rows],
                         ['{"rev": 1}', '{"rev": 2}'])
        # A repeat measurement in a new job replaces the row of the same
        # (profile, access, endpoint): one address is one row, so a fresh failure
        # cannot leave a stale success standing beside it in every artifact
        # (F28, F09).  The per-job item is `job_item(job_id, item_id)`, not this key.
        conn.execute(
            "INSERT OR REPLACE INTO results(profile, proxy, payload, endpoint_id, access_id,"
            " access_revision, profile_id, profile_revision, job_id, checked_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("p", "http://1.2.3.4:8080", '{"rev": 3}', endpoint, "acc-1", 1, "p", 1, "job-2", 1002.0))
        self.assertEqual(conn.execute("SELECT count(*) FROM results").fetchone()[0], 2)
        remeasured = conn.execute("SELECT job_id, payload FROM results"
                                  " WHERE access_revision = 1").fetchone()
        self.assertEqual(remeasured["job_id"], "job-2")
        self.assertEqual(json_payload(remeasured), '{"rev": 3}')

    def test_old_key_would_have_collapsed_those_rows(self):
        legacy_database(self.db_path, results=False)
        conn = db.connect(self.db_path)
        # This is what `store()` does today: one row per (profile, proxy), replaced.
        conn.execute("INSERT OR REPLACE INTO results VALUES (?,?,?)",
                     ("p", "http://1.2.3.4:8080", '{"rev": 1}'))
        conn.execute("INSERT OR REPLACE INTO results VALUES (?,?,?)",
                     ("p", "http://1.2.3.4:8080", '{"rev": 2}'))
        self.assertEqual(conn.execute("SELECT count(*) FROM results").fetchone()[0], 1)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO results VALUES (?,?,?)",
                         ("p", "http://1.2.3.4:8080", '{"rev": 1}'))
        conn.close()

        db.migrate(self.db_path)
        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        conn.execute(
            "INSERT INTO results(profile, proxy, payload, endpoint_id, access_id,"
            " access_revision, profile_id, profile_revision, job_id)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            ("p", "http://1.2.3.4:8080", '{"rev": 1}', db.endpoint_id("http://1.2.3.4:8080"),
             "acc-1", 1, "p", 1, "job-1"))
        self.assertEqual(conn.execute("SELECT count(*) FROM results").fetchone()[0], 2)

    # --- 8. every migration is idempotent and transactional ----------------

    def test_every_migration_is_idempotent(self):
        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        db.migrate(self.db_path)
        reference = db.describe(conn)
        for migration in db.MIGRATIONS:
            context = db._MigrationContext(now=1000.0)
            for _ in range(2):
                conn.execute("BEGIN IMMEDIATE")
                migration.apply(conn, context)
                conn.execute("COMMIT")
        self.assertEqual(db.describe(conn), reference)
        conn.execute("INSERT INTO candidates(proxy, endpoint_id) VALUES (?,?)",
                     ("http://2.2.2.2:80", db.endpoint_id("http://2.2.2.2:80")))
        conn.execute("INSERT OR IGNORE INTO candidates(proxy, endpoint_id) VALUES (?,?)",
                     ("http://2.2.2.2:80", db.endpoint_id("http://2.2.2.2:80")))
        self.assertEqual(conn.execute("SELECT count(*) FROM candidates").fetchone()[0], 1)

    def test_a_failing_migration_leaves_the_previous_version_in_place(self):
        db.migrate(self.db_path)
        conn = db.connect(self.db_path)
        conn.execute(f"PRAGMA user_version={db.SCHEMA_VERSION - 1}")
        conn.execute("DROP TABLE IF EXISTS canary_table")
        conn.close()

        original = db.MIGRATIONS
        exploding = db.Migration(db.SCHEMA_VERSION, "explodes", _explode)

        def broken(conn, context):
            conn.execute("CREATE TABLE half_applied(a)")
            _explode(conn, context)

        db.MIGRATIONS = original[:-1] + (db.Migration(db.SCHEMA_VERSION, "explodes", broken),)
        self.addCleanup(setattr, db, "MIGRATIONS", original)
        with self.assertRaises(db.DbError) as caught:
            db.migrate(self.db_path)
        self.assertEqual(caught.exception.code, db.E_MIGRATION_FAILED)
        self.assertIn("explodes", caught.exception.message)

        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0],
                         db.SCHEMA_VERSION - 1)
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'half_applied'").fetchone())
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")


def _explode(conn, context):
    raise sqlite3.OperationalError("simulated failure inside a migration")


def json_payload(row):
    return row["payload"]


if __name__ == '__main__':
    unittest.main()
