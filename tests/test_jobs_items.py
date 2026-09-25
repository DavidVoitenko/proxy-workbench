"""Queue, item states, and what pause, cancel and retry must never destroy.

CONTRACTS.ru.md §6.3, F11 acceptance, defect 6.
"""
from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_jobs_support import JobFixture  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import jobs  # noqa: E402


class QueueTests(JobFixture):
    def test_the_queue_is_fixed_once_the_job_left_the_created_state(self):
        job = self.submit('ep-1')
        self.store.queue(job.id)
        with self.assertRaises(jobs.StateConflict) as caught:
            self.store.enqueue(job.id, self.items('ep-2'))
        self.assertEqual(caught.exception.code, 'E_STATE_JOB_TRANSITION')
        self.assertEqual([item.item_id for item in self.store.items(job.id)], ['ep-1'])

    def test_items_may_be_added_before_the_job_is_queued(self):
        created = self.store.create_job('scan', self.scope(), self.items('ep-1'))
        self.assertEqual(self.store.enqueue(created.id, self.items('ep-2', 'ep-3')), 2)
        self.assertEqual(self.store.unfinished(created.id), 3)

    def test_claiming_takes_each_item_once_and_in_order(self):
        job = self.submit('ep-1', 'ep-2')
        self.store.start(job.id)
        self.assertEqual(self.store.claim(job.id).item_id, 'ep-1')
        self.assertEqual(self.store.claim(job.id).item_id, 'ep-2')
        self.assertIsNone(self.store.claim(job.id))
        with self.assertRaises(jobs.StateConflict):
            self.store.claim(job.id, item_id='ep-1')

    def test_claiming_is_only_possible_on_a_running_job(self):
        job = self.submit('ep-1')
        with self.assertRaises(jobs.StateConflict):
            self.store.claim(job.id)

    def test_a_duplicate_item_id_is_refused_rather_than_checked_twice(self):
        with self.assertRaises(jobs.Validation):
            self.submit(items=[jobs.QueueItem('ep-1'), jobs.QueueItem('ep-1')])
        created = self.store.create_job('scan', self.scope(), self.items('ep-1'))
        with self.assertRaises(jobs.Validation):
            self.store.enqueue(created.id, self.items('ep-1'))

    def test_the_queue_bound_is_reported_as_a_limit(self):
        store = jobs.JobStore(self.conn, clock=self.clock, max_queue_items=2)
        with self.assertRaises(jobs.QueueLimit) as caught:
            store.submit('scan', self.scope(), self.items('ep-1', 'ep-2', 'ep-3'))
        self.assertEqual(caught.exception.code, 'E_LIMIT_QUEUE')
        self.assertEqual(len(store.jobs()), 0, 'a refused job must not leave a row behind')

    def test_unknown_job_and_item_are_named_by_code(self):
        with self.assertRaises(jobs.JobNotFound) as caught:
            self.store.job('job-nope')
        self.assertEqual(caught.exception.code, 'E_STATE_JOB_NOT_FOUND')
        job = self.submit('ep-1')
        with self.assertRaises(jobs.ItemNotFound) as caught:
            self.store.item(job.id, 'ep-9')
        self.assertEqual(caught.exception.code, 'E_STATE_ITEM_NOT_FOUND')


