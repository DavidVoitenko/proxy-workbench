"""Migrations 16..18 as migrations: the version sequence, the old file, the backup.

Everything here runs `db.migrate()` on a real temporary file.  The properties
under test are the ones a user only finds out about when they are broken:
that a re-run changes nothing, that a file from an older build still opens, that
an older build cannot write into the new schema, that the pre-migration copy
still fires for the versions that need it, and that none of it needs the whole
database in memory.
"""
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import tracemalloc
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import db  # noqa: E402

#: The version this area's migrations start at.  A file below it must still open.
FIRST_NEW_MIGRATION = 16
#: The version the branch carried before this area: a file written by that build
#: is the realistic "old database" and has to open.
PREVIOUS_HEAD = 15
#: A realistic row count for the memory measurement: large enough that a migration
#: which rewrote a table would have to hold it, small enough to build in a test.
BULK_ROWS = 200_000


def migrate_upto(path, version, **kwargs):
    """Migrate with the migration list and version pinned to `version`.

    This is how a genuine old file is produced: the real migrations, run in the
    real order, writing the real `user_version` and the real journal rows -- not
    a hand-written `PRAGMA user_version` over a database that never had the
    earlier migrations.
    """
    original_migrations, original_version = db.MIGRATIONS, db.SCHEMA_VERSION
    db.MIGRATIONS = tuple(item for item in original_migrations if item.version <= version)
    db.SCHEMA_VERSION = version
    try:
        return db.migrate(path, **kwargs)
    finally:
        db.MIGRATIONS, db.SCHEMA_VERSION = original_migrations, original_version


class MigrationSequenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "data" / db.DB_FILENAME

    def test_the_new_migrations_continue_the_sequence_rather_than_reusing_one(self):
        # "does not skip and does not reuse" -- the previous head was 15, so the
        # new versions are exactly 16, 17, 18.
        self.assertEqual([item.version for item in db.MIGRATIONS[-3:]],
                         [FIRST_NEW_MIGRATION, FIRST_NEW_MIGRATION + 1,
                          FIRST_NEW_MIGRATION + 2])
        self.assertEqual(PREVIOUS_HEAD, FIRST_NEW_MIGRATION - 1)
        self.assertEqual(db.SCHEMA_VERSION, FIRST_NEW_MIGRATION + 2)

    def test_a_fresh_file_runs_every_migration_in_one_go(self):
        report = db.migrate(self.path, app_version="test")
        self.assertEqual(report.status, "created")
        self.assertEqual(report.previous_version, 0)
        self.assertEqual([item[0] for item in report.applied], list(range(db.SCHEMA_VERSION + 1)))
        conn = db.connect(self.path)
        self.addCleanup(conn.close)
        self.assertEqual(db.read_header(conn), (db.SCHEMA_VERSION, db.APPLICATION_ID))
        self.assertEqual([row[0] for row in conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version")],
            list(range(db.SCHEMA_VERSION + 1)))

    def test_the_sequence_is_continuous_and_every_version_has_its_own_name(self):
        self.assertEqual([item.version for item in db.MIGRATIONS],
                         list(range(db.SCHEMA_VERSION + 1)))
        names = [item.name for item in db.MIGRATIONS]
        self.assertEqual(len(set(names)), len(names))

    def test_migrating_twice_changes_nothing_at_all(self):
        db.migrate(self.path, app_version="test")
        before = self.path.read_bytes()
        report = db.migrate(self.path, app_version="test")
        self.assertEqual(report.status, "current")
        self.assertEqual(report.applied, ())
        self.assertEqual(report.previous_version, db.SCHEMA_VERSION)
        conn = db.connect(self.path)
        self.addCleanup(conn.close)
        self.assertEqual(db.read_header(conn), (db.SCHEMA_VERSION, db.APPLICATION_ID))
        self.assertEqual(self.path.read_bytes(), before)

    def test_a_migration_at_the_files_own_version_runs_again_and_must_be_a_no_op(self):
        # `migrate` applies every migration whose version is >= `user_version`,
        # so the newest applied one runs again on the next call.  That is why
        # every migration has to be idempotent; this pins the behaviour so a new
        # migration is written knowing it will be applied at least twice.
        migrate_upto(self.path, PREVIOUS_HEAD, app_version="old")
        first = db.migrate(self.path, app_version="test")
        self.assertEqual([item[0] for item in first.applied], [PREVIOUS_HEAD, 16, 17, 18])
        before = db.describe(db.connect(self.path))
        second = db.migrate(self.path, app_version="test")
        self.assertEqual(second.applied, ())
        conn = db.connect(self.path)
        self.addCleanup(conn.close)
        self.assertEqual(db.describe(conn), before)

    def test_every_migration_is_idempotent_on_its_own(self):
        """Each new migration applied twice must write the same schema."""
        db.migrate(self.path, app_version="test")
        conn = db.connect(self.path)
        self.addCleanup(conn.close)
        expected = db.describe(conn)
        for version in (FIRST_NEW_MIGRATION, FIRST_NEW_MIGRATION + 1, FIRST_NEW_MIGRATION + 2):
            migration = db.MIGRATIONS[version]
            with self.subTest(version=version, name=migration.name):
                conn.execute("BEGIN IMMEDIATE")
                migration.apply(conn, db._MigrationContext(now=time.time()))
                conn.execute("COMMIT")
                self.assertEqual(db.describe(conn), expected)

    def test_a_migration_failure_leaves_the_previous_version_and_the_schema(self):
        migrate_upto(self.path, db.SCHEMA_VERSION - 1, app_version="old")
        before = db.describe(db.connect(self.path))
        conn = db.connect(self.path)
        self.addCleanup(conn.close)

        def explode(conn_, context):
            raise sqlite3.OperationalError("simulated failure")

        original = db.MIGRATIONS
        db.MIGRATIONS = original[:-1] + (db.Migration(db.SCHEMA_VERSION, "explodes", explode),)
        self.addCleanup(setattr, db, "MIGRATIONS", original)
        with self.assertRaises(db.DbError):
            db.migrate(self.path, app_version="test")
        # One transaction per migration: the file is left at the version before
        # the one that failed, with the schema that version produced.
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION - 1)
        self.assertEqual(db.describe(conn), before)


