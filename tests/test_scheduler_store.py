"""Persistence seam: the in-memory store and the adapter over migration 7 (F15).

The tables below are the DDL of CONTRACTS 3.3, migration 7, copied here so the
adapter can be exercised before `db.migrate()` exists. It is a fixture, not a
second source of truth: the module under test issues no DDL of its own, and when
`db.py` lands these two statements are replaced by one `db.migrate()` call.
"""
import json
import sqlite3
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import scheduler as sch

CONTRACT_DDL = """
CREATE TABLE schedules(
    id TEXT PRIMARY KEY, pool_id TEXT, kind TEXT, interval_minutes REAL, window_json TEXT,
    timezone TEXT, quiet_hours_json TEXT, budgets_json TEXT, next_run_at REAL, enabled INTEGER);
CREATE TABLE schedule_run(
    id TEXT PRIMARY KEY, schedule_id TEXT, started_at REAL, finished_at REAL, state TEXT,
    counters_json TEXT);
"""

# The same tables plus the columns this module needs and CONTRACTS 3.3 does not define yet.
EXTENDED_DDL = CONTRACT_DDL.replace(
    'enabled INTEGER);',
    'enabled INTEGER, last_run_at REAL, paused INTEGER, pause_reason TEXT, resume_at REAL,'
    ' dst_policy TEXT, catch_up INTEGER, max_catch_up INTEGER, wake_gap_s REAL,'
    ' power_json TEXT, notify_json TEXT, notifications_json TEXT, counters_json TEXT);') + (
    'ALTER TABLE schedule_run ADD COLUMN reason TEXT;'
    'ALTER TABLE schedule_run ADD COLUMN missed INTEGER;')

T0 = 1_780_000_000.0
CANARY = 'CANARY-schedule-token-7c41b6de'


def open_db(ddl):
    connection = sqlite3.connect(':memory:')
    connection.executescript(ddl)
    return connection


def sample(**kwargs):
    options = dict(id='pool-main', pool_id='main', kind=sch.KIND_INTERVAL, interval_minutes=30,
                   timezone='Europe/Berlin', windows=(sch.Window.parse('09:00-18:00',
                                                                       weekdays=['mon', 'tue']),),
                   quiet_hours=(sch.Window.parse('22:00-08:00'),),
                   budgets=sch.Budgets(requests=100, bytes=10 ** 6, concurrency=4, reset=sch.RESET_DAILY,
                                       timezone='Europe/Berlin'),
                   power=sch.PowerPolicy(on_battery=sch.POWER_BELOW_PERCENT, battery_min_percent=40,
                                         metered=sch.POWER_CHEAP_ONLY),
                   notify=sch.NotifyPolicy(enter_below=2, exit_above=4, min_events=2),
                   notifications=sch.NotificationConfig(
                       email=sch.Target(label='ops', enabled=True, user_confirmed=True)),
                   dst_policy=sch.DST_SHIFT, catch_up=True, max_catch_up=4, wake_gap_s=120.0)
    options.update(kwargs)
    return sch.ScheduleSpec(**options)


