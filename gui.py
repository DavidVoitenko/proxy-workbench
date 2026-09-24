#!/usr/bin/env python3
"""Loopback-only browser interface; no external server or frontend dependencies."""
from __future__ import annotations

import argparse
import copy
from contextlib import contextmanager
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

from branding import PRODUCT_ID, PRODUCT_NAME, PRODUCT_VERSION, REQUEST_PROFILES
import proxytool as core
from maintenance import clear_runtime, exclusive_lock
from reputation import Denylist, normalize_zones, result_allowed
import anonymity
import geoip

ROOT = Path(__file__).resolve().parent
MAX_BODY = 32 * 1024 * 1024


def read_json(path, fallback):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return fallback


def public_source(value):
    if not isinstance(value, str):
        return ''
    parts = value.strip().split(None, 1)
    if len(parts) == 2 and parts[0] in ('http', 'https', 'socks5', 'socks5h', 'geonode', 'http-fields'):
        return parts[0] + ' ' + core.public_url(parts[1])
    return core.public_url(value)


RESULT_ORDERS = {
    'quality': "json_extract(payload,'$.score') DESC, json_extract(payload,'$.latency_ms'), proxy",
    'speed': "json_extract(payload,'$.latency_ms'), json_extract(payload,'$.reliability') DESC, proxy",
    'stability': "json_extract(payload,'$.jitter_ms'), json_extract(payload,'$.latency_ms'), proxy",
}
DOWNLOADS = ('proxies.txt', 'ranked.csv', 'ranked.json', *core.PROTOCOL_EXPORTS.values())


def public_sources(values):
    return [public_source(value) for value in values] if isinstance(values, list) else []


def defaults():
    return dict(settings_version=2, targets=[dict(name='example.com', url='https://example.com/', statuses=[200],
                             contains='Example Domain', headers={}, method='GET')],
                sources=json.loads((ROOT/'sources.json').read_text(encoding='utf-8')), use_sources=True,
                proxies='', attempts=3, timeout=8, workers=128, rate=100,
                max_bytes=1048576, source_timeout=60, min_success=2/3, top=0, sort='quality',
                request_profile='workbench', denylist='',
                reputation=dict(local_enabled=True, dnsbl_enabled=False, dnsbl_zones=[],
                                timeout=2.5, strict=False),
                anonymity=dict(judge_url=''), min_anonymity='any',
                connect_timeout=4, fail_fast=True, protocol='all', max_latency=0, countries='', want=0)