class OldDatabaseUpgradeTests(unittest.TestCase):
    """A file a previous build wrote has to open, migrate and keep its data."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "data" / db.DB_FILENAME

    def populate(self):
        """Real rows in the tables this area's migrations must not damage."""
        conn = db.connect(self.path)
        self.addCleanup(conn.close)
        for index in range(5):
            canonical = f"http://198.51.100.{index + 1}:8080"
            conn.execute("INSERT OR IGNORE INTO endpoints(id, canonical) VALUES (?,?)",
                         (db.endpoint_id(canonical), canonical))
            db.add_member(conn, db.PUBLIC_COLLECTION_ID, db.endpoint_id(canonical), now=1.0)
        conn.execute("INSERT INTO pools(id, collection_id, desired) VALUES ('p1', ?, 1)",
                     (db.PUBLIC_COLLECTION_ID,))
        conn.execute("INSERT INTO schedules(id, pool_id, kind, interval_minutes, enabled)"
                     " VALUES ('s1', 'p1', 'interval', 60, 1)")

    def test_a_file_from_the_previous_version_opens_and_keeps_its_data(self):
        migrate_upto(self.path, PREVIOUS_HEAD, app_version="old")
        self.populate()
        conn = db.connect(self.path)
        self.assertNotIn("source_feed", db.tables(conn))
        self.assertNotIn("paused", db.columns(conn, "schedules"))
        self.assertNotIn("candidate_scope_exclusion", db.tables(conn))
        conn.close()

        report = db.migrate(self.path, app_version="test")
        self.assertEqual(report.status, "migrated")
        self.assertEqual(report.previous_version, PREVIOUS_HEAD)
        self.assertEqual([item[0] for item in report.applied], [15, 16, 17, 18])
        conn = db.connect(self.path)
        self.addCleanup(conn.close)
        self.assertIn("source_feed", db.tables(conn))
        self.assertIn("paused", db.columns(conn, "schedules"))
        # The rows the old build wrote are still there, with their values.
        self.assertEqual(conn.execute("SELECT count(*) FROM endpoints").fetchone()[0], 5)
        self.assertEqual(conn.execute("SELECT count(*) FROM membership").fetchone()[0], 5)
        self.assertEqual(conn.execute("SELECT enabled FROM schedules WHERE id='s1'").fetchone()[0], 1)
        # A row that predates the new columns reads as honestly "not paused".
        self.assertEqual(conn.execute("SELECT paused FROM schedules WHERE id='s1'").fetchone()[0], 0)
        self.assertIsNone(conn.execute(
            "SELECT counters_json FROM schedules WHERE id='s1'").fetchone()[0])

    def test_every_intermediate_version_opens(self):
        """The reported bug: files at user_version 1..12 were never updated."""
        for version in range(0, db.SCHEMA_VERSION):
            with self.subTest(version=version):
                target = Path(self.temp.name) / f"v{version}" / db.DB_FILENAME
                migrate_upto(target, version, app_version="old")
                self.assertEqual(db.probe(target)[0], version)
                report = db.migrate(target, app_version="test")
                self.assertEqual(report.schema_version, db.SCHEMA_VERSION)
                conn = db.connect(target)
                try:
                    self.assertIn("source_feed", db.tables(conn))
                    self.assertIn("paused", db.columns(conn, "schedules"))
                    self.assertIn("candidate_scope_exclusion", db.tables(conn))
                finally:
                    conn.close()

    def test_a_legacy_file_with_real_rows_still_migrates(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.executescript("""
            CREATE TABLE candidates(proxy TEXT PRIMARY KEY);
            CREATE TABLE candidate_meta(proxy TEXT PRIMARY KEY, country TEXT, source TEXT);
            CREATE TABLE profiles(id TEXT PRIMARY KEY, config TEXT NOT NULL);
            CREATE TABLE candidate_seen(proxy TEXT NOT NULL, source TEXT NOT NULL,
                PRIMARY KEY(proxy, source)) WITHOUT ROWID;
            CREATE TABLE results(profile TEXT NOT NULL, proxy TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(profile, proxy));""")
        conn.execute("INSERT INTO candidates VALUES ('http://1.2.3.4:8080')")
        conn.execute("INSERT INTO candidate_seen VALUES ('http://1.2.3.4:8080', 'list-1')")
        conn.execute("INSERT INTO profiles VALUES ('p000001', '{}')")
        conn.execute("INSERT INTO results VALUES ('p000001', 'http://1.2.3.4:8080', '{}')")
        conn.commit()
        conn.close()

        report = db.migrate(self.path, app_version="test")
        self.assertEqual(report.previous_version, 0)
        self.assertEqual(report.legacy_candidates, 1)
        conn = db.connect(self.path)
        self.addCleanup(conn.close)
        self.assertIn("source_feed", db.tables(conn))
        self.assertEqual(conn.execute("SELECT count(*) FROM membership_source").fetchone()[0], 0)
        self.assertEqual(db.legacy_summary(conn)["candidates"], 1)


class OldBinaryRefusalTests(unittest.TestCase):
    """An older build must not write into a schema it does not know."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "data" / db.DB_FILENAME
        db.migrate(self.path, app_version="test")

    def test_a_build_that_knows_an_older_version_refuses_the_new_file(self):
        # This is the "old binary must not write into the new schema" guard: a
        # build that knows fewer migrations sees a file ahead of it and is
        # refused with the code that means exactly that, before any write.
        conn = db.connect(self.path)
        self.addCleanup(conn.close)
        original = db.SCHEMA_VERSION
        db.SCHEMA_VERSION = PREVIOUS_HEAD
        self.addCleanup(setattr, db, "SCHEMA_VERSION", original)
        with self.assertRaises(db.DbVersionError) as caught:
            db.assert_current(conn)
        self.assertEqual(caught.exception.code, db.E_VERSION_AHEAD)
        self.assertIn(f"this program knows {PREVIOUS_HEAD}", str(caught.exception))

    def test_a_build_that_knows_a_newer_version_refuses_to_open_an_older_file(self):
        conn = db.connect(self.path)
        self.addCleanup(conn.close)
        original = db.SCHEMA_VERSION
        db.SCHEMA_VERSION = db.SCHEMA_VERSION + 1
        self.addCleanup(setattr, db, "SCHEMA_VERSION", original)
        with self.assertRaises(db.DbVersionError) as caught:
            db.assert_current(conn)
        self.assertEqual(caught.exception.code, db.E_MIGRATION_FAILED)
        self.assertIn("migrate()", str(caught.exception))

    def test_open_db_refuses_the_file_instead_of_handing_back_a_connection(self):
        current = db.SCHEMA_VERSION
        original = db.SCHEMA_VERSION
        db.SCHEMA_VERSION = PREVIOUS_HEAD
        self.addCleanup(setattr, db, "SCHEMA_VERSION", original)
        with self.assertRaises(db.DbVersionError):
            db.open_db(self.path)
        # The file itself is untouched by the refusal: no downgrade, no write.
        self.assertEqual(db.probe(self.path), (current, db.APPLICATION_ID))


class BackupTriggerTests(unittest.TestCase):
    """The pre-migration copy has to fire for the versions that need it.

    The bug this guards is named in the module: backing up only a version-0
    legacy file left every intermediate build of this branch (``user_version``
    1..15) rewriting ``results`` with nothing to check the result against.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    def test_a_version_that_still_runs_a_destructive_migration_is_copied(self):
        for version in range(0, FIRST_NEW_MIGRATION):
            if not (set(item.version for item in db.MIGRATIONS if item.version >= version)
                    & db.DESTRUCTIVE_MIGRATIONS):
                continue
            with self.subTest(version=version):
                target = self.home / f"v{version}" / db.DB_FILENAME
                migrate_upto(target, version, app_version="old")
                report = db.migrate(target, app_version="test")
                self.assertIsNotNone(report.backup, f"version {version} was not backed up")
                self.assertTrue(db.verify_backup(report.backup))
                self.assertEqual(report.backup.schema_version, version)
                manifest = db.read_json(db.manifest_path(report.backup.path))
                self.assertEqual(manifest["schema_version"], version)
                self.assertEqual(len(manifest["sha256"]), 64)

    def test_a_version_that_only_needs_additive_migrations_is_not_copied(self):
        target = self.home / "v16" / db.DB_FILENAME
        migrate_upto(target, FIRST_NEW_MIGRATION, app_version="old")
        report = db.migrate(target, app_version="test")
        self.assertIsNone(report.backup)
        self.assertEqual([item[0] for item in report.applied], [16, 17, 18])
        self.assertEqual(db.list_backups(target.parent / db.BACKUP_DIRNAME), [])

    def test_the_new_migrations_are_not_in_the_destructive_set(self):
        self.assertEqual(db.DESTRUCTIVE_MIGRATIONS & set(range(FIRST_NEW_MIGRATION,
                                                               db.SCHEMA_VERSION + 1)),
                         frozenset())

    def test_an_already_current_file_is_neither_copied_nor_rewritten(self):
        target = self.home / "current" / db.DB_FILENAME
        db.migrate(target, app_version="test")
        report = db.migrate(target, app_version="test")
        self.assertIsNone(report.backup)
        self.assertFalse(report.changed)


class LargeDatabaseMigrationTests(unittest.TestCase):
    """The cost of the new migrations must not depend on the size of the file.

    Migrations 16..18 only `CREATE ... IF NOT EXISTS` and `ALTER TABLE ADD
    COLUMN`, so no row of an existing table is read.  This measures that on a
    file big enough that a rewriting migration would have shown up.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "data" / db.DB_FILENAME

    def test_migrating_a_large_file_does_not_hold_it_in_memory(self):
        migrate_upto(self.path, PREVIOUS_HEAD, app_version="old")
        conn = db.connect(self.path)
        conn.execute("BEGIN")
        conn.executemany(
            "INSERT OR IGNORE INTO endpoints(id, canonical, last_seen_at) VALUES (?,?,?)",
            ((db.endpoint_id(f"http://198.51.100.{index % 256}:{1000 + index}"),
              f"http://198.51.100.{index % 256}:{1000 + index}", 1.0) for index in range(BULK_ROWS)))
        conn.execute("COMMIT")
        conn.close()
        size_before = self.path.stat().st_size
        self.assertGreater(size_before, 8 * 1024 * 1024, "the fixture is not big enough to mean anything")

        tracemalloc.start()
        started = time.perf_counter()
        report = db.migrate(self.path, app_version="test")
        elapsed = time.perf_counter() - started
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        self.assertEqual([item[0] for item in report.applied], [15, 16, 17, 18])
        # Migrations 16..18 only issue DDL, so the cost cannot scale with the file.
        # Measured on this fixture: a ~35 MB file peaks in the low megabytes, well
        # under a tenth of it.  A migration that read or rewrote a table would be
        # several times the file size.
        self.assertLess(peak, size_before // 8,
                        f"migration peaked at {peak} bytes on a {size_before} byte file")
        self.assertLess(elapsed, 30.0, f"migration took {elapsed:.1f}s")
        conn = db.connect(self.path)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT count(*) FROM endpoints").fetchone()[0],
                         len({db.endpoint_id(f"http://198.51.100.{index % 256}:{1000 + index}")
                              for index in range(BULK_ROWS)}))
        self.assertIn("source_feed", db.tables(conn))

    def test_a_backup_of_a_large_file_is_written_in_chunks_not_slurped(self):
        # `sha256_file` reads by the megabyte; this is the same guarantee for the
        # copy, whose size the caller is told rather than has to hold.
        migrate_upto(self.path, PREVIOUS_HEAD, app_version="old")
        conn = db.connect(self.path)
        conn.execute("BEGIN")
        conn.executemany(
            "INSERT OR IGNORE INTO endpoints(id, canonical) VALUES (?,?)",
            ((db.endpoint_id(f"http://203.0.113.{index}:8080"), f"http://203.0.113.{index}:8080")
             for index in range(150_000)))
        conn.execute("COMMIT")
        tracemalloc.start()
        manifest = db.create_backup(conn, self.path.parent / db.BACKUP_DIRNAME, reason="test")
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        conn.close()
        self.assertGreater(manifest.bytes, 8 * 1024 * 1024)
        # The peak is bounded by a constant -- the megabyte chunk `sha256_file`
        # reads by -- and does not grow with the file.
        self.assertLess(peak, 4 * 1024 * 1024,
                        f"backup peaked at {peak} bytes for a {manifest.bytes} byte file")
        self.assertLess(peak, manifest.bytes // 4,
                        f"backup peaked at {peak} bytes for a {manifest.bytes} byte file")
        self.assertTrue(db.verify_backup(manifest))


if __name__ == "__main__":
    unittest.main()
