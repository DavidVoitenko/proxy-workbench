import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import proxytool as p
from proxy_workbench import source_adapters
from proxy_workbench import source_catalog
from proxy_workbench import source_management


RESEARCH_DIR = Path(__file__).resolve().parents[2] / 'proxy-sources-research-2026-09-25'
SAMPLES = RESEARCH_DIR / 'samples'
REQUIRED_SAMPLES = (
    'cur-01', 'cur-08', 'cur-19', 'cur-42', 'cur-43', 'cur-47', 'cur-52', 'cur-55',
    'new-009', 'new-010', 'new-026', 'new-045',
)


class ResearchCollectIntegrationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        missing = [sample for sample in REQUIRED_SAMPLES if not (SAMPLES / f'{sample}.bin').is_file()]
        if missing:
            raise unittest.SkipTest(f'исходные образцы исследования не найдены: {", ".join(missing)}')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = p.open_db(Path(self.temp.name) / 'collect.sqlite3')
        self.requests = []

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def sample(self, source_id):
        return (SAMPLES / f'{source_id}.bin').read_bytes()

    async def serve(self, pages):
        async def handler(reader, writer):
            try:
                request_line = await reader.readuntil(b'\r\n\r\n')
                target = request_line.split(b' ', 2)[1].decode('ascii')
                parsed = urlsplit(target)
                self.requests.append(target)
                if parsed.path == '/page-json' and parse_qs(parsed.query).get('page') == ['2']:
                    body = json.dumps({'data': [], 'page': 2, 'limit': 500, 'total': 162}).encode()
                elif parsed.path == '/geonode' and parse_qs(parsed.query).get('page') == ['2']:
                    body = json.dumps({'data': [], 'page': 2, 'limit': 500, 'total': 3088}).encode()
                else:
                    body = pages[parsed.path]
                writer.write(
                    b'HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n'
                    b'Content-Length: %d\r\nConnection: close\r\n\r\n' % len(body) + body
                )
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(handler, '127.0.0.1', 0)
        base = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'
        return server, base

    def candidates(self):
        return sorted(row[0] for row in self.db.execute('SELECT proxy FROM candidates'))

    async def test_planned_formats_are_collect_kinds(self):
        pages = {
            '/json-records': self.sample('new-009'),
            '/fields': self.sample('new-026'),
            '/page-json': self.sample('new-045'),
            '/html-table': self.sample('new-010'),
        }
        server, base = await self.serve(pages)
        async with server:
            report = await p.collect(self.db, [
                f'json-records {base}/json-records',
                f'fields {base}/fields',
                f'page-json {base}/page-json',
                f'html-table {base}/html-table',
            ], [], allow_private_sources=True, max_pages=1)
        by_index = {item['source']: item for item in report['sources']}
        self.assertEqual([by_index[index]['format'] for index in range(1, 5)],
                         ['json-records', 'fields', 'page-json', 'html-table'])
        self.assertTrue(by_index[1]['complete'])
        self.assertTrue(by_index[2]['complete'])
        self.assertIn(by_index[3]['error'], ('SOURCE_PAGE_LIMIT', 'SOURCE_PAGINATION_EMPTY'))
        self.assertGreater(by_index[1]['accepted'], 0)
        self.assertGreater(by_index[2]['accepted'], 0)
        self.assertGreater(by_index[4]['accepted'], 0)
        self.assertTrue(self.candidates())

    async def test_all_existing_source_kinds_accept_real_saved_samples(self):
        pages = {
            '/http': self.sample('cur-01'),
            '/https': self.sample('cur-19'),
            '/socks4': self.sample('cur-47'),
            '/socks5': self.sample('cur-43'),
            '/socks5h': self.sample('cur-47'),
            '/auto': self.sample('cur-42'),
            '/text': self.sample('cur-52'),
            '/http-fields': self.sample('cur-08'),
            '/geonode': self.sample('cur-55'),
        }
        expected = {
            'http': '/http',
            'https': '/https',
            'socks4': '/socks4',
            'socks5': '/socks5',
            'socks5h': '/socks5h',
            'auto': '/auto',
            'text': '/text',
            'geonode': '/geonode',
            'http-fields': '/http-fields',
        }
        server, base = await self.serve(pages)
        specs = [f'{kind} {base}{path}' for kind, path in expected.items()]
        async with server:
            report = await p.collect(self.db, specs, [], allow_private_sources=True)

        self.assertTrue(set(expected).issubset(p.SOURCE_KINDS))
        by_index = {item['source']: item for item in report['sources']}
        keys = {kind: p.source_key(spec) for spec, kind in zip(specs, expected)}
        accepted = dict(self.db.execute(
            'SELECT source, count(*) FROM candidate_seen WHERE source IN ({}) GROUP BY source'.format(
                ','.join('?' for _ in keys)
            ),
            tuple(keys.values()),
        ))
        self.assertEqual(set(accepted), set(keys.values()))
        self.assertTrue(all(count > 0 for count in accepted.values()))
        for index, kind in enumerate(expected, 1):
            self.assertEqual(by_index[index]['format'], kind)
            if kind != 'geonode':
                self.assertTrue(by_index[index]['complete'], by_index[index])


