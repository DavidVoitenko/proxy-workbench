"""Budgets, traffic classes and the metered/battery policy (F15)."""
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import scheduler as sch

UTC = timezone.utc
DAY = 86_400


def at(*args):
    return datetime(*args, tzinfo=UTC).timestamp()


class BudgetValidationTests(unittest.TestCase):
    def test_limits_must_be_numbers_and_reserves_non_negative(self):
        for bad in ({'requests': -1}, {'bytes': 'many'}, {'seconds': -0.5}, {'concurrency': 0},
                    {'inflight_reserve_bytes': -1}, {'reset': 'weekly'}):
            with self.assertRaises(sch.ScheduleError) as caught:
                sch.Budgets(**bad)
            self.assertEqual(caught.exception.code, 'E_VALIDATION_FIELD')
        self.assertFalse(sch.Budgets().limited)
        self.assertTrue(sch.Budgets(requests=1).limited)
        self.assertIsNone(sch.Budgets.from_dict(None).requests)
        self.assertEqual(sch.Budgets.from_dict({'requests': 5}).requests, 5)
        with self.assertRaises(sch.ScheduleError):
            sch.Budgets.from_dict([1, 2, 3])


class LimitTests(unittest.TestCase):
    def test_byte_cap_stops_the_next_reservation(self):
        ledger = sch.BudgetLedger(sch.Budgets(bytes=1000, inflight_reserve_bytes=0), now=0.0)
        first = ledger.try_reserve(sch.PROBE, requests=1, bytes=1000)
        self.assertTrue(first.ok)
        ledger.commit(first)
        second = ledger.try_reserve(sch.PROBE, requests=1, bytes=1)
        self.assertFalse(second.ok)
        self.assertEqual(second.denial.code, 'E_LIMIT_BUDGET')
        self.assertEqual(second.denial.reason, 'bytes_exhausted')
        self.assertEqual(second.denial.axis, 'bytes')
        self.assertEqual(second.denial.remaining, 0.0)
        self.assertIn('bytes', second.denial.text())

    def test_work_already_started_may_land_inside_the_inflight_reserve(self):
        budgets = sch.Budgets(bytes=1000, inflight_reserve_bytes=100)
        ledger = sch.BudgetLedger(budgets, now=0.0)
        landed = ledger.try_reserve(sch.PROBE, requests=1, bytes=1000)
        overshoot = ledger.try_reserve(sch.SPEEDTEST, requests=1, bytes=100)
        self.assertTrue(overshoot.ok, 'the reserve exists for work that was already running')
        refused = ledger.try_reserve(sch.SPEEDTEST, requests=1, bytes=101)
        self.assertFalse(refused.ok, 'past the reserve nothing more starts')
        self.assertEqual(ledger.snapshot().bytes.in_flight, 100.0)
        ledger.commit(landed)
        ledger.commit(overshoot)
        snapshot = ledger.snapshot()
        self.assertTrue(snapshot.bytes.over_limit)
        self.assertTrue(snapshot.bytes.exhausted)
        self.assertEqual(snapshot.bytes.used, 1100)
        self.assertIn('bytes', snapshot.exhausted)

    def test_inflight_allowance_is_finite_and_reported(self):
        budgets = sch.Budgets(requests=3, bytes=50, inflight_reserve_requests=2,
                              inflight_reserve_bytes=10)
        ledger = sch.BudgetLedger(budgets, now=0.0)
        full = ledger.try_reserve(sch.PROBE, requests=2, bytes=50)
        self.assertTrue(full.ok)
        view = ledger.snapshot().bytes
        self.assertEqual(view.limit, 50)
        self.assertEqual(view.allowance, 10.0)
        self.assertEqual(view.remaining, 0.0)
        self.assertEqual(view.in_flight, 0.0, 'nothing is running past the limit yet')
        overshoot = ledger.try_reserve(sch.PROBE, requests=1, bytes=10)
        self.assertTrue(overshoot.ok)
        self.assertEqual(ledger.snapshot().bytes.in_flight, 10.0, 'the overrun is bounded by the reserve')
        denied = ledger.try_reserve(sch.PROBE, requests=1, bytes=1)
        self.assertEqual(denied.denial.in_flight, 10.0)
        self.assertEqual(denied.denial.reserved, 60)
        self.assertEqual(denied.denial.remaining, 0.0)
        for item in (full, overshoot):
            ledger.release(item)
        self.assertTrue(ledger.try_reserve(sch.PROBE, requests=1, bytes=50).ok)

    def test_time_and_concurrency_are_separate_axes(self):
        ledger = sch.BudgetLedger(sch.Budgets(seconds=10.0, concurrency=2, inflight_reserve_seconds=0.0),
                                  now=0.0)
        first = ledger.try_reserve(sch.JUDGE, requests=1, seconds=6.0)
        second = ledger.try_reserve(sch.JUDGE, requests=1, seconds=6.0)
        self.assertTrue(first.ok)
        self.assertFalse(second.ok)
        self.assertEqual(second.denial.reason, 'seconds_exhausted')
        third = ledger.try_reserve(sch.JUDGE, requests=1, seconds=1.0)
        self.assertTrue(third.ok)
        self.assertFalse(ledger.try_reserve(sch.PROBE, requests=1).ok, 'concurrency is full')
        self.assertEqual(ledger.try_reserve(sch.PROBE, requests=1).denial.reason, 'concurrency_exhausted')
        ledger.release(third)
        self.assertTrue(ledger.try_reserve(sch.PROBE, requests=1).ok)

    def test_unlimited_axis_reports_no_limit(self):
        ledger = sch.BudgetLedger(sch.Budgets(requests=5), now=0.0)
        view = ledger.snapshot().bytes
        self.assertIsNone(view.limit)
        self.assertFalse(view.exhausted)
        self.assertIsNone(view.to_dict()['remaining'])
        self.assertTrue(ledger.try_reserve(sch.PROBE, requests=1, bytes=10 ** 9).ok)

    def test_settled_reservation_cannot_be_spent_twice(self):
        ledger = sch.BudgetLedger(sch.Budgets(requests=10), now=0.0)
        reservation = ledger.try_reserve(sch.PROBE, requests=1)
        ledger.commit(reservation)
        with self.assertRaises(ValueError):
            ledger.commit(reservation)
        ledger.release(reservation)          # a second release is a no-op, not a corruption
        self.assertEqual(ledger.snapshot().requests.used, 1)


