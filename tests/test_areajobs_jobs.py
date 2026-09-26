"""F11 / defect 6 acceptance, run against the real migrator.

MASTER-PROMPT F11: «остановка на любой стадии не теряет завершённые
observations, повторный запрос не создаёт дубликат job, resume не расширяет
scope и не использует obsolete membership».

These are the three sentences of the acceptance, one test class each, plus the
properties around them that the acceptance implies: a bounded number of
DB-writing executors, idempotent control actions, structured progress and
events, and a sleep that is not a crash.

Everything is local — a temporary SQLite file migrated by ``db.migrate()``, an
injected clock and a fake measurement. No socket is opened, no address is
dialled, and no public proxy is involved anywhere in this file.
"""
from __future__ import annotations

import sqlite3
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import db, jobs  # noqa: E402

from tests.test_jobs_support import JobFixture  # noqa: E402  (fixtures, no network)

MEMBERS = ('ep-01', 'ep-02', 'ep-03', 'ep-04', 'ep-05', 'ep-06', 'ep-07', 'ep-08')


class StopKeepsFinishedObservationsTest(JobFixture):
    """Acceptance, sentence 1: a stop at ANY stage loses nothing already measured."""

    def _run_and_stop_at(self, stage: str) -> tuple:
        """Run a job, stop it at ``stage``, and return the observations before/after."""
        job = self.submit(*MEMBERS, kind='recheck', idempotency_key=f'req-{stage}')
        self.store.start(job.id)
        # two items fully measured, one claimed but not judged, the rest untouched
        for item_id in ('ep-01', 'ep-02'):
            self.store.claim(job.id, item_id=item_id)
            self.store.record_observation(job.id, item_id, f'obs-{item_id}')
            self.store.finish_item(job.id, item_id, 'done', observation_id=f'obs-{item_id}')
        self.store.claim(job.id, item_id='ep-03')
        before = sorted(item.item_id for item in self.store.items(job.id) if item.observation_id)

        if stage == 'before-judgement':
            # the crash case: an observation exists, the verdict does not
            self.store.record_observation(job.id, 'ep-03', 'obs-ep-03')
        elif stage == 'cancel':
            self.store.cancel(job.id, reason_code='E_STATE_USER')
        elif stage == 'pause':
            self.store.pause(job.id, reason_code='E_STATE_USER')
        elif stage == 'deadline':
            self.clock.advance(10 ** 4)
            self.store.enforce_deadline(job.id)
        elif stage == 'crash':
            self.clock.advance(10 ** 4)
            self.store.recover(heartbeat_timeout_s=60.0)
        else:  # pragma: no cover - a typo in the test itself
            raise AssertionError(f'unknown stage {stage!r}')

        after = sorted(item.item_id for item in self.store.items(job.id) if item.observation_id)
        return before, after, self.store.progress(job.id)

    def test_no_stage_of_a_stop_drops_a_finished_observation(self):
        for stage in ('before-judgement', 'cancel', 'pause', 'deadline', 'crash'):
            with self.subTest(stage=stage):
                # Close the previous fixture before replacing its temporary
                # directory; an open SQLite file cannot be removed on Windows.
                self.tearDown()
                self.setUp()
                before, after, progress = self._run_and_stop_at(stage)
                self.assertTrue(set(before) <= set(after),
                                f'{stage}: lost {set(before) - set(after)}')
                self.assertGreaterEqual(progress.finished, 2,
                                        f'{stage}: two items were measured before the stop')

    def test_a_cancel_keeps_the_queue_of_unfinished_items_readable(self):
        job = self.submit(*MEMBERS)
        self.store.start(job.id)
        for item_id in ('ep-01', 'ep-02'):
            self.store.claim(job.id, item_id=item_id)
            self.store.record_observation(job.id, item_id, f'obs-{item_id}')
            self.store.finish_item(job.id, item_id, 'done', observation_id=f'obs-{item_id}')

        self.store.cancel(job.id, reason_code='E_STATE_USER')
        progress = self.store.progress(job.id)

        self.assertEqual(self.store.job(job.id).state, 'cancelled')
        self.assertEqual(progress.finished, 2)
        self.assertEqual(progress.unfinished, 6, 'cancel does not discard the queue')
        self.assertEqual(len(self.store.items(job.id)), 8, 'no item row is ever deleted')

    def test_a_crash_between_measuring_and_judging_keeps_the_measurement(self):
        job = self.submit(*MEMBERS)
        self.store.start(job.id)
        self.store.claim(job.id, item_id='ep-01')
        self.store.record_observation(job.id, 'ep-01', 'obs-ep-01')
        self.clock.advance(10 ** 4)

        recovery = self.store.recover(heartbeat_timeout_s=60.0)

        item = self.store.item(job.id, 'ep-01')
        self.assertIn(job.id, recovery.jobs)
        self.assertEqual(item.observation_id, 'obs-ep-01',
                         'the measurement survives even though there is no verdict')
        self.assertEqual(item.state, 'pending', 'unfinished work returns to the queue')


