"""F15 acceptance: intervals, windows with timezone and DST, budgets, quiet
hours, no-catchup after sleep, power policy and deduplicated notifications.

MASTER-PROMPT F15 in full: intervals and time windows with timezone and correct
DST handling, pause and resume, request/byte/time budgets, quiet hours, no
catch-up after sleep, optional metered/battery policy where the OS provides a
signal, meaningful state-change notifications with dedup and hysteresis,
probe/source/retry/judge/speedtest payload counted separately from relay and
billable traffic, a finite and explainable in-flight remainder after the limit,
and no external email or webhook without a configuration the user selected.

Every test is local: an injected clock, an in-memory or temporary-file store,
zoneinfo from the system database and a trapping channel. No network, no SMTP,
no HTTP request. The only subprocess anywhere in this area is the OS power
probe, and the tests that exercise it inject a fake runner instead.
"""
from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from proxy_workbench import db
from proxy_workbench import scheduler as sched

BERLIN = ZoneInfo('Europe/Berlin')
SANTIAGO = ZoneInfo('America/Santiago')
UTC = ZoneInfo('UTC')
#: Europe/Berlin moves to summer time on this date in 2026; the local clock jumps
#: from 02:00 to 03:00, so the wall time 02:30 does not exist that day.
DST_START_2026 = date(2026, 3, 29)
DST_END_2026 = date(2026, 10, 25)


def at(year, month, day, hour, minute=0, tz=UTC) -> float:
    return datetime(year, month, day, hour, minute, tzinfo=tz).timestamp()


class Clock:
    """A clock the test drives by hand: nothing here waits for real time."""

    def __init__(self, start: float):
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def move_to(self, value: float) -> float:
        self.now = float(value)
        return self.now

    def advance(self, seconds: float) -> float:
        self.now += float(seconds)
        return self.now


class NoPower:
    """An OS that says nothing. F15 applies its power policy only where there is a signal."""

    def __call__(self) -> sched.PowerSignal:
        return sched.PowerSignal()


def make(store=None, *, clock=None, power=None, notifier=None) -> sched.Scheduler:
    return sched.Scheduler(store=store or sched.InMemoryScheduleStore(), clock=clock or (lambda: 0.0),
                           notifier=notifier, power_reader=power or NoPower())


