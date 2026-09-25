import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from proxy_workbench import core

NOW = 1_700_000_000.0
SCOPE = core.Scope(collection_id='public', profile_id='p1', profile_revision=3)
ACCESS = core.Access(access_id='acc-1', access_revision=2)
POLICY = core.Policy(max_age_seconds=600.0, min_success=0.5)


def row(index, checked_at, max_age=600.0, **extra):
    data = {
        'proxy': f'http://11.0.0.{index}:80',
        'endpoint_id': f'ep-{index}',
        'collection_id': 'public',
        'profile_id': 'p1',
        'profile_revision': 3,
        'network_id': 'default',
        'access_id': 'acc-1',
        'access_revision': 2,
        'checked_at': checked_at,
        'valid_until': checked_at + max_age,
        'min_target_reliability': 1.0,
        'protocol': 'http',
    }
    data.update(extra)
    return data


class ClockRollbackTests(unittest.TestCase):
    def setUp(self):
        self.engine = core.AdmissionEngine(POLICY)

    def test_clock_moving_backwards_is_reported_and_remembered(self):
        fresh = row(1, NOW - 10)
        self.assertTrue(self.engine.admit(fresh, SCOPE, ACCESS, NOW).admitted)
        back = self.engine.admit(fresh, SCOPE, ACCESS, NOW - 3600)
        self.assertEqual(back.reason_code, 'E_TIME_CLOCK_ROLLBACK')
        self.assertEqual(back.time_state, core.CLOCK_ROLLBACK)
        self.assertTrue(self.engine.rollback_pending('ep-1'))

    def test_rollback_is_reported_instead_of_a_phantom_future(self):
        fresh = row(1, NOW - 10)
        self.engine.admit(fresh, SCOPE, ACCESS, NOW)
        back = self.engine.admit(fresh, SCOPE, ACCESS, NOW - 3600)
        self.assertEqual(back.reason_code, 'E_TIME_CLOCK_ROLLBACK')
        self.assertNotEqual(back.reason_code, 'E_TIME_FUTURE')

    def test_pending_rollback_survives_the_clock_being_fixed(self):
        fresh = row(1, NOW - 10)
        self.engine.admit(fresh, SCOPE, ACCESS, NOW)
        self.engine.admit(fresh, SCOPE, ACCESS, NOW - 3600)
        restored = self.engine.admit(fresh, SCOPE, ACCESS, NOW + 60)
        self.assertEqual(restored.reason_code, 'E_TIME_CLOCK_ROLLBACK')
        self.assertTrue(self.engine.rollback_pending('ep-1'))

    def test_a_newer_measurement_clears_the_pending_rollback(self):
        fresh = row(1, NOW - 10)
        self.engine.admit(fresh, SCOPE, ACCESS, NOW)
        self.engine.admit(fresh, SCOPE, ACCESS, NOW - 3600)
        remeasured = row(1, NOW + 30)
        result = self.engine.admit(remeasured, SCOPE, ACCESS, NOW + 60)
        self.assertTrue(result.admitted)
        self.assertFalse(self.engine.rollback_pending('ep-1'))

    def test_a_measurement_from_before_the_rollback_does_not_clear_it(self):
        fresh = row(1, NOW - 10)
        self.engine.admit(fresh, SCOPE, ACCESS, NOW)
        self.engine.admit(fresh, SCOPE, ACCESS, NOW - 3600)
        stale = row(1, NOW - 400)
        result = self.engine.admit(stale, SCOPE, ACCESS, NOW + 60)
        self.assertEqual(result.reason_code, 'E_TIME_CLOCK_ROLLBACK')

    def test_rollback_is_tracked_per_endpoint(self):
        first, second = row(1, NOW - 10), row(2, NOW - 10)
        self.engine.admit(first, SCOPE, ACCESS, NOW)
        self.engine.admit(first, SCOPE, ACCESS, NOW - 3600)
        self.assertTrue(self.engine.rollback_pending('ep-1'))
        self.assertFalse(self.engine.rollback_pending('ep-2'))
        self.assertTrue(self.engine.admit(second, SCOPE, ACCESS, NOW + 60).admitted)
        self.assertFalse(self.engine.rollback_pending('ep-2'))

    def test_forward_time_never_reports_a_rollback(self):
        fresh = row(1, NOW - 10)
        for now in (NOW, NOW + 100, NOW + 10_000):
            with self.subTest(now=now):
                result = self.engine.admit(fresh, SCOPE, ACCESS, now)
                self.assertNotEqual(result.reason_code, 'E_TIME_CLOCK_ROLLBACK')
                self.assertFalse(self.engine.rollback_pending('ep-1'))

    def test_state_survives_a_restart_of_the_engine(self):
        fresh = row(1, NOW - 10)
        self.engine.admit(fresh, SCOPE, ACCESS, NOW)
        self.engine.admit(fresh, SCOPE, ACCESS, NOW - 3600)
        restored = core.AdmissionEngine(POLICY, self.engine.state_dict())
        self.assertTrue(restored.rollback_pending('ep-1'))
        self.assertEqual(restored.admit(fresh, SCOPE, ACCESS, NOW).reason_code,
                         'E_TIME_CLOCK_ROLLBACK')
        self.assertTrue(restored.admit(row(1, NOW + 30), SCOPE, ACCESS, NOW + 60).admitted)

    def test_stateless_admission_has_no_rollback_to_remember(self):
        fresh = row(1, NOW - 10)
        first = core.admit(fresh, SCOPE, ACCESS, POLICY, NOW)
        second = core.admit(fresh, SCOPE, ACCESS, POLICY, NOW - 3600)
        self.assertTrue(first.admitted)
        self.assertEqual(second.reason_code, 'E_TIME_FUTURE')

    def test_selection_through_the_engine_reports_the_rollback(self):
        rows = [row(1, NOW - 10), row(2, NOW - 20)]
        first = self.engine.select(rows, SCOPE, ACCESS, NOW)
        self.assertEqual(len(first.admitted), 2)
        after = self.engine.select(rows, SCOPE, ACCESS, NOW - 3600)
        self.assertEqual(after.admitted, ())
        self.assertEqual(after.counts, {'E_TIME_CLOCK_ROLLBACK': 2})
        self.assertEqual((after.state, after.state_detail), ('stale', 'all_untrusted'))


if __name__ == '__main__':
    unittest.main()
