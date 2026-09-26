"""Backend regressions on temporary databases and deterministic local fixtures."""
import ctypes
import dataclasses
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import types
import unittest
from unittest import mock

from proxy_workbench import api, apiv1, db, importer, pools, scheduler, secrets
from tests.pools_support import FakeSource, ManualClock, PoolTestCase
from tests.test_apiv1_limits import Service
from tests.test_apiv1_service import FakeKeys
from tests.test_importer_fixtures import Schema, members, source


class ApiRegressionTests(unittest.TestCase):
    def test_key_quota_refusals_do_not_exhaust_the_server_slots(self):
        principal = apiv1.Principal('limited', permissions=frozenset(apiv1.PERMISSIONS),
                                    concurrency=1)
        client = apiv1.ApiV1(Service(), FakeKeys([principal]),
                             apiv1.ApiConfig(max_concurrency=2))
        held = client.key_quota.acquire(principal)
        request = apiv1.Request('GET', '/v1/results',
                                headers={'Host': '127.0.0.1', 'Authorization': 'Bearer limited'})
        try:
            for _ in range(5):
                refused = client.handle(request)
                self.assertEqual(refused.status_code, 429)
                self.assertEqual(refused.json()['error']['details']['max_active'], 1)
                self.assertEqual(client.slots._used, 0)
        finally:
            client.key_quota.release(held)
        self.assertEqual(client.handle(request).status_code, 200)

    def test_the_last_partial_page_has_no_next_cursor(self):
        with tempfile.TemporaryDirectory() as folder:
            service = api.WorkbenchService(folder)
            rows = list(range(5))
            self.assertEqual(service.page(rows, 'items', 0, 2)['next_seq'], 3)
            self.assertEqual(service.page(rows, 'items', 2, 2)['next_seq'], 5)
            final = service.page(rows, 'items', 4, 2)
            self.assertEqual(final['items'], [4])
            self.assertEqual(final['cursor_seq'], 5)
            self.assertIsNone(final['next_seq'])


class ImportRegressionTests(unittest.TestCase):
    def setUp(self):
        self.schema = Schema()
        self.addCleanup(self.schema.close)
        self.conn = self.schema.conn
        self.collection = importer.create_collection(self.conn, 'large import')

    def plan(self, text, **kwargs):
        return importer.preview(self.conn, source(text), collection_id=self.collection, **kwargs)

    def test_replace_keeps_more_than_one_chunk_of_existing_members(self):
        rows = [f'11.1.{index // 250}.{index % 250 + 1}:8080' for index in range(1100)]
        importer.commit(self.conn, self.plan('\n'.join(rows)))
        kept = rows[:700]
        report = importer.commit(self.conn, self.plan('\n'.join(kept), mode='replace'))
        self.assertEqual(report.counts['removed'], 400)
        self.assertEqual(members(self.conn, self.collection), sorted('http://' + row for row in kept))

    def test_writer_that_waited_for_the_lock_rechecks_the_preview_revision(self):
        plan = self.plan('11.0.0.1:8080')
        waiting = threading.Event()
        outcome = []

        def import_waiter():
            conn = db.connect(self.schema.path)
            conn.set_trace_callback(lambda sql: waiting.set() if sql == 'BEGIN IMMEDIATE' else None)
            try:
                outcome.append(importer.commit(conn, plan))
            except Exception as exc:
                outcome.append(exc)
            finally:
                conn.close()

        self.conn.execute('BEGIN IMMEDIATE')
        self.conn.execute('INSERT INTO import_batch(id, collection_id, state, revision) '
                          "VALUES ('concurrent', ?, 'committed', 1)", (self.collection,))
        thread = threading.Thread(target=import_waiter)
        thread.start()
        try:
            self.assertTrue(waiting.wait(3), 'the import must reach the write lock')
        finally:
            self.conn.commit()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], importer.ImportRevisionConflict)
        self.assertEqual(members(self.conn, self.collection), [])
        self.assertEqual(importer.collection_revision(self.conn, self.collection), 1)

    def test_credentials_remain_readable_on_another_connection(self):
        vault = secrets.SessionVault()
        plan = self.plan('http://alice:secret@11.0.0.1:8080',
                         policy=importer.DestinationPolicy(public_only=False, credentials='store'))
        with mock.patch.object(secrets, 'vault_for_database', return_value=vault):
            report = importer.commit(self.conn, plan)
            self.assertEqual(report.counts['credentials_stored'], 1)
            other = db.connect(self.schema.path)
            try:
                store = secrets.AccessStore(other, secrets.vault_for_database(other))
                access = store.list_for_endpoint(plan.valid[0].endpoint_id)[0]
                self.assertTrue(store.usable(access))
                self.assertEqual(vault.get(access.secret_ref).username, 'alice')
            finally:
                other.close()

    def test_failed_second_credential_rolls_back_membership_and_first_secret(self):
        vault = secrets.SessionVault()
        plan = self.plan('http://alice:secret@11.0.0.1:8080\nhttp://bob:secret@11.0.0.2:8080',
                         policy=importer.DestinationPolicy(public_only=False, credentials='store'))
        original = vault.stage
        references = []

        def stage(ref, payload):
            references.append(ref)
            if len(references) == 2:
                raise secrets.SecretError('vault unavailable')
            return original(ref, payload)

        with mock.patch.object(secrets, 'vault_for_database', return_value=vault), \
                mock.patch.object(vault, 'stage', side_effect=stage):
            with self.assertRaises(secrets.SecretError):
                importer.commit(self.conn, plan)
        self.assertEqual(members(self.conn, self.collection), [])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM accesses').fetchone()[0], 0)
        self.assertIsNone(vault.state(references[0]))


