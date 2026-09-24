#!/usr/bin/env python3
"""Loopback-only browser interface; no external server or frontend dependencies."""
from __future__ import annotations

import argparse
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit
import webbrowser

import proxytool as core

ROOT = Path(__file__).resolve().parent
MAX_BODY = 32 * 1024 * 1024


def read_json(path, fallback):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return fallback


def defaults():
    return dict(targets=[dict(name='Проверка HTTPS', url='https://example.com/', statuses=[200],
                             contains='Example Domain', headers={}, method='GET')],
                sources=json.loads((ROOT/'sources.json').read_text()), use_sources=True,
                proxies='', attempts=3, timeout=8, workers=128, rate=100,
                max_bytes=1048576, source_timeout=60, min_success=2/3, top=0, sort='quality')


def validate(settings):
    if not isinstance(settings, dict):
        raise ValueError('Ожидаются настройки проверки.')
    clean = defaults()
    clean.update({k: settings[k] for k in clean if k in settings})
    for key, low, high, integer in [('attempts', 1, 100, True), ('timeout', .1, 300, False),
            ('workers', 1, 2048, True), ('rate', 0, 10000, False), ('max_bytes', 1, 100_000_000, True),
            ('source_timeout', 1, 3600, False), ('top', 0, 1_000_000_000, True), ('min_success', 0, 1, False)]:
        value = clean[key]
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high or (integer and int(value) != value):
            raise ValueError(f'Недопустимое значение: {key}.')
        clean[key] = int(value) if integer else value
    if clean['sort'] not in ('speed', 'quality') or type(clean['use_sources']) is not bool:
        raise ValueError('Неверный режим сортировки или источников.')
    if not isinstance(clean['proxies'], str) or len(clean['proxies']) > 20_000_000:
        raise ValueError('Список прокси слишком большой: максимум 20 МБ.')
    if not isinstance(clean['sources'], list) or len(clean['sources']) > 5000:
        raise ValueError('Источники должны быть списком URL (до 5000).')
    for url in clean['sources']:
        core.source_spec(url)
    clean['sources'] = list(dict.fromkeys(clean['sources']))
    targets = clean['targets']
    if not isinstance(targets, list) or not 1 <= len(targets) <= 20:
        raise ValueError('Добавьте от 1 до 20 сервисов.')
    # Use the exact same normalization/validation as the scanner without writing a file.
    args = argparse.Namespace(config=None, url=None, attempts=clean['attempts'],
                              timeout=clean['timeout'], max_bytes=clean['max_bytes'])
    normalized = core.validate_targets(copy.deepcopy(targets), args)
    for t in normalized['targets']:
        name = t.get('name', '')
        if not isinstance(name, str) or len(name) > 160:
            raise ValueError('Название сервиса: максимум 160 символов.')
    clean['targets'] = normalized['targets']
    return clean


