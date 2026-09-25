"""Windows, slots, DST and the reset boundary (F15)."""
from datetime import datetime, timezone
from pathlib import Path
import sys
import unittest
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import scheduler as sch

UTC = timezone.utc
BERLIN = ZoneInfo('Europe/Berlin')
SANTIAGO = ZoneInfo('America/Santiago')


def at(tz, *args, fold=0):
    """A local wall-clock moment, as the tests spell it."""
    return datetime(*args, tzinfo=tz, fold=fold)


def stamp(tz, *args):
    """The same moment as an aware UTC datetime, which is what the module returns."""
    return at(tz, *args).astimezone(UTC)


class ParseTests(unittest.TestCase):
    def test_accepted_and_rejected_times(self):
        self.assertEqual(sch.parse_hhmm('09:30'), 570)
        self.assertEqual(sch.parse_hhmm('9:05'), 545)
        self.assertEqual(sch.parse_hhmm(0), 0)
        self.assertEqual(sch.format_hhmm(545), '09:05')
        for bad in ('24:00', '9:5', '0900', '9', '', None, 'ab:cd', -1, True, 1440):
            with self.assertRaises(sch.ScheduleError) as caught:
                sch.parse_hhmm(bad)
            self.assertEqual(caught.exception.code, 'E_VALIDATION_FIELD')

    def test_window_needs_a_non_empty_range(self):
        self.assertEqual(sch.Window.parse('09:00-18:00').end, 18 * 60)
        self.assertTrue(sch.Window.parse('22:00-06:00').crosses_midnight)
        with self.assertRaises(sch.ScheduleError):
            sch.Window.parse('09:00-09:00')
        with self.assertRaises(sch.ScheduleError):
            sch.Window.parse('09:00')          # no range at all
        with self.assertRaises(sch.ScheduleError):
            sch.Window.parse('09:00-18:00', weekdays=['sun', 'funday'])

    def test_unknown_timezone_is_rejected_before_anything_runs(self):
        with self.assertRaises(sch.ScheduleError) as caught:
            sch.ScheduleSpec(id='x', interval_minutes=5, timezone='Mars/Olympus')
        self.assertEqual(caught.exception.field, 'timezone')
        self.assertEqual(caught.exception.code, 'E_VALIDATION_FIELD')
        self.assertIn('Mars/Olympus', caught.exception.text())


class MembershipTests(unittest.TestCase):
    def test_inside_and_outside(self):
        window = sch.Window.parse('09:00-18:00')
        self.assertTrue(window.contains(at(BERLIN, 2026, 9, 28, 9, 0)))
        self.assertTrue(window.contains(at(BERLIN, 2026, 9, 28, 17, 59)))
        self.assertFalse(window.contains(at(BERLIN, 2026, 9, 28, 18, 0)))
        self.assertFalse(window.contains(at(BERLIN, 2026, 9, 28, 8, 59)))

    def test_window_over_midnight_belongs_to_the_day_it_opens_on(self):
        window = sch.Window.parse('22:00-06:00', weekdays=['mon'])
        self.assertTrue(window.contains(at(BERLIN, 2026, 9, 21, 23, 0)))    # Monday night
        self.assertTrue(window.contains(at(BERLIN, 2026, 9, 22, 3, 0)))     # tail of Monday
        self.assertFalse(window.contains(at(BERLIN, 2026, 9, 22, 23, 0)))   # Tuesday did not open a window
        self.assertFalse(window.contains(at(BERLIN, 2026, 9, 26, 3, 0)))    # tail of Friday
        self.assertFalse(window.contains(at(BERLIN, 2026, 9, 25, 23, 0)))  # Friday did not open a window

    def test_no_windows_means_always_open(self):
        self.assertTrue(sch.in_windows((), at(BERLIN, 2026, 9, 28, 3, 0), BERLIN))
        self.assertFalse(sch.in_windows((sch.Window.parse('09:00-18:00'),), at(BERLIN, 2026, 9, 28, 3, 0), BERLIN))