class ScheduleApiRegressionTests(unittest.TestCase):
    def setUp(self):
        self.schema = Schema()
        self.addCleanup(self.schema.close)
        self.service = api.WorkbenchService(self.schema.path.parent, db_path=self.schema.path)
        principal = apiv1.Principal('admin', permissions=frozenset(apiv1.PERMISSIONS))
        self.client = apiv1.ApiV1(self.service, FakeKeys([principal]))
        self.sequence = 0

    def call(self, method, path, body=None, revision=None):
        self.sequence += 1
        headers = {'Host': '127.0.0.1', 'Authorization': 'Bearer admin',
                   'Content-Type': 'application/json', 'Idempotency-Key': str(self.sequence)}
        if revision is not None:
            headers['If-Match'] = str(revision)
        return self.client.handle(apiv1.Request(method, path, headers=headers,
                                  body=json.dumps(body).encode() if body is not None else b''))

    def test_create_and_patch_preserve_budgets_scope_and_optimistic_revision(self):
        created = self.call('POST', '/v1/schedules', {
            'name': 'daily', 'kind': 'check', 'collection_id': db.PUBLIC_COLLECTION_ID,
            'interval_minutes': 30, 'budgets': {'max_requests': 10, 'max_bytes': 10000},
            'window': {'from': '09:00', 'to': '17:00', 'timezone': 'UTC'}})
        self.assertEqual(created.status_code, 200, created.json())
        self.assertEqual(created.json()['budgets']['requests'], 10)
        self.assertEqual(created.json()['collection_id'], db.PUBLIC_COLLECTION_ID)
        updated = self.call('PATCH', '/v1/schedules/daily', {
            'name': 'renamed', 'budgets': {'max_requests': 3},
            'window': {'from': '10:00', 'to': '16:00'}}, revision=1)
        self.assertEqual(updated.status_code, 200, updated.json())
        self.assertEqual(updated.json()['id'], 'daily')
        self.assertEqual(updated.json()['name'], 'renamed')
        self.assertEqual(updated.json()['revision'], 2)
        self.assertEqual(updated.json()['budgets']['requests'], 3)
        self.assertEqual(updated.json()['budgets']['bytes'], 10000)
        self.assertEqual(updated.json()['windows'][0]['window'], '10:00-16:00')
        stale = self.call('PATCH', '/v1/schedules/daily', {'interval_minutes': 60}, revision=1)
        self.assertEqual(stale.status_code, 409)
        loaded = self.call('GET', '/v1/schedules/daily').json()
        self.assertEqual(loaded['interval_minutes'], 30)
        self.assertEqual(loaded['action'], 'check')

    def test_counters_return_structured_runs(self):
        self.call('POST', '/v1/schedules', {'name': 'daily', 'kind': 'check', 'interval_minutes': 30})
        store = scheduler.SqliteScheduleStore(self.schema.conn)
        store.append_run('daily', scheduler.RunRequest('daily', 'r1', 'due', 1000, 1000))
        response = self.call('GET', '/v1/schedules/daily/counters')
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()['runs'][0]['id'], 'r1')


