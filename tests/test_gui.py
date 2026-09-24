import asyncio
import copy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest

import httpx
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import gui
import proxytool as p


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
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = self.client.get('/api/state').json()
            if not result['running'] and 'exit_code' in result['job']:
                return result
            time.sleep(.05)
        self.fail('job did not finish')

    def test_local_api_security_and_settings(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Прокси под ваши задачи', response.text)
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

    def test_existing_denylist_is_preserved_when_settings_omits_it(self):
        (self.home/'denylist.txt').write_text('11.8.0.0/24\n')
        payload = gui.defaults()
        payload.pop('denylist')
        saved = self.server.app.save(payload)
        self.assertIn('11.8.0.0/24', saved['denylist'])
        self.assertIn('11.8.0.0/24', (self.home/'denylist.txt').read_text(encoding='utf-8'))
        self.assertEqual(gui.public_source('https://example.org/list?token=secret'), 'https://example.org/')

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
        db.commit(); db.close()
        response = self.client.post('/api/start', json={'action':'scan','settings':settings})
        self.assertEqual(response.status_code, 200, response.text)
        result = self.await_job()
        self.assertEqual(result['job']['exit_code'], 0, result['log'])
        self.assertEqual(result['progress']['phase'], 'complete')
        self.assertEqual(result['export']['checked'], 0)
        self.assertEqual(self.client.get('/api/download/proxies.txt').status_code, 200)

    def test_results_require_every_service_and_export_matches_sort(self):
        db = p.open_db(self.home/'proxies.sqlite3')
        cfg = dict(targets=[dict(url='https://one.invalid/'),dict(url='https://two.invalid/')])
        db.execute('INSERT INTO profiles VALUES (?,?)', ('fixture', json.dumps(cfg)))
        for i in range(65):
            proxy = f'http://11.0.0.{i}:80'
            samples=[dict(target=t, attempt=a+1, ok=not(i==0 and t==1), ms=100+i,
                          bytes=1, status=200, error=None) for t in range(2) for a in range(3)]
            row = p.summarize(proxy, samples, cfg)
            db.execute('INSERT INTO candidates VALUES (?)',(proxy,))
            db.execute('INSERT INTO results VALUES (?,?,?)',('fixture',proxy,json.dumps(row)))
        listed = p.summarize('http://11.0.0.200:80', [dict(target=0, attempt=1, ok=True, ms=5, bytes=1, status=200, error=None), dict(target=1, attempt=1, ok=True, ms=5, bytes=1, status=200, error=None)], cfg)
        listed['reputation'] = {'status':'listed', 'dnsbl':[{'zone':'bl.example.org','status':'listed'}], 'checked_at':0}
        db.execute('INSERT INTO candidates VALUES (?)',(listed['proxy'],))
        db.execute('INSERT INTO results VALUES (?,?,?)',('fixture',listed['proxy'],json.dumps(listed)))
        db.commit(); db.close()
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
        db.execute('INSERT INTO profiles VALUES (?,?)', ('detail', json.dumps(cfg)))
        row = p.summarize('http://11.0.0.1:80', [dict(target=0, attempt=1, ok=True, ms=4, bytes=1, status=200, error=None)], cfg)
        db.execute('INSERT INTO candidates VALUES (?)', (row['proxy'],))
        db.execute('INSERT INTO results VALUES (?,?,?)', ('detail', row['proxy'], json.dumps(row)))
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
            db.executemany('INSERT INTO candidates VALUES (?)',((f'http://11.0.0.{i}:80',) for i in range(20)))
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
        class Proxy(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self):
                requested.append(self.path)
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
            db.executemany('INSERT INTO candidates VALUES (?)',((proxy,) for proxy in proxies))
            db.commit();db.close()
            settings=gui.defaults()
            settings.update(targets=[dict(url='http://service.invalid/'+name,contains='healthy',statuses=[200]) for name in ('one','two')],
                            workers=2,rate=0,timeout=5,min_success=1)
            self.client.post('/api/start',json=dict(action='scan',settings=settings)).raise_for_status()
            result=self.await_job()
            self.assertEqual(result['job']['exit_code'],0,result['log'])
            self.assertEqual(result['export']['checked'],2)
            self.assertEqual(result['export']['passed'],1)
            self.assertEqual(len(requested),12)
            self.assertEqual(self.client.get('/api/download/proxies.txt').text.strip(),proxies[0])
            servers[0].slow=True;servers[1].slow=True
            self.client.post('/api/start',json=dict(action='recheck',settings=settings)).raise_for_status()
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                if self.client.get('/api/state').json()['progress'].get('phase')=='scanning': break
                time.sleep(.03)
            self.client.post('/api/stop',json={}).raise_for_status()
            result=self.await_job()
            self.assertEqual(result['job']['exit_code'],130,result['log'])
            self.assertEqual(result['progress']['phase'],'stopped')
            self.assertLess(result['export']['checked'],2)
        finally:
            for server in servers:
                server.shutdown();server.server_close()

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


if __name__=='__main__':
    unittest.main()
