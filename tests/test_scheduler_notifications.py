"""State-change notifications: dedup, hysteresis and who may receive them (F15)."""
from pathlib import Path
import json
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import scheduler as sch
from proxy_workbench import i18n

CANARY = 'CANARY-webhook-secret-2f7c1a9e'


class FakeChannel:
    """A channel that keeps what it was asked to send, and the secret its owner holds."""

    def __init__(self, name, secret=None):
        self.name = name
        self.secret = secret
        self.sent = []

    def send(self, notification):
        self.sent.append(notification)


class Clock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make_watcher(**kwargs):
    policy = sch.NotifyPolicy(**kwargs)
    return sch.StateWatcher('pool:main', policy), policy


class DeduplicationTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.channel = FakeChannel('in_app')
        self.notifier = sch.Notifier(sch.Dispatcher(sch.NotificationConfig(in_app=True),
                                                    {'in_app': self.channel}), now=self.clock)

    def test_ten_identical_degraded_observations_give_one_notification(self):
        seen, watcher = make_watcher(enter_below=5, exit_above=6)
        for _ in range(10):
            change = seen.observe(3, self.clock.now)
            if change is not None:
                self.notifier.for_change(change, watcher)
        self.assertEqual(seen.observations, 10)
        self.assertEqual(seen.episodes, 1)
        self.assertEqual([item.code for item in self.channel.sent], [sch.NOTIFY_BELOW_MINIMUM])

    def test_recovery_gives_a_second_notification(self):
        seen, policy = make_watcher(enter_below=5, exit_above=6)
        for _ in range(10):
            change = seen.observe(3, self.clock.now)
            if change is not None:
                self.notifier.for_change(change, policy)
        change = seen.observe(6, self.clock.now)
        self.assertIsNotNone(change)
        self.notifier.for_change(change, policy)
        self.assertEqual([item.code for item in self.channel.sent],
                         [sch.NOTIFY_BELOW_MINIMUM, sch.NOTIFY_RECOVERED])
        self.assertEqual(seen.state, sch.STATE_OK)

    def test_recovery_needs_the_hysteresis_threshold_not_just_better_news(self):
        seen, _ = make_watcher(enter_below=5, exit_above=8)
        self.assertIsNotNone(seen.observe(3, 0.0))
        for value in (4, 5, 7):                 # inside the dead band: still degraded, no new event
            self.assertIsNone(seen.observe(value, 1.0), value)
        self.assertEqual(seen.state, sch.STATE_ALERT)
        self.assertIsNotNone(seen.observe(8, 2.0))

    def test_a_single_noisy_sample_does_not_alert(self):
        seen, _ = make_watcher(enter_below=5, min_events=3)
        self.assertIsNone(seen.observe(1, 0.0))
        self.assertIsNone(seen.observe(9, 1.0))    # confirmations do not accumulate across good samples
        self.assertIsNone(seen.observe(1, 2.0))
        self.assertIsNone(seen.observe(1, 3.0))
        self.assertIsNotNone(seen.observe(1, 4.0), 'the third consecutive bad sample confirms')

    def test_the_same_event_is_not_resent_inside_the_repeat_window(self):
        seen, policy = make_watcher(enter_below=5, repeat_window_s=1800.0)
        first = seen.observe(1, self.clock.now)
        self.notifier.for_change(first, policy)
        self.clock.advance(60)
        seen.state = sch.STATE_OK                     # a new episode after a recovery
        second = seen.observe(1, self.clock.now)
        self.assertIsNotNone(second)
        report = self.notifier.for_change(second, policy)
        self.assertEqual(report.deliveries, ())
        self.assertEqual(len(self.channel.sent), 1)
        self.assertEqual(self.notifier.suppressed_count(), 1)
        self.clock.advance(3600)
        self.notifier.for_change(second, policy)
        self.assertEqual(len(self.channel.sent), 2)

    def test_unknown_value_is_only_an_alert_when_asked_for(self):
        quiet, _ = make_watcher(enter_below=5)
        self.assertIsNone(quiet.observe(None, 0.0))
        loud, _ = make_watcher(enter_below=5, alert_on_unknown=True)
        self.assertIsNotNone(loud.observe(None, 0.0))

    def test_policy_validation(self):
        with self.assertRaises(sch.ScheduleError):
            sch.NotifyPolicy(min_events=0)
        with self.assertRaises(sch.ScheduleError):
            sch.NotifyPolicy(enter_below=5, exit_above=2)
        with self.assertRaises(sch.ScheduleError):
            sch.NotifyPolicy(repeat_window_s=-1)


