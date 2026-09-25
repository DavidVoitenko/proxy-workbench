"""F24: an old binary must not be able to write into the new schema.

CONTRACTS §3.5: the new schema breaks the old positional INSERTs immediately and
without a write, on the whole collection path -- `results`, `candidates` and
`candidate_seen` -- while `candidate_meta` is a deliberate exception.
"""
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import db
from tests.workbench_support import legacy_database  # noqa: E402

#: The three positional writes the old worker performs, with their exact SQL.
OLD_WRITES = {
    "results": "INSERT OR REPLACE INTO results VALUES (?,?,?)",
    "candidates": "INSERT OR IGNORE INTO candidates VALUES (?)",
    "candidate_seen": "INSERT OR IGNORE INTO candidate_seen VALUES (?, ?)",
}
OLD_ARGUMENTS = {
    "results": ("profile0001", "http://1.2.3.4:8080", "{}"),
    "candidates": ("http://7.7.7.7:80",),
    "candidate_seen": ("http://7.7.7.7:80", "list-1"),
}


class WriteGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.db_path = self.home / "data" / db.DB_FILENAME
        conn = legacy_database(self.db_path)
        conn.execute("INSERT INTO candidates VALUES (?)", ("http://1.2.3.4:8080",))
        conn.execute("INSERT INTO candidate_seen VALUES (?,?)", ("http://1.2.3.4:8080", "list-1"))
        conn.execute("INSERT INTO results VALUES (?,?,?)",
                     ("profile0001", "http://1.2.3.4:8080", "{}"))
        conn.commit()
        conn.close()
        self.report = db.migrate(self.db_path)

    def counts(self, conn):
        return {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("candidates", "candidate_seen", "results", "candidate_meta")}

    def test_old_positional_write_is_refused_on_every_guarded_table(self):
        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        for table, statement in OLD_WRITES.items():
            with self.subTest(table=table):
                with self.assertRaises(sqlite3.OperationalError) as caught:
                    conn.execute(statement, OLD_ARGUMENTS[table])
                self.assertIn("columns", str(caught.exception))
                self.assertIn(str(len(db.columns(conn, table))), str(caught.exception))

    def test_refused_old_write_leaves_the_database_untouched(self):
        conn = db.connect(self.db_path)
        before = self.counts(conn)
        conn.close()
        digest = db.sha256_file(self.db_path)

        for table, statement in OLD_WRITES.items():
            conn = db.connect(self.db_path)
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute(statement, OLD_ARGUMENTS[table])
            conn.close()

        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        self.assertEqual(self.counts(conn), before)
        self.assertEqual(db.sha256_file(self.db_path), digest)
        self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], db.SCHEMA_VERSION)
        self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_guard_columns_are_not_null_with_a_default(self):
        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        for table, column in db.WRITE_GUARD_COLUMNS:
            with self.subTest(table=table):
                info = {row[1]: row for row in conn.execute(f"PRAGMA table_info({table})")}
                self.assertIn(column, info)
                self.assertEqual(info[column][3], 1, "column must be NOT NULL")
                self.assertEqual(info[column][4], "''")
                self.assertEqual(info[column][5], 0, "column must not be part of the key")
        # The legacy candidate was given a real endpoint reference, not a dead default.
        row = conn.execute("SELECT endpoint_id FROM candidates").fetchone()
        self.assertEqual(row[0], db.endpoint_id("http://1.2.3.4:8080"))

    def test_candidate_meta_stays_writable_on_purpose(self):
        # §3.5.2: candidate_meta is not on the protected path, because the old code
        # already writes it with an explicit column list and holds no proof in it.
        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        conn.execute("""INSERT INTO candidate_meta(proxy, country, source) VALUES (?,?,?)
                        ON CONFLICT(proxy) DO UPDATE SET country = excluded.country""",
                     ("http://1.2.3.4:8080", "NL", "list-2"))
        self.assertEqual(conn.execute("SELECT country FROM candidate_meta").fetchone()[0], "NL")

    def test_new_code_writes_with_explicit_columns(self):
        conn = db.connect(self.db_path)
        self.addCleanup(conn.close)
        endpoint = db.endpoint_id("http://1.2.3.4:8080")
        conn.execute("INSERT INTO candidates(proxy, endpoint_id) VALUES (?,?)",
                     ("http://8.8.8.8:3128", db.endpoint_id("http://8.8.8.8:3128")))
        conn.execute("INSERT INTO candidate_seen(proxy, source, endpoint_id) VALUES (?,?,?)",
                     ("http://8.8.8.8:3128", "list-2", db.endpoint_id("http://8.8.8.8:3128")))
        conn.execute("""INSERT INTO results(profile, proxy, payload, endpoint_id, access_id,
                         access_revision, profile_id, profile_revision, job_id, checked_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?)""",
                     ("profile0001", "http://1.2.3.4:8080", '{"score": 1}', endpoint,
                      "acc-1", 1, "profile0001", 1, "job-1", 1000.0))
        self.assertEqual(self.counts(conn),
                         {"candidates": 2, "candidate_seen": 2, "results": 2, "candidate_meta": 0})

    def test_every_written_table_refuses_a_write_from_a_connection_without_the_version(self):
        # A connection opened by anything other than this module has no guard of its
        # own: only the schema shape protects the file.
        raw = sqlite3.connect(self.db_path, isolation_level=None)
        self.addCleanup(raw.close)
        for table, statement in OLD_WRITES.items():
            with self.subTest(table=table):
                with self.assertRaises(sqlite3.OperationalError):
                    raw.execute(statement, OLD_ARGUMENTS[table])


if __name__ == '__main__':
    unittest.main()
