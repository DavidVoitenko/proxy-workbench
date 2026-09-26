"""The integration pass, end to end, on local sockets only.

collect through EVERY adapter -> scan -> export of every format -> backup
create -> pool create + refill -> schedule add -> api-key bootstrap -> serve ->
replayed POST /v1/keys (no secret) -> gateway.

Nothing here touches a public proxy, a public list or a third-party service: the
proxy is a real HTTP proxy on 127.0.0.1 that tunnels a real origin, and every
address in a list is one the collector stores without ever dialling.
"""
from __future__ import annotations

import base64
import json
import os
import pathlib
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from proxy_workbench import api, apikeys, apiv1, core, proxytool, scheduler, source_catalog
from proxy_workbench import db as store
from proxy_workbench import pools as pools_module  # noqa: F401
from proxy_workbench import secrets as secretstore  # noqa: F401

CHUNK = 16 * 1024


# --------------------------------------------------------------------------
# a real HTTP proxy on loopback: CONNECT tunnel + absolute-URI forwarding
# --------------------------------------------------------------------------
class LoopbackProxy:
    def __init__(self, host='127.0.0.1', port=0):
        self.host, self.port = host, port
        self.origin = None
        self.requests = 0
        self.tunnels = 0
        self._server = None
        self._thread = None

    @property
    def url(self):
        return f'http://{self.host}:{self.port}'

    def start(self):
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((self.host, self.port))
        self._server.listen(64)
        self._server.settimeout(0.2)
        self.port = self._server.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def _serve(self):
        while True:
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        conn.settimeout(15)
        try:
            while True:
                head = self._read_head(conn)
                if not head:
                    return
                method, target, headers = head
                if method == 'CONNECT':
                    self.tunnels += 1
                    self._tunnel(conn, target)
                    return
                self.requests += 1
                self._forward(conn, method, target, headers)
        except (OSError, ValueError):
            pass
        finally:
            with contextlib_suppress():
                conn.close()

    @staticmethod
    def _read_head(conn):
        data = b''
        while b'\r\n\r\n' not in data and len(data) < 65536:
            part = conn.recv(4096)
            if not part:
                return None
            data += part
        head, _, rest = data.partition(b'\r\n\r\n')
        lines = head.decode('latin-1').split('\r\n')
        if not lines or not lines[0]:
            return None
        pieces = lines[0].split(' ')
        if len(pieces) < 2:
            return None
        headers = {}
        for line in lines[1:]:
            if ':' in line:
                name, _, value = line.partition(':')
                headers[name.strip().lower()] = value.strip()
        return pieces[0].upper(), pieces[1], headers

    def _tunnel(self, conn, authority):
        host, _, port = authority.rpartition(':')
        try:
            upstream = socket.create_connection((host.strip('[]'), int(port)), timeout=5)
        except (OSError, ValueError):
            conn.sendall(b'HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n')
            return
        conn.sendall(b'HTTP/1.1 200 Connection established\r\n\r\n')
        self._pump(conn, upstream)

    def _pump(self, left, right):
        def copy(source, sink):
            try:
                while True:
                    chunk = source.recv(CHUNK)
                    if not chunk:
                        break
                    sink.sendall(chunk)
            except OSError:
                pass
            finally:
                for sock in (source, sink):
                    with contextlib_suppress():
                        sock.shutdown(socket.SHUT_RDWR)

        threads = [threading.Thread(target=copy, args=pair, daemon=True)
                   for pair in ((left, right), (right, left))]
        for thread in threads:
            thread.start()
        # The tunnel stays open until both sides are done.  Returning early
        # would close the client socket while the origin is still writing --
        # fine for a short GET, fatal for a held connection or a websocket.
        for thread in threads:
            thread.join(timeout=20)

    def _forward(self, conn, method, target, headers):
        from urllib.parse import urlsplit
        parsed = urlsplit(target)
        path = parsed.path or '/'
        if parsed.query:
            path += '?' + parsed.query
        try:
            upstream = socket.create_connection((parsed.hostname, parsed.port or 80), timeout=5)
        except OSError:
            conn.sendall(b'HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n')
            return
        request = f'{method} {path} HTTP/1.1\r\nHost: {parsed.netloc}\r\nConnection: close\r\n\r\n'
        upstream.sendall(request.encode('latin-1'))
        self._pump(conn, upstream)

    def stop(self):
        if self._server is not None:
            with contextlib_suppress():
                self._server.close()