class IntervalTests(unittest.TestCase):
    def test_open_interval_matches_local_clock(self):
        window = sch.Window.parse('09:00-18:00')
        start, end = window.intervals_on(BERLIN, datetime(2026, 9, 28).date())[0]
        self.assertEqual(start, stamp(BERLIN, 2026, 9, 28, 9, 0))
        self.assertEqual(end, stamp(BERLIN, 2026, 9, 28, 18, 0))

    def test_window_over_midnight_spans_two_dates(self):
        window = sch.Window.parse('22:00-06:00', weekdays=['mon'])
        start, end = window.intervals_on(BERLIN, datetime(2026, 9, 21).date())[0]
        self.assertEqual(start, stamp(BERLIN, 2026, 9, 21, 22, 0))
        self.assertEqual(end, stamp(BERLIN, 2026, 9, 22, 6, 0))
        self.assertEqual(window.intervals_on(BERLIN, datetime(2026, 9, 22).date()), [])

    def test_next_open_and_current_close(self):
        window = sch.Window.parse('09:00-18:00')
        now = at(BERLIN, 2026, 9, 28, 10, 0)
        self.assertEqual(window.next_open(stamp(BERLIN, 2026, 9, 27, 20, 0), BERLIN),
                         stamp(BERLIN, 2026, 9, 28, 9, 0))
        self.assertEqual(window.next_open(now.astimezone(UTC), BERLIN), now.astimezone(UTC))
        self.assertEqual(window.current_close(now.astimezone(UTC), BERLIN), stamp(BERLIN, 2026, 9, 28, 18, 0))
        self.assertIsNone(window.current_close(stamp(BERLIN, 2026, 9, 28, 7, 0), BERLIN))

    def test_jump_over_a_closed_weekend(self):
        window = sch.Window.parse('09:00-18:00', weekdays=['mon', 'tue', 'wed', 'thu', 'fri'])
        opened = window.next_open(at(UTC, 2026, 9, 26, 12, 0), BERLIN)   # Saturday
        self.assertEqual(opened, stamp(BERLIN, 2026, 9, 28, 9, 0))

    def test_quiet_hours_report_when_they_end(self):
        quiet = (sch.Window.parse('22:00-08:00'),)
        inside = stamp(BERLIN, 2026, 9, 28, 23, 0)
        self.assertEqual(sch.quiet_until(quiet, inside, BERLIN), stamp(BERLIN, 2026, 9, 29, 8, 0))
        self.assertIsNone(sch.quiet_until(quiet, stamp(BERLIN, 2026, 9, 28, 12, 0), BERLIN))