class TrafficClassTests(unittest.TestCase):
    """Probe, source, retry, judge and speedtest are ours; relay is the client's."""

    def setUp(self):
        self.ledger = sch.BudgetLedger(sch.Budgets(requests=4, bytes=1000, inflight_reserve_bytes=0),
                                       now=at(2026, 9, 28, 12, 0))

    def test_relay_traffic_never_consumes_a_workbench_budget(self):
        relay = self.ledger.try_reserve(sch.RELAY, requests=500, bytes=10 ** 7)
        self.assertTrue(relay.ok)
        snapshot = self.ledger.snapshot()
        self.assertEqual(snapshot.relay_bytes, 10 ** 7)
        self.assertEqual(snapshot.bytes.used, 0)
        self.assertEqual(snapshot.bytes.remaining, 1000)
        self.assertEqual(snapshot.requests.remaining, 4)
        self.assertTrue(self.ledger.try_reserve(sch.PROBE, requests=4, bytes=1000).ok)

    def test_workbench_traffic_never_appears_as_billable_traffic(self):
        spent = self.ledger.try_reserve(sch.SPEEDTEST, requests=1, bytes=900)
        self.ledger.commit(spent)
        snapshot = self.ledger.snapshot()
        self.assertEqual(snapshot.relay_bytes, 0)
        self.assertEqual(snapshot.relay_requests, 0)
        self.assertEqual(snapshot.bytes.used, 900)

    def test_every_workbench_class_shares_one_budget(self):
        for index, traffic_class in enumerate(sch.WORKBENCH_CLASSES):
            reservation = self.ledger.try_reserve(traffic_class, requests=1, bytes=1)
            self.assertTrue(reservation.ok, traffic_class)
            self.ledger.commit(reservation)
            self.assertEqual(self.ledger.snapshot().requests.used, index + 1)
        self.assertFalse(self.ledger.try_reserve(sch.PROBE, requests=1, bytes=1).ok)

    def test_metered_cheap_only_keeps_expensive_stages_out(self):
        ledger = sch.BudgetLedger(sch.Budgets(requests=50, bytes=10 ** 6), now=0.0,
                                   allowed_classes=sch.CHEAP_CLASSES)
        for traffic_class in sch.CHEAP_CLASSES:
            self.assertTrue(ledger.try_reserve(traffic_class, requests=1, bytes=10).ok)
        for traffic_class in (sch.JUDGE, sch.SPEEDTEST):
            refused = ledger.try_reserve(traffic_class, requests=1, bytes=10)
            self.assertFalse(refused.ok, traffic_class)
            self.assertEqual(refused.denial.reason, sch.REASON_STAGE_NOT_ALLOWED)
        self.assertTrue(ledger.try_reserve(sch.RELAY, requests=1, bytes=10).ok,
                        'relay is not a workbench stage')
        self.assertFalse(ledger.stage_allowed(sch.SPEEDTEST))
        self.assertEqual(ledger.snapshot().stages, sch.CHEAP_CLASSES)

    def test_unknown_traffic_class_is_refused(self):
        refused = self.ledger.try_reserve('torrent', requests=1)
        self.assertFalse(refused.ok)
        self.assertEqual(refused.denial.code, 'E_VALIDATION_FIELD')


