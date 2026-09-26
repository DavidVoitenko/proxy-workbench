import argparse
import asyncio
from contextlib import asynccontextmanager
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import proxytool as p
from proxy_workbench import source_adapters
from proxy_workbench import source_catalog
from proxy_workbench import source_management

ROOT = Path(__file__).resolve().parents[1]
BUNDLED = ROOT / 'proxy_workbench' / 'sources.json'


def args_for(data, **overrides):
    values = dict(sources=BUNDLED, data=Path(data), no_sources=False)
    values.update(overrides)
    return argparse.Namespace(**values)


class SelectionDrivesCollectionTest(unittest.TestCase):
    """What the user chose is what the collector fetches.

    This is the link the catalog, the sets and the adapters all depend on: a
    selection that the collector ignores would make the whole catalog a
    read-only display.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)
        self.catalog = source_catalog.load_bundled()
        quick = [row for row in self.catalog['sets'] if row['id'] == 'quick']
        self.quick = set(quick[0]['members']) if quick else set()
        self.assertTrue(self.quick, 'в каталоге должен быть базовый набор')

    def tearDown(self):
        self.temp.cleanup()

    def _select_quick(self):
        settings = source_management.read_settings(self.data) or {'settings_version': 3}
        source_management.write_settings(self.data, source_management.apply_set(settings, 'quick'))

    def test_a_fresh_install_keeps_the_previous_sources(self):
        # Nobody has chosen anything yet, so the 55 sources the app used before
        # are carried over — now as catalog entries, with their own adapters and
        # provenance instead of bare strings.
        values = p.resolve_collect_sources(args_for(self.data))
        self.assertEqual({value['id'] for value in values},
                         {f'cur-{index:02d}' for index in range(1, 56)})
        for value in values:
            self.assertTrue(value['endpoints'])
            self.assertNotEqual(value['adapter']['kind'], 'unsupported')

    def test_a_selection_resolves_to_the_chosen_catalog_sources(self):
        self._select_quick()
        values = p.resolve_collect_sources(args_for(self.data))
        self.assertTrue(values, 'выбранный набор обязан дать источники')
        ids = {value.get('id') for value in values if isinstance(value, dict)}
        self.assertEqual(ids, self.quick)
        for value in values:
            self.assertTrue(value['endpoints'], 'у источника должен быть endpoint')
            self.assertNotEqual(value['adapter']['kind'], 'unsupported')

    def test_an_explicit_sources_file_wins_over_the_selection(self):
        self._select_quick()  # noqa: F841 - the point is that the file still wins
        own = self.data / 'mine.json'
        own.write_text(json.dumps(['https://example.invalid/list.txt']), encoding='utf-8')
        values = p.resolve_collect_sources(args_for(self.data, sources=own))
        self.assertEqual(values, ['https://example.invalid/list.txt'])

    def test_no_sources_flag_yields_nothing(self):
        self._select_quick()
        self.assertEqual(p.resolve_collect_sources(args_for(self.data, no_sources=True)), [])

    def test_collect_reports_the_selected_sources(self):
        self._select_quick()
        values = p.resolve_collect_sources(args_for(self.data))

        seen = []
        by_url = {value['endpoints'][0]['url']: value for value in values}

        @asynccontextmanager
        async def source_response(client, url, *args, **kwargs):
            source = by_url[url]
            seen.append(source['id'])
            kind = source['adapter']['kind']
            rows = [{'ip': '11.1.1.1', 'port': 8080, 'protocol': 'http', 'protocols': ['http']}]
            body = (b'11.1.1.1:8080\n' if kind == 'line' else
                    json.dumps({'data': rows, 'page': 1, 'total': 1} if kind == 'page-json' else rows).encode())
            yield httpx.Response(200, content=body, request=httpx.Request('GET', url))

        db = p.open_db(self.data / 'test.sqlite3')
        try:
            with mock.patch.object(p, '_source_stream', source_response):
                report = asyncio.run(p.collect(db, values, [], on_progress=None, quiet=True))
            self.assertTrue(all(row['accepted'] for row in report['sources']))
            self.assertEqual({row[0] for row in db.execute('SELECT DISTINCT source FROM candidate_seen')},
                             self.quick)
        finally:
            db.close()
        self.assertEqual(set(seen), self.quick)
        self.assertEqual({item['source_id'] for item in report['sources']}, self.quick)

    def test_an_explicitly_empty_selection_fetches_nothing(self):
        settings = source_management.read_settings(self.data)
        selection = settings['source_selection']
        selection['selected_ids'] = []
        source_management.write_settings(self.data, settings)
        self.assertEqual(p.resolve_collect_sources(args_for(self.data)), [])

    def test_custom_format_and_name_survive_collection_resolution(self):
        descriptor = source_catalog.custom_source('https://mine.example/list.json', 'json-records')
        settings = source_catalog.migrate_settings({'sources': []})
        settings['source_selection'].update(selected_ids=[descriptor['id']],
                                            custom_sources=[dict(descriptor, name='My list')])
        source_management.write_settings(self.data, settings)
        values = p.resolve_collect_sources(args_for(self.data))
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0]['id'], descriptor['id'])
        self.assertEqual(values[0]['adapter']['kind'], 'json-records')
        self.assertEqual(values[0]['endpoints'][0]['url'], descriptor['url'])
        self.assertEqual(values[0]['name'], 'My list')


class RecordLimitIsReportedAsPartialTest(unittest.TestCase):
    """A list larger than the cap is read up to the cap, and says so."""

    limits = {'max_records': 10, 'max_bytes': 4 * 1024 * 1024, 'max_depth': 8,
              'max_string': 65536, 'max_columns': 32, 'max_nodes': 200000}

    def test_a_long_json_list_is_truncated_not_refused(self):
        body = json.dumps([{'ip': f'11.0.0.{index}', 'port': 8080} for index in range(50)]).encode()
        profile = {'kind': 'json-records', 'profile': 'ip-port-v1', 'config': {'default_protocol': 'http'}}
        parsed = source_adapters.parse_page(body, profile, {'page': 1, 'url': 'https://x.invalid/'},
                                            self.limits)
        self.assertEqual(len(parsed['records']), 10)
        self.assertTrue(parsed.get('truncated'))
        self.assertEqual(parsed.get('reason'), 'SOURCE_RECORD_LIMIT')
        self.assertEqual(parsed['state'], 'partial')

    def test_a_long_text_list_is_truncated_not_refused(self):
        body = '\n'.join(f'11.0.0.{index}:8080' for index in range(50)).encode()
        profile = {'kind': 'line', 'profile': 'http-v1', 'config': {'default_protocol': 'http'}}
        parsed = source_adapters.parse_page(body, profile, {'page': 1, 'url': 'https://x.invalid/'},
                                            self.limits)
        self.assertEqual(len(parsed['records']), 10)
        self.assertTrue(parsed.get('truncated'))

    def test_a_list_under_the_cap_is_complete(self):
        body = json.dumps([{'ip': f'11.0.0.{index}', 'port': 8080} for index in range(3)]).encode()
        profile = {'kind': 'json-records', 'profile': 'ip-port-v1', 'config': {'default_protocol': 'http'}}
        parsed = source_adapters.parse_page(body, profile, {'page': 1, 'url': 'https://x.invalid/'},
                                            self.limits)
        self.assertEqual(parsed['state'], 'complete')
        self.assertIsNone(parsed.get('truncated'))



class NotModifiedServesTheStoredSetTest(unittest.TestCase):
    """304 is not an empty answer: the stored set is still what the source offers."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)
        self.hits = 0
        self.body = b'11.0.0.1:8080\n11.0.0.2:3128\n'

    def tearDown(self):
        self.temp.cleanup()

    async def _server(self, reader, writer):
        self.hits += 1
        request = await reader.read(2048)
        if b'If-None-Match' in request or b'If-Modified-Since' in request:
            writer.write(b'HTTP/1.1 304 Not Modified\r\nETag: "abc"\r\n\r\n')
        else:
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nETag: "abc"\r\n'
                         b'Content-Length: %d\r\n\r\n' % len(self.body) + self.body)
        await writer.drain()
        writer.close()

    def test_a_second_run_reports_the_stored_rows_not_zero(self):
        async def scenario():
            server = await asyncio.start_server(self._server, '127.0.0.1', 0)
            url = 'http://127.0.0.1:%d/list.txt' % server.sockets[0].getsockname()[1]
            db = p.open_db(self.data / 't.sqlite3')
            try:
                first = await p.collect(db, [url], [], 30, allow_private_sources=True)
                second = await p.collect(db, [url], [], 30, allow_private_sources=True)
            finally:
                server.close()
                db.close()
            return first['sources'][0], second['sources'][0]

        first, second = asyncio.run(scenario())
        self.assertEqual(first['rows'], 2)
        self.assertEqual(first['http_state'], 'http_2xx_nonempty')
        self.assertFalse(first.get('served_from_cache'))
        self.assertEqual(second['http_state'], 'not_modified')
        self.assertEqual(second['rows'], 2, '304 не должен обнулять данные источника')
        self.assertTrue(second.get('served_from_cache'))
        self.assertIsNotNone(second.get('cache_age_seconds'))
        self.assertEqual(self.hits, 2, 'условный запрос должен уходить ровно один раз за прогон')

if __name__ == '__main__':
    unittest.main()