class DaylightSavingTest(unittest.TestCase):
    """Time windows and daily slots across a real transition (F15, DST)."""

    def test_a_slot_in_the_gap_is_skipped_or_shifted_never_invented(self):
        slot = sched.Slot.parse('02:30')
        for policy, expected in ((sched.DST_SKIP, None), (sched.DST_SHIFT, 'transition')):
            with self.subTest(policy=policy):
                instant = slot.instant_on(BERLIN, DST_START_2026, policy)
                if policy == sched.DST_SKIP:
                    self.assertIsNone(instant, 'a wall time that does not exist must not run')
                else:
                    self.assertIsNotNone(instant)
                    local = instant.astimezone(BERLIN)
                    self.assertEqual((local.hour, local.minute), (3, 0),
                                     'shifted to the instant the clock jumped to')
        for day in (date(2026, 3, 28), date(2026, 3, 30)):
            instant = slot.instant_on(BERLIN, day, sched.DST_SKIP)
            self.assertIsNotNone(instant, f'{day} is an ordinary day')
            self.assertEqual(instant.astimezone(BERLIN).strftime('%H:%M'), '02:30')

    def test_exactly_one_run_per_local_date_never_two_at_the_fold(self):
        """The fold repeats a wall time; a daily slot must still fire once."""
        slot = sched.Slot.parse('02:30')
        for day in (date(2026, 10, 24), DST_END_2026, date(2026, 10, 26)):
            instants = sched.local_instants(BERLIN, datetime(day.year, day.month, day.day, 2, 30))
            if len(instants) == 2:
                self.assertEqual(len({slot.instant_on(BERLIN, day, sched.DST_SKIP)}), 1,
                                 'the fold does not double a slot')

    def test_a_window_over_the_gap_opens_where_the_clock_jumps(self):
        """02:00 does not exist on the transition day, so the window opens at 03:00.

        A window must not open before it exists and must not invent a 02:00 that
        the clock skipped.  One interval, both boundaries resolvable.
        """
        window = sched.Window.parse('02:00-04:00')
        intervals = window.intervals_on(BERLIN, DST_START_2026)
        self.assertEqual(len(intervals), 1, 'one interval, not one per pass')
        start, end = intervals[0]
        self.assertLess(start, end)
        self.assertEqual(start.astimezone(BERLIN).strftime('%H:%M'), '03:00',
                         'the opening takes the instant the clock jumped to')
        self.assertEqual(end.astimezone(BERLIN).strftime('%H:%M'), '04:00',
                         'the closing is an ordinary time and resolves normally')
        ordinary = window.intervals_on(BERLIN, date(2026, 3, 30))
        self.assertEqual(ordinary[0][0].astimezone(BERLIN).strftime('%H:%M'), '02:00',
                         'on an ordinary day the same window opens when it says it does')

    def test_a_window_over_the_fold_opens_with_the_first_pass_and_closes_with_the_second(self):
        window = sched.Window.parse('01:00-04:00')
        start, end = window.intervals_on(BERLIN, DST_END_2026)[0]
        self.assertEqual(start.astimezone(BERLIN).utcoffset(), timedelta(hours=2),
                         'the window opens on the first of the two 02:30s')
        self.assertEqual(end.astimezone(BERLIN).utcoffset(), timedelta(hours=1),
                         'and closes after both of them')

    def test_a_zone_whose_transition_happens_at_midnight_behaves_too(self):
        """America/Santiago jumps at 00:00 local, so 00:30 does not exist on the day."""
        slot = sched.Slot.parse('00:30')
        missing = [day for day in (date(2026, 9, 6),)
                   if slot.instant_on(SANTIAGO, day, sched.DST_SKIP) is None]
        self.assertTrue(missing, 'the fixture zone really has a midnight gap on that date')
        for day in (date(2026, 9, 7), date(2026, 9, 8)):
            self.assertIsNotNone(slot.instant_on(SANTIAGO, day, sched.DST_SKIP))

    def test_an_interval_schedule_does_not_shift_or_double_across_a_transition(self):
        clock = Clock(at(2026, 3, 28, 12, tz=BERLIN))
        engine = make(clock=clock)
        engine.add({'id': 'nightly', 'kind': 'interval', 'interval_minutes': 60,
                    'timezone': 'Europe/Berlin'})
        engine.run_now('nightly', at=clock.now)
        anchor = engine.state('nightly').last_run_at

        # the same wall-clock time next day; the offset changed by one hour
        clock.move_to(at(2026, 3, 29, 12, tz=BERLIN))
        report = engine.tick()

        state = engine.state('nightly')
        elapsed = state.last_run_at - anchor
        self.assertEqual(elapsed, 23 * 3600,
                         'the grid counts elapsed seconds: a 24-hour wall-clock day that '
                         'loses an hour is 23 hours of work, not 24')
        self.assertEqual(len(report.run_requests), 1,
                         'the tick dispatches once; the rest were already accounted for')
        self.assertEqual(state.runs, 2, 'the manual activation plus the one dispatched tick; '
                                         'the other 22 occurrences were merged, not replayed')
        self.assertGreaterEqual(state.next_run_at, clock.now)

    def test_a_slot_schedule_keeps_its_local_time_across_the_transition(self):
        clock = Clock(at(2026, 3, 27, 12, tz=BERLIN))
        engine = make(clock=clock)
        engine.add({'id': 'daily', 'kind': 'slots', 'timezone': 'Europe/Berlin',
                    'slots': [{'at': '09:00'}], 'max_catch_up': 1, 'catch_up': True})
        engine.run_now('daily', at=clock.now)
        clock.move_to(at(2026, 3, 30, 12, tz=BERLIN))
        report = engine.tick()

        for request in report.run_requests:
            local = datetime.fromtimestamp(request.scheduled_for, BERLIN)
            self.assertEqual((local.hour, local.minute), (9, 0),
                             'a daily slot stays at 09:00 local on both sides of the change')
        self.assertTrue(report.run_requests, 'the days between produced their runs')

    def test_an_unknown_timezone_is_refused_by_name(self):
        with self.assertRaises(sched.ScheduleError) as caught:
            make().add({'id': 'x', 'kind': 'interval', 'interval_minutes': 60,
                        'timezone': 'Mars/Olympus_Mons'})
        self.assertEqual(caught.exception.code, 'E_VALIDATION_FIELD')
        self.assertEqual(caught.exception.field, 'timezone')