class ResetTests(unittest.TestCase):
    def test_counters_restart_with_the_period_and_relay_survives(self):
        budgets = sch.Budgets(requests=2, reset=sch.RESET_HOURLY, timezone='UTC',
                              inflight_reserve_requests=0)
        ledger = sch.BudgetLedger(budgets, now=at(2026, 9, 28, 10, 10))
        relay = ledger.try_reserve(sch.RELAY, requests=9, bytes=900)
        spent = ledger.try_reserve(sch.PROBE, requests=2)
        ledger.commit(spent)
        self.assertFalse(ledger.try_reserve(sch.PROBE, requests=1).ok)
        moved = at(2026, 9, 28, 11, 5)
        self.assertTrue(ledger.maybe_reset(moved))
        ledger.commit(ledger.try_reserve(sch.PROBE, requests=1))
        snapshot = ledger.snapshot(moved)
        self.assertEqual(snapshot.relay_requests, 9)
        self.assertEqual(snapshot.requests.used, 1)
        self.assertFalse(ledger.maybe_reset(at(2026, 9, 28, 11, 30)))
        ledger.commit(relay, requests=9)

    def test_counters_survive_a_restart_through_the_snapshot(self):
        budgets = sch.Budgets(requests=4, bytes=100)
        counters = sch.BudgetCounters(requests=3, reset_key=sch.reset_key(budgets, at(2026, 9, 28, 12, 0)))
        ledger = sch.BudgetLedger(budgets, counters, now=at(2026, 9, 28, 12, 30))
        self.assertEqual(ledger.snapshot().requests.used, 3)
        self.assertTrue(ledger.try_reserve(sch.PROBE, requests=1, bytes=1).ok)
        self.assertEqual(ledger.snapshot().requests.remaining, 0.0)

    def test_a_new_period_resets_stale_counters_on_construction(self):
        budgets = sch.Budgets(requests=4, reset=sch.RESET_DAILY, timezone='Europe/Berlin')
        counters = sch.BudgetCounters(requests=3, bytes=90, reset_key='daily:2020-01-01')
        ledger = sch.BudgetLedger(budgets, counters, now=at(2026, 9, 28, 12, 0))
        self.assertEqual(ledger.snapshot().requests.used, 0)
        self.assertEqual(ledger.snapshot().bytes.used, 0)
        self.assertTrue(ledger.try_reserve(sch.PROBE, requests=4, bytes=100).ok)


