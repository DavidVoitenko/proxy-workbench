"""Whole-job collection budgets with local response mocks and temporary data."""
from contextlib import asynccontextmanager
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import httpx

from proxy_workbench import proxytool as p


class CollectBudgetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.home = Path(self.directory.name)
        self.db = p.open_db(self.home / 'collect.sqlite3')
        self.hits = []

    def tearDown(self):
        self.db.close()
        self.directory.cleanup()

    def response(self, url, body=b'11.1.1.1:8080\n', status=200, headers=None):
        return httpx.Response(status, content=body, headers=headers, request=httpx.Request('GET', url))

    def stream(self, responder=None):
        @asynccontextmanager
        async def stream(client, url, *args, **kwargs):
            self.hits.append(url)
            await asyncio.sleep(0)  # several sources compete for one shared budget
            yield responder(url) if responder else self.response(url)
        return mock.patch.object(p, '_source_stream', stream)

    async def collect(self, sources, **kwargs):
        return await p.collect(self.db, sources, [], quiet=True, sleep=lambda _: asyncio.sleep(0), **kwargs)

    def member_count(self):
        return self.db.execute('SELECT count(*) FROM membership').fetchone()[0]

    async def test_zero_budgets_do_not_start_requests_or_add_members(self):
        for name, code in (('max_requests', 'SOURCE_REQUEST_BUDGET'),
                           ('max_items', 'SOURCE_ITEM_BUDGET'), ('max_bytes', 'SOURCE_BYTE_BUDGET')):
            with self.subTest(name=name), self.stream():
                report = await self.collect(['https://source.invalid/' + name], **{name: 0})
                self.assertEqual(self.hits, [])
                self.assertEqual(self.member_count(), 0)
                self.assertEqual(report['sources'][0]['attempts'], 0)
                self.assertIn(code, report['budget']['exhausted'])
                self.assertTrue(report['budget_exhausted'])

    async def test_concurrent_sources_share_one_request_limit(self):
        with self.stream():
            report = await self.collect([f'https://source.invalid/{n}' for n in range(8)], max_requests=2)
        self.assertEqual(len(self.hits), 2)
        self.assertEqual(report['budget']['requests'], 2)
        self.assertEqual(sum(row['attempts'] for row in report['sources']), 2)
        self.assertEqual(sum(row['accepted'] for row in report['sources']), 2)

    async def test_retries_and_mirrors_cannot_exceed_the_request_limit(self):
        plan = p.SourcePlan('with-mirror', 'https://source.invalid/main', 'http',
                            fallbacks=('https://source.invalid/mirror',))
        with self.stream(lambda url: self.response(url, b'', status=503)):
            report = await self.collect([plan], max_requests=2)
        self.assertEqual(self.hits, [plan.url, plan.url])
        self.assertEqual(report['sources'][0]['error'], 'SOURCE_REQUEST_BUDGET')
        self.assertEqual(report['budget']['requests'], 2)
        self.assertEqual(self.db.execute('SELECT count(*) FROM source_state').fetchone()[0], 0)

    async def test_redirects_and_pages_share_the_request_limit(self):
        with self.stream(lambda url: self.response(url, b'', 302, {'Location': '/redirected'})):
            redirected = await self.collect(['https://source.invalid/start'], max_requests=1)
        self.assertEqual(len(self.hits), 1)
        self.assertEqual(redirected['sources'][0]['error'], 'SOURCE_REQUEST_BUDGET')
        self.hits.clear()
        body = json.dumps({'data': [{'ip': '11.2.2.2', 'port': 8080, 'protocol': 'http'}],
                           'page': 1, 'total': 2}).encode()
        plan = p.SourcePlan('paged', 'https://source.invalid/page', 'page-json',
                            {'kind': 'page-json', 'profile': 'page-number-v1',
                             'config': {'records_path': 'data', 'page_path': 'page', 'total_path': 'total'}})
        with self.stream(lambda url: self.response(url, body)):
            paged = await self.collect([plan], max_requests=1)
        self.assertEqual(len(self.hits), 1)
        self.assertEqual(paged['sources'][0]['accepted'], 1)
        self.assertEqual(paged['sources'][0]['error'], 'SOURCE_REQUEST_BUDGET')

    async def test_bytes_are_global_across_sources_and_oversized_bodies_are_refused(self):
        body = b'11.3.3.3:8080\n'
        with self.stream(lambda url: self.response(url, body)):
            report = await self.collect(['https://source.invalid/a', 'https://source.invalid/b'],
                                         max_bytes=len(body) + 2)
        self.assertLessEqual(report['budget']['bytes'], len(body) + 2)
        self.assertEqual(sum(row['accepted'] for row in report['sources']), 1)
        self.assertIn('SOURCE_BYTE_BUDGET', report['budget']['exhausted'])

    async def test_stream_without_content_length_stops_on_shared_byte_budget(self):
        delivered = []

        class Chunks(httpx.AsyncByteStream):
            async def __aiter__(self):
                for chunk in (b'#123\n', b'11.4.4.4:8080\n', b'11.5.5.5:8080\n'):
                    delivered.append(chunk)
                    yield chunk

        with self.stream(lambda url: httpx.Response(200, stream=Chunks(), request=httpx.Request('GET', url))):
            report = await self.collect(['https://source.invalid/chunked'], max_bytes=8)
        self.assertEqual(len(delivered), 2)
        self.assertLessEqual(report['budget']['bytes'], 8)
        self.assertEqual(self.member_count(), 0)
        self.assertEqual(report['sources'][0]['error'], 'SOURCE_BYTE_BUDGET')

    async def test_existing_members_do_not_spend_the_new_membership_budget(self):
        collection = p.ensure_collection(self.db, None)
        endpoint = p.upsert_endpoint(self.db, 'http://11.6.6.1:8080')
        p.schema.add_member(self.db, collection, endpoint, origin='public')
        self.db.commit()
        body = b'11.6.6.1:8080\n11.6.6.2:8080\n11.6.6.3:8080\n'
        with self.stream(lambda url: self.response(url, body)):
            report = await self.collect(['https://source.invalid/items'], max_items=1)
        self.assertEqual(report['budget']['items'], 1)
        self.assertEqual(self.member_count(), 2)
        self.assertEqual(report['sources'][0]['error'], 'SOURCE_ITEM_BUDGET')
        self.assertEqual(self.db.execute('SELECT count(*) FROM source_state').fetchone()[0], 0)

    async def test_multi_protocol_records_and_local_files_obey_the_same_item_cap(self):
        body = json.dumps([{'ip': '11.7.7.7', 'port': 8080, 'protocols': ['http', 'socks5']}]).encode()
        with self.stream(lambda url: self.response(url, body)):
            report = await self.collect(['json-records https://source.invalid/protocols'], max_items=1)
        self.assertEqual(self.member_count(), 1)
        self.assertEqual(report['sources'][0]['error'], 'SOURCE_ITEM_BUDGET')
        file = self.home / 'input.txt'
        file.write_text('11.8.8.1:8080\n11.8.8.2:8080\n', encoding='utf-8')
        local = await p.collect(self.db, [], [file], max_items=1, quiet=True)
        self.assertEqual(local['budget']['items'], 1)
        self.assertEqual(self.member_count(), 2)
        self.assertTrue(local['budget_exhausted'])

    async def test_preview_forwards_global_budgets_without_persisting_members(self):
        with self.stream():
            report = await p.preview_collect(self.db, ['https://source.invalid/preview'], max_requests=0)
        self.assertEqual(self.hits, [])
        self.assertEqual(report['budget']['requests'], 0)
        self.assertTrue(report['budget_exhausted'])
        self.assertEqual(self.member_count(), 0)


if __name__ == '__main__':
    unittest.main()