class RepeatRequestTest(JobFixture):
    """Acceptance, sentence 2: a repeated request does not create a second job."""

    def _rows(self, table: str, where: str = '', args: tuple = ()) -> int:
        sql = f'SELECT count(*) FROM {table}' + (f' WHERE {where}' if where else '')
        return int(self.conn.execute(sql, args).fetchone()[0])

    def test_a_replay_returns_the_same_job_in_every_state_it_can_reach(self):
        job = self.submit(*MEMBERS, idempotency_key='req-1')
        states = []

        replay = self.store.submit('scan', self.scope(), self.items(*MEMBERS),
                                   idempotency_key='req-1')
        states.append(('queued', replay.id, replay.state))
        self.assertEqual(replay.id, job.id)

        self.store.start(job.id)
        replay = self.store.submit('scan', self.scope(), self.items(*MEMBERS),
                                   idempotency_key='req-1')
        states.append(('running', replay.id, replay.state))

        self.store.pause(job.id)
        replay = self.store.submit('scan', self.scope(), self.items(*MEMBERS),
                                   idempotency_key='req-1')
        states.append(('paused', replay.id, replay.state))

        self.store.cancel(job.id)
        replay = self.store.submit('scan', self.scope(), self.items(*MEMBERS),
                                   idempotency_key='req-1')
        states.append(('cancelled', replay.id, replay.state))

        for expected_state, replayed_id, actual_state in states:
            self.assertEqual(replayed_id, job.id, f'replay in {expected_state} produced a new job')
            self.assertEqual(actual_state, expected_state,
                             f'replay in {expected_state} changed the state')
        self.assertEqual(self._rows('job'), 1, 'exactly one job exists')
        self.assertEqual(self._rows('job_item', 'job_id=?', (job.id,)), 8)

    def test_a_replay_does_not_add_a_single_event(self):
        job = self.submit(*MEMBERS, idempotency_key='req-1')
        before = len(self.store.events(job.id))
        for _ in range(3):
            self.store.submit('scan', self.scope(), self.items(*MEMBERS), idempotency_key='req-1')
        self.assertEqual(len(self.store.events(job.id)), before)

    def test_the_same_key_with_another_input_is_a_conflict_not_a_second_job(self):
        self.submit(*MEMBERS, idempotency_key='req-1')
        wider = jobs.Scope('col-public', 'prof-1', 1, 'd' * 20,
                           {'protocol': 'http', 'countries': ['DE']}, {'timeout_s': 300.0})
        with self.assertRaises(jobs.IdempotencyConflict) as caught:
            self.store.submit('scan', wider, self.items(*MEMBERS), idempotency_key='req-1')
        self.assertEqual(caught.exception.code, 'E_CONFLICT_IDEMPOTENCY')
        self.assertEqual(self._rows('job'), 1)