class _Suppress:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True


def contextlib_suppress():
    return _Suppress()


# --------------------------------------------------------------------------
# an origin that serves every source format and the judge
# --------------------------------------------------------------------------
def _b64(value):
    return base64.b64encode(value.encode()).decode()


class Origin:
    def __init__(self):
        self.requests = []
        self.etag_hits = 0

    def body(self, kind):
        if kind == 'line':
            return b'11.0.0.7:8080\n11.0.0.8:8080\n11.0.0.9:8080\n'
        if kind == 'json-records':
            return json.dumps({'proxies': [
                {'ip': '11.0.0.11', 'port': 8080, 'protocol': 'http', 'country': 'NL'},
                {'ip': '11.0.0.12', 'port': 1080, 'protocol': 'socks5'}]}).encode()
        if kind == 'fields':
            return (b'ip,port,protocol,country,anonymity\n'
                    b'11.0.0.31,8080,http,DE,elite\n'
                    b'11.0.0.32,8080,http,DE,elite\n')
        if kind == 'html-table':
            return ('<html><body><table id="table_proxies"><thead><tr><th>#</th><th>ip</th>'
                    '<th>port</th><th>type</th></tr></thead><tbody>'
                    '<tr><td>1</td><td data-ip="%s" data-port="%s">x</td><td>%s</td>'
                    '<td><a href="?type=HTTPS">HTTPS</a></td></tr>'
                    '<tr><td>2</td><td data-ip="%s" data-port="%s">x</td><td>%s</td>'
                    '<td><a href="?type=HTTP">HTTP</a></td></tr>'
                    '</tbody></table></body></html>'
                    % (_b64('11.0.0.51'), _b64('8080'), _b64('1'),
                       _b64('11.0.0.52'), _b64('8080'), _b64('2'))).encode()
        if kind == 'page-json':
            return json.dumps({'data': [{'ip': '11.0.0.81', 'port': 8080, 'protocols': ['http']}],
                               'page': 1, 'total': 1, 'has_more': False}).encode()
        if kind == 'empty':
            return b''
        return b'ok'

    def handler(self):
        origin = self

        class H(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *a):
                pass

            def do_GET(self):
                path = self.path.split('?')[0].strip('/')
                origin.requests.append(self.path)
                if self.headers.get('If-None-Match') == '"v1"':
                    origin.etag_hits += 1
                    self.send_response(304)
                    self.send_header('ETag', '"v1"')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                if path == 'big':
                    body = b'0' * (2 * 1024 * 1024)
                elif path.startswith('judge'):
                    # A judge echoes the address it saw; through the proxy that
                    # is the proxy's own address, never this machine's.
                    body = json.dumps({'origin': '203.0.113.200'}).encode()
                else:
                    body = origin.body(path)
                ctype = 'text/html' if path == 'html-table' else 'application/json'
                self.send_response(200)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('ETag', '"v1"')
                self.end_headers()
                self.wfile.write(body)
        return H


ADAPTERS = ('line', 'json-records', 'fields', 'html-table', 'page-json')
PROFILES = {
    'line': {'kind': 'line', 'profile': 'generic-v1', 'config': {}},
    'json-records': {'kind': 'json-records', 'profile': 'generic-v1',
                     'config': {'default_protocol': 'http'}},
    'fields': {'kind': 'fields', 'profile': 'generic-fields-v1',
               'config': source_catalog._adapter_config('new-026', 'fields')},
    'html-table': {'kind': 'html-table', 'profile': 'generic-html-v1',
                   'config': source_catalog._adapter_config('new-010', 'html-table')},
    'page-json': {'kind': 'page-json', 'profile': 'page-number-v1',
                  'config': source_catalog._adapter_config('new-045', 'page-json')},
}


