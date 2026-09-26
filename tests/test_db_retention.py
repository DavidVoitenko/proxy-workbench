"""F24: retention and cleanup, both with a preview that changes nothing.

A cleanup removes runtime artifacts, keeps the user's own settings, and never
touches secret material.
"""
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import db
from proxy_workbench import maintenance

NOW = 1_800_000_000.0
HOUR = 3600.0


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.db_path = self.home / "data" / db.DB_FILENAME
        db.migrate(self.db_path)
        self.conn = db.connect(self.db_path)
        self.addCleanup(self.conn.close)
        self.now = NOW

    def seed(self):
        """Six result rows and four observations with an explicit, documented age.

        results: three past `valid_until` (checked 10-12 h ago), two still valid
        (1 h and 3 h ago), one without `valid_until` at all (the defect-1 row).
        observations: finished 1-4 h ago.

        Every row names the endpoint of its own `proxy`: a result row is one
        address, so a row whose `proxy` and `endpoint_id` disagree would be six
        measurements of one address and would be refused by the key (F28).
        """
        self.expires_in = [(0.5, None), (1, self.now + HOUR), (3, self.now + HOUR),
                           (10, self.now - HOUR), (11, self.now - HOUR), (12, self.now - HOUR)]
        for index, (age, valid_until) in enumerate(self.expires_in):
            proxy = f"http://1.2.3.4:{8000 + index}"
            endpoint = db.upsert_endpoint(self.conn, proxy)
            self.conn.execute("INSERT INTO accesses(id, endpoint_id, mode, access_revision,"
                              " created_at) VALUES (?,?,?,?,?)",
                              (f"acc-{index}", endpoint, "public", 1, 0.0))
            self.conn.execute(
                "INSERT INTO results(profile, proxy, payload, endpoint_id, access_id,"
                " access_revision, profile_id, profile_revision, job_id, checked_at, valid_until)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("p", proxy, '{"score": 1}', endpoint, f"acc-{index}", 1,
                 "p", 1, f"job-{index}", self.now - age * HOUR, valid_until))
        endpoint = db.upsert_endpoint(self.conn, "http://1.2.3.4:8080")
        self.conn.execute("INSERT INTO accesses(id, endpoint_id, mode, access_revision,"
                          " created_at) VALUES (?,?,?,?,?)",
                          ("acc-obs", endpoint, "public", 1, 0.0))
        for index in range(4):
            self.conn.execute(
                "INSERT INTO observations(id, job_id, endpoint_id, access_id, access_revision,"
                " profile_id, profile_revision, started_at, finished_at, verdict)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (f"obs-{index}", "job-1", endpoint, "acc-obs", 1, "p", 1,
                 self.now - (index + 1) * HOUR, self.now - (index + 1) * HOUR,
                 '{"reliability": 1}'))

    def count(self, table):
        return self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def test_preview_reports_what_would_go_without_deleting_anything(self):
        self.seed()
        preview = db.retention_preview(self.conn, db.RetentionPolicy(max_age_seconds=2 * HOUR),
                                       now=self.now)
        self.assertEqual(self.count("results"), 6)
        self.assertEqual(self.count("observations"), 4)
        self.assertEqual(preview.rows_for("results"), 3)
        self.assertEqual(preview.rows_for("observations"), 3)
        self.assertEqual(preview.total_rows, 6)
        self.assertGreater(preview.database_bytes, 0)
        described = {entry["table"]: entry for entry in preview.to_dict()["targets"]}
        self.assertEqual(described["results"]["time_column"], "checked_at")
        self.assertEqual(described["observations"]["time_column"], "finished_at")
        self.assertEqual(described["results"]["oldest"], self.now - 12 * HOUR)
        self.assertEqual(self.count("results"), 6)
        self.assertEqual(self.count("observations"), 4)

    def test_expired_only_never_deletes_a_row_without_a_valid_until(self):
        self.seed()
        preview = db.retention_preview(self.conn, db.RetentionPolicy(), now=self.now)
        # Three rows are past valid_until; the row without one is inconsistent, not old.
        self.assertEqual(preview.rows_for("results"), 3)
        self.assertEqual(preview.rows_for("observations"), 0)
        report = db.apply_retention(self.conn, db.RetentionPolicy(), now=self.now)
        self.assertEqual(dict(report.deleted)["results"], 3)
        remaining = self.conn.execute(
            "SELECT valid_until FROM results ORDER BY valid_until").fetchall()
        self.assertEqual(len(remaining), 3)
        self.assertTrue(any(row[0] is None for row in remaining))
        self.assertFalse(report.vacuumed)
        self.assertGreater(report.freed_bytes, 0)

    def test_max_age_alone_keeps_rows_that_are_not_expired_yet(self):
        self.seed()
        policy = db.RetentionPolicy(max_age_seconds=4 * HOUR, expired_only=False)
        preview = db.retention_preview(self.conn, policy, now=self.now)
        self.assertEqual(preview.rows_for("results"), 3)
        report = db.apply_retention(self.conn, policy, now=self.now)
        self.assertEqual(dict(report.deleted)["results"], 3)
        self.assertEqual(self.count("results"), 3)
        self.assertEqual(report.to_dict()["deleted"]["results"], 3)

    def test_keep_newest_protects_the_freshest_rows(self):
        self.seed()
        # No age criterion: keep the two newest rows and drop the other four.
        policy = db.RetentionPolicy(max_age_seconds=None, expired_only=False, keep_newest=2)
        preview = db.retention_preview(self.conn, policy, now=self.now)
        self.assertEqual(preview.rows_for("results"), 4)
        report = db.apply_retention(self.conn, policy, now=self.now)
        # What the preview promised is what the delete removed.
        self.assertEqual(dict(report.deleted)["results"], 4)
        self.assertEqual(self.count("results"), 2)
        kept = self.conn.execute("SELECT checked_at FROM results").fetchall()
        self.assertEqual(sorted(row[0] for row in kept),
                         sorted([self.now - 0.5 * HOUR, self.now - 1 * HOUR]))

    def test_a_policy_that_deletes_nothing_reports_zero_and_keeps_the_data(self):
        self.seed()
        empty = db.RetentionPolicy(max_age_seconds=None, expired_only=False)
        preview = db.retention_preview(self.conn, empty, now=self.now)
        self.assertEqual(preview.total_rows, 0)
        report = db.apply_retention(self.conn, empty, now=self.now)
        self.assertEqual(report.deleted, ())
        self.assertEqual(self.count("results"), 6)

    def test_vacuum_is_opt_in(self):
        self.seed()
        pages_before = self.conn.execute("PRAGMA page_count").fetchone()[0]
        without = db.apply_retention(self.conn, db.RetentionPolicy(), now=self.now)
        self.assertFalse(without.vacuumed)
        report = db.apply_retention(
            self.conn, db.RetentionPolicy(expired_only=False, keep_newest=1),
            now=self.now, vacuum=True)
        self.assertTrue(report.vacuumed)
        self.assertLessEqual(self.conn.execute("PRAGMA page_count").fetchone()[0], pages_before)
        self.assertEqual(self.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(report.to_dict()["vacuumed"], True)

    def test_policy_rejects_unknown_targets_and_negative_values(self):
        with self.assertRaises(db.RetentionError):
            db.RetentionPolicy(include=("results", "secrets"))
        with self.assertRaises(db.RetentionError):
            db.RetentionPolicy(max_age_seconds=-1)
        with self.assertRaises(db.RetentionError):
            db.RetentionPolicy(keep_newest=-5)

    def test_retention_ignores_tables_that_do_not_exist(self):
        conn = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(conn.close)
        preview = db.retention_preview(conn, db.RetentionPolicy(), now=self.now)
        self.assertEqual(preview.targets, ())
        self.assertEqual(preview.total_rows, 0)


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name) / "data"
        self.data.mkdir()
        for name in maintenance.RUNTIME_FILES:
            (self.data / name).write_bytes(b"x" * 10)
        (self.data / "exports" / "gen1").mkdir(parents=True)
        (self.data / "exports" / "gen1" / "proxies.txt").write_text("http://1.2.3.4:80", encoding="utf-8")
        (self.data / "gui-settings.json").write_text('{"lang": "ru"}', encoding="utf-8")
        (self.data / "denylist.txt").write_text("blocked.example\n", encoding="utf-8")
        (self.data / "secrets.json").write_text('{"vault": "locked"}', encoding="utf-8")

    def test_preview_lists_removals_and_protected_files_without_touching_anything(self):
        preview = db.cleanup_preview(self.data)
        names = [name for name, _size in preview.remove]
        self.assertIn("proxies.sqlite3", names)
        self.assertIn("exports/", names)
        self.assertIn("gui-progress.json", names)
        self.assertGreater(preview.total_bytes, 0)
        protected = {name: reason for name, _size, reason in preview.protected}
        self.assertEqual(protected["gui-settings.json"], "user settings")
        self.assertEqual(protected["denylist.txt"], "user settings")
        self.assertEqual(protected["secrets.json"], "secret material")
        self.assertFalse(preview.applied)
        self.assertTrue((self.data / "gui-settings.json").exists())

    def test_cleanup_without_apply_removes_nothing(self):
        report = db.cleanup(self.data)
        self.assertEqual(report.removed, ())
        self.assertFalse(report.preview.applied)
        self.assertTrue((self.data / "proxies.sqlite3").exists())
        self.assertTrue((self.data / "exports" / "gen1" / "proxies.txt").exists())

    def test_cleanup_removes_runtime_artifacts_and_keeps_user_files(self):
        report = db.cleanup(self.data, apply=True)
        self.assertIn("proxies.sqlite3", report.removed)
        self.assertIn("exports/", report.removed)
        self.assertEqual(report.failed, ())
        self.assertFalse((self.data / "proxies.sqlite3").exists())
        self.assertFalse((self.data / "exports").exists())
        self.assertTrue((self.data / "gui-settings.json").exists())
        self.assertTrue((self.data / "denylist.txt").exists())
        self.assertTrue((self.data / "secrets.json").exists())
        self.assertTrue(report.to_dict()["preview"]["applied"])

    def test_cleanup_can_keep_the_workbench_lock(self):
        db.cleanup(self.data, apply=True, keep_lock=True)
        self.assertTrue((self.data / "workbench.lock").exists())
        self.assertFalse((self.data / "proxies.sqlite3").exists())

    def test_cleanup_never_lists_a_preserved_name_for_removal(self):
        preview = db.cleanup_preview(self.data)
        removable = {name.rstrip("/") for name, _size in preview.remove}
        self.assertEqual(removable & set(db.PRESERVED_FILES), set())
        self.assertEqual(removable & set(db.PRESERVED_SECRET_FILES), set())
        self.assertEqual(removable, set(maintenance.RUNTIME_FILES) | set(maintenance.RUNTIME_DIRS))
        self.assertEqual(set(db.PRESERVED_FILES) & set(maintenance.RUNTIME_FILES), set())

    def test_cleanup_preview_of_a_missing_folder_is_empty(self):
        preview = db.cleanup_preview(Path(self.temp.name) / "nowhere")
        self.assertEqual(preview.remove, ())
        self.assertEqual(preview.total_bytes, 0)


if __name__ == '__main__':
    unittest.main()
