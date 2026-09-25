"""What the scheduler decides: intervals, windows, pause, quiet hours, no catch-up (F15)."""
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import scheduler as sch

UTC = timezone.utc
BERLIN = ZoneInfo('Europe/Berlin')
T0 = 1_780_000_000.0        # 2026-05-29 09:46:40 UTC, an ordinary moment
MINUTE = 60
HOUR = 3600


def at(*args):
    """Unix seconds of a UTC wall clock, so the tests read like a calendar."""
    return datetime(*args, tzinfo=UTC).timestamp()


def local(tz, *args):
    return datetime(*args, tzinfo=tz).astimezone(UTC).timestamp()


class FakeClock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        return self.now


class SchedulerCase(unittest.TestCase):
    def build(self, **kwargs):
        kwargs.setdefault('power_reader', sch.PowerSignal.unknown)
        self.clock = FakeClock()
        self.store = sch.InMemoryScheduleStore()
        self.scheduler = sch.Scheduler(store=self.store, clock=self.clock, **kwargs)
        return self.scheduler

    def add(self, **kwargs):
        spec = sch.ScheduleSpec(id=kwargs.pop('id', 'scan'), **kwargs)
        self.scheduler.add(spec)
        return spec

    def tick(self, at_time=None):
        if at_time is not None:
            self.clock.advance(at_time - self.clock.now)
        return self.scheduler.tick()

    def reasons(self, report):
        return [item.reason for item in report.decisions]


class IntervalPlanTests(SchedulerCase):
    def test_activation_is_the_origin_not_a_run(self):
        self.build()
        self.add(interval_minutes=30)
        report = self.tick()
        self.assertEqual(self.reasons(report), [sch.REASON_NOT_DUE])
        self.assertEqual(report.decisions[0].next_run_at, T0 + 30 * MINUTE)
        self.assertEqual(report.run_requests, ())

    def test_one_run_per_interval_and_no_second_run_for_the_same_instant(self):
        self.build()
        self.add(interval_minutes=30)
        self.tick()
        first = self.tick(self.clock.now + 30 * MINUTE)
        self.assertEqual(len(first.run_requests), 1)
        self.assertEqual(first.run_requests[0].scheduled_for, T0 + 30 * MINUTE)
        self.assertEqual(first.run_requests[0].reason, sch.REASON_DUE)
        self.assertFalse(first.run_requests[0].coalesced)
        self.assertEqual(first.run_requests[0].missed, 0)
        again = self.tick(self.clock.now + 10)
        self.assertEqual(self.reasons(again), [sch.REASON_NOT_DUE])
        self.assertEqual(again.run_requests, ())

    def test_run_id_is_derived_so_a_replay_is_idempotent(self):
        self.build()
        self.add(interval_minutes=30)
        self.tick()
        run = self.tick(self.clock.now + 30 * MINUTE).run_requests[0]
        self.assertEqual(run.run_id, sch.make_run_id('scan', run.scheduled_for))
        self.assertNotEqual(run.run_id, sch.make_run_id('scan', run.scheduled_for + 1))
        self.assertNotEqual(run.run_id, sch.make_run_id('other', run.scheduled_for))

    def test_a_disabled_schedule_never_runs(self):
        self.build()
        self.add(interval_minutes=30, enabled=False)
        self.tick()
        report = self.tick(self.clock.now + 90 * MINUTE)
        self.assertEqual(self.reasons(report), [sch.REASON_DISABLED])
        self.assertEqual(report.run_requests, ())

    def test_interval_outside_the_allowed_range_is_refused(self):
        self.build()
        with self.assertRaises(sch.ScheduleError) as caught:
            self.add(interval_minutes=0)
        self.assertEqual(caught.exception.field, 'interval_minutes')
        with self.assertRaises(sch.ScheduleError):
            self.add(interval_minutes=1441)
        with self.assertRaises(sch.ScheduleError):
            self.add(kind=sch.KIND_SLOTS)

    def test_unknown_fields_are_refused_instead_of_ignored(self):
        with self.assertRaises(sch.ScheduleError) as caught:
            sch.ScheduleSpec.from_dict({'id': 'x', 'interval_minutes': 5, 'typo': 1})
        self.assertEqual(caught.exception.code, 'E_VALIDATION_UNKNOWN_FIELD')
        with self.assertRaises(sch.ScheduleError):
            sch.ScheduleSpec.from_dict('not a mapping')