class App:
    def __init__(self, data):
        self.data = data.resolve()
        self.data.mkdir(parents=True, exist_ok=True)
        self.instance_lock = (self.data/'gui-instance.lock').open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                self.instance_lock.write(b'0'); self.instance_lock.flush(); self.instance_lock.seek(0)
                msvcrt.locking(self.instance_lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.instance_lock.close()
            raise OSError('GUI already running') from None
        self.token = secrets.token_urlsafe(32)
        self.mutex = threading.RLock()
        self.process = None
        self.log_handle = None
        self.job = read_json(self.data/'gui-job.json', {})
        self.stop_path = self.data/'gui-stop'
        self.progress_path = self.data/'gui-progress.json'

    def settings(self):
        return read_json(self.data/'gui-settings.json', defaults())

    def save(self, payload):
        settings = validate(payload)
        with self.mutex:
            core.atomic(self.data/'gui-settings.json', json.dumps(settings, ensure_ascii=False, indent=2))
        return settings

    def running(self):
        return self.process is not None and self.process.poll() is None

    def start(self, payload):
        with self.mutex:
            if self.running():
                raise ValueError('Проверка уже идёт. Сначала остановите её.')
            action = payload.get('action', 'run')
            if action not in ('run', 'scan', 'recheck', 'collect', 'export'):
                raise ValueError('Неизвестное действие.')
            settings = self.save(payload.get('settings', self.settings()))
            if action == 'export' and not (self.data/'last-profile.txt').exists():
                raise ValueError('Сначала запустите проверку.')
            if action in ('run', 'collect') and not settings['proxies'].strip() and (not settings['use_sources'] or not settings['sources']):
                raise ValueError('Включите источники или добавьте свой список прокси.')
            core.atomic(self.data/'gui-targets.json', json.dumps({'targets': settings['targets']}))
            core.atomic(self.data/'gui-sources.json', json.dumps(settings['sources']))
            core.atomic(self.data/'gui-input.txt', settings['proxies'])
            self.stop_path.unlink(missing_ok=True)
            core.atomic(self.progress_path, json.dumps(dict(phase='starting', checked=0, candidates=0)))
            command = [sys.executable, '-u', str(ROOT/'proxytool.py'), 'scan' if action == 'recheck' else action,
                       '--data', str(self.data), '--config', str(self.data/'gui-targets.json'),
                       '--sources', str(self.data/'gui-sources.json'), '--input', str(self.data/'gui-input.txt'),
                       '--progress-file', str(self.progress_path), '--stop-file', str(self.stop_path)]
            for key in ('attempts', 'timeout', 'workers', 'rate', 'max_bytes', 'source_timeout', 'min_success', 'top', 'sort'):
                command.extend(['--'+key.replace('_', '-'), str(settings[key])])
            if not settings['use_sources']:
                command.append('--no-sources')
            if action == 'recheck':
                command.append('--recheck')
            self.job = dict(id=secrets.token_hex(8), action=action, started_at=time.time(),
                            targets=[dict(name=t.get('name', ''), url=t['url']) for t in settings['targets']],
                            min_success=settings['min_success'], sort=settings['sort'], top=settings['top'])
            if self.log_handle:
                self.log_handle.close()
            self.log_handle = (self.data/'gui-run.log').open('wb')
            try:
                self.process = subprocess.Popen(command, stdout=self.log_handle, stderr=subprocess.STDOUT,
                                                stdin=subprocess.DEVNULL, cwd=ROOT)
            except OSError:
                self.log_handle.close()
                self.log_handle = None
                raise ValueError('Не удалось запустить проверку.') from None
            core.atomic(self.data/'gui-job.json', json.dumps(self.job))
            threading.Thread(target=self._wait, args=(self.process,), daemon=True).start()
            return self.job

    def _wait(self, process):
        code = process.wait()
        with self.mutex:
            if self.process is process:
                self.job.update(exit_code=code, finished_at=time.time())
                core.atomic(self.data/'gui-job.json', json.dumps(self.job))
                if self.log_handle:
                    self.log_handle.close()
                    self.log_handle = None

    def stop(self):
        with self.mutex:
            if self.running():
                core.atomic(self.stop_path, 'stop')
                self.job['stopping'] = True
            return dict(stopping=self.running())

    def state(self):
        with self.mutex:
            active = self.running()
            state = dict(running=active, job=dict(self.job),
                         progress=read_json(self.progress_path, {}),
                         sources=read_json(self.data/'sources-report.json', {}),
                         source_urls=read_json(self.data/'gui-sources.json', []),
                         export=read_json(self.data/'exports/status.json', {}),
                         downloads=[n for n in ('proxies.txt', 'ranked.csv', 'ranked.json') if (self.data/'exports'/n).exists()])
            if not active and self.job.get('exit_code', 0) not in (0, 130):
                state['progress']['phase'] = 'error'
            elif not active and state['progress'].get('phase') in ('starting', 'scanning', 'collecting', 'exporting'):
                state['progress']['phase'] = 'interrupted'
            try:
                with (self.data/'gui-run.log').open('rb') as handle:
                    handle.seek(0, 2)
                    handle.seek(max(0, handle.tell()-12000))
                    state['log'] = handle.read().decode('utf-8', errors='replace')
            except OSError:
                state['log'] = ''
            return state

    def results(self, query):
        profile_path = self.data/'last-profile.txt'
        if not profile_path.exists() or not (self.data/'proxies.sqlite3').exists():
            return dict(rows=[], total=0, targets=[], profile=None)
        profile = profile_path.read_text().strip()
        sort = query.get('sort', ['quality'])[0]
        try:
            threshold = float(query.get('min_success', [2/3])[0])
            offset = max(0, int(query.get('offset', [0])[0]))
            if not 0 <= threshold <= 1:
                raise ValueError()
        except ValueError:
            raise ValueError('Неверные параметры рейтинга.') from None
        order = "json_extract(payload,'$.latency_ms'), json_extract(payload,'$.reliability') DESC, proxy" if sort == 'speed' else "json_extract(payload,'$.score') DESC, json_extract(payload,'$.latency_ms'), proxy"
        # Separate read-only connection: no writing or long-lived transaction against the worker.
        db = sqlite3.connect((self.data/'proxies.sqlite3').as_uri()+'?mode=ro', uri=True, timeout=2)
        try:
            condition = "profile=? AND json_extract(payload,'$.min_target_reliability')>0 AND json_extract(payload,'$.min_target_reliability')+1e-12>=?"
            total = db.execute('SELECT count(*) FROM results WHERE '+condition, (profile, threshold)).fetchone()[0]
            rows = [json.loads(r[0]) for r in db.execute('SELECT payload FROM results WHERE '+condition+' ORDER BY '+order+' LIMIT 50 OFFSET ?', (profile, threshold, offset))]
            record = db.execute('SELECT config FROM profiles WHERE id=?', (profile,)).fetchone()
            targets = [] if not record else [dict(name=t.get('name',''), url=t['url']) for t in json.loads(record[0])['targets']]
            return dict(rows=rows, total=total, profile=profile, targets=targets, offset=offset)
        finally:
            db.close()

    def close(self):
        self.stop()
        if self.process:
            try:
                self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=10)
        self.instance_lock.close()


