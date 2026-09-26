"""The pause, the budget and the scope exclusion, on a migrated database.

The measured defect this file closes: a schedule that was paused with three of
three daily requests spent came back unpaused with a zero counter after a
restart, because `schedules` had no column to keep either.  The test does what
a restart does -- closes every handle, reopens the file, builds a new store --
so a column that exists but is not read back fails here.
"""
from pathlib import Path
import json
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import db, scheduler  # noqa: E402

T0 = 1_700_000_000.0
SCHEDULE_ID = "sched-1"
POOL_ID = "pool-1"
#: The daily budget the acceptance scenario spends against.
DAILY_REQUESTS = 3


def a_spec(**overrides):
    fields = {
        "id": SCHEDULE_ID,
        "kind": scheduler.KIND_INTERVAL,
        "interval_minutes": 60.0,
        "pool_id": POOL_ID,
        "budgets": {"requests": DAILY_REQUESTS, "seconds": 600.0},
    }
    fields.update(overrides)
    return scheduler.ScheduleSpec.from_dict(fields)


class PauseAndBudgetRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "data" / db.DB_FILENAME
        db.migrate(self.path, app_version="test")

    def reopen(self):
        """What a restart is: every handle closed, the file opened again."""
        return db.connect(self.path)

    def test_the_columns_the_module_asks_for_are_the_columns_that_exist(self):
        conn = self.reopen()
        self.addCleanup(conn.close)
        store = scheduler.SqliteScheduleStore(conn)
        for table, requested in scheduler.REQUESTED_COLUMNS.items():
            present = set(db.columns(conn, table))
            missing = [name for name in requested if name not in present]
            with self.subTest(table=table, missing=missing):
                self.assertEqual(missing, [])

    def test_the_store_reports_that_nothing_is_lost_on_restart(self):
        conn = self.reopen()
        self.addCleanup(conn.close)
        store = scheduler.SqliteScheduleStore(conn)
        self.assertTrue(store.persists_runtime_state)
        self.assertTrue(store.persists_counters)
        self.assertEqual(scheduler.Scheduler(store=store).persistence()["lost_on_restart"], [])

    def test_a_pause_and_its_spent_budget_survive_a_restart(self):
        conn = self.reopen()
        store = scheduler.SqliteScheduleStore(conn)
        store.save_spec(a_spec())
        engine = scheduler.Scheduler(store=store, clock=lambda: T0)
        engine.add(store.load_specs()[0])
        engine.pause(SCHEDULE_ID, reason=scheduler.REASON_PAUSED_USER, at=T0)
        # The whole daily budget is spent, the way a run that was not stopped spends it.
        store.save_state(SCHEDULE_ID, scheduler.ScheduleState(
            last_run_at=T0 - 60, next_run_at=T0 + 3600, paused=True,
            pause_reason=scheduler.REASON_PAUSED_USER, resume_at=None,
            counters=scheduler.BudgetCounters(requests=DAILY_REQUESTS, bytes=DAILY_REQUESTS * 1024,
                                              seconds=float(DAILY_REQUESTS), reset_key="day",
                                              reset_at=T0 + 86_400)))

        # ---- the restart ----
        conn.close()
        conn = self.reopen()
        self.addCleanup(conn.close)
        state = scheduler.SqliteScheduleStore(conn).load_state(SCHEDULE_ID)
        self.assertIsNotNone(state)
        self.assertTrue(state.paused, "the pause was lost across a restart")
        self.assertEqual(state.pause_reason, scheduler.REASON_PAUSED_USER)
        self.assertEqual(state.counters.requests, DAILY_REQUESTS)
        self.assertEqual(state.counters.bytes, DAILY_REQUESTS * 1024)
        self.assertEqual(state.counters.reset_key, "day")
        self.assertEqual(state.last_run_at, T0 - 60)

    def test_the_spent_budget_still_refuses_work_after_a_restart(self):
        # The user-visible half of the same property: a budget that resets on
        # restart is worse than no budget, because the counter the user reads
        # stops being the truth.
        conn = self.reopen()
        store = scheduler.SqliteScheduleStore(conn)
        # A zero in-flight reserve, so "three of three" is a hard stop rather
        # than a stop that still lets one more request through the headroom.
        store.save_spec(a_spec(budgets={"requests": DAILY_REQUESTS, "seconds": 600.0,
                                        "inflight_reserve_requests": 0}))
        engine = scheduler.Scheduler(store=store, clock=lambda: T0,
                                     power_reader=lambda: scheduler.PowerSignal())
        engine.add(store.load_specs()[0])
        budgets = store.load_specs()[0].budgets
        store.save_state(SCHEDULE_ID, scheduler.ScheduleState(
            next_run_at=T0 + 3600,
            counters=scheduler.BudgetCounters(
                requests=DAILY_REQUESTS, reset_key=scheduler.reset_key(budgets, T0),
                reset_at=scheduler.next_reset_at(budgets, T0))))
        conn.close()

        conn = self.reopen()
        self.addCleanup(conn.close)
        restarted = scheduler.Scheduler(store=scheduler.SqliteScheduleStore(conn),
                                        clock=lambda: T0 + 60,
                                        power_reader=lambda: scheduler.PowerSignal())
        ledger = restarted.ledger(SCHEDULE_ID, now=T0 + 60)
        refused = ledger.try_reserve(scheduler.PROBE, requests=1, bytes=1)
        self.assertFalse(refused.ok, "the exhausted daily budget was handed back by a restart")
        self.assertEqual(refused.denial.reason, "requests_exhausted")

    def test_a_paused_schedule_stays_paused_for_the_engine_that_reads_it_back(self):
        conn = self.reopen()
        store = scheduler.SqliteScheduleStore(conn)
        store.save_spec(a_spec())
        engine = scheduler.Scheduler(store=store, clock=lambda: T0)
        engine.add(store.load_specs()[0])
        engine.pause(SCHEDULE_ID, reason=scheduler.REASON_PAUSED_USER, at=T0)
        conn.close()

        conn = self.reopen()
        self.addCleanup(conn.close)
        restarted = scheduler.Scheduler(store=scheduler.SqliteScheduleStore(conn), clock=lambda: T0)
        self.assertTrue(restarted.state(SCHEDULE_ID).paused)
        report = restarted.tick(at=T0 + 86_400)
        self.assertEqual(report.run_requests, (),
                         "a paused schedule produced a run request after a restart")
        self.assertIn("skip", [decision.action for decision in report.decisions])

    def test_an_unpaused_schedule_still_runs(self):
        # The other half of the same property: the new columns must not freeze
        # every schedule by making `paused` read as truthy.
        conn = self.reopen()
        store = scheduler.SqliteScheduleStore(conn)
        store.save_spec(a_spec())
        engine = scheduler.Scheduler(store=store, clock=lambda: T0,
                                     power_reader=lambda: scheduler.PowerSignal())
        engine.add(store.load_specs()[0])
        self.assertEqual(engine.tick().decisions[0].action, "skip")  # not due yet
        report = engine.tick(at=T0 + 3600)
        self.assertEqual(len(report.run_requests), 1)
        self.assertEqual(report.run_requests[0].schedule_id, SCHEDULE_ID)

    def test_a_row_written_before_the_migration_reads_as_not_paused(self):
        conn = self.reopen()
        conn.execute("INSERT INTO schedules(id, pool_id, kind, interval_minutes, enabled)"
                     " VALUES ('legacy', NULL, 'interval', 60, 1)")
        state = scheduler.SqliteScheduleStore(conn).load_state("legacy")
        self.assertFalse(state.paused)
        self.assertIsNone(state.pause_reason)
        self.assertEqual(state.counters.requests, 0)

    def test_the_run_history_keeps_its_reason_and_the_missed_flag(self):
        conn = self.reopen()
        store = scheduler.SqliteScheduleStore(conn)
        store.save_spec(a_spec())
        run = scheduler.RunRequest(schedule_id=SCHEDULE_ID, run_id="r1", reason="due",
                                   scheduled_for=T0, requested_at=T0, missed=1)
        store.append_run(SCHEDULE_ID, run, finished_at=T0 + 5, run_state="done")
        rows = conn.execute("SELECT reason, missed FROM schedule_run WHERE id='r1'").fetchall()
        self.assertEqual([tuple(row) for row in rows], [("due", 1)])


