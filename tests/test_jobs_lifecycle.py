"""Job lifecycle: fixed input, one writer, honest transitions, honest progress.

CONTRACTS.ru.md §6.2 and F11.
"""
from __future__ import annotations

import dataclasses
import sqlite3
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_jobs_support import JobFixture  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import jobs  # noqa: E402

try:
    from proxy_workbench import db
except ImportError:  # pragma: no cover
    db = None


class ScopeIsFixedAtInputTests(JobFixture):
    def test_scope_and_profile_revision_survive_the_whole_lifecycle(self):
        job = self.submit('ep-1', 'ep-2', profile_revision=4, timeout_s=120.0)
        original = job.scope.canonical()
        self.store.start(job.id)
        self.work(job.id, {'ep-1': 'done'})
        self.store.pause(job.id)
        self.store.resume(job.id)
        self.store.start(job.id)
        self.store.finish(job.id)
        final = self.store.job(job.id)
        self.assertEqual(final.scope.canonical(), original)
        self.assertEqual(final.profile_revision, 4)
        self.assertEqual(final.input_digest, job.input_digest)
        self.assertEqual(final.collection_id, 'col-public')

    def test_a_scope_object_cannot_be_edited_after_the_job_owns_it(self):
        scope = self.scope()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            scope.profile_revision = 9
        with self.assertRaises(TypeError):
            scope.filters['protocol'] = 'socks5'
        with self.assertRaises(TypeError):
            scope.budgets['timeout_s'] = 1.0

    def test_scope_fields_are_validated(self):
        for build in (lambda: jobs.Scope('', 'prof-1', 1), lambda: jobs.Scope('col', 'prof-1', 0),
                      lambda: jobs.Scope('col', 'prof-1', True),
                      lambda: jobs.Scope('col', 'prof-1', 1, filters={'targets': {1, 2}})):
            with self.assertRaises(jobs.Validation):
                build()
        with self.assertRaises(jobs.Validation):
            self.scope(timeout_s=0).timeout_s


class IdempotentRequestTests(JobFixture):
    def test_repeat_of_the_same_request_does_not_create_a_second_job(self):
        first = self.submit('ep-1', 'ep-2', 'ep-3', idempotency_key='request-7')
        before = self.store.last_seq(first.id)
        second = self.submit('ep-1', 'ep-2', 'ep-3', idempotency_key='request-7')
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(self.store.jobs()), 1)
        self.assertEqual(len(self.store.items(first.id)), 3)
        self.assertEqual(self.store.last_seq(first.id), before, 'a replay must not emit events')

    def test_the_same_key_with_another_scope_is_a_conflict(self):
        self.submit('ep-1', idempotency_key='request-7')
        with self.assertRaises(jobs.IdempotencyConflict) as caught:
            self.submit('ep-2', idempotency_key='request-7')
        self.assertEqual(caught.exception.code, 'E_CONFLICT_IDEMPOTENCY')
        self.assertEqual(len(self.store.jobs()), 1)

    def test_requests_without_a_key_are_separate_jobs(self):
        first = self.submit('ep-1')
        second = self.submit('ep-1')
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(len(self.store.jobs()), 2)