class DispatchTests(unittest.TestCase):
    note = sch.Notification(subject='pool:main', code=sch.NOTIFY_BELOW_MINIMUM, at=10.0,
                            severity=sch.SEVERITY_WARNING, data=(('value', 3),))

    def test_nothing_goes_out_until_the_user_selects_a_configuration(self):
        channels = {name: FakeChannel(name) for name in
                    (sch.CHANNEL_IN_APP, sch.CHANNEL_OS, sch.CHANNEL_EMAIL, sch.CHANNEL_WEBHOOK)}
        dispatcher = sch.Dispatcher(sch.NotificationConfig(in_app=False), channels)
        report = dispatcher.dispatch(self.note)
        self.assertEqual(report.deliveries, ())
        reasons = dict(report.skipped)
        self.assertEqual(reasons[sch.CHANNEL_EMAIL], sch.SKIP_NO_TARGET)
        self.assertEqual(reasons[sch.CHANNEL_WEBHOOK], sch.SKIP_NO_TARGET)
        self.assertEqual(reasons[sch.CHANNEL_IN_APP], sch.SKIP_NO_CHANNEL)
        for channel in channels.values():
            self.assertEqual(channel.sent, [], 'nothing was handed to any channel')

    def test_a_target_that_is_not_enabled_or_not_confirmed_stays_silent(self):
        email = FakeChannel('email')
        channels = {'email': email}
        for target, expected in ((sch.Target(label='ops'), sch.SKIP_TARGET_OFF),
                                 (sch.Target(label='ops', enabled=True), sch.SKIP_NOT_CONFIRMED)):
            dispatcher = sch.Dispatcher(sch.NotificationConfig(in_app=False, email=target), channels)
            report = dispatcher.dispatch(self.note)
            self.assertEqual(report.deliveries, ())
            self.assertEqual(dict(report.skipped)[sch.CHANNEL_EMAIL], expected)
            self.assertEqual(email.sent, [])

    def test_a_confirmed_target_receives_the_notification(self):
        email = FakeChannel('email', secret=CANARY)
        config = sch.NotificationConfig(in_app=False, email=sch.Target(label='ops', enabled=True,
                                                                      user_confirmed=True))
        report = sch.Dispatcher(config, {'email': email}).dispatch(self.note)
        self.assertEqual([(item.channel, item.target_ref) for item in report.deliveries],
                         [('email', 'ops')])
        self.assertEqual(len(email.sent), 1)

    def test_a_channel_without_an_implementation_is_reported_not_assumed(self):
        config = sch.NotificationConfig(in_app=True, os_notifications=True,
                                        webhook=sch.Target(label='ops', enabled=True,
                                                           user_confirmed=True))
        report = sch.Dispatcher(config, {}).dispatch(self.note)
        self.assertEqual(report.deliveries, ())
        self.assertEqual(dict(report.skipped)[sch.CHANNEL_WEBHOOK], sch.SKIP_NO_IMPLEMENTATION)
        self.assertEqual(dict(report.skipped)[sch.CHANNEL_IN_APP], sch.SKIP_NO_IMPLEMENTATION)

    def test_nothing_the_module_stores_contains_a_secret(self):
        secret_channel = FakeChannel('webhook', secret=CANARY)
        config = sch.NotificationConfig(in_app=True, webhook=sch.Target(label='hook-1', enabled=True,
                                                                        user_confirmed=True))
        spec = sch.ScheduleSpec(id='pool-main', interval_minutes=30,
                                notifications=config, notify=sch.NotifyPolicy(enter_below=5))
        report = sch.Dispatcher(config, {'webhook': secret_channel, 'in_app': FakeChannel('in_app')}
                                ).dispatch(self.note)
        blobs = [json.dumps(spec.to_dict(), sort_keys=True),
                 json.dumps([item.to_dict() for item in report.deliveries]),
                 json.dumps(self.note.to_dict()),
                 json.dumps(secret_channel.sent[0].to_dict())]
        for blob in blobs:
            self.assertNotIn(CANARY, blob)
            json.loads(blob)                      # and every blob is valid JSON

    def test_only_whitelisted_text_leaves_the_module(self):
        config = sch.NotificationConfig(in_app=True)
        channel = FakeChannel('in_app')
        sch.Dispatcher(config, {'in_app': channel}).dispatch(self.note)
        body = channel.sent[0].text()
        self.assertIn('pool:main', body)
        self.assertIn('value=3', body)
        self.assertEqual(channel.sent[0].get('value'), 3)
        self.assertNotIn(CANARY, body)


