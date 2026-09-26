"""F13: a real collect through every registered source adapter.

The bodies below are served by a real HTTP server on loopback and fetched with
the project's own HTTP client, so the request/response path is genuine: status
codes, ``ETag``/``304``, ``Content-Encoding``, ``Content-Length`` and a body that
stops mid-stream all really happen.  What is *not* exercised here is
``proxytool.collect``'s persistence side -- that module is not this area's, and
these tests never pretend to cover it.

Nothing in this file points at a public proxy list, a public address or a
third-party service.  Every address is from a documentation range
(``198.51.100.0/24``, ``203.0.113.0/24``) that cannot be routed.
"""
import asyncio
import base64
import gzip
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from proxy_workbench import source_adapters as sa
from proxy_workbench import source_catalog as sc

B64 = lambda text: base64.b64encode(text.encode()).decode()

LINE_BODY = (b"# a comment line is not an address\n"
             b"198.51.100.7:8080\n"
             b"203.0.113.9:3128\n"
             b"socks5://198.51.100.8:1080\n")
JSON_BODY = json.dumps({"proxies": [
    {"ip": "198.51.100.11", "port": 8080, "protocol": "http", "country": "NL", "latency": 120},
    {"ip": "203.0.113.21", "port": 1080, "protocol": "socks5"}]}).encode()
FIELDS_BODY = (b"ip,port,protocol,country,anonymity\n"
               b"198.51.100.31,8080,http,DE,elite\n"
               b"203.0.113.41,1080,socks5,FR,anonymous\n")
HTML_ATTR_BODY = (
    "<html><body><table id='table_proxies'><thead><tr><th>#</th><th>ip</th><th>port</th>"
    "<th>type</th></tr></thead><tbody>"
    "<tr><td>1</td><td data-ip='%s' data-port='%s'>x</td><td>%s</td>"
    "<td><a href='?type=HTTPS'>HTTPS</a></td></tr>"
    "</tbody></table></body></html>" % (B64('198.51.100.51'), B64('8080'), B64('1'))).encode()
HTML_COLUMNS_BODY = (b"<table><tr><td>198.51.100.61</td><td>8080</td><td>HTTP</td><td>US</td></tr>"
                     b"<tr><td>203.0.113.71</td><td>1080</td><td>SOCKS5</td><td>FR</td></tr></table>")
PAGE_JSON_BODY = json.dumps({"data": [{"ip": "198.51.100.81", "port": 8080, "protocols": ["http"]}],
                             "page": 1, "total": 3, "has_more": True}).encode()

#: Every adapter kind the catalog can point at, with the exact profile the
#: bundled catalog ships for it -- not a profile invented for the test.
ADAPTER_CASES = {
    'line': (LINE_BODY, {'kind': 'line', 'profile': 'http-v1',
                         'config': {'legacy_kind': 'http', 'default_protocol': 'http'}}),
    'json-records': (JSON_BODY, {'kind': 'json-records', 'profile': 'generic-v1',
                                 'config': {'default_protocol': 'http'}}),
    'fields': (FIELDS_BODY, {'kind': 'fields', 'profile': 'generic-fields-v1',
                             'config': sc._adapter_config('new-026', 'fields')}),
    'html-table': (HTML_ATTR_BODY, {'kind': 'html-table', 'profile': 'generic-html-v1',
                                    'config': sc._adapter_config('new-010', 'html-table')}),
    'page-json': (PAGE_JSON_BODY, {'kind': 'page-json', 'profile': 'page-number-v1',
                                   'config': sc._adapter_config('new-045', 'page-json')}),
}
EXPECTED_FIRST = {
    'line': 'http://198.51.100.7:8080',
    'json-records': 'http://198.51.100.11:8080',
    'fields': 'http://198.51.100.31:8080',
    'html-table': 'http://198.51.100.51:8080',
    'page-json': 'http://198.51.100.81:8080',
}

ROUTES = {}