class ResumeScopeTest(JobFixture):
    """Acceptance, sentence 3: resume widens nothing and trusts no obsolete membership."""

    def test_resume_keeps_the_queue_it_was_created_with(self):
        job = self.submit(*MEMBERS)
        self.store.start(job.id)
        for item_id in ('ep-01', 'ep-02', 'ep-03'):
            self.store.claim(job.id, item_id=item_id)
            self.store.record_observation(job.id, item_id, f'obs-{item_id}')
            self.store.finish_item(job.id, item_id, 'done', observation_id=f'obs-{item_id}')
        self.store.pause(job.id)

        # the collection grew after the job was created
        grown = list(MEMBERS) + ['ep-09', 'ep-10']
        self.store.resume(job.id, member_ids=grown)
        progress = self.store.progress(job.id)

        self.assertEqual(progress.total, 8, 'resume never adds an item the job did not have')
        self.assertEqual(sorted(item.item_id for item in self.store.items(job.id)), sorted(MEMBERS))

    def test_resume_blocks_an_address_that_left_the_collection(self):
        job = self.submit(*MEMBERS)
        self.store.start(job.id)
        self.store.claim(job.id, item_id='ep-01')
        self.store.record_observation(job.id, 'ep-01', 'obs-ep-01')
        self.store.finish_item(job.id, 'ep-01', 'done', observation_id='obs-ep-01')
        self.store.pause(job.id)

        self.store.resume(job.id, member_ids=[m for m in MEMBERS if m != 'ep-04'])
        obsolete = self.store.item(job.id, 'ep-04')
        still_queued = [item.item_id for item in self.store.items(job.id) if not item.terminal]

        self.assertEqual(obsolete.state, 'blocked')
        self.assertEqual(obsolete.error_code, jobs.CODE_OBSOLETE_MEMBERSHIP)
        self.assertNotIn('ep-04', still_queued, 'a blocked address is not measured')
        self.assertEqual(self.store.item(job.id, 'ep-01').state, 'done',
                         'blocking does not touch what was already measured')

    def test_resume_refuses_a_changed_profile_revision(self):
        job = self.submit(*MEMBERS, profile_revision=1)
        self.store.start(job.id)
        self.store.pause(job.id)

        with self.assertRaises(jobs.Conflict) as caught:
            self.store.resume(job.id, scope=self.scope(profile_revision=2))
        self.assertEqual(caught.exception.code, 'E_CONFLICT_REVISION')
        self.assertEqual(self.store.job(job.id).scope.profile_revision, 1,
                         'the recorded scope did not move')

    def test_the_scope_of_a_finished_job_still_names_the_revision_it_ran_under(self):
        job = self.submit(*MEMBERS, profile_revision=3)
        self.store.start(job.id)
        self.store.cancel(job.id)
        self.assertEqual(self.store.job(job.id).scope.profile_revision, 3)


