"""Defect 23: the quick test is diagnostic, and it is signed with its volume.

It must not bypass the shared policy, must not run without a deadline, must
say what it did and did not measure, and must not pretend to have refreshed
the stored row.
"""
import asyncio
import json
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests import web_support as ws
from proxy_workbench import gui
from proxy_workbench import proxytool as core


def fake_row(proxy, *, ok=True, latency=90.0, samples=None, error=None):
    return {
        'proxy': proxy,
        'latency_ms': latency,
        'jitter_ms': 4.0,
        'score': 80.0,
        'reliability': 1.0 if ok else 0.0,
        'min_target_reliability': 1.0 if ok else 0.0,
        'successes': 2 if ok else 0,
        'requests': 2,
        'history': {'checks': 1, 'passes': 1 if ok else 0},
        'samples': samples if samples is not None else [
            {'target': 0, 'attempt': 1, 'ok': ok, 'status': 200 if ok else 403, 'elapsed_ms': latency},
            {'target': 1, 'attempt': 1, 'ok': ok, 'status': 200 if ok else 403, 'elapsed_ms': latency},
        ],
        **({'error': error} if error else {}),
    }


class QuickTestTests(unittest.TestCase):
    def setUp(self):
        self.now = time.time()
        self.proxy = 'http://11.0.0.5:8080'
        self.home = ws.build_data([ws.measurement(self.proxy, age=60, now=self.now)], now=self.now)
        self.fixture = ws.ServerFixture(self.home)
        self.app = self.fixture.app

    def tearDown(self):
        self.fixture.close()

    def test_the_answer_carries_the_real_volume_of_the_click(self):
        with mock.patch.object(core, 'check_proxy', new=mock.AsyncMock(return_value=fake_row(self.proxy))):
            answer = self.app.test_proxy({'proxy': self.proxy, 'min_success': 0.5})
        scope = answer['scope']
        self.assertEqual(scope['kind'], 'quick_diagnostic')
        self.assertEqual(scope['targets'], 2)
        self.assertEqual(scope['attempts'], 3)
        self.assertEqual(scope['planned_requests'], 6)
        self.assertEqual(scope['whole_deadline_s'], gui.QUICK_TEST_DEADLINE_S)
        self.assertLessEqual(scope['request_timeout_s'], gui.QUICK_TEST_TIMEOUT_CAP_S)
        self.assertLessEqual(scope['max_request_bytes'], gui.QUICK_TEST_MAX_BYTES)
        self.assertEqual(scope['skipped'], ['reputation_pipeline', 'anonymity_judge', 'bandwidth'])
        self.assertEqual(scope['recheck_action'], 'recheck')
        self.assertTrue(scope['recheck_note'])
        self.assertEqual(scope['requests_made'], 2)
        self.assertIs(scope['stored'], False)

    def test_the_test_does_not_store_an_observation(self):
        with mock.patch.object(core, 'check_proxy', new=mock.AsyncMock(return_value=fake_row(self.proxy))):
            self.app.test_proxy({'proxy': self.proxy})
        conn = core.open_db(self.home / 'proxies.sqlite3')
        try:
            payload = conn.execute('SELECT payload FROM results WHERE proxy=?', (self.proxy,)).fetchone()[0]
        finally:
            conn.close()
        before = json.loads(payload)
        self.assertEqual(before['history']['checks'], 2)
        self.assertEqual(before['checked_at'], self.now - 60)

    def test_the_verdict_uses_the_shared_policy_instead_of_a_lenient_one(self):
        # The profile requires elite anonymity; the quick test does not run the
        # judge, so this axis must be unknown, never a pass.
        conn = core.open_db(self.home / 'proxies.sqlite3')
        config = json.loads(conn.execute('SELECT config FROM profiles WHERE id=?',
                                         (ws.profile_id(),)).fetchone()[0])
        conn.close()
        config['anonymity'] = {'judge_url': 'http://judge.example/'}
        config['min_anonymity'] = 'elite'
        conn = core.open_db(self.home / 'proxies.sqlite3')
        with conn:
            conn.execute('UPDATE profiles SET config=? WHERE id=?', (json.dumps(config), ws.profile_id()))
        conn.close()
        with mock.patch.object(core, 'check_proxy', new=mock.AsyncMock(return_value=fake_row(self.proxy))):
            answer = self.app.test_proxy({'proxy': self.proxy})
        self.assertFalse(answer['ok'])
        self.assertEqual(answer['admission'], 'E_STATE_ANONYMITY')
        self.assertFalse(answer['anonymity_evaluated'])

    def test_a_denied_address_is_refused_before_any_request(self):
        (self.home / 'denylist.txt').write_text(self.proxy + '\n', encoding='utf-8')
        checker = mock.AsyncMock(return_value=fake_row(self.proxy))
        with mock.patch.object(core, 'check_proxy', new=checker):
            answer = self.app.test_proxy({'proxy': self.proxy})
        checker.assert_not_called()
        self.assertFalse(answer['ok'])
        self.assertEqual(answer['admission'], 'E_SCOPE_DENYLIST')

    def test_the_whole_call_has_a_deadline(self):
        async def slow(proxy, config, rate):
            await asyncio.sleep(30)
            return fake_row(proxy)
        with mock.patch.object(gui, 'QUICK_TEST_DEADLINE_S', 0.4), \
                mock.patch.object(core, 'check_proxy', new=slow):
            started = time.monotonic()
            answer = self.app.test_proxy({'proxy': self.proxy})
            elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5, 'the quick test ran without a whole-call deadline')
        self.assertTrue(answer['timed_out'])
        self.assertEqual(answer['error'], 'E_LIMIT_BUDGET')
        self.assertEqual(answer['scope']['whole_deadline_s'], 0.4)

    def test_a_failed_measurement_reports_the_code_and_never_a_pass(self):
        with mock.patch.object(core, 'check_proxy',
                               new=mock.AsyncMock(return_value=fake_row(self.proxy, ok=False, error='HTTP_4XX'))):
            answer = self.app.test_proxy({'proxy': self.proxy})
        self.assertFalse(answer['ok'])
        self.assertEqual(answer['error'], 'HTTP_4XX')
        self.assertEqual(answer['admission'], 'E_STATE_MEASUREMENT_FAILED')

    def test_the_clicked_threshold_is_the_one_that_decides(self):
        with mock.patch.object(core, 'check_proxy', new=mock.AsyncMock(return_value=fake_row(self.proxy))):
            passing = self.app.test_proxy({'proxy': self.proxy, 'min_success': 0.5})
        self.assertTrue(passing['ok'])
        self.assertEqual(passing['scope']['policy']['min_success'], 0.5)
        with mock.patch.object(core, 'check_proxy', new=mock.AsyncMock(return_value=fake_row(self.proxy, ok=False))):
            refused = self.app.test_proxy({'proxy': self.proxy, 'min_success': 0.5})
        self.assertFalse(refused['ok'])
        self.assertEqual(refused['admission'], 'E_STATE_MEASUREMENT_FAILED')

    def test_an_address_with_credentials_is_refused(self):
        with self.assertRaises(ValueError):
            self.app.test_proxy({'proxy': 'http://user:pass@11.0.0.5:8080'})

    def test_the_quick_test_works_while_a_scan_holds_the_data_lock(self):
        from proxy_workbench.maintenance import exclusive_lock
        with exclusive_lock(self.home / 'workbench.lock'):
            with mock.patch.object(core, 'check_proxy', new=mock.AsyncMock(return_value=fake_row(self.proxy))):
                answer = self.app.test_proxy({'proxy': self.proxy})
        self.assertTrue(answer['ok'])


if __name__ == '__main__':
    unittest.main()