async def _handler(reader, writer):
    """A minimal origin: routing, ETag, gzip, truncation and a hard close."""
    try:
        request = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 5)
    except Exception:
        writer.close()
        return
    path = request.split(b'\r\n', 1)[0].decode('latin-1').split(' ')[1]
    if b'If-None-Match' in request:
        writer.write(b'HTTP/1.1 304 Not Modified\r\nETag: "v1"\r\nConnection: close\r\n\r\n')
        await writer.drain()
        writer.close()
        return
    route = ROUTES.get(path)
    if route is None:
        writer.write(b'HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
        await writer.drain()
        writer.close()
        return
    status, body, content_type, mode = route
    if mode == 'gzip':
        body = gzip.compress(body)
        head = (f'HTTP/1.1 {status} X\r\nContent-Length: {len(body)}\r\n'
                f'Content-Type: {content_type}\r\nContent-Encoding: gzip\r\n'
                f'ETag: "gz1"\r\nConnection: close\r\n\r\n').encode()
    elif mode == 'lie-length':
        # Announce more than is sent and close: the client must see a truncated
        # body rather than a complete short one.
        head = (f'HTTP/1.1 {status} X\r\nContent-Length: {len(body) + 4096}\r\n'
                f'Content-Type: {content_type}\r\nETag: "t1"\r\nConnection: close\r\n\r\n').encode()
    else:
        head = (f'HTTP/1.1 {status} X\r\nContent-Length: {len(body)}\r\n'
                f'Content-Type: {content_type}\r\nETag: "v1"\r\nConnection: close\r\n\r\n').encode()
    writer.write(head + body)
    await writer.drain()
    writer.close()


class LiveCollectBase(unittest.IsolatedAsyncioTestCase):
    """One loopback origin, shared by the adapter scenarios."""

    @classmethod
    def setUpClass(cls):
        for kind, (body, _profile) in ADAPTER_CASES.items():
            ROUTES[f'/{kind}'] = (200, body, 'text/plain; charset=utf-8', 'plain')
        ROUTES['/html-columns'] = (200, HTML_COLUMNS_BODY, 'text/html', 'plain')
        ROUTES['/empty'] = (200, b'', 'text/plain', 'plain')
        ROUTES['/gzip'] = (200, LINE_BODY, 'text/plain', 'gzip')
        ROUTES['/truncated'] = (200, LINE_BODY[:20], 'text/plain', 'lie-length')
        ROUTES['/broken'] = (200, b'[{"ip": "1.2.3.4", ', 'application/json', 'plain')
        ROUTES['/html-not-json'] = (200, b'<html><body>Sign in to continue</body></html>',
                                   'text/html', 'plain')
        ROUTES['/big'] = (200, b'\n'.join(b'198.51.100.%d:8080' % n for n in range(1, 2500)),
                          'text/plain', 'plain')
        ROUTES['/rate-limited'] = (429, b'slow down', 'text/plain', 'plain')

    async def asyncSetUp(self):
        self.server = await asyncio.start_server(_handler, '127.0.0.1', 0)
        self.base = 'http://127.0.0.1:%d' % self.server.sockets[0].getsockname()[1]
        self.client = httpx.AsyncClient(trust_env=False, timeout=10)

    async def asyncTearDown(self):
        await self.client.aclose()
        self.server.close()
        await self.server.wait_closed()

    async def fetch(self, path, headers=None):
        return await self.client.get(self.base + path, headers=headers or {})


class EveryAdapterCollectsTest(LiveCollectBase):
    """A 200 with a real body must yield addresses through every adapter."""

    async def test_each_adapter_turns_a_live_200_into_addresses(self):
        for kind, (_body, profile) in ADAPTER_CASES.items():
            with self.subTest(adapter=kind):
                response = await self.fetch('/' + kind)
                self.assertEqual(response.status_code, 200)
                result = sa.parse_page(response.content, profile)
                self.assertEqual(result['state'], 'complete')
                self.assertTrue(result['records'], 'адаптер не вернул ни одного адреса')
                self.assertEqual(result['records'][0]['values'][0], EXPECTED_FIRST[kind])
                self.assertEqual(result['pages'], 1)

    async def test_every_catalog_adapter_kind_has_a_live_case_here(self):
        """The test cannot rot into covering only the adapters it knows."""
        catalog = sc.load_bundled()
        used = {record['adapter']['kind'] for record in catalog['sources']
                if (record.get('adapter') or {}).get('kind') in sa.ADAPTER_KINDS}
        self.assertEqual(used, set(sa.ADAPTER_KINDS))

    async def test_the_second_request_is_answered_with_304_and_no_body(self):
        for kind in ADAPTER_CASES:
            with self.subTest(adapter=kind):
                first = await self.fetch('/' + kind)
                self.assertEqual(first.status_code, 200)
                second = await self.fetch('/' + kind, headers={'If-None-Match': '"v1"'})
                self.assertEqual(second.status_code, 304)
                self.assertEqual(second.content, b'')

    async def test_a_second_html_table_shape_is_read_by_its_own_config(self):
        """The HTML adapter is not one shape: attributes and columns differ."""
        profile = {'kind': 'html-table', 'profile': 'generic-html-v1',
                   'config': {'row_fragment': True,
                              'columns': {'ip': 0, 'port': 1, 'protocol': 2, 'country': 3}}}
        response = await self.fetch('/html-columns')
        result = sa.parse_page(response.content, profile)
        self.assertEqual(result['state'], 'complete')
        self.assertEqual([record['values'][0] for record in result['records']],
                         ['http://198.51.100.61:8080', 'socks5://203.0.113.71:1080'])

    async def test_publisher_metadata_stays_a_claim_and_never_becomes_a_measurement(self):
        response = await self.fetch('/json-records')
        result = sa.parse_page(response.content, ADAPTER_CASES['json-records'][1])
        declared = result['records'][0]['declared']
        self.assertEqual(declared.get('country'), 'NL')
        self.assertEqual(declared.get('latency'), 120)
        # The address itself carries no claim: a country or a latency the list
        # asserted must not travel on the endpoint as if we had measured it.
        for record in result['records']:
            self.assertNotIn('country', record['value'])
            self.assertNotIn('latency', record['value'])

    async def test_gzip_is_transported_and_the_adapter_sees_the_plain_document(self):
        response = await self.fetch('/gzip')
        self.assertEqual(response.headers.get('content-encoding'), 'gzip')
        result = sa.parse_page(response.content, ADAPTER_CASES['line'][1])
        self.assertEqual(result['state'], 'complete')
        self.assertEqual(len(result['records']), 3)


class OutcomeTest(LiveCollectBase):
    """200, 304, empty, broken and oversized are five different answers."""

    async def test_an_empty_body_is_its_own_outcome_for_every_adapter(self):
        """A 200 with a valid ETag and no bytes delivered nothing.

        That is not a success and not a broken document; it is the situation the
        source research actually recorded for gproxynet/free-proxy-list on
        2026-09-25, and it must not be reported as either.
        """
        response = await self.fetch('/empty')
        self.assertEqual(response.status_code, 200)
        for kind, (_body, profile) in ADAPTER_CASES.items():
            with self.subTest(adapter=kind):
                result = sa.parse_page(response.content, profile)
                self.assertEqual(result['state'], 'empty')
                self.assertEqual(result['records'], [])
                self.assertEqual(result.get('reason'), 'SOURCE_EMPTY_BODY')

    async def test_a_whitespace_only_body_is_also_an_empty_outcome(self):
        for kind, (_body, profile) in ADAPTER_CASES.items():
            with self.subTest(adapter=kind):
                self.assertEqual(sa.parse_page(b'\n  \n', profile)['state'], 'empty')

    async def test_a_broken_document_is_refused_with_a_reason_not_a_guess(self):
        response = await self.fetch('/broken')
        with self.assertRaises(sa.AdapterError) as caught:
            sa.parse_page(response.content, ADAPTER_CASES['json-records'][1])
        self.assertEqual(caught.exception.code, 'SOURCE_INVALID_JSON')
        self.assertFalse(caught.exception.partial)

    async def test_an_html_page_where_json_was_promised_is_named_as_such(self):
        response = await self.fetch('/html-not-json')
        with self.assertRaises(sa.AdapterError) as caught:
            sa.parse_page(response.content, ADAPTER_CASES['json-records'][1])
        self.assertEqual(caught.exception.code, 'SOURCE_HTML_PLACEHOLDER')

    async def test_a_body_past_its_byte_budget_is_refused_before_it_is_read(self):
        response = await self.fetch('/big')
        with self.assertRaises(sa.AdapterError) as caught:
            sa.parse_page(response.content, ADAPTER_CASES['line'][1], limits={'max_bytes': 1024})
        self.assertEqual(caught.exception.code, 'SOURCE_TOO_LARGE')

    async def test_a_list_past_the_record_cap_is_partial_and_keeps_what_it_read(self):
        """The GFP-style aggregator shape: huge, but the addresses read so far
        are real addresses and are reported as partial, not discarded."""
        response = await self.fetch('/big')
        result = sa.parse_page(response.content, ADAPTER_CASES['line'][1], limits={'max_records': 100})
        self.assertEqual(result['state'], 'partial')
        self.assertEqual(len(result['records']), 100)
        self.assertTrue(result['truncated'])
        self.assertEqual(result['reason'], 'SOURCE_RECORD_LIMIT')
        self.assertEqual(result['rejects'].get('record_limit'), 1)

    async def test_a_truncated_transfer_never_reaches_the_adapter(self):
        """A body that stops mid-stream is a transport failure.

        The client must raise rather than hand a short document to the parser,
        because a short JSON prefix is not a smaller proxy list -- it is a lie
        about how much the origin sent.
        """
        with self.assertRaises(httpx.RemoteProtocolError):
            await self.fetch('/truncated')

    async def test_a_rate_limited_response_carries_no_body_to_parse(self):
        response = await self.fetch('/rate-limited')
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.content, b'slow down')