class InMemoryStoreTests(unittest.TestCase):
    def test_spec_state_and_runs_survive_a_new_scheduler(self):
        store = sch.InMemoryScheduleStore()
        first = sch.Scheduler(store=store, clock=lambda: T0, power_reader=sch.PowerSignal.unknown)
        first.add(sample(quiet_hours=(), windows=()))
        first.tick(T0)
        report = first.tick(T0 + 30 * 60)
        self.assertEqual(len(report.run_requests), 1)

        second = sch.Scheduler(store=store, clock=lambda: T0 + 30 * 60, power_reader=sch.PowerSignal.unknown)
        self.assertEqual([item.id for item in second.list()], ['pool-main'])
        again = second.tick(T0 + 30 * 60)
        self.assertEqual(again.run_requests, (), 'the restart must not replay the same instant')
        following = second.tick(T0 + 60 * 60)
        self.assertEqual(len(following.run_requests), 1)
        self.assertEqual(second.state('pool-main').runs, 2)
        self.assertEqual(len(store.recent_runs('pool-main')), 2)

    def test_a_pause_survives_a_restart(self):
        store = sch.InMemoryScheduleStore()
        first = sch.Scheduler(store=store, clock=lambda: T0, power_reader=sch.PowerSignal.unknown)
        first.add(sample(quiet_hours=(), windows=()))
        first.tick(T0)
        first.pause('pool-main')
        second = sch.Scheduler(store=store, clock=lambda: T0 + 60, power_reader=sch.PowerSignal.unknown)
        report = second.tick(T0 + 3600)
        self.assertEqual([item.reason for item in report.decisions], [sch.REASON_PAUSED_USER])
        self.assertEqual(report.run_requests, ())
        second.resume('pool-main')
        resumed = second.tick(T0 + 3610)
        # sample() has catch_up on, so the two missed occurrences come back, and only those.
        self.assertEqual([item.reason for item in resumed.decisions], [sch.REASON_DUE])
        self.assertEqual(len(resumed.run_requests), 2)
        self.assertEqual([item.scheduled_for for item in resumed.run_requests],
                         [T0 + 1800, T0 + 3600])

    def test_budget_counters_are_carried_over(self):
        store = sch.InMemoryScheduleStore()
        first = sch.Scheduler(store=store, clock=lambda: T0, power_reader=sch.PowerSignal.unknown)
        first.add(sample(quiet_hours=(), windows=(),
                         budgets=sch.Budgets(requests=2, reset=sch.RESET_DAILY, timezone='UTC')))
        first.tick(T0)
        ledger = first.ledger('pool-main', now=T0)
        ledger.commit(ledger.try_reserve(sch.PROBE, requests=2))
        first.state('pool-main').counters = ledger.counters
        first.store.save_state('pool-main', first.state('pool-main'))
        second = sch.Scheduler(store=store, clock=lambda: T0, power_reader=sch.PowerSignal.unknown)
        self.assertEqual(second.ledger('pool-main', now=T0).snapshot().requests.used, 2)
        self.assertTrue(second.ledger('pool-main', now=T0).snapshot().bytes.exhausted is False)


