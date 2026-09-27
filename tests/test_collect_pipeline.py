"""Collection path: line formats, batched writes, limits and validators."""
import asyncio
import contextlib
import ipaddress
from pathlib import Path
import random
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import proxytool as p
from proxy_workbench import source_adapters


def line_record(url, source_id, config):
    return {'id': source_id, 'name': source_id, 'publisher': {'id': 'local', 'name': 'local'},
            'family_id': source_id, 'category': 'custom', 'endpoints': [
                {'id': 'primary', 'url': url, 'role': 'primary', 'relation': 'custom'}],
            'data_urls': [url], 'fallback_urls': [], 'protocols': [], 'protocol_hints': [],
            'adapter': {'kind': 'line', 'profile': 'line-v1', 'config': config},
            'access': {'kind': 'public'}, 'collection_allowed': True, 'payload_role': 'proxy_list',
            'evidence': {}, 'limits': {}, 'rights': {}, 'tags': []}


class NormalizeFastPathTests(unittest.TestCase):
    def test_fast_path_matches_the_general_parser(self):
        rng = random.Random(7)
        octets = ['0', '1', '9', '10', '01', '99', '100', '127', '169', '172', '192', '224', '254', '255', '256', '999']
        ports = ['0', '1', '80', '080', '8080', '65535', '65536', '99999', '000080', '']
        schemes = ['', 'http://', 'https://', 'socks4://', 'socks5://', 'socks5h://', 'HTTP://', 'ftp://']
        values = []
        for _ in range(20_000):
            host = '.'.join(rng.choice(octets) for _ in range(rng.choice((3, 4, 4, 4, 5))))
            port = rng.choice(ports)
            value = rng.choice(schemes) + host + (':' + port if port else '') + rng.choice(('', '', '/', ' ', '/x'))
            values.append(value)
        values += ['8.8.8.8:80', ' 8.8.8.8:80 ', '١.٢.٣.٤:80', '8.8.8.8:８０', '[2001:4860::8888]:80',
                   '100.64.0.1:80', '198.18.0.1:80', 'socks4://8.8.8.8:1080']
        for value in values:
            for public_only in (True, False):
                self.assertEqual(p._normalize_proxy(value, public_only=public_only),
                                 p._normalize_proxy_general(value, public_only=public_only), value)

    def test_fast_path_answers(self):
        self.assertEqual(p.normalize('8.8.8.8:0080'), 'http://8.8.8.8:80')
        self.assertIsNone(p.normalize('10.0.0.1:80'))
        self.assertEqual(p.normalize_custom('10.0.0.1:80'), 'http://10.0.0.1:80')
        self.assertTrue(ipaddress.ip_address('8.8.8.8').is_global)