class PowerPolicyTests(unittest.TestCase):
    def setUp(self):
        self.battery = sch.PowerSignal(on_battery=True, battery_percent=35, battery_supported=True,
                                       source='os')
        self.plugged = sch.PowerSignal(on_battery=False, battery_percent=90, battery_supported=True,
                                       source='os')
        self.metered = sch.PowerSignal(metered=True, metered_supported=True, source='os')
        self.cheap = sch.PowerSignal(metered=False, metered_supported=True, source='os')
        self.silent = sch.PowerSignal()

    def test_no_policy_means_no_restriction(self):
        decision = sch.power_decision(sch.PowerPolicy(), self.battery)
        self.assertFalse(decision.restricted)
        self.assertEqual(decision.stages, sch.WORKBENCH_CLASSES)

    def test_no_signal_means_no_restriction(self):
        for policy in (sch.PowerPolicy(on_battery=sch.POWER_PAUSE, metered=sch.POWER_PAUSE),
                       sch.PowerPolicy(on_battery=sch.POWER_BELOW_PERCENT, battery_min_percent=50)):
            decision = sch.power_decision(policy, self.silent)
            self.assertFalse(decision.restricted, 'F15 applies only where the OS has a signal')
            self.assertEqual(decision.reason, sch.POWER_SIGNAL_ABSENT)

    def test_battery_policy(self):
        self.assertTrue(sch.power_decision(sch.PowerPolicy(on_battery=sch.POWER_PAUSE),
                                           self.battery).restricted)
        self.assertFalse(sch.power_decision(sch.PowerPolicy(on_battery=sch.POWER_PAUSE),
                                            self.plugged).restricted)
        policy = sch.PowerPolicy(on_battery=sch.POWER_BELOW_PERCENT, battery_min_percent=50)
        self.assertTrue(sch.power_decision(policy, self.battery).restricted)
        self.assertFalse(sch.power_decision(policy, self.plugged).restricted)

    def test_metered_policy(self):
        self.assertTrue(sch.power_decision(sch.PowerPolicy(metered=sch.POWER_PAUSE), self.metered).restricted)
        self.assertFalse(sch.power_decision(sch.PowerPolicy(metered=sch.POWER_PAUSE), self.cheap).restricted)
        limited = sch.power_decision(sch.PowerPolicy(metered=sch.POWER_CHEAP_ONLY), self.metered)
        self.assertFalse(limited.restricted)
        self.assertEqual(limited.stages, sch.CHEAP_CLASSES)

    def test_unknown_metered_is_conservative_by_default_and_says_so(self):
        unknown = sch.PowerSignal(metered=None, metered_supported=True, source='os')
        decision = sch.power_decision(sch.PowerPolicy(metered=sch.POWER_CHEAP_ONLY), unknown)
        self.assertEqual(decision.stages, sch.CHEAP_CLASSES)
        self.assertEqual(decision.reason, sch.POWER_METERED_UNKNOWN)
        relaxed = sch.power_decision(sch.PowerPolicy(metered=sch.POWER_CHEAP_ONLY,
                                                      unknown_metered=sch.UNKNOWN_AS_UNMETERED), unknown)
        self.assertEqual(relaxed.stages, sch.WORKBENCH_CLASSES)
        self.assertFalse(relaxed.restricted)

    def test_policy_validation(self):
        for bad in ({'on_battery': 'explode'}, {'metered': 'sometimes'}, {'unknown_metered': 'maybe'},
                    {'battery_min_percent': 120}):
            with self.assertRaises(sch.ScheduleError):
                sch.PowerPolicy(**bad)


class SystemSignalTests(unittest.TestCase):
    def test_linux_sysfs_is_read_from_files_only(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for name, kind, extra in (('AC', 'Mains', {'online': '0'}), ('BAT0', 'Battery', {'capacity': '42'})):
                supply = root / name
                supply.mkdir()
                (supply / 'type').write_text(kind + '\n')
                for key, value in extra.items():
                    (supply / key).write_text(value + '\n')
            signal = sch.read_system_signal('linux', sysfs=root)
        self.assertTrue(signal.on_battery)
        self.assertEqual(signal.battery_percent, 42)
        self.assertTrue(signal.battery_supported)
        self.assertFalse(signal.metered_supported, 'no portable network-cost API on Linux')

    def test_missing_sysfs_means_no_signal(self):
        signal = sch.read_system_signal('linux', sysfs=Path('/nonexistent-power-supply'))
        self.assertEqual(signal, sch.PowerSignal())

    def test_macos_pmset_output_is_parsed(self):
        class Result:
            returncode = 0
            stdout = 'Now drawing from \'Battery Power\'\n -InternalBattery-0 42%; discharging\n'

        def runner(command, **kwargs):
            self.assertEqual(command, ['pmset', '-g', 'batt'])
            return Result()

        signal = sch.read_system_signal('darwin', runner=runner)
        self.assertTrue(signal.on_battery)
        self.assertEqual(signal.battery_percent, 42)
        self.assertEqual(signal.source, 'pmset')

    def test_a_broken_probe_returns_no_signal_instead_of_raising(self):
        def runner(*args, **kwargs):
            raise FileNotFoundError('pmset')

        self.assertEqual(sch.read_system_signal('darwin', runner=runner), sch.PowerSignal())
        with mock.patch.object(sch, '_windows_power', side_effect=OSError('power API unavailable')):
            self.assertEqual(sch.read_system_signal('win32'), sch.PowerSignal())


if __name__ == '__main__':
    unittest.main()
