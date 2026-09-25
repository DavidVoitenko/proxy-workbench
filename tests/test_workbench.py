import asyncio
import contextlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace

from tests.workbench_support import add_candidate, add_candidates, store_result  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench.branding import merge_headers
from proxy_workbench import proxytool as p
from proxy_workbench.reputation import Denylist, make_policy, result_allowed, screen_proxy
from proxy_workbench.maintenance import clear_runtime


def config(targets=1):
    return dict(version=1, targets=[dict(url='http://service.invalid/check', method='GET',
                statuses=[200], headers={}, contains='healthy', sha256=None) for _ in range(targets)],
                attempts=3, timeout=0.3, max_bytes=1024)


def row(proxy, cfg, ok=True, ms=10):
    samples = [dict(ok=ok, ms=ms, target=i, attempt=a, status=200 if ok else 503,
                    error=None, bytes=7) for a in range(cfg['attempts']) for i in range(len(cfg['targets']))]
    return p.summarize(proxy, samples, cfg)


class WorkbenchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.db = p.open_db(self.home / 'test.sqlite3')

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    async def test_190000_full_coverage_bounded_workers_resume_and_export(self):
        count = 190000
        add_candidates(self.db,
                     ((f'http://11.{i//65536}.{i//256%256}.{i%256}:80',) for i in range(count)))
        self.db.commit()
        seen = set()
        active = peak = 0
        cfg = config()
        async def probe(proxy, cfg, rate):
            nonlocal active, peak
            self.assertNotIn(proxy, seen)
            seen.add(proxy)
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1
            return row(proxy, cfg)
        profile = await p.scan(self.db, cfg, workers=17, rate=0, probe=probe, progress=False)
        self.assertEqual(len(seen), count)
        self.assertLessEqual(peak, 17)
        self.assertEqual(self.db.execute('SELECT count(*) FROM results').fetchone()[0], count)
        async def should_not_run(*args):
            self.fail('resume repeated a completed proxy')
        await p.scan(self.db, cfg, workers=3, probe=should_not_run, progress=False)
        report = p.export(self.db, profile, self.home / 'out', top=137, min_success=1)
        self.assertTrue(report['complete'])
        self.assertEqual(report['exported'], 137)
        self.assertEqual(len((self.home/'out/proxies.txt').read_text(encoding='utf-8').splitlines()), 137)

    async def test_interruption_resume_and_profile_isolation(self):
        add_candidates(self.db, ((f'http://11.0.0.{i}:80',) for i in range(12)))
        self.db.commit()
        cfg = config()
        blocked = asyncio.Event()
        calls = []
        async def probe(proxy, cfg, rate):
            calls.append(proxy)
            if len(calls) == 5:
                blocked.set()
                await asyncio.sleep(60)
            return row(proxy, cfg)
        task = asyncio.create_task(p.scan(self.db, cfg, workers=1, probe=probe, progress=False))
        await blocked.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.db.execute('SELECT count(*) FROM results').fetchone()[0], 4)
        resumed = []
        async def resume(proxy, cfg, rate):
            resumed.append(proxy)
            return row(proxy, cfg)
        profile = await p.scan(self.db, cfg, workers=2, probe=resume, progress=False)
        self.assertEqual(len(resumed), 8)
        self.assertFalse(set(resumed) & set(calls[:4]))
        cfg['targets'][0]['contains'] = 'different'
        resumed.clear()
        other = await p.scan(self.db, cfg, workers=2, probe=resume, progress=False)
        self.assertNotEqual(profile, other)
        self.assertEqual(len(resumed), 12)
        resumed.clear()
        await p.scan(self.db, cfg, workers=2, probe=resume, progress=False, recheck=True)
        self.assertEqual(len(resumed), 12)

    async def test_real_http_proxy_transport_and_failures(self):
        requests = []
        mode = ['good']
        async def handler(reader, writer):
            try:
                data = await reader.readuntil(b'\r\n\r\n')
                requests.append(data)
                if mode[0] == 'timeout':
                    await asyncio.sleep(0.5)
                    return
                body = b'healthy' if mode[0] != 'wrong' else b'bad'
                status = 503 if mode[0] == 'status' else 200
                writer.write(f'HTTP/1.1 {status} Test\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n'.encode()+body)
                await writer.drain()
            finally:
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()
        server = await asyncio.start_server(handler, '127.0.0.1', 0)
        proxy = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'
        cfg = config()
        async with server:
            good = await p.check_proxy(proxy, cfg, p.Rate(0))
            self.assertEqual(good['successes'], 3)
            self.assertEqual(len(requests), 3)
            self.assertTrue(requests[0].startswith(b'GET http://service.invalid/check HTTP/1.1'))
            for value, error in [('wrong', 'CONTENT_MISMATCH'), ('status', 'HTTP_503'), ('timeout', 'TimeoutError')]:
                mode[0] = value
                result = await p.request_once(proxy, cfg['targets'][0], cfg, p.Rate(0))
                self.assertFalse(result['ok'])
                self.assertIn(result['error'], [error, 'ReadTimeout'] if value == 'timeout' else [error])
            mode[0] = 'good'
            cfg['max_bytes'] = 2
            result = await p.request_once(proxy, cfg['targets'][0], cfg, p.Rate(0))
            self.assertEqual(result['error'], 'BODY_TOO_LARGE')
            cfg['max_bytes'] = 1024
            cfg['targets'][0]['sha256'] = '0'*64
            result = await p.request_once(proxy, cfg['targets'][0], cfg, p.Rate(0))
            self.assertEqual(result['error'], 'HASH_MISMATCH')

    async def test_source_limits_and_safe_target_headers(self):
        args = SimpleNamespace(attempts=1, timeout=1, max_bytes=1024)
        with self.assertRaises(ValueError):
            p.validate_targets([dict(url='https://example.org/', headers={'Authorization':'secret'})], args)
        with self.assertRaises(p.SourceFetchError):
            await p._validate_source_destination('http://127.0.0.1:80')
        self.assertEqual(p._parse_source_url('https://example.org/list')[2], 443)

    async def test_source_limits_reject_oversized_response(self):
        payload = b'11.1.1.1:80\n'
        async def handler(reader, writer):
            await reader.readuntil(b'\r\n\r\n')
            writer.write(f'HTTP/1.1 200 OK\r\nContent-Length: {len(payload)}\r\n\r\n'.encode()+payload)
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        server = await asyncio.start_server(handler, '127.0.0.1', 0)
        async with server:
            url = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/list'
            report = await p.collect(self.db, [url], [], allow_private_sources=True, max_source_bytes=4)
        self.assertEqual(report['sources'][0]['error'], 'SOURCE_TOO_LARGE')
        self.assertEqual(report['unique'], 0)

    async def test_collect_streams_deduplicates_and_reports(self):
        payload = b'11.1.1.1:80\n11.1.1.1:80\nsocks5://11.2.2.2:1080\n127.0.0.1:80\nuser:secret@11.1.1.2:80\n'
        async def handler(reader, writer):
            await reader.readuntil(b'\r\n\r\n')
            writer.write(f'HTTP/1.1 200 OK\r\nContent-Length: {len(payload)}\r\n\r\n'.encode()+payload)
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        server = await asyncio.start_server(handler, '127.0.0.1', 0)
        async with server:
            url = f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}/list'
            result = await p.collect(self.db, [url, url], [], allow_private_sources=True)
        self.assertEqual(result['unique'], 2)
        self.assertEqual(result['raw_rows'], 5)
        self.assertEqual(result['sources'][0]['invalid'], 2)
        self.assertEqual(len(result['sources']), 1)

    async def test_quality_speed_filters_and_unlimited_export(self):
        cfg = config(2)
        self.db.execute('INSERT INTO profiles(id, config) VALUES (?, ?)', ('test', '{}'))
        stable = row('http://11.0.0.1:80', cfg, ms=100)
        fast = row('http://11.0.0.2:80', cfg, ms=1)
        fast['samples'][0]['ok'] = False
        fast = p.summarize(fast['proxy'], fast['samples'], cfg)
        failed_target = row('http://11.0.0.3:80', cfg)
        for sample in failed_target['samples']:
            if sample['target'] == 1:
                sample['ok'] = False
        failed_target = p.summarize(failed_target['proxy'], failed_target['samples'], cfg)
        for r in [stable, fast, failed_target]:
            add_candidate(self.db, (r['proxy']))
            store_result(self.db, ('test', r['proxy'], json.dumps(r)))
        self.db.commit()
        out = self.home/'out'
        report = p.export(self.db, 'test', out, min_success=2/3)
        self.assertEqual(report['exported'], 2)
        self.assertEqual(json.loads((out/'ranked.json').read_text(encoding='utf-8'))[0]['proxy'], stable['proxy'])
        p.export(self.db, 'test', out, sort='speed', min_success=2/3)
        self.assertEqual(json.loads((out/'ranked.json').read_text(encoding='utf-8'))[0]['proxy'], fast['proxy'])
        self.assertEqual(p.export(self.db, 'test', out, min_success=1)['exported'], 1)
        self.assertEqual(p.export(self.db, 'test', out, min_success=0)['exported'], 2)

    async def test_socks_source_and_geonode_all_pages_retry(self):
        from urllib.parse import urlsplit, parse_qs
        visits=[]
        async def handler(reader, writer):
            request=await reader.readuntil(b'\r\n\r\n')
            path=request.split(b' ')[1].decode()
            visits.append(path)
            if path.startswith('/socks'):
                payload=b'11.4.4.4:1080\n'
                status=200
            else:
                page=int(parse_qs(urlsplit(path).query)['page'][0])
                status=503 if len([x for x in visits if x==path])==1 and page==2 else 200
                payload=json.dumps(dict(data=[dict(ip=f'11.5.5.{page}',port=1080,protocols=['socks5','http'])],
                                        page=page,total=3,limit=1)).encode()
            writer.write(f'HTTP/1.1 {status} Test\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n\r\n'.encode()+payload)
            await writer.drain();writer.close();await writer.wait_closed()
        server=await asyncio.start_server(handler,'127.0.0.1',0)
        async with server:
            base=f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'
            report=await p.collect(self.db,['socks5 '+base+'/socks','geonode '+base+'/api?limit=1'],[],allow_private_sources=True)
        self.assertEqual(report['unique'],7)
        self.assertTrue(all(r['complete'] for r in report['sources']))
        geo=next(r for r in report['sources'] if r['format']=='geonode')
        self.assertEqual(geo['pages'],3)
        self.assertEqual(geo['attempts'],4)
        actual={r[0] for r in self.db.execute('SELECT proxy FROM candidates')}
        self.assertIn('socks5://11.4.4.4:1080',actual)
        self.assertNotIn('http://11.4.4.4:1080',actual)

    def test_denylist_parser_and_request_profiles(self):
        denylist = Denylist.from_text('# local\n11.0.0.0/24\n2001:db8::/32\nhttp://11.0.0.1:8080\nbad-rule\n', normalizer=p.normalize)
        self.assertEqual(denylist.match('http://11.0.0.9:3128'), 'cidr')
        self.assertEqual(denylist.match('socks5://[2001:db8::1]:1080'), 'cidr')
        self.assertEqual(denylist.match('http://11.0.0.1:8080'), 'proxy')
        self.assertIsNone(denylist.match('http://11.0.1.1:8080'))
        self.assertEqual(denylist.invalid, ['bad-rule'])
        headers = merge_headers('workbench', {'user-agent':'Custom/1.0', 'X-Test':'yes'})
        self.assertEqual(headers['user-agent'], 'Custom/1.0')
        self.assertEqual(headers['X-Test'], 'yes')
        self.assertNotIn('User-Agent', headers)
        self.assertEqual(p.public_url('https://example.org/private/path?token=secret'), 'https://example.org/')

    async def test_dnsbl_mock_and_strict_policy(self):
        denylist = Denylist.empty()
        self.assertFalse(make_policy({'dnsbl_enabled':True, 'dnsbl_zones':[]}, denylist)['dnsbl_enabled'])
        policy = make_policy({'dnsbl_enabled':True, 'dnsbl_zones':['bl.example.org'], 'strict':True}, denylist)
        calls = 0
        async def resolver(query):
            nonlocal calls
            calls += 1
            return [(2, 1, 6, '', ('127.0.0.2', 0))] if calls == 1 else []
        listed = await screen_proxy('http://11.1.1.1:80', policy, denylist, resolver=resolver)
        self.assertEqual(listed['status'], 'listed')
        clear = await screen_proxy('http://11.1.1.2:80', policy, denylist, resolver=resolver)
        self.assertEqual(clear['status'], 'clean')
        row = dict(min_target_reliability=1, proxy='http://11.1.1.1:80', reputation=listed)
        self.assertFalse(result_allowed(row, 1, denylist, strict=True))
        self.assertTrue(result_allowed(dict(row, reputation=clear), 1, denylist, strict=True))

    async def test_no_dnsbl_does_not_claim_a_verified_clean_verdict(self):
        policy = make_policy({}, Denylist.empty())
        verdict = await screen_proxy('http://11.1.1.1:80', policy, Denylist.empty())
        self.assertEqual(verdict['status'], 'unknown')
        self.assertEqual(verdict['dnsbl'], [])

    async def test_scan_stores_blocked_verdict_and_resumes(self):
        add_candidate(self.db, ('http://11.4.4.4:80'))
        self.db.commit()
        denylist = Denylist.from_text('11.4.4.0/24', normalizer=p.normalize)
        cfg = config()
        cfg['reputation'] = make_policy({}, denylist)
        calls = []
        async def probe(*args):
            calls.append(args[0])
            return row(args[0], cfg)
        async def screen(proxy, scan_config):
            return await screen_proxy(proxy, scan_config['reputation'], denylist)
        profile = await p.scan(self.db, cfg, workers=1, rate=0, probe=probe, screen=screen,
                                denylist=denylist, progress=False)
        self.assertEqual(calls, [])
        stored = json.loads(self.db.execute('SELECT payload FROM results WHERE profile=?', (profile,)).fetchone()[0])
        self.assertEqual(stored['reputation']['status'], 'local_denied')
        await p.scan(self.db, cfg, workers=1, rate=0, probe=probe, screen=screen,
                     denylist=denylist, progress=False)
        self.assertEqual(calls, [])

    def test_export_filters_blacklist_and_cleanliness(self):
        cfg = config()
        cfg['reputation'] = make_policy({'strict':True}, Denylist.empty())
        self.db.execute('INSERT INTO profiles(id, config) VALUES (?, ?)', ('fixture', json.dumps(cfg)))
        for index, status in enumerate(('clean', 'listed', 'unknown')):
            value = row(f'http://11.6.6.{index+1}:80', cfg)
            value['reputation'] = {'status':status, 'dnsbl':[], 'checked_at':0}
            add_candidate(self.db, (value['proxy']))
            store_result(self.db, ('fixture', value['proxy'], json.dumps(value)))
        self.db.commit()
        report = p.export(self.db, 'fixture', self.home/'clean-out', min_success=1, denylist=Denylist.empty())
        self.assertEqual(report['exported'], 1)
        self.assertEqual(report['reputation']['counts']['listed'], 1)
        self.assertEqual(json.loads((self.home/'clean-out'/'ranked.json').read_text(encoding='utf-8'))[0]['reputation_status'], 'clean')
        self.assertTrue((self.home/'clean-out'/'current.json').exists())

    def test_export_generation_retention_is_bounded(self):
        root = self.home/'exports'/'generations'
        root.mkdir(parents=True)
        for index in range(5):
            path = root/f'.generation-{index}'
            path.mkdir()
            (path/'status.json').write_text('{}', encoding='utf-8')
        removed, failed, remaining = p.prune_export_generations(self.home/'exports', keep=2)
        self.assertEqual(len(removed), 3)
        self.assertEqual(failed, [])
        self.assertEqual(remaining, 2)
        self.assertEqual(len(list(root.iterdir())), 2)

    def test_clear_runtime_preserves_user_settings(self):
        (self.home/'gui-settings.json').write_text('{}', encoding='utf-8')
        (self.home/'denylist.txt').write_text('11.0.0.0/24\n', encoding='utf-8')
        (self.home/'gui-selection.json').write_text('["http://11.0.0.1:80"]', encoding='utf-8')
        (self.home/'proxies.sqlite3').write_bytes(b'db')
        (self.home/'exports').mkdir()
        (self.home/'exports'/'proxies.txt').write_text('11.0.0.1:80\n', encoding='utf-8')
        removed = clear_runtime(self.home, keep_lock=True)
        self.assertIn('proxies.sqlite3', removed)
        self.assertIn('gui-selection.json', removed)
        self.assertTrue((self.home/'gui-settings.json').exists())
        self.assertTrue((self.home/'denylist.txt').exists())
        self.assertFalse((self.home/'gui-selection.json').exists())
        self.assertFalse((self.home/'exports').exists())

    def test_atomic_retries_transient_replace_denial(self):
        target = self.home / 'progress.json'
        real_replace = Path.replace
        calls = []

        def flaky_replace(source, destination):
            calls.append(destination)
            if len(calls) < 3:
                raise PermissionError('file is open in another process')
            return real_replace(source, destination)

        with mock.patch.object(Path, 'replace', flaky_replace):
            p.atomic(target, '{"checked": 1}')
        self.assertEqual(len(calls), 3)
        self.assertEqual(target.read_text(encoding='utf-8'), '{"checked": 1}')

        def always_denied(source, destination):
            raise PermissionError('still locked')

        with mock.patch.object(Path, 'replace', always_denied), mock.patch.object(p.time, 'sleep'):
            with self.assertRaises(PermissionError):
                p.atomic(target, '{}')

    def test_normalization(self):
        self.assertEqual(p.normalize('https://11.1.1.1:80'), 'https://11.1.1.1:80')
        for raw in ['ftp://11.1.1.1:80', '11.1.1.1:99999', 'http://u:p@11.1.1.1:80', '127.0.0.1:80', '11.1.1.1:80/a', None, 123]:
            self.assertIsNone(p.normalize(raw))


if __name__ == '__main__':
    unittest.main()