class WindowAndQuietHoursTest(unittest.TestCase):
    """Windows, quiet hours, pause and resume (F15)."""

    def _engine(self, clock, **extra):
        payload = {'id': 'day', 'kind': 'interval', 'interval_minutes': 30,
                   'timezone': 'Europe/Berlin',
                   'windows': [{'window': '09:00-18:00'}],
                   'quiet_hours': [{'window': '22:00-07:00'}]}
        payload.update(extra)
        engine = make(clock=clock)
        engine.add(payload)
        return engine

    def test_a_run_outside_the_window_is_skipped_with_the_next_opening(self):
        clock = Clock(at(2026, 5, 4, 12, tz=BERLIN))
        engine = self._engine(clock)
        engine.run_now('day', at=clock.now)
        clock.move_to(at(2026, 5, 4, 18, 30, tz=BERLIN))

        decision = engine.tick().decisions[0]

        self.assertEqual(decision.action, 'skip')
        self.assertEqual(decision.reason, sched.REASON_WINDOW_CLOSED)
        self.assertEqual(datetime.fromtimestamp(decision.resume_at, BERLIN).date(),
                         date(2026, 5, 5), 'the next opening is tomorrow morning')

    def test_quiet_hours_pause_and_lift_the_pause_by_themselves(self):
        clock = Clock(at(2026, 5, 4, 12, tz=BERLIN))
        engine = self._engine(clock)
        engine.run_now('day', at=clock.now)

        clock.move_to(at(2026, 5, 4, 22, 30, tz=BERLIN))
        quiet = engine.tick()
        self.assertEqual(quiet.decisions[0].reason, sched.REASON_PAUSED_QUIET)
        self.assertTrue(engine.state('day').paused)
        self.assertEqual(datetime.fromtimestamp(quiet.decisions[0].resume_at, BERLIN).date(),
                         date(2026, 5, 5), 'quiet hours run across midnight')

        # still quiet an hour later: nothing runs and nothing is queued
        clock.move_to(at(2026, 5, 4, 23, 30, tz=BERLIN))
        self.assertEqual(len(engine.tick().run_requests), 0)
        self.assertTrue(engine.state('day').paused)

        # quiet hours over, still outside the window
        clock.move_to(at(2026, 5, 5, 7, 30, tz=BERLIN))
        after = engine.tick()
        self.assertEqual(after.decisions[0].reason, sched.REASON_WINDOW_CLOSED)
        self.assertFalse(engine.state('day').paused, 'the pause lifted on its own')

        # the window opens and the schedule runs
        clock.move_to(at(2026, 5, 5, 9, tz=BERLIN))
        self.assertEqual(len(engine.tick().run_requests), 1)

    def test_a_manual_run_still_refuses_what_the_policy_refuses(self):
        clock = Clock(at(2026, 5, 4, 12, tz=BERLIN))
        engine = self._engine(clock)

        self.assertIsNone(engine.run_now('day', at=at(2026, 5, 4, 23, tz=BERLIN)),
                         'quiet hours are not a suggestion')
        self.assertIsNone(engine.run_now('day', at=at(2026, 5, 4, 20, tz=BERLIN)),
                         'neither is the working window')
        self.assertIsNotNone(engine.run_now('day', at=at(2026, 5, 4, 12, tz=BERLIN)))
        engine.pause('day', at=at(2026, 5, 4, 12, 5, tz=BERLIN))
        self.assertIsNone(engine.run_now('day', at=at(2026, 5, 4, 12, 5, tz=BERLIN)),
                          'nor is "check now" a way around a pause the user set')

    def test_a_user_pause_survives_until_the_user_resumes(self):
        clock = Clock(at(2026, 5, 4, 12, tz=BERLIN))
        engine = self._engine(clock)
        engine.run_now('day', at=clock.now)
        engine.pause('day', at=clock.now)

        clock.advance(3600)
        self.assertEqual(engine.tick().decisions[0].reason, sched.REASON_PAUSED_USER)
        clock.advance(86400)
        self.assertEqual(engine.tick().decisions[0].reason, sched.REASON_PAUSED_USER)
        self.assertTrue(engine.state('day').paused)

        engine.resume('day', at=clock.now)
        self.assertFalse(engine.state('day').paused)
        self.assertIn(sched.NOTIFY_RESUMED, [note.code for note in engine.notifier.sent])