class SchedulePersistenceRegressionTests(unittest.TestCase):
    def setUp(self):
        self.schema = Schema()
        self.addCleanup(self.schema.close)
        self.store = scheduler.SqliteScheduleStore(self.schema.conn)
        self.now = 1_780_000_000.0

    def engine(self):
        return scheduler.Scheduler(self.store, clock=lambda: self.now,
                                   power_reader=scheduler.PowerSignal.unknown)

    def test_first_interval_anchor_survives_restart_before_first_run(self):
        first = self.engine()
        first.add(scheduler.ScheduleSpec(id='scan', interval_minutes=30, catch_up_grace_s=90))
        first.tick(self.now)
        persisted = self.store.load_state('scan')
        self.assertEqual(persisted.activated_at, self.now)
        self.assertEqual(persisted.next_run_at, self.now + 1800)
        second = self.engine()
        self.assertEqual(second.get('scan').catch_up_grace_s, 90)
        self.assertEqual(second.tick(self.now + 1700).run_requests, ())
        due = second.tick(self.now + 1800)
        self.assertEqual([run.scheduled_for for run in due.run_requests], [self.now + 1800])

    def test_editing_spec_keeps_next_run_and_runtime_counters(self):
        spec = scheduler.ScheduleSpec(id='scan', interval_minutes=30)
        self.store.save_spec(spec)
        state = scheduler.ScheduleState(activated_at=self.now, next_run_at=self.now + 1800,
                                         skipped=7, paused=True, pause_reason='user')
        self.store.save_state('scan', state)
        self.store.save_spec(dataclasses.replace(spec, interval_minutes=15))
        restored = self.store.load_state('scan')
        self.assertEqual(restored.next_run_at, state.next_run_at)
        self.assertEqual(restored.activated_at, self.now)
        self.assertEqual(restored.skipped, 7)
        self.assertTrue(restored.paused)

    def test_reset_ledger_is_attached_to_the_runtime_state(self):
        engine = self.engine()
        engine.add(scheduler.ScheduleSpec(id='scan', interval_minutes=30,
                   budgets=scheduler.Budgets(requests=5, inflight_reserve_requests=0)))
        ledger = engine.ledger('scan', now=self.now)
        ledger.commit(ledger.try_reserve(scheduler.PROBE, requests=3))
        self.assertEqual(engine.ledger('scan', now=self.now).snapshot().requests.used, 3)

    def test_prefixed_tables_load_their_own_column_names(self):
        conn = sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        from tests.test_scheduler_store import CONTRACT_DDL
        conn.executescript(CONTRACT_DDL.replace('schedules', 'local_schedules')
                           .replace('schedule_run', 'local_schedule_run'))
        store = scheduler.SqliteScheduleStore(conn, table_prefix='local_')
        store.save_spec(scheduler.ScheduleSpec(id='scan', interval_minutes=30))
        self.assertEqual(store.load_specs()[0].id, 'scan')

    def test_infinite_tick_times_are_refused_before_calendar_conversion(self):
        for now in (float('inf'), float('-inf')):
            with self.subTest(now=now), self.assertRaises(scheduler.ScheduleError):
                self.engine().tick(now)