def validate(settings):
    if not isinstance(settings, dict):
        raise ValueError('Ожидаются настройки проверки.')
    clean = defaults()
    clean.update({k: settings[k] for k in clean if k in settings})
    if clean['settings_version'] not in (1, 2):
        raise ValueError('Неизвестная версия настроек.')
    clean['settings_version'] = 2
    if not isinstance(clean['request_profile'], str) or clean['request_profile'] not in REQUEST_PROFILES:
        raise ValueError('Неизвестный request-профиль.')
    if not isinstance(clean['denylist'], str) or len(clean['denylist']) > 2_000_000:
        raise ValueError('Список denylist слишком большой: максимум 2 МБ.')
    incoming_rep = settings.get('reputation', {})
    if not isinstance(incoming_rep, dict):
        raise ValueError('Настройки чистоты должны быть объектом.')
    reputation = defaults()['reputation']
    reputation.update({k: incoming_rep[k] for k in reputation if k in incoming_rep})
    for key in ('local_enabled', 'dnsbl_enabled', 'strict'):
        if type(reputation[key]) is not bool:
            raise ValueError('Настройки чистоты должны быть логическими.')
    if not isinstance(reputation['dnsbl_zones'], list):
        raise ValueError('DNSBL-зоны должны быть списком.')
    reputation['dnsbl_zones'] = normalize_zones(reputation['dnsbl_zones'])
    if reputation['dnsbl_enabled'] and not reputation['dnsbl_zones']:
        reputation['dnsbl_enabled'] = False
    try:
        reputation['timeout'] = float(reputation['timeout'])
    except (TypeError, ValueError):
        raise ValueError('Таймаут DNSBL должен быть числом.') from None
    if not math.isfinite(reputation['timeout']) or not .1 <= reputation['timeout'] <= 30:
        raise ValueError('Таймаут DNSBL должен быть от 0.1 до 30 секунд.')
    clean['reputation'] = reputation
    for key, low, high, integer in [('attempts', 1, 100, True), ('timeout', .1, 300, False),
            ('workers', 1, 2048, True), ('rate', 0, 10000, False), ('max_bytes', 1, 100_000_000, True),
            ('source_timeout', 1, 3600, False), ('top', 0, 1_000_000_000, True), ('min_success', 0, 1, False),
            ('connect_timeout', .1, 300, False), ('max_latency', 0, 600_000, False), ('want', 0, 1_000_000_000, True)]:
        value = clean[key]
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high or (integer and int(value) != value):
            raise ValueError(f'Недопустимое значение: {key}.')
        clean[key] = int(value) if integer else value
    if (clean['sort'] not in core.SORTS or clean['protocol'] not in core.PROTOCOLS
            or type(clean['use_sources']) is not bool or type(clean['fail_fast']) is not bool):
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
                              timeout=clean['timeout'], max_bytes=clean['max_bytes'],
                              request_profile=clean['request_profile'])
    normalized = core.validate_targets(copy.deepcopy(targets), args, request_profile=clean['request_profile'])
    for t in normalized['targets']:
        name = t.get('name', '')
        if not isinstance(name, str) or len(name) > 160:
            raise ValueError('Название сервиса: максимум 160 символов.')
    clean['targets'] = normalized['targets']
    judge = clean['anonymity']
    if not isinstance(judge, dict):
        raise ValueError('Настройки анонимности должны быть объектом.')
    judge_url = judge.get('judge_url') or ''
    if not isinstance(judge_url, str):
        raise ValueError('anonymity.judge_url: ожидается http(s) URL')
    judge_url = judge_url.strip()
    anonymity.validate_judge({'judge_url': judge_url})
    clean['anonymity'] = dict(judge_url=judge_url)
    anonymity.validate_min_level(clean['min_anonymity'])
    if not isinstance(clean['countries'], str) or len(clean['countries']) > 1000:
        raise ValueError('Страны: используйте двухбуквенные ISO-коды, например DE,NL.')
    clean['countries'] = ','.join(geoip.parse_countries(clean['countries']))
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
        self.geo_cache = (None, None)
        self.mutex = threading.RLock()
        self.process = None
        self.log_handle = None
        self.job = read_json(self.data/'gui-job.json', {})
        self.stop_path = self.data/'gui-stop'
        self.progress_path = self.data/'gui-progress.json'

    def settings(self):
        settings_path = self.data/'gui-settings.json'
        try:
            stored = json.loads(settings_path.read_text(encoding='utf-8')) if settings_path.exists() else None
        except FileNotFoundError:
            stored = None
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise ValueError('Файл gui-settings.json повреждён или недоступен; исправьте его перед продолжением.') from None
        if stored is None:
            stored = defaults()
            try:
                stored['denylist'] = (self.data/'denylist.txt').read_text(encoding='utf-8')
            except FileNotFoundError:
                pass
            except (OSError, UnicodeError):
                raise ValueError('Не удалось прочитать data/denylist.txt. Исправьте файл перед сохранением.') from None
        elif not isinstance(stored, dict):
            raise ValueError('Файл gui-settings.json должен содержать объект настроек.')
        elif 'denylist' not in stored:
            try:
                stored['denylist'] = (self.data/'denylist.txt').read_text(encoding='utf-8')
            except FileNotFoundError:
                stored['denylist'] = ''
            except (OSError, UnicodeError):
                raise ValueError('Не удалось прочитать data/denylist.txt. Исправьте файл перед сохранением.') from None
        return validate(stored)

    def save(self, payload):
        if isinstance(payload, dict) and 'denylist' not in payload:
            payload = dict(payload)
            payload['denylist'] = self.settings().get('denylist', '')
        settings = validate(payload)
        with self.mutex:
            core.atomic(self.data/'gui-settings.json', json.dumps(settings, ensure_ascii=False, indent=2))
            core.atomic(self.data/'denylist.txt', settings['denylist'])
        return settings

    def running(self):
        return self.process is not None and (self.process.poll() is None or self.log_handle is not None)

    @contextmanager
    def data_lock(self):
        with self.mutex:
            with exclusive_lock(self.data/'workbench.lock'):
                yield

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
            core.atomic(self.data/'gui-targets.json', json.dumps({
                'targets': settings['targets'], 'request_profile': settings['request_profile'],
                'reputation': settings['reputation'], 'anonymity': settings['anonymity']}, ensure_ascii=False))
            core.atomic(self.data/'gui-sources.json', json.dumps(settings['sources']))
            core.atomic(self.data/'gui-input.txt', settings['proxies'])
            self.stop_path.unlink(missing_ok=True)
            core.atomic(self.progress_path, json.dumps(dict(phase='starting', checked=0, candidates=0)))
            command = [sys.executable, '-u', str(ROOT/'proxytool.py'), 'scan' if action == 'recheck' else action,
                       '--data', str(self.data), '--config', str(self.data/'gui-targets.json'),
                       '--sources', str(self.data/'gui-sources.json'), '--input', str(self.data/'gui-input.txt'),
                       '--denylist-file', str(self.data/'denylist.txt'),
                       '--progress-file', str(self.progress_path), '--stop-file', str(self.stop_path)]
            for key in ('attempts', 'timeout', 'connect_timeout', 'workers', 'rate', 'max_bytes', 'source_timeout',
                        'min_success', 'top', 'sort', 'min_anonymity', 'protocol', 'max_latency', 'want'):
                command.extend(['--'+key.replace('_', '-'), str(settings[key])])
            command.append('--fail-fast' if settings['fail_fast'] else '--no-fail-fast')
            command.extend(['--country', settings['countries']])
            reputation = settings['reputation']
            command.append('--local-denylist' if reputation['local_enabled'] else '--no-local-denylist')
            if reputation['dnsbl_enabled']:
                command.append('--dnsbl')
            for zone in reputation['dnsbl_zones']:
                command.extend(['--dnsbl-zone', zone])
            command.extend(['--reputation-timeout', str(reputation['timeout'])])
            if reputation['strict']:
                command.append('--strict-clean')
            if not settings['use_sources']:
                command.append('--no-sources')
            if action == 'recheck':
                command.append('--recheck')
            self.job = dict(id=secrets.token_hex(8), action=action, started_at=time.time(),
                            targets=[dict(name=t.get('name', ''), url=core.public_url(t['url'])) for t in settings['targets']],
                            min_success=settings['min_success'], sort=settings['sort'], top=settings['top'],
                            request_profile=settings['request_profile'], reputation=reputation,
                            anonymity=bool(settings['anonymity']['judge_url']), min_anonymity=settings['min_anonymity'])
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

    def geo(self):
        """The offline country database, reloaded when the file changes."""
        path = geoip.default_path(self.data)
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            return None
        if self.geo_cache[0] != stamp:
            try:
                self.geo_cache = (stamp, geoip.CountryDB.from_file(path))
            except (OSError, EOFError, UnicodeError, ValueError):
                self.geo_cache = (stamp, None)
        return self.geo_cache[1]

    def geo_status(self):
        database = self.geo()
        return dict(available=database is not None, ranges=database.size if database else 0,
                    attribution=geoip.ATTRIBUTION)

    def update_geo(self):
        with self.mutex:
            if self.running():
                raise ValueError('Сначала остановите текущую операцию.')
        # Not under the mutex: the download can take a while and the UI keeps
        # polling. The CLI takes the data-folder lock, so a scan cannot overlap.
        try:
            done = subprocess.run([sys.executable, str(ROOT/'proxytool.py'), 'update-geoip', '--data', str(self.data)],
                                  capture_output=True, text=True, timeout=600, cwd=ROOT, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError('Не удалось скачать базу стран.') from None
        if done.returncode:
            lines = (done.stderr or done.stdout).strip().splitlines()
            raise ValueError(lines[-1] if lines else 'Не удалось скачать базу стран.')
        return self.geo_status()

    def clear_data(self):
        with self.mutex:
            if self.running():
                raise ValueError('Сначала остановите текущую операцию.')
            try:
                with self.data_lock():
                    removed = clear_runtime(self.data, keep_lock=True)
            except RuntimeError as exc:
                raise ValueError(str(exc)) from None
            return dict(removed=removed)

    def state(self):
        with self.mutex:
            active = self.running()
            state = dict(running=active, job=dict(self.job),
                         progress=read_json(self.progress_path, {}),
                         sources=read_json(self.data/'sources-report.json', {}),
                         source_urls=public_sources(read_json(self.data/'gui-sources.json', [])),
                         export=read_json(self.data/'exports/status.json', {}),
                         downloads=[n for n in DOWNLOADS
                                     if core.export_file(self.data/'exports', n).is_file()])
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
        profile = profile_path.read_text(encoding='utf-8').strip()
        order = RESULT_ORDERS.get(query.get('sort', ['quality'])[0])
        try:
            threshold = float(query.get('min_success', [2/3])[0])
            offset = max(0, int(query.get('offset', ['0'])[0]))
            min_anonymity = anonymity.validate_min_level(query.get('min_anonymity', ['any'])[0])
            protocol = query.get('protocol', ['all'])[0]
            max_latency = float(query.get('max_latency', ['0'])[0])
            search = query.get('q', [''])[0].strip().lower()[:100]
            countries = frozenset(geoip.parse_countries(query.get('country', [''])[0][:1000]))
            if (not 0 <= threshold <= 1 or order is None or protocol not in core.PROTOCOLS
                    or not math.isfinite(max_latency) or max_latency < 0):
                raise ValueError()
        except ValueError:
            raise ValueError('Неверные параметры рейтинга.') from None
        try:
            with self.data_lock():
                db = sqlite3.connect((self.data/'proxies.sqlite3').as_uri()+'?mode=ro', uri=True, timeout=2)
                try:
                    record = db.execute('SELECT config FROM profiles WHERE id=?', (profile,)).fetchone()
                    cfg = json.loads(record[0]) if record else {}
                    policy = cfg.get('reputation', {})
                    strict = bool(policy.get('strict', False))
                    if not cfg.get('anonymity'):
                        min_anonymity = 'any'
                    denylist = Denylist.from_file(self.data/'denylist.txt', normalizer=core.normalize)
                    try:
                        country_of = core.country_resolver(db, self.geo())
                    except sqlite3.Error:
                        # Databases created before 1.5 have no candidate_meta table yet.
                        geo = self.geo()
                        country_of = geo.country_of if geo else None
                    current_settings = self.settings()
                    local_enabled = current_settings.get('reputation', {}).get('local_enabled', True)
                    active_denylist = denylist if local_enabled else None
                    if active_denylist is not None and active_denylist.error:
                        raise ValueError('Не удалось прочитать локальный denylist; обновите список.')
                    condition = "profile=? AND json_extract(payload,'$.min_target_reliability')>0 AND json_extract(payload,'$.min_target_reliability')+1e-12>=?"
                    total = 0
                    rows = []
                    for (payload,) in db.execute('SELECT payload FROM results WHERE '+condition+' ORDER BY '+order, (profile, threshold)):
                        row = json.loads(payload)
                        if not result_allowed(row, threshold, denylist=active_denylist, strict=strict,
                                              min_anonymity=min_anonymity):
                            continue
                        if not core.matches_selection(row, protocol, max_latency or None, countries, country_of):
                            continue
                        if search and search not in row.get('proxy', '').lower():
                            continue
                        if total >= offset and len(rows) < 50:
                            summary = dict(row)
                            summary.pop('samples', None)
                            summary['country'] = core.row_country(row, country_of)
                            rows.append(summary)
                        total += 1
                    targets = [dict(name=t.get('name',''), url=core.public_url(t['url'])) for t in cfg.get('targets', [])]
                    return dict(rows=rows, total=total, profile=profile, targets=targets, offset=offset,
                                request_profile=cfg.get('request_profile', 'workbench'),
                                reputation_policy=policy, anonymity=bool(cfg.get('anonymity')))
                finally:
                    db.close()
        except RuntimeError as exc:
            raise ValueError(str(exc)) from None

    def detail(self, proxy):
        if not isinstance(proxy, str) or not proxy or len(proxy) > 512:
            raise ValueError('Некорректный адрес прокси.')
        profile_path = self.data/'last-profile.txt'
        if not profile_path.exists() or not (self.data/'proxies.sqlite3').exists():
            raise ValueError('Результаты не найдены.')
        profile = profile_path.read_text(encoding='utf-8').strip()
        try:
            with self.data_lock():
                db = sqlite3.connect((self.data/'proxies.sqlite3').as_uri()+'?mode=ro', uri=True, timeout=2)
                try:
                    record = db.execute('SELECT payload FROM results WHERE profile=? AND proxy=?', (profile, proxy)).fetchone()
                finally:
                    db.close()
        except RuntimeError as exc:
            raise ValueError(str(exc)) from None
        if not record:
            raise ValueError('Детали прокси не найдены.')
        return json.loads(record[0])

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
    server_version = f'{PRODUCT_ID}/{PRODUCT_VERSION}'

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
                content = (ROOT/'ui/index.html').read_text(encoding='utf-8').replace('__TOKEN__', self.app.token)
                content = content.replace('__PRODUCT_VERSION__', PRODUCT_VERSION)
                return self.respond(200, content.encode(), 'text/html; charset=utf-8')
            if path.path in ('/app.js', '/style.css'):
                mime = 'text/javascript; charset=utf-8' if path.path.endswith('.js') else 'text/css; charset=utf-8'
                if path.path == '/app.js':
                    content = (ROOT/'ui'/'app.js').read_text(encoding='utf-8').replace('__PRODUCT_VERSION__', PRODUCT_VERSION)
                    return self.respond(200, content.encode(), mime)
                return self.respond(200, (ROOT/'ui'/'style.css').read_bytes(), mime)
            if path.path == '/favicon.ico':
                return self.respond(204, b'')
            if path.path == '/api/settings':
                return self.respond(200, self.app.settings())
            if path.path == '/api/geoip':
                return self.respond(200, self.app.geo_status())
            if path.path == '/api/defaults':
                return self.respond(200, defaults())
            if path.path == '/api/state':
                return self.respond(200, self.app.state())
            if path.path == '/api/results':
                return self.respond(200, self.app.results(parse_qs(path.query)))
            if path.path == '/api/result-detail':
                proxy = parse_qs(path.query).get('proxy', [''])[0]
                return self.respond(200, self.app.detail(proxy))
            if path.path.startswith('/api/download/'):
                name = path.path.rsplit('/', 1)[1]
                if name not in DOWNLOADS:
                    return self.respond(404, dict(error='Файл не найден.'))
                # Stream exports so a large JSON does not fill server memory.
                try:
                    with self.app.data_lock():
                        with core.export_file(self.app.data/'exports', name).open('rb') as handle:
                            self.send_response(200)
                            self.send_header('Content-Type', 'application/octet-stream')
                            self.send_header('Content-Length', str(os.fstat(handle.fileno()).st_size))
                            self.send_header('Content-Disposition', f'attachment; filename="{name}"')
                            self.send_header('Cache-Control', 'no-store')
                            self.end_headers()
                            while chunk := handle.read(65536):
                                self.wfile.write(chunk)
                except RuntimeError as exc:
                    return self.respond(409, dict(error=str(exc)))
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
            if path == '/api/geoip/update':
                return self.respond(200, self.app.update_geo())
            if path == '/api/clear-data':
                return self.respond(200, self.app.clear_data())
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
    parser = argparse.ArgumentParser(description=f'Локальный интерфейс {PRODUCT_NAME}')
    parser.add_argument('--version', action='version', version=f'{PRODUCT_NAME} {PRODUCT_VERSION}')
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
                if response.status_code == 200 and PRODUCT_NAME in response.text:
                    print(f'Приложение уже запущено: {url}', flush=True)
                    if not args.no_browser:
                        webbrowser.open(url)
                    return
            except httpx.HTTPError:
                pass
        raise SystemExit('Не удалось открыть интерфейс: папка data или порт уже используются.')
    core.atomic(args.data/'gui-address.json', json.dumps(dict(port=server.server_port)))
    url = f'http://127.0.0.1:{server.server_port}/'
    print(f'{PRODUCT_NAME} {PRODUCT_VERSION}: {url}\nНе закрывайте это окно, пока работает приложение. Ctrl+C — закрыть.', flush=True)
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
