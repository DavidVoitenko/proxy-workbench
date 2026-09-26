import asyncio
import copy
import json
import re
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import httpx
from tests.workbench_support import add_candidate, add_candidates, store_result  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from proxy_workbench import gui
from proxy_workbench import proxytool as p


class GuiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.server = gui.make_server(self.home)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = httpx.Client(base_url=f'http://127.0.0.1:{self.server.server_port}', trust_env=False,
                                   headers={'X-Workbench-Token': self.server.app.token}, timeout=5)

    def tearDown(self):
        self.server.app.close()
        self.client.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def await_job(self):
        # These checks launch a fresh interpreter and exercise real loopback
        # sockets; loaded CI runners need room for process startup as well as
        # the request deadlines.  A failure still reports the worker's state.
        deadline = time.monotonic() + 30
        result = {}
        while time.monotonic() < deadline:
            result = self.client.get('/api/state').json()
            if not result['running'] and 'exit_code' in result['job']:
                return result
            time.sleep(.05)
        self.fail(f'job did not finish: {result.get("progress")}; {result.get("log")}')

    def test_local_api_security_and_settings(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Proxies for your tasks', response.text)
        self.assertIn('id="lang-toggle"', response.text)
        self.assertIn(self.server.app.token, response.text)
        self.assertEqual(self.client.get('/api/state', headers={'X-Workbench-Token':''}).status_code, 403)
        self.assertEqual(self.client.post('/api/stop', json={}, headers={'Origin':'http://untrusted.invalid'}).status_code, 403)
        self.assertEqual(self.client.get('/api/state', headers={'Host':'untrusted.invalid'}).status_code, 403)
        self.assertEqual(self.client.get('/api/download/gui-settings.json').status_code, 404)
        settings = gui.defaults()
        settings['request_profile'] = 'minimal'
        settings['denylist'] = '11.9.0.0/24\n# local\n'
        settings['reputation'].update(dnsbl_enabled=True, dnsbl_zones=['bl.example.org'], strict=True)
        settings['targets'].append(dict(url='https://service.invalid/health', statuses=[204], name='API'))
        response = self.client.post('/api/settings', json=settings)
        self.assertEqual(response.status_code, 200, response.text)
        saved = self.client.get('/api/settings').json()
        self.assertEqual(len(saved['targets']), 2)
        self.assertEqual(saved['request_profile'], 'minimal')
        self.assertTrue(saved['reputation']['dnsbl_enabled'])
        self.assertIn('11.9.0.0/24', (self.home/'denylist.txt').read_text(encoding='utf-8'))
        settings['targets'][0]['headers'] = ['wrong']
        self.assertEqual(self.client.post('/api/settings', json=settings).status_code, 400)

    def test_hosting_labels_are_canonicalized_for_results_and_export(self):
        self.assertEqual(gui.normalize_hosting_filter('keep'), '')
        self.assertEqual(gui.normalize_hosting_filter('exclude'), 'hide')
        self.assertEqual(gui.normalize_hosting_filter('any'), '')
        with self.assertRaises(ValueError):
            gui.normalize_hosting_filter('maybe')

    def test_quick_filters_apply_real_clean_speed_and_http_conditions(self):
        db = p.open_db(self.home / 'proxies.sqlite3')
        cfg = dict(targets=[dict(url='http://service.invalid/')])
        db.execute('INSERT INTO profiles(id, config) VALUES (?, ?)', ('fixture', json.dumps(cfg)))
        fixtures = [
            ('http://11.0.0.1:80', 'clean', 10),
            ('https://11.0.0.2:443', 'unknown', 20),
            ('socks5://11.0.0.3:1080', 'clean', None),
        ]
        for proxy, status, mbps in fixtures:
            row = p.summarize(proxy, [dict(ok=True, ms=4, target=0, attempt=1)], cfg)
            row['reputation'] = {'status': status, 'dnsbl': []}
            if mbps is not None:
                row['speed'] = {'mbps': mbps}
            add_candidate(db, (proxy))
            store_result(db, ('fixture', proxy, json.dumps(row)))
        db.commit()
        # The published snapshot is what names the collection and the profile for
        # every reader; without one there is nothing for the surfaces to agree on
        # (CONTRACTS §1.2 rule 2).
        p.export(db, 'fixture', self.home / 'exports', min_success=1)
        db.close()
        (self.home / 'last-profile.txt').write_text('fixture', encoding='utf-8')
        def query(quick):
            response = self.client.get('/api/results?min_success=1&quick=' + quick)
            self.assertEqual(response.status_code, 200, response.text)
            return [row['proxy'] for row in response.json()['rows']]
        self.assertEqual(query('clean'), ['http://11.0.0.1:80', 'socks5://11.0.0.3:1080'])
        self.assertEqual(query('speed'), ['http://11.0.0.1:80', 'https://11.0.0.2:443'])
        self.assertEqual(query('http'), ['http://11.0.0.1:80', 'https://11.0.0.2:443'])
        self.assertEqual(self.client.get('/api/results?hosting=exclude').status_code, 200)

    def test_quick_test_uses_the_active_profile_targets(self):
        db = p.open_db(self.home / 'proxies.sqlite3')
        cfg = dict(attempts=1, timeout=2, max_bytes=1024, request_profile='workbench',
                   targets=[dict(name='one', url='http://one.invalid/', method='GET', statuses=[200],
                                 headers={}, contains=None, sha256=None),
                            dict(name='two', url='http://two.invalid/', method='GET', statuses=[200],
                                 headers={}, contains=None, sha256=None)])
        db.execute('INSERT INTO profiles(id, config) VALUES (?, ?)', ('fixture', json.dumps(cfg)))
        db.commit(); db.close()
        (self.home / 'last-profile.txt').write_text('fixture', encoding='utf-8')
        captured = {}
        async def fake_check(proxy, config, rate):
            captured['proxy'] = proxy
            captured['targets'] = [target['url'] for target in config['targets']]
            return dict(proxy=proxy, reliability=1, min_target_reliability=1, latency_ms=4,
                        jitter_ms=0, successes=2, requests=2, checked_at=time.time(), samples=[])
        with mock.patch.object(p, 'check_proxy', fake_check):
            result = self.server.app.test_proxy({'proxy': 'http://11.0.0.1:80', 'min_success': 1})
        self.assertTrue(result['ok'])
        self.assertEqual(captured['targets'], ['http://one.invalid/', 'http://two.invalid/'])
        self.assertNotIn('google.com', json.dumps(captured))

    def test_settings_export_validates_without_writing_credentials(self):
        payload = gui.defaults()
        payload['proxies'] = 'http://user:password@11.0.0.1:80\n'
        response = self.client.post('/api/settings/export', json=payload)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertFalse((self.home / 'gui-settings.json').exists())
        response = self.client.post('/api/settings/export', json=gui.defaults())
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse((self.home / 'gui-settings.json').exists())

        with self.assertRaises(ValueError):
            self.server.app.save(dict(gui.defaults(), proxies='http://user:password@11.0.0.1:80\n'))
        self.assertFalse((self.home / 'gui-settings.json').exists())

    def test_results_follow_stale_coherent_generation(self):
        db = p.open_db(self.home / 'proxies.sqlite3')
        cfg = dict(targets=[dict(url='http://service.invalid/')])
        proxy = 'http://11.0.0.1:80'
        row = p.summarize(proxy, [dict(ok=True, target=0, attempt=1, ms=1, status=200,
                                          error=None, bytes=1)], cfg)
        row['checked_at'] = time.time()
        row['valid_until'] = time.time() - 1
        db.execute('INSERT INTO profiles(id, config) VALUES (?, ?)', ('fixture', json.dumps(cfg)))
        add_candidate(db, (proxy))
        store_result(db, ('fixture', proxy, json.dumps(row)))
        db.commit()
        p.export(db, 'fixture', self.home / 'exports', min_success=1)
        db.close()
        (self.home / 'last-profile.txt').write_text('fixture', encoding='utf-8')
        response = self.client.get('/api/results?min_success=1')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['total'], 0)

    def test_existing_denylist_is_preserved_when_settings_omits_it(self):
        (self.home/'denylist.txt').write_text('11.8.0.0/24\n')
        payload = gui.defaults()
        payload.pop('denylist')
        saved = self.server.app.save(payload)
        self.assertIn('11.8.0.0/24', saved['denylist'])
        self.assertIn('11.8.0.0/24', (self.home/'denylist.txt').read_text(encoding='utf-8'))
        self.assertEqual(gui.public_source('https://example.org/list?token=secret', keyed=False), 'https://example.org/')

    def test_denylist_http_contract_is_normalized_and_atomic(self):
        response = self.client.post('/api/denylist/add', json={'proxies': ['11.0.0.1:80', 'http://11.0.0.1:80']})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {'added': ['http://11.0.0.1:80'], 'count': 1})
        before = (self.home / 'denylist.txt').read_text(encoding='utf-8')
        class Busy:
            def poll(self): return None
        self.server.app.process = Busy()
        busy = self.client.post('/api/denylist/add', json={'proxies': ['11.0.0.3:80']})
        self.server.app.process = None
        self.assertEqual(busy.status_code, 400)
        for payload in ({'proxies': ['http://user:pass@11.0.0.2:80']}, {'proxies': []}, {'entries': ['11.0.0.3:80']}):
            self.assertEqual(self.client.post('/api/denylist/add', json=payload).status_code, 400, payload)
        self.assertEqual((self.home / 'denylist.txt').read_text(encoding='utf-8'), before)

    def test_selected_export_validation_happens_before_settings_or_worker(self):
        db = p.open_db(self.home/'proxies.sqlite3')
        db.execute('INSERT INTO profiles(id, config) VALUES (?, ?)', ('fixture', json.dumps(dict(targets=[dict(url='https://one.invalid/')]))))
        db.commit(); db.close()
        (self.home/'last-profile.txt').write_text('fixture')
        saved = gui.defaults(); saved.update(top=7, sort='speed')
        self.server.app.save(saved)
        before = (self.home/'gui-settings.json').read_bytes()
        invalid = [
            {'action':'export', 'settings':saved, 'selection':[]},
            {'action':'export', 'settings':saved, 'selection':['proxy.example:80']},
            {'action':'export', 'settings':saved, 'selection':['http://user:pass@11.0.0.1:80']},
            {'action':'export', 'settings':saved, 'selection':['11.0.0.1:80'] * 1001},
            {'action':'export', 'settings':saved, 'selection':['11.0.0.1:80'], 'hosting':'maybe'},
            {'action':'scan', 'settings':saved, 'selection':['11.0.0.1:80']},
        ]
        with mock.patch.object(gui.subprocess, 'Popen') as popen:
            for payload in invalid:
                response = self.client.post('/api/start', json=payload)
                self.assertEqual(response.status_code, 400, (payload, response.text))
            (self.home/'last-profile.txt').write_text('missing-profile')
            missing = self.client.post('/api/start', json={'action':'export', 'settings':saved, 'selection':['11.0.0.1:80']})
            self.assertEqual(missing.status_code, 400)
            popen.assert_not_called()
        self.assertEqual((self.home/'gui-settings.json').read_bytes(), before)
        self.assertFalse((self.home/'gui-selection.json').exists())

    def test_selected_export_is_normalized_transient_and_passed_to_worker(self):
        db = p.open_db(self.home/'proxies.sqlite3')
        db.execute('INSERT INTO profiles(id, config) VALUES (?, ?)', ('fixture', json.dumps(dict(targets=[dict(url='https://one.invalid/')]))))
        db.commit(); db.close()
        (self.home/'last-profile.txt').write_text('fixture')
        saved = gui.defaults(); saved.update(top=7, sort='speed')
        self.server.app.save(saved)
        before = (self.home/'gui-settings.json').read_bytes()
        release = threading.Event()

        class Process:
            def poll(self):
                return 0 if release.is_set() else None

            def wait(self, timeout=None):
                release.wait(10 if timeout is None else timeout)
                return 0

        process = Process()
        payload = dict(action='export', settings=dict(saved, top=1), q='11.0.0', hosting='hide', quick='speed',
                       selection=['11.0.0.1:80', 'http://11.0.0.1:80', 'socks5://11.0.0.2:1080'])
        try:
            with mock.patch.object(gui.subprocess, 'Popen', return_value=process) as popen:
                response = self.client.post('/api/start', json=payload)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()['selection_requested'], 2)
                command = popen.call_args.args[0]
                option = lambda name: command[command.index(name) + 1]
                self.assertEqual(Path(option('--selection-file')).resolve(), (self.home/'gui-selection.json').resolve())
                self.assertEqual(option('--export-query'), '11.0.0')
                self.assertEqual(option('--export-hosting'), 'hide')
                self.assertEqual(option('--export-quick'), 'speed')
                self.assertIn('--no-hosting', command)
                self.assertEqual(option('--top'), '0')
                self.assertEqual(json.loads((self.home/'gui-selection.json').read_text(encoding='utf-8')),
                                 ['http://11.0.0.1:80', 'socks5://11.0.0.2:1080'])
                self.assertEqual((self.home/'gui-settings.json').read_bytes(), before)
        finally:
            release.set()
        state = self.await_job()
        self.assertEqual(state['job']['exit_code'], 0)
        self.assertFalse((self.home/'gui-selection.json').exists())
        self.assertEqual((self.home/'gui-settings.json').read_bytes(), before)

    def test_collect_job_dedup_and_empty_profile_scan(self):
        settings = gui.defaults()
        settings.update(use_sources=False, proxies='11.1.1.1:80\n11.1.1.1:80\n127.0.0.1:80\n')
        response = self.client.post('/api/start', json={'action':'collect','settings':settings})
        self.assertEqual(response.status_code, 200, response.text)
        result = self.await_job()
        self.assertEqual(result['job']['exit_code'], 0, result['log'])
        self.assertEqual(result['sources']['unique'], 1)
        self.assertEqual(result['sources']['sources'][0]['invalid'], 1)
        # Clear only this synthetic test candidate: no public requests in tests.
        db=sqlite3.connect(self.home/'proxies.sqlite3')
        db.execute('DELETE FROM candidates')
        # The scan scope is the collection membership, not the candidate list.
        db.execute('DELETE FROM membership')
        db.commit(); db.close()
        response = self.client.post('/api/start', json={'action':'scan','settings':settings})
        self.assertEqual(response.status_code, 200, response.text)
        result = self.await_job()
        self.assertEqual(result['job']['exit_code'], 0, result['log'])
        self.assertEqual(result['progress']['phase'], 'complete')
        self.assertEqual(result['export']['checked'], 0)
        self.assertEqual(self.client.get('/api/download/proxies.txt').status_code, 200)

        # A published generation is immutable and carries a manifest, so the only
        # honest way to have an expired one is to publish one whose row lifetime
        # has passed (CONTRACTS §4.2).
        db = p.open_db(self.home/'proxies.sqlite3')
        active = (self.home/'last-profile.txt').read_text(encoding='utf-8').strip()
        proxy = 'http://11.1.1.1:80'
        add_candidate(db, (proxy))
        store_result(db, (active, proxy, json.dumps(
            dict(proxy=proxy, reliability=1, min_target_reliability=1, latency_ms=10, jitter_ms=1,
                 score=90, successes=1, requests=1, checked_at=time.time() - 7200,
                 valid_until=time.time() - 3600, samples=[]))), valid_until=time.time() - 3600)
        db.commit()
        p.export(db, active, self.home/'exports', min_success=1)
        db.close()
        state = self.client.get('/api/state').json()
        # A row whose lifetime has passed is not served: the table is empty and
        # the download yields an empty file, not an expired address.  The set
        # state names the reason instead of calling it "complete" (defect 3).
        # The published set is empty because its only row had expired, and the
        # status says so instead of claiming a completed export.
        self.assertEqual(state['export']['state'], 'empty')
        self.assertEqual(state['export']['exported'], 0)
        self.assertEqual(state['export']['available'], 0)
        self.assertFalse(state['export']['complete'])
        self.assertEqual(self.client.get('/api/results?min_success=1').json()['total'], 0)
        self.assertEqual(self.client.get('/api/download/proxies.txt').text.strip(), '')

    def test_results_require_every_service_and_export_matches_sort(self):
        db = p.open_db(self.home/'proxies.sqlite3')
        cfg = dict(targets=[dict(url='https://one.invalid/'),dict(url='https://two.invalid/')])
        db.execute('INSERT INTO profiles(id, config) VALUES (?, ?)', ('fixture', json.dumps(cfg)))
        for i in range(65):
            proxy = f'http://11.0.0.{i}:80'
            samples=[dict(target=t, attempt=a+1, ok=not(i==0 and t==1), ms=100+i,
                          bytes=1, status=200, error=None) for t in range(2) for a in range(3)]
            row = p.summarize(proxy, samples, cfg)
            add_candidate(db, (proxy))
            store_result(db, ('fixture',proxy,json.dumps(row)))
        listed = p.summarize('http://11.0.0.200:80', [dict(target=0, attempt=1, ok=True, ms=5, bytes=1, status=200, error=None), dict(target=1, attempt=1, ok=True, ms=5, bytes=1, status=200, error=None)], cfg)
        listed['reputation'] = {'status':'listed', 'dnsbl':[{'zone':'bl.example.org','status':'listed'}], 'checked_at':0}
        add_candidate(db, (listed['proxy']))
        store_result(db, ('fixture',listed['proxy'],json.dumps(listed)))
        db.commit()
        p.export(db, 'fixture', self.home/'exports', min_success=1)
        db.close()
        (self.home/'last-profile.txt').write_text('fixture')
        first=self.client.get('/api/results?sort=speed&min_success=0').json()
        self.assertEqual(first['total'],64)
        self.assertEqual(len(first['rows']),50)
        self.assertEqual(first['rows'][0]['proxy'],'http://11.0.0.1:80')
        second=self.client.get('/api/results?sort=speed&min_success=0&offset=50').json()
        self.assertEqual(len(second['rows']),14)
        settings=gui.defaults();settings.update(top=7,sort='speed',min_success=0)
        self.client.post('/api/start',json=dict(action='export',settings=settings)).raise_for_status()
        state=self.await_job()
        self.assertEqual(state['job']['exit_code'],0,state['log'])
        content=self.client.get('/api/download/proxies.txt').text.splitlines()
        self.assertEqual(content,[r['proxy'] for r in first['rows'][:7]])

    def test_result_detail_is_loaded_on_demand(self):
        db = p.open_db(self.home/'detail.sqlite3')
        cfg = dict(version=2, targets=[dict(url='https://service.invalid/')], request_profile='workbench', reputation={})
        db.execute('INSERT INTO profiles(id, config) VALUES (?, ?)', ('detail', json.dumps(cfg)))
        row = p.summarize('http://11.0.0.1:80', [dict(target=0, attempt=1, ok=True, ms=4, bytes=1, status=200, error=None)], cfg)
        add_candidate(db, (row['proxy']))
        store_result(db, ('detail', row['proxy'], json.dumps(row)))
        db.commit(); db.close()
        (self.home/'proxies.sqlite3').write_bytes((self.home/'detail.sqlite3').read_bytes())
        (self.home/'last-profile.txt').write_text('detail')
        response = self.client.get('/api/result-detail', params={'proxy':row['proxy']})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()['samples']), 1)

    def test_clear_data_preserves_settings(self):
        (self.home/'gui-settings.json').write_text('{}', encoding='utf-8')
        (self.home/'denylist.txt').write_text('11.0.0.0/24\n', encoding='utf-8')
        (self.home/'proxies.sqlite3').write_bytes(b'db')
        (self.home/'exports').mkdir()
        response = self.client.post('/api/clear-data', json={})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue((self.home/'gui-settings.json').exists())
        self.assertTrue((self.home/'denylist.txt').exists())
        self.assertFalse((self.home/'proxies.sqlite3').exists())

    def test_cooperative_stop_and_progress(self):
        async def run():
            db=p.open_db(self.home/'stop.sqlite3')
            cfg=dict(targets=[dict(url='http://service.invalid')],attempts=1)
            add_candidates(db, ((f'http://11.0.0.{i}:80',) for i in range(20)))
            db.commit()
            stopped=self.home/'stop-marker'
            seen=[]; events=[]
            async def probe(proxy,cfg,rate):
                seen.append(proxy)
                if len(seen)==4:
                    stopped.write_text('stop')
                    await asyncio.sleep(60)
                return p.summarize(proxy,[dict(ok=True,ms=10,target=0)],cfg)
            with self.assertRaises(asyncio.CancelledError):
                await p.stoppable(p.scan(db,cfg,workers=1,probe=probe,progress=False,on_progress=events.append),stopped)
            self.assertEqual(db.execute('SELECT count(*) FROM results').fetchone()[0],3)
            self.assertEqual(events[-1]['checked'],3)
            db.close()
        asyncio.run(run())

    def test_real_worker_checks_two_services_and_stop_button(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        requested=[]
        request_started = threading.Event()
        class Proxy(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self):
                requested.append(self.path)
                request_started.set()
                if self.server.slow:
                    time.sleep(2)
                body=b'healthy'
                code=503 if self.server.reject_second and self.path.endswith('/two') else 200
                try:
                    self.send_response(code);self.send_header('Content-Length',str(len(body)));self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError,ConnectionResetError):
                    pass
        servers=[]
        try:
            for reject in (False,True):
                server=ThreadingHTTPServer(('127.0.0.1',0),Proxy)
                server.reject_second=reject;server.slow=False
                threading.Thread(target=server.serve_forever,daemon=True).start()
                servers.append(server)
            db=p.open_db(self.home/'proxies.sqlite3')
            proxies=[f'http://127.0.0.1:{s.server_port}' for s in servers]
            add_candidates(db, ((proxy,) for proxy in proxies))
            db.commit();db.close()
            settings=gui.defaults()
            settings.update(targets=[dict(url='http://service.invalid/'+name,contains='healthy',statuses=[200]) for name in ('one','two')],
                            workers=2,rate=0,timeout=5,min_success=1,use_sources=False)
            self.client.post('/api/start',json=dict(action='scan',settings=settings)).raise_for_status()
            result=self.await_job()
            self.assertEqual(result['job']['exit_code'],0,result['log'])
            # `checked` is what the run checked; `admitted` is what survived
            # admission.  The GUI recomputes the latter from the snapshot.
            self.assertEqual(result['progress']['checked'], 2)
            self.assertEqual(result['progress']['passed'], 1)
            self.assertEqual(result['export']['admitted'], 1)
            # 6 requests for the good proxy; fail-fast stops the rejecting one after its
            # first failure on service two (strict threshold), instead of 6 more.
            self.assertEqual(len(requested),8)
            self.assertEqual(self.client.get('/api/download/proxies.txt').text.strip(),proxies[0])
            servers[0].slow=True;servers[1].slow=True
            request_started.clear()
            self.client.post('/api/start',json=dict(action='recheck',settings=settings)).raise_for_status()
            self.assertTrue(request_started.wait(30), 'recheck did not reach the local proxy')
            self.client.post('/api/stop',json={}).raise_for_status()
            result=self.await_job()
            self.assertEqual(result['job']['exit_code'],130,result['log'])
            self.assertEqual(result['progress']['phase'],'stopped')
            # A stopped recheck never replaces the last coherent current export.
            self.assertEqual(result['export']['admitted'], 1)
            self.assertEqual(result['diagnostic']['state'], 'partial')
            self.assertEqual(result['diagnostic']['stop_reason'], 'stopped')
            self.assertLess(result['diagnostic']['checked'], 2)
        finally:
            for server in servers:
                server.shutdown();server.server_close()

    def test_cancelled_new_profile_keeps_previous_active_profile(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        request_started = threading.Event()
        class Proxy(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self):
                request_started.set()
                if self.server.slow:
                    time.sleep(2)
                body=b'healthy'
                try:
                    self.send_response(200); self.send_header('Content-Length',str(len(body))); self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Proxy)
        server.slow=False
        threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            db=p.open_db(self.home/'proxies.sqlite3')
            proxy=f'http://127.0.0.1:{server.server_port}'
            add_candidate(db, (proxy))
            db.commit(); db.close()
            settings=gui.defaults()
            settings.update(targets=[dict(url='http://service.invalid/one',contains='healthy',statuses=[200])],
                            workers=1,rate=0,timeout=5,min_success=1,use_sources=False)
            self.client.post('/api/start',json=dict(action='scan',settings=settings)).raise_for_status()
            first=self.await_job()
            self.assertEqual(first['job']['exit_code'],0,first['log'])
            previous_profile=(self.home/'last-profile.txt').read_text(encoding='utf-8').strip()

            settings['targets'][0]['url']='http://service.invalid/new'
            server.slow=True
            request_started.clear()
            self.client.post('/api/start',json=dict(action='scan',settings=settings)).raise_for_status()
            self.assertTrue(request_started.wait(30), 'scan did not reach the local proxy')
            self.client.post('/api/stop',json={}).raise_for_status()
            cancelled=self.await_job()
            self.assertEqual(cancelled['job']['exit_code'],130,cancelled['log'])
            self.assertEqual((self.home/'last-profile.txt').read_text(encoding='utf-8').strip(), previous_profile)
            self.assertEqual(cancelled['export']['profile'], previous_profile)
        finally:
            server.shutdown(); server.server_close()

    def test_keep_fresh_schedule_waits_and_stops(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        class Proxy(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self):
                body=b'healthy'
                self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers()
                self.wfile.write(body)
        server=ThreadingHTTPServer(('127.0.0.1',0),Proxy)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            db=p.open_db(self.home/'proxies.sqlite3')
            add_candidate(db, (f'http://127.0.0.1:{server.server_port}'))
            db.commit();db.close()
            settings=gui.defaults()
            settings.update(targets=[dict(url='http://service.invalid/one',contains='healthy',statuses=[200])],
                            workers=1,rate=0,timeout=5,min_success=1,watch=1,use_sources=False)
            self.client.post('/api/start',json=dict(action='scan',settings=settings)).raise_for_status()
            deadline=time.monotonic()+30
            while time.monotonic()<deadline:
                state=self.client.get('/api/state').json()
                if state['progress'].get('phase')=='waiting': break
                time.sleep(.1)
            self.assertEqual(state['progress']['phase'],'waiting',state.get('log'))
            self.assertTrue(state['running'])
            self.assertGreater(state['progress']['next_check_at'],time.time()+30)
            self.assertEqual(state['export']['passed'],1)
            self.client.post('/api/stop',json={}).raise_for_status()
            result=self.await_job()
            self.assertEqual(result['job']['exit_code'],130,result['log'])
        finally:
            server.shutdown();server.server_close()
        with self.assertRaises(ValueError):
            gui.validate(dict(gui.defaults(),watch=-1))

    def test_only_one_gui_per_data_folder(self):
        with self.assertRaises(OSError):
            gui.App(self.home)

    def test_second_start_rejected_while_job_active(self):
        # Simulated process keeps this test strictly local and deterministic.
        class Busy:
            def poll(self): return None
        self.server.app.process=Busy()
        response=self.client.post('/api/start',json=dict(action='run',settings=gui.defaults()))
        self.server.app.process=None
        self.assertEqual(response.status_code,400)


class TranslationTests(unittest.TestCase):
    def test_every_ui_key_is_translated(self):
        ui = Path(__file__).resolve().parents[1]/'proxy_workbench'/'ui'
        script = (ui/'app.js').read_text(encoding='utf-8')
        page = (ui/'index.html').read_text(encoding='utf-8')
        block = script[script.index('const messages = {'):script.index('\n};\n')]
        english, russian = block.split('\n  ru: {')
        keys = lambda text: re.findall(r"^    '([\w.]+)': '((?:[^'\\]|\\.)*)',?$", text, re.M)
        english, russian = dict(keys(english)), dict(keys(russian))
        self.assertGreater(len(english), 100)
        self.assertEqual(english.keys(), russian.keys())
        self.assertFalse([key for key, value in english.items() if re.search('[\u0400-\u04ff]', value)])
        self.assertFalse(re.search('[\u0400-\u04ff]', page))
        used = set(re.findall(r'data-i18n(?:-[a-z-]+)?="([^"]+)"', page))
        used |= set(re.findall(r"(?:\bt\(|text\(|attr\('[a-z-]+', )'([a-zA-Z]+\.[\w.]+)'", script))
        # `t('results.detail.' + detailId)` is a prefix being concatenated, not a
        # whole key; a regex cannot tell, so drop anything the code goes on to
        # build upon rather than loosening the check for real missing keys.
        used = {key for key in used if not key.endswith('.')}
        self.assertFalse(used - english.keys())
    def test_scenarios_set_their_defining_workflow_fields(self):
        ui = Path(__file__).resolve().parents[1]/'proxy_workbench'/'ui'
        script = (ui/'app.js').read_text(encoding='utf-8')
        self.assertIn('function setScenarioTargets', script)
        self.assertIn("url: 'https://telegram.org/'", script)
        self.assertIn("url: 'https://www.youtube.com/'", script)
        self.assertIn("setVal('judge-url', 'http://azenv.net/')", script)
        self.assertIn("setVal('dnsbl-zones', 'zen.spamhaus.org", script)
        self.assertIn("t('scenario.customUnchanged')", script)

    def test_download_and_offline_gateway_controls_have_real_targets(self):
        ui = Path(__file__).resolve().parents[1]/'proxy_workbench'/'ui'
        page = (ui/'index.html').read_text(encoding='utf-8')
        script = (ui/'app.js').read_text(encoding='utf-8')
        self.assertIn('data-download="proxies.txt"', page)
        self.assertNotIn('data-download="ranked.txt"', page)
        self.assertIn('id="copy-gateway" disabled', page)
        self.assertIn('id="copy-gateway-hero" title="Copy endpoint" disabled', page)
        self.assertNotIn('id="gw-tg-link" href=', page)
        self.assertNotIn('id="mobile-tg-btn-link" href=', page)
        self.assertIn("if (!state.gateway || !state.gateway.address)", script)
        self.assertIn("} else {\n    if ($('gateway-address'))", script)

    def test_result_filters_do_not_sync_back_into_scan_settings(self):
        ui = Path(__file__).resolve().parents[1]/'proxy_workbench'/'ui'
        script = (ui/'app.js').read_text(encoding='utf-8')
        self.assertNotIn('syncResultsToScan', script)
        self.assertIn('Results controls are view/export filters only', script)

        ui = Path(__file__).resolve().parents[1]/'proxy_workbench'/'ui'
        script = (ui/'app.js').read_text(encoding='utf-8')
        page = (ui/'index.html').read_text(encoding='utf-8')
        identifiers = re.findall(r'\bid="([^"]+)"', page)
        self.assertEqual(len(identifiers), len(set(identifiers)))
        self.assertNotIn('selected-proxies.txt', script)
        self.assertNotIn('{entries: Array.from(selectedProxies)}', script)
        self.assertIn('request.selection = selection', script)
        self.assertIn("api('/api/denylist/add', {proxies:Array.from(selectedProxies)})", script)
        self.assertIn("t('confirm.denylist'", script)
        self.assertIn("snapshotNotes(exportReport)", script)
        self.assertEqual(script.count('function getCountryFlag('), 1)
        for action in ('export-settings', 'import-settings', 'clear-data'):
            self.assertEqual(page.count(f'data-action="{action}"'), 1)
            self.assertIn(f"document.querySelectorAll('[data-action=\"{action}\"]')", script)
        self.assertIn('data-i18n-aria-label="results.selectedRegion"', page)
        self.assertIn('data-i18n-aria-label="results.selectAll"', page)
        self.assertIn("node.onkeydown", script)
        self.assertIn("card.onkeydown", script)
        self.assertIn("row.setAttribute('role', 'option')", script)
        self.assertIn("copyGatewayAddress(event.currentTarget)", script)
        self.assertIn("document.querySelectorAll('[data-copy-proxy]')", script)
        self.assertIn("button.dataset.copyText", script)


if __name__=='__main__':
    unittest.main()