class ItemStateTests(JobFixture):
    def test_the_item_state_machine_follows_the_contract(self):
        job = self.submit('ep-1', 'ep-2', 'ep-3', 'ep-4')
        self.store.start(job.id)
        self.store.mark_prefiltered(job.id, 'ep-1')
        self.assertEqual(self.store.item(job.id, 'ep-1').state, 'prefiltered')
        self.assertEqual(self.store.claim(job.id, item_id='ep-1').state, 'probing')
        with self.assertRaises(jobs.StateConflict):
            self.store.mark_prefiltered(job.id, 'ep-1')
        with self.assertRaises(jobs.StateConflict):
            self.store.mark_prefiltered(job.id, 'ep-1')

        self.store.claim(job.id, item_id='ep-2')
        self.store.finish_item(job.id, 'ep-2', 'unreachable', error_code='UNREACHABLE')
        self.store.claim(job.id, item_id='ep-3')
        self.store.finish_item(job.id, 'ep-3', 'blocked', error_code='E_DENYLISTED')
        self.assertEqual(self.store.item(job.id, 'ep-3').error_code, 'E_DENYLISTED')

    def test_done_requires_the_observation_it_claims(self):
        job = self.submit('ep-1')
        self.store.start(job.id)
        self.store.claim(job.id)
        with self.assertRaises(jobs.Validation) as caught:
            self.store.finish_item(job.id, 'ep-1', 'done')
        self.assertEqual(caught.exception.code, 'E_VALIDATION_FIELD')
        self.assertEqual(self.store.item(job.id, 'ep-1').state, 'probing',
                         'a refused verdict must leave the item where it was')

    def test_a_refused_verdict_leaves_the_measurement_attached(self):
        job = self.submit('ep-1')
        self.store.start(job.id)
        self.store.claim(job.id)
        self.store.record_observation(job.id, 'ep-1', 'obs-ep-1')
        with self.assertRaises(jobs.StateConflict):
            self.store.finish_item(job.id, 'ep-1', 'probing')
        item = self.store.item(job.id, 'ep-1')
        self.assertEqual(item.state, 'probing')
        self.assertEqual(item.observation_id, 'obs-ep-1')

    def test_a_terminal_item_is_not_rewritten(self):
        job = self.submit('ep-1')
        self.store.start(job.id)
        self.store.claim(job.id)
        self.measure(job.id, 'ep-1')
        with self.assertRaises(jobs.StateConflict):
            self.measure(job.id, 'ep-1')
        with self.assertRaises(jobs.StateConflict):
            self.store.record_observation(job.id, 'ep-1', 'obs-late')
        self.assertEqual(self.store.item(job.id, 'ep-1').observation_id, 'obs-ep-1')

    def test_unfinished_counts_the_queue_the_worker_still_owns(self):
        job = self.submit('ep-1', 'ep-2', 'ep-3')
        self.store.start(job.id)
        self.work(job.id, {'ep-1': 'done', 'ep-2': 'failed'})
        self.store.claim(job.id)
        self.assertEqual(self.store.unfinished(job.id), 1)
        self.assertEqual(len(self.store.items(job.id, state='pending')), 0)
        self.assertEqual(len(self.store.items(job.id, state='probing')), 1)
        self.assertEqual(len(self.store.items(job.id, state='failed')), 1)


class DefectSixTests(JobFixture):
    """Recheck, cancel and crash keep history, the last result and the queue."""

    def test_pause_keeps_finished_observations_and_the_unfinished_queue(self):
        job = self.submit('ep-1', 'ep-2', 'ep-3')
        self.store.start(job.id)
        self.work(job.id, {'ep-1': 'done', 'ep-2': 'done'})
        self.store.claim(job.id)
        paused = self.store.pause(job.id, reason_code='E_CONFLICT_BUSY')
        self.assertEqual(paused.state, 'paused')
        states = {item.item_id: item for item in self.store.items(job.id)}
        self.assertEqual([states['ep-1'].state, states['ep-2'].state], ['done', 'done'])
        self.assertEqual([states['ep-1'].observation_id, states['ep-2'].observation_id],
                         ['obs-ep-1', 'obs-ep-2'])
        self.assertEqual(states['ep-3'].state, 'pending', 'an interrupted item returns to the queue')
        self.assertEqual(self.store.unfinished(job.id), 1)

    def test_cancel_touches_neither_items_nor_observations(self):
        job = self.submit('ep-1', 'ep-2')
        self.store.start(job.id)
        self.work(job.id, {'ep-1': 'done'})
        self.store.claim(job.id)
        before = {item.item_id: item for item in self.store.items(job.id)}
        self.store.cancel(job.id, reason_code='E_STATE_POOL_EMPTY')
        after = {item.item_id: item for item in self.store.items(job.id)}
        self.assertEqual(before, after, 'cancel must not rewrite the queue of a job')

    def test_a_recheck_is_a_new_job_and_leaves_the_previous_run_intact(self):
        first = self.submit('ep-1', 'ep-2', 'ep-3')
        self.store.start(first.id)
        self.work(first.id, {'ep-1': 'done', 'ep-2': 'done'})
        self.store.cancel(first.id)

        second = self.store.retry(first.id)
        self.assertNotEqual(second.id, first.id)
        self.assertEqual(second.kind, 'recheck')
        self.assertEqual(second.collection_id, first.collection_id)
        self.assertEqual(second.profile_revision, first.profile_revision)
        self.assertEqual([item.item_id for item in self.store.items(second.id)], ['ep-3'],
                         'a repeated check is a new item, not a rewritten one')
        self.assertEqual(self.store.job(first.id).state, 'cancelled')
        self.assertEqual([item.state for item in self.store.items(first.id)],
                         ['done', 'done', 'pending'])
        self.assertEqual(self.store.item(first.id, 'ep-1').observation_id, 'obs-ep-1')

    def test_retry_can_target_the_failed_items_of_a_partial_run(self):
        job = self.submit('ep-1', 'ep-2', 'ep-3')
        self.store.start(job.id)
        self.work(job.id, {'ep-1': 'done', 'ep-2': 'unreachable'})
        self.assertEqual(self.store.finish(job.id).state, 'partial')
        second = self.store.retry(job.id, items=['ep-2'])
        self.assertEqual([item.item_id for item in self.store.items(second.id)], ['ep-2'])
        with self.assertRaises(jobs.StateConflict):
            self.store.retry(job.id, items=['ep-1'])

    def test_retry_is_idempotent_by_key(self):
        job = self.submit('ep-1', 'ep-2')
        self.store.start(job.id)
        self.work(job.id, {'ep-1': 'done'})
        self.store.cancel(job.id)
        first = self.store.retry(job.id, idempotency_key='retry-1')
        second = self.store.retry(job.id, idempotency_key='retry-1')
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(self.store.jobs()), 2)

    def test_a_changed_profile_is_a_new_job_not_a_silent_retry(self):
        job = self.submit('ep-1', 'ep-2')
        self.store.start(job.id)
        self.store.cancel(job.id)
        with self.assertRaises(jobs.Conflict) as caught:
            self.store.retry(job.id, scope=self.scope(profile_revision=2))
        self.assertEqual(caught.exception.code, 'E_CONFLICT_REVISION')
        self.assertEqual(len(self.store.jobs()), 1)

    def test_retry_of_a_finished_run_has_nothing_to_do(self):
        job = self.submit('ep-1')
        self.store.start(job.id)
        self.work(job.id, {'ep-1': 'done'})
        self.store.finish(job.id)
        with self.assertRaises(jobs.StateConflict):
            self.store.retry(job.id)


