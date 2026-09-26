import asyncio
import contextlib
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import geoip
from proxy_workbench import gui
from proxy_workbench import proxytool as p
from proxy_workbench import source_adapters
from proxy_workbench import source_catalog
from proxy_workbench import source_management


RESEARCH = Path(__file__).resolve().parents[2] / 'proxy-sources-research-2026-09-25'
SAMPLES = RESEARCH / 'samples'


def sample(name):
    path = SAMPLES / f'{name}.bin'
    if not path.is_file():
        raise unittest.SkipTest(f'образец исследования не найден: {name}')
    return path.read_bytes()


def line_plan(url, source_id='local-line'):
    """A collectable plan for a plain-text list, as a catalog record is."""
    return {'id': source_id, 'name': source_id, 'publisher': {'id': 'local', 'name': 'local'},
            'family_id': source_id, 'category': 'custom', 'endpoints': [
                {'id': 'primary', 'url': url, 'role': 'primary', 'relation': 'custom'}],
            'data_urls': [url], 'fallback_urls': [], 'protocols': [], 'protocol_hints': [],
            'adapter': {'kind': 'line', 'profile': 'line-v1', 'config': {}},
            'access': {'kind': 'public'}, 'collection_allowed': True, 'payload_role': 'proxy_list',
            'evidence': {}, 'limits': {}, 'rights': {}, 'tags': []}


class CatalogTests(unittest.TestCase):
    def test_corrected_catalog_is_versioned_and_legacy_aliases_survive(self):
        catalog = source_catalog.load_bundled()
        self.assertEqual(catalog['schema_version'], 1)
        self.assertEqual(len(catalog['sources']), 150)
        self.assertEqual(catalog['revision'], 2026092501)
        self.assertEqual(len(catalog['sources'][0]['legacy_specs']), 1)
        aliases = source_catalog.legacy_aliases(catalog)
        self.assertEqual(aliases['https://proxyspace.pro/http.txt'], 'cur-02')
        self.assertEqual(aliases['socks5 https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&protocol=socks5&proxy_format=ipport&format=text'], 'cur-46')
        self.assertEqual(catalog['sources'][0]['evidence']['proxy_liveness']['state'], 'not_run')

    def test_remote_catalog_rejects_unsafe_endpoint_and_revision_downgrade(self):
        catalog = source_catalog.load_bundled()
        raw = json.loads(source_catalog.bundled_path().read_text(encoding='utf-8'))
        unsafe = dict(raw['sources'][0], data_urls=['http://127.0.0.1:9/list'])
        with self.assertRaises(source_catalog.CatalogError):
            source_catalog.validate_catalog(dict(raw, sources=[unsafe]), allow_research=True, allow_unsafe=False)
        safe_raw = dict(raw, sources=[item for item in raw['sources'] if item['id'] != 'new-082'], sets=[])
        with self.assertRaises(source_catalog.CatalogError):
            source_catalog.accept_catalog(catalog, dict(safe_raw, revision=catalog['revision'] - 1))
        newer = source_catalog.accept_catalog(catalog, dict(safe_raw, revision=catalog['revision'] + 1))
        self.assertEqual(newer['revision'], catalog['revision'] + 1)

    def test_gui_settings_v2_is_atomically_upgraded_without_losing_urls(self):
        old = gui.defaults()
        old['settings_version'] = 2
        old.pop('source_selection')
        old['sources'] = [old['sources'][0], 'socks5 https://mine.example/list.txt']
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / 'gui-settings.json').write_text(json.dumps(old, ensure_ascii=False), encoding='utf-8')
            app = gui.App.__new__(gui.App)
            app.data = home
            migrated = app.settings()
            self.assertEqual(migrated['settings_version'], 3)
            self.assertIn('socks5 https://mine.example/list.txt', migrated['sources'])
            stored = json.loads((home / 'gui-settings.json').read_text(encoding='utf-8'))
            self.assertEqual(stored['settings_version'], 3)
            self.assertIn('socks5 https://mine.example/list.txt', stored['sources'])


        old = gui.defaults()
        old['settings_version'] = 2
        old.pop('source_selection')
        old['use_sources'] = False
        old['sources'] = old['sources'][:54] + ['socks5 https://mine.example/list.txt']
        migrated = source_catalog.migrate_settings(old)
        self.assertEqual(migrated['settings_version'], 3)
        self.assertFalse(migrated['use_sources'])
        self.assertEqual(len(migrated['source_selection']['selected_ids']), 55)
        self.assertTrue(any(item['url'] == 'https://mine.example/list.txt'
                            for item in migrated['source_selection']['custom_sources']))
        self.assertNotIn('new-001', migrated['source_selection']['selected_ids'])
        self.assertIn('socks5 https://mine.example/list.txt', migrated['sources'])
        # Migration is idempotent.
        self.assertEqual(source_catalog.migrate_settings(migrated)['sources'], migrated['sources'])

    def test_set_application_is_a_materialized_snapshot(self):
        settings = source_catalog.migrate_settings({'sources': ['https://mine.example/list.txt']})
        settings['source_selection']['selected_ids'] = ['cur-01', 'custom-' + '0' * 16]
        settings['source_selection']['custom_sources'] = [dict(id='custom-' + '0' * 16,
                                                               url='https://mine.example/list.txt',
                                                               adapter={'kind': 'line', 'profile': 'custom-v1', 'config': {}})]
        applied = source_catalog.apply_set(settings, 'quick')
        self.assertEqual(applied['source_selection']['selected_ids'][:2], ['cur-02', 'cur-04'])
        self.assertIn('custom-' + '0' * 16, applied['source_selection']['selected_ids'])
        self.assertNotIn('cur-01', applied['source_selection']['selected_ids'])


