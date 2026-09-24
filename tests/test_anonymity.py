import asyncio
import contextlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import anonymity
import gui
import proxytool as p
from reputation import result_allowed

# Synthetic public address standing in for "this machine"; no traffic leaves loopback.
OWN_IP = '93.184.216.34'
JUDGE = 'http://judge.invalid/get'


def echo(origin, headers=None):
    return json.dumps({'origin': origin, 'headers': dict({'Host': 'judge.invalid'}, **(headers or {}))})


def scan_config(judge=JUDGE):
    cfg = dict(version=2, targets=[dict(url='http://service.invalid/check', method='GET', statuses=[200],
                                        headers={}, contains='healthy', sha256=None)],
               attempts=2, timeout=0.5, max_bytes=4096, request_profile='workbench')
    if judge:
        cfg['anonymity'] = {'judge_url': judge}
    return cfg


def result_row(proxy, level=None):
    row = dict(proxy=proxy, reliability=1, min_target_reliability=1, latency_ms=10, jitter_ms=1,
               score=90, successes=2, requests=2, checked_at=0, samples=[])
    if level:
        row['anonymity'] = {'level': level, 'signals': []}
    return row


class ClassifyTests(unittest.TestCase):
    def test_levels_from_json_echo(self):
        own = {OWN_IP}
        self.assertEqual(anonymity.classify(echo(OWN_IP), own)['level'], 'transparent')
        leaked = anonymity.classify(echo('11.0.0.1', {'X-Forwarded-For': OWN_IP}), own)
        self.assertEqual(leaked, {'level': 'transparent', 'signals': ['real_ip']})
        revealed = anonymity.classify(echo('11.0.0.1', {'Via': '1.1 squid', 'X-Forwarded-For': 'unknown'}), own)
        self.assertEqual(revealed['level'], 'anonymous')
        self.assertEqual(revealed['signals'], ['via', 'x-forwarded-for'])
        self.assertEqual(anonymity.classify(echo('11.0.0.1'), own), {'level': 'elite', 'signals': []})

    def test_levels_from_cgi_style_echo(self):
        own = {OWN_IP}
        body = b'REMOTE_ADDR = 11.0.0.1\nHTTP_PROXY_CONNECTION = keep-alive\nSERVER_ADDR = 11.0.0.9\n'
        self.assertEqual(anonymity.classify(body, own), {'level': 'anonymous', 'signals': ['proxy-connection']})
        self.assertEqual(anonymity.classify(b'REMOTE_ADDR = 11.0.0.1\n', own)['level'], 'elite')

    def test_plain_words_are_not_header_signals(self):
        text = '<p>Sent via our gateway. Forwarded to support.</p><p>origin 11.0.0.1</p>'
        self.assertEqual(anonymity.classify(text, {OWN_IP})['level'], 'elite')

    def test_public_ip_extraction_skips_private_ranges(self):
        text = 'a 10.0.0.1 b 192.168.1.1 c 11.0.0.1 d 2001:4860:4860::8888 e 127.0.0.1 f 1.1'
        self.assertEqual(anonymity.extract_public_ips(text), {'11.0.0.1', '2001:4860:4860::8888'})

    def test_validation_and_filters(self):
        self.assertIsNone(anonymity.validate_judge(None))
        self.assertIsNone(anonymity.validate_judge({'judge_url': ''}))
        self.assertEqual(anonymity.validate_judge({'judge_url': JUDGE}), {'judge_url': JUDGE})
        for bad in ('ftp://judge.invalid/', 'http://user:pw@judge.invalid/', 'http://judge.invalid:0/', 42):
            with self.assertRaises(ValueError):
                anonymity.validate_judge({'judge_url': bad})
        with self.assertRaises(ValueError):
            anonymity.validate_min_level('paranoid')
        elite, plain = result_row('http://11.0.0.1:80', 'elite'), result_row('http://11.0.0.2:80', 'anonymous')
        self.assertTrue(anonymity.allows(plain, 'any'))
        self.assertTrue(anonymity.allows(plain, 'anonymous'))
        self.assertFalse(anonymity.allows(plain, 'elite'))
        self.assertTrue(result_allowed(elite, 1, min_anonymity='elite'))
        self.assertFalse(result_allowed(result_row('http://11.0.0.3:80'), 1, min_anonymity='anonymous'))

    def test_profile_identity_is_unchanged_without_judge(self):
        args = SimpleNamespace(config=None, url='http://service.invalid/', attempts=3, timeout=8,
                               max_bytes=1024, request_profile=None, judge_url=None)
        self.assertNotIn('anonymity', p.target_config(args))
        args.judge_url = JUDGE
        self.assertEqual(p.target_config(args)['anonymity'], {'judge_url': JUDGE})

    def test_gui_settings_validation(self):
        settings = gui.defaults()
        settings.update(anonymity={'judge_url': ' ' + JUDGE + ' '}, min_anonymity='elite')
        clean = gui.validate(settings)
        self.assertEqual(clean['anonymity'], {'judge_url': JUDGE})
        self.assertEqual(clean['min_anonymity'], 'elite')
        for bad in ({'anonymity': {'judge_url': 'file:///etc/hosts'}}, {'anonymity': 'x'}, {'min_anonymity': 'max'}):
            with self.assertRaises(ValueError):
                gui.validate(dict(gui.defaults(), **bad))