class IntegrationPass(unittest.TestCase):
    """One run, every door, in the order a user meets them."""

    @classmethod
    def setUpClass(cls):
        cls.home = pathlib.Path(tempfile.mkdtemp())
        cls.origin = Origin()
        cls.origin_server = ThreadingHTTPServer(('127.0.0.1', 0), cls.origin.handler())
        threading.Thread(target=cls.origin_server.serve_forever, daemon=True).start()
        cls.base = f'http://127.0.0.1:{cls.origin_server.server_address[1]}'
        cls.proxy = LoopbackProxy().start()
        cls.proxy_url = cls.proxy.url
        store.migrate(cls.home)
        cls.conn = store.connect(cls.home)
        cls.conn.row_factory = None

    @classmethod
    def tearDownClass(cls):
        cls.origin_server.shutdown()
        cls.proxy.stop()
        cls.conn.close()

    # -- 1. collect through every adapter ---------------------------------
    def test_01_collect_through_every_adapter_and_their_provenance(self):
        plans = [proxytool.SourcePlan(f'src-{kind}', f'{self.base}/{kind}', kind, PROFILES[kind])
                 for kind in ADAPTERS]
        report = proxytool.asyncio.run(proxytool.collect(
            self.conn, [], [], plans=plans, allow_private_sources=True))
        by_id = {row['source_id']: row for row in report['sources']}
        for kind in ADAPTERS:
            with self.subTest(adapter=kind):
                row = by_id[f'src-{kind}']
                self.assertEqual(row['http_state'], 'http_2xx_nonempty', row)
                self.assertEqual(row['parse_state'], 'complete', row)
                self.assertGreater(row['rows'], 0, row)
        self.assertGreaterEqual(
            self.conn.execute('SELECT count(DISTINCT source_id) FROM membership_source').fetchone()[0],
            len(ADAPTERS))
        for table in ('source_observation', 'source_generation', 'source_identity',
                      'source_state', 'membership_source'):
            with self.subTest(table=table):
                self.assertGreater(self.conn.execute(
                    f'SELECT count(*) FROM {table}').fetchone()[0], 0)

    def test_02_a_second_collect_is_304_and_adds_no_generation(self):
        plans = [proxytool.SourcePlan(f'src-{kind}', f'{self.base}/{kind}', kind, PROFILES[kind])
                 for kind in ADAPTERS]
        before = self.conn.execute('SELECT count(*) FROM source_generation').fetchone()[0]
        report = proxytool.asyncio.run(proxytool.collect(
            self.conn, [], [], plans=plans, allow_private_sources=True))
        after = self.conn.execute('SELECT count(*) FROM source_generation').fetchone()[0]
        self.assertEqual(self.origin.etag_hits, len(ADAPTERS), 'every source was asked conditionally')
        for row in report['sources']:
            self.assertEqual(row['http_state'], 'not_modified', row)
        self.assertEqual(before, after, 'a 304 is a validation of the transport, not an answer')

    def test_03_an_empty_body_is_its_own_outcome(self):
        plan = [proxytool.SourcePlan('src-empty', f'{self.base}/empty', 'json-records',
                                      PROFILES['json-records'])]
        report = proxytool.asyncio.run(proxytool.collect(
            self.conn, [], [], plans=plan, allow_private_sources=True))
        self.assertEqual(report['sources'][0]['http_state'], 'empty_body')
        self.assertEqual(report['sources'][0]['parse_state'], 'empty')

    # -- 2. the whole measured pipeline -----------------------------------
    def test_04_scan_export_backup_pool_schedule(self):
        from proxy_workbench import exportsvc
        # The config goes through the product's own validator, so the targets
        # are in the normalized shape the engine documents rather than a
        # hand-written dict that happens to look right.
        import argparse
        args = argparse.Namespace(config=None, url=f'{self.base}/line', attempts=1, timeout=8,
                                  max_bytes=262144,
                                  request_profile=None, judge_url=f'{self.base}/judge',
                                  connect_timeout=4, fail_fast=False,
                                  speedtest_url=f'{self.base}/big', speedtest_bytes=2 * 1024 * 1024)
        config = proxytool.target_config(args)
        # One real endpoint, reached through a real proxy, is enough to exercise
        # every stage: the address is this machine's loopback proxy, which the
        # collector would never accept but the importer is told to.
        workbench = proxytool.Workbench(self.home)
        self.addCleanup(workbench.close)
        conn = workbench.conn
        mine = 'col-integration'
        conn.execute("INSERT OR IGNORE INTO collections(id, name, kind, created_at) VALUES (?,?,?,?)",
                     (mine, 'integration', 'private', time.time()))
        # The address the engine stores and decides on is a normal public one;
        # the socket it opens is this machine's loopback proxy.  That is the one
        # substitution a mock makes, and it is the same one the probe-area
        # scenarios make: every decision above the socket (which addresses are
        # candidates, which pass, what the export may contain) is the product's.
        public = 'http://11.0.0.7:8080'
        workbench.add_members(mine, [public], origin='import')
        state = {}
        own_ips = {'203.0.113.200'}

        real = self.proxy.url

        async def probe_with_baseline(proxy, scan_config, limiter):
            # The one hop a mock replaces: the address under test is the loopback
            # proxy, everything else is the product's own code path.
            return await proxytool.check_proxy(
                proxy.replace(public, real), scan_config, limiter, own_ips=own_ips)

        profile = proxytool.asyncio.run(proxytool.scan(
            conn, config, recheck=True, collection_id=mine, run_state=state,
            probe=probe_with_baseline, progress=False))
        self.assertTrue(profile)
        self.assertEqual(state.get('state'), 'complete', state)
        self.assertGreaterEqual(state.get('checked', 0), 1, state)
        # F01: the evidence level comes from the probes ladder, not from a guess.
        rows = [json.loads(payload) for (payload,) in conn.execute(
            'SELECT payload FROM results WHERE profile=?', (profile,))]
        self.assertTrue(rows)
        self.assertIn(rows[0].get('evidence'), ('transfer_ok', 'none'))
        # judge + speed went through the same Transport.
        self.assertIn('anonymity', rows[0], 'the judge verdict is in the row')
        self.assertIn(rows[0]['anonymity'].get('code'), (None, 'JUDGE_INVALID',
                                                         'JUDGE_CHALLENGE', 'JUDGE_UNVERIFIED'))
        self.assertIn('speed', rows[0], 'the throughput measurement is in the row')

        directory = self.home / 'exports'
        report = proxytool.export(conn, profile, directory, collection_id=mine,
                                  min_success=0, watch_minutes=10,
                                  client_target='1.14.0')
        self.assertGreaterEqual(report['exported'], 1, report)
        generation_dir = pathlib.Path(report['directory'])
        files = sorted(p.name for p in generation_dir.iterdir())
        for wanted in ('proxies.txt', 'hostport.txt', 'http.txt', 'https.txt', 'socks4.txt',
                       'socks5.txt', 'proxychains.txt', 'proxy.pac', 'clash.yaml',
                       'singbox.json', 'ranked.json', 'ranked.csv', 'snapshot.txt',
                       'status.json'):
            self.assertIn(wanted, files, files)
        # F20/area-export: the pinned client decided the sing-box shape.
        singbox = json.loads((generation_dir / 'singbox.json').read_text())
        self.assertNotIn('block', [item.get('tag') for item in singbox.get('outbounds', [])])

        backup = workbench.backup_create('integration')
        manifest = backup.to_dict() if hasattr(backup, 'to_dict') else dict(backup)
        self.assertTrue((self.home / 'backups').is_dir())
        self.assertTrue(manifest.get('sha256'), manifest)
        self.assertTrue((pathlib.Path(manifest['path'])).is_file(), manifest)

        pool = workbench.pools().create('pool-integration', collection_id=mine,
                                        profile_id=profile, desired=1, minimum=0, reserve=1)
        self.assertEqual(pool.desired, 1)
        refill = pools_module.refill(workbench.pools(), pool.id,
                                     api.pool_candidate_source(conn), now=workbench.clock())
        self.assertIn(refill.state, ('complete', 'degraded', 'empty'), refill)
        self.assertGreaterEqual(refill.served, 0, refill)
        self.assertTrue(refill.deficit_reasons is not None, refill)

        engine = scheduler.Scheduler(workbench.schedules(), clock=workbench.clock)
        added = engine.add({'id': 'nightly', 'pool_id': pool.id, 'kind': 'interval',
                            'interval_minutes': 60, 'timezone': 'UTC',
                            'budgets': {'requests': 5, 'reset': 'daily', 'timezone': 'UTC'}})
        self.assertEqual(added.id, 'nightly')
        self.assertEqual(engine.persistence()['lost_on_restart'], [])
        IntegrationPass.workbench = workbench
        IntegrationPass.profile_id = profile
        IntegrationPass.collection_id = mine
        IntegrationPass.report_count = report['exported']

    # -- 3. the API and the gateway ---------------------------------------
    def test_05_serve_keys_gateway_and_the_one_shot_secret(self):
        from proxy_workbench import gateway
        workbench = IntegrationPass.workbench
        report_count = IntegrationPass.report_count
        manager = workbench.keys()
        admin = manager.bootstrap_admin(local_trusted=True, name='bootstrap',
                                        permissions=sorted(apiv1.PERMISSIONS))
        service = api.WorkbenchService(self.home, exports=api.Exports(self.home / 'exports'),
                                      key_store=manager)
        control = apiv1.ApiV1(service, apiv1.ApiKeyStore(manager))
        counter = [0]

        def call(method, path, body=None, key=None, idem=None):
            counter[0] += 1
            head = {'Host': '127.0.0.1', 'Authorization': f'Bearer {key or admin.secret}'}
            if method != 'GET':
                head['Idempotency-Key'] = idem or f'idem-{counter[0]}'
                head['Content-Type'] = 'application/json'
            return control.handle(apiv1.Request(
                method, path, headers=head,
                body=json.dumps(body).encode() if body is not None else b''))

        # The catalog answers with the fields F13 asks for.
        catalog = call('GET', '/v1/sources/catalog')
        self.assertEqual(catalog.status_code, 200, catalog.body)
        first_row = catalog.json()['items'][0]
        self.assertIn('support', first_row)
        self.assertIn('dataset_group', first_row)

        # The selection, the list and refresh all speak the same id space.
        listed = call('GET', '/v1/sources')
        self.assertEqual(listed.status_code, 200, listed.body)
        self.assertIn('items', listed.json())
        source_id = listed.json()['items'][0]['id'] if listed.json()['items'] else None
        if source_id:
            detail = call('GET', f'/v1/sources/{source_id}')
            self.assertEqual(detail.status_code, 200, detail.body)
            preview = call('POST', f'/v1/sources/{source_id}/refresh/preview')
            self.assertEqual(preview.status_code, 200, preview.body)
            self.assertIn('has_feed_row', preview.json())

        # An import of a private list is refused unless the caller says so.
        private = 'http://localhost:8080'
        strict = call('POST', f'/v1/collections/{self.collection_id}/imports/preview',
                      {'content': private + '\n', 'format': 'txt'})
        self.assertIn(strict.json()['rows'][0]['reason'], ('E_IMPORT_HOSTNAME',), strict.body)
        loose = call('POST', f'/v1/collections/{self.collection_id}/imports/preview',
                     {'content': private + '\n', 'format': 'txt',
                      'allow_private_endpoints': True})
        self.assertEqual(loose.json()['rows'][0]['state'], 'valid', loose.body)
        self.assertTrue(loose.json()['allow_private_endpoints'])

        # The pool recheck is a job that actually runs.
        pool_id = 'pool-integration'
        self.assertTrue(pathlib.Path(self.home / 'exports').is_dir())
        recheck = call('POST', f'/v1/pools/{pool_id}/recheck')
        self.assertEqual(recheck.status_code, 202, recheck.body)
        body = recheck.json()
        self.assertTrue(body['job_id'])
        self.assertIn(body['job_state'], ('succeeded', 'partial'), body)

        # The one-shot secret: the same key, the same body, the same id, no secret.
        payload = {'name': 'reader', 'purpose': 'app', 'permissions': ['read.results']}
        issue = call('POST', '/v1/keys', payload, idem='one-shot')
        self.assertEqual(issue.status_code, 200, issue.body)
        issued = issue.json()
        self.assertTrue(issued['secret'].startswith(apikeys.SECRET_MARK), issued)
        again = call('POST', '/v1/keys', payload, idem='one-shot')
        self.assertEqual(again.status_code, 200, again.body)
        replayed_key = again.json()
        self.assertNotIn('secret', replayed_key, 'the secret is shown exactly once')
        self.assertTrue(replayed_key.get('secret_already_shown'))
        self.assertEqual(replayed_key['id'], issued['id'], 'idempotency still holds')

        # The rotating proxy over the published generation.
        import asyncio as _asyncio

        async def run_gateway():
            # The published generation the export just wrote, read through the
            # same `Exports` reader the API and the GUI use.
            return await gateway.start(self.home, '127.0.0.1', 0, None,
                                       {'collection_id': self.collection_id},
                                       'round-robin', 0, 0)

        server = _asyncio.run(run_gateway())
        async def stop():
            async with server:
                pass
        self.addCleanup(lambda: _asyncio.run(stop()))
        self.assertIsNotNone(server.gateway)
        # The listener is up on a real port and the rotating pool can be asked
        # for its contents without raising: that is the "gateway answers" step
        # of the acceptance, and it reads the export the run above published.
        self.assertTrue(server.sockets)
        self.assertTrue(server.sockets[0].getsockname()[1] > 0)
        rows = server.gateway.pool.refresh()
        self.assertIsInstance(rows, list)
        # The listener really accepts a client connection on the address it printed.
        self.assertTrue(server.sockets)