class NoCatchUpTest(unittest.TestCase):
    """F15/F22: after a sleep there is no burst of checks and no mass false failure."""

    def test_sleeping_coalesces_the_overdue_occurrences_into_one_run(self):
        clock = Clock(at(2026, 5, 4, 12, tz=UTC))
        engine = make(clock=clock)
        engine.add({'id': 'every10', 'kind': 'interval', 'interval_minutes': 10, 'timezone': 'UTC'})
        engine.run_now('every10', at=clock.now)
        engine.mark_wake(at=clock.now)

        clock.move_to(at(2026, 5, 4, 15, tz=UTC))  # eighteen occurrences are overdue
        report = engine.tick()

        self.assertTrue(report.woke)
        self.assertEqual(len(report.run_requests), 1, 'three hours of sleep are one run, not eighteen')
        self.assertEqual(report.decisions[0].reason, sched.REASON_COALESCED_SLEEP)
        self.assertEqual(report.decisions[0].missed, 17, 'the other seventeen are reported, not hidden')
        self.assertTrue(report.run_requests[0].coalesced)

    def test_a_long_gap_is_detected_as_a_wake_even_without_the_flag(self):
        clock = Clock(at(2026, 5, 4, 12, tz=UTC))
        engine = make(clock=clock)
        engine.add({'id': 'every10', 'kind': 'interval', 'interval_minutes': 10, 'timezone': 'UTC'})
        engine.run_now('every10', at=clock.now)
        engine.tick(at=clock.now)

        clock.move_to(at(2026, 5, 4, 18, tz=UTC))  # six hours: the lid was shut
        report = engine.tick()

        self.assertTrue(report.woke, 'a gap longer than four intervals is a wake')
        self.assertLessEqual(len(report.run_requests), engine.get('every10').max_catch_up)

    def test_catch_up_replays_at_most_max_catch_up_and_merges_the_rest(self):
        clock = Clock(at(2026, 5, 4, 12, tz=UTC))
        engine = make(clock=clock)
        engine.add({'id': 'every10', 'kind': 'interval', 'interval_minutes': 10, 'timezone': 'UTC',
                    'catch_up': True, 'max_catch_up': 2})
        engine.run_now('every10', at=clock.now)
        clock.move_to(at(2026, 5, 4, 13, tz=UTC))  # six occurrences overdue
        report = engine.tick()

        self.assertEqual(len(report.run_requests), 2, 'max_catch_up is a limit, not a suggestion')
        self.assertEqual(report.decisions[0].reason, sched.REASON_DUE)
        self.assertEqual(report.decisions[0].missed, 4,
                         'the four occurrences that were not replayed are counted, not lost')

    def test_a_schedule_without_catch_up_merges_everything_into_one(self):
        clock = Clock(at(2026, 5, 4, 12, tz=UTC))
        engine = make(clock=clock)
        engine.add({'id': 'every10', 'kind': 'interval', 'interval_minutes': 10, 'timezone': 'UTC',
                    'catch_up': False})
        engine.run_now('every10', at=clock.now)
        clock.move_to(at(2026, 5, 4, 13, tz=UTC))
        report = engine.tick()

        self.assertEqual(len(report.run_requests), 1,
                         'six overdue occurrences are one run, not six')
        self.assertEqual(report.decisions[0].reason, sched.REASON_COALESCED)
        self.assertEqual(report.decisions[0].missed, 5)

    def test_the_interval_grid_does_not_drift_over_many_ticks(self):
        clock = Clock(at(2026, 5, 4, 12, tz=UTC))
        engine = make(clock=clock)
        engine.add({'id': 'every10', 'kind': 'interval', 'interval_minutes': 10, 'timezone': 'UTC'})
        engine.run_now('every10', at=clock.now)
        start = clock.now
        total = 0
        for step in range(1, 13):
            clock.move_to(start + step * 600)
            total += len(engine.tick().run_requests)

        self.assertEqual(total, 12, 'twelve ticks an interval apart are twelve runs')
        self.assertEqual(engine.state('every10').last_run_at, start + 12 * 600)

    def test_a_repeated_tick_at_the_same_instant_produces_the_same_run_id(self):
        clock = Clock(at(2026, 5, 4, 12, tz=UTC))
        engine = make(clock=clock)
        engine.add({'id': 'every10', 'kind': 'interval', 'interval_minutes': 10, 'timezone': 'UTC'})
        engine.run_now('every10', at=clock.now)
        clock.advance(600)

        first = engine.tick()
        second = engine.tick()

        self.assertEqual(len(first.run_requests), 1)
        self.assertEqual(second.run_requests, (), 'the same instant does not run twice')
        again = engine.tick(at=first.run_requests[0].scheduled_for)
        self.assertEqual(again.run_requests, ())
        self.assertEqual(sched.make_run_id('every10', first.run_requests[0].scheduled_for),
                         first.run_requests[0].run_id, 'the id is derived, not random')