class JudgeTransportTests(unittest.IsolatedAsyncioTestCase):
    async def serve(self, respond):
        async def handler(reader, writer):
            try:
                request = await reader.readuntil(b'\r\n\r\n')
                body = respond(request).encode()
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n' % len(body) + body)
                await writer.drain()
            finally:
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()
        server = await asyncio.start_server(handler, '127.0.0.1', 0)
        return server, f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'

    async def test_check_proxy_rates_each_mock_proxy(self):
        mode = ['elite']

        def respond(request):
            if request.startswith(b'GET http://service.invalid/'):
                return 'healthy'
            assert request.startswith(b'GET ' + JUDGE.encode())
            return {'transparent': echo('11.0.0.5', {'X-Forwarded-For': OWN_IP}),
                    'anonymous': echo('11.0.0.5', {'Via': '1.1 mock'}),
                    'elite': echo('11.0.0.5')}[mode[0]]

        server, proxy = await self.serve(respond)
        async with server:
            for level in ('transparent', 'anonymous', 'elite'):
                mode[0] = level
                row = await p.check_proxy(proxy, scan_config(), p.Rate(0), own_ips={OWN_IP})
                self.assertEqual(row['successes'], 2)
                self.assertEqual(row['anonymity']['level'], level)
            row = await p.check_proxy(proxy, scan_config(judge=None), p.Rate(0), own_ips={OWN_IP})
            self.assertNotIn('anonymity', row)

    async def test_dead_proxy_is_not_judged_and_judge_errors_are_unknown(self):
        def respond(request):
            return 'healthy' if request.startswith(b'GET http://service.invalid/') else 'x' * (anonymity.MAX_JUDGE_BYTES + 1)

        server, proxy = await self.serve(respond)
        async with server:
            row = await p.check_proxy(proxy, scan_config(), p.Rate(0), own_ips={OWN_IP})
            self.assertEqual(row['anonymity']['level'], 'unknown')
            self.assertEqual(row['anonymity']['error'], 'BODY_TOO_LARGE')
        dead = await p.check_proxy(proxy, scan_config(), p.Rate(0), own_ips={OWN_IP})
        self.assertEqual(dead['successes'], 0)
        self.assertNotIn('anonymity', dead)

    async def test_detect_own_ips_from_direct_judge(self):
        body = [f'REMOTE_ADDR = {OWN_IP}\nSERVER_ADDR = 127.0.0.1\n']
        server, url = await self.serve(lambda request: body[0])
        async with server:
            self.assertEqual(await p.detect_own_ips(scan_config(judge=url + '/azenv')), {OWN_IP})
            body[0] = 'REMOTE_ADDR = 10.1.2.3\n'
            with self.assertRaises(ValueError):
                await p.detect_own_ips(scan_config(judge=url + '/azenv'))


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.db = p.open_db(self.home / 'test.sqlite3')

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def store(self, profile, cfg, rows):
        self.db.execute('INSERT INTO profiles VALUES (?,?)', (profile, json.dumps(cfg)))
        for row in rows:
            self.db.execute('INSERT OR IGNORE INTO candidates VALUES (?)', (row['proxy'],))
            self.db.execute('INSERT INTO results VALUES (?,?,?)', (profile, row['proxy'], json.dumps(row)))
        self.db.commit()

    def test_min_anonymity_and_protocol_files(self):
        rows = [result_row('http://11.0.0.1:8080', 'elite'), result_row('socks5://11.0.0.2:1080', 'anonymous'),
                result_row('socks5h://[2001:db8::1]:1080', 'elite'), result_row('https://11.0.0.4:443', 'transparent'),
                result_row('http://11.0.0.5:3128', 'unknown')]
        self.store('judged', scan_config(), rows)
        out = self.home / 'out'
        report = p.export(self.db, 'judged', out, min_success=1)
        self.assertEqual(report['exported'], 5)
        self.assertEqual(report['anonymity']['counts'],
                         {'elite': 2, 'anonymous': 1, 'transparent': 1, 'unknown': 1})
        self.assertEqual((out / 'http.txt').read_text(encoding='utf-8').split(), ['11.0.0.1:8080', '11.0.0.5:3128'])
        self.assertEqual(sorted((out / 'socks5.txt').read_text(encoding='utf-8').split()),
                         ['11.0.0.2:1080', '[2001:db8::1]:1080'])
        self.assertEqual((out / 'https.txt').read_text(encoding='utf-8').split(), ['11.0.0.4:443'])
        self.assertIn('anonymity', (out / 'ranked.csv').read_text(encoding='utf-8').splitlines()[0])
        self.assertEqual(p.export(self.db, 'judged', out, min_success=1, min_anonymity='anonymous')['exported'], 3)
        elite = p.export(self.db, 'judged', out, min_success=1, min_anonymity='elite')
        self.assertEqual(elite['exported'], 2)
        self.assertEqual(elite['anonymity']['min_level'], 'elite')

    def test_min_anonymity_is_ignored_without_judge(self):
        self.store('plain', scan_config(judge=None), [result_row('http://11.0.0.1:8080')])
        report = p.export(self.db, 'plain', self.home / 'out', min_success=1, min_anonymity='elite')
        self.assertEqual(report['exported'], 1)
        self.assertFalse(report['anonymity']['enabled'])


if __name__ == '__main__':
    unittest.main()