class Handler(BaseHTTPRequestHandler):
    server_version = 'ProxyWorkbench'

    def log_message(self, *args):
        pass

    @property
    def app(self):
        return self.server.app

    def respond(self, code, payload, mime='application/json; charset=utf-8'):
        if not isinstance(payload, bytes):
            payload = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(payload)

    def allowed(self, auth=True):
        host = self.headers.get('Host', '')
        expected = f'127.0.0.1:{self.server.server_port}'
        if host != expected:
            self.respond(403, dict(error='Неверный адрес приложения.'))
            return False
        origin = self.headers.get('Origin')
        if origin and origin != 'http://'+expected:
            self.respond(403, dict(error='Запрос с другого сайта отклонён.'))
            return False
        if auth and not secrets.compare_digest(self.headers.get('X-Workbench-Token',''), self.app.token):
            self.respond(403, dict(error='Обновите страницу приложения.'))
            return False
        return True

    def do_GET(self):
        path = urlsplit(self.path)
        if not self.allowed(auth=path.path not in ('/', '/app.js', '/style.css', '/favicon.ico')):
            return
        try:
            if path.path == '/':
                content = (ROOT/'ui/index.html').read_text().replace('__TOKEN__', self.app.token)
                return self.respond(200, content.encode(), 'text/html; charset=utf-8')
            if path.path in ('/app.js', '/style.css'):
                mime = 'text/javascript; charset=utf-8' if path.path.endswith('.js') else 'text/css; charset=utf-8'
                return self.respond(200, (ROOT/'ui'/path.path[1:]).read_bytes(), mime)
            if path.path == '/favicon.ico':
                return self.respond(204, b'')
            if path.path == '/api/settings':
                return self.respond(200, self.app.settings())
            if path.path == '/api/defaults':
                return self.respond(200, defaults())
            if path.path == '/api/state':
                return self.respond(200, self.app.state())
            if path.path == '/api/results':
                return self.respond(200, self.app.results(parse_qs(path.query)))
            if path.path.startswith('/api/download/'):
                name = path.path.rsplit('/', 1)[1]
                if name not in ('proxies.txt', 'ranked.csv', 'ranked.json'):
                    return self.respond(404, dict(error='Файл не найден.'))
                # Stream exports so a large JSON does not fill server memory.
                with (self.app.data/'exports'/name).open('rb') as handle:
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/octet-stream')
                    self.send_header('Content-Length', str(os.fstat(handle.fileno()).st_size))
                    self.send_header('Content-Disposition', f'attachment; filename="{name}"')
                    self.send_header('Cache-Control', 'no-store')
                    self.end_headers()
                    while chunk := handle.read(65536):
                        self.wfile.write(chunk)
                return
            self.respond(404, dict(error='Не найдено.'))
        except (ValueError, OSError, sqlite3.Error):
            self.respond(400, dict(error='Не удалось прочитать данные. Повторите после завершения операции.'))

    def do_POST(self):
        if not self.allowed():
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= MAX_BODY:
                return self.respond(413, dict(error='Слишком большой запрос.'))
            payload = json.loads(self.rfile.read(length))
            path = urlsplit(self.path).path
            if path == '/api/settings':
                return self.respond(200, self.app.save(payload))
            if path == '/api/start':
                return self.respond(200, self.app.start(payload))
            if path == '/api/stop':
                return self.respond(200, self.app.stop())
            self.respond(404, dict(error='Не найдено.'))
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            message = str(exc) if isinstance(exc, ValueError) and not isinstance(exc, json.JSONDecodeError) else 'Проверьте поля настроек.'
            self.respond(400, dict(error=message))
        except OSError:
            self.respond(500, dict(error='Не удалось записать настройки. Проверьте доступ к папке data.'))


def make_server(data, port=0):
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.daemon_threads = True
    try:
        server.app = App(data)
    except Exception:
        server.server_close()
        raise
    return server


def main():
    parser = argparse.ArgumentParser(description='Локальный интерфейс Proxy Workbench')
    parser.add_argument('--data', type=Path, default=ROOT/'data')
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        server = make_server(args.data, args.port)
    except OSError:
        old = read_json(args.data/'gui-address.json', {})
        if old.get('port') and type(old['port']) is int and 1 <= old['port'] <= 65535:
            url = f"http://127.0.0.1:{old['port']}/"
            try:
                import httpx
                response = httpx.get(url, timeout=2, trust_env=False)
                if response.status_code == 200 and 'Proxy Workbench' in response.text:
                    print(f'Приложение уже запущено: {url}', flush=True)
                    if not args.no_browser:
                        webbrowser.open(url)
                    return
            except httpx.HTTPError:
                pass
        raise SystemExit('Не удалось открыть интерфейс: папка data или порт уже используются.')
    core.atomic(args.data/'gui-address.json', json.dumps(dict(port=server.server_port)))
    url = f'http://127.0.0.1:{server.server_port}/'
    print(f'Proxy Workbench: {url}\nНе закрывайте это окно, пока работает приложение. Ctrl+C — закрыть.', flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.app.close()
        server.server_close()


if __name__ == '__main__':
    main()