class WindowPlanTests(SchedulerCase):
    def test_work_runs_only_inside_the_window(self):
        self.build()
        self.add(interval_minutes=30, timezone='Europe/Berlin',
                 windows=(sch.Window.parse('09:00-18:00'),))
        self.tick(local(BERLIN, 2026, 9, 28, 9, 0))         # 09:00 local: activation, not a run
        self.assertEqual(self.scheduler.state('scan').paused, False)
        report = self.tick(local(BERLIN, 2026, 9, 28, 9, 30))
        self.assertEqual(self.reasons(report), [sch.REASON_DUE])
        evening = local(BERLIN, 2026, 9, 28, 21, 0)
        report = self.tick(evening)
        self.assertEqual(self.reasons(report), [sch.REASON_WINDOW_CLOSED])
        self.assertEqual(report.decisions[0].resume_at, local(BERLIN, 2026, 9, 29, 9, 0))
        self.assertEqual(report.run_requests, ())

    def test_the_next_run_is_reported_while_the_window_is_closed(self):
        self.build()
        self.add(interval_minutes=60, timezone='Europe/Berlin',
                 windows=(sch.Window.parse('09:00-18:00', weekdays=['mon', 'tue', 'wed', 'thu', 'fri']),))
        self.tick(local(BERLIN, 2026, 9, 28, 12, 0))
        report = self.tick(local(BERLIN, 2026, 9, 28, 12, 30))
        self.assertEqual(report.decisions[0].next_run_at, local(BERLIN, 2026, 9, 28, 13, 0))
        saturday = local(BERLIN, 2026, 10, 3, 12, 0)
        report = self.tick(saturday)
        self.assertEqual(self.reasons(report), [sch.REASON_WINDOW_CLOSED])
        self.assertEqual(report.decisions[0].resume_at, local(BERLIN, 2026, 10, 5, 9, 0))

    def test_slots_run_once_a_day_in_local_time(self):
        self.build()
        self.add(kind=sch.KIND_SLOTS, slots=(sch.Slot.parse('09:00'), sch.Slot.parse('21:30')),
                 timezone='Europe/Berlin')
        self.tick(local(BERLIN, 2026, 9, 28, 12, 0))
        seen = []
        moment = local(BERLIN, 2026, 9, 28, 12, 10)
        while moment < local(BERLIN, 2026, 9, 30, 12, 0):
            seen += [item.scheduled_for for item in self.tick(moment).run_requests]
            moment += 10 * MINUTE
        self.assertEqual(seen, [local(BERLIN, 2026, 9, 28, 21, 30),
                                 local(BERLIN, 2026, 9, 29, 9, 0),
                                 local(BERLIN, 2026, 9, 29, 21, 30),
                                 local(BERLIN, 2026, 9, 30, 9, 0)],
                         'each slot fires once, at the local wall-clock time, every day')

    def test_dst_night_does_not_duplicate_a_slot(self):
        self.build()
        self.add(kind=sch.KIND_SLOTS, slots=(sch.Slot.parse('02:30'),), timezone='Europe/Berlin')
        self.tick(local(BERLIN, 2026, 10, 24, 12, 0))
        seen = []
        moment = local(BERLIN, 2026, 10, 25, 0, 0)
        while moment < local(BERLIN, 2026, 10, 26, 1, 0):
            seen += [item.scheduled_for for item in self.tick(moment).run_requests]
            moment += 10 * MINUTE
        self.assertEqual(seen, [local(BERLIN, 2026, 10, 25, 2, 30)],
                         'the repeated wall clock must not produce a second run')

    def test_dst_gap_is_skipped_by_default_and_shifted_on_request(self):
        for policy, expected in ((sch.DST_SKIP, []), (sch.DST_SHIFT, [local(BERLIN, 2026, 3, 29, 3, 0)])):
            with self.subTest(policy=policy):
                self.build()
                self.add(kind=sch.KIND_SLOTS, slots=(sch.Slot.parse('02:30'),), dst_policy=policy,
                         timezone='Europe/Berlin')
                self.tick(local(BERLIN, 2026, 3, 28, 12, 0))
                seen = []
                moment = local(BERLIN, 2026, 3, 29, 0, 0)
                while moment < local(BERLIN, 2026, 3, 29, 12, 0):
                    seen += [item.scheduled_for for item in self.tick(moment).run_requests]
                    moment += 10 * MINUTE
                self.assertEqual(seen, expected)

    def test_daily_window_keeps_its_local_hours_across_a_dst_change(self):
        self.build()
        self.add(interval_minutes=30, timezone='Europe/Berlin', windows=(sch.Window.parse('09:00-18:00'),))
        for day in (datetime(2026, 3, 28).date(), datetime(2026, 3, 29).date(),
                    datetime(2026, 10, 24).date(), datetime(2026, 10, 25).date()):
            with self.subTest(day=str(day)):
                start = local(BERLIN, day.year, day.month, day.day, 9, 30)
                self.tick(start)
                report = self.tick(start + 30 * MINUTE)
                self.assertEqual(len(report.run_requests), 1)
                scheduled = datetime.fromtimestamp(report.run_requests[0].scheduled_for, BERLIN)
                self.assertEqual((scheduled.hour, scheduled.minute), (10, 0),
                                 'the run stays an hour after the 09:30 local start, DST or not')