class BoundedWritersTest(JobFixture):
    """F11: «ограничение числа одновременно изменяющих БД исполнителей» (CONTRACTS §6.4)."""

    def test_a_second_job_may_not_start_while_one_is_running(self):
        first = self.submit('ep-01')
        self.store.start(first.id)
        second = self.submit('ep-02')
        with self.assertRaises(jobs.Busy) as caught:
            self.store.start(second.id)
        self.assertEqual(caught.exception.code, 'E_CONFLICT_BUSY')
        self.assertEqual(self.store.job(first.id).state, 'running')

    def test_a_second_process_is_refused_the_same_way(self):
        first = self.submit('ep-01')
        self.store.start(first.id)
        second = self.submit('ep-02')
        self.conn.close()
        other_conn = sqlite3.connect(str(self.path), isolation_level=None)
        try:
            other = jobs.JobStore(other_conn, clock=self.clock)
            with self.assertRaises(jobs.Busy):
                other.start(second.id)
        finally:
            other_conn.close()
            self.conn = sqlite3.connect(str(self.path), isolation_level=None)
            self.store = jobs.JobStore(self.conn, clock=self.clock)

    def test_the_writer_gate_hands_the_lock_over_and_frees_it_after_an_error(self):
        gate = jobs.WriterGate()
        with gate.hold('scanner-1'):
            self.assertTrue(gate.busy)
            self.assertEqual(gate.owner, 'scanner-1')
            with self.assertRaises(jobs.Busy):
                with gate.hold('scanner-2'):
                    pass
        self.assertFalse(gate.busy)
        with gate.hold('scanner-3'):
            self.assertEqual(gate.owner, 'scanner-3')
        with self.assertRaises(RuntimeError):
            with gate.hold('scanner-4'):
                raise RuntimeError('the worker died')
        self.assertFalse(gate.busy, 'an error does not leave the gate held')

    def test_the_gate_serialises_two_threads(self):
        gate = jobs.WriterGate()
        order: list[str] = []
        started = threading.Event()

        def first():
            with gate.hold('a'):
                order.append('a-in')
                started.set()
                threading.Event().wait(0.05)
                order.append('a-out')

        def second():
            started.wait(1.0)
            with self.assertRaises(jobs.Busy):
                with gate.hold('b'):
                    order.append('b-in')

        threads = [threading.Thread(target=first), threading.Thread(target=second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
        self.assertEqual(order, ['a-in', 'a-out'], 'the second writer never got in')


class ProgressAndEventsTest(JobFixture):
    """F11: «structured progress/events» and defect 25 (one event per measurement)."""

    def test_progress_counts_items_from_one_grouped_read(self):
        job = self.submit(*MEMBERS)
        self.store.start(job.id)
        for item_id in ('ep-01', 'ep-02', 'ep-03'):
            self.store.claim(job.id, item_id=item_id)
            self.store.record_observation(job.id, item_id, f'obs-{item_id}')
            self.store.finish_item(job.id, item_id, 'done', observation_id=f'obs-{item_id}')
        self.store.claim(job.id, item_id='ep-04')

        progress = self.store.progress(job.id)
        self.assertEqual(progress.total, 8)
        self.assertEqual(progress.finished, 3)
        self.assertEqual(progress.unfinished, 5)
        self.assertEqual(progress.by_state['done'], 3)
        self.assertEqual(progress.by_state['probing'], 1)
        self.assertEqual(progress.by_state['pending'], 4)

    def test_sequence_numbers_are_gapless_and_a_cursor_returns_only_the_tail(self):
        job = self.submit('ep-01', 'ep-02', 'ep-03')
        self.store.start(job.id)
        for item_id in ('ep-01', 'ep-02', 'ep-03'):
            self.store.claim(job.id, item_id=item_id)
            self.store.record_observation(job.id, item_id, f'obs-{item_id}')
            self.store.finish_item(job.id, item_id, 'done', observation_id=f'obs-{item_id}')

        events = self.store.events(job.id)
        self.assertEqual([event.seq for event in events], list(range(1, len(events) + 1)))
        tail = self.store.events(job.id, after_seq=events[-1].seq - 2)
        self.assertEqual([event.seq for event in tail], [events[-1].seq - 1, events[-1].seq])

    def test_a_measurement_is_exactly_one_event_naming_its_item(self):
        job = self.submit('ep-01', 'ep-02')
        self.store.start(job.id)
        for item_id in ('ep-01', 'ep-02'):
            self.store.claim(job.id, item_id=item_id)
            self.store.record_observation(job.id, item_id, f'obs-{item_id}')
            self.store.finish_item(job.id, item_id, 'done', observation_id=f'obs-{item_id}')

        verdicts = [event for event in self.store.events(job.id) if event.type == 'item.verdict']
        self.assertEqual(len(verdicts), 2, 'one verdict per measured item, not a log line')
        self.assertEqual([event.item_id for event in verdicts], ['ep-01', 'ep-02'])
        for event in verdicts:
            self.assertEqual(json_shape(event)['observation_id'], f'obs-{event.item_id}')

    def test_a_control_action_is_idempotent_by_state_and_by_key(self):
        job = self.submit('ep-01', 'ep-02')
        self.store.start(job.id)
        first = self.store.pause(job.id, idempotency_key='P1')
        replay = self.store.pause(job.id, idempotency_key='P1')
        self.assertEqual(first.state, replay.state, 'a replayed pause does not re-pause')
        self.store.resume(job.id)
        after_resume = self.store.pause(job.id, idempotency_key='P1')
        self.assertEqual(after_resume.state, 'queued',
                         'a pause replayed after a resume must not stop the job again')


class SleepHandlingTest(JobFixture):
    """F11: sleep handling — closing the lid is not a crash (scenario 19)."""

    def test_a_job_that_is_still_suspended_is_never_taken_for_a_crash(self):
        job = self.submit('ep-01', 'ep-02', scope=self.scope(timeout_s=3600.0))
        self.store.start(job.id)
        self.store.claim(job.id, item_id='ep-01')
        self.store.mark_suspended(job.id)
        self.clock.advance(8 * 3600)  # eight hours with the lid shut, no heartbeats

        # the machine is asleep: recovery must not touch the job at all
        recovery = self.store.recover(heartbeat_timeout_s=300.0)
        self.assertNotIn(job.id, recovery.jobs,
                         'a sleeping machine sends no heartbeats and is not a dead worker')
        self.assertEqual(self.store.job(job.id).state, 'running')

    def test_after_waking_the_time_asleep_is_given_back_to_the_budget(self):
        job = self.submit('ep-01', 'ep-02', scope=self.scope(timeout_s=3600.0))
        self.store.start(job.id)
        self.store.claim(job.id, item_id='ep-01')
        self.store.mark_suspended(job.id)
        self.clock.advance(8 * 3600)
        self.store.mark_awake(job.id)
        self.store.heartbeat(job.id)          # the worker is back and says so

        self.assertEqual(self.store.slept_s(job.id), 8 * 3600)
        progress = self.store.progress(job.id)
        self.assertEqual(self.store.job(job.id).state, 'running')
        self.assertGreater(progress.remaining_s, 3500,
                           'eight hours asleep do not come out of a one-hour budget')
        self.assertEqual(self.store.enforce_deadline(job.id).state, 'running')

    def test_time_actually_spent_awake_still_runs_the_budget_out(self):
        job = self.submit('ep-01', scope=self.scope(timeout_s=600.0))
        self.store.start(job.id)
        self.clock.advance(601)
        job_after = self.store.enforce_deadline(job.id)
        self.assertEqual(job_after.state, 'timed_out')


class SchemaAndVocabularyTest(unittest.TestCase):
    """The store reaches its schema through the real migrator, and names stay shared."""

    def test_the_migrator_produces_the_columns_the_store_writes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'workbench.sqlite3'
            db.migrate(path)
            conn = sqlite3.connect(str(path), isolation_level=None)
            try:
                store = jobs.JobStore(conn)
                job = store.submit('scan', jobs.Scope('col', 'prof', 1), [jobs.QueueItem('ep-01')])
                store.start(job.id)
                self.assertEqual(store.claim(job.id).state, 'probing')
            finally:
                conn.close()

    def test_the_state_and_code_vocabularies_are_the_contract_ones(self):
        self.assertEqual(jobs.CODE_OBSOLETE_MEMBERSHIP, 'OBSOLETE_MEMBERSHIP')
        self.assertEqual(jobs.JobNotFound.code, 'E_STATE_JOB_NOT_FOUND')
        self.assertEqual(jobs.ItemNotFound.code, 'E_STATE_ITEM_NOT_FOUND')
        self.assertEqual(jobs.StateConflict.code, 'E_STATE_JOB_TRANSITION')
        self.assertEqual(jobs.CODE_INTERRUPTED, 'E_STATE_JOB_INTERRUPTED')


def json_shape(event) -> dict:
    return dict(event.to_json()['data'])


if __name__ == '__main__':
    unittest.main()