class StateMachineTests(JobFixture):
    def test_a_finished_run_reaches_succeeded_with_every_item_terminal(self):
        job = self.submit('ep-1', 'ep-2')
        self.store.start(job.id)
        self.work(job.id, {'ep-1': 'done', 'ep-2': 'unreachable'})
        finished = self.store.finish(job.id)
        self.assertEqual(finished.state, 'succeeded')
        self.assertIsNotNone(finished.finished_at)
        self.assertEqual(self.store.unfinished(job.id), 0)

    def test_stopping_with_unfinished_items_ends_as_partial(self):
        job = self.submit('ep-1', 'ep-2', 'ep-3')
        self.store.start(job.id)
        self.work(job.id, {'ep-1': 'done'})
        self.assertEqual(self.store.finish(job.id).state, 'partial')
        self.assertEqual(self.store.unfinished(job.id), 2)

    def test_a_cancelled_job_keeps_its_queue_readable(self):
        job = self.submit('ep-1', 'ep-2')
        self.store.start(job.id)
        self.work(job.id, {'ep-1': 'done'})
        self.store.cancel(job.id, reason_code='E_STATE_POOL_EMPTY')
        self.assertEqual(self.store.job(job.id).state, 'cancelled')
        self.assertEqual(self.store.unfinished(job.id), 1)
        self.assertEqual(self.store.item(job.id, 'ep-1').observation_id, 'obs-ep-1')

    def test_forbidden_transitions_name_a_code(self):
        created = self.store.create_job('scan', self.scope(), self.items('ep-1'))
        with self.assertRaises(jobs.StateConflict) as caught:
            self.store.start(created.id)
        self.assertEqual(caught.exception.code, 'E_STATE_JOB_TRANSITION')

        job = self.submit('ep-1')
        self.store.start(job.id)
        with self.assertRaises(jobs.StateConflict):
            self.store.resume(job.id)
        self.assertEqual(self.store.finish(job.id).state, 'partial')
        for call in (lambda: self.store.pause(job.id), lambda: self.store.cancel(job.id),
                     lambda: self.store.resume(job.id)):
            with self.assertRaises(jobs.StateConflict):
                call()
        self.assertEqual(self.store.job(job.id).state, 'partial')

    def test_a_failure_keeps_a_diagnosis_and_the_measurements(self):
        job = self.submit('ep-1', 'ep-2')
        self.store.start(job.id)
        self.work(job.id, {'ep-1': 'done'})
        failed = self.store.fail(job.id, reason_code='E_DATA_MIGRATION_FAILED', detail='no writer')
        self.assertEqual(failed.state, 'failed')
        self.assertEqual(self.store.item(job.id, 'ep-1').observation_id, 'obs-ep-1')
        self.assertEqual(self.store.unfinished(job.id), 1)
        self.assertIn('E_DATA_MIGRATION_FAILED', [event.code for event in self.store.events(job.id)])

    def test_only_one_job_may_run_at_a_time(self):
        first = self.submit('ep-1')
        second = self.submit('ep-2')
        self.store.start(first.id)
        with self.assertRaises(jobs.Busy) as caught:
            self.store.start(second.id)
        self.assertEqual(caught.exception.code, 'E_CONFLICT_BUSY')
        self.assertEqual(self.store.job(second.id).state, 'queued')

    def test_control_actions_are_idempotent_by_state_and_by_key(self):
        job = self.submit('ep-1', 'ep-2')
        self.store.start(job.id)
        self.store.pause(job.id, idempotency_key='pause-1')
        events = [event for event in self.store.events(job.id) if event.data.get('action') == 'pause']
        self.assertEqual(len(events), 1)
        self.assertEqual(self.store.pause(job.id, idempotency_key='pause-1').state, 'paused')
        self.assertEqual(self.store.pause(job.id).state, 'paused')
        self.assertEqual(len([e for e in self.store.events(job.id) if e.data.get('action') == 'pause']), 1)

        self.store.resume(job.id)
        self.store.start(job.id)
        self.assertEqual(self.store.job(job.id).state, 'running')
        self.assertEqual(self.store.pause(job.id, idempotency_key='pause-1').state, 'running',
                         'a replayed pause must not stop a job that has since resumed')

    def test_cancel_is_idempotent(self):
        job = self.submit('ep-1')
        self.store.cancel(job.id)
        self.assertEqual(self.store.cancel(job.id).state, 'cancelled')
        self.assertEqual(len([e for e in self.store.events(job.id) if e.data.get('action') == 'cancel']), 1)