class QuietHoursTests(SchedulerCase):
    def test_quiet_hours_stop_the_schedule_and_name_the_moment_it_ends(self):
        self.build()
        self.add(interval_minutes=30, timezone='Europe/Berlin',
                 quiet_hours=(sch.Window.parse('22:00-08:00'),))
        self.tick(local(BERLIN, 2026, 9, 28, 12, 0))
        report = self.tick(local(BERLIN, 2026, 9, 28, 23, 0))
        self.assertEqual(self.reasons(report), [sch.REASON_PAUSED_QUIET])
        self.assertEqual(report.decisions[0].resume_at, local(BERLIN, 2026, 9, 29, 8, 0))
        self.assertTrue(self.scheduler.state('scan').paused)
        self.assertEqual(self.scheduler.state('scan').pause_reason, sch.REASON_PAUSED_QUIET)

    def test_a_loop_ticking_through_the_night_resumes_once_without_a_storm(self):
        self.build()
        self.add(interval_minutes=30, timezone='Europe/Berlin',
                 quiet_hours=(sch.Window.parse('22:00-08:00'),))
        self.tick(local(BERLIN, 2026, 9, 28, 12, 0))
        runs = []
        moment = local(BERLIN, 2026, 9, 28, 12, 30)
        end = local(BERLIN, 2026, 9, 29, 9, 0)
        while moment < end:
            runs += [(item.scheduled_for, item.reason) for item in self.tick(moment).run_requests]
            moment += 30 * MINUTE
        end_of_quiet = local(BERLIN, 2026, 9, 29, 8, 0)
        after_quiet = [item for item in runs if item[0] >= end_of_quiet]
        self.assertEqual(after_quiet[0], (end_of_quiet, sch.REASON_COALESCED),
                         'quiet hours end with exactly one run that carries the merged count')
        self.assertEqual(after_quiet[1][0], end_of_quiet + 30 * MINUTE,
                         'and the calendar picks up again on the usual interval')
        self.assertFalse(self.scheduler.state('scan').paused)

    def test_resuming_after_a_long_pause_merges_the_missed_occurrences(self):
        self.build()
        self.add(interval_minutes=30)
        self.tick()
        self.scheduler.pause('scan')
        report = self.tick(self.clock.now + 5 * HOUR)
        self.assertEqual(self.reasons(report), [sch.REASON_PAUSED_USER])
        self.assertEqual(report.run_requests, ())
        self.assertEqual(self.scheduler.state('scan').runs, 0)
        self.scheduler.resume('scan')
        resumed = self.tick(self.clock.now + 10)
        self.assertEqual(self.reasons(resumed), [sch.REASON_COALESCED],
                         'a resume never replays the hours that passed while paused')
        self.assertEqual(resumed.run_requests[0].missed, 9)
        self.assertEqual(len(resumed.run_requests), 1)
        following = self.tick(self.clock.now + 30 * MINUTE)
        self.assertEqual(self.reasons(following), [sch.REASON_DUE], 'and the interval carries on')


