import asyncio
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import gui
from proxy_workbench import proxytool as p

HTML = b'''<table><tr><th>IP Address</th><th>Port</th></tr>
<tr><td>11.1.1.1</td><td>8080</td><td>DE</td></tr>
<tr><td>11.1.1.2</td>
    <td>3128</td></tr></table>
<p>Updated at 12:30. Version 2.1</p> socks5://11.1.1.3:1080, 11.1.1.4 80
<p>\xff\xfe not utf-8 here</p>'''


class LooseParserTests(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(p.loose_addresses('<td>11.0.0.1</td><td>8080</td>'), ['http://11.0.0.1:8080'])
        self.assertEqual(p.loose_addresses('11.0.0.1,3128,US'), ['http://11.0.0.1:3128'])
        self.assertEqual(p.loose_addresses('11.0.0.1\t80 | socks4://11.0.0.2:4145'),
                         ['http://11.0.0.1:80', 'socks4://11.0.0.2:4145'])
        self.assertEqual(p.loose_addresses('released 1.2.3 on 12:30, build 2024'), [])

    def test_source_kinds(self):
        for kind in ('auto', 'text', 'socks4'):
            self.assertEqual(p.source_spec(f'{kind} https://example.org/x')[0], kind)
        self.assertEqual(gui.public_source('text https://example.org/list?key=secret', keyed=False), 'text https://example.org/')


class CollectTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = p.open_db(Path(self.temp.name) / 'db.sqlite3')

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def candidates(self):
        return sorted(row[0] for row in self.db.execute('SELECT proxy FROM candidates'))

    async def serve(self, pages):
        async def handler(reader, writer):
            path = (await reader.readuntil(b'\r\n\r\n')).split(b' ')[1].decode()
            body = pages[path]
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n' % len(body) + body)
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        server = await asyncio.start_server(handler, '127.0.0.1', 0)
        return server, f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'

    async def test_text_source_reads_html_tables(self):
        server, base = await self.serve({'/page': HTML})
        async with server:
            report = await p.collect(self.db, ['text ' + base + '/page'], [], allow_private_sources=True)
        self.assertEqual(self.candidates(), ['http://11.1.1.1:8080', 'http://11.1.1.2:3128',
                                             'http://11.1.1.4:80', 'socks5://11.1.1.3:1080'])
        self.assertTrue(report['sources'][0]['complete'])
        self.assertEqual(report['sources'][0]['invalid'], 0)

    async def test_auto_source_and_detected_local_input(self):
        server, base = await self.serve({'/list': b'11.2.2.1:1080\nsocks5://11.2.2.2:1080\n'})
        local = Path(self.temp.name) / 'mine.txt'
        local.write_text('11.3.3.1:8080\n', encoding='utf-8')
        async with server:
            await p.collect(self.db, ['auto ' + base + '/list'], [str(local)], allow_private_sources=True,
                            detect_protocols=True)
        self.assertEqual(self.candidates(), sorted([
            'http://11.2.2.1:1080', 'socks4://11.2.2.1:1080', 'socks5://11.2.2.1:1080', 'socks5://11.2.2.2:1080',
            'http://11.3.3.1:8080', 'socks4://11.3.3.1:8080', 'socks5://11.3.3.1:8080']))

    async def test_local_input_without_detection_stays_http(self):
        local = Path(self.temp.name) / 'mine.txt'
        local.write_text('11.3.3.1:8080\n', encoding='utf-8')
        await p.collect(self.db, [], [str(local)])
        self.assertEqual(self.candidates(), ['http://11.3.3.1:8080'])


class GuiSettingTests(unittest.TestCase):
    def test_detect_protocols_setting(self):
        self.assertFalse(gui.defaults()['detect_protocols'])
        self.assertTrue(gui.validate(dict(gui.defaults(), detect_protocols=True))['detect_protocols'])
        with self.assertRaises(ValueError):
            gui.validate(dict(gui.defaults(), detect_protocols='yes'))


if __name__ == '__main__':
    unittest.main()