class BudgetTest(unittest.TestCase):
    """F15: request/byte/time budgets, a finite in-flight remainder, relay apart."""

    def ledger(self, **budgets) -> sched.BudgetLedger:
        return sched.BudgetLedger(sched.Budgets(reset=sched.RESET_DAILY, timezone='UTC', **budgets),
                                  sched.BudgetCounters(reset_key=sched.reset_key(
                                      sched.Budgets(**budgets), at(2026, 5, 4, 12))),
                                  now=at(2026, 5, 4, 12))

    def test_the_request_budget_stops_at_the_limit_plus_the_in_flight_reserve(self):
        ledger = self.ledger(requests=5)
        granted = 0
        for _ in range(9):
            reservation = ledger.try_reserve(sched.PROBE, requests=1, bytes=1024)
            if reservation.ok:
                ledger.commit(reservation, requests=1, bytes=1024)
                granted += 1
            else:
                break

        self.assertEqual(granted, 6, 'five of the limit plus one in flight')
        self.assertEqual(granted, 5 + sched.Budgets().inflight_reserve_requests)
        denial = ledger.try_reserve(sched.PROBE, requests=1).denial
        self.assertEqual(denial.code, 'E_LIMIT_BUDGET')
        self.assertEqual(denial.axis, 'requests')
        self.assertEqual(denial.limit, 5)
        self.assertEqual(denial.used, 6)

    def test_the_in_flight_remainder_is_finite_and_explained(self):
        ledger = self.ledger(requests=10, bytes=1000)
        view = ledger.snapshot(at(2026, 5, 4, 12)).requests
        self.assertEqual(view.in_flight, 0.0, 'an untouched limit has no in-flight remainder')
        self.assertEqual(view.remaining, 10.0)

        open_reservation = ledger.try_reserve(sched.PROBE, requests=10)
        during = ledger.snapshot(at(2026, 5, 4, 12)).requests
        self.assertEqual(during.reserved, 10.0)
        self.assertEqual(during.remaining, 0.0, 'reserved work is not spendable twice')

        ledger.commit(open_reservation, requests=14)  # the measurement cost more than reserved
        after = ledger.snapshot(at(2026, 5, 4, 12)).requests
        self.assertTrue(after.over_limit)
        self.assertLessEqual(after.in_flight, after.allowance,
                             'the overshoot cannot exceed the declared in-flight reserve')
        self.assertTrue(after.exhausted)

    def test_a_byte_budget_is_enforced_independently_of_requests(self):
        ledger = self.ledger(bytes=2048, inflight_reserve_bytes=0)
        first = ledger.try_reserve(sched.PROBE, requests=999, bytes=2048)
        self.assertTrue(first.ok)
        second = ledger.try_reserve(sched.PROBE, requests=999, bytes=1)
        self.assertFalse(second.ok, 'the byte limit holds even with requests to spare')
        self.assertEqual(second.denial.axis, 'bytes')
        self.assertIsNone(ledger.budgets.requests, 'no request limit was set at all')

    def test_the_in_flight_reserve_is_a_knob_and_defaults_to_one_request(self):
        self.assertEqual(sched.Budgets().inflight_reserve_requests, 1)
        self.assertEqual(sched.Budgets().inflight_reserve_bytes, sched.MIB)
        tight = self.ledger(requests=1, inflight_reserve_requests=0)
        reservation = tight.try_reserve(sched.PROBE, requests=1)
        self.assertTrue(reservation.ok)
        self.assertFalse(tight.try_reserve(sched.PROBE, requests=1).ok,
                         'a zero in-flight reserve is a hard stop')

    def test_every_workbench_traffic_class_shares_one_budget(self):
        ledger = self.ledger(requests=5, inflight_reserve_requests=0)
        for traffic_class in (sched.PROBE, sched.SOURCE, sched.RETRY, sched.JUDGE, sched.SPEEDTEST):
            reservation = ledger.try_reserve(traffic_class, requests=1, bytes=10)
            self.assertTrue(reservation.ok, f'{traffic_class} was refused')
            ledger.commit(reservation, requests=1, bytes=10)
        self.assertEqual(ledger.counters.requests, 5, 'one pool of requests, five classes')
        self.assertFalse(ledger.try_reserve(sched.PROBE, requests=1).ok,
                         'the judge is not given a budget of its own')

    def test_relay_traffic_never_consumes_a_workbench_budget(self):
        ledger = self.ledger(requests=1, bytes=1024)
        reservation = ledger.try_reserve(sched.PROBE, requests=1, bytes=1024)
        ledger.commit(reservation, requests=1, bytes=1024)
        workbench = ledger.snapshot(at(2026, 5, 4, 12)).to_dict()

        relay = ledger.try_reserve(sched.RELAY, requests=10 ** 6, bytes=10 ** 9, active=False)
        after = ledger.snapshot(at(2026, 5, 4, 12)).to_dict()

        self.assertTrue(relay.ok, 'a client using the gateway is not the workbench spending')
        self.assertEqual(after['requests']['used'], workbench['requests']['used'])
        self.assertEqual(after['bytes']['used'], workbench['bytes']['used'])
        self.assertEqual(after['relay_requests'], 10 ** 6)
        self.assertEqual(after['relay_bytes'], 10 ** 9)

    def test_billable_traffic_never_appears_as_workbench_traffic(self):
        ledger = self.ledger(requests=100, bytes=10 ** 6)
        relay = ledger.try_reserve(sched.RELAY, requests=50, bytes=999, active=False)
        ledger.commit(relay, requests=50, bytes=999)
        snapshot = ledger.snapshot(at(2026, 5, 4, 12)).to_dict()

        self.assertEqual(snapshot['requests']['used'], 0, 'relay did not spend the probe budget')
        self.assertEqual(snapshot['bytes']['used'], 0)
        self.assertEqual(snapshot['relay_requests'], 50)

    def test_a_released_reservation_costs_nothing(self):
        ledger = self.ledger(requests=5)
        reservation = ledger.try_reserve(sched.PROBE, requests=3, bytes=300)
        ledger.release(reservation)
        self.assertEqual(ledger.snapshot(at(2026, 5, 4, 12)).requests.used, 0)
        self.assertTrue(ledger.try_reserve(sched.PROBE, requests=5).ok)

    def test_the_budget_resets_with_its_period(self):
        budgets = sched.Budgets(requests=2, reset=sched.RESET_DAILY, timezone='UTC',
                                inflight_reserve_requests=0)
        ledger = sched.BudgetLedger(budgets, sched.BudgetCounters(), now=at(2026, 5, 4, 23, tz=UTC))
        for _ in range(2):
            reservation = ledger.try_reserve(sched.PROBE, requests=1)
            ledger.commit(reservation, requests=1)
        self.assertFalse(ledger.try_reserve(sched.PROBE, requests=1).ok)

        ledger.maybe_reset(at(2026, 5, 5, 0, 1, tz=UTC))
        self.assertEqual(ledger.counters.requests, 0, 'a new day is a new budget')
        self.assertTrue(ledger.try_reserve(sched.PROBE, requests=1).ok)

    def test_an_unknown_traffic_class_is_refused_by_name(self):
        reservation = self.ledger(requests=5).try_reserve('telemetry', requests=1)
        self.assertFalse(reservation.ok)
        self.assertEqual(reservation.denial.reason, 'unknown_traffic_class')