class PowerRegressionTests(unittest.TestCase):
    def test_power_pause_resumes_automatically_but_a_user_pause_stays_paused(self):
        for policy, unavailable, available in (
                (scheduler.PowerPolicy(on_battery=scheduler.POWER_PAUSE),
                 scheduler.PowerSignal(on_battery=True, battery_supported=True),
                 scheduler.PowerSignal(on_battery=False, battery_supported=True)),
                (scheduler.PowerPolicy(metered=scheduler.POWER_PAUSE),
                 scheduler.PowerSignal(metered=True, metered_supported=True),
                 scheduler.PowerSignal(metered=False, metered_supported=True))):
            with self.subTest(policy=policy):
                signal = [unavailable]
                engine = scheduler.Scheduler(power_reader=lambda: signal[0])
                engine.add(scheduler.ScheduleSpec(id='scan', interval_minutes=30, power=policy))
                self.assertEqual(engine.tick(1000).run_requests, ())
                self.assertTrue(engine.state('scan').paused)
                signal[0] = available
                self.assertEqual(len(engine.tick(2800).run_requests), 1)
                self.assertFalse(engine.state('scan').paused)
                engine.pause('scan')
                self.assertEqual(engine.tick(4600).run_requests, ())
                self.assertTrue(engine.state('scan').paused)

    def test_windows_power_status_supports_battery_ac_and_unknown(self):
        for ac, flag, percent, expected_battery, expected_percent in (
                (0, 0, 37, True, 37), (1, 0, 100, False, 100),
                (255, 255, 255, None, None), (0, 128, 255, False, None)):
            with self.subTest(ac=ac, flag=flag):
                def read(pointer):
                    status = pointer._obj
                    status.ACLineStatus = ac
                    status.BatteryFlag = flag
                    status.BatteryLifePercent = percent
                    self.assertEqual(ctypes.sizeof(status), 12)
                    return 1
                windll = types.SimpleNamespace(kernel32=types.SimpleNamespace(GetSystemPowerStatus=read))
                with mock.patch.object(ctypes, 'windll', windll, create=True):
                    signal = scheduler.read_system_signal('win32')
                self.assertEqual(signal.on_battery, expected_battery)
                self.assertEqual(signal.battery_percent, expected_percent)
                self.assertTrue(signal.battery_supported)


class PoolRegressionTests(PoolTestCase):
    def test_unbounded_watch_delivers_all_ticks_but_retains_only_recent_history(self):
        self.create_pool(desired=1, reserve=0)
        received = []
        history = pools.watch(self.store, 'main', FakeSource(), clock=ManualClock(self.now),
                              on_status=lambda status: received.append(status) or len(received) < 130)
        self.assertEqual(len(received), 130)
        self.assertEqual(len(history), 100)
        self.assertEqual(history, received[-100:])

    def test_error_state_ends_registry_watch_after_one_attempt(self):
        self.create_pool(desired=1, reserve=0)
        registry = pools.WatchRegistry(lambda: pools.PoolStore.open(self.path))
        self.addCleanup(registry.stop_all)
        done = threading.Event()
        calls = []

        def failed_watch(store, pool_id, source, **kwargs):
            calls.append(pool_id)
            keep_going = kwargs['on_status'](types.SimpleNamespace(state=pools.STATE_ERROR,
                                                                  deficit_reason='source failed'))
            self.assertFalse(keep_going)
            done.set()
            return []

        with mock.patch.object(pools, 'watch', side_effect=failed_watch):
            registry.start('main', source=FakeSource())
            self.assertTrue(done.wait(3))
            with registry._lock:
                thread = registry._threads.get('main')
            if thread:
                thread.join(3)
        self.assertEqual(calls, ['main'])
        self.assertFalse(registry.watching('main'))
        self.assertNotIn('main', registry.status()['watching'])
        self.assertEqual(registry.status()['errors']['main'], 'source failed')

    def test_data_folders_have_independent_watch_registries(self):
        first = pools.watch_registry(self.path.parent)
        self.assertIs(first, pools.watch_registry(self.path.parent / '.'))
        self.assertIsNot(first, pools.watch_registry(self.path.parent / 'other'))


class ColumnCacheRegressionTests(unittest.TestCase):
    def test_schema_changes_and_rollbacks_invalidate_column_cache(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'db.sqlite3'
            conn = db.connect(path)
            other = db.connect(path)
            try:
                conn.execute('CREATE TABLE example(id TEXT)')
                self.assertEqual(db.columns(conn, 'example'), ['id'])
                other.execute('ALTER TABLE example ADD COLUMN name TEXT')
                self.assertEqual(db.columns(conn, 'example'), ['id', 'name'])
                conn.execute('BEGIN')
                conn.execute('ALTER TABLE example ADD COLUMN transient TEXT')
                self.assertIn('transient', db.columns(conn, 'example'))
                conn.rollback()
                self.assertEqual(db.columns(conn, 'example'), ['id', 'name'])
                changed = db.columns(conn, 'example')
                changed.clear()
                self.assertEqual(db.columns(conn, 'example'), ['id', 'name'])
            finally:
                other.close()
                conn.close()


if __name__ == '__main__':
    unittest.main()
