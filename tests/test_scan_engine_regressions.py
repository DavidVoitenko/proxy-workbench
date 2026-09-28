"""Checking-engine regressions, on local mock proxies only.

* The CLI scan opened a second SQLite connection for its job store while the
  scan held an open write batch on the first one.  Every ``claim`` then waited
  the whole busy timeout with the event loop blocked (about 5 s per result),
  gave up, and left the job item ``pending``: a run of 130 candidates took
  minutes and a finished job still looked unfinished.
* The adaptive limit counted a dead proxy as overload, so a corpus that is
  mostly dead walked the scan down to one check at a time.
* A connect that failed because this machine ran out of descriptors was
  recorded as the proxy being ``UNREACHABLE``.
"""
import asyncio
import contextlib
import errno
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import pipeline, proxytool


class _Healthy(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body = b'healthy'
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _BlackholeAfterConnect(BaseHTTPRequestHandler):
    """Accept TCP, but leave the target or speed response unanswered."""

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError):
            pass  # The TCP prefilter closes as soon as connect succeeds.

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.server.basic_works and 'service.invalid/health' in self.path:
            body = b'healthy'
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        time.sleep(1.5)


def _closed_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


class CliScanJobTests(unittest.TestCase):
    def test_speed_scan_records_blackholed_proxies_and_speed_timeout(self):
        servers = []
        for index in range(8):
            server = ThreadingHTTPServer(('127.0.0.1', 0), _BlackholeAfterConnect)
            server.basic_works = index == 0
            threading.Thread(target=server.serve_forever, daemon=True).start()
            servers.append(server)
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)

        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            listing = data / 'list.txt'
            listing.write_text('\n'.join(f'http://127.0.0.1:{server.server_port}'
                                         for server in servers) + '\n')
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out), \
                    mock.patch.dict('os.environ', {'HTTP_PROXY': '', 'HTTPS_PROXY': '', 'ALL_PROXY': ''}):
                self.assertEqual(proxytool.main(['collect', '--no-sources', '--data', str(data),
                                                 '--input', str(listing), '--allow-private-endpoints']), 0,
                                 out.getvalue())
                code = proxytool.main(['scan', '--data', str(data),
                                       '--url', 'http://service.invalid/health', '--attempts', '1',
                                       '--timeout', '0.2', '--connect-timeout', '0.2',
                                       '--workers', '8', '--max-per-host', '8', '--rate', '0',
                                       '--prefilter', '8', '--prefilter-timeout', '0.2',
                                       '--speedtest-url', 'http://speed.invalid/file',
                                       '--speedtest-bytes', '2000000', '--deadline', '5'])
            self.assertEqual(code, 0, out.getvalue())
            self.assertIn('Checked 8/8', out.getvalue())
            with contextlib.closing(sqlite3.connect(data / 'proxies.sqlite3')) as conn:
                job_id = conn.execute('SELECT id FROM job ORDER BY created_at DESC LIMIT 1').fetchone()[0]
                results = [json.loads(payload) for (payload,) in conn.execute(
                    'SELECT payload FROM results WHERE job_id=?', (job_id,))]
                observations = conn.execute('SELECT COUNT(*) FROM observations WHERE job_id=?',
                                            (job_id,)).fetchone()[0]
                pending = conn.execute("SELECT COUNT(*) FROM job_item WHERE job_id=? AND state='pending'",
                                       (job_id,)).fetchone()[0]
            self.assertEqual((len(results), observations, pending), (8, 8, 0))
            self.assertEqual(sum(row['successes'] > 0 for row in results), 1)
            self.assertEqual(sum(row.get('speed', {}).get('state') == 'error' for row in results), 1)

    def test_a_cli_scan_finishes_every_job_item_without_waiting_on_itself(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), _Healthy)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            listing = data / 'list.txt'
            dead = sorted({_closed_port() for _ in range(6)})
            listing.write_text('\n'.join([f'http://127.0.0.1:{server.server_port}']
                                         + [f'http://127.0.0.1:{port}' for port in dead]) + '\n')
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out), \
                    mock.patch.dict('os.environ', {'HTTP_PROXY': '', 'HTTPS_PROXY': '', 'ALL_PROXY': ''}):
                self.assertEqual(proxytool.main(['collect', '--no-sources', '--data', str(data),
                                                 '--input', str(listing), '--allow-private-endpoints']), 0,
                                 out.getvalue())
                started = time.monotonic()
                code = proxytool.main(['scan', '--data', str(data), '--url', 'http://service.invalid/health',
                                       '--attempts', '1', '--timeout', '3', '--connect-timeout', '2',
                                       '--max-per-host', '16', '--rate', '0'])
                elapsed = time.monotonic() - started
            self.assertEqual(code, 0, out.getvalue())
            # One busy-timeout wait per result was ~5 s each; seven of them
            # would be over half a minute.
            self.assertLess(elapsed, 15, out.getvalue())
            with contextlib.closing(sqlite3.connect(data / 'proxies.sqlite3')) as conn:
                states = dict(conn.execute('SELECT state, count(*) FROM job_item GROUP BY state'))
                passing = conn.execute("SELECT count(*) FROM results WHERE error_code IS NULL "
                                       "AND json_extract(payload, '$.successes') > 0").fetchone()[0]
            self.assertEqual(states, {'done': 1, 'unreachable': len(dead)})
            self.assertEqual(passing, 1)

    def test_a_lent_connection_is_not_closed_by_the_workbench(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = proxytool.open_db(Path(tmp) / 'proxies.sqlite3')
            try:
                bench = proxytool.Workbench(tmp, conn=conn)
                self.assertIs(bench.conn, conn)
                bench.close()
                conn.execute('SELECT 1')  # still open
            finally:
                conn.close()


class EndpointConcurrencyTests(unittest.TestCase):
    def test_dead_proxies_do_not_walk_the_limit_down(self):
        controller = proxytool.EndpointConcurrency(minimum=1, maximum=64, window=8, increase=4)
        before = controller.limit
        for _ in range(64):
            controller.observe_outcome(pipeline.StageOutcome('cheap', False, code='UNREACHABLE',
                                                             failed_stage='tcp'))
        self.assertGreater(controller.limit, before)
        self.assertEqual(controller.snapshot().decreases, 0)

    def test_local_failures_still_shrink_it(self):
        controller = proxytool.EndpointConcurrency(minimum=1, maximum=64, window=8)
        for _ in range(32):
            controller.observe_outcome(pipeline.StageOutcome('basic', False, code='OSError'))
        self.assertEqual(controller.limit, 1)

    def test_a_mostly_dead_scan_keeps_its_workers(self):
        dead = sorted({_closed_port() for _ in range(80)})

        async def run():
            config = pipeline.PipelineConfig(
                sources=(pipeline.fixture_source('s', [f'http://127.0.0.1:{p}' for p in dead]),),
                budgets=pipeline.Budgets(max_inflight=32, max_open_fds=1000, max_requests=None,
                                         max_bytes=None),
                limits=pipeline.Limits(max_per_host=64),
                runners=pipeline.Runners(cheap=cheap, basic=None, expensive=None),
                run_cheap=True, run_basic=False, run_expensive=False,
                normalize=lambda value: value or None)
            return await pipeline.Pipeline(config, concurrency=proxytool.EndpointConcurrency(
                minimum=1, maximum=32, window=8, increase=4)).run()

        async def cheap(item, *, stage, limit):
            await asyncio.sleep(0.002)
            ok = await proxytool.reachable(item.endpoint, 1)
            return pipeline.StageOutcome(stage, ok, code=None if ok else 'UNREACHABLE',
                                         failed_stage=None if ok else 'tcp')

        result = asyncio.run(run())
        self.assertEqual(result.metrics.measured, len(dead))
        self.assertEqual(result.metrics.concurrency['decreases'], 0)
        self.assertGreater(result.metrics.peak_inflight, 8)


class _Counting(BaseHTTPRequestHandler):
    """A healthy proxy that records every request and can raise the stop file."""

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.server.world.hit(self.server.server_port)
        time.sleep(self.server.world.delay)
        body = b'healthy'
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        with contextlib.suppress(OSError):
            self.wfile.write(body)


class _World:
    def __init__(self, test, count, delay=0.0):
        self.delay = delay
        self.hits = []
        self.lock = threading.Lock()
        self.stop_at = None
        self.stop_file = None
        self.servers = []
        for _ in range(count):
            server = ThreadingHTTPServer(('127.0.0.1', 0), _Counting)
            server.daemon_threads = True
            server.world = self
            threading.Thread(target=server.serve_forever, daemon=True).start()
            test.addCleanup(server.server_close)
            test.addCleanup(server.shutdown)
            self.servers.append(server)

    def hit(self, port):
        with self.lock:
            self.hits.append(port)
            if self.stop_at is not None and len(self.hits) >= self.stop_at:
                self.stop_file.write_text('stop')

    def proxies(self):
        return [f'http://127.0.0.1:{server.server_port}' for server in self.servers]


def _cli(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out), \
            mock.patch.dict('os.environ', {'HTTP_PROXY': '', 'HTTPS_PROXY': '', 'ALL_PROXY': ''}):
        return proxytool.main(argv), out.getvalue()


def _collect(test, data, proxies):
    listing = data / 'list.txt'
    listing.write_text('\n'.join(proxies) + '\n')
    code, out = _cli(['collect', '--no-sources', '--data', str(data), '--input', str(listing),
                      '--allow-private-endpoints'])
    test.assertEqual(code, 0, out)


def _scan_args(data, *extra):
    return ['scan', '--data', str(data), '--url', 'http://service.invalid/health', '--attempts', '1',
            '--timeout', '3', '--connect-timeout', '1', '--rate', '0', *extra]


class StopAndContinueTests(unittest.TestCase):
    def test_the_same_command_continues_a_stopped_scan(self):
        # A stopped run left its job ``running``; every later run was refused
        # with E_CONFLICT_BUSY ("Error: Busy") and nothing could continue it.
        world = _World(self, 10, delay=0.3)
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            dead = sorted({_closed_port() for _ in range(4)})
            _collect(self, data, world.proxies() + [f'http://127.0.0.1:{port}' for port in dead])
            world.stop_file, world.stop_at = data / 'stop', 3
            argv = _scan_args(data, '--max-per-host', '2', '--stop-file', str(world.stop_file))
            code, out = _cli(argv)
            self.assertEqual(code, 130, out)
            with contextlib.closing(sqlite3.connect(data / 'proxies.sqlite3')) as conn:
                jobs = conn.execute('SELECT id, state FROM job').fetchall()
                finished = {row[0] for row in conn.execute(
                    "SELECT e.canonical FROM job_item j JOIN endpoints e ON e.id=j.endpoint_id "
                    "WHERE j.state IN ('done', 'unreachable')")}
            self.assertEqual([state for _, state in jobs], ['paused'])
            self.assertTrue(finished)
            self.assertLess(len(finished), 14)

            world.stop_file.unlink()
            world.stop_at = None
            first_run = len(world.hits)
            code, out = _cli(argv)
            self.assertEqual(code, 0, out)
            self.assertIn(jobs[0][0], out)
            with contextlib.closing(sqlite3.connect(data / 'proxies.sqlite3')) as conn:
                self.assertEqual(conn.execute('SELECT id, state FROM job').fetchall(),
                                 [(jobs[0][0], 'succeeded')])
                states = dict(conn.execute('SELECT state, count(*) FROM job_item GROUP BY state'))
            self.assertEqual(states, {'done': 10, 'unreachable': len(dead)})
            # Nothing the stopped run finished was measured a second time.
            again = {f'http://127.0.0.1:{port}' for port in world.hits[first_run:]}
            self.assertFalse(again & finished)

    def test_a_job_that_cannot_start_is_not_left_queued(self):
        from proxy_workbench import jobs
        with tempfile.TemporaryDirectory() as tmp:
            conn = proxytool.open_db(Path(tmp) / 'proxies.sqlite3')
            bench = proxytool.Workbench(tmp, conn=conn)
            try:
                store = bench.jobs()
                scope = jobs.Scope(collection_id='c', profile_id='p', profile_revision=1, profile_digest='p')
                store.start(store.submit('pool_recheck', scope).id)
                with self.assertRaises(jobs.Busy):
                    proxytool.submit_scan_job(bench, conn, 'check', profile='p', profile_revision=1,
                                              collection_id=proxytool.ensure_collection(conn, None),
                                              candidates=['http://203.0.113.9:8080'])
                self.assertEqual(store.jobs(state='queued'), [])
            finally:
                # Windows cannot delete the temporary folder while the database is open.
                bench.close()
                conn.close()


class WatchRoundTests(unittest.TestCase):
    def test_every_watch_round_measures_the_passing_proxies_under_its_own_job(self):
        # Each round reused the first run's closed job, so every address looked
        # finished and a round measured nothing at all.
        world = _World(self, 3)
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            _collect(self, data, world.proxies() + [f'http://127.0.0.1:{_closed_port()}'])
            # First run and one full round, then stop at the first hit of the next round.
            world.stop_file, world.stop_at = data / 'stop', 7
            # A round that measures nothing never reaches the stop; end it anyway.
            safety = threading.Timer(20, lambda: world.stop_file.write_text('stop'))
            safety.start()
            self.addCleanup(safety.cancel)
            code, out = _cli(_scan_args(data, '--max-per-host', '4', '--watch', '0.01',
                                        '--stop-file', str(world.stop_file)))
            self.assertEqual(code, 130, out)
            with contextlib.closing(sqlite3.connect(data / 'proxies.sqlite3')) as conn:
                jobs = conn.execute('SELECT id, state FROM job ORDER BY created_at').fetchall()
                round_items = dict(conn.execute(
                    'SELECT state, count(*) FROM job_item WHERE job_id=? GROUP BY state', (jobs[1][0],)))
                observations = conn.execute('SELECT count(*) FROM observations WHERE job_id=?',
                                            (jobs[1][0],)).fetchone()[0]
        self.assertGreaterEqual(len(jobs), 3)
        self.assertEqual(jobs[0][1], 'succeeded')
        self.assertEqual(jobs[1][1], 'succeeded')
        self.assertEqual(round_items, {'done': 3})
        self.assertEqual(observations, 3)
        self.assertTrue(all(world.hits.count(server.server_port) >= 2 for server in world.servers))


class HostParkingTests(unittest.TestCase):
    def _run(self, endpoints, *, want=0, workers=8, delay=0.05):
        started = []
        inflight = set()
        peak = [0]

        async def basic(item, *, stage, limit):
            started.append(item.endpoint)
            inflight.add(item.endpoint)
            peak[0] = max(peak[0], len(inflight))
            await asyncio.sleep(delay)
            inflight.discard(item.endpoint)
            return pipeline.StageOutcome(stage, True)

        async def run():
            config = pipeline.PipelineConfig(
                sources=(pipeline.fixture_source('s', endpoints),),
                budgets=pipeline.Budgets(max_inflight=workers, max_open_fds=1000, max_requests=None,
                                         max_bytes=None),
                limits=pipeline.Limits(max_per_host=1), find=pipeline.FindPolicy(n=want, what='endpoint'),
                runners=pipeline.Runners(cheap=None, basic=basic, expensive=None),
                run_cheap=False, run_basic=True, run_expensive=False,
                normalize=lambda value: value or None)
            return await pipeline.Pipeline(config).run()

        return asyncio.run(run()), started, peak[0]

    def test_a_busy_host_does_not_hold_the_other_hosts_back(self):
        # Ten ports on each of four addresses, in address order: the workers all
        # waited on the first address while the other three sat in the queue.
        endpoints = [f'http://10.0.{host}.1:{8000 + port}' for host in range(4) for port in range(10)]
        result, started, peak = self._run(endpoints)
        self.assertEqual(result.reason, 'items_exhausted')
        self.assertEqual(sorted(started), sorted(endpoints))
        self.assertEqual(peak, 4)
        # Every address had started before the first one's third measurement.
        first = {endpoint.split(':')[1] for endpoint in started[:4]}
        self.assertEqual(len(first), 4)

    def test_a_reached_want_is_not_followed_by_the_items_waiting_for_their_host(self):
        endpoints = [f'http://10.0.0.1:{8000 + port}' for port in range(20)]
        result, started, _ = self._run(endpoints, want=3)
        self.assertEqual(result.reason, 'want_reached')
        self.assertEqual(len(started), 3)

    def test_a_parked_item_still_runs_once_its_host_is_free(self):
        endpoints = [f'http://10.0.0.1:{8000 + port}' for port in range(6)] + ['http://10.0.0.2:80']
        result, started, _ = self._run(endpoints, workers=2, delay=0.01)
        self.assertEqual(result.reason, 'items_exhausted')
        self.assertEqual(sorted(started), sorted(endpoints))


class ExportAndOptionTests(unittest.TestCase):
    def test_hostport_lists_a_mixed_port_once(self):
        from proxy_workbench import exportsvc
        rows = [{'proxy': 'http://203.0.113.7:8080'}, {'proxy': 'socks5://203.0.113.7:8080'},
                {'proxy': 'socks5://[2001:db8::1]:1080'}, {'proxy': 'http://[2001:db8::1]:1080'}]
        self.assertEqual(exportsvc.render_hostport(rows), '203.0.113.7:8080\n[2001:db8::1]:1080\n')

    def test_a_speed_test_smaller_than_the_minimum_sample_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(SystemExit):
            _cli(_scan_args(Path(tmp), '--speedtest-url', 'http://speed.invalid/f',
                            '--speedtest-bytes', '400000'))


class LocalResourceErrorTests(unittest.TestCase):
    def test_running_out_of_descriptors_is_not_an_unreachable_proxy(self):
        async def refuse(*args, **kwargs):
            raise OSError(errno.EMFILE, 'Too many open files')

        with mock.patch('asyncio.open_connection', refuse), self.assertRaises(OSError):
            asyncio.run(proxytool.reachable('http://127.0.0.1:9', 1))

    def test_a_refused_connect_is_still_unreachable(self):
        self.assertFalse(asyncio.run(proxytool.reachable(f'http://127.0.0.1:{_closed_port()}', 1)))

    def test_the_wrapped_local_error_is_found(self):
        try:
            try:
                raise OSError(errno.EMFILE, 'Too many open files')
            except OSError as inner:
                raise RuntimeError('wrapped') from inner
        except RuntimeError as outer:
            self.assertIsNotNone(proxytool.local_os_error(outer))
        self.assertIsNone(proxytool.local_os_error(ConnectionRefusedError(errno.ECONNREFUSED, 'no')))


if __name__ == '__main__':
    unittest.main()


class RamBudgetTests(unittest.TestCase):
    def test_the_worker_count_is_not_capped_at_64_by_the_body_reservation(self):
        from proxy_workbench import pipeline
        mib = 1024 * 1024
        for workers in (128, 512):
            budgets = pipeline.Budgets(max_inflight=workers, max_open_fds=10 ** 6, fds_per_request=3,
                                       max_ram_bytes=proxytool.scan_ram_budget(workers, mib),
                                       ram_per_inflight_bytes=mib)
            self.assertEqual(budgets.ram_ceiling >= workers, True, workers)

    def test_the_reservation_has_a_ceiling(self):
        mib = 1024 * 1024
        self.assertEqual(proxytool.scan_ram_budget(100_000, mib), proxytool.MAX_SCAN_RAM_BYTES)
        self.assertEqual(proxytool.scan_ram_budget(1, mib), proxytool.DEFAULT_MAX_RAM_BYTES)


class HostSpreadTests(unittest.TestCase):
    def test_many_ports_of_one_host_do_not_come_in_a_row(self):
        # Address order put thousands of ports of one IP next to each other, so
        # every worker waited for that host under the per-host limit.
        stream = [f'http://203.0.113.1:{port}' for port in range(1, 6)] + ['http://203.0.113.2:80',
                  'http://198.51.100.7:8080', 'http://198.51.100.7:3128']
        spread = proxytool.HostSpread()
        now = [proxy for proxy in stream if spread.admit(proxy)]
        self.assertEqual(now, ['http://203.0.113.1:1', 'http://203.0.113.2:80', 'http://198.51.100.7:8080'])
        rounds = list(spread.rounds())
        self.assertEqual(rounds[0], ['http://203.0.113.1:2', 'http://198.51.100.7:3128'])
        self.assertEqual(rounds[1:], [['http://203.0.113.1:3'], ['http://203.0.113.1:4'], ['http://203.0.113.1:5']])
        self.assertEqual(sorted(now + [p for r in rounds for p in r]), sorted(stream))