class SqliteStoreTests(unittest.TestCase):
    def test_the_module_writes_no_ddl(self):
        connection = open_db(CONTRACT_DDL)
        before = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        store = sch.SqliteScheduleStore(connection)
        store.save_spec(sample())
        store.append_run('pool-main', sch.RunRequest('pool-main', 'run-1', 'due', T0, T0))
        after = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        self.assertEqual(before, after)

    def test_spec_survives_the_contract_schema(self):
        connection = open_db(CONTRACT_DDL)
        store = sch.SqliteScheduleStore(connection)
        spec = sample()
        store.save_spec(spec)
        loaded = store.load_specs()[0]
        for name in ('id', 'pool_id', 'kind', 'interval_minutes', 'timezone', 'enabled'):
            self.assertEqual(getattr(loaded, name), getattr(spec, name), name)
        self.assertEqual(loaded.windows, spec.windows)
        self.assertEqual(loaded.quiet_hours, spec.quiet_hours)
        self.assertEqual(loaded.budgets.to_dict(), spec.budgets.to_dict())
        store.save_spec(loaded)                       # a rewrite of a loaded row changes nothing
        self.assertEqual(len(store.load_specs()), 1)

    def test_slots_survive_the_contract_schema(self):
        connection = open_db(CONTRACT_DDL)
        store = sch.SqliteScheduleStore(connection)
        spec = sample(id='slots', kind=sch.KIND_SLOTS, interval_minutes=None, windows=(),
                      slots=(sch.Slot.parse('09:00', ['mon']), sch.Slot.parse('21:30')))
        store.save_spec(spec)
        loaded = store.load_specs()[0]
        self.assertEqual(loaded.kind, sch.KIND_SLOTS)
        self.assertEqual(loaded.slots, spec.slots)

    def test_runtime_state_is_reported_as_unsupported_on_the_contract_schema(self):
        connection = open_db(CONTRACT_DDL)
        store = sch.SqliteScheduleStore(connection)
        store.save_spec(sample())
        self.assertFalse(store.persists_runtime_state)
        store.save_state('pool-main', sch.ScheduleState(last_run_at=T0, paused=True,
                                                         pause_reason='user'))
        state = store.load_state('pool-main')
        self.assertIsNone(state.last_run_at)
        self.assertFalse(state.paused)
        self.assertEqual(store.load_state('missing'), None)

    def test_the_interval_grid_is_recovered_from_the_run_history(self):
        connection = open_db(CONTRACT_DDL)
        store = sch.SqliteScheduleStore(connection)
        first = sch.Scheduler(store=store, clock=lambda: T0, power_reader=sch.PowerSignal.unknown)
        first.add(sample(interval_minutes=30, timezone='UTC', windows=(), quiet_hours=()))
        first.tick(T0)
        first.tick(T0 + 1800)
        state = store.load_state('pool-main')
        self.assertEqual(state.last_run_at, T0 + 1800)
        self.assertEqual(state.runs, 1)
        second = sch.Scheduler(store=store, clock=lambda: T0 + 1800, power_reader=sch.PowerSignal.unknown)
        self.assertEqual(second.tick(T0 + 1800).run_requests, ())
        self.assertEqual(len(second.tick(T0 + 3600).run_requests), 1)

    def test_runtime_state_is_persisted_when_the_columns_exist(self):
        connection = open_db(EXTENDED_DDL)
        store = sch.SqliteScheduleStore(connection)
        self.assertTrue(store.persists_runtime_state)
        spec = sample()
        store.save_spec(spec)
        store.save_state('pool-main', sch.ScheduleState(last_run_at=T0 + 60, next_run_at=T0 + 90,
                                                         paused=True, pause_reason='quiet_hours',
                                                         resume_at=T0 + 300))
        state = store.load_state('pool-main')
        self.assertEqual(state.last_run_at, T0 + 60)
        self.assertEqual(state.next_run_at, T0 + 90)
        self.assertTrue(state.paused)
        self.assertEqual(state.pause_reason, 'quiet_hours')
        self.assertEqual(state.resume_at, T0 + 300)
        loaded = store.load_specs()[0]
        self.assertEqual(loaded.dst_policy, sch.DST_SHIFT)
        self.assertTrue(loaded.catch_up)
        self.assertEqual(loaded.max_catch_up, 4)
        self.assertEqual(loaded.wake_gap_s, 120.0)
        self.assertEqual(loaded.power.to_dict(), spec.power.to_dict())
        self.assertEqual(loaded.notify.to_dict(), spec.notify.to_dict())
        self.assertEqual(loaded.notifications.to_dict(), spec.notifications.to_dict())

    def test_runs_are_recorded_with_their_reason_and_count(self):
        connection = open_db(EXTENDED_DDL)
        store = sch.SqliteScheduleStore(connection)
        store.save_spec(sample())
        run = sch.RunRequest('pool-main', 'run-1', sch.REASON_COALESCED, T0, T0, missed=7, coalesced=True)
        store.append_run('pool-main', run)
        store.append_run('pool-main', run, finished_at=T0 + 5, run_state='finished')
        history = store.recent_runs('pool-main')
        self.assertEqual(len(history), 1, 'the run id is the primary key, so a replay is one row')
        self.assertEqual(history[0]['state'], 'finished')
        self.assertEqual(history[0]['finished_at'], T0 + 5)
        self.assertEqual(history[0]['reason'], sch.REASON_COALESCED)
        self.assertEqual(history[0]['missed'], 7)
        self.assertEqual(json.loads(history[0]['counters_json'])['run_id'], 'run-1')
        self.assertEqual(store.recent_runs('pool-main', limit=0), [])

    def test_a_missing_table_is_a_configuration_state_not_a_crash(self):
        store = sch.SqliteScheduleStore(sqlite3.connect(':memory:'))
        self.assertEqual(store.available, set())
        self.assertEqual(store.load_specs(), [])
        self.assertIsNone(store.load_state('pool-main'))
        self.assertEqual(store.recent_runs('pool-main'), [])
        self.assertFalse(store.remove_spec('pool-main'))

    def test_remove_and_reload(self):
        connection = open_db(CONTRACT_DDL)
        store = sch.SqliteScheduleStore(connection)
        store.save_spec(sample())
        store.save_spec(sample(id='second', pool_id='other'))
        self.assertEqual([item.id for item in store.load_specs()], ['pool-main', 'second'])
        self.assertTrue(store.remove_spec('second'))
        self.assertFalse(store.remove_spec('second'))
        self.assertEqual([item.id for item in store.load_specs()], ['pool-main'])

    def test_nothing_persisted_contains_a_secret(self):
        connection = open_db(EXTENDED_DDL)
        store = sch.SqliteScheduleStore(connection)
        spec = sample(notifications=sch.NotificationConfig(
            in_app=True, email=sch.Target(label='ops', enabled=True, user_confirmed=True)))
        store.save_spec(spec)
        store.append_run('pool-main', sch.RunRequest('pool-main', 'run-1', 'due', T0, T0))
        store.save_state('pool-main', sch.ScheduleState(last_run_at=T0))
        dump = '\n'.join(connection.iterdump())
        self.assertNotIn(CANARY, dump)
        self.assertIn('ops', dump, 'the target is a label, not an address or a token')

    def test_a_real_file_database_keeps_the_schedule(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'workbench.sqlite3'
            connection = sqlite3.connect(path)
            connection.executescript(CONTRACT_DDL)
            store = sch.SqliteScheduleStore(connection)
            store.save_spec(sample())
            connection.close()

            connection = sqlite3.connect(path)
            reloaded = sch.SqliteScheduleStore(connection).load_specs()
            self.assertEqual([item.id for item in reloaded], ['pool-main'])
            self.assertEqual(reloaded[0].windows, sample().windows)
            connection.close()


class RestartTests(unittest.TestCase):
    def test_budget_usage_survives_a_restart_through_sqlite(self):
        connection = open_db(EXTENDED_DDL)      # counters_json is a requested column
        store = sch.SqliteScheduleStore(connection)
        self.assertTrue(store.persists_counters)
        spec = sample(timezone='UTC', windows=(), quiet_hours=(),
                      budgets=sch.Budgets(requests=1, reset=sch.RESET_DAILY, timezone='UTC',
                                          inflight_reserve_requests=0))
        store.save_spec(spec)
        first = sch.Scheduler(store=store, clock=lambda: T0, power_reader=sch.PowerSignal.unknown)
        first.tick(T0)
        ledger = first.ledger('pool-main', now=T0)
        ledger.commit(ledger.try_reserve(sch.PROBE, requests=1))
        first.state('pool-main').counters = ledger.counters
        store.save_state('pool-main', first.state('pool-main'))

        second = sch.Scheduler(store=store, clock=lambda: T0, power_reader=sch.PowerSignal.unknown)
        recovered = second.ledger('pool-main', now=T0)
        self.assertEqual(recovered.snapshot().requests.used, 1)
        self.assertTrue(recovered.snapshot().requests.exhausted)
        second.tick(T0 + 1800)
        after = second.ledger('pool-main', now=T0 + 1800)
        self.assertEqual(after.snapshot().requests.remaining, 0.0)

    def test_the_contract_schema_still_recovers_the_interval_grid(self):
        connection = open_db(CONTRACT_DDL)
        store = sch.SqliteScheduleStore(connection)
        self.assertFalse(store.persists_counters)
        spec = sample(timezone='UTC', windows=(), quiet_hours=())
        store.save_spec(spec)
        first = sch.Scheduler(store=store, clock=lambda: T0, power_reader=sch.PowerSignal.unknown)
        first.tick(T0)
        first.tick(T0 + 1800)
        second = sch.Scheduler(store=store, clock=lambda: T0 + 1800, power_reader=sch.PowerSignal.unknown)
        self.assertEqual(second.state('pool-main').last_run_at, T0 + 1800)
        self.assertEqual(second.tick(T0 + 1800).run_requests, ())


if __name__ == '__main__':
    unittest.main()
