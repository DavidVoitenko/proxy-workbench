"""Structured events and progress, and the secrets that must never reach them.

CONTRACTS.ru.md §5.6 (events), §3.3 (migration 6), F29 (canary secrets).
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_jobs_support import JobFixture  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import jobs  # noqa: E402

EVENT_KEYS = {'stream', 'seq', 'at', 'type', 'job_id', 'item_id', 'code', 'data'}


class EventStreamTests(JobFixture):
    def test_events_are_sequential_and_gapless_per_job(self):
        job = self.submit('ep-1', 'ep-2', 'ep-3')
        self.store.start(job.id)
        self.work(job.id, {'ep-1': 'done', 'ep-2': 'unreachable', 'ep-3': 'done'})
        self.store.finish(job.id)
        events = self.store.events(job.id)
        self.assertEqual([event.seq for event in events], list(range(1, len(events) + 1)))
        self.assertEqual(self.store.last_seq(job.id), len(events))

    def test_two_jobs_have_independent_sequences(self):
        first = self.submit('ep-1')
        second = self.submit('ep-2', 'ep-3')
        self.store.start(first.id)
        self.work(first.id, {'ep-1': 'done'})
        self.store.finish(first.id)
        self.store.start(second.id)
        self.work(second.id, {'ep-2': 'done', 'ep-3': 'done'})
        self.store.finish(second.id)
        self.assertEqual([event.seq for event in self.store.events(first.id)],
                         list(range(1, len(self.store.events(first.id)) + 1)))
        self.assertEqual([event.seq for event in self.store.events(second.id)],
                         list(range(1, len(self.store.events(second.id)) + 1)))
        self.assertLess(self.store.last_seq(first.id), self.store.last_seq(second.id),
                        'seq is per job, not global across jobs')
        self.assertTrue(all(event.job_id == first.id for event in self.store.events(first.id)))
        self.assertTrue(all(event.job_id == second.id for event in self.store.events(second.id)))

    def test_a_cursor_returns_only_what_came_after_it(self):
        job = self.submit('ep-1', 'ep-2')
        self.store.start(job.id)
        self.work(job.id, {'ep-1': 'done'})
        middle = self.store.last_seq(job.id)
        self.work(job.id, {'ep-2': 'done'})
        tail = self.store.events(job.id, after_seq=middle)
        self.assertEqual([event.seq for event in tail], [middle + 1, middle + 2])
        self.assertEqual(self.store.events(job.id, after_seq=self.store.last_seq(job.id)), [])

    def test_a_measurement_is_one_event_per_item_not_an_aggregated_line(self):
        job = self.submit('ep-1', 'ep-2', 'ep-3')
        self.store.start(job.id)
        self.work(job.id, {'ep-1': 'done', 'ep-2': 'done'})
        self.store.claim(job.id, item_id='ep-3')
        self.measure(job.id, 'ep-3', state='unreachable', error_code='UNREACHABLE')
        verdicts = self.store.events(job.id, types=('item.verdict',))
        self.assertEqual(len(verdicts), 3)
        self.assertEqual([event.item_id for event in verdicts], ['ep-1', 'ep-2', 'ep-3'])
        self.assertEqual([event.code for event in verdicts],
                         [jobs.CODE_OK, jobs.CODE_OK, 'UNREACHABLE'])
        self.assertTrue(all('checked' not in json.dumps(event.to_json()) for event in verdicts),
                        'an aggregated progress line is not a measurement event')

    def test_the_event_shape_is_the_contract_shape(self):
        job = self.submit('ep-1')
        self.store.start(job.id)
        self.work(job.id, {'ep-1': 'done'})
        for event in self.store.events(job.id):
            payload = event.to_json()
            self.assertEqual(set(payload), EVENT_KEYS)
            self.assertEqual(payload['stream'], f'job:{job.id}')
            self.assertEqual(payload['job_id'], job.id)
            self.assertIsInstance(payload['at'], float)
            self.assertIsInstance(payload['data'], dict)
            json.dumps(payload)

    def test_types_and_codes_are_validated(self):
        job = self.submit('ep-1')
        with self.assertRaises(jobs.Validation):
            self.store.emit(job.id, '')
        with self.assertRaises(jobs.Validation):
            self.store.emit(job.id, 'job.progress', '')
        with self.assertRaises(jobs.JobNotFound):
            self.store.emit('job-nope', 'job.progress')

    def test_an_access_id_is_public_data_and_travels_with_the_event(self):
        job = self.store.submit('scan', self.scope(), [jobs.QueueItem('ep-1', access_id='acc-1',
                                                                        access_revision=3)])
        self.store.start(job.id)
        self.work(job.id, {'ep-1': 'done'})
        item = self.store.item(job.id, 'ep-1')
        self.assertEqual((item.access_id, item.access_revision), ('acc-1', 3))
        verdicts = self.store.events(job.id, types=('item.verdict',))
        self.assertEqual(verdicts[0].data['observation_id'], 'obs-ep-1')


class HistoryTests(JobFixture):
    def test_a_cancelled_job_keeps_its_whole_event_log(self):
        job = self.submit('ep-1', 'ep-2')
        self.store.start(job.id)
        self.work(job.id, {'ep-1': 'done'})
        before = [event.to_json() for event in self.store.events(job.id)]
        self.store.cancel(job.id, reason_code='E_CONFLICT_BUSY')
        after = self.store.events(job.id)
        self.assertEqual([event.to_json() for event in after[:len(before)]], before,
                         'cancelling must not rewrite what already happened')
        self.assertEqual(after[-1].code, 'E_CONFLICT_BUSY')
        self.assertEqual(after[-1].data['to'], 'cancelled')

    def test_a_failed_job_keeps_its_diagnosis_and_its_measurements(self):
        job = self.submit('ep-1', 'ep-2')
        self.store.start(job.id)
        self.work(job.id, {'ep-1': 'done'})
        self.store.fail(job.id, reason_code='E_LIMIT_BUDGET', detail='requests exhausted')
        events = self.store.events(job.id)
        self.assertEqual(events[-1].code, 'E_LIMIT_BUDGET')
        self.assertEqual(events[-1].data['detail'], 'requests exhausted')
        self.assertEqual(self.store.item(job.id, 'ep-1').observation_id, 'obs-ep-1')
        self.assertEqual(self.store.unfinished(job.id), 1)

    def test_the_store_never_writes_outside_its_own_four_tables(self):
        statements: list[str] = []
        self.conn.set_trace_callback(statements.append)
        job = self.submit('ep-1', 'ep-2', 'ep-3')
        self.store.start(job.id)
        self.work(job.id, {'ep-1': 'done'})
        self.store.save_checkpoint(job.id, 'queue', {'offset': 1})
        self.store.pause(job.id)
        self.store.resume(job.id, member_ids=['ep-1', 'ep-2'])
        self.store.start(job.id)
        self.work(job.id, {'ep-2': 'done'})
        self.store.cancel(job.id)
        self.store.retry(job.id)
        self.clock.advance(10 ** 4)
        self.store.recover()
        self.conn.set_trace_callback(None)

        self.assertTrue(statements, 'the trace callback saw nothing')
        foreign = re.compile(r'\b(results|observations|candidates|endpoints|membership|profiles|'
                             r'collections|accesses|pools|api_keys)\b')
        for statement in statements:
            lowered = ' '.join(statement.lower().split())
            self.assertIsNone(foreign.search(lowered), f'foreign table touched: {statement}')
            if lowered.startswith('delete from'):
                self.assertTrue(lowered.startswith('delete from checkpoint'), lowered)
            self.assertFalse(lowered.startswith('drop table'), lowered)

    def test_nothing_is_deleted_from_a_job_that_happened(self):
        job = self.submit('ep-1', 'ep-2')
        self.store.start(job.id)
        self.work(job.id, {'ep-1': 'done'})
        self.store.cancel(job.id)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM job WHERE id=?', (job.id,)).fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM job_item WHERE job_id=?',
                                           (job.id,)).fetchone()[0], 2)
        self.assertEqual(self.store.item(job.id, 'ep-1').observation_id, 'obs-ep-1')


class SecretTests(JobFixture):
    def test_credentials_are_refused_in_events_and_checkpoints(self):
        canary = self.canary()
        job = self.submit('ep-1')
        self.store.start(job.id)
        leaks = [
            {'password': canary},
            {'note': f'http://user:{canary}@proxy.invalid:80'},
            {'nested': [{'api_key': canary}]},
            {'detail': {'credentials': {'login': 'u', 'password': canary}}},
        ]
        for payload in leaks:
            with self.assertRaises(jobs.Validation) as caught:
                self.store.emit(job.id, 'job.progress', 'E_VALIDATION_FIELD', payload)
            self.assertEqual(caught.exception.code, 'E_VALIDATION_FIELD')
        with self.assertRaises(jobs.Validation):
            self.store.save_checkpoint(job.id, 'queue', {'password': canary})
        with self.assertRaises(jobs.Validation):
            self.store.fail(job.id, reason_code='E_SECRET_UPSTREAM_AUTH_FAILED',
                            detail=f'upstream http://user:{canary}@proxy.invalid:80 refused the probe')
        with self.assertRaises(jobs.Validation):
            self.measure(job.id, 'ep-1', verdict={'reliability': 1.0, 'password': canary})

    def test_a_refused_verdict_still_leaves_the_item_where_it_was(self):
        canary = self.canary()
        job = self.submit('ep-1')
        self.store.start(job.id)
        self.store.claim(job.id)
        with self.assertRaises(jobs.Validation):
            self.measure(job.id, 'ep-1', verdict={'password': canary})
        self.assertEqual(self.store.item(job.id, 'ep-1').state, 'probing')
        self.assertNotIn(canary.encode(), self.db_bytes())

    def test_the_canary_reaches_neither_the_database_nor_the_json(self):
        canary = self.canary()
        job = self.submit('ep-1', 'ep-2')
        self.store.start(job.id)
        with self.assertRaises(jobs.Validation):
            self.store.emit(job.id, 'job.progress', 'E_VALIDATION_FIELD', {'password': canary})
        self.work(job.id, {'ep-1': 'done'})
        self.store.save_checkpoint(job.id, 'queue', {'offset': 1})
        self.store.cancel(job.id)

        self.assertNotIn(canary.encode(), self.db_bytes())
        stored = self.conn.execute(
            'SELECT count(*) FROM job_event WHERE data_json LIKE ? OR data_json LIKE ?',
            (f'%{canary}%', f'%{canary}%')).fetchone()[0]
        self.assertEqual(stored, 0)
        exported = json.dumps({
            'job': self.store.job(job.id).to_json(),
            'progress': self.store.progress(job.id).to_json(),
            'events': [event.to_json() for event in self.store.events(job.id)],
            'checkpoints': self.store.checkpoint(job.id),
            'items': [item.to_json() for item in self.store.items(job.id)],
        })
        self.assertNotIn(canary, exported)

    def test_the_refusal_message_never_quotes_the_value(self):
        canary = self.canary()
        job = self.submit('ep-1')
        with self.assertRaises(jobs.Validation) as caught:
            self.store.emit(job.id, 'job.progress', 'E_VALIDATION_FIELD', {'password': canary})
        self.assertNotIn(canary, str(caught.exception))
        self.assertIn('data.password', str(caught.exception))


if __name__ == '__main__':
    import unittest
    unittest.main()