class SchedulerNotificationTests(unittest.TestCase):
    def make(self, **kwargs):
        self.clock = Clock(1_780_000_000.0)
        self.channel = FakeChannel('in_app')
        self.notifier = sch.Notifier(sch.Dispatcher(sch.NotificationConfig(in_app=True),
                                                    {'in_app': self.channel}), now=self.clock)
        self.scheduler = sch.Scheduler(store=sch.InMemoryScheduleStore(), clock=self.clock,
                                       notifier=self.notifier, power_reader=sch.PowerSignal.unknown)
        self.scheduler.add(sch.ScheduleSpec(id='pool-main', interval_minutes=30, timezone='UTC', **kwargs))
        return self.scheduler

    def test_pause_and_resume_are_reported_once(self):
        self.make()
        self.scheduler.pause('pool-main')
        self.scheduler.pause('pool-main')
        self.assertEqual([item.code for item in self.channel.sent], [sch.NOTIFY_PAUSED])
        self.scheduler.resume('pool-main')
        self.scheduler.resume('pool-main')
        self.assertEqual([item.code for item in self.channel.sent],
                         [sch.NOTIFY_PAUSED, sch.NOTIFY_RESUMED])

    def test_ticking_repeatedly_does_not_announce_anything_by_itself(self):
        self.make()
        self.assertEqual(self.scheduler.tick().decisions[0].reason, sch.REASON_NOT_DUE)
        self.clock.advance(1800)
        first = self.scheduler.tick()
        self.assertEqual(first.decisions[0].action, 'run')
        self.assertEqual(len(first.run_requests), 1)
        for _ in range(10):
            again = self.scheduler.tick()
        self.assertEqual(again.decisions[0].reason, sch.REASON_NOT_DUE,
                         'a repeat tick at the same instant is not a new run')
        self.assertEqual(again.run_requests, ())
        self.assertEqual(self.channel.sent, [], 'a healthy schedule says nothing')

    def test_the_pool_layer_feeds_health_through_the_same_watcher(self):
        self.make()
        seen, policy = make_watcher(enter_below=5, exit_above=6)
        for value in (3,) * 10 + (6,):
            change = seen.observe(value, self.clock.now)
            if change is not None:
                self.notifier.for_change(change, policy)
        self.assertEqual([item.code for item in self.channel.sent],
                         [sch.NOTIFY_BELOW_MINIMUM, sch.NOTIFY_RECOVERED])


class MessageTests(unittest.TestCase):
    def test_codes_are_translated_through_i18n_and_keep_their_machine_name(self):
        ru, en = sch.describe('E_LIMIT_BUDGET')
        self.assertIn('{}', ru)
        self.assertIn('{}', en)
        original = i18n.LANG
        try:
            i18n.LANG = 'ru'
            self.assertTrue(sch.message('E_LIMIT_BUDGET', 'bytes', 'bytes_exhausted').startswith('Бюджет'))
            i18n.LANG = 'en'
            self.assertTrue(sch.message('E_LIMIT_BUDGET', 'bytes', 'bytes_exhausted').startswith('Budget'))
        finally:
            i18n.LANG = original
        self.assertEqual(sch.describe('E_UNKNOWN_CODE'), ('E_UNKNOWN_CODE', 'E_UNKNOWN_CODE'))

    def test_every_notification_code_has_text(self):
        for code in sch.NOTIFICATION_CODES:
            ru, en = sch.describe(code)
            self.assertNotEqual(ru, code, code)
            self.assertNotEqual(en, code, code)
            note = sch.Notification(subject='x', code=code, at=0.0, data=(('reason', 'why'),))
            self.assertIn('x', note.text())
            self.assertNotIn('{}', note.text())

    def test_decision_reasons_are_a_closed_set(self):
        for name in (sch.REASON_DUE, sch.REASON_PAUSED_QUIET, sch.REASON_WINDOW_CLOSED,
                     sch.REASON_COALESCED, sch.REASON_COALESCED_SLEEP, sch.REASON_PAUSED_BUDGET):
            self.assertIn(name, sch.DECISION_REASONS)


if __name__ == '__main__':
    unittest.main()