class BudgetTests(JobFixture):
    def test_the_whole_job_deadline_ends_the_run_and_keeps_the_queue(self):
        job = self.submit('ep-1', 'ep-2', timeout_s=30.0)
        self.store.start(job.id)
        self.work(job.id, {'ep-1': 'done'})
        self.clock.advance(10)
        self.assertEqual(self.store.enforce_deadline(job.id).state, 'running')
        self.clock.advance(25)
        timed_out = self.store.enforce_deadline(job.id)
        self.assertEqual(timed_out.state, 'timed_out')
        self.assertEqual(timed_out.finished_at, self.clock.now)
        self.assertEqual(self.store.unfinished(job.id), 1)
        self.assertEqual(self.store.item(job.id, 'ep-1').observation_id, 'obs-ep-1')
        self.assertEqual(self.store.progress(job.id).state_detail, 'budget_exhausted')

    def test_a_job_without_a_budget_never_times_out(self):
        job = jobs.JobStore(self.conn, clock=self.clock).submit(
            'scan', jobs.Scope('col-public', 'prof-1', 1), self.items('ep-1'))
        self.store.start(job.id)
        self.clock.advance(10 ** 6)
        self.assertEqual(self.store.enforce_deadline(job.id).state, 'running')
        self.assertIsNone(self.store.progress(job.id).deadline_at)


class ProgressTests(JobFixture):
    def test_counters_come_from_one_grouped_read(self):
        job = self.submit('ep-1', 'ep-2', 'ep-3', 'ep-4')
        self.store.start(job.id)
        self.work(job.id, {'ep-1': 'done', 'ep-2': 'unreachable', 'ep-3': 'blocked'})
        progress = self.store.progress(job.id)
        self.assertEqual(progress.total, 4)
        self.assertEqual(progress.finished, 3)
        self.assertEqual(progress.unfinished, 1)
        self.assertEqual(progress.by_state['pending'], 1)
        self.assertEqual(progress.by_state['done'], 1)
        self.assertEqual(progress.to_json()['stream'], jobs.stream_id(job.id))

    def test_state_detail_explains_why_a_run_ended(self):
        empty = self.store.submit('scan', self.scope(), [])
        self.store.start(empty.id)
        self.store.finish(empty.id)
        self.assertEqual(self.store.progress(empty.id).state_detail, 'empty_no_match')

        good = self.submit('ep-1')
        self.store.start(good.id)
        self.work(good.id, {'ep-1': 'done'})
        self.store.finish(good.id)
        self.assertEqual(self.store.progress(good.id).state_detail, 'complete')

        bad = self.submit('ep-1', 'ep-2')
        self.store.start(bad.id)
        self.work(bad.id, {'ep-1': 'failed', 'ep-2': 'unreachable'})
        self.store.finish(bad.id)
        self.assertEqual(self.store.progress(bad.id).state_detail, 'all_failed')

        stopped = self.submit('ep-1', 'ep-2')
        self.store.start(stopped.id)
        self.work(stopped.id, {'ep-1': 'done'})
        self.store.pause(stopped.id)
        self.assertEqual(self.store.progress(stopped.id).state_detail, 'stopped')

    def test_elapsed_and_remaining_follow_the_injected_clock(self):
        job = self.submit('ep-1', timeout_s=90.0)
        self.store.start(job.id)
        self.clock.advance(15)
        progress = self.store.progress(job.id)
        self.assertAlmostEqual(progress.elapsed_s, 15.0, places=3)
        self.assertAlmostEqual(progress.remaining_s, 75.0, places=3)


class WriterGateTests(JobFixture):
    def test_a_second_writer_is_refused_with_a_conflict_code(self):
        gate = jobs.WriterGate()
        with gate.hold('scan-worker'):
            self.assertEqual(gate.owner, 'scan-worker')
            error = []

            def other():
                try:
                    with gate.hold('import-worker'):
                        pass
                except jobs.Busy as exc:
                    error.append(exc)

            thread = threading.Thread(target=other)
            thread.start()
            thread.join(5)
        self.assertEqual(len(error), 1)
        self.assertEqual(error[0].code, 'E_CONFLICT_BUSY')
        self.assertIsNone(gate.owner)

    def test_the_integrator_can_plug_the_existing_file_lock_in(self):
        import contextlib
        taken = []

        @contextlib.contextmanager
        def exclusive_lock(path):
            taken.append(path)
            if len(taken) > 1:
                raise OSError('workbench.lock is held')
            yield

        gate = jobs.WriterGate(os_lock=exclusive_lock)
        with gate.hold('worker-1', '/data/workbench.lock'):
            pass
        self.assertEqual(taken, ['/data/workbench.lock'])
        with self.assertRaises(jobs.Busy):
            with gate.hold('worker-2', '/data/workbench.lock'):
                pass

    def test_the_gate_is_released_after_an_error(self):
        gate = jobs.WriterGate()
        with self.assertRaises(RuntimeError):
            with gate.hold('worker'):
                raise RuntimeError('worker died')
        self.assertFalse(gate.busy)
        with gate.hold('worker-2'):
            self.assertEqual(gate.owner, 'worker-2')