class CollectFixture(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = p.open_db(Path(self.temp.name) / 'collect.sqlite3')

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    @contextlib.asynccontextmanager
    async def serve(self, pages, headers=None, statuses=None, seen=None):
        async def handler(reader, writer):
            try:
                request = await reader.readuntil(b'\r\n\r\n')
                path = request.split(b' ', 2)[1].decode('ascii')
                if seen is not None:
                    seen.append(request.decode('latin-1'))
                status = (statuses or {}).get(path, 200)
                body = pages.get(path, b'') if status == 200 else b''
                extra = ''.join(f'{name}: {value}\r\n' for name, value in (headers or {}).get(path, {}).items())
                reason = {200: 'OK', 304: 'Not Modified'}.get(status, 'X')
                writer.write(f'HTTP/1.1 {status} {reason}\r\n{extra}Content-Length: {len(body)}\r\n'
                             'Connection: close\r\n\r\n'.encode() + body)
                await writer.drain()
            except (ConnectionError, asyncio.IncompleteReadError):
                pass
            finally:
                writer.close()
                with contextlib.suppress(ConnectionError):
                    await writer.wait_closed()
        server = await asyncio.start_server(handler, '127.0.0.1', 0)
        try:
            yield f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'
        finally:
            server.close()
            await server.wait_closed()

    def candidates(self):
        return sorted(row[0] for row in self.db.execute('SELECT proxy FROM candidates'))


class CollectPipelineTests(CollectFixture):
    async def test_line_records_honor_their_legacy_kind_and_inline_comments(self):
        pages = {
            '/fields': '11.5.5.1:8080:Germany\n11.5.5.2:3128:Türkiye\n11.5.5.3:80:\n'.encode(),
            '/text': b'Proxy list header\n11.6.6.1:80 HK-H - \n11.6.6.2:8080 US-N-S +\n',
            '/commented': b'11.7.7.1:8085 # HTTP [ID]\n# whole comment\n11.7.7.2:80\n',
        }
        async with self.serve(pages) as base:
            report = await p.collect(self.db, [], [], plans=[
                line_record(base + '/fields', 'fields', {'legacy_kind': 'http-fields'}),
                line_record(base + '/text', 'text', {'legacy_kind': 'text'}),
                line_record(base + '/commented', 'commented', {'legacy_kind': 'http', 'default_protocol': 'http'}),
            ], allow_private_sources=True, quiet=True)
        by_id = {entry['source_id']: entry for entry in report['sources']}
        self.assertEqual(by_id['fields']['accepted'], 2)
        self.assertEqual(by_id['fields']['invalid'], 1)
        self.assertEqual(by_id['text']['accepted'], 2)
        self.assertEqual(by_id['commented']['accepted'], 2)
        self.assertEqual(self.candidates(), [
            'http://11.5.5.1:8080', 'http://11.5.5.2:3128', 'http://11.6.6.1:80',
            'http://11.6.6.2:8080', 'http://11.7.7.1:8085', 'http://11.7.7.2:80'])
        parsed = source_adapters.parse_line(pages['/fields'], {'config': {'legacy_kind': 'http-fields'}})
        self.assertEqual([record['value'] for record in parsed['records']], ['11.5.5.1:8080', '11.5.5.2:3128'])
        parsed = source_adapters.parse_line(pages['/commented'], {'config': {'legacy_kind': 'http'}})
        self.assertEqual([record['value'] for record in parsed['records']], ['11.7.7.1:8085', '11.7.7.2:80'])

    async def test_batched_writer_keeps_counts_membership_and_provenance(self):
        first = ''.join(f'11.1.{index // 250}.{index % 250 + 1}:8080\n' for index in range(2500))
        second = ''.join(f'11.1.{index // 250}.{index % 250 + 1}:8080\n' for index in range(2000, 3000))
        pages = {'/a': (first + first[:200]).encode(), '/b': second.encode()}
        original = p.COLLECT_WRITE_BATCH
        p.COLLECT_WRITE_BATCH = 300  # several flushes per source
        try:
            async with self.serve(pages) as base:
                report = await p.collect(self.db, [f'http {base}/a'], [], allow_private_sources=True, quiet=True)
                later = await p.collect(self.db, [f'http {base}/b'], [], allow_private_sources=True, quiet=True)
        finally:
            p.COLLECT_WRITE_BATCH = original
        entry, other = report['sources'][0], later['sources'][0]
        self.assertEqual((entry['accepted'], entry['new'], entry['duplicate']), (2500, 2500, 14))
        self.assertEqual((other['accepted'], other['new']), (1000, 500))
        self.assertEqual(later['unique'], 3000)
        for table in ('endpoints', 'membership'):
            self.assertEqual(self.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 3000)
        self.assertEqual(self.db.execute('SELECT count(*) FROM candidate_seen').fetchone()[0], 3500)
        self.assertEqual(self.db.execute('SELECT count(*) FROM membership_source').fetchone()[0], 3500)
        # The first source that named an address stays its first-seen source.
        self.assertEqual(self.db.execute('SELECT count(DISTINCT source) FROM candidate_meta').fetchone()[0], 2)
        self.assertEqual(self.db.execute(
            'SELECT count(*) FROM candidate_meta WHERE source=?', (entry['source_id'],)).fetchone()[0], 2500)
        self.assertEqual(self.db.execute(
            'SELECT count(*) FROM source_generation_entry').fetchone()[0], 3500)

    async def test_a_list_without_countries_keeps_an_earlier_country(self):
        self.db.execute("INSERT INTO endpoints(id, canonical, country, country_source) VALUES (?,?,?,?)",
                        (p.schema.endpoint_id('http://11.8.8.1:80'), 'http://11.8.8.1:80', 'DE', 'import'))
        self.db.commit()
        async with self.serve({'/l': b'11.8.8.1:80\n'}) as base:
            await p.collect(self.db, [f'http {base}/l'], [], allow_private_sources=True, quiet=True)
        self.assertEqual(tuple(self.db.execute(
            'SELECT country, country_source FROM endpoints WHERE canonical=?', ('http://11.8.8.1:80',)).fetchone()),
            ('DE', 'import'))

    async def test_item_budget_still_stops_at_the_exact_address(self):
        body = ''.join(f'11.2.0.{index}:80\n' for index in range(1, 51)).encode()
        async with self.serve({'/l': body}) as base:
            report = await p.collect(self.db, [f'http {base}/l'], [], allow_private_sources=True,
                                     quiet=True, max_items=7)
        self.assertEqual(report['budget']['items'], 7)
        self.assertEqual(self.db.execute('SELECT count(*) FROM membership').fetchone()[0], 7)

    async def test_candidate_limit_is_a_partial_read_not_a_provider_failure(self):
        body = ''.join(f'11.3.0.{index}:80\n' for index in range(1, 21)).encode()
        async with self.serve({'/l': body}, headers={'/l': {'ETag': '"big"'}}) as base:
            spec = f'http {base}/l'
            for _ in range(p.SOURCE_QUARANTINE_AFTER + 1):
                report = await p.collect(self.db, [spec], [], allow_private_sources=True,
                                         quiet=True, max_source_candidates=5)
                entry = report['sources'][0]
                self.assertEqual(entry['error'], 'SOURCE_CANDIDATE_LIMIT')
                self.assertEqual(entry['http_state'], 'http_2xx_nonempty')
                self.assertEqual(entry['parse_state'], 'partial')
                self.assertTrue(entry['partial'])
        state = p._source_state_row(self.db, entry['source_id'])
        self.assertEqual(state['consecutive_failures'], 0)
        self.assertIsNone(state['quarantine_until'])
        self.assertIsNone(state['backoff_until'])
        # The body was not read whole, so its validator is not stored.
        self.assertIsNone(state['etag'])

    async def test_validators_survive_a_304_without_them_and_a_failed_fetch(self):
        requests = []
        pages = {'/l': b'11.4.0.1:80\n'}
        headers = {'/l': {'ETag': '"v1"', 'Last-Modified': 'Sat, 26 Sep 2026 00:00:00 GMT'}}
        async with self.serve(pages, headers=headers, seen=requests) as base:
            spec = f'http {base}/l'
            await p.collect(self.db, [spec], [], allow_private_sources=True, quiet=True)
        key = p.source_key(spec)
        self.assertEqual(p._source_state_row(self.db, key)['etag'], '"v1"')
        async with self.serve(pages, statuses={'/l': 304}, seen=requests) as base2:
            # The same source id, now answering 304 without any validator.
            report = await p.collect(self.db, [], [], plans=[p.SourcePlan(key, base2 + '/l', 'http')],
                                     allow_private_sources=True, quiet=True)
        self.assertTrue(report['sources'][0].get('served_from_cache'))
        self.assertIn('If-None-Match: "v1"', requests[-1])
        state = p._source_state_row(self.db, key)
        self.assertEqual((state['etag'], state['last_modified']), ('"v1"', 'Sat, 26 Sep 2026 00:00:00 GMT'))
        # A body the collector refused must not replace the stored validator.
        bad = {'/l': b'\xff\xfe not utf-8\n'}
        async with self.serve(bad, headers={'/l': {'ETag': '"broken"'}}) as base3:
            report = await p.collect(self.db, [], [], plans=[p.SourcePlan(key, base3 + '/l', 'http')],
                                     allow_private_sources=True, quiet=True)
        self.assertEqual(report['sources'][0]['error'], 'SOURCE_INVALID_UTF8')
        self.assertEqual(p._source_state_row(self.db, key)['etag'], '"v1"')


class SourceHistoryTests(CollectFixture):
    """Generations stay bounded; 304 re-applies; retries and pages count per attempt."""

    def entries(self):
        return self.db.execute('SELECT count(*) FROM source_generation_entry').fetchone()[0]

    def generations(self, source_id):
        return [row[0] for row in self.db.execute(
            'SELECT id FROM source_generation WHERE source_id=? ORDER BY id', (source_id,))]

    @staticmethod
    def listing(run, count, offset=0):
        # One address changes per run, so every run is a new answer (a 200, not a 304).
        rows = [f'11.20.{(offset + index) // 250}.{(offset + index) % 250 + 1}:8080' for index in range(count)]
        rows[-1] = f'11.21.{run // 250}.{run % 250 + 1}:8080'
        return ('\n'.join(rows) + '\n').encode()

    async def test_generations_stay_bounded_across_many_runs_and_comparisons_hold(self):
        keep = p.schema.SOURCE_GENERATION_KEEP
        pages = {}
        async with self.serve(pages) as base:
            specs = [f'http {base}/a', f'http {base}/b']
            ids = [p.source_key(spec) for spec in specs]
            for run in range(keep + 6):
                pages['/a'] = self.listing(run, 300)            # 11.20.0.1 .. 11.20.1.50
                pages['/b'] = self.listing(1000 + run, 200, offset=200)  # overlaps /a by 99
                report = await p.collect(self.db, specs, [], allow_private_sources=True, quiet=True)
                self.assertTrue(all(entry['generation_id'] for entry in report['sources']), report)
                for source_id in ids:
                    self.assertLessEqual(len(self.generations(source_id)), keep)
                # Entries of at most `keep` generations of each list, whatever the run count.
                self.assertLessEqual(self.entries(), keep * (300 + 200))
        self.assertEqual(self.entries(), keep * (300 + 200))
        for source_id in ids:
            state = p._source_state_row(self.db, source_id)
            self.assertEqual(state['last_good_generation'], self.generations(source_id)[-1])
            self.assertEqual(p.stored_generation(self.db, source_id)['generation_id'],
                             state['last_good_generation'])
        # The comparison reads the newest generation only and still adds up.
        contributions = p.source_contributions(self.db, ids)
        self.assertEqual(contributions[ids[0]]['accepted'], 300)
        self.assertEqual(contributions[ids[1]]['accepted'], 200)
        self.assertEqual(contributions[ids[0]]['shared'], 99)
        self.assertEqual(contributions[ids[1]]['shared'], 99)
        self.assertEqual(contributions[ids[0]]['exclusive'], 201)
        # Observations are bounded as well, and those a kept generation cites stay.
        self.assertEqual(p.schema.prune_source_history(self.db, keep_observations=2),
                         {'generations': 0, 'entries': 0, 'observations': 2 * (keep + 6) - 2 * keep})
        cited = {row[0] for row in self.db.execute('SELECT observation_id FROM source_generation')}
        remaining = {row[0] for row in self.db.execute('SELECT id FROM source_observation')}
        self.assertLessEqual(cited, remaining)

    async def test_retention_covers_history_left_by_an_older_version(self):
        pages = {}
        original = p.schema.SOURCE_GENERATION_KEEP
        p.schema.SOURCE_GENERATION_KEEP = 100   # an unbounded writer, as before
        try:
            async with self.serve(pages) as base:
                for run in range(6):
                    pages['/a'] = self.listing(run, 50)
                    await p.collect(self.db, [f'http {base}/a'], [], allow_private_sources=True, quiet=True)
        finally:
            p.schema.SOURCE_GENERATION_KEEP = original
        source_id = p.source_key(f'http {base}/a')
        self.assertEqual(len(self.generations(source_id)), 6)
        preview = p.schema.retention_preview(self.db)
        history = preview.to_dict()['source_history']
        self.assertEqual((history['generations'], history['entries']), (6 - original, (6 - original) * 50))
        self.assertEqual(self.entries(), 300)   # a preview deletes nothing
        report = p.schema.apply_retention(self.db)
        deleted = report.to_dict()['deleted']
        self.assertEqual(deleted['source_generation_entry'], (6 - original) * 50)
        self.assertEqual(deleted['source_generation'], 6 - original)
        self.assertEqual(len(self.generations(source_id)), original)
        self.assertEqual(p.schema.retention_preview(self.db).to_dict()['source_history']['entries'], 0)
        self.assertEqual(p.source_contributions(self.db, [source_id])[source_id]['accepted'], 50)

    async def test_304_reapplies_the_last_good_list_to_the_target_collection(self):
        body = b'11.30.0.1:80\n11.30.0.2:80\n11.30.0.3:80\n'
        headers = {'/l': {'ETag': '"v1"'}}
        async with self.serve({'/l': body}, headers=headers) as base:
            spec = f'http {base}/l'
            await p.collect(self.db, [spec], [], allow_private_sources=True, quiet=True)
        key = p.source_key(spec)
        public = p.ensure_collection(self.db, None)
        second = p.schema.create_collection(self.db, 'second')
        removed = p.schema.endpoint_id('http://11.30.0.2:80')
        p.schema.remove_member(self.db, public, removed)
        self.db.commit()

        def members(collection):
            return sorted(row[0] for row in self.db.execute(
                'SELECT e.canonical FROM membership m JOIN endpoints e ON e.id=m.endpoint_id'
                ' WHERE m.collection_id=?', (collection,)))

        expected = ['http://11.30.0.1:80', 'http://11.30.0.2:80', 'http://11.30.0.3:80']
        generations = len(self.generations(key))
        async with self.serve({'/l': body}, statuses={'/l': 304}) as base2:
            plan = p.SourcePlan(key, base2 + '/l', 'http')
            into_second = await p.collect(self.db, [], [], plans=[plan], collection_id=second,
                                          allow_private_sources=True, quiet=True)
            into_public = await p.collect(self.db, [], [], plans=[plan],
                                          allow_private_sources=True, quiet=True)
        for report in (into_second, into_public):
            entry = report['sources'][0]
            self.assertTrue(entry['served_from_cache'])
            self.assertEqual((entry['rows'], entry['accepted'], entry['recognized']), (3, 0, 0))
            self.assertTrue(entry['complete'])
        self.assertEqual(members(second), expected)
        self.assertEqual(members(public), expected)
        self.assertEqual(self.db.execute('SELECT count(*) FROM membership_source WHERE collection_id=?',
                                         (second,)).fetchone()[0], 3)
        self.assertEqual(len(self.generations(key)), generations, 'a 304 is not a new generation')

    async def test_304_reapply_respects_the_item_budget(self):
        body = ''.join(f'11.31.0.{index}:80\n' for index in range(1, 11)).encode()
        async with self.serve({'/l': body}, headers={'/l': {'ETag': '"v1"'}}) as base:
            spec = f'http {base}/l'
            await p.collect(self.db, [spec], [], allow_private_sources=True, quiet=True)
        key = p.source_key(spec)
        second = p.schema.create_collection(self.db, 'second')
        self.db.commit()
        async with self.serve({'/l': body}, statuses={'/l': 304}) as base2:
            report = await p.collect(self.db, [], [], plans=[p.SourcePlan(key, base2 + '/l', 'http')],
                                     collection_id=second, allow_private_sources=True, quiet=True,
                                     max_items=4)
        entry = report['sources'][0]
        self.assertEqual(entry['error'], 'SOURCE_ITEM_BUDGET')
        self.assertFalse(entry['complete'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM membership WHERE collection_id=?',
                                         (second,)).fetchone()[0], 4)

    async def test_a_retry_after_a_broken_download_starts_its_counters_again(self):
        body = ''.join(f'11.40.0.{index}:80\n' for index in range(1, 201)).encode()
        calls = []

        async def handler(reader, writer):
            try:
                await reader.readuntil(b'\r\n\r\n')
                calls.append(1)
                head = f'HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n'.encode()
                # The first answer breaks off two thirds into the list.
                writer.write(head + (body[:len(body) * 2 // 3] if len(calls) == 1 else body))
                await writer.drain()
            finally:
                writer.close()
                with contextlib.suppress(ConnectionError):
                    await writer.wait_closed()

        async def no_sleep(_seconds):
            return None

        server = await asyncio.start_server(handler, '127.0.0.1', 0)
        try:
            url = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/l'
            # Room for the list once, not for the broken attempt plus the list.
            report = await p.collect(self.db, [f'http {url}'], [], allow_private_sources=True, quiet=True,
                                     max_source_bytes=len(body) + 10, max_source_candidates=250,
                                     sleep=no_sleep)
        finally:
            server.close()
            await server.wait_closed()
        entry = report['sources'][0]
        self.assertEqual(len(calls), 2)
        self.assertIsNone(entry['error'], entry)
        self.assertEqual((entry['rows'], entry['accepted'], entry['duplicate']), (200, 200, 0))
        self.assertEqual(entry['bytes'], len(body))
        self.assertEqual(entry['pages'], 1)

    async def test_broken_attempt_does_not_leave_candidates_from_its_prefix(self):
        first = ''.join(f'11.43.0.{index}:80\n' for index in range(1, 21)).encode()
        replacement = b'11.44.0.1:80\n11.44.0.2:80\n'
        calls = []

        async def handler(reader, writer):
            try:
                await reader.readuntil(b'\r\n\r\n')
                calls.append(1)
                body = first if len(calls) == 1 else replacement
                head = (f'HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n'
                        'Connection: close\r\n\r\n').encode()
                writer.write(head + (first[:len(first) // 2] if len(calls) == 1 else body))
                await writer.drain()
            finally:
                writer.close()
                with contextlib.suppress(ConnectionError):
                    await writer.wait_closed()

        async def no_sleep(_seconds):
            return None

        server = await asyncio.start_server(handler, '127.0.0.1', 0)
        original = p.COLLECT_WRITE_BATCH
        p.COLLECT_WRITE_BATCH = 5
        try:
            url = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/l'
            report = await p.collect(self.db, [f'http {url}'], [], allow_private_sources=True,
                                     quiet=True, sleep=no_sleep)
        finally:
            p.COLLECT_WRITE_BATCH = original
            server.close()
            await server.wait_closed()
        self.assertEqual(len(calls), 2)
        self.assertIsNone(report['sources'][0]['error'])
        self.assertEqual(report['sources'][0]['accepted'], 2)
        self.assertEqual(self.candidates(), ['http://11.44.0.1:80', 'http://11.44.0.2:80'])
        self.assertEqual(self.db.execute('SELECT count(*) FROM membership_source').fetchone()[0], 2)

    async def test_a_source_cut_off_by_the_candidate_limit_reports_its_page(self):
        body = ''.join(f'11.41.0.{index}:80\n' for index in range(1, 21)).encode()
        async with self.serve({'/l': body}) as base:
            report = await p.collect(self.db, [f'http {base}/l'], [], allow_private_sources=True,
                                     quiet=True, max_source_candidates=5)
        entry = report['sources'][0]
        self.assertEqual(entry['error'], 'SOURCE_CANDIDATE_LIMIT')
        self.assertEqual((entry['pages'], entry['accepted']), (1, 5))

    async def test_a_json_page_is_parsed_once(self):
        import json
        from unittest import mock
        pages = {
            '/p?page=1': json.dumps({'data': [{'ip': '11.42.0.1', 'port': 80, 'protocol': 'http'}],
                                     'page': 1, 'total': 2, 'marker': 'page-body'}).encode(),
            '/p?page=2': json.dumps({'data': [{'ip': '11.42.0.2', 'port': 80, 'protocol': 'http'}],
                                     'page': 2, 'total': 2, 'marker': 'page-body'}).encode(),
        }
        profile = {'kind': 'page-json', 'profile': 'page-number-v1',
                   'config': {'records_path': 'data', 'page_path': 'page', 'total_path': 'total'}}
        real = json.loads
        parsed = []

        def counting(text, *args, **kwargs):
            if 'page-body' in (text.decode('utf-8', 'replace') if isinstance(text, bytes) else str(text)):
                parsed.append(1)
            return real(text, *args, **kwargs)

        async with self.serve(pages) as base:
            with mock.patch('json.loads', counting):
                report = await p.collect(self.db, [], [], plans=[
                    p.SourcePlan('paged', base + '/p?page=1', 'page-json', profile)],
                    allow_private_sources=True, quiet=True)
        entry = report['sources'][0]
        self.assertEqual((entry['accepted'], entry['pages'], entry['error']), (2, 2, None), entry)
        self.assertEqual(len(parsed), 2)


class NextUrlPaginationTests(unittest.TestCase):
    def test_absolute_and_relative_links_keep_their_path_and_query(self):
        profile = {'config': {'mode': 'next-url'}}
        base = 'https://example.test/api?page=1'
        self.assertEqual(source_adapters.next_page_url(base, {'next': 'https://example.test/api?page=2'}, profile, 2),
                         'https://example.test/api?page=2')
        self.assertEqual(source_adapters.next_page_url(base, {'next': '/api?page=3'}, profile, 3),
                         'https://example.test/api?page=3')
        with self.assertRaises(source_adapters.AdapterError):
            source_adapters.next_page_url(base, {'next': 'https://other.test/api?page=2'}, profile, 2)


if __name__ == '__main__':
    unittest.main()
