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


def _closed_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


class CliScanJobTests(unittest.TestCase):
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