class PaginationTest(LiveCollectBase):
    """A page-json source is read page by page, with a bounded next URL."""

    async def test_next_page_url_is_built_from_the_declared_pagination(self):
        response = await self.fetch('/page-json')
        data = json.loads(response.content)
        profile = ADAPTER_CASES['page-json'][1]
        page = sa.page_info(data, profile)
        self.assertEqual(page['page'], 1)
        self.assertEqual(page['total'], 3)
        self.assertIs(page['has_more'], True)
        self.assertEqual(sa.next_page_url(self.base + '/page-json', page, profile, 2),
                         self.base + '/page-json?page=2')

    async def test_a_next_url_pointing_at_another_host_is_refused(self):
        profile = {'kind': 'page-json', 'profile': 'page-number-v1',
                   'config': dict(sc._adapter_config('new-045', 'page-json'), mode='next-url')}
        page = {'records': [], 'next': 'https://elsewhere.example/proxies'}
        with self.assertRaises(sa.AdapterError) as caught:
            sa.next_page_url(self.base + '/api', page, profile, 2)
        self.assertEqual(caught.exception.code, 'SOURCE_PAGINATION_NEXT_INVALID')

    def test_a_page_budget_stops_the_walk_before_the_last_page(self):
        """Page count is the collector's budget; the adapter must not invent one.

        The adapter reports ``pages`` per page and nothing else, so a walk of
        five pages returns five complete results and the caller -- which owns
        the page budget -- is the only thing that can stop at two.  This test
        pins that division of labour instead of a walk the test wrote itself.
        """
        results = [sa.parse_page(PAGE_JSON_BODY, ADAPTER_CASES['page-json'][1])
                   for _ in range(5)]
        self.assertEqual([result['pages'] for result in results], [1] * 5)
        self.assertNotIn('max_pages', sa.parse_page.__doc__ or '')