class DstTests(unittest.TestCase):
    """The wall clock moves twice a year; a schedule must not gain or lose a run because of it."""

    def test_local_instants_counts_the_real_number_of_occurrences(self):
        ordinary = sch.local_instants(BERLIN, datetime(2026, 6, 1, 12, 30))
        self.assertEqual(len(ordinary), 1)
        self.assertEqual(len(sch.local_instants(BERLIN, datetime(2026, 10, 25, 2, 30))), 2)  # fold
        self.assertEqual(sch.local_instants(BERLIN, datetime(2026, 3, 29, 2, 30)), [])      # gap

    def test_transition_instant_is_the_jump_itself(self):
        self.assertEqual(sch.dst_gap_transition(BERLIN, datetime(2026, 3, 29, 2, 30)),
                         at(UTC, 2026, 3, 29, 1, 0))

    def test_window_inside_the_gap_never_opens(self):
        window = sch.Window.parse('02:00-02:30')       # entirely inside the spring-forward gap
        self.assertEqual(window.intervals_on(BERLIN, datetime(2026, 3, 29).date()), [])
        self.assertFalse(window.contains(at(BERLIN, 2026, 3, 29, 3, 15)))

    def test_window_spanning_the_gap_stops_at_the_transition(self):
        window = sch.Window.parse('01:00-02:30')       # the tail of the range does not exist
        start, end = window.intervals_on(BERLIN, datetime(2026, 3, 29).date())[0]
        self.assertEqual(start, stamp(BERLIN, 2026, 3, 29, 1, 0))
        self.assertEqual(end, at(UTC, 2026, 3, 29, 1, 0))
        self.assertTrue(window.contains(at(BERLIN, 2026, 3, 29, 1, 59)))
        self.assertFalse(window.contains(at(BERLIN, 2026, 3, 29, 3, 0)))

    def test_window_covering_the_fold_covers_both_passes_once(self):
        window = sch.Window.parse('02:00-03:00')
        start, end = window.intervals_on(BERLIN, datetime(2026, 10, 25).date())[0]
        self.assertEqual(start, at(UTC, 2026, 10, 25, 0, 0))      # first pass, 02:00 CEST
        self.assertEqual(end, at(UTC, 2026, 10, 25, 2, 0))        # second pass, 03:00 CET
        self.assertTrue(window.contains(at(BERLIN, 2026, 10, 25, 2, 30, fold=0)))
        self.assertTrue(window.contains(at(BERLIN, 2026, 10, 25, 2, 30, fold=1)))

    def test_daily_slot_fires_once_on_a_fold_night(self):
        slot = sch.Slot.parse('02:30')
        first = slot.instant_on(BERLIN, datetime(2026, 10, 25).date(), sch.DST_SKIP)
        self.assertEqual(first, at(UTC, 2026, 10, 25, 0, 30))
        # After the first pass the second one must not produce a second run.
        following, skipped = sch.next_slots(BERLIN, (slot,), at(UTC, 2026, 10, 25, 0, 30), sch.DST_SKIP)
        self.assertEqual(following, at(UTC, 2026, 10, 26, 1, 30))
        self.assertEqual(skipped, 0)

    def test_daily_slot_on_a_gap_day_is_skipped_or_shifted(self):
        slot = sch.Slot.parse('02:30')
        day = datetime(2026, 3, 29).date()
        self.assertIsNone(slot.instant_on(BERLIN, day, sch.DST_SKIP))
        self.assertEqual(slot.instant_on(BERLIN, day, sch.DST_SHIFT), at(UTC, 2026, 3, 29, 1, 0))
        _, skipped = sch.next_slots(BERLIN, (slot,), at(UTC, 2026, 3, 28, 12, 0), sch.DST_SKIP)
        following, skipped = sch.next_slots(BERLIN, (slot,), at(UTC, 2026, 3, 29, 12, 0), sch.DST_SKIP)
        self.assertEqual(following, at(UTC, 2026, 3, 30, 0, 30))     # March 30, the gap day is skipped
        self.assertEqual(skipped, 1)

    def test_daily_reset_uses_local_midnight_even_when_it_does_not_exist(self):
        budgets = sch.Budgets(requests=10, reset=sch.RESET_DAILY, timezone='America/Santiago')
        # 2026-09-06 00:00 local does not exist in Santiago: the clock jumps to 01:00.
        before = at(UTC, 2026, 9, 5, 4, 0)
        after = at(UTC, 2026, 9, 6, 4, 30)  # the day is already 01:00 local
        self.assertEqual(sch.reset_key(budgets, before.timestamp()), 'daily:2026-09-05')
        self.assertEqual(sch.reset_key(budgets, after.timestamp()), 'daily:2026-09-06')
        self.assertEqual(sch.next_reset_at(budgets, before.timestamp()),
                         at(UTC, 2026, 9, 6, 4, 0).timestamp())

    def test_reset_keys_and_next_reset(self):
        hourly = sch.Budgets(requests=1, reset=sch.RESET_HOURLY, timezone='UTC')
        now = at(UTC, 2026, 9, 28, 10, 30)
        self.assertEqual(sch.reset_key(hourly, now.timestamp()), 'hourly:2026-09-28T10')
        self.assertEqual(sch.next_reset_at(hourly, now.timestamp()),
                         at(UTC, 2026, 9, 28, 11, 0).timestamp())
        monthly = sch.Budgets(requests=1, reset=sch.RESET_MONTHLY, timezone='UTC')
        self.assertEqual(sch.reset_key(monthly, now.timestamp()), 'monthly:2026-09')
        self.assertEqual(sch.next_reset_at(monthly, now.timestamp()),
                         at(UTC, 2026, 10, 1, 0, 0).timestamp())
        forever = sch.Budgets(requests=1, reset=sch.RESET_NONE)
        self.assertEqual(sch.reset_key(forever, now.timestamp()), 'none')
        self.assertIsNone(sch.next_reset_at(forever, now.timestamp()))

    def test_window_instant_is_unaffected_by_a_30_minute_utc_step_back(self):
        """A UTC-based scheduler would drift; wall-clock windows must not."""
        window = sch.Window.parse('09:00-18:00')
        before = window.intervals_on(BERLIN, datetime(2026, 10, 24).date())[0]
        after = window.intervals_on(BERLIN, datetime(2026, 10, 26).date())[0]
        self.assertEqual(before[0].astimezone(BERLIN).hour, after[0].astimezone(BERLIN).hour)
        self.assertEqual(before[0].astimezone(BERLIN).hour, 9)


if __name__ == '__main__':
    unittest.main()