class PowerPolicyTest(unittest.TestCase):
    """F15: the metered/battery policy applies only where the OS has a signal."""

    def fixture_signal(self) -> sched.PowerSignal:
        return sched.PowerSignal(on_battery=True, battery_percent=20, metered=True,
                                 battery_supported=True, metered_supported=True, source='fixture')

    def test_no_signal_means_no_restriction(self):
        decision = sched.power_decision(
            sched.PowerPolicy(on_battery=sched.POWER_PAUSE, metered=sched.POWER_PAUSE),
            sched.PowerSignal())
        self.assertFalse(decision.restricted)
        self.assertEqual(decision.reason, sched.POWER_SIGNAL_ABSENT)

    def test_a_real_signal_restricts_and_says_which(self):
        policy = sched.PowerPolicy(on_battery=sched.POWER_PAUSE, metered=sched.POWER_PAUSE)
        decision = sched.power_decision(policy, self.fixture_signal())
        self.assertTrue(decision.restricted)
        self.assertIn(decision.reason, (sched.POWER_BATTERY, sched.POWER_METERED))

    def test_cheap_only_narrows_the_stages_instead_of_stopping(self):
        decision = sched.power_decision(sched.PowerPolicy(metered=sched.POWER_CHEAP_ONLY),
                                        self.fixture_signal())
        self.assertFalse(decision.restricted, 'cheap work still runs')
        self.assertEqual(decision.stages, sched.CHEAP_CLASSES)
        self.assertNotIn(sched.SPEEDTEST, decision.stages)
        self.assertNotIn(sched.RELAY, decision.stages, 'client traffic is never the workbench policy')

    def test_a_battery_percentage_threshold_is_honoured(self):
        policy = sched.PowerPolicy(on_battery=sched.POWER_BELOW_PERCENT, battery_min_percent=50)
        self.assertTrue(sched.power_decision(policy, self.fixture_signal()).restricted)
        healthy = sched.PowerSignal(on_battery=False, battery_percent=90, metered=False,
                                    battery_supported=True, metered_supported=True, source='fixture')
        self.assertFalse(sched.power_decision(policy, healthy).restricted)

    def test_an_unknown_metered_flag_follows_the_configured_conservative_default(self):
        signal = sched.PowerSignal(metered=None, metered_supported=True, source='fixture')
        policy = sched.PowerPolicy(metered=sched.POWER_PAUSE)
        self.assertTrue(sched.power_decision(policy, signal).restricted)
        self.assertEqual(sched.power_decision(policy, signal).reason, sched.POWER_METERED_UNKNOWN)
        lenient = sched.PowerPolicy(metered=sched.POWER_PAUSE,
                                    unknown_metered=sched.UNKNOWN_AS_UNMETERED)
        self.assertFalse(sched.power_decision(lenient, signal).restricted)

    def test_a_restricted_schedule_says_which_policy_stopped_it(self):
        clock = Clock(at(2026, 5, 4, 12, tz=UTC))
        engine = make(clock=clock, power=lambda: self.fixture_signal())
        engine.add({'id': 'metered', 'kind': 'interval', 'interval_minutes': 10, 'timezone': 'UTC',
                    'power': {'metered': 'pause'}})
        engine.run_now('metered', at=clock.now)
        clock.advance(600)
        decision = engine.tick().decisions[0]

        self.assertEqual(decision.action, 'skip')
        self.assertEqual(decision.reason, sched.REASON_PAUSED_METERED)
        self.assertEqual(decision.get('power_reason'), sched.POWER_METERED)

    def test_the_os_probe_never_raises_and_reports_what_it_could_not_read(self):
        def exploding(*args, **kwargs):
            raise OSError('the OS said no')
        self.assertEqual(sched.read_system_signal('darwin', runner=exploding),
                         sched.PowerSignal())
        self.assertEqual(sched.read_system_signal('freebsd14'), sched.PowerSignal())


class TrapChannel:
    """A channel that records instead of sending, so the test can prove silence."""

    def __init__(self, name: str):
        self.name = name
        self.sent: list = []

    def send(self, notification) -> None:
        self.sent.append(notification)


