"""Checkpoints, crash recovery and sleep: a stop is resumable, never lossy.

CONTRACTS.ru.md §6.2, F11 «журнал, checkpoint, crash recovery и обработка сна».
"""
from __future__ import annotations

from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_jobs_support import JobFixture  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import jobs  # noqa: E402


class CheckpointTests(JobFixture):
    def test_a_named_checkpoint_survives_a_restart(self):
        job = self.submit('ep-1', 'ep-2')
        self.store.start(job.id)
        self.work(job.id, {'ep-1': 'done'})
        self.store.save_checkpoint(job.id, 'queue', {'offset': 1, 'cursor': 'ep-2'})
        self.conn.close()

        reopened = sqlite3.connect(self.path, isolation_level=None)
        try:
            store = jobs.JobStore(reopened, clock=self.clock)
            saved = store.checkpoint(job.id, 'queue')
            self.assertEqual(saved['state'], {'offset': 1, 'cursor': 'ep-2'})
            self.assertEqual(store.job(job.id).state, 'running')
            self.assertEqual(store.unfinished(job.id), 1)
            self.assertEqual(store.item(job.id, 'ep-1').observation_id, 'obs-ep-1')
        finally:
            reopened.close()

    def test_a_checkpoint_is_written_again_rather_than_duplicated(self):
        job = self.submit('ep-1')
        self.store.save_checkpoint(job.id, 'queue', {'offset': 1})
        self.assertEqual(set(self.store.checkpoint(job.id)), {'queue'})
        self.clock.advance(5)
        self.store.save_checkpoint(job.id, 'queue', {'offset': 2})
        self.assertEqual(set(self.store.checkpoint(job.id)), {'queue'})
        self.assertEqual(self.store.checkpoint(job.id, 'queue')['state'], {'offset': 2})
        self.assertEqual(self.store.checkpoint(job.id, 'queue')['at'], self.clock.now)
        self.store.drop_checkpoint(job.id, 'queue')
        self.assertIsNone(self.store.checkpoint(job.id, 'queue'))

    def test_the_store_keeps_its_own_checkpoints(self):
        job = self.submit('ep-1')
        for name in jobs.RESERVED_CHECKPOINTS:
            with self.assertRaises(jobs.Validation):
                self.store.save_checkpoint(job.id, name, {'x': 1})
        for bad in ('', 'queue/1', '../escape', 'x' * 65):
            with self.assertRaises(jobs.Validation):
                self.store.save_checkpoint(job.id, bad, {'x': 1})

    def test_a_checkpoint_event_carries_counters_but_not_arbitrary_text(self):
        job = self.submit('ep-1')
        self.store.save_checkpoint(job.id, 'queue', {'offset': 3, 'note': 'phase 1'})
        events = self.store.events(job.id, types=('job.checkpoint',))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].data['counters'], {'offset': 3})
        self.assertEqual(events[0].data['name'], 'queue')


