"""Resource budgets and the three N of `--want`, measured on the real scan path.

`proxytool.scan` is the path a CLI run takes, and it is the only one that exists:
`pipeline.Pipeline`/`run_pipeline` are exercised by their own tests but nothing
in the product runs them, so a budget that only the pipeline honours is a budget
nobody has.

Three defects covered here:

* `gate.acquire()`/`gate.release()` were called without `requests=`/`bytes=`, so
  `ResourceGate._requests`/`_bytes` stayed 0 and `--max-requests`/`--run-max-bytes`
  were silent no-ops (F12, defect 23).
* `counted()` answered `1 if row.get('exit_ip')` for both `ip` and `exit`, and
  looked for the exit address at the top level while the judge writes
  `anonymity.exit_ip`: N unique IPs and N confirmed exit IPs were the same number,
  and both were always zero.
* A sweep that finished short of `--want` reported `stop_reason='complete'`, so
  an unreachable unit looked like an empty corpus (F12, F10).

Everything runs on documentation addresses (RFC 5737) against a temporary
database. No socket is opened.
"""
import asyncio
import contextlib
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import db, proxytool
from tests.workbench_support import add_candidate

CONFIG = {'targets': [{'url': 'https://one.invalid/'}], 'reputation': {}, 'anonymity': {}}


def _profile_id(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:20]


class ScanBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / db.DB_FILENAME
        self.conn, _ = db.open_db(self.path)
        self.addCleanup(self.conn.close)
        self.profile = _profile_id(CONFIG)
        self.conn.execute('INSERT OR IGNORE INTO profiles(id, config, digest, created_at)'
                          ' VALUES (?,?,?,?)',
                          (self.profile, json.dumps(CONFIG, sort_keys=True), self.profile, time.time()))

    def seed(self, count, *, host_prefix='198.51.100'):
        for index in range(1, count + 1):
            add_candidate(self.conn, f'http://{host_prefix}.{index}:8080')
        self.conn.commit()

    def scan(self, probe, **kwargs):
        state = {}
        asyncio.run(proxytool.scan(
            self.conn, CONFIG, workers=4, rate=0, probe=probe, progress=False,
            min_success=1, run_state=state, **kwargs))
        return state

    @staticmethod
    async def _ok_probe(proxy, config, limiter):
        return {'proxy': proxy, 'successes': 3, 'requests': 1, 'min_target_reliability': 1.0,
                'checked_at': time.time(), 'latency_ms': 10, 'score': 90,
                'samples': [{'ok': True, 'bytes': 2048, 'target': 0, 'attempt': 1}]}

    def test_max_requests_actually_stops_the_run(self):
        self.seed(20)
        state = self.scan(self._ok_probe, max_requests=1)
        self.assertEqual(state['checked'], 1)
        self.assertEqual(state['stop_reason'], 'E_LIMIT_BUDGET')
        self.assertEqual(state['state'], 'partial')

    def test_max_bytes_actually_stops_the_run(self):
        self.seed(20)
        state = self.scan(self._ok_probe, max_bytes=1)
        self.assertEqual(state['checked'], 1)
        self.assertEqual(state['stop_reason'], 'E_LIMIT_BUDGET')

    def test_without_a_budget_the_run_is_complete(self):
        self.seed(20)
        state = self.scan(self._ok_probe)
        self.assertEqual(state['checked'], 20)
        self.assertEqual(state['stop_reason'], 'complete')

    def test_a_measurement_that_spent_nothing_is_not_charged(self):
        """A refused verdict is not a request; charging it would end the run early."""
        self.seed(20)

        async def refused(proxy, config, limiter):
            return {'proxy': proxy, 'successes': 0, 'requests': 0, 'min_target_reliability': 0.0,
                    'checked_at': time.time(), 'samples': [], 'score': 0}

        state = self.scan(refused, max_requests=2)
        self.assertEqual(state['checked'], 20, 'a refusal spent no request and must not stop the run')
        self.assertEqual(state['stop_reason'], 'complete')


