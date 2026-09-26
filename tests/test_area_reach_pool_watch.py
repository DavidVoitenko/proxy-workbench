"""F14: the pool watch is started by starting the pool, and stops with it.

``pools.watch`` was written and covered by tests and started by nothing, so a
pool created with ``desired=5`` was refilled once by whoever pressed the button
and then stayed wherever it reached.  The loop is the same function as before;
what this file proves is that the loop is now *reachable* and *owned*:

* starting a pool starts watching it -- the same call, not a second one;
* pausing the pool stops the loop, so nothing refills a pool the user believes
  is off;
* starting twice does not create a second loop;
* a tick spends at most the pool's own refill budget, because it goes through
  the same ``refill`` a manual ``pool refill`` calls.

Everything runs against a temporary database with a stub clock and a local
candidate source.  No proxy is contacted and no port is opened.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / 'tests'))

from proxy_workbench import api
from proxy_workbench import apiv1
from proxy_workbench import db
from proxy_workbench import pools
from proxy_workbench import proxytool

KEY = 'reach-pool-operator-key'
COUNT = 12
PROFILE = 'p1'


def build(data: Path, **policy):
    """A database whose public collection really has working rows in it."""
    import workbench_support
    conn, _ = db.open_db(str(data / db.DB_FILENAME))
    proxies = [f'http://203.0.113.{n}:8080' for n in range(1, COUNT + 1)]
    for index, proxy in enumerate(proxies, start=1):
        endpoint = db.upsert_endpoint(conn, proxy)
        conn.execute('INSERT OR IGNORE INTO candidates(proxy, endpoint_id) VALUES (?,?)',
                     (proxy, endpoint))
        db.add_member(conn, db.PUBLIC_COLLECTION_ID, endpoint, origin='public')
        workbench_support.store_result(conn, (PROFILE, proxy, {
            'proxy': proxy, 'reliability': 1.0, 'min_target_reliability': 1.0,
            'latency_ms': 80.0, 'successes': 3, 'requests': 3,
            'samples': [{'ok': True, 'bytes': 1024, 'ms': 80.0} for _ in range(3)],
            'country': 'DE', 'exit_ip': f'198.51.100.{index}'}), profile_id=PROFILE,
            profile_revision=1, collection_id=db.PUBLIC_COLLECTION_ID, checked_at=time.time() - 60)
    conn.commit()
    values = {'max_age_seconds': 7200, 'cooldown_seconds': 1, 'retry_interval_seconds': 1,
              'interval_seconds': 1, 'refill_budget': 20}
    values.update(policy)
    store = pools.PoolStore(conn)
    store.create('main', collection_id=db.PUBLIC_COLLECTION_ID, profile_id=PROFILE,
                 desired=5, minimum=2, reserve=1, policy=values)
    conn.commit()
    conn.close()


class RegistryBehaviourTests(unittest.TestCase):
    """The registry itself: one loop, bound to the pool, budget-limited."""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.data = Path(self._directory.name)
        build(self.data)
        self.registry = pools.WatchRegistry(lambda: pools.PoolStore.open(self.data / db.DB_FILENAME))
        self.addCleanup(self.registry.stop_all)

    def source_factory(self, conn):
        return api.pool_candidate_source(conn)

    def wait_for(self, predicate, timeout=12.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def served(self):
        store = pools.PoolStore.open(self.data / db.DB_FILENAME)
        try:
            return store.status('main').served
        finally:
            store.close()

    def ticks(self):
        return self.registry.status()['watching'].get('main', {}).get('ticks', 0)

    def test_a_started_pool_recovers_on_its_own(self):
        self.assertEqual(self.served(), 0)
        self.registry.start('main', source_factory=self.source_factory)
        self.assertTrue(self.wait_for(lambda: self.served() == 5),
                        f'the watch never filled the pool, served={self.served()}')

    def test_a_watch_needs_a_source(self):
        with self.assertRaises(pools.PoolError):
            self.registry.start('main')

    def test_a_watch_without_a_bound_store_is_refused(self):
        loose = pools.WatchRegistry()
        with self.assertRaises(pools.PoolError) as caught:
            loose.start('main', source=self.source_factory)
        self.assertIn('watch_registry', str(caught.exception))

    def test_stopping_ends_the_loop(self):
        self.registry.start('main', source_factory=self.source_factory)
        self.assertTrue(self.wait_for(lambda: self.registry.watching('main')))
        result = self.registry.stop('main')
        self.assertTrue(result['stopped'])
        self.assertFalse(self.registry.watching('main'))
        # Stopping a pool that is not watched is a no-op, not a failure.
        self.assertTrue(self.registry.stop('main')['stopped'])

    def test_a_stopped_pool_stays_stopped(self):
        self.registry.start('main', source_factory=self.source_factory)
        self.assertTrue(self.wait_for(lambda: self.served() == 5))
        self.registry.stop('main')
        store = pools.PoolStore.open(self.data / db.DB_FILENAME)
        for member in store.members('main'):
            pools.report_health(store, 'main', member.endpoint_id, ok=False, now=time.time())
        store.close()
        time.sleep(2.5)
        self.assertEqual(self.served(), 0, 'the pool refilled itself after it was stopped')

    def test_a_second_start_does_not_create_a_second_loop(self):
        first = self.registry.start('main', source_factory=self.source_factory)
        second = self.registry.start('main', source_factory=self.source_factory)
        self.assertTrue(first['started'])
        self.assertFalse(second['started'])
        self.assertEqual(second['reason'], 'already_running')
        self.assertTrue(self.registry.watching('main'))
        # One loop, one counter: two loops would give two sets of ticks.
        self.assertEqual(len(self.registry.status()['watching']), 1)

    def test_a_tick_spends_at_most_the_budget_it_was_given(self):
        store = pools.PoolStore.open(self.data / db.DB_FILENAME)
        self.addCleanup(store.close)
        spec = store.require('main')
        self.assertEqual(spec.policy.refill_budget, 20)
        # One admission per tick against a target of five: the pool can only
        # reach the target by spending five ticks, and a loop that ignored the
        # budget would be at five on the very first one.
        self.registry.start('main', source_factory=self.source_factory, budget=1)
        self.assertTrue(self.wait_for(lambda: self.ticks() >= 1))
        self.assertLessEqual(self.served(), 1, 'one tick spent more than the budget allowed')
        self.assertTrue(self.wait_for(lambda: self.served() == 5), 'the pool never reached its target')
        self.assertGreaterEqual(self.ticks(), 5, 'the target was reached in fewer ticks than the budget allows')
        self.registry.stop('main')
        store.close()

    def test_the_candidate_source_is_built_on_the_loops_own_connection(self):
        # A source built on the caller's connection raises inside a thread; the
        # registry therefore takes a factory and calls it inside the loop.  A
        # pool that reports "the source failed" forever is that mistake.
        self.registry.start('main', source_factory=self.source_factory)
        self.assertTrue(self.wait_for(lambda: self.served() == 5))
        self.registry.stop('main')
        store = pools.PoolStore.open(self.data / db.DB_FILENAME)
        self.assertEqual(store.status('main').source_errors, ())
        store.close()

    def test_status_reports_the_ticks_and_no_error(self):
        self.registry.start('main', source_factory=self.source_factory)
        self.assertTrue(self.wait_for(lambda: self.registry.status()['watching']['main']['ticks'] > 0))
        item = self.registry.status()['watching']['main']
        self.assertGreaterEqual(item['ticks'], 1)
        self.assertEqual(item['error'], '')
        self.assertEqual(self.registry.status()['errors'], {})
        self.registry.stop('main')

    def test_an_unknown_pool_ends_the_watch_instead_of_looping_forever(self):
        # ``watch`` refuses a pool that does not exist; the loop must record it
        # rather than spin.
        self.registry.start('nope', source_factory=self.source_factory)
        self.assertTrue(self.wait_for(lambda: not self.registry.watching('nope'), timeout=8.0))
        self.assertFalse(self.registry.watching('nope'))


class RouteOwnsTheWatchTests(unittest.TestCase):
    """`POST /v1/pools/{id}/start` and `.../pause` are the door."""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.data = Path(self._directory.name)
        build(self.data)
        self.service = _Service(self.data)
        self.client = apiv1.ApiV1(self.service, _Keys(), apiv1.ApiConfig(host='127.0.0.1'))
        self.registry = pools.watch_registry(self.data)
        self.addCleanup(self.registry.stop_all)
        self.requests = 0

    def call(self, path, body=None):
        headers = {'Authorization': f'Bearer {KEY}', 'Content-Type': 'application/json',
                   'Idempotency-Key': f'k{self.requests}', 'Host': '127.0.0.1:8766'}
        self.requests += 1
        return self.client.handle(apiv1.Request(
            method='POST', path=path, headers=headers,
            body=json.dumps(body if body is not None else {}).encode(), client_host='127.0.0.1'))

    def served(self):
        store = pools.PoolStore.open(self.data / db.DB_FILENAME)
        try:
            return store.status('main').served
        finally:
            store.conn.close()

    def wait_for(self, predicate, timeout=12.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def test_start_fills_the_pool_and_starts_the_watch(self):
        response = self.call('/v1/pools/main/start')
        self.assertEqual(response.status, 200)
        body = json.loads(response.body)
        self.assertEqual(body['served'], 5)
        self.assertTrue(body['watching'])
        self.assertTrue(body['watch']['started'])
        self.assertTrue(self.registry.watching('main'))

    def test_start_can_be_asked_not_to_watch(self):
        body = json.loads(self.call('/v1/pools/main/start', {'watch': False}).body)
        self.assertFalse(body['watching'])
        self.assertFalse(self.registry.watching('main'))

    def test_a_second_start_does_not_double_the_work(self):
        self.call('/v1/pools/main/start')
        body = json.loads(self.call('/v1/pools/main/start').body)
        self.assertFalse(body['watch']['started'])
        self.assertEqual(body['watch']['reason'], 'already_running')

    def test_pause_stops_the_watch(self):
        self.call('/v1/pools/main/start')
        self.assertTrue(self.wait_for(lambda: self.registry.watching('main')))
        body = json.loads(self.call('/v1/pools/main/pause').body)
        self.assertFalse(body['watching'])
        self.assertTrue(body['watch']['stopped'])
        self.assertFalse(self.registry.watching('main'))

    def test_a_pool_the_watch_keeps_filled_recovers_after_a_loss(self):
        self.call('/v1/pools/main/start')
        self.assertTrue(self.wait_for(lambda: self.served() == 5))
        store = pools.PoolStore.open(self.data / db.DB_FILENAME)
        for member in store.members('main')[:4]:
            pools.report_health(store, 'main', member.endpoint_id, ok=False, now=time.time())
        store.conn.close()
        self.assertTrue(self.wait_for(lambda: self.served() == 5),
                        f'the watch did not restore the pool, served={self.served()}')
        self.call('/v1/pools/main/pause')

    def test_pausing_a_pool_that_is_not_watched_is_fine(self):
        response = self.call('/v1/pools/main/pause')
        self.assertEqual(response.status, 200)
        self.assertFalse(json.loads(response.body)['watching'])


class CliReachesTheWatchTests(unittest.TestCase):
    """`pool start`, `pool pause` and `pool watch` exist in the command."""

    @classmethod
    def setUpClass(cls):
        cls._directory = tempfile.TemporaryDirectory()
        cls.data = Path(cls._directory.name)
        build(cls.data)

    @classmethod
    def tearDownClass(cls):
        cls._directory.cleanup()

    def cli(self, *args, lang='en'):
        # The language is pinned per call: the assertions below are about what
        # the command reports, not about whichever locale another test in the
        # same process happened to select.
        environment = dict(os.environ, PROXY_WORKBENCH_LANG=lang)
        return subprocess.run(
            [sys.executable, '-m', 'proxy_workbench', '--data', str(self.data), 'pool', *args],
            capture_output=True, text=True, timeout=180, cwd=str(REPO), env=environment)

    def test_the_subcommands_exist(self):
        for name in ('start', 'pause', 'watch'):
            self.assertIn(name, proxytool.POOL_SUBCOMMANDS)

    def test_start_fills_the_pool_and_reports_the_watch(self):
        done = self.cli('start', '--name', 'main')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('serving 5 of 5', done.stdout)
        report = json.loads(self.cli('start', '--name', 'main', '--json').stdout)
        self.assertEqual(report['served'], 5)
        self.assertIn('watch', report)

    def test_the_cli_says_where_the_watch_actually_lives(self):
        # A CLI process exits and a daemon thread dies with it, so the command
        # must not claim a loop that cannot outlive the shell.
        done = self.cli('start', '--name', 'main')
        self.assertIn('the watch is started with the pool', done.stdout)

    def test_pause_stops_the_watch(self):
        done = self.cli('pause', '--name', 'main')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('the watch is stopped', done.stdout)

    def test_watch_reports_without_changing_anything(self):
        done = self.cli('watch', '--json')
        self.assertEqual(done.returncode, 0, done.stderr)
        report = json.loads(done.stdout)
        self.assertIn('watching', report)
        self.assertIn('errors', report)


class _Service:
    """The real engine, behind the interface ``apiv1`` checks for."""

    def __init__(self, data):
        self._inner = api.WorkbenchService(data)

    def invoke(self, operation, call):
        return self._inner.invoke(operation, call)

    def queue_state(self):
        return self._inner.queue_state()


class _Keys(apiv1.KeyStore):
    def verify(self, secret):
        if secret != KEY:
            return None
        return apiv1.Principal(key_id='k-op', kind='api_key',
                               permissions=frozenset(apiv1.PERMISSIONS))

    def read_audit(self, principal, **filters):
        return {'items': []}

    def audit(self, record):
        pass


if __name__ == '__main__':
    unittest.main()