class MigrationDatabaseTests(unittest.TestCase):
    def test_legacy_membership_rows_keep_unknown_dates(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'old.sqlite3'
            old = __import__('sqlite3').connect(path)
            old.executescript('CREATE TABLE candidate_seen(proxy TEXT NOT NULL, source TEXT NOT NULL,'
                              ' PRIMARY KEY(proxy, source)) WITHOUT ROWID;'
                              "INSERT INTO candidate_seen VALUES ('http://11.0.0.9:80','legacy');")
            old.commit(); old.close()
            db = p.open_db(path)
            try:
                row = db.execute('SELECT first_seen_at,last_seen_at,legacy FROM candidate_seen_meta').fetchone()
                self.assertEqual(row, (None, None, 1))
                self.assertEqual(db.execute('SELECT count(*) FROM candidate_seen').fetchone()[0], 1)
            finally:
                db.close()


class AdapterIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = p.open_db(Path(self.temp.name) / 'collect.sqlite3')

    def tearDown(self):
        self.db.close(); self.temp.cleanup()

    async def serve(self, pages):
        async def handler(reader, writer):
            try:
                request = await reader.readuntil(b'\r\n\r\n')
                target = request.split(b' ', 2)[1].decode('ascii')
                parsed = urlsplit(target)
                value = pages.get((parsed.path, parse_qs(parsed.query).get('page', [''])[0]))
                if value is None:
                    value = pages.get(parsed.path, b'{}')
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n' % len(value) + value)
                await writer.drain()
            finally:
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()
        server = await asyncio.start_server(handler, '127.0.0.1', 0)
        return server, f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'

    def plan(self, source_id, base, path=None):
        catalog = source_catalog.load_bundled()
        item = dict(next(source for source in catalog['sources'] if source['id'] == source_id))
        item['endpoints'] = [dict(item['endpoints'][0], url=base + (path or '/' + source_id))]
        return item

    async def test_each_real_schema_uses_collect_normalize_and_storage(self):
        json_body = sample('new-009')
        fields_body = sample('new-026')
        html_body = sample('new-010')
        json_server, json_base = await self.serve({'/new-009': json_body})
        fields_server, fields_base = await self.serve({'/new-026': fields_body})
        html_server, html_base = await self.serve({'/freeproxy': html_body})
        try:
            for source_id, base, minimum, path in (('new-009', json_base, 100, None),
                                                     ('new-026', fields_base, 100, None),
                                                     ('new-010', html_base, 10, '/freeproxy')):
                report = await p.collect(self.db, [self.plan(source_id, base, path)], [], allow_private_sources=True, timeout=5)
                self.assertTrue(report['sources'][0]['complete'], report['sources'][0])
                self.assertGreaterEqual(report['unique'], minimum)
                self.assertEqual(report['sources'][0]['format'],
                                 {'new-009': 'json-records', 'new-026': 'fields', 'new-010': 'html-table'}[source_id])
                self.assertTrue(any(row[0].startswith(('http://', 'socks')) for row in self.db.execute('SELECT proxy FROM candidates')))
        finally:
            for server in (json_server, fields_server, html_server):
                server.close(); await server.wait_closed()

    async def test_page_json_uses_two_pages_and_stops_on_declared_total(self):
        first = json.loads(sample('new-045').decode('utf-8'))
        second_records = [{'ip': f'11.9.{index // 250}.{index % 250 + 1}', 'port': 8080, 'protocol': 'http'}
                          for index in range(62)]
        second = {'data': second_records, 'page': 2, 'limit': 100, 'total': 162}
        server, base = await self.serve({('/new-045', '1'): json.dumps(first).encode(),
                                         ('/new-045', '2'): json.dumps(second).encode()})
        try:
            report = await p.collect(self.db, [self.plan('new-045', base, '/new-045?page=1')], [], allow_private_sources=True, timeout=5)
            self.assertTrue(report['sources'][0]['complete'], report['sources'][0])
            self.assertEqual(report['sources'][0]['pages'], 2)
            self.assertEqual(report['unique'], 162)
        finally:
            server.close(); await server.wait_closed()

    async def test_metadata_claim_is_separate_and_normalized(self):
        body = sample('new-077')
        server, base = await self.serve({'/new-077': body})
        try:
            report = await p.collect(self.db, [self.plan('new-077', base)], [], allow_private_sources=True, timeout=5)
            self.assertGreater(report['unique'], 0)
            row = self.db.execute('SELECT country,asn,claimed_exit_ip,origin FROM source_metadata WHERE country IS NOT NULL LIMIT 1').fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[3], 'source_claimed')
            self.assertTrue(row[0].isalpha())
        finally:
            server.close(); await server.wait_closed()

    async def test_preview_uses_same_parser_but_never_writes_persistent_scope(self):
        body = b'[{"ip":"11.7.7.7","port":80,"protocol":"http"}]'
        server, base = await self.serve({'/preview': body})
        try:
            before = self.db.execute('SELECT count(*) FROM candidates').fetchone()[0]
            report = await p.collect(self.db, [f'json-records {base}/preview'], [], allow_private_sources=True, preview=True)
            self.assertTrue(report['preview'])
            self.assertEqual(report['sample'], ['http://11.7.7.7:80'])
            self.assertEqual(self.db.execute('SELECT count(*) FROM candidates').fetchone()[0], before)
            self.assertEqual(self.db.execute('SELECT count(*) FROM source_observation').fetchone()[0], 0)
        finally:
            server.close(); await server.wait_closed()


        cases = {
            'empty': b'',
            'html': b'<!doctype html><html><body>not a list</body></html>',
            'invalid': b'{not json',
            'credentials': b'[{"proxy":"http://user:secret@11.1.1.1:80","protocol":"http"}]',
        }
        server, base = await self.serve({f'/{name}': body for name, body in cases.items()})
        try:
            for name in cases:
                report = await p.collect(self.db, [f'json-records {base}/{name}'], [], allow_private_sources=True, timeout=5)
                self.assertEqual(report['sources'][0]['source_id'].startswith('custom-'), True)
                if name == 'empty':
                    self.assertEqual(report['sources'][0]['outcome'], 'empty')
                elif name == 'html':
                    self.assertEqual(report['sources'][0]['error'], 'SOURCE_HTML_PLACEHOLDER')
                elif name == 'invalid':
                    self.assertEqual(report['sources'][0]['error'], 'SOURCE_INVALID_JSON')
                else:
                    self.assertEqual(report['sources'][0]['rejected'], 1)
        finally:
            server.close(); await server.wait_closed()


class ResilienceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = p.open_db(Path(self.temp.name) / 'collect.sqlite3')

    def tearDown(self):
        self.db.close(); self.temp.cleanup()

    async def test_etag_304_does_not_change_membership_or_generation(self):
        body = b'[{"ip":"11.1.1.1","port":80,"protocol":"http"}]'
        calls = []
        async def handler(reader, writer):
            request = await reader.readuntil(b'\r\n\r\n')
            calls.append(request)
            if len(calls) == 1:
                value = b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\nETag: "v1"\r\nConnection: close\r\n\r\n' % len(body) + body
            else:
                value = b'HTTP/1.1 304 Not Modified\r\nETag: "v1"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n'
            writer.write(value); await writer.drain(); writer.close(); await writer.wait_closed()
        server = await asyncio.start_server(handler, '127.0.0.1', 0)
        base = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'
        try:
            first = await p.collect(self.db, [f'json-records {base}/list'], [], allow_private_sources=True)
            seen_before = self.db.execute('SELECT last_seen_at FROM candidate_seen_meta').fetchone()[0]
            generation_before = self.db.execute('SELECT count(*) FROM source_generation').fetchone()[0]
            second = await p.collect(self.db, [f'json-records {base}/list'], [], allow_private_sources=True)
            self.assertEqual(second['sources'][0]['http_state'], 'not_modified')
            self.assertEqual(second['sources'][0]['cache_state'], 'not_modified')
            self.assertEqual(self.db.execute('SELECT count(*) FROM source_generation').fetchone()[0], generation_before)
            self.assertEqual(self.db.execute('SELECT last_seen_at FROM candidate_seen_meta').fetchone()[0], seen_before)
            self.assertIn(b'If-None-Match', calls[1])
            self.assertTrue(first['sources'][0]['complete'])
        finally:
            server.close(); await server.wait_closed()

    async def test_fallback_is_same_source_and_last_good_survives_failure(self):
        first_body = b'[{"ip":"11.1.1.1","port":80,"protocol":"http"}]'
        calls = []
        async def handler(reader, writer):
            request = await reader.readuntil(b'\r\n\r\n')
            path = urlsplit(request.split(b' ')[1].decode()).path
            calls.append(path)
            if path == '/primary' and calls.count('/primary') == 1:
                value = b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n' % len(first_body) + first_body
            else:
                value = b'HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n'
            writer.write(value); await writer.drain(); writer.close(); await writer.wait_closed()
        server = await asyncio.start_server(handler, '127.0.0.1', 0)
        base = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'
        plan = {'id': 'mirror-source', 'endpoints': [{'id': 'primary', 'url': base + '/primary'},
                                                     {'id': 'fallback', 'url': base + '/fallback'}],
                'adapter': {'kind': 'json-records', 'profile': 'generic-v1', 'config': {}}, 'collection_allowed': True,
                'family_id': 'mirror-source', 'publisher': {'id': 'local', 'name': 'local'}}
        try:
            first = await p.collect(self.db, [plan], [], allow_private_sources=True, timeout=2,
                                    sleep=lambda _: asyncio.sleep(0))
            self.assertTrue(first['sources'][0]['complete'])
            self.assertEqual(first['sources'][0]['source_id'], 'mirror-source')
            second = await p.collect(self.db, [plan], [], allow_private_sources=True, timeout=2,
                                     sleep=lambda _: asyncio.sleep(0))
            self.assertEqual(second['sources'][0]['cache_state'], 'stale_last_good')
            self.assertEqual(second['unique'], 1)
        finally:
            server.close(); await server.wait_closed()

    async def test_unexpected_adapter_error_does_not_cancel_other_source(self):
        good = b'[{"ip":"11.2.2.2","port":80,"protocol":"http"}]'
        async def handler(reader, writer):
            request = await reader.readuntil(b'\r\n\r\n')
            path = urlsplit(request.split(b' ')[1].decode()).path
            body = good if path == '/good' else b'[{"ip":"11.3.3.3","port":80,"protocol":"http"}]'
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n' % len(body) + body)
            await writer.drain(); writer.close(); await writer.wait_closed()
        server = await asyncio.start_server(handler, '127.0.0.1', 0)
        base = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'
        bad = {'id': 'bad-source', 'endpoints': [{'id': 'primary', 'url': base + '/bad'}],
               'adapter': {'kind': 'json-records', 'profile': 'generic-v1', 'config': {}}, 'collection_allowed': True,
               'family_id': 'bad', 'publisher': {'id': 'local', 'name': 'local'}}
        good_plan = {'id': 'good-source', 'endpoints': [{'id': 'primary', 'url': base + '/good'}],
                     'adapter': {'kind': 'json-records', 'profile': 'generic-v1', 'config': {}}, 'collection_allowed': True,
                     'family_id': 'good', 'publisher': {'id': 'local', 'name': 'local'}}
        original = source_adapters.parse_page
        def parse(body, *args, **kwargs):
            if b'11.3.3.3' in body:
                raise RuntimeError('synthetic adapter failure')
            return original(body, *args, **kwargs)
        try:
            with mock.patch.object(source_adapters, 'parse_page', side_effect=parse):
                report = await p.collect(self.db, [bad, good_plan], [], allow_private_sources=True, timeout=2)
            by_id = {item['source_id']: item for item in report['sources']}
            self.assertEqual(by_id['bad-source']['error'], 'SOURCE_ADAPTER_ERROR')
            self.assertTrue(by_id['good-source']['complete'])
            self.assertIn('http://11.2.2.2:80', {row[0] for row in self.db.execute('SELECT proxy FROM candidates')})
        finally:
            server.close(); await server.wait_closed()


    def test_legacy_hash_membership_is_mapped_to_stable_catalog_id(self):
        catalog_item = next(item for item in source_catalog.load_bundled()['sources'] if item['id'] == 'cur-01')
        spec = catalog_item['legacy_specs'][0]
        old_id = p.source_key(spec)
        self.db.execute('INSERT INTO candidates VALUES (?)', ('http://11.6.6.6:80',))
        self.db.execute('INSERT INTO candidate_seen VALUES (?,?)', ('http://11.6.6.6:80', old_id))
        self.db.commit()
        plan = dict(catalog_item, endpoints=[dict(catalog_item['endpoints'][0], url='http://127.0.0.1:9/list')])
        # The mapping is applied before the network attempt and is visible even
        # when the endpoint is not eligible in this offline test.
        with mock.patch.object(p, '_collect_rich_sources', wraps=p._collect_rich_sources):
            report = asyncio.run(p.collect(self.db, [plan], [], allow_private_sources=True, timeout=0.1,
                                           sleep=lambda _: asyncio.sleep(0)))
        self.assertIn('cur-01', {row[0] for row in self.db.execute('SELECT source FROM candidate_seen')})


        self.db.execute('INSERT INTO candidates VALUES (?)', ('http://11.8.8.8:80',))
        self.db.commit()
        self.assertEqual(p.exclude_scope(self.db, ['http://11.8.8.8:80'], reason='user', scope_digest='test'), 1)
        self.assertEqual(p.scope_excluded(self.db, 'test'), {'http://11.8.8.8:80'})
        self.db.execute('DELETE FROM candidate_scope_exclusion WHERE scope_digest=?', ('test',))
        self.db.commit()
        self.assertEqual(p.scope_excluded(self.db, 'test'), set())
        self.assertEqual(self.db.execute('SELECT count(*) FROM candidates WHERE proxy=?', ('http://11.8.8.8:80',)).fetchone()[0], 1)

    def test_mirror_membership_does_not_inflate_independent_contribution(self):
        for source, family in (('mirror-a', 'publisher-feed'), ('mirror-b', 'publisher-feed')):
            self.db.execute('INSERT OR REPLACE INTO source_identity(source_id,family_id,publisher_id) VALUES (?,?,?)',
                            (source, family, 'publisher'))
            self.db.execute('INSERT OR IGNORE INTO candidates VALUES (?)', ('http://11.4.4.4:80',))
            self.db.execute('INSERT OR IGNORE INTO candidate_seen VALUES (?,?)', ('http://11.4.4.4:80', source))
        self.db.commit()
        self.assertEqual(p.listed_counts(self.db)['http://11.4.4.4:80'], 1)
        self.db.execute('INSERT INTO source_generation(source_id,state,active,last_good,created_at,record_count) VALUES (?,?,?,?,?,?)',
                        ('mirror-a', 'complete', 1, 1, time.time(), 1))
        generation = self.db.execute('SELECT last_insert_rowid()').fetchone()[0]
        self.db.execute('INSERT INTO source_generation_entry(generation_id,proxy) VALUES (?,?)',
                        (generation, 'http://11.4.4.4:80'))
        self.db.execute('INSERT INTO source_generation(source_id,state,active,last_good,created_at,record_count) VALUES (?,?,?,?,?,?)',
                        ('mirror-b', 'complete', 1, 1, time.time(), 1))
        generation = self.db.execute('SELECT last_insert_rowid()').fetchone()[0]
        self.db.execute('INSERT INTO source_generation_entry(generation_id,proxy) VALUES (?,?)',
                        (generation, 'http://11.4.4.4:80'))
        self.db.commit()
        contributions = p.source_contributions(self.db)
        self.assertEqual(contributions['mirror-a']['exclusive'], 0)
        self.assertEqual(contributions['mirror-b']['exclusive'], 0)


class TransportAndPreviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = p.open_db(Path(self.temp.name) / 'collect.sqlite3')

    def tearDown(self):
        self.db.close(); self.temp.cleanup()

    async def serve(self, handler):
        server = await asyncio.start_server(handler, '127.0.0.1', 0)
        return server, f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'

    async def test_dropped_connection_after_a_retry_is_a_transport_error(self):
        # A retryable refusal on the first attempt, then a connection torn down
        # mid-body.  The transport handler must bind its own exception: reusing
        # the name the refusal handler deletes raises UnboundLocalError, and the
        # source is then reported as a broken adapter instead of a failed fetch.
        body = b'[{"ip":"11.4.4.4","port":80,"protocol":"http"}]'
        attempts = []
        async def handler(reader, writer):
            await reader.readuntil(b'\r\n\r\n')
            attempts.append(1)
            if len(attempts) == 1:
                writer.write(b'HTTP/1.1 429 Too Many Requests\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
                await writer.drain(); writer.close(); await writer.wait_closed()
                return
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n' % (len(body) * 4) + body)
            await writer.drain()
            writer.transport.abort()
            with contextlib.suppress(Exception):
                writer.close(); await writer.wait_closed()
        server, base = await self.serve(handler)
        try:
            report = await p.collect(self.db, [f'json-records {base}/list'], [], allow_private_sources=True,
                                     timeout=2, sleep=lambda _: asyncio.sleep(0))
        finally:
            server.close(); await server.wait_closed()
        entry = report['sources'][0]
        self.assertEqual(len(attempts), 2)
        self.assertNotEqual(entry['error'], 'SOURCE_ADAPTER_ERROR')
        self.assertNotIn('exc', str(entry['error']).lower())
        self.assertEqual(entry['cache_state'], 'none')
        self.assertEqual(entry['outcome'], 'unavailable')
        self.assertIn(entry['error'], ('RemoteProtocolError', 'ReadError', 'IncompleteRead', 'ConnectError',
                                       'ServerDisconnectedError', 'ReadTimeout', 'TimeoutException'))

    async def test_byte_limit_keeps_its_own_outcome_when_the_origin_drops_the_connection(self):
        # A body past the byte budget must stay "limit exceeded" even when the
        # server tears the connection down instead of finishing it, and the
        # byte limit must not collapse into the record limit.
        big = b'[' + b','.join(json.dumps({'ip': f'11.{index // 256}.{index % 256}.1', 'port': 80,
                                           'protocol': 'http'}).encode() for index in range(50)) + b']'
        async def handler(reader, writer):
            await reader.readuntil(b'\r\n\r\n')
            # No Content-Length, so the limit can only be found while reading.
            writer.write(b'HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n' + big * 400)
            await writer.drain()
            writer.transport.abort()
            with contextlib.suppress(Exception):
                writer.close(); await writer.wait_closed()
        server, base = await self.serve(handler)
        try:
            report = await p.collect(self.db, [f'json-records {base}/list'], [], allow_private_sources=True,
                                     max_source_bytes=20_000, timeout=2, sleep=lambda _: asyncio.sleep(0))
        finally:
            server.close(); await server.wait_closed()
        entry = report['sources'][0]
        self.assertEqual(entry['error'], 'SOURCE_TOO_LARGE')
        self.assertEqual(entry['outcome'], 'limit_exceeded')
        self.assertNotEqual(entry['error'], 'SOURCE_ADAPTER_ERROR')

    async def test_adapter_crash_on_a_source_without_cache_is_not_reported_as_stale(self):
        body = b'[{"ip":"11.9.9.9","port":80,"protocol":"http"}]'
        async def handler(reader, writer):
            await reader.readuntil(b'\r\n\r\n')
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n' % len(body) + body)
            await writer.drain(); writer.close(); await writer.wait_closed()
        server, base = await self.serve(handler)
        with mock.patch.object(source_adapters, 'parse_page', side_effect=RuntimeError('synthetic')):
            try:
                report = await p.collect(self.db, [f'json-records {base}/list'], [], allow_private_sources=True, timeout=2)
            finally:
                server.close(); await server.wait_closed()
        entry = report['sources'][0]
        self.assertEqual(entry['error'], 'SOURCE_ADAPTER_ERROR')
        self.assertEqual(entry['cache_state'], 'none')
        self.assertEqual(self.db.execute('SELECT count(*) FROM source_generation').fetchone()[0], 0)
        runtime = source_management.runtime_snapshot(self.db)
        self.assertEqual(runtime[entry['source_id']]['cache_state'], 'none')
        observed = dict(runtime[entry['source_id']], observed_at=time.time())
        self.assertNotEqual(source_management._row_state(
            {'custom': False, 'collectable': True, 'retired': False, 'rights': {'data_license': 'unknown'},
             'access_blocked_reason': None, 'runtime': observed}, time.time()), 'stale')

    async def test_preview_reads_a_bounded_prefix_and_says_so(self):
        # A list far past the preview's own record budget: the check must stop
        # at that budget, say so, and keep what it read.
        rows = b'\n'.join(f'11.{index // 256}.{index % 256}.1:{8000 + index}'.encode()
                          for index in range(300_000))
        served = []
        async def handler(reader, writer):
            await reader.readuntil(b'\r\n\r\n')
            served.append(len(rows))
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n' % len(rows) + rows)
            await writer.drain(); writer.close(); await writer.wait_closed()
        server, base = await self.serve(handler)
        try:
            report = await p.preview_collect(self.db, [f'http {base}/list'], 20, allow_private_sources=True)
        finally:
            server.close(); await server.wait_closed()
        view = source_management.preview_view(report, 'bounded', 'bounded')
        self.assertEqual(served, [len(rows)])
        self.assertTrue(view['truncated'])
        self.assertEqual(view['limits'], {'max_bytes': p.PREVIEW_MAX_BYTES,
                                          'max_candidates': p.PREVIEW_MAX_CANDIDATES})
        self.assertEqual(view['error'], 'SOURCE_CANDIDATE_LIMIT')
        self.assertEqual(view['outcome'], 'limit_exceeded')
        self.assertEqual(len(view['sample']), 20)
        # A list well inside the bound is read and counted in full, and is not
        # reported as truncated.
        small = json.dumps([{'ip': '11.1.1.1', 'port': 8080, 'protocol': 'http'},
                            {'ip': '11.1.1.2', 'port': 8080, 'protocol': 'http'}]).encode()

        async def small_handler(reader, writer):
            await reader.readuntil(b'\r\n\r\n')
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n' % len(small) + small)
            await writer.drain(); writer.close(); await writer.wait_closed()

        server, base = await self.serve(small_handler)
        try:
            report = await p.preview_collect(self.db, [f'json-records {base}/list'], 20, allow_private_sources=True)
        finally:
            server.close(); await server.wait_closed()
        view = source_management.preview_view(report, 'small', 'small')
        self.assertFalse(view['truncated'])
        self.assertIsNone(view['error'])
        self.assertEqual(view['accepted'], 2)
        self.assertEqual(view['outcome'], 'available')

    async def test_preview_reads_at_most_the_collectors_own_byte_budget(self):
        # A check must be able to answer for every list the collector can read,
        # and must never read past its own byte budget either: past it the
        # preview keeps the prefix, counts what it contains, and reports the
        # truncation as a fact about the check, not about the source.
        self.assertEqual(p.PREVIEW_MAX_BYTES, p.DEFAULT_SOURCE_MAX_BYTES)
        padding = b'#' + b'x' * 65_534 + b'\n'
        body = b'\n'.join([b'11.1.1.1:8080', b'11.1.1.2:8080', b'11.1.1.3:8080']) + b'\n'
        body += padding * ((p.PREVIEW_MAX_BYTES // len(padding)) + 2)
        self.assertGreater(len(body), p.PREVIEW_MAX_BYTES)
        async def handler(reader, writer):
            await reader.readuntil(b'\r\n\r\n')
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n' % len(body))
            try:
                for start in range(0, len(body), 1 << 20):
                    writer.write(body[start:start + (1 << 20)])
                    await writer.drain()
            except (ConnectionResetError, BrokenPipeError, OSError):
                pass  # the check stopped reading at its bound; that is the point
            finally:
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()
        server, base = await self.serve(handler)
        try:
            report = await p.preview_collect(self.db, [line_plan(base + '/list')], 60, allow_private_sources=True)
        finally:
            server.close(); await server.wait_closed()
        view = source_management.preview_view(report, 'huge', 'huge')
        self.assertEqual(view['error'], 'SOURCE_TRUNCATED')
        self.assertEqual(view['outcome'], 'partial')
        self.assertTrue(view['truncated'])
        self.assertTrue(view['partial'])
        self.assertFalse(view['complete'])
        self.assertEqual(view['http_state'], 'http_2xx_nonempty')
        self.assertEqual(view['bytes'], p.PREVIEW_MAX_BYTES)
        self.assertEqual(view['recognized'], 3)
        self.assertEqual(view['accepted'], 3)
        self.assertEqual(view['limits'], {'max_bytes': p.PREVIEW_MAX_BYTES,
                                          'max_candidates': p.PREVIEW_MAX_CANDIDATES})

    async def test_a_truncated_body_is_not_reported_as_an_unreadable_format(self):
        # The prefix of a JSON list is not valid JSON, and that is the check's
        # doing: the source must not be told its format could not be read.
        record = b'{"ip":"11.1.1.1","port":8080,"protocol":"http"}'
        body = b'[' + (record + b',') * ((p.PREVIEW_MAX_BYTES // len(record)) + 2)
        self.assertGreater(len(body), p.PREVIEW_MAX_BYTES)
        async def handler(reader, writer):
            await reader.readuntil(b'\r\n\r\n')
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n' % len(body))
            try:
                for start in range(0, len(body), 1 << 20):
                    writer.write(body[start:start + (1 << 20)])
                    await writer.drain()
            except (ConnectionResetError, BrokenPipeError, OSError):
                pass
            finally:
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()
        server, base = await self.serve(handler)
        try:
            report = await p.preview_collect(self.db, [f'json-records {base}/list'], 60, allow_private_sources=True)
        finally:
            server.close(); await server.wait_closed()
        view = source_management.preview_view(report, 'huge-json', 'huge-json')
        self.assertEqual(view['error'], 'SOURCE_TRUNCATED')
        self.assertNotEqual(view['outcome'], 'invalid_json')
        self.assertEqual(view['parse_state'], 'partial')
        self.assertEqual(view['http_state'], 'http_2xx_nonempty')
        self.assertTrue(view['truncated'])
        self.assertEqual(view['bytes'], p.PREVIEW_MAX_BYTES)

    async def test_a_collect_refuses_a_body_past_its_byte_budget_instead_of_reading_it(self):
        # The preview's prefix mode must not leak into a real run: a collect
        # still refuses the whole body, so a public list cannot fill memory.
        body = b'11.1.1.1:8080\n' * 10
        async def handler(reader, writer):
            await reader.readuntil(b'\r\n\r\n')
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n' % len(body) + body)
            await writer.drain(); writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
        server, base = await self.serve(handler)
        try:
            for value, counter in ((f'http {base}/list', 'rows'), (line_plan(base + '/list'), 'recognized')):
                with self.subTest(kind=repr(value)[:12]):
                    report = await p.collect(self.db, [value], [], allow_private_sources=True,
                                             max_source_bytes=8)
                    entry = report['sources'][0]
                    self.assertEqual(entry['error'], 'SOURCE_TOO_LARGE')
                    self.assertEqual(entry[counter], 0)
                    if isinstance(value, dict):
                        self.assertEqual(entry['outcome'], 'limit_exceeded')
        finally:
            server.close(); await server.wait_closed()

    async def test_the_built_in_line_reader_keeps_a_prefix_in_preview_mode_only(self):
        # The same reader, in both modes: a check keeps the lines it read and
        # says the list was not read whole, a collect refuses the body.
        body = b'11.1.1.1:8080\n11.1.1.2:8080\n11.1.1.3:8080\n# padding\n' * 4
        async def handler(reader, writer):
            await reader.readuntil(b'\r\n\r\n')
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n' % len(body) + body)
            await writer.drain(); writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
        server, base = await self.serve(handler)
        try:
            bounded = await p._collect_legacy(self.db, [f'http {base}/list'], [], 20, None, None,
                                              allow_private_sources=True, max_source_bytes=14,
                                              bounded_prefix=True)
            entry = bounded['sources'][0]
            self.assertEqual(entry['error'], 'SOURCE_TRUNCATED')
            self.assertFalse(entry['complete'])
            self.assertEqual(entry['rows'], 1)
            self.assertEqual(entry['http_state'], 'http_2xx_nonempty')
            strict = await p._collect_legacy(self.db, [f'http {base}/list'], [], 20, None, None,
                                             allow_private_sources=True, max_source_bytes=14)
            self.assertEqual(strict['sources'][0]['error'], 'SOURCE_TOO_LARGE')
            self.assertEqual(strict['sources'][0]['rows'], 0)
        finally:
            server.close(); await server.wait_closed()



class ScopeDigestTests(unittest.TestCase):
    def test_cli_and_gui_compute_the_same_digest_for_the_same_scope(self):
        # ``''.split(',')`` and ``geoip.parse_countries('')`` describe the same
        # empty country filter; they must not produce two different scopes, or
        # an exclusion written by one of them is invisible to the other.
        settings = gui.defaults()
        self.assertEqual(settings['countries'], '')
        self.assertEqual(p.scope_digest_for(settings['protocol'], settings['countries'].split(','),
                                            settings['max_latency'], settings['exclude_hosting']),
                         p.scope_digest_for(settings['protocol'], geoip.parse_countries(settings['countries']),
                                            settings['max_latency'], settings['exclude_hosting']))
        self.assertEqual(p.scope_digest_for('all', [' DE ', '', 'nl']),
                         p.scope_digest_for('all', geoip.parse_countries('DE,NL')))
        self.assertNotEqual(p.scope_digest_for('all', []), p.scope_digest_for('all', ['de']))
        self.assertNotEqual(p.scope_digest_for('socks5', []), p.scope_digest_for('http', []))


class HtmlTableShapeTests(unittest.TestCase):
    HEADER = ('<html><body><table><tr><td>layout</td></tr></table>'
              '<table><tr><th>IP Address</th><th>Port</th><th>Type</th></tr>'
              '<tr><td>104.25.107.155</td><td>80</td><td>http</td></tr>'
              '<tr><td>104.21.18.115</td><td>8080</td><td>SOCKS4 SOCKS5</td></tr></table></body></html>')

    def test_declared_columns_read_a_table_that_is_not_the_first_one(self):
        profile = {'kind': 'html-table', 'profile': 'generic-html-v1',
                   'config': {'table_index': 2, 'columns': {'ip': 'IP Address', 'port': 'Port',
                                                            'protocol': 'Type'}}}
        parsed = source_adapters.parse_page(self.HEADER.encode(), profile, {}, {'max_records': 100})
        self.assertEqual([record['value'] for record in parsed['records']],
                         ['104.25.107.155:80', '104.21.18.115:8080'])
        self.assertEqual(parsed['records'][1]['values'],
                         ['socks4://104.21.18.115:8080', 'socks5://104.21.18.115:8080'])

    def test_a_layout_table_before_the_target_does_not_count_towards_the_depth_budget(self):
        deep = ''.join('<div>' for _ in range(400))
        page = (f'<html><body><div>{deep}</div>'
                '<table><tr><th>IP</th><th>Port</th></tr><tr><td>104.25.107.155</td><td>80</td></tr></table>'
                f'<div>{deep}</div></body></html>')
        profile = {'kind': 'html-table', 'profile': 'generic-html-v1',
                   'config': {'columns': {'ip': 'IP', 'port': 'Port'}}}
        parsed = source_adapters.parse_page(page.encode(), profile, {}, {'max_records': 100})
        self.assertEqual([record['value'] for record in parsed['records']], ['104.25.107.155:80'])

    def test_the_depth_budget_still_guards_the_table_itself(self):
        deep = ''.join('<div>' for _ in range(400))
        page = f'<html><body><table><tr><td>{deep}104.25.107.155:80</td></tr></table></body></html>'
        profile = {'kind': 'html-table', 'profile': 'generic-html-v1', 'config': {}}
        with self.assertRaises(source_adapters.AdapterError) as raised:
            source_adapters.parse_page(page.encode(), profile, {}, {'max_records': 100})
        self.assertEqual(raised.exception.code, 'SOURCE_HTML_DEPTH')

    def test_a_nested_table_does_not_shift_the_target_ordinal(self):
        page = ('<html><body><table><tr><td>'
                '<table><tr><th>IP</th><th>Port</th></tr><tr><td>104.25.107.155</td><td>80</td></tr></table>'
                '</td></tr></table><table><tr><td>footer</td></tr></table></body></html>')
        profile = {'kind': 'html-table', 'profile': 'generic-html-v1',
                   'config': {'table_index': 1, 'columns': {'ip': 'IP', 'port': 'Port'}}}
        parsed = source_adapters.parse_page(page.encode(), profile, {}, {'max_records': 100})
        self.assertEqual([record['value'] for record in parsed['records']], ['104.25.107.155:80'])

    def test_json_envelope_carrying_a_table_fragment_is_read_as_markup_only(self):
        payload = json.dumps({'table_html': '<tr><td>104.25.107.155</td><td>80</td><td>HTTP</td></tr>',
                              'page': 1})
        profile = {'kind': 'html-table', 'profile': 'generic-html-v1',
                   'config': {'json_html_field': 'table_html', 'row_fragment': True,
                              'columns': {'ip': 0, 'port': 1, 'protocol': 2}}}
        parsed = source_adapters.parse_page(payload.encode(), profile, {}, {'max_records': 100})
        self.assertEqual([record['value'] for record in parsed['records']], ['104.25.107.155:80'])
        missing = {'kind': 'html-table', 'profile': 'generic-html-v1',
                   'config': {'json_html_field': 'nothing_here', 'row_fragment': True,
                              'columns': {'ip': 0, 'port': 1}}}
        with self.assertRaises(source_adapters.AdapterError) as raised:
            source_adapters.parse_page(payload.encode(), missing, {}, {'max_records': 100})
        self.assertEqual(raised.exception.code, 'SOURCE_JSON_SHAPE')

    def test_a_malformed_line_is_rejected_without_killing_the_source(self):
        body = b'socks5://43.164.131.149:7777\ttime\t[room] Korea\nsocks5://43.164.132.228:7777\n'
        profile = {'kind': 'line', 'profile': 'auto-v1',
                   'config': {'legacy_kind': 'auto', 'line_address': 'first-token'}}
        parsed = source_adapters.parse_page(body, profile, {}, {'max_records': 100})
        self.assertEqual([record['value'] for record in parsed['records']],
                         ['socks5://43.164.131.149:7777', 'socks5://43.164.132.228:7777'])
        # Without the declared shape the line keeps its trailing note and the
        # bracket makes it malformed: that is one rejected line, not a source
        # whose adapter raised.
        plain = {'kind': 'line', 'profile': 'auto-v1', 'config': {'legacy_kind': 'auto'}}
        parsed = source_adapters.parse_page(body, plain, {}, {'max_records': 100})
        self.assertEqual([record['value'] for record in parsed['records']], ['socks5://43.164.132.228:7777'])
        self.assertEqual(parsed['rejects']['invalid_address'], 1)



class CatalogSetsTest(unittest.TestCase):
    """The shipped sets must be filled and must agree with the source fields."""

    def setUp(self):
        self.catalog = source_catalog.load_bundled()
        self.sources = {row['id']: row for row in self.catalog['sources']}
        self.sets = {row['id']: row for row in self.catalog['sets']}

    def test_derived_sets_are_not_empty(self):
        for name in ('quick', 'extended', 'experimental'):
            self.assertTrue(self.sets.get(name, {}).get('members'), f'набор {name} пуст')

    def test_no_set_contains_a_source_that_may_not_be_collected(self):
        for name, row in self.sets.items():
            for member in row.get('members') or []:
                self.assertIn(member, self.sources, f'{name}: неизвестный источник {member}')
                self.assertTrue(self.sources[member].get('collection_allowed'),
                                f'{name}: {member} помечен как несобираемый')

    def test_derived_sets_never_contain_account_gated_sources(self):
        for name in ('quick', 'extended'):
            for member in self.sets.get(name, {}).get('members') or []:
                access = self.sources[member].get('access') or {}
                self.assertFalse(access.get('account_required'),
                                 f'{name}: {member} требует аккаунт или ключ')

    def test_experimental_members_are_the_unproven_collectable_tail(self):
        # The experimental set is the opt-in tail: collectable, but with nothing
        # in this project having measured what it is worth yet.
        quick = {member for member in self.sets.get('quick', {}).get('members') or []}
        for member in self.sets.get('experimental', {}).get('members') or []:
            source = self.sources[member]
            self.assertNotIn(member, quick, f'{member} уже входит в базовый набор')
            self.assertEqual(source.get('payload_role'), 'proxy_list')
            self.assertIn(source.get('priority'), ('low', 'experimental'),
                          f'{member} не относится к недоказанному хвосту')

    def test_source_sets_field_agrees_with_set_definitions(self):
        for member, source in self.sources.items():
            expected = {name for name, row in self.sets.items()
                        if member in (row.get('members') or []) and not name.startswith('custom')}
            self.assertEqual(set(source.get('sets') or []), expected,
                             f'{member}: поле sets не совпадает с определениями наборов')


if __name__ == '__main__':
    unittest.main()
