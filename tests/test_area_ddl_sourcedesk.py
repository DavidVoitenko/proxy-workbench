"""The source tables of db.py against the contract sourcedesk declares (F13, F27).

`sourcedesk.REQUESTED_DDL` is a string constant the module never executes: it is
the request the migrator has to answer.  Every test here is a comparison between
that request and what `db.migrate()` really built, so the two cannot drift --
and then a live `SourceDesk` on the migrated file, so "the schema exists" is
never mistaken for "the module works".
"""
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import db, sourcedesk as sd  # noqa: E402

#: The seven tables the six independent checks stopped on.  Listed by name so a
#: regression says which one is missing instead of printing a set difference.
SOURCE_TABLES = (
    "source_state", "source_observation", "source_generation",
    "source_generation_entry", "source_identity", "source_feed", "membership_source",
)
#: The columns `source_management.runtime_snapshot` selects, per table.  These
#: are the reads that were silently falling into `except sqlite3.Error: pass`.
STATE_COLUMNS = (
    "source_id", "endpoint_id", "final_url", "etag", "last_modified", "last_attempt_at",
    "last_success_at", "last_body_at", "last_304_at", "current_generation",
    "last_good_generation", "consecutive_failures", "backoff_until", "quarantine_until",
    "retry_after", "last_error", "profile_digest",
)
OBSERVATION_COLUMNS = (
    "id", "run_id", "source_id", "endpoint_id", "started_at", "ended_at", "http_state",
    "parse_state", "cache_state", "outcome", "status", "attempts", "pages", "bytes",
    "received", "recognized", "accepted", "rejected", "duplicate", "blocked",
    "new_endpoints", "partial", "error", "retryable", "body_sha256", "fallback_used",
    "profile_digest", "retry_after",
)
GENERATION_COLUMNS = (
    "id", "source_id", "observation_id", "state", "active", "last_good", "created_at",
    "record_count", "estimated_bytes", "profile_digest", "endpoint_url",
)
IDENTITY_COLUMNS = ("source_id", "family_id", "publisher_id", "metadata_json")

#: Modules that still run their own CREATE statements, kept here so the check
#: below fails on a *new* one instead of quietly accepting a growing list.
#: `apikeys.ensure_schema()` re-declares migrations 8/10/14 so the module can be
#: exercised before the migrator lands; it is idempotent and belongs to the
#: apikeys owner -- see docs/integration/HANDOFF/area-ddl.md.
KNOWN_SCHEMA_WRITERS = {"apikeys.py"}


def requested_database():
    """An in-memory database built from the constant, the way the module's own tests do."""
    conn = sqlite3.connect(":memory:")
    for statement in sd.REQUESTED_DDL:
        conn.execute(statement)
    return conn


def shape(conn, table):
    """(columns, primary key) of one table, read from the file.

    Indexes are compared separately: a table may carry more indexes than the
    request asks for (this area adds one for a read path the request did not
    name), and that is additive rather than a disagreement about the shape.
    """
    rows = conn.execute(f"PRAGMA table_info({table!r})").fetchall()
    return [row[1] for row in rows], [row[1] for row in rows if row[5]]


