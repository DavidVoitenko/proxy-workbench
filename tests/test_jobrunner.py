"""The durable worker executes real queued jobs with local HTTP fixtures only."""
import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from proxy_workbench import api, apiv1, db, jobrunner, jobs, profiles, proxytool, scheduler, servicecatalog
from proxy_workbench.maintenance import exclusive_lock
from tests.test_apiv1_service import FakeKeys
from tests.test_workbench import config, row


class LocalProxy(BaseHTTPRequestHandler):
    calls = 0

    def do_GET(self):
        type(self).calls += 1
        body = b'healthy'
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class JobRunnerTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.data = Path(folder.name)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), LocalProxy)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        LocalProxy.calls = 0
        self.workbench = proxytool.Workbench(self.data)
        self.addCleanup(self.workbench.close)
        self.proxy = f'http://127.0.0.1:{self.server.server_port}'
        endpoint = db.upsert_endpoint(self.workbench.conn, self.proxy)
        db.add_member(self.workbench.conn, db.PUBLIC_COLLECTION_ID, endpoint, origin='import')
        cfg = config()
        (self.data / 'gui-targets.json').write_text(json.dumps(cfg), encoding='utf-8')
        self.profile, self.revision, _ = jobrunner.resolve_profile(self.workbench)
        self.runner = jobrunner.JobRunner(self.data)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)

    def submit(self, kind='check', key='one', **kwargs):
        scope = jobs.Scope(collection_id=db.PUBLIC_COLLECTION_ID, profile_id=self.profile,
                           profile_revision=1, **kwargs)
        return self.workbench.jobs().submit(kind, scope,
                                            jobrunner.queue_items(self.workbench, db.PUBLIC_COLLECTION_ID),
                                            idempotency_key=key)

    def test_queued_check_reaches_real_probe_observation_and_published_export(self):
        job = self.submit()
        report = self.runner.run_pending()[0]
        self.assertEqual(report['state'], 'succeeded', report)
        self.assertEqual(self.workbench.jobs().job(job.id).state, 'succeeded')
        self.assertEqual([item.state for item in self.workbench.jobs().items(job.id)], ['done'])
        rows = self.workbench.conn.execute('SELECT profile_id, payload, job_id FROM results').fetchall()
        self.assertEqual([(row[0], json.loads(row[1])['collection_id'], row[2]) for row in rows],
                         [(self.profile, db.PUBLIC_COLLECTION_ID, job.id)])
        self.assertGreater(LocalProxy.calls, 0)
        self.assertTrue((self.data / 'exports' / 'current.json').is_file())
        before = LocalProxy.calls
        self.assertEqual(self.runner.run_pending(), [])
        self.assertEqual(LocalProxy.calls, before, 'a completed job is never executed twice')

    def test_api_check_and_recheck_use_the_same_durable_worker(self):
        principal = apiv1.Principal('admin', permissions=frozenset(apiv1.PERMISSIONS))
        client = apiv1.ApiV1(api.WorkbenchService(self.data), FakeKeys([principal]))
        for index, operation in enumerate(('check', 'recheck')):
            response = client.handle(apiv1.Request('POST', '/v1/checks/' + operation,
                        headers={'Host': '127.0.0.1', 'Authorization': 'Bearer admin',
                                 'Content-Type': 'application/json', 'Idempotency-Key': str(index)},
                        body=json.dumps({'collection_id': db.PUBLIC_COLLECTION_ID,
                                         'profile_id': self.profile}).encode()))
            self.assertEqual(response.status_code, 202, response.json())
            report = self.runner.run_pending()[0]
            self.assertEqual(report['job_id'], response.json()['job_id'])
            self.assertEqual(report['state'], 'succeeded', report)
        self.assertGreaterEqual(LocalProxy.calls, 2)

    def api_client(self, collections=()):
        principal = apiv1.Principal('admin', permissions=frozenset(apiv1.PERMISSIONS),
                                    collections=collections)
        return apiv1.ApiV1(api.WorkbenchService(self.data), FakeKeys([principal]))

    def api_submit(self, client, operation, body, key='test'):
        return client.handle(apiv1.Request('POST', '/v1/checks/' + operation,
                    headers={'Host': '127.0.0.1', 'Authorization': 'Bearer admin',
                             'Content-Type': 'application/json', 'Idempotency-Key': key},
                    body=json.dumps(body).encode()))

    def test_api_nested_budgets_limit_real_work_and_frozen_items(self):
        extra = db.upsert_endpoint(self.workbench.conn, 'http://127.0.0.2:1')
        db.add_member(self.workbench.conn, db.PUBLIC_COLLECTION_ID, extra, origin='import')
        response = self.api_submit(self.api_client(), 'check', {
            'collection_id': db.PUBLIC_COLLECTION_ID, 'profile_id': self.profile, 'want': 1,
            'max_seconds': 10,
            'budget': {'max_requests': 1, 'max_bytes': 64, 'max_items': 1, 'max_seconds': 5}})
        self.assertEqual(response.status_code, 202, response.json())
        job = self.workbench.jobs().job(response.json()['job_id'])
        self.assertEqual(job.scope.filters['want'], 1)
        self.assertEqual(dict(job.scope.budgets), {'max_requests': 1, 'max_bytes': 64,
                                                 'max_items': 1, 'max_seconds': 5})
        self.assertEqual(len(self.workbench.jobs().items(job.id)), 1)
        report = self.runner.run_pending()[0]
        self.assertLessEqual(report['result']['requests'], 1, report)
        self.assertLessEqual(report['result']['bytes'], 64, report)
        self.assertEqual(LocalProxy.calls, 1)

    def test_quick_test_checks_only_its_named_member(self):
        extra = db.upsert_endpoint(self.workbench.conn, 'http://127.0.0.2:1')
        db.add_member(self.workbench.conn, db.PUBLIC_COLLECTION_ID, extra, origin='import')
        response = self.api_submit(self.api_client(), 'quick-test', {
            'collection_id': db.PUBLIC_COLLECTION_ID, 'profile_id': self.profile, 'endpoint': self.proxy})
        self.assertEqual(response.status_code, 202, response.json())
        job_id = response.json()['job_id']
        items = self.workbench.jobs().items(job_id)
        self.assertEqual([item.endpoint_id for item in items], [db.endpoint_id(self.proxy)])
        report = self.runner.run_pending()[0]
        self.assertEqual(report['state'], 'succeeded', report)
        self.assertEqual(LocalProxy.calls, 1)
        self.assertEqual([row[0] for row in self.workbench.conn.execute('SELECT proxy FROM results')],
                         [self.proxy])

    def test_quick_test_and_check_refuse_endpoints_outside_key_or_collection_scope(self):
        private = db.create_collection(self.workbench.conn, 'private', collection_id='private', kind='private')
        client = self.api_client(collections=(private,))
        for operation, body in (
                ('quick-test', {'collection_id': private, 'profile_id': self.profile, 'endpoint': self.proxy}),
                ('quick-test', {'collection_id': db.PUBLIC_COLLECTION_ID, 'endpoint': self.proxy}),
                ('check', {'collection_id': db.PUBLIC_COLLECTION_ID}),
                ('collect', {'collection_id': db.PUBLIC_COLLECTION_ID})):
            response = self.api_submit(client, operation, body)
            self.assertEqual(response.status_code, 404, (operation, response.json()))
        self.assertEqual(self.workbench.jobs().jobs(), [])
        self.assertEqual(LocalProxy.calls, 0)

    def test_unimplemented_modes_and_monetary_cost_are_refused_before_submit(self):
        client = self.api_client()
        for operation, extra in (('collect', {'mode': 'full'}), ('collect', {'mode': 'tcp'}),
                                 ('check', {'mode': 'tcp'}), ('check', {'mode': 'basic'}),
                                 ('check', {'mode': 'collect_only'}),
                                 ('collect', {'budget': {'max_cost': 1}}),
                                 ('check', {'budget': {'max_cost': 0}})):
            response = self.api_submit(client, operation, {'collection_id': db.PUBLIC_COLLECTION_ID,
                                                           **extra})
            self.assertEqual(response.status_code, 400, (operation, response.json()))
        self.assertEqual(self.workbench.jobs().jobs(), [])

    def test_collect_forwards_global_limits_and_remaining_candidate_goal(self):
        captured = []

        async def local_collector(conn, urls, inputs, **kwargs):
            captured.append(kwargs)
            return {'sources': [{'attempts': 1, 'bytes': 12}], 'raw_rows': 1,
                    'budget': {'requests': 1, 'bytes': 12, 'items': 1}}

        response = self.api_submit(self.api_client(), 'collect', {
            'collection_id': db.PUBLIC_COLLECTION_ID, 'want': 3, 'max_seconds': 5,
            'budget': {'max_requests': 2, 'max_bytes': 128, 'max_items': 10}})
        self.assertEqual(response.status_code, 202, response.json())
        runner = jobrunner.JobRunner(self.data, collect=local_collector)
        with mock.patch.object(proxytool, 'resolve_collect_sources', return_value=['http://source.invalid']):
            report = runner.run_pending()[0]
        self.assertEqual(report['state'], 'partial', report)
        self.assertEqual(report['result']['stop_reason'], 'want_short_endpoint')
        self.assertEqual(captured[0]['max_requests'], 2)
        self.assertEqual(captured[0]['max_bytes'], 128)
        self.assertEqual(captured[0]['max_items'], 2)
        self.assertEqual(report['result']['requests'], 1)
        self.assertEqual(report['result']['bytes'], 12)

    def test_a_new_check_measures_its_items_again_instead_of_leaving_them_pending(self):
        first = self.submit(key='first')
        self.assertEqual(self.runner.run_pending()[0]['state'], 'succeeded')
        calls = LocalProxy.calls
        second = self.submit(key='second')
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(self.runner.run_pending()[0]['state'], 'succeeded')
        self.assertGreater(LocalProxy.calls, calls)
        self.assertEqual([item.state for item in self.workbench.jobs().items(second.id)], ['done'])

    def test_named_profile_threshold_is_not_overridden_by_the_legacy_default(self):
        spec = profiles.create_spec(targets=[{'id': 'local-service', 'min_success': 0.3}], attempts=3)
        ref = self.workbench.profiles().create('Local service', spec)
        self.profile, self.revision = ref.profile_id, ref.revision
        probe = servicecatalog.Probe('local', 'http://service.invalid/check', 'GET', (200,),
                                     'healthy', None, (), 'local fixture', 'local fixture')
        catalog = SimpleNamespace(preset=lambda _: SimpleNamespace(probes=(probe,), title_ru='local'))

        async def partial_probe(proxy, cfg, rate, own_ips=None):
            result = row(proxy, cfg)
            for sample in result['samples'][1:]:
                sample['ok'] = False
            return proxytool.summarize(proxy, result['samples'], cfg)

        self.submit()
        with mock.patch.object(servicecatalog, 'load_catalog', return_value=catalog), \
                mock.patch.object(proxytool, 'check_proxy', side_effect=partial_probe):
            report = self.runner.run_pending()[0]
        self.assertEqual(report['state'], 'succeeded', report)
        self.assertEqual(report['result']['passed'], 1)

    def test_an_existing_cli_lock_leaves_the_job_queued(self):
        job = self.submit()
        with exclusive_lock(self.data / 'workbench.lock'):
            self.assertEqual(self.runner.run_pending(), [])
        self.assertEqual(self.workbench.jobs().job(job.id).state, 'queued')
        self.assertEqual(LocalProxy.calls, 0)
        self.assertEqual(self.runner.run_pending()[0]['state'], 'succeeded')

    def test_cli_resume_marks_fresh_items_reused_without_another_measurement(self):
        first = self.submit(key='original')
        self.assertEqual(self.runner.run_pending()[0]['state'], 'succeeded')
        previous_item = self.workbench.jobs().items(first.id)[0]
        calls = LocalProxy.calls
        resumed = self.submit(key='resume')
        self.workbench.jobs().start(resumed.id)
        _id, revision, cfg = jobrunner.resolve_profile(self.workbench, self.profile)
        state = {}
        asyncio.run(proxytool.scan(self.workbench.conn, cfg, workers=1, rate=0, progress=False,
                                    profile_id=self.profile, profile_revision=revision,
                                    job_id=resumed.id, job_store=self.workbench.jobs(), run_state=state))
        self.assertEqual(LocalProxy.calls, calls)
        item = self.workbench.jobs().items(resumed.id)[0]
        self.assertEqual(item.state, 'done')
        self.assertEqual(item.observation_id, previous_item.observation_id)
        self.assertEqual(state['pending'], 0)

    def test_members_added_after_submit_stay_outside_the_frozen_job(self):
        job = self.submit()
        with ThreadingHTTPServer(('127.0.0.1', 0), LocalProxy) as other:
            thread = threading.Thread(target=other.serve_forever, daemon=True)
            thread.start()
            try:
                later = f'http://127.0.0.1:{other.server_port}'
                endpoint = db.upsert_endpoint(self.workbench.conn, later)
                db.add_member(self.workbench.conn, db.PUBLIC_COLLECTION_ID, endpoint, origin='import')
                report = self.runner.run_pending()[0]
                self.assertEqual(report['state'], 'succeeded', report)
                measured = [row[0] for row in self.workbench.conn.execute(
                    'SELECT proxy FROM results WHERE job_id=?', (job.id,))]
                self.assertEqual(measured, [self.proxy])
            finally:
                other.shutdown()
                thread.join(3)

    def test_bad_profile_finishes_failed_instead_of_sticking_queued(self):
        job = self.workbench.jobs().submit('check', jobs.Scope(
            collection_id=db.PUBLIC_COLLECTION_ID, profile_id='missing', profile_revision=1))
        report = self.runner.run_pending()[0]
        self.assertEqual(report['state'], 'failed', report)
        self.assertEqual(self.workbench.jobs().job(job.id).state, 'failed')
        self.assertEqual(LocalProxy.calls, 0)

    def test_pause_and_cancel_interrupt_an_inflight_scan(self):
        for operation, expected in (('pause', 'paused'), ('cancel', 'cancelled')):
            with self.subTest(operation=operation):
                started, interrupted = threading.Event(), threading.Event()

                async def slow_scan(*args, **kwargs):
                    started.set()
                    try:
                        await asyncio.sleep(60)
                    finally:
                        interrupted.set()

                runner = jobrunner.JobRunner(self.data, scan=slow_scan)
                job = self.submit(key=operation)
                results = []
                thread = threading.Thread(target=lambda: results.extend(runner.run_pending()))
                thread.start()
                try:
                    self.assertTrue(started.wait(3))
                    getattr(self.workbench.jobs(), operation)(job.id)
                    self.assertTrue(interrupted.wait(3), 'control must cancel pending I/O')
                finally:
                    runner._stop.set()
                    thread.join(3)
                self.assertFalse(thread.is_alive())
                self.assertEqual(results[0]['state'], expected)
                self.assertEqual(LocalProxy.calls, 0)

    def test_item_lifecycle_inside_a_scan_transaction_rolls_back_with_it(self):
        job = self.submit()
        self.workbench.jobs().start(job.id)
        self.workbench.conn.execute('BEGIN IMMEDIATE')
        item = self.workbench.jobs().claim(job.id)
        self.assertEqual(item.state, 'probing')
        self.workbench.conn.rollback()
        self.assertEqual(self.workbench.jobs().items(job.id)[0].state, 'pending')

    def test_schedule_dispatch_deduplicates_and_records_spent_budget(self):
        engine = scheduler.Scheduler(self.workbench.schedules(), clock=lambda: 1000,
                                     power_reader=scheduler.PowerSignal.unknown)
        engine.add(scheduler.ScheduleSpec(id='periodic', interval_minutes=1,
                   collection_id=db.PUBLIC_COLLECTION_ID, budgets=scheduler.Budgets(requests=50)))
        run = engine.run_now('periodic', at=1000)
        job = jobrunner.submit_schedule(self.data, run)
        self.assertEqual(jobrunner.submit_schedule(self.data, run).id, job.id)
        report = self.runner.run_pending()[0]
        self.assertEqual(report['state'], 'succeeded', report)
        record = self.workbench.conn.execute('SELECT state, counters_json FROM schedule_run WHERE id=?',
                                             (run.run_id,)).fetchone()
        self.assertEqual(record[0], 'succeeded')
        self.assertEqual(json.loads(record[1])['job_id'], job.id)
        self.assertGreater(self.workbench.schedules().load_state('periodic').counters.requests, 0)

    def test_schedule_budget_accumulates_cost_across_pause_and_smaller_resume(self):
        engine = scheduler.Scheduler(self.workbench.schedules(),
                                     power_reader=scheduler.PowerSignal.unknown)
        engine.add(scheduler.ScheduleSpec(id='resume-cost', interval_minutes=1,
                   collection_id=db.PUBLIC_COLLECTION_ID, budgets=scheduler.Budgets(requests=50)))
        run = engine.run_now('resume-cost')
        job = jobrunner.submit_schedule(self.data, run)
        costs = [(5, 500), (2, 200)]

        async def scan_segment(conn, config, **kwargs):
            requests, received = costs.pop(0)
            kwargs['run_state'].update(state='partial', requests=requests, bytes=received)
            kwargs['job_store'].pause(kwargs['job_id'])

        runner = jobrunner.JobRunner(self.data, scan=scan_segment)
        first = runner.run_pending()[0]
        self.assertEqual(first['state'], 'paused')
        self.workbench.jobs().resume(job.id)
        second = runner.run_pending()[0]
        self.assertEqual(second['state'], 'paused')
        self.assertEqual(second['result']['requests'], 7)
        self.assertEqual(second['result']['bytes'], 700)
        persisted = self.workbench.schedules().load_state('resume-cost').counters
        self.assertEqual(persisted.requests, 7)
        self.assertEqual(persisted.bytes, 700)
        self.assertGreater(persisted.seconds, first['result']['seconds'])

    def test_resume_can_spend_only_the_remaining_job_budget(self):
        limits = []

        async def bounded_segment(conn, config, **kwargs):
            limits.append((kwargs['max_requests'], kwargs['max_bytes']))
            spent = min(2, kwargs['max_requests'])
            kwargs['run_state'].update(state='partial', requests=spent, bytes=100 * spent)
            kwargs['job_store'].pause(kwargs['job_id'])

        job = self.submit(budgets={'max_requests': 3, 'max_bytes': 300})
        runner = jobrunner.JobRunner(self.data, scan=bounded_segment)
        self.assertEqual(runner.run_pending()[0]['state'], 'paused')
        self.workbench.jobs().resume(job.id)
        second = runner.run_pending()[0]
        self.assertEqual(second['state'], 'paused')
        self.assertEqual(limits, [(3, 300), (1, 100)])
        self.assertEqual(second['result']['requests'], 3)
        self.workbench.jobs().resume(job.id)
        exhausted = runner.run_pending()[0]
        self.assertEqual(exhausted['state'], 'partial')
        self.assertEqual(exhausted['result']['stop_reason'], 'E_LIMIT_BUDGET')
        self.assertEqual(exhausted['result']['requests'], 3)
        self.assertEqual(len(limits), 2, 'an exhausted job must never probe again')

    def test_headless_worker_ticks_due_schedules_and_executes_without_desktop(self):
        engine = scheduler.Scheduler(self.workbench.schedules(),
                                     power_reader=scheduler.PowerSignal.unknown)
        engine.add(scheduler.ScheduleSpec(id='headless', interval_minutes=1,
                   collection_id=db.PUBLIC_COLLECTION_ID, budgets=scheduler.Budgets(requests=50)))
        self.workbench.schedules().save_state('headless', scheduler.ScheduleState(
            activated_at=time.time() - 65, next_run_at=time.time() - 5))
        self.runner.start()
        try:
            deadline = time.monotonic() + 5
            completed = []
            while time.monotonic() < deadline:
                completed = self.workbench.jobs().jobs(state='succeeded')
                if completed:
                    break
                time.sleep(0.02)
            self.assertEqual(len(completed), 1, self.runner.last_error)
            self.assertEqual(completed[0].scope.filters['schedule_id'], 'headless')
            self.assertEqual(self.workbench.jobs().items(completed[0].id)[0].state, 'done')
            self.assertGreater(LocalProxy.calls, 0)
        finally:
            self.runner.stop()

    def test_server_runtime_is_opt_in_and_stops_with_its_last_owner(self):
        with mock.patch.object(jobrunner.JobRunner, 'run_pending', return_value=[]):
            server = api.make_api_server(self.data, port=0, execute_jobs=True)
            worker = server.job_runner
            serve = threading.Thread(target=server.serve_forever, daemon=True)
            serve.start()
            try:
                worker.start()  # another surface owns the same worker
                server.shutdown()
                serve.join(3)
                self.assertEqual(worker._users, 1)
                self.assertTrue(worker._thread.is_alive())
                worker.stop()
                self.assertFalse(worker._thread.is_alive())
            finally:
                server.server_close()


if __name__ == '__main__':
    unittest.main()