class ResumeScopeTests(JobFixture):
    def test_resume_never_widens_the_scope_of_a_job(self):
        job = self.submit('ep-1', 'ep-2')
        self.store.start(job.id)
        self.store.pause(job.id)
        scope_before = self.store.job(job.id).scope.canonical()
        resumed = self.store.resume(job.id)
        self.assertEqual(resumed.state, 'queued')
        self.assertEqual(resumed.scope.canonical(), scope_before)
        self.assertEqual(resumed.input_digest, self.store.job(job.id).input_digest)
        self.assertEqual([item.item_id for item in self.store.items(job.id)], ['ep-1', 'ep-2'])

    def test_resume_with_another_scope_is_refused(self):
        job = self.submit('ep-1')
        self.store.start(job.id)
        self.store.pause(job.id)
        with self.assertRaises(jobs.Conflict) as caught:
            self.store.resume(job.id, scope=self.scope(profile_revision=2, timeout_s=999.0))
        self.assertEqual(caught.exception.code, 'E_CONFLICT_REVISION')
        self.assertEqual(self.store.job(job.id).state, 'paused')

    def test_resume_does_not_measure_membership_that_has_gone(self):
        job = self.submit('ep-1', 'ep-2', 'ep-3')
        self.store.start(job.id)
        self.store.pause(job.id)
        self.store.resume(job.id, member_ids=['ep-1', 'ep-3'], membership_epoch=5)
        states = {item.item_id: item for item in self.store.items(job.id)}
        self.assertEqual(states['ep-2'].state, 'blocked')
        self.assertEqual(states['ep-2'].error_code, jobs.CODE_OBSOLETE_MEMBERSHIP)
        self.assertEqual([states['ep-1'].state, states['ep-3'].state], ['pending', 'pending'])
        codes = [event.code for event in self.store.events(job.id)]
        self.assertIn(jobs.CODE_OBSOLETE_MEMBERSHIP, codes)

    def test_a_blocked_item_is_never_claimed_and_still_counts_as_finished(self):
        job = self.submit('ep-1', 'ep-2')
        self.store.start(job.id)
        self.store.pause(job.id)
        self.store.resume(job.id, member_ids=['ep-1'])
        self.store.start(job.id)
        claimed = self.store.claim(job.id)
        self.assertEqual(claimed.item_id, 'ep-1')
        self.measure(job.id, 'ep-1')
        self.assertIsNone(self.store.claim(job.id))
        self.assertEqual(self.store.finish(job.id).state, 'succeeded')
        self.assertEqual(self.store.progress(job.id).by_state['blocked'], 1)

    def test_membership_of_a_finished_job_is_history(self):
        job = self.submit('ep-1')
        self.store.start(job.id)
        self.store.cancel(job.id)
        with self.assertRaises(jobs.StateConflict):
            self.store.sync_membership(job.id, [])

    def test_sync_membership_never_adds_items(self):
        job = self.submit('ep-1')
        self.store.start(job.id)
        report = self.store.sync_membership(job.id, ['ep-1', 'ep-2', 'ep-3'], membership_epoch=9)
        self.assertEqual(report, {'blocked': 0, 'items': []})
        self.assertEqual([item.item_id for item in self.store.items(job.id)], ['ep-1'])
        report = self.store.sync_membership(job.id, [], membership_epoch=10)
        self.assertEqual(report, {'blocked': 1, 'items': ['ep-1']})
        self.assertEqual(self.store.item(job.id, 'ep-1').state, 'blocked')
        self.store.sync_membership(job.id, ['ep-1'], membership_epoch=10)
        self.assertEqual(self.store.item(job.id, 'ep-1').state, 'blocked',
                         'membership that returns does not resurrect a blocked item')


if __name__ == '__main__':
    import unittest
    unittest.main()