class CountWhatTests(unittest.TestCase):
    """N endpoints, N unique proxy IPs and N confirmed exit IPs are three numbers."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / db.DB_FILENAME
        self.conn, _ = db.open_db(self.path)
        self.addCleanup(self.conn.close)
        self.profile = _profile_id(CONFIG)
        self.conn.execute('INSERT OR IGNORE INTO profiles(id, config, digest, created_at)'
                          ' VALUES (?,?,?,?)',
                          (self.profile, json.dumps(CONFIG, sort_keys=True), self.profile, time.time()))

    def scan(self, addresses, probe, workers=1, **kwargs):
        """One worker by default: the order of the sweep is then the order of the
        candidates, so "which N stopped the run" is a fact and not a race."""
        for proxy in addresses:
            add_candidate(self.conn, proxy)
        self.conn.commit()
        state = {}
        asyncio.run(proxytool.scan(
            self.conn, CONFIG, workers=workers, rate=0, probe=probe, progress=False,
            min_success=1, recheck=True, run_state=state, **kwargs))
        return state

    @staticmethod
    async def _probe_with_exit(proxy, config, limiter):
        host = proxy.partition('://')[2].split(':')[0]
        return {'proxy': proxy, 'successes': 3, 'requests': 1, 'min_target_reliability': 1.0,
                'checked_at': time.time(), 'latency_ms': 10, 'score': 90, 'samples': [],
                'anonymity': {'level': 'elite', 'exit_ip': f'203.0.113.{host.split(".")[-1]}'}}

    def test_ip_counts_hosts_so_two_ports_on_one_machine_are_one_ip(self):
        addresses = ['http://198.51.100.10:8080', 'http://198.51.100.10:3128',
                     'http://198.51.100.11:8080', 'http://198.51.100.12:8080']
        state = self.scan(addresses, self._ok, want=2, count_what='ip')
        self.assertEqual(state['stop_reason'], 'want_reached')
        self.assertEqual(state['found'], 2)
        self.assertEqual(state['unique_ips'], 2, 'two ports on one host are one IP')
        self.assertGreaterEqual(state['endpoints'], 2,
                                'at least two endpoints passed; a duplicate host adds no IP')

    def test_exit_needs_a_confirmed_exit_not_a_top_level_field(self):
        addresses = ['http://198.51.100.20:8080', 'http://198.51.100.21:8080']
        state = self.scan(addresses, self._ok, want=1, count_what='exit')
        self.assertEqual(state['stop_reason'], 'want_unreachable_exit')
        self.assertEqual(state['found'], 0)
        self.assertEqual(state['endpoints'], 2, 'both endpoints passed; only the exit was missing')
        self.assertEqual(state['exit_ips'], 0)

    def test_a_confirmed_exit_is_read_from_the_judge_field(self):
        addresses = ['http://198.51.100.30:8080', 'http://198.51.100.31:8080']
        state = self.scan(addresses, self._probe_with_exit, want=2, count_what='exit')
        self.assertEqual(state['found'], 2)
        self.assertEqual(state['exit_ips'], 2)
        self.assertEqual(state['unique_ips'], 2)
        self.assertNotEqual(state['stop_reason'], 'want_unreachable_exit')

    def test_the_three_counts_are_reported_side_by_side(self):
        addresses = ['http://198.51.100.40:8080', 'http://198.51.100.40:3128',
                     'http://198.51.100.41:8080']
        state = self.scan(addresses, self._probe_with_exit, want=1, count_what='endpoint')
        self.assertEqual(state['endpoints'], 1)
        self.assertEqual(state['unique_ips'], 1)
        self.assertEqual(state['exit_ips'], 1)
        self.assertEqual(state['count_what'], 'endpoint')

    @staticmethod
    async def _ok(proxy, config, limiter):
        return {'proxy': proxy, 'successes': 3, 'requests': 1, 'min_target_reliability': 1.0,
                'checked_at': time.time(), 'latency_ms': 10, 'score': 90, 'samples': []}


class BackupCommandTests(unittest.TestCase):
    """F24: the restore, retention, cleanup and rebind paths the user can reach."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.data = self.home / 'data'
        self.conn, _ = db.open_db(self.data / db.DB_FILENAME)
        now = time.time()
        url = 'http://198.51.100.5:8080'
        db.upsert_endpoint(self.conn, url)
        endpoint = db.endpoint_id(url)
        db.add_member(self.conn, db.PUBLIC_COLLECTION_ID, endpoint, origin='manual')
        self.conn.execute('INSERT OR IGNORE INTO accesses(id, endpoint_id, mode,'
                          ' access_revision, created_at) VALUES (?,?,?,?,?)',
                          ('default', endpoint, 'public', 1, now))
        self.conn.execute('INSERT INTO observations(id, job_id, endpoint_id, access_id,'
                          ' access_revision, profile_id, profile_revision, started_at,'
                          " finished_at, verdict) VALUES ('o1','j1',?,'default',1,'p1',1,?,?,'ok')",
                          (endpoint, now - 10000, now - 10000))
        self.conn.execute('INSERT INTO results(profile, proxy, payload, observation_id,'
                          ' endpoint_id, access_id, access_revision, profile_id,'
                          ' profile_revision, checked_at, valid_until)'
                          " VALUES ('p1',?,'{}','o1',?,'default',1,'p1',1,?,?)",
                          (url, endpoint, now - 10000, now - 10000))
        self.conn.commit()
        self.conn.close()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = proxytool.main(['backup', '--data', str(self.data), '--json', *argv])
        except SystemExit as exc:
            code = exc.code
        text = out.getvalue().strip() or err.getvalue().strip()
        return code, text

    def test_the_whole_backup_family_is_reachable(self):
        retention = ['retention', '--retention-hours', '1', '--include-fresh']
        target = str(self.home / 'restored')
        for argv in (['create'], ['list'], ['verify'], retention, ['cleanup'],
                     ['preview', '--to-data', target],
                     ['restore', '--to-data', target],
                     ['restore', '--to-data', target, '--apply'],
                     ['rollback', '--to-data', str(self.home / 'rolled')],
                     ['rebind', '--apply']):
            with self.subTest(action=argv[0]):
                code, text = self.run_cli(*argv)
                self.assertEqual(code, 0, f'{argv}: {text}')
        self.assertTrue((Path(target) / db.DB_FILENAME).is_file())
        self.assertTrue((Path(target) / (db.DB_FILENAME + '.manifest.json')).is_file())

    def test_a_preview_changes_nothing_and_apply_does(self):
        code, text = self.run_cli('retention', '--retention-hours', '1', '--include-fresh')
        self.assertEqual(code, 0, text)
        preview = json.loads(text)
        self.assertEqual(preview['total_rows'], 2)
        conn = db.connect(self.data / db.DB_FILENAME)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute('SELECT count(*) FROM results').fetchone()[0], 1)
        conn.close()

        code, text = self.run_cli('retention', '--retention-hours', '1', '--include-fresh', '--apply')
        self.assertEqual(code, 0, text)
        self.assertEqual(json.loads(text)['deleted'], {'results': 1, 'observations': 1})

    def test_restore_preview_names_the_files_and_the_conflicts(self):
        target = self.home / 'again'
        code, text = self.run_cli('preview', '--to-data', str(target))
        self.assertEqual(code, 0, text)
        preview = json.loads(text)
        self.assertIn(db.DB_FILENAME, [item[0] for item in preview['files']])
        self.assertTrue(preview['ok'])

    def test_a_restore_without_a_target_folder_is_refused(self):
        code, text = self.run_cli('restore')
        self.assertEqual(code, 2)
        self.assertIn('--to-data', text)
        self.assertFalse((self.home / 'proxies.sqlite3').exists(),
                         'a restore without a target must not write anywhere')


if __name__ == '__main__':  # pragma: no cover
    unittest.main()