class NoCatchUpTests(SchedulerCase):
    def test_sleep_merges_everything_that_is_overdue_into_one_run(self):
        self.build()
        self.add(interval_minutes=30)
        self.tick()
        self.tick(self.clock.now + 30 * MINUTE)
        slept = self.tick(self.clock.now + 8 * HOUR)
        self.assertTrue(slept.woke)
        self.assertEqual(self.reasons(slept), [sch.REASON_COALESCED_SLEEP])
        self.assertEqual(len(slept.run_requests), 1, 'no catch-up storm after sleep')
        self.assertEqual(slept.run_requests[0].missed, 15)
        self.assertTrue(slept.run_requests[0].coalesced)
        self.assertEqual(slept.run_requests[0].scheduled_for, self.clock.now)
        self.assertEqual(self.scheduler.state('scan').skipped, 15)

    def test_wake_can_be_declared_by_the_platform_layer(self):
        self.build()
        self.add(interval_minutes=30)
        self.tick()
        self.scheduler.mark_wake(self.clock.now)
        report = self.tick(self.clock.now + 6 * HOUR)
        self.assertTrue(report.woke)
        self.assertEqual(self.reasons(report), [sch.REASON_COALESCED_SLEEP])

    def test_a_normal_gap_is_not_mistaken_for_sleep(self):
        self.build()
        self.add(interval_minutes=30, catch_up=True, max_catch_up=5)   # the wake gap is four intervals
        self.tick()
        report = self.tick(self.clock.now + 90 * MINUTE)
        self.assertFalse(report.woke)
        self.assertEqual([item.reason for item in report.run_requests], [sch.REASON_DUE] * 3,
                         'every due occurrence is replayed, and none of them is called a sleep')

    def test_without_catch_up_the_missed_occurrences_are_merged(self):
        self.build()
        self.add(interval_minutes=30, catch_up=False)
        self.tick()
        report = self.tick(self.clock.now + 90 * MINUTE + 10)     # three intervals, under the wake gap
        self.assertFalse(report.woke)
        self.assertEqual(len(report.run_requests), 1)
        self.assertEqual(report.run_requests[0].reason, sch.REASON_COALESCED)
        self.assertTrue(report.run_requests[0].coalesced)
        self.assertEqual(report.run_requests[0].missed, 2)

    def test_catch_up_replays_at_most_max_catch_up(self):
        for limit, expected in ((3, [30, 60, 90]), (1, [90])):
            with self.subTest(limit=limit):
                self.build()
                self.add(interval_minutes=30, catch_up=True, max_catch_up=limit)
                self.tick()
                report = self.tick(self.clock.now + 90 * MINUTE + 10)
                offsets = [item.scheduled_for - T0 for item in report.run_requests]
                self.assertEqual(offsets, [item * MINUTE for item in expected])
                self.assertEqual([item.coalesced for item in report.run_requests],
                                 [False] * len(expected))

    def test_an_expired_catch_up_grace_still_runs_once_now(self):
        self.build()
        self.add(interval_minutes=30, catch_up=True, max_catch_up=5, catch_up_grace_s=600)
        self.tick()
        report = self.tick(self.clock.now + 75 * MINUTE)
        self.assertEqual(self.reasons(report), [sch.REASON_COALESCED])
        self.assertEqual(len(report.run_requests), 1)
        self.assertEqual(report.run_requests[0].scheduled_for, self.clock.now)

    def test_a_disabled_schedule_does_not_accumulate_work(self):
        self.build()
        self.add(interval_minutes=30, catch_up=True, max_catch_up=10, enabled=False)
        self.tick()
        self.tick(self.clock.now + 10 * HOUR)
        state = self.scheduler.state('scan')
        self.assertEqual(state.runs, 0)
        self.assertEqual(state.skipped, 0)