class ScopeExclusionTests(unittest.TestCase):
    """The table the GUI had to keep in `gui-scope-exclusions.json`."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "data" / db.DB_FILENAME
        db.migrate(self.path, app_version="test")
        self.conn = db.connect(self.path)
        self.addCleanup(self.conn.close)
        self.addresses = tuple(f"http://198.51.100.{index}:8080" for index in range(1, 6))
        for canonical in self.addresses:
            db.upsert_endpoint(self.conn, canonical)
            db.add_member(self.conn, db.PUBLIC_COLLECTION_ID, db.endpoint_id(canonical), now=T0)

    def test_the_table_carries_scope_source_address_time_and_the_exclusive_flag(self):
        for column in ("scope_digest", "source_id", "proxy", "created_at", "exclusive"):
            with self.subTest(column=column):
                self.assertIn(column, db.columns(self.conn, "candidate_scope_exclusion"))
        db.add_scope_exclusion(self.conn, self.addresses[0], source_id="s-one", now=T0)
        row = db.scope_exclusions(self.conn)[0]
        self.assertEqual(row["proxy"], self.addresses[0])
        self.assertEqual(row["source_id"], "s-one")
        self.assertEqual(row["created_at"], T0)
        self.assertEqual(row["exclusive"], 1)
        self.assertEqual(row["shared"], 0)

    def test_the_two_names_for_one_fact_cannot_disagree(self):
        db.add_scope_exclusion(self.conn, self.addresses[0], source_id="s-one", shared=True, now=T0)
        row = db.scope_exclusions(self.conn)[0]
        self.assertEqual((row["exclusive"], row["shared"]), (0, 1))
        with self.assertRaises(db.exc_sqlite if hasattr(db, "exc_sqlite") else Exception):
            self.conn.execute(
                "INSERT INTO candidate_scope_exclusion(proxy, created_at, exclusive, shared)"
                " VALUES ('http://198.51.100.9:8080', 1, 1, 1)")

    def test_excluding_an_address_removes_it_from_a_result_read(self):
        # The read the GUI performs: take the results of the scope, drop the
        # excluded canonical addresses, and everything else stays.
        for index, canonical in enumerate(self.addresses):
            self.conn.execute(
                "INSERT OR REPLACE INTO results(profile, proxy, payload, endpoint_id)"
                " VALUES ('p1', ?, ?, ?)",
                (canonical, json.dumps({"proxy": canonical}), db.endpoint_id(canonical)))
        db.add_scope_exclusion(self.conn, self.addresses[1], source_id="s-one", now=T0)
        excluded = db.excluded_addresses(self.conn)
        self.assertEqual(excluded, frozenset({self.addresses[1]}))
        rows = self.conn.execute("SELECT proxy FROM results ORDER BY proxy").fetchall()
        visible = [row[0] for row in rows if row[0] not in excluded]
        self.assertEqual(visible, [value for value in self.addresses if value != self.addresses[1]])
        self.assertEqual(len(visible), 4)

    def test_an_exclusion_removes_no_data_and_touches_no_membership(self):
        before_membership = self.conn.execute(
            "SELECT count(*) FROM membership").fetchone()[0]
        before_endpoints = self.conn.execute("SELECT count(*) FROM endpoints").fetchone()[0]
        before_candidates = self.conn.execute("SELECT count(*) FROM candidates").fetchone()[0]
        db.add_scope_exclusion(self.conn, self.addresses[1], source_id="s-one", now=T0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM membership").fetchone()[0],
                         before_membership)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM endpoints").fetchone()[0],
                         before_endpoints)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM candidates").fetchone()[0],
                         before_candidates)
        # Undo is a delete and brings the address back.
        self.assertEqual(db.clear_scope_exclusions(self.conn, source_id="s-one"), 1)
        self.assertEqual(db.excluded_addresses(self.conn), frozenset())

    def test_two_scopes_exclude_the_same_address_independently(self):
        db.add_scope_exclusion(self.conn, self.addresses[0], source_id="s-one", now=T0)
        db.add_scope_exclusion(self.conn, self.addresses[0], source_id="s-one",
                               scope_digest="review", now=T0)
        self.assertEqual(db.excluded_addresses(self.conn), frozenset({self.addresses[0]}))
        self.assertEqual(db.excluded_addresses(self.conn, scope_digest="review"),
                         frozenset({self.addresses[0]}))
        self.assertEqual(len(db.scope_exclusions(self.conn)), 1)
        self.assertEqual(len(db.scope_exclusions(self.conn, scope_digest="review")), 1)
        self.assertEqual(db.clear_scope_exclusions(self.conn, scope_digest="review"), 1)
        self.assertEqual(len(db.scope_exclusions(self.conn)), 1)

    def test_the_exclusive_rule_the_exclude_scope_route_uses(self):
        # `gui.exclude_source_scope` excludes what one source delivered and keeps
        # what another source also delivered, unless it is told otherwise.
        delivered = self.addresses[:4]
        shared = delivered[:2]
        for canonical in delivered:
            db.add_scope_exclusion(self.conn, canonical, source_id="s-one",
                                   shared=canonical in shared, now=T0)
        rows = {row["proxy"]: row for row in db.scope_exclusions(self.conn)}
        exclusive = [value for value, row in rows.items() if row["exclusive"]]
        self.assertEqual(sorted(exclusive), sorted(delivered[2:]))
        self.assertEqual(sorted(value for value in shared if not rows[value]["exclusive"]),
                         sorted(shared))

    def test_an_expired_exclusion_stops_applying(self):
        db.add_scope_exclusion(self.conn, self.addresses[0], source_id="s-one",
                               expires_at=T0 + 10, now=T0)
        self.assertEqual(db.excluded_addresses(self.conn, now=T0), frozenset({self.addresses[0]}))
        self.assertEqual(db.excluded_addresses(self.conn, now=T0 + 20, include_expired=False),
                         frozenset())
        self.assertEqual(len(db.scope_exclusions(self.conn, now=T0 + 20)), 1)

    def test_the_same_address_excluded_twice_is_still_one_row(self):
        db.add_scope_exclusion(self.conn, self.addresses[0], source_id="s-one", now=T0)
        db.add_scope_exclusion(self.conn, self.addresses[0], source_id="s-one", now=T0 + 1)
        rows = db.scope_exclusions(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["created_at"], T0)

    def test_an_empty_address_is_refused(self):
        with self.assertRaises(db.DbError):
            db.add_scope_exclusion(self.conn, "  ", source_id="s-one", now=T0)

    def test_clearing_a_source_that_excluded_nothing_is_not_an_error(self):
        db.add_scope_exclusion(self.conn, self.addresses[0], source_id="s-one", now=T0)
        self.assertEqual(db.clear_scope_exclusions(self.conn, source_id="s-absent"), 0)
        self.assertEqual(len(db.scope_exclusions(self.conn)), 1)


if __name__ == "__main__":
    unittest.main()