class SchemaTests(JobFixture):
    def test_install_schema_is_idempotent(self):
        query = ("SELECT name, sql FROM sqlite_master WHERE type='table'"
                 " AND (name LIKE 'job%' OR name='checkpoint')")
        before = self.conn.execute(query).fetchall()
        jobs.install_schema(self.conn)
        after = self.conn.execute(query).fetchall()
        self.assertEqual(before, after)
        self.assertEqual({name for name, _ in before},
                         {'job', 'job_item', 'job_event', 'checkpoint'})

    def test_the_store_uses_the_migration_six_columns(self):
        for table in ('job', 'job_item', 'job_event', 'checkpoint'):
            columns = [row[1] for row in self.conn.execute(f'PRAGMA table_info({table})')]
            self.assertTrue(columns, table)
        self.assertEqual(
            [row[1] for row in self.conn.execute('PRAGMA table_info(job)')],
            ['id', 'kind', 'state', 'scope_json', 'input_digest', 'profile_id', 'profile_revision',
             'collection_id', 'idempotency_key', 'created_at', 'started_at', 'finished_at'])
        self.assertEqual(
            [row[1] for row in self.conn.execute('PRAGMA table_info(job_item)')],
            ['job_id', 'item_id', 'endpoint_id', 'access_id', 'access_revision', 'state',
             'observation_id', 'error_code'])
        self.assertEqual(jobs.MIGRATION, 6)

    def test_the_migrator_and_the_store_agree_on_migration_six(self):
        self.assertEqual(self.migrated_by, 'db.migrate' if db is not None else 'jobs.install_schema')
        if db is None:  # pragma: no cover - only while db.py is not in the tree
            self.skipTest('db.migrate is not available in this tree')
        standalone = sqlite3.connect(':memory:', isolation_level=None)
        try:
            jobs.install_schema(standalone)
            for table in ('job', 'job_item', 'job_event', 'checkpoint'):
                self.assertEqual(
                    [row[1:] for row in standalone.execute(f'PRAGMA table_info({table})')],
                    [row[1:] for row in self.conn.execute(f'PRAGMA table_info({table})')],
                    f'{table}: db.migrate and jobs.SCHEMA describe different tables')
        finally:
            standalone.close()

    def test_a_foreign_key_breaks_a_job_item_of_an_unknown_job(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute('INSERT INTO job_item (job_id, item_id, state) VALUES (?,?,?)',
                              ('job-missing', 'ep-1', 'pending'))

    def test_the_store_works_on_a_connection_left_in_the_default_mode(self):
        # `open_db` hands out a connection with the stdlib default
        # isolation_level, so the store must not depend on autocommit.
        default_mode = sqlite3.connect(self.path, isolation_level='')
        try:
            default_mode.execute('PRAGMA foreign_keys=ON')
            store = jobs.JobStore(default_mode, clock=self.clock)
            job = store.submit('scan', self.scope(), self.items('ep-1'))
            store.start(job.id)
            self.assertEqual(default_mode.isolation_level, '', 'the caller setting is restored')
            self.assertEqual(store.unfinished(job.id), 1)
            self.assertEqual(store.job(job.id).state, 'running')
        finally:
            default_mode.close()
        self.assertEqual(self.store.job(job.id).state, 'running',
                         'the work of a committed transaction is visible to everyone')


if __name__ == '__main__':
    import unittest
    unittest.main()
