"""The shared admission fixture must stay usable by every other module.

`tests/fixtures/admission.py` is the one place where the observation rows and the
`(canonical, admission_reason)` expectations are defined (CONTRACTS §2.3). Seventeen
modules import it, so it is tested here: a broken fixture would otherwise fail in
their suites, one at a time, with no obvious cause.
"""
from pathlib import Path
import re
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import db
from proxy_workbench import proxytool
from tests.fixtures import admission
from tests.workbench_support import add_candidates

#: Documentation (RFC 5737) and reserved TLD (RFC 2606) only. The shared fixture
#: must not name a routable address, so no module can be tempted into a live check.
RESERVED_HOST = re.compile(r"^(192\.0\.2\.|198\.51\.100\.|203\.0\.113\.|.*\.invalid$)")


class AdmissionFixtureTests(unittest.TestCase):
    def test_every_time_state_of_the_contract_has_a_case(self):
        reasons = {case["reason_code"] for case in admission.ADMISSION_CASES}
        for state in (admission.TIME_OK, admission.TIME_UNKNOWN, admission.TIME_FUTURE,
                      admission.CLOCK_ROLLBACK, admission.TTL_EXPIRED, admission.DENIED):
            self.assertIn(state, reasons)
        for case in admission.ADMISSION_CASES:
            self.assertEqual(case["admitted"], case["reason_code"] == admission.TIME_OK,
                             f"{case['name']}: admitted must match the positive reason")

    def test_case_names_and_addresses_are_unique(self):
        names = [case["name"] for case in admission.ADMISSION_CASES]
        canonicals = [case["canonical"] for case in admission.ADMISSION_CASES]
        self.assertEqual(len(set(names)), len(names))
        self.assertEqual(len(set(canonicals)), len(canonicals))
        self.assertEqual(sorted(admission.CASES_BY_NAME), sorted(names))

    def test_no_case_points_at_a_reachable_address(self):
        for case in admission.ADMISSION_CASES:
            host = case["canonical"].split("://", 1)[1].rsplit(":", 1)[0]
            with self.subTest(case=case["name"]):
                self.assertRegex(host, RESERVED_HOST)

    def test_expectations_are_exact_sorted_canonical_pairs(self):
        self.assertEqual(admission.EXPECTED_ALL,
                         tuple(sorted(admission.EXPECTED_ADMITTED + admission.EXPECTED_REJECTED)))
        self.assertEqual(len(admission.EXPECTED_ADMITTED) + len(admission.EXPECTED_REJECTED),
                         len(admission.ADMISSION_CASES))
        self.assertEqual(admission.EXPECTED_ADMITTED,
                         (("http://192.0.2.10:8080", admission.TIME_OK),
                          ("https://198.51.100.20:3128", admission.TIME_OK)))

    def test_reason_aliases_cover_the_short_spelling_of_the_contract(self):
        self.assertEqual(admission.REASON_ALIASES["time_unknown"], admission.TIME_UNKNOWN)
        self.assertEqual(admission.REASON_ALIASES["time_future"], admission.TIME_FUTURE)
        self.assertEqual(admission.REASON_ALIASES["clock_rollback"], admission.CLOCK_ROLLBACK)
        self.assertEqual(admission.REASON_ALIASES["TTL_EXPIRED"], admission.TTL_EXPIRED)
        self.assertEqual(admission.REASON_ALIASES["time_ok"], admission.TIME_OK)

    def test_row_builder_keeps_the_payload_field_names_of_the_current_code(self):
        row = admission.admission_row("fresh_ok")
        for field in ("proxy", "latency_ms", "reliability", "min_target_reliability",
                      "successes", "requests", "score", "checked_at", "valid_until",
                      "history", "error"):
            self.assertIn(field, row["payload"], field)
        self.assertEqual(row["payload"]["proxy"], row["canonical"])
        self.assertEqual(row["endpoint_id"], db.endpoint_id(row["canonical"]))
        self.assertIsNone(admission.admission_row("unknown_time")["checked_at"])

    def test_fixture_digest_changes_with_the_fixture(self):
        self.assertEqual(len(admission.FIXTURE_DIGEST), 64)
        self.assertEqual(admission.FIXTURE_DIGEST, admission.FIXTURE_DIGEST)

    def test_fixture_rows_fit_the_migrated_schema(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        db_path = Path(temp.name) / db.DB_FILENAME
        db.migrate(db_path)
        conn = db.connect(db_path)
        self.addCleanup(conn.close)
        mine = db.create_collection(conn, "Fixture", kind="private")

        written = admission.insert_rows(conn, collection_id=mine)
        self.assertEqual(len(written), len(admission.ADMISSION_CASES))
        stored = conn.execute(
            "SELECT proxy, checked_at, valid_until, profile_id, profile_revision,"
            " access_id, access_revision FROM results ORDER BY proxy").fetchall()
        self.assertEqual(len(stored), len(admission.ADMISSION_CASES))
        by_proxy = {row["proxy"]: row for row in stored}
        for case in admission.ADMISSION_CASES:
            row = by_proxy[case["canonical"]]
            self.assertEqual(row["checked_at"], case["checked_at"])
            self.assertEqual(row["valid_until"], case["valid_until"])
            self.assertEqual(row["profile_id"], "fixture-profile")
            self.assertEqual(row["access_revision"], 1)
        # The rows sit in the collection the caller named and in no other one.
        self.assertEqual(len(db.collection_members(conn, mine)), len(admission.ADMISSION_CASES))
        self.assertEqual(db.collection_members(conn, db.LEGACY_COLLECTION_ID), [])

    def test_fixture_rows_can_be_retention_aged_without_a_crash(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        db_path = Path(temp.name) / db.DB_FILENAME
        db.migrate(db_path)
        conn = db.connect(db_path)
        self.addCleanup(conn.close)
        admission.insert_rows(conn)
        preview = db.retention_preview(
            conn, db.RetentionPolicy(max_age_seconds=admission.HOUR), now=admission.NOW)
        self.assertEqual(preview.rows_for("results"), 1)


class CandidateFixtureTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.conn = proxytool.open_db(Path(temp.name) / db.DB_FILENAME)
        self.addCleanup(self.conn.close)

    def test_bulk_fixture_writes_are_transactional_on_an_autocommit_connection(self):
        transactions = []

        def trace(statement):
            if statement.startswith('INSERT OR IGNORE INTO candidates'):
                transactions.append(self.conn.in_transaction)

        self.conn.set_trace_callback(trace)
        add_candidates(self.conn, (f'http://192.0.2.1:{port}' for port in range(1000, 2017)))
        self.conn.set_trace_callback(None)
        self.assertEqual(len(transactions), 1017)
        self.assertTrue(all(transactions))
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM candidates').fetchone()[0], 1017)

    def test_bulk_fixture_does_not_commit_the_callers_transaction(self):
        self.conn.execute('BEGIN')
        add_candidates(self.conn, ['http://192.0.2.1:8080'])
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM candidates').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM endpoints').fetchone()[0], 0)

    def test_a_failed_fixture_rolls_back_its_batch_and_preserves_outer_work(self):
        self.conn.execute('BEGIN')
        collection = db.create_collection(self.conn, 'Caller work')
        self.conn.execute('SAVEPOINT caller')
        with self.assertRaises(ValueError):
            add_candidates(self.conn, ['http://192.0.2.1:8080', ('invalid', 'tuple')])
        self.assertTrue(self.conn.in_transaction)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM candidates').fetchone()[0], 0)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM endpoints').fetchone()[0], 0)
        self.assertIsNotNone(db.get_collection(self.conn, collection))
        self.conn.execute('RELEASE SAVEPOINT caller')
        add_candidates(self.conn, ['http://192.0.2.2:8080'])
        self.conn.rollback()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM candidates').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