class PowerPlanTests(SchedulerCase):
    def test_a_battery_policy_pauses_and_the_reason_is_visible(self):
        signal = sch.PowerSignal(on_battery=True, battery_percent=30, battery_supported=True, source='os')
        self.build(power_reader=lambda: signal)
        self.add(interval_minutes=30, power=sch.PowerPolicy(on_battery=sch.POWER_PAUSE))
        report = self.tick()               # nothing starts while the machine is on battery
        self.assertEqual(self.reasons(report), [sch.REASON_PAUSED_BATTERY])
        self.assertEqual(report.decisions[0].get('power_reason'), sch.POWER_BATTERY)
        self.assertEqual(report.run_requests, ())
        self.assertEqual(self.scheduler.state('scan').pause_reason, sch.REASON_PAUSED_BATTERY)
        held = self.tick(self.clock.now + 30 * MINUTE)
        self.assertEqual(self.reasons(held), [sch.REASON_PAUSED_BATTERY])
        self.assertEqual(held.run_requests, ())

    def test_metered_cheap_only_narrows_the_stages_of_the_run_request(self):
        signal = sch.PowerSignal(metered=True, metered_supported=True, source='os')
        self.build(power_reader=lambda: signal)
        self.add(interval_minutes=30, power=sch.PowerPolicy(metered=sch.POWER_CHEAP_ONLY))
        self.tick()
        report = self.tick(self.clock.now + 30 * MINUTE)
        self.assertEqual(len(report.run_requests), 1)
        self.assertEqual(report.run_requests[0].stages, sch.CHEAP_CLASSES)
        self.assertEqual(report.decisions[0].get('stages'), ','.join(sch.CHEAP_CLASSES))

    def test_no_signal_means_the_schedule_keeps_its_normal_stages(self):
        self.build(power_reader=sch.PowerSignal.unknown)
        self.add(interval_minutes=30, power=sch.PowerPolicy(metered=sch.POWER_PAUSE,
                                                             on_battery=sch.POWER_PAUSE))
        self.tick()
        report = self.tick(self.clock.now + 30 * MINUTE)
        self.assertEqual(len(report.run_requests), 1)
        self.assertEqual(report.run_requests[0].stages, sch.WORKBENCH_CLASSES)