class SourceTableContractTests(unittest.TestCase):
    """The migrated tables, on a real file produced by `db.migrate()`."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "data" / db.DB_FILENAME
        self.report = db.migrate(self.path, app_version="test")
        self.conn = db.connect(self.path)
        self.addCleanup(self.conn.close)

    def test_every_table_the_checks_stopped_on_exists(self):
        present = set(db.tables(self.conn))
        for table in SOURCE_TABLES:
            with self.subTest(table=table):
                self.assertIn(table, present)

    def test_the_tables_the_migration_declares_are_exactly_the_ones_it_creates(self):
        declared = set()
        for statement in db.SOURCE_TABLES_DDL:
            words = statement.split()
            declared.add(words[words.index("EXISTS") + 1].rstrip("("))
        self.assertEqual(declared, set(SOURCE_TABLES))

    def test_source_state_carries_every_column_the_runtime_view_selects(self):
        self.assertTrue(set(STATE_COLUMNS) <= set(db.columns(self.conn, "source_state")))

    def test_source_observation_carries_every_column_the_runtime_view_selects(self):
        self.assertTrue(set(OBSERVATION_COLUMNS) <= set(db.columns(self.conn, "source_observation")))

    def test_source_generation_carries_every_column_the_cache_view_selects(self):
        self.assertTrue(set(GENERATION_COLUMNS) <= set(db.columns(self.conn, "source_generation")))

    def test_source_identity_carries_the_family_and_publisher_columns(self):
        self.assertEqual(db.columns(self.conn, "source_identity"), list(IDENTITY_COLUMNS))

    def test_source_generation_entry_is_keyed_by_endpoint_id_not_by_a_second_address_model(self):
        # CONTRACTS 1.1 plus HANDOFF/sources-handoff.ru.md 1.2 p.6: one address
        # entity.  The sources branch keyed it by the address string (`proxy`).
        columns, key = shape(self.conn, "source_generation_entry")
        self.assertIn("endpoint_id", columns)
        self.assertNotIn("proxy", columns)
        self.assertEqual(key, ["generation_id", "endpoint_id"])

    # -- the two tables sourcedesk declares ---------------------------------

    def test_the_declared_tables_match_the_requested_ddl_exactly(self):
        request = requested_database()
        self.addCleanup(request.close)
        for table in ("source_feed", "membership_source"):
            with self.subTest(table=table):
                self.assertEqual(shape(self.conn, table), shape(request, table))

    def test_the_declared_indexes_exist(self):
        names = set(db.indexes(self.conn))
        for expected in ("membership_source_by_source", "source_feed_by_collection"):
            with self.subTest(index=expected):
                self.assertIn(expected, names)

    def test_membership_source_has_no_foreign_key_to_endpoints(self):
        # A deliberate decision, and the reason is a fact about the module:
        # `SourceDesk.apply_plan` writes the canonical address it received from
        # `ImportedEndpoint.endpoint` into this column, so a reference to
        # `endpoints(id)` would reject the module's own writes.  A test that
        # pins the decision stops a later reader from "fixing" it silently.
        rows = self.conn.execute("PRAGMA foreign_key_list('membership_source')").fetchall()
        self.assertEqual([row[2] for row in rows], [])

    def test_a_generation_and_its_observation_cannot_dangle(self):
        # The two relationships whose parent is unconditionally written first.
        for table, expected in (("source_generation", ["source_observation"]),
                                ("source_generation_entry", ["source_generation"])):
            with self.subTest(table=table):
                rows = self.conn.execute(f"PRAGMA foreign_key_list({table!r})").fetchall()
                self.assertEqual([row[2] for row in rows], expected)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO source_generation_entry(generation_id, endpoint_id)"
                " VALUES (999999, 'http://198.51.100.1:8080')")

    def test_the_read_paths_the_views_issue_are_indexed(self):
        names = set(db.indexes(self.conn))
        for expected in ("source_observation_by_source", "source_generation_by_source",
                         "source_state_by_source", "membership_source_by_endpoint"):
            with self.subTest(index=expected):
                self.assertIn(expected, names)
        plan = self.conn.execute(
            "EXPLAIN QUERY PLAN SELECT id,outcome FROM source_observation"
            " WHERE source_id=? ORDER BY id DESC LIMIT 10", ("s1",)).fetchall()
        self.assertTrue(any("source_observation_by_source" in str(row[-1]) for row in plan),
                        f"source_observation lookup is not indexed: {plan}")

    # -- a real SourceDesk on the migrated file -----------------------------

    def test_a_source_desk_runs_on_the_migrated_database(self):
        source = sd.user_source(binding_id="b1", url="https://sub.invalid/v1/list",
                                source_format="clash")
        desk = sd.SourceDesk(self.conn)
        bound = desk.bind(source, collection_id=db.PUBLIC_COLLECTION_ID, mode="replace")
        self.assertEqual(bound.source_id, source.id)
        self.assertEqual(desk.get(source.id, db.PUBLIC_COLLECTION_ID).mode, "replace")
        row = desk.feed_row(source.id, db.PUBLIC_COLLECTION_ID)
        self.assertEqual(row["public_url"], "https://sub.invalid/v1/list")

    def test_membership_keeps_a_shared_address_attributed_to_both_sources(self):
        shared = "http://198.51.100.33:8080"
        exclusive = "http://198.51.100.34:8080"
        desk = sd.SourceDesk(self.conn)
        self.assertEqual(desk.add_membership(db.PUBLIC_COLLECTION_ID, "s-one",
                                             [shared, exclusive], now=1.0), 2)
        self.assertEqual(desk.add_membership(db.PUBLIC_COLLECTION_ID, "s-two",
                                             [shared], now=1.0), 1)
        self.assertEqual(desk.contributors(shared, db.PUBLIC_COLLECTION_ID), ("s-one", "s-two"))
        self.assertEqual(desk.foreign_membership("s-one", db.PUBLIC_COLLECTION_ID,
                                                 [shared, exclusive]), frozenset({shared}))

    def test_a_replace_refresh_does_not_remove_another_sources_membership(self):
        # The F27 acceptance: a replace refresh drops only what *this* source
        # stopped offering, and an address another source still contributes
        # keeps that source's row.
        first, shared, dropped = ("http://198.51.100.31:8080", "http://198.51.100.32:8080",
                                  "http://198.51.100.33:8080")
        offered = (first, shared, dropped)
        desk = sd.SourceDesk(self.conn)
        source = sd.user_source(binding_id="b1", url="https://sub.invalid/v1/list",
                                source_format="text")
        desk.bind(source, collection_id=db.PUBLIC_COLLECTION_ID, mode="replace")
        desk.add_membership(db.PUBLIC_COLLECTION_ID, source.id, offered, now=1.0)
        desk.add_membership(db.PUBLIC_COLLECTION_ID, "s-two", (shared,), now=1.0)
        state = sd.FeedState(source.id, db.PUBLIC_COLLECTION_ID, mode="replace",
                             active=offered, last_good=offered)
        result = sd.FeedResult(outcome="ok", fetched_at=10.0, entries=(first,))
        plan = sd.plan_refresh(
            state, result, now=10.0,
            foreign=desk.foreign_membership(source.id, db.PUBLIC_COLLECTION_ID, offered))
        desk.apply_plan(plan, now=10.0)
        self.assertEqual(plan.removed, (shared, dropped))
        self.assertEqual(plan.retained_shared, (shared,))
        self.assertEqual(desk.membership(source.id, db.PUBLIC_COLLECTION_ID), (first,))
        self.assertEqual(desk.membership("s-two", db.PUBLIC_COLLECTION_ID), (shared,))
        self.assertEqual(desk.contributors(shared, db.PUBLIC_COLLECTION_ID), ("s-two",))


class RequestedDdlIsNotExecutedTests(unittest.TestCase):
    """The contract that the constant stays a request and the migrator answers it."""

    def test_the_module_still_executes_no_ddl(self):
        source = Path(sd.__file__).read_text(encoding="utf-8")
        for line in source.splitlines():
            stripped = line.strip()
            if not stripped.startswith(("CREATE ", "DROP ", "ALTER ")):
                continue
            with self.subTest(line=stripped[:60]):
                self.fail(f"sourcedesk.py must not execute DDL, found: {stripped[:60]}")

    def test_nothing_outside_db_py_executes_a_create_statement(self):
        # HANDOFF/sources-handoff.ru.md 3.1 p.2.  A string constant is fine; a
        # call that runs one is not.  A `TEMP` table is excluded: it lives on
        # the connection and is gone when it closes, so it is scratch space and
        # not this package's schema.  The exceptions below are recorded findings
        # for their own owners rather than permission: each new one fails here.
        package = Path(db.__file__).resolve().parent
        offenders = []
        for path in sorted(package.glob("*.py")):
            if path.name == "db.py":
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                code = line.split("#", 1)[0].upper()
                if "CREATE TEMP " in code:
                    continue
                if "executescript" in code or (".execute(" in code and "CREATE " in code):
                    offenders.append(f"{path.name}:{number}")
        modules = sorted({item.split(":")[0] for item in offenders})
        self.assertEqual([name for name in modules if name not in KNOWN_SCHEMA_WRITERS], [],
                         f"modules that execute DDL: {offenders}")


if __name__ == "__main__":
    unittest.main()