class SyntheticCollectIntegrationTests(ResearchCollectIntegrationTests):
    """Every collection format also runs without the optional research folder."""

    @classmethod
    def setUpClass(cls):
        pass

    def sample(self, source_id):
        line = b'11.1.1.1:8080\n11.1.1.2:3128\n'
        records = [{'ip': '11.2.2.1', 'port': 8080, 'protocol': 'http', 'protocols': ['http']}]
        if source_id in ('new-009',):
            return json.dumps(records).encode()
        if source_id in ('new-045', 'cur-55'):
            return json.dumps({'data': records, 'page': 1, 'limit': 1, 'total': 2}).encode()
        if source_id == 'new-026':
            return b'ip,port,protocol,country\n11.3.3.1,8080,http,DE\n'
        if source_id in ('new-010', 'cur-52'):
            return (b'<table><tr><th>IP Address</th><th>Port</th><th>Protocol</th></tr>'
                    b'<tr><td data-ip="MTEuNC40LjE=">11.4.4.1</td>'
                    b'<td data-port="ODA4MA==">8080</td><td>http</td></tr></table>')
        if source_id == 'cur-08':
            return b'11.5.5.1:8080:Germany\n'
        return line


class CatalogHonestyTests(unittest.TestCase):
    """A source the app cannot read must not be offered as an ordinary list.

    Every catalog record the collector is allowed to download is run through
    the adapter of its saved research sample.  A record that yields nothing has
    to say why, so the GUI shows a reason instead of offering "Add to the set"
    for a page whose addresses are rendered by JavaScript.
    """

    @classmethod
    def setUpClass(cls):
        if not SAMPLES.is_dir():
            raise unittest.SkipTest('образцы исследования не найдены')

    def collectable_records(self):
        catalog = source_catalog.load_bundled()
        for source in catalog['sources']:
            path = SAMPLES / f"{source['id']}.bin"
            if not path.is_file():
                continue
            if not source_catalog.collectable_source(source) or source_management.not_proxy_reason(source):
                continue
            yield source, path

    def test_every_collectable_source_yields_addresses_from_its_own_sample(self):
        empty = []
        for source, path in self.collectable_records():
            parsed = source_adapters.parse_page(path.read_bytes(), dict(source['adapter']),
                                                {'page': 1, 'url': source['data_urls'][0]},
                                                {'max_records': 2_000_000, 'max_bytes': 64 * 1024 * 1024})
            if not parsed['records']:
                empty.append((source['id'], parsed['state']))
        # Only sources the research itself found empty may stay empty: a list
        # that was empty when it was checked, not one the adapter cannot read.
        self.assertEqual([source_id for source_id, _ in empty], ['cur-37', 'new-086'])

    def test_sources_the_app_cannot_read_say_why_instead_of_looking_ordinary(self):
        # The address or the port of each of these is written by a
        # document.write() the parser must not execute, or the page is rendered
        # client-side and carries no rows at all.
        catalog = source_catalog.load_bundled()
        for source_id in ('new-011', 'new-012', 'new-050', 'new-052', 'new-057', 'new-065'):
            source = source_catalog.source_by_id(catalog, source_id)
            with self.subTest(source=source_id):
                self.assertFalse(source_catalog.collectable_source(source))
                self.assertEqual(source['adapter']['kind'], 'unsupported')
                self.assertEqual(source_management.not_proxy_reason(source), 'no_adapter')
                row = source_management.public_row(source)
                self.assertFalse(row['collectable'])
                self.assertTrue(row['not_proxy_source_reason'])
        # The research recorded an HTTP 404 for new-053: the snapshot itself is
        # unavailable, which is a different reason and states it.
        missing = source_catalog.source_by_id(catalog, 'new-053')
        self.assertFalse(source_catalog.collectable_source(missing))
        self.assertEqual(missing['access']['kind'], 'snapshot_unavailable')
        self.assertTrue(source_management.public_row(missing)['access_blocked_reason'])

    def test_a_readable_table_source_names_the_columns_it_reads(self):
        # A table source is offered as readable only when the adapter reproduces
        # the first record the research observed from the same saved sample.
        # Sources whose layout could not be confirmed must say so instead.
        catalog = source_catalog.load_bundled()
        expected = {
            'new-016': '104.25.107.155:80',
            'new-051': '104.225.220.233:80',
            'new-064': '18.162.158.218:80',
            'new-066': '218.92.227.173:14826',
        }
        for source_id, first in expected.items():
            source = source_catalog.source_by_id(catalog, source_id)
            with self.subTest(source=source_id):
                body = (SAMPLES / f'{source_id}.bin').read_bytes()
                if source['adapter']['kind'] != 'html-table':
                    self.assertFalse(source_catalog.collectable_source(source))
                    self.assertTrue(source_management.not_proxy_reason(source),
                                    f'{source_id}: нечитаемый источник обязан называть причину')
                    continue
                parsed = source_adapters.parse_page(body, dict(source['adapter']),
                                                    {'page': 1, 'url': source['data_urls'][0]},
                                                    {'max_records': 2_000_000, 'max_bytes': 64 * 1024 * 1024})
                self.assertTrue(parsed['records'], f'{source_id}: адаптер ничего не прочитал')
                self.assertEqual(parsed['records'][0]['value'], first)


if __name__ == '__main__':
    unittest.main()