class ReportAndIdempotencyTests(SchedulerCase):
    def test_the_report_explains_state_without_guessing(self):
        self.build()
        self.add(interval_minutes=30, timezone='Europe/Berlin', pool_id='main',
                 quiet_hours=(sch.Window.parse('22:00-08:00'),),
                 budgets=sch.Budgets(requests=100, bytes=10_000, reset=sch.RESET_DAILY,
                                     timezone='Europe/Berlin'))
        self.tick(local(BERLIN, 2026, 9, 28, 12, 0))
        self.tick(local(BERLIN, 2026, 9, 28, 23, 0))
        report = self.scheduler.report(local(BERLIN, 2026, 9, 28, 23, 0))
        entry = report['schedules'][0]
        self.assertEqual(entry['id'], 'scan')
        self.assertTrue(entry['paused'])
        self.assertEqual(entry['pause_reason'], sch.REASON_PAUSED_QUIET)
        self.assertEqual(entry['resume_at'], local(BERLIN, 2026, 9, 29, 8, 0))
        self.assertTrue(entry['quiet'])
        self.assertTrue(entry['in_window'])
        self.assertEqual(entry['power']['restricted'], False)
        self.assertEqual(entry['budget']['requests']['limit'], 100)
        self.assertEqual(entry['budget']['reset'], sch.RESET_DAILY)
        self.assertIn('power', report)
        self.assertIn('suppressed', report)

    def test_a_broken_clock_is_refused_with_a_code(self):
        self.build()
        self.add(interval_minutes=30)
        for bad in (float('nan'), 'now', True, object()):
            with self.assertRaises(sch.ScheduleError) as caught:
                self.scheduler.tick(bad)
            self.assertIn(caught.exception.code, ('E_TIME_UNKNOWN', 'E_VALIDATION_FIELD'))
        self.assertEqual(self.scheduler.tick().at, self.clock.now, 'None means the scheduler clock')

    def test_runs_are_recorded_with_their_reason(self):
        self.build()
        self.add(interval_minutes=30)
        self.tick()
        self.tick(self.clock.now + 30 * MINUTE)
        history = self.store.recent_runs('scan')
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['counters']['reason'], sch.REASON_DUE)
        self.assertEqual(history[0]['id'], sch.make_run_id('scan', T0 + 30 * MINUTE))
        self.assertEqual(self.store.recent_runs('scan', limit=0), [])

    def test_a_removed_schedule_disappears(self):
        self.build()
        self.add(interval_minutes=30)
        self.assertEqual([item.id for item in self.scheduler.list()], ['scan'])
        self.assertIsNotNone(self.scheduler.get('scan'))
        self.assertTrue(self.scheduler.remove('scan'))
        self.assertEqual(self.scheduler.list(), [])
        self.assertIsNone(self.scheduler.get('scan'))
        self.assertFalse(self.scheduler.remove('scan'))

    def test_a_spec_survives_a_round_trip_through_its_own_dict(self):
        self.build()
        spec = sch.ScheduleSpec(id='scan', kind=sch.KIND_SLOTS, timezone='Europe/Berlin',
                                slots=(sch.Slot.parse('09:00', ['mon', 'fri']),),
                                windows=(sch.Window.parse('08:00-20:00'),),
                                quiet_hours=(sch.Window.parse('22:00-07:00'),),
                                budgets=sch.Budgets(requests=10, bytes=10 ** 6, concurrency=4,
                                                    reset=sch.RESET_HOURLY, timezone='Europe/Berlin'),
                                power=sch.PowerPolicy(on_battery=sch.POWER_BELOW_PERCENT,
                                                      battery_min_percent=40,
                                                      metered=sch.POWER_CHEAP_ONLY,
                                                      unknown_metered=sch.UNKNOWN_AS_UNMETERED),
                                notify=sch.NotifyPolicy(enter_below=2, exit_above=4, min_events=2),
                                notifications=sch.NotificationConfig(
                                    email=sch.Target(label='ops', enabled=True, user_confirmed=True)),
                                dst_policy=sch.DST_SHIFT, catch_up=True, max_catch_up=4, pool_id='main')
        self.assertEqual(sch.ScheduleSpec.from_dict(spec.to_dict()).to_dict(), spec.to_dict())
        payload = spec.to_dict()
        self.scheduler.add(payload)
        self.assertEqual(self.scheduler.get('scan').to_dict(), payload)