class CrashRecoveryTests(JobFixture):
    def test_a_dead_job_comes_back_paused_with_everything_it_had(self):
        job = self.submit('ep-1', 'ep-2', 'ep-3')
        self.store.start(job.id)
        self.work(job.id, {'ep-1': 'done', 'ep-2': 'done'})
        self.store.claim(job.id)  # ep-3 is being measured when the process dies
        self.clock.advance(600)

        recovery = self.store.recover(heartbeat_timeout_s=300)
        self.assertEqual(recovery.jobs, (job.id,))
        self.assertEqual(recovery.requeued_items, 1)
        self.assertEqual(recovery.observations_kept, 2)
        self.assertEqual(self.store.job(job.id).state, 'paused')
        states = {item.item_id: item for item in self.store.items(job.id)}
        self.assertEqual([states[key].state for key in ('ep-1', 'ep-2')], ['done', 'done'])
        self.assertEqual([states[key].observation_id for key in ('ep-1', 'ep-2')],
                         ['obs-ep-1', 'obs-ep-2'])
        self.assertEqual(states['ep-3'].state, 'pending')
        self.assertEqual(self.store.progress(job.id).state_detail, 'crashed')
        self.assertIn(jobs.CODE_INTERRUPTED, [event.code for event in self.store.events(job.id)])

    def test_recovery_is_idempotent(self):
        job = self.submit('ep-1')
        self.store.start(job.id)
        self.clock.advance(600)
        self.assertEqual(len(self.store.recover(heartbeat_timeout_s=300).jobs), 1)
        self.assertEqual(self.store.recover(heartbeat_timeout_s=300).jobs, ())

    def test_a_live_job_is_never_recovered(self):
        job = self.submit('ep-1', 'ep-2')
        self.store.start(job.id)
        self.clock.advance(400)
        self.assertEqual(self.store.recover(heartbeat_timeout_s=300).jobs, (job.id,),
                         'without a heartbeat the start time is the last sign of life')
        self.store.resume(job.id)
        self.store.start(job.id)
        self.store.heartbeat(job.id, done=0)
        self.clock.advance(200)
        self.assertEqual(self.store.recover(heartbeat_timeout_s=300).jobs, ())
        self.clock.advance(200)
        self.assertEqual(self.store.recover(heartbeat_timeout_s=300).jobs, (job.id,),
                         'a heartbeat that stopped arriving means the worker is gone')

    def test_a_measurement_without_a_verdict_is_not_lost_by_the_crash(self):
        job = self.submit('ep-1')
        self.store.start(job.id)
        self.store.claim(job.id)
        self.store.record_observation(job.id, 'ep-1', 'obs-ep-1')
        self.clock.advance(600)
        self.store.recover(heartbeat_timeout_s=300)
        item = self.store.item(job.id, 'ep-1')
        self.assertEqual(item.state, 'pending')
        self.assertEqual(item.observation_id, 'obs-ep-1',
                         'the measurement stays attached to the item it belongs to')

    def test_resume_after_recovery_continues_the_same_queue(self):
        job = self.submit('ep-1', 'ep-2', 'ep-3')
        self.store.start(job.id)
        self.work(job.id, {'ep-1': 'done'})
        self.store.claim(job.id)
        self.clock.advance(600)
        self.store.recover(heartbeat_timeout_s=300)

        self.store.resume(job.id)
        self.store.start(job.id)
        self.assertEqual(self.store.item(job.id, 'ep-1').state, 'done')
        for expected in ('ep-2', 'ep-3'):
            claimed = self.store.claim(job.id)
            self.assertEqual(claimed.item_id, expected)
            self.measure(job.id, expected)
        self.assertEqual(self.store.finish(job.id).state, 'succeeded')
        self.assertEqual(self.store.job(job.id).input_digest,
                         self.store.jobs(kind='scan')[0].input_digest,
                         'the digest of the input never moved')

    def test_recovery_leaves_terminal_jobs_alone(self):
        job = self.submit('ep-1')
        self.store.start(job.id)
        self.work(job.id, {'ep-1': 'done'})
        self.store.finish(job.id)
        self.clock.advance(10 ** 5)
        self.assertEqual(self.store.recover(heartbeat_timeout_s=300).jobs, ())
        self.assertEqual(self.store.job(job.id).state, 'succeeded')

    def test_a_worker_keeps_its_heartbeat_only_while_the_job_is_alive(self):
        job = self.submit('ep-1')
        self.store.start(job.id)
        self.store.heartbeat(job.id, done=1)
        self.store.cancel(job.id)
        with self.assertRaises(jobs.StateConflict):
            self.store.heartbeat(job.id)


class SleepTests(JobFixture):
    def test_sleeping_is_not_a_crash(self):
        job = self.submit('ep-1', 'ep-2', timeout_s=3600.0)
        self.store.start(job.id)
        self.work(job.id, {'ep-1': 'done'})
        self.store.mark_suspended(job.id)
        self.clock.advance(8 * 3600)
        self.assertEqual(self.store.recover(heartbeat_timeout_s=300).jobs, (),
                         'a sleeping machine sends no heartbeats and is not a dead worker')
        self.assertEqual(self.store.job(job.id).state, 'running')

    def test_the_time_asleep_is_given_back_to_the_budget(self):
        job = self.submit('ep-1', 'ep-2', timeout_s=600.0)
        self.store.start(job.id)
        self.clock.advance(60)
        deadline_before = self.store.progress(job.id).deadline_at

        self.store.mark_suspended(job.id)
        self.clock.advance(4 * 3600)
        awake = self.store.mark_awake(job.id)
        self.assertAlmostEqual(awake['state']['slept_s'], 4 * 3600, places=3)
        self.assertAlmostEqual(self.store.slept_s(job.id), 4 * 3600, places=3)
        self.assertAlmostEqual(self.store.progress(job.id).deadline_at, deadline_before + 4 * 3600,
                               places=3)
        self.assertEqual(self.store.enforce_deadline(job.id).state, 'running',
                         'a night of sleep must not consume the working budget')

    def test_the_budget_still_runs_out_while_the_machine_is_awake(self):
        job = self.submit('ep-1', 'ep-2', timeout_s=600.0)
        self.store.start(job.id)
        self.store.mark_suspended(job.id)
        self.clock.advance(60)
        self.store.mark_awake(job.id)
        self.clock.advance(700)
        self.assertEqual(self.store.enforce_deadline(job.id).state, 'timed_out')
        self.assertEqual(self.store.unfinished(job.id), 2)

    def test_two_sleeps_accumulate(self):
        job = self.submit('ep-1', timeout_s=3600.0)
        self.store.start(job.id)
        for hours in (2, 3):
            self.store.mark_suspended(job.id)
            self.clock.advance(hours * 3600)
            self.store.mark_awake(job.id)
        self.assertAlmostEqual(self.store.slept_s(job.id), 5 * 3600, places=3)

    def test_waking_without_suspending_is_refused(self):
        job = self.submit('ep-1')
        with self.assertRaises(jobs.Validation):
            self.store.mark_awake(job.id)

    def test_a_finished_job_is_not_suspended(self):
        job = self.submit('ep-1')
        self.store.start(job.id)
        self.store.cancel(job.id)
        with self.assertRaises(jobs.StateConflict):
            self.store.mark_suspended(job.id)


if __name__ == '__main__':
    import unittest
    unittest.main()