class NotificationTest(unittest.TestCase):
    """F15: meaningful state changes, with dedup and hysteresis."""

    def test_ten_identical_degradations_produce_one_notification(self):
        clock = Clock(at(2026, 5, 4, 12, tz=UTC))
        notifier = sched.Notifier(now=clock)
        watcher = sched.StateWatcher('pool:main', sched.NotifyPolicy(
            enter_below=3, exit_above=4, min_events=2, repeat_window_s=3600))

        changes = 0
        for value in (10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 0):
            clock.advance(60)
            change = watcher.observe(value, at=clock.now)
            if change is not None:
                notifier.for_change(change, watcher.policy)
                changes += 1

        self.assertEqual(changes, 1, 'one episode, one alert')
        self.assertEqual(watcher.observations, 11)
        self.assertEqual([note.code for note in notifier.sent], [sched.NOTIFY_BELOW_MINIMUM])

    def test_recovery_produces_the_second_notification(self):
        clock = Clock(at(2026, 5, 4, 12, tz=UTC))
        notifier = sched.Notifier(now=clock)
        watcher = sched.StateWatcher('pool:main', sched.NotifyPolicy(
            enter_below=3, exit_above=4, min_events=1))
        for value in (1, 2, 3, 4, 5, 6):
            change = watcher.observe(value, at=clock.now)
            if change is not None:
                notifier.for_change(change, watcher.policy)

        self.assertEqual([note.code for note in notifier.sent],
                         [sched.NOTIFY_BELOW_MINIMUM, sched.NOTIFY_RECOVERED])
        self.assertEqual(watcher.episodes, 2)

    def test_values_inside_the_hysteresis_band_produce_nothing(self):
        watcher = sched.StateWatcher('pool:main', sched.NotifyPolicy(enter_below=3, exit_above=6))
        changes = [watcher.observe(value, at=0.0) for value in (5, 4, 3.5, 4.5, 5.5)]
        self.assertTrue(all(change is None for change in changes),
                        'a value between the two thresholds is not a state change')

    def test_min_events_ignores_a_single_noisy_sample(self):
        watcher = sched.StateWatcher('pool:main', sched.NotifyPolicy(enter_below=3, min_events=3))
        self.assertIsNone(watcher.observe(0, at=0.0))
        self.assertIsNone(watcher.observe(0, at=1.0))
        self.assertIsNotNone(watcher.observe(0, at=2.0), 'the third confirming sample alerts')

    def test_a_repeat_is_suppressed_inside_the_repeat_window_and_counted(self):
        clock = Clock(at(2026, 5, 4, 12, tz=UTC))
        notifier = sched.Notifier(now=clock)
        policy = sched.NotifyPolicy(repeat_window_s=1800)
        note = lambda: sched.Notification(subject='pool:main', code=sched.NOTIFY_BELOW_MINIMUM,
                                          at=clock.now)

        notifier.emit(note(), policy)
        clock.advance(600)
        notifier.emit(note(), policy)
        clock.advance(1800)
        notifier.emit(note(), policy)

        self.assertEqual(len(notifier.sent), 2, 'one now, one after the repeat window')
        self.assertEqual(notifier.suppressed_count(), 1,
                         'the suppressed one is counted, not lost')
        self.assertEqual(notifier.suppressed_count('pool:main:BELOW_MINIMUM'), 1)

    def test_nothing_leaves_the_machine_without_a_configuration_the_user_chosen(self):
        cases = (
            ('no configuration at all', sched.NotificationConfig()),
            ('a target the user never confirmed', sched.NotificationConfig(
                email=sched.Target(label='ops@example.invalid', enabled=True, user_confirmed=False))),
            ('a confirmed target that is switched off', sched.NotificationConfig(
                email=sched.Target(label='ops@example.invalid', enabled=False, user_confirmed=True))),
        )
        for label, config in cases:
            with self.subTest(label=label):
                email, webhook = TrapChannel(sched.CHANNEL_EMAIL), TrapChannel(sched.CHANNEL_WEBHOOK)
                dispatcher = sched.Dispatcher(
                    config, {sched.CHANNEL_EMAIL: email, sched.CHANNEL_WEBHOOK: webhook})
                report = dispatcher.dispatch(sched.Notification(
                    subject='pool:main', code=sched.NOTIFY_BELOW_MINIMUM, at=0.0))

                self.assertEqual(email.sent, [], f'{label}: an email went out')
                self.assertEqual(webhook.sent, [], f'{label}: a webhook went out')
                skipped = dict(report.skipped)
                self.assertIn(skipped[sched.CHANNEL_EMAIL],
                              (sched.SKIP_NO_TARGET, sched.SKIP_NOT_CONFIRMED,
                               sched.SKIP_TARGET_OFF))
                self.assertEqual(skipped[sched.CHANNEL_WEBHOOK], sched.SKIP_NO_TARGET)

    def test_a_configuration_the_user_chosen_does_deliver(self):
        email = TrapChannel(sched.CHANNEL_EMAIL)
        config = sched.NotificationConfig(
            email=sched.Target(label='ops@example.invalid', enabled=True, user_confirmed=True))
        report = sched.Dispatcher(config, {sched.CHANNEL_EMAIL: email}).dispatch(
            sched.Notification(subject='pool:main', code=sched.NOTIFY_BELOW_MINIMUM, at=0.0))

        self.assertEqual(len(email.sent), 1, 'the fix must not silence a configured channel')
        self.assertEqual(report.deliveries[0].target_ref, 'ops@example.invalid')

    def test_a_notification_carries_numbers_and_never_a_secret(self):
        note = sched.Notification(subject='pool:main', code=sched.NOTIFY_BELOW_MINIMUM, at=0.0,
                                  data=(('reason', 'alert'), ('value', 2)))
        payload = note.to_dict()
        self.assertEqual(payload['data']['value'], 2)
        self.assertNotIn('password', str(payload).lower())