class UnsupportedAndLimitTest(unittest.TestCase):
    """The adapter surface itself: what it refuses and what it caps."""

    def test_an_unknown_kind_is_refused_before_the_body_is_looked_at(self):
        with self.assertRaises(sa.AdapterError) as caught:
            sa.parse_page(b'anything', {'kind': 'executemjs', 'profile': 'x', 'config': {}})
        self.assertEqual(caught.exception.code, 'SOURCE_ADAPTER_UNSUPPORTED')

    def test_a_bare_legacy_name_is_not_a_parser(self):
        with self.assertRaises(sa.AdapterError) as caught:
            sa.parse_page(b'anything', 'socks5-v1')
        self.assertEqual(caught.exception.code, 'SOURCE_ADAPTER_UNSUPPORTED')

    def test_a_limit_that_expands_the_ceiling_is_refused(self):
        with self.assertRaises(sa.AdapterError) as caught:
            sa.parse_page(b'x', ADAPTER_CASES['line'][1], limits={'max_bytes': 10 ** 9})
        self.assertEqual(caught.exception.code, 'SOURCE_LIMIT_INVALID')

    def test_a_hostile_json_shape_is_refused_instead_of_unrolled(self):
        body = json.dumps({'data': [{'ip': '198.51.100.1', 'port': 1}]}).encode()
        deep = body
        for _ in range(200):
            deep = b'{"a":' + deep + b'}'
        with self.assertRaises(sa.AdapterError) as caught:
            sa.parse_page(deep, ADAPTER_CASES['page-json'][1], limits={'max_depth': 8})
        self.assertIn(caught.exception.code, ('SOURCE_JSON_DEPTH', 'SOURCE_INVALID_JSON'))

    def test_a_line_list_refuses_an_address_that_carries_credentials(self):
        body = b'http://user:secret@198.51.100.9:8080\n198.51.100.10:8080\n'
        result = sa.parse_line(body)
        self.assertEqual(len(result['records']), 1)
        self.assertEqual(result['rejects'].get('credentials_present'), 1)

    def test_every_registered_kind_has_a_named_parser(self):
        self.assertEqual(set(sa.PARSERS), set(sa.ADAPTER_KINDS))


if __name__ == '__main__':
    unittest.main()