class ManualRunTests(SchedulerCase):
    def test_a_manual_run_is_possible_outside_the_calendar(self):
        self.build()
        self.add(interval_minutes=30, pool_id='main')
        self.tick()
        run = self.scheduler.run_now('scan')
        self.assertEqual(run.reason, sch.REASON_MANUAL)
        self.assertEqual(run.pool_id, 'main')
        self.assertEqual(run.scheduled_for, self.clock.now)
        self.assertEqual(run.run_id, sch.make_run_id('scan', self.clock.now))
        self.assertEqual(self.scheduler.state('scan').runs, 1)
        early = self.tick(self.clock.now + 10 * MINUTE)
        self.assertEqual([item.reason for item in early.decisions], [sch.REASON_NOT_DUE])
        later = self.tick(self.clock.now + 30 * MINUTE)
        self.assertEqual(len(later.run_requests), 1, 'the interval grid keeps running after it')
        self.assertEqual(later.run_requests[0].reason, sch.REASON_DUE)

    def test_a_manual_run_does_not_go_around_the_policy(self):
        self.build()
        self.add(interval_minutes=30, timezone='Europe/Berlin',
                 quiet_hours=(sch.Window.parse('22:00-08:00'),))
        self.tick(local(BERLIN, 2026, 9, 28, 12, 0))
        night = local(BERLIN, 2026, 9, 28, 23, 0)
        self.assertIsNone(self.scheduler.run_now('scan', night),
                          'quiet hours apply to a manual run as well')
        self.assertEqual(self.scheduler.blocked_by('scan', night).reason, sch.REASON_PAUSED_QUIET)
        self.assertIsNotNone(self.scheduler.run_now('scan', local(BERLIN, 2026, 9, 28, 12, 5)))
        self.scheduler.pause('scan')
        self.assertIsNone(self.scheduler.run_now('scan'))
        self.assertEqual(self.scheduler.blocked_by('scan').reason, sch.REASON_PAUSED_USER)
        self.assertEqual(self.scheduler.state('scan').runs, 1)

    def test_blocked_by_names_the_first_reason(self):
        self.build()
        self.add(interval_minutes=30, timezone='Europe/Berlin',
                 windows=(sch.Window.parse('09:00-18:00'),))
        self.tick(local(BERLIN, 2026, 9, 28, 21, 0))
        blocked = self.scheduler.blocked_by('scan', local(BERLIN, 2026, 9, 28, 21, 0))
        self.assertEqual(blocked.reason, sch.REASON_WINDOW_CLOSED)
        self.assertEqual(blocked.resume_at, local(BERLIN, 2026, 9, 29, 9, 0))
        self.assertIsNone(self.scheduler.blocked_by('scan', local(BERLIN, 2026, 9, 28, 12, 0)))
        with self.assertRaises(sch.ScheduleError):
            self.scheduler.blocked_by('missing')

    def test_a_disabled_schedule_never_runs_manually(self):
        self.build()
        self.add(interval_minutes=30, enabled=False)
        self.assertIsNone(self.scheduler.run_now('scan'))
        self.assertEqual(self.scheduler.blocked_by('scan').reason, sch.REASON_DISABLED)
        with self.assertRaises(sch.ScheduleError) as caught:
            self.scheduler.run_now('missing')
        self.assertEqual(caught.exception.code, 'E_VALIDATION_FIELD')


class RunRequestTests(SchedulerCase):
    def test_a_request_carries_everything_the_job_layer_needs(self):
        self.build()
        self.add(interval_minutes=30, pool_id='main',
                 budgets=sch.Budgets(requests=100, bytes=10 ** 6, reset=sch.RESET_DAILY))
        self.tick()
        run = self.tick(self.clock.now + 30 * MINUTE).run_requests[0]
        self.assertEqual(run.schedule_id, 'scan')
        self.assertEqual(run.pool_id, 'main')
        self.assertEqual(run.requested_at, T0 + 30 * MINUTE)
        self.assertEqual(run.budgets.requests, 100)
        self.assertEqual(run.stages, sch.WORKBENCH_CLASSES)
        payload = run.to_dict()
        self.assertEqual(payload['run_id'], run.run_id)
        self.assertEqual(payload['scheduled_for'], run.scheduled_for)


if __name__ == '__main__':
    unittest.main()