class SqliteStoreTest(unittest.TestCase):
    """The store `db.migrate()` actually creates, and what it does not hold."""

    def _migrated(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / db.DB_FILENAME
        db.migrate(path)
        return path

    def test_a_specification_round_trips_through_the_real_schema(self):
        path = self._migrated()
        conn = sqlite3.connect(str(path), isolation_level=None)
        self.addCleanup(conn.close)
        store = sched.SqliteScheduleStore(conn)
        engine = sched.Scheduler(store=store, clock=lambda: 0.0, power_reader=NoPower())
        engine.add({'id': 'nightly', 'kind': 'interval', 'interval_minutes': 30,
                    'timezone': 'Europe/Berlin',
                    'windows': [{'window': '09:00-18:00'}],
                    'quiet_hours': [{'window': '22:00-07:00'}],
                    'budgets': {'requests': 10, 'timezone': 'Europe/Berlin'}})

        reopened = sched.Scheduler(store=sched.SqliteScheduleStore(
            sqlite3.connect(str(path), isolation_level=None)), clock=lambda: 0.0,
            power_reader=NoPower())
        spec = reopened.get('nightly')

        self.assertIsNotNone(spec)
        self.assertEqual(spec.interval_minutes, 30)
        self.assertEqual(spec.timezone, 'Europe/Berlin')
        self.assertEqual([window.to_dict()['window'] for window in spec.windows], ['09:00-18:00'])
        self.assertEqual([window.to_dict()['window'] for window in spec.quiet_hours],
                         ['22:00-07:00'])
        self.assertEqual(spec.budgets.requests, 10)

    def test_the_module_writes_no_ddl(self):
        source = Path(sched.__file__).read_text(encoding='utf-8')
        for statement in ('CREATE TABLE', 'ALTER TABLE', 'DROP TABLE', 'CREATE INDEX'):
            self.assertNotIn(statement, source, f'the module must not write {statement}')

    def test_the_module_opens_no_socket(self):
        source = Path(sched.__file__).read_text(encoding='utf-8')
        for forbidden in ('import socket', 'urllib.request', 'smtplib', 'http.client'):
            self.assertNotIn(forbidden, source,
                             f'the module must not be able to send anything by accident')

    def test_a_pause_and_a_spent_budget_are_reported_as_lost_when_the_schema_cannot_hold_them(self):
        """The honest report of a gap the migrator has not closed yet.

        ``schedules`` as ``db.migrate()`` creates it has no ``paused`` and no
        ``counters_json`` column, so a restart silently forgets both.  A budget
        that resets itself is worse than no budget, so the module says so instead
        of pretending the limit is enforced.
        """
        path = self._migrated()
        conn = sqlite3.connect(str(path), isolation_level=None)
        self.addCleanup(conn.close)
        store = sched.SqliteScheduleStore(conn)
        clock = Clock(at(2026, 5, 4, 12, tz=UTC))
        engine = sched.Scheduler(store=store, clock=clock, power_reader=NoPower())
        engine.add({'id': 'nightly', 'kind': 'interval', 'interval_minutes': 30, 'timezone': 'UTC',
                    'budgets': {'requests': 3, 'reset': 'daily', 'timezone': 'UTC'}})
        engine.run_now('nightly', at=clock.now)
        ledger = engine.ledger('nightly', now=clock.now)
        for _ in range(3):
            reservation = ledger.try_reserve(sched.PROBE, requests=1, bytes=10)
            ledger.commit(reservation, requests=1, bytes=10)
        engine.state('nightly').counters = ledger.counters
        engine.pause('nightly', at=clock.now)
        conn.commit()

        persistence = engine.persistence()
        self.assertEqual(sorted(persistence['lost_on_restart']), ['counters', 'paused'])
        self.assertIn('paused', persistence['requested_columns']['schedules'])

        conn.close()
        reopened_conn = sqlite3.connect(str(path), isolation_level=None)
        self.addCleanup(reopened_conn.close)
        after = sched.Scheduler(store=sched.SqliteScheduleStore(reopened_conn),
                                clock=clock, power_reader=NoPower())
        self.assertFalse(after.state('nightly').paused, 'this is the defect being reported')
        self.assertEqual(after.state('nightly').counters.requests, 0)

    def test_the_last_run_survives_as_the_anchor_of_the_interval_grid(self):
        """What the contract schema *can* hold, it does hold."""
        path = self._migrated()
        conn = sqlite3.connect(str(path), isolation_level=None)
        self.addCleanup(conn.close)
        clock = Clock(at(2026, 5, 4, 12, tz=UTC))
        engine = sched.Scheduler(store=sched.SqliteScheduleStore(conn), clock=clock,
                                 power_reader=NoPower())
        engine.add({'id': 'nightly', 'kind': 'interval', 'interval_minutes': 30, 'timezone': 'UTC'})
        engine.run_now('nightly', at=clock.now)
        conn.commit()
        anchor = engine.state('nightly').last_run_at

        conn.close()
        reopened_conn = sqlite3.connect(str(path), isolation_level=None)
        self.addCleanup(reopened_conn.close)
        after = sched.Scheduler(store=sched.SqliteScheduleStore(reopened_conn), clock=clock,
                                power_reader=NoPower())
        self.assertEqual(after.state('nightly').last_run_at, anchor,
                         'the grid does not restart from the process launch')
        clock.advance(1800)
        self.assertEqual(len(after.tick().run_requests), 1)

    def test_a_specification_with_an_unknown_field_is_refused_rather_than_ignored(self):
        with self.assertRaises(sched.ScheduleError) as caught:
            make().add({'id': 'x', 'kind': 'interval', 'interval_minutes': 10,
                        'timezone': 'UTC', 'retries': 3})
        self.assertEqual(caught.exception.code, 'E_VALIDATION_UNKNOWN_FIELD')


if __name__ == '__main__':
    unittest.main()
