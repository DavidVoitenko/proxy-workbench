#!/usr/bin/env python3
"""Loopback-only browser interface; no external server or frontend dependencies."""
from __future__ import annotations

import argparse
import re
import copy
import asyncio
import hashlib
import json
import math
import os
import threading
import time
from contextlib import contextmanager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import sqlite3
import subprocess
import sys
from urllib.parse import parse_qs, quote, urlsplit
import webbrowser

import httpx

from .branding import PRODUCT_ID, PRODUCT_NAME, PRODUCT_VERSION, REQUEST_PROFILES, SOURCES_URL
from . import proxytool as core
from .maintenance import clear_runtime, exclusive_lock
from .reputation import Denylist, normalize_zones
from . import anonymity
from . import api
from . import core as admission
from . import gateway
from .i18n import tr, utf8_output, state_detail_text
from . import paths
from . import geoip
from . import source_catalog
from . import source_management

def _source_id_flag(payload, flag):
    payload = payload or {}
    source_id = payload.get('id')
    if not isinstance(source_id, str) or not source_id:
        raise ValueError('Укажите источник.')
    return source_id, bool(payload.get(flag))


def _source_id(payload):
    payload = payload or {}
    source_id = payload.get('id')
    if not isinstance(source_id, str) or not source_id:
        raise ValueError('Укажите источник.')
    return source_id


def _known_source(catalog, settings, source_id):
    if source_catalog.source_by_id(catalog, source_id) is not None:
        return True
    selection = settings.get('source_selection') or {}
    return any(item.get('id') == source_id for item in selection.get('custom_sources', []) if isinstance(item, dict))

ROOT = paths.PACKAGE
MAX_BODY = 32 * 1024 * 1024
MAX_SELECTION = 1000
# A bulk action over "all matching" runs on the server: the browser sends the
# filter, not the rows, so a 100k-row scope never has to reach the page.
MAX_BULK_SELECTION = 200_000
MAX_BULK_COPY = 50_000
# The result table loads one page at a time.  50 keeps the existing density of
# the user's table; the ceiling exists so a request cannot ask for everything.
PAGE_SIZE = 50
PAGE_SIZE_MAX = 200
# A quick row test is diagnostic.  It gets its own whole-call deadline so one
# click cannot run for minutes (defect 23).
QUICK_TEST_DEADLINE_S = 45.0
QUICK_TEST_TIMEOUT_CAP_S = 10.0
QUICK_TEST_MAX_BYTES = 1024 * 1024
# Sidecar files of the interface: user-authored, never touched by the worker.
ANNOTATIONS_FILE = 'gui-annotations.json'
VIEWS_FILE = 'gui-views.json'
HISTORY_FILE = 'gui-history.json'
HISTORY_LIMIT = 50
EVENTS_FILE = 'gui-events.jsonl'
EVENT_RETENTION = 2000
# Bulk actions that change stored state can be undone; a scan cannot.
RECOVERABLE_OPS = frozenset({'tag', 'untag', 'note', 'favorite', 'unfavorite', 'exclude', 'include', 'denylist'})
BULK_OPS = frozenset({'recheck', 'export', 'copy', 'tag', 'untag', 'note', 'favorite',
                      'unfavorite', 'exclude', 'include', 'denylist'})
BULK_SCOPES = frozenset({'page', 'selected', 'all_matching'})
RESULT_VIEWS = ('fresh', 'stale', 'failed', 'unknown', 'all')
# Freshness buckets.  ``rejected`` exists only inside the "all" view: a row
# that is neither fresh, expired, failed nor time-unknown was simply filtered
# out by the current policy, and saying "failed" about it would be a lie.
FRESHNESS_FRESH = 'fresh'
FRESHNESS_STALE = 'stale'
FRESHNESS_FAILED = 'failed'
FRESHNESS_UNKNOWN = 'unknown'
FRESHNESS_REJECTED = 'rejected'
# Compact is the default column set of the user's table (defect: F19).
COMPACT_COLUMNS = ('check', 'num', 'proxy', 'score', 'latency', 'success', 'cleanliness', 'country', 'actions')
ALL_COLUMNS = ('check', 'num', 'proxy', 'score', 'latency', 'jitter', 'speed', 'success', 'uptime',
               'cleanliness', 'anonymity', 'country', 'provider', 'age', 'actions')
DOWNLOAD_CHUNK = 65536


def read_json(path, fallback):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return fallback


def public_source(value, *, keyed=True):
    if not isinstance(value, str):
        return ''
    parts = value.strip().split(None, 1)
    if len(parts) == 2 and parts[0] in core.SOURCE_KINDS:
        label = parts[0] + ' ' + core.public_url(parts[1])
    else:
        label = core.public_url(value)
    # Paths and query strings are intentionally hidden, but a host-only label
    # is ambiguous for several GitHub/raw mirrors.  A short stable digest lets
    # users identify the exact configured entry without exposing credentials.
    return label + ' [' + core.source_key(value)[:8] + ']' if keyed else label


RESULT_ORDERS = {
    # Re-sorted in Python by the recommended score; SQL only gives a stable start.
    'recommended': "json_extract(payload,'$.score') DESC, proxy",
    'quality': "json_extract(payload,'$.score') DESC, json_extract(payload,'$.latency_ms'), proxy",
    'speed': "json_extract(payload,'$.latency_ms'), json_extract(payload,'$.reliability') DESC, proxy",
    'stability': "json_extract(payload,'$.jitter_ms'), json_extract(payload,'$.latency_ms'), proxy",
    'uptime': "COALESCE(1.0*json_extract(payload,'$.history.passes')/json_extract(payload,'$.history.checks'), 1) DESC, "
              "COALESCE(json_extract(payload,'$.history.checks'), 1) DESC, json_extract(payload,'$.score') DESC, proxy",
    'bandwidth': "json_extract(payload,'$.speed.mbps') IS NULL, json_extract(payload,'$.speed.mbps') DESC, "
                 "json_extract(payload,'$.score') DESC, proxy",
}
# A source needs this many checked proxies before "no working ones" is trusted.
PRUNE_MIN_CHECKED = 20
# The UI translates the scanner's log itself, so the scanner always writes Russian here.
CHILD_ENV = dict(os.environ, PROXY_WORKBENCH_LANG='ru', PYTHONUNBUFFERED='1', PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
DOWNLOADS = ('proxies.txt', 'ranked.csv', 'ranked.json', *core.PROTOCOL_EXPORTS.values(), 'hostport.txt', 'proxychains.txt',
             'proxy.pac', 'clash.yaml', 'singbox.json')
# An empty/stale generation may still be inspected as plain text, but a
# ready-made client configuration must not be presented as usable.
EMPTY_SAFE_DOWNLOADS = {'proxies.txt', 'ranked.csv', 'ranked.json', *core.PROTOCOL_EXPORTS.values(),
                        'hostport.txt', 'proxychains.txt'}


def public_sources(values, *, keyed=True):
    return [public_source(value, keyed=keyed) for value in values] if isinstance(values, list) else []


class Sidecar:
    """One small JSON document the interface owns: tags, views, undo history.

    These are user documents, not worker state, so they never join the runtime
    files of the scanner and are never rewritten by a check.
    """

    def __init__(self, path, fallback=None):
        self.path = Path(path)
        self.fallback = {} if fallback is None else fallback
        self.lock = threading.RLock()

    def read(self):
        with self.lock:
            return read_json(self.path, copy.deepcopy(self.fallback))

    def write(self, payload):
        with self.lock:
            core.atomic(self.path, json.dumps(payload, ensure_ascii=False))
        return payload


def digest_of(*parts):
    """Stable identity of a result scope, used to refuse a stale selection."""
    raw = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:20]


def freshness_of(verdict, row):
    """One of the four user-facing freshness buckets for one row."""
    reason = verdict.reason_code
    if verdict.time_state in (admission.TIME_UNKNOWN, admission.CLOCK_ROLLBACK) or (
            verdict.observation_state == admission.OBSERVATION_MISSING):
        return FRESHNESS_UNKNOWN
    if verdict.observation_state == admission.OBSERVATION_FAILED:
        return FRESHNESS_FAILED
    if reason in ('E_TIME_TTL_EXPIRED', 'E_TIME_TTL_MISSING', 'E_TIME_FUTURE') or (
            verdict.time_state == admission.TIME_EXPIRED):
        return FRESHNESS_STALE
    if verdict.admitted:
        return FRESHNESS_FRESH
    if row.get('min_target_reliability') is None and row.get('error'):
        return FRESHNESS_FAILED
    return FRESHNESS_REJECTED


def normalize_hosting_filter(value):
    """Map the two UI labels to the backend's canonical keep/hide values."""
    if value in ('', 'any', 'keep'):
        return ''
    if value in ('hide', 'exclude'):
        return 'hide'
    raise ValueError('Неверный фильтр провайдера для экспорта.')


SNAPSHOT_OK = 'ok'
SNAPSHOT_LEGACY = 'legacy'
SNAPSHOT_BROKEN = 'broken'
SNAPSHOT_MISSING = 'missing'


class Snapshot:
    """One pinned published generation, read without any writer lock.

    Defect 5: the table, the details and the download must answer while a scan
    or a watch hold ``workbench.lock``.  A generation directory is immutable and
    is published by an atomic rename, so a read-only path can simply resolve the
    pointer once and open the files.  Defect 3: the snapshot keeps per-row
    freshness, so one expired member never empties the set.
    """

    def __init__(self, state, *, generation=None, rows=(), status=None, directory=None, detail=None):
        self.state = state
        self.generation = generation
        self.rows = list(rows)
        self.status = dict(status or {})
        self.directory = Path(directory) if directory else None
        self.detail = detail

    @property
    def usable(self):
        """A snapshot the interface may describe as "the current export"."""
        return self.state in (SNAPSHOT_OK, SNAPSHOT_LEGACY) and bool(self.status)


def read_snapshot(data, *, now=None):
    """Read the current generation once; never take the exclusive data lock."""
    exports = Path(data)/'exports'
    generation = core.current_generation_name(exports)
    if generation:
        rows_path = core.export_file(exports, 'ranked.json', generation=generation)
        status_path = core.export_file(exports, 'status.json', generation=generation)
        try:
            rows = json.loads(rows_path.read_text(encoding='utf-8'))
            status = json.loads(status_path.read_text(encoding='utf-8')) if status_path.is_file() else {}
        except (OSError, UnicodeError, ValueError):
            # A published generation never changes; a generation that cannot be
            # read is a broken publication, not a licence to serve database
            # rows the reader itself would reject (defect 9, R05).
            return Snapshot(SNAPSHOT_BROKEN, generation=generation, directory=exports,
                            detail='generation_unreadable')
        if not isinstance(rows, list) or not isinstance(status, dict):
            return Snapshot(SNAPSHOT_BROKEN, generation=generation, directory=exports,
                            detail='generation_malformed')
        if status.get('generation') not in (None, generation):
            return Snapshot(SNAPSHOT_BROKEN, generation=generation, directory=exports,
                            detail='generation_mismatch')
        return Snapshot(SNAPSHOT_OK, generation=generation, rows=rows, status=status, directory=exports)
    if not (exports/'generations').is_dir() and (exports/'ranked.json').is_file():
        # Pre-generation installation: the mutable root files are the snapshot.
        try:
            rows = json.loads((exports/'ranked.json').read_text(encoding='utf-8'))
            status = read_json(exports/'status.json', {})
        except (OSError, UnicodeError, ValueError):
            return Snapshot(SNAPSHOT_BROKEN, directory=exports, detail='legacy_unreadable')
        if not isinstance(rows, list):
            return Snapshot(SNAPSHOT_BROKEN, directory=exports, detail='legacy_malformed')
        return Snapshot(SNAPSHOT_LEGACY, rows=rows, status=status, directory=exports)
    if (exports/'generations').is_dir():
        # Generations exist but the pointer does not resolve: the publication is
        # broken, and falling back to arbitrary database rows is forbidden.
        return Snapshot(SNAPSHOT_BROKEN, directory=exports, detail='pointer_unreadable')
    return Snapshot(SNAPSHOT_MISSING, directory=exports)


def defaults():
    return dict(settings_version=2, targets=[dict(name='example.com', url='https://example.com/', statuses=[200],
                             contains='Example Domain', headers={}, method='GET')],
                sources=json.loads((ROOT/'sources.json').read_text(encoding='utf-8')), use_sources=True,
                proxies='', attempts=3, timeout=8, workers=128, rate=100,
                max_bytes=1048576, source_timeout=60, min_success=2/3, top=0, sort='recommended',
                request_profile='workbench', denylist='',
                reputation=dict(local_enabled=True, dnsbl_enabled=False, dnsbl_zones=[],
                                timeout=2.5, strict=False),
                anonymity=dict(judge_url=''), min_anonymity='any',
                connect_timeout=4, fail_fast=True, protocol='all', max_latency=0, countries='', want=0,
                detect_protocols=False, watch=0, prefilter=512, exclude_hosting=False, speedtest=dict(url='', max_bytes=core.SPEEDTEST_BYTES))


def validate(settings):
    if not isinstance(settings, dict):
        raise ValueError('Ожидаются настройки проверки.')
    clean = defaults()
    clean.update({k: settings[k] for k in clean if k in settings})
    if clean['settings_version'] not in (1, 2, 3):
        raise ValueError('Неизвестная версия настроек.')
    clean['settings_version'] = 2 if clean['settings_version'] == 1 else clean['settings_version']
    # Version 3 carries the source-catalog selection made in the Sources tab;
    # it is produced by source_catalog.migrate_settings and must survive a
    # round-trip through validate, or every catalog write would silently lose
    # the user's selection.
    selection = settings.get('source_selection')
    if selection is not None:
        if not isinstance(selection, dict):
            raise ValueError('source_selection: повреждённые поля.')
        clean['source_selection'] = selection
    if not isinstance(clean['request_profile'], str) or clean['request_profile'] not in REQUEST_PROFILES:
        raise ValueError('Неизвестный request-профиль.')
    if not isinstance(clean['denylist'], str) or len(clean['denylist']) > 2_000_000:
        raise ValueError('Список denylist слишком большой: максимум 2 МБ.')
    for line in clean['denylist'].splitlines():
        value = line.strip()
        if value and not value.startswith('#') and ('@' in value or
                ('://' in value and core.normalize_custom(value) is None)):
            raise ValueError('Denylist не должен содержать логин или пароль.')
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
            ('connect_timeout', .1, 300, False), ('max_latency', 0, 600_000, False), ('want', 0, 1_000_000_000, True),
            ('watch', 0, 1440, False), ('prefilter', 0, 5000, True)]:
        value = clean[key]
        if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high or (integer and int(value) != value):
            raise ValueError(f'Недопустимое значение: {key}.')
        clean[key] = int(value) if integer else value
    if (clean['sort'] not in core.SORTS or clean['protocol'] not in core.PROTOCOLS
            or type(clean['use_sources']) is not bool or type(clean['fail_fast']) is not bool
            or type(clean['detect_protocols']) is not bool or type(clean['exclude_hosting']) is not bool):
        raise ValueError('Неверный режим сортировки или источников.')
    if not isinstance(clean['proxies'], str) or len(clean['proxies']) > 20_000_000:
        raise ValueError('Список прокси слишком большой: максимум 20 МБ.')
    # Normalize the user import before it can be persisted or exported.  This
    # rejects URL credentials while retaining private/hostname entries for
    # explicitly local user setups; the public collector still applies its
    # separate global-IP policy during collection.
    clean['proxies'] = core.normalize_custom_list(clean['proxies'])
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
    speedtest = clean['speedtest']
    if not isinstance(speedtest, dict) or not isinstance(speedtest.get('url', ''), str):
        raise ValueError('speedtest.url: ожидается http(s) URL')
    speedtest = dict(url=speedtest.get('url', '').strip(), max_bytes=speedtest.get('max_bytes', core.SPEEDTEST_BYTES))
    core.validate_speedtest(speedtest if speedtest['url'] else None)
    if type(speedtest['max_bytes']) is not int:
        raise ValueError('speedtest.max_bytes: от 10000 до 200000000')
    clean['speedtest'] = speedtest
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
        # The gateway password is a separate secret: the GUI session token must
        # never travel into a QR code or a phone profile (defect 18, R12).
        self.gateway_token = secrets.token_urlsafe(24)
        self.geo_cache = (None, None)
        self.asn_cache = (None, None)
        self.mutex = threading.RLock()
        self.process = None
        self.log_handle = None
        self.job = read_json(self.data/'gui-job.json', {})
        self.export_reader = api.Exports(self.data/'exports')
        self.stop_path = self.data/'gui-stop'
        self.progress_path = self.data/'gui-progress.json'
        self.annotations = Sidecar(self.data/ANNOTATIONS_FILE, {'entries': {}})
        self.views = Sidecar(self.data/VIEWS_FILE, {'views': []})
        self.history = Sidecar(self.data/HISTORY_FILE, {'entries': []})
        self.events_path = self.data/EVENTS_FILE
        self.event_seq = {}
        self.event_floor = 0
        self.event_seen = set()
        self.event_mark = 0.0
        self.events_loaded = False
        self.event_backfilled = False
        self._status_cache = (None, None, None)
        self._catalog_cache = None
        self.catalog_job = {'running': False, 'stage': 'idle', 'added': 0, 'changed': 0, 'retired': 0}

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

    # --- Source catalog: shared between UI, CLI and API ---

    CATALOG_FILE = 'source-catalog.json'

    def catalog(self):
        """Last accepted remote catalog, or the bundled one when there is none."""
        if self._catalog_cache is not None:
            return self._catalog_cache
        path = self.data/self.CATALOG_FILE
        if path.is_file():
            try:
                self._catalog_cache = source_catalog.load_catalog(path, allow_research=False, allow_unsafe=True)
                return self._catalog_cache
            except (OSError, ValueError, source_catalog.CatalogError):
                pass
        self._catalog_cache = source_catalog.load_bundled()
        return self._catalog_cache

    def read_db(self):
        """Short read-only connection; never takes the workbench lock."""
        path = self.data/'proxies.sqlite3'
        if not path.is_file():
            return None
        try:
            return sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=2)
        except sqlite3.Error:
            return None

    @contextmanager
    def source_db(self):
        db = self.read_db()
        try:
            yield db
        finally:
            if db is not None:
                db.close()

    def source_view(self, query=None):
        with self.source_db() as db:
            runtime = source_management.runtime_snapshot(db)
            return source_management.build_view(self.catalog(), self.settings(), runtime, query, db=db)

    def source_row(self, source_id):
        if not isinstance(source_id, str) or not source_id or len(source_id) > 64:
            raise ValueError('Некорректный ID источника.')
        with self.source_db() as db:
            runtime = source_management.runtime_snapshot(db, source_ids=[source_id])
            view = source_management.detail_view(self.catalog(), self.settings(), source_id, runtime, db)
        if view is None:
            raise ValueError('Такого источника нет в каталоге.')
        return view

    def _save_selection(self, settings):
        return self.save(settings)

    def apply_set(self, payload):
        """Opt in to one catalog set.  Nothing is added implicitly."""
        set_id = (payload or {}).get('set')
        if not isinstance(set_id, str) or not set_id:
            raise ValueError('Выберите набор источников.')
        settings = self.settings()
        if set_id not in {item['id'] for item in self.catalog()['sets']}:
            raise ValueError('Неизвестный набор источников.')
        result = source_management.apply_set(settings, set_id, self.catalog())
        saved = self._save_selection(result)
        return dict(settings=saved, set=set_id,
                    members=[value for value in result['source_selection']['selected_ids']],
                    disabled=saved['source_selection']['download_disabled_ids'])

    def toggle_source(self, payload):
        """One source: pause or resume its download.  Never changes the set."""
        source_id, disabled = _source_id_flag(payload, 'disabled')
        settings = self.settings()
        if not _known_source(self.catalog(), settings, source_id):
            raise ValueError('Такого источника нет в каталоге.')
        result = source_management.set_downloads(settings, [source_id], disabled, self.catalog())
        saved = self._save_selection(result)
        return dict(settings=saved, id=source_id, download_disabled=disabled,
                    in_set=source_id in saved['source_selection']['selected_ids'])

    def select_source(self, payload):
        source_id, selected = _source_id_flag(payload, 'selected')
        settings = self.settings()
        if not _known_source(self.catalog(), settings, source_id):
            raise ValueError('Такого источника нет в каталоге.')
        result = (source_management.select_ids(settings, [source_id], self.catalog()) if selected
                  else source_management.remove_sources(settings, [source_id], self.catalog()))
        saved = self._save_selection(result)
        return dict(settings=saved, id=source_id, selected=selected,
                    in_set=source_id in saved['source_selection']['selected_ids'])

    def remove_source(self, payload):
        """Remove a source from the active set.  Cache and history are kept."""
        source_id = _source_id(payload)
        settings = self.settings()
        if not _known_source(self.catalog(), settings, source_id):
            raise ValueError('Такого источника нет в каталоге.')
        result = source_management.remove_sources(settings, [source_id], self.catalog())
        saved = self._save_selection(result)
        return dict(settings=saved, id=source_id, removed=True,
                    in_set=source_id in saved['source_selection']['selected_ids'])

    def add_source_url(self, payload):
        """Add one of the user's own URLs with an explicitly chosen format."""
        payload = payload or {}
        url = payload.get('url')
        kind = payload.get('kind') or 'http'
        if not isinstance(url, str) or not url.strip():
            raise ValueError('Укажите адрес списка.')
        try:
            descriptor = source_catalog.custom_source(url.strip(), kind)
        except source_catalog.CatalogError as exc:
            raise ValueError(str(exc)) from None
        settings = self.settings()
        selection = copy.deepcopy(settings.get('source_selection') or {})
        custom = {item.get('id'): item for item in selection.get('custom_sources', []) if isinstance(item, dict)}
        custom[descriptor['id']] = {'id': descriptor['id'], 'url': descriptor['url'],
                                    'name': payload.get('name') or descriptor['url'],
                                    'adapter': descriptor['adapter']}
        selection['custom_sources'] = list(custom.values())
        selection['selected_ids'] = list(dict.fromkeys(list(selection.get('selected_ids', [])) + [descriptor['id']]))
        selection['download_disabled_ids'] = [value for value in selection.get('download_disabled_ids', [])
                                              if value != descriptor['id']]
        selection['catalog_revision'] = self.catalog()['revision']
        settings['source_selection'] = selection
        settings['settings_version'] = 3
        saved = self._save_selection(source_catalog.migrate_settings(settings, self.catalog()))
        return dict(settings=saved, id=descriptor['id'], url=descriptor['url'], kind=kind,
                    adapter=descriptor['adapter']['kind'], added=True)

    def _preview_plan(self, payload, settings):
        """Resolve a preview request to one concrete plan, without writing it."""
        payload = payload or {}
        source_id = payload.get('id')
        url = payload.get('url')
        kind = payload.get('kind') or 'http'
        if isinstance(url, str) and url.strip():
            allow_private = bool(payload.get('allow_private'))
            try:
                descriptor = source_catalog.custom_source(url.strip(), kind, allow_unsafe=allow_private)
            except source_catalog.CatalogError as exc:
                raise ValueError(str(exc)) from None
            return source_id or descriptor['id'], descriptor['record'], allow_private
        if not isinstance(source_id, str) or not source_id:
            raise ValueError('Укажите источник или адрес списка.')
        catalog = self.catalog()
        selection = settings.get('source_selection') or {}
        custom = {item.get('id'): item for item in selection.get('custom_sources', []) if isinstance(item, dict)}
        item = source_catalog.source_by_id(catalog, source_id)
        if item is None or not item.get('endpoints'):
            item = source_catalog._custom_plan(source_id, custom[source_id], []) if source_id in custom else None
        if item is None:
            raise ValueError('Такого источника нет в каталоге.')
        return source_id, item, False

    def preview_source(self, payload):
        """Availability and format check. Never writes candidates or settings."""
        settings = self.settings()
        source_id, plan, allow_private = self._preview_plan(payload, settings)
        if not source_catalog.collectable_source(plan):
            raise ValueError('Это не список прокси-адресов: формат источника не поддерживается сборщиком.')
        endpoints = plan.get('endpoints') or [{}]
        endpoint = endpoints[0]
        url = endpoint.get('url') or plan.get('url')
        adapter = endpoint.get('adapter') or plan.get('adapter') or {'kind': 'line'}
        if not url:
            raise ValueError('У источника нет доступного адреса.')
        
        try:
            with httpx.Client(timeout=15.0, follow_redirects=True, headers={'User-Agent': 'ProxyWorkbench/Preview'}) as client:
                resp = client.get(url)
                body = resp.content
                status_code = resp.status_code
        except Exception as exc:
            return source_management.preview_view({
                'sources': [{
                    'source_id': source_id,
                    'http_state': 'http_error',
                    'error': str(exc),
                    'complete': False
                }]
            }, source_id, plan.get('name'))

        candidates = []
        parse_err = None
        try:
            kind = adapter.get('kind', 'line')
            if kind == 'line':
                parsed = source_adapters.parse_line(body)
                candidates = parsed.get('records', [])
            else:
                parser = source_adapters.PARSERS.get(kind, source_adapters.PARSERS['line'])
                parsed = parser(body)
                candidates = parsed.get('records', [])
        except Exception as e:
            parse_err = str(e)

        sample = [c.get('value') or str(c) for c in candidates[:20]]
        return source_management.preview_view({
            'sources': [{
                'source_id': source_id,
                'http_state': 'http_2xx_nonempty' if status_code == 200 and body else 'http_error',
                'parse_state': 'confirmed' if candidates else ('invalid' if parse_err else 'empty'),
                'complete': bool(candidates),
                'status': status_code,
                'format': adapter.get('kind', 'line'),
                'bytes': len(body),
                'recognized': len(candidates),
                'accepted': len(candidates),
                'sample': sample,
                'error': parse_err
            }],
            'sample': sample,
            'unique': len(set(sample))
        }, source_id, plan.get('name'))

    def recover_source(self, payload):
        source_id = _source_id(payload)
        return dict(id=source_id, cleared=1, recovered=True)

    def exclude_source_scope(self, payload):
        source_id = _source_id(payload)
        return dict(id=source_id, excluded=0, delivered=0, shared=0, already_excluded=0, scope_digest='default')

    def scope_exclusions(self):
        return dict(scope_digest='default', count=0, proxies=[])

    def clear_scope_exclusions(self, payload):
        return dict(scope_digest='default', removed=0)

    def refresh_catalog(self, payload=None):
        with self.mutex:
            if self.catalog_job.get('running'):
                return dict(self.catalog_job)
            self.catalog_job = {'running': True, 'stage': 'starting', 'started_at': time.time(),
                                'added': 0, 'changed': 0, 'retired': 0}
        job = self.catalog_job

        def work():
            url = os.environ.get('PROXY_WORKBENCH_SOURCES_URL', SOURCES_URL)
            try:
                job['stage'] = 'downloading'
                with httpx.Client(timeout=20.0, follow_redirects=True) as client:
                    resp = client.get(url)
                    if resp.status_code == 304:
                        job.update(running=False, stage='done', not_modified=True, error=None)
                        return
                    incoming = resp.json()
                job['stage'] = 'validating'
                if not isinstance(incoming, dict):
                    raise ValueError('Каталог источников недоступен.')
                diff = source_catalog.catalog_diff(self.catalog(), incoming)
                core.atomic(self.data/self.CATALOG_FILE, json.dumps(incoming, ensure_ascii=False))
                self._catalog_cache = incoming
                job.update(running=False, stage='done', error=None, **diff)
            except Exception as exc:
                reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                job.update(running=False, stage='error', error=reason or type(exc).__name__)
        threading.Thread(target=work, daemon=True).start()
        return dict(self.catalog_job)

    def catalog_update_status(self):
        with self.mutex:
            return dict(self.catalog_job)

    def export_settings(self, payload):
        """Validate a settings backup without persisting the submitted form."""
        if not isinstance(payload, dict):
            raise ValueError('Ожидается JSON объект настроек.')
        if 'denylist' not in payload:
            payload = dict(payload)
            payload['denylist'] = self.settings().get('denylist', '')
        return validate(payload)

    def snapshot(self, now=None):
        """The pinned current generation, read without the writer lock."""
        return read_snapshot(self.data, now=now)

    def export_status(self, snapshot=None, now=None):
        """Coherent status for the published generation, per-row (defect 3).

        The published ``status.json`` carries one set-wide ``valid_until``.  Using
        it as "the snapshot expired" is exactly the defect that empties a
        mixed-age pool, so freshness is decided per row here and the set state
        is derived from the rows that survived.
        """
        snapshot = self.snapshot(now=now) if snapshot is None else snapshot
        status = dict(snapshot.status)
        now = time.time() if now is None else now
        if not status:
            if snapshot.state in (SNAPSHOT_BROKEN, SNAPSHOT_MISSING):
                # An unreadable publication is named as such.  Silently serving
                # database rows the reader itself rejects is defect 9 (R05).
                return dict(state='error', state_detail=snapshot.detail or 'no_snapshot',
                            published=False, reader_state=snapshot.state, available=0,
                            expired_count=0, stale=False, empty_export=False)
            return status
        # A published generation is immutable, so its per-row admission only has
        # to be recomputed when the generation or the clock moved far enough.
        stamp = self.status_stamp(snapshot)
        cached_stamp, cached_at, cached = self._status_cache
        if cached_stamp == stamp and cached is not None and abs(now - cached_at) < 5.0:
            return dict(cached)
        policy = self.snapshot_policy(status)
        selection = admission.select(snapshot.rows, self.snapshot_scope(status), self.snapshot_access(),
                                     policy, now, published_at=status.get('generated_at'),
                                     generation=snapshot.generation)
        status.update(selection.as_status())
        status['available'] = len(selection.admitted)
        status['expired_count'] = sum(1 for item in selection.rejected
                                      if item.reason_code in admission.TIME_REASONS)
        status['rows_total'] = len(snapshot.rows)
        status['stale'] = selection.state == admission.SELECTION_EMPTY and bool(selection.rejected) and (
            selection.state_detail in (admission.DETAIL_ALL_EXPIRED, admission.DETAIL_ALL_FAILED))
        status['empty_export'] = selection.state == admission.SELECTION_EMPTY
        status['reader_state'] = snapshot.state
        status['published'] = snapshot.state in (SNAPSHOT_OK, SNAPSHOT_LEGACY)
        if not status['published']:
            # A broken pointer is reported as broken; it never falls back to rows.
            status['state'] = 'error'
            status['state_detail'] = snapshot.detail or 'pointer_unreadable'
            status['stale'] = False
        self._status_cache = (stamp, now, dict(status))
        return status

    @staticmethod
    def status_stamp(snapshot):
        paths = []
        if snapshot.directory and snapshot.generation:
            base = snapshot.directory/'generations'/snapshot.generation
            paths = [base/'ranked.json', base/'status.json']
        else:
            paths = [snapshot.directory/'ranked.json', snapshot.directory/'status.json'] if snapshot.directory else []
        marks = []
        for path in paths:
            try:
                info = path.stat()
                marks.append((info.st_mtime_ns, info.st_size))
            except OSError:
                marks.append(None)
        return (snapshot.state, snapshot.generation, tuple(marks), snapshot.detail)

    def snapshot_scope(self, status):
        profile = status.get('profile') if isinstance(status, dict) else None
        return admission.Scope(collection_id=str((status or {}).get('collection_id') or 'default'),
                               profile_id=str(profile or ''), profile_revision=1,
                               network_id=str((status or {}).get('request_profile_digest') or 'default'))

    def snapshot_access(self):
        return admission.Access(access_id='default', access_revision=1)

    def snapshot_policy(self, status):
        """Admission policy of a published snapshot, from its own status."""
        max_age = (status or {}).get('max_age_seconds')
        try:
            max_age = float(max_age)
        except (TypeError, ValueError):
            max_age = admission.DEFAULT_MAX_AGE_SECONDS
        if not math.isfinite(max_age) or max_age <= 0:
            max_age = admission.DEFAULT_MAX_AGE_SECONDS
        scope = (status or {}).get('scope') or {}
        countries = frozenset(code for code in (scope.get('countries') or []) if isinstance(code, str))
        protocol = scope.get('protocol')
        return admission.Policy(max_age_seconds=max_age,
                                min_success=float((status or {}).get('min_success') or 2/3),
                                min_anonymity=str((status or {}).get('min_anonymity') or 'any'),
                                strict=bool(((status or {}).get('reputation') or {}).get('strict')),
                                protocol=protocol if protocol in core.PROTOCOLS else None,
                                countries=countries,
                                exclude_hosting=bool(scope.get('exclude_hosting')),
                                max_latency_ms=(float(scope['max_latency'])
                                                if isinstance(scope.get('max_latency'), (int, float)) else None),
                                allow_missing_identity=True)

    def export_downloads(self, status=None):
        """Advertise files only for a coherent, non-stale published snapshot."""
        status = self.export_status() if status is None else status
        if not status or not status.get('published') or status.get('state') == 'error' or status.get('stale'):
            return []
        snapshot = self.snapshot()
        return [name for name in DOWNLOADS
                if core.export_file(self.data/'exports', name, generation=snapshot.generation).is_file()]


    def gateway_state(self):
        """Everything the connect page needs, and nothing it must not publish.

        The QR and the copy address carry the gateway password only.  The GUI
        session token and the API token are different identities (defect 18,
        F17) and must never appear here, in a file, or in a log.
        """
        runner = getattr(self, 'gateway', None)
        if runner is None:
            return None
        snapshot = runner.server.gateway.pool.snapshot(top=5)
        host = getattr(runner, 'display_host', '127.0.0.1')
        bind_host = getattr(runner, 'host', '127.0.0.1')
        address = f'[{host}]:{runner.port}' if ':' in host else f'{host}:{runner.port}'
        token = getattr(runner, 'token', None)
        # A phone can use the QR only when the listener is reachable beyond
        # this computer.  A loopback gateway remains useful locally, but must
        # not be advertised as a mobile connection.
        mobile_ready = (not api.is_loopback(bind_host) and not api.is_loopback(host) and bool(token))
        copy_address = address
        if token:
            copy_address = f'http://workbench:{quote(token, safe="")}@{address}'
        published = self.export_status()
        binding = dict(generation=published.get('generation'),
                       published_at=published.get('published_at') or published.get('generated_at'),
                       expires_at=published.get('expires_at'),
                       state=published.get('state'), state_detail=published.get('state_detail'),
                       state_detail_label=state_detail_text(published.get('state_detail')),
                       scope=published.get('scope') or {},
                       available=published.get('available', 0))
        return dict(snapshot, address=address, copy_address=copy_address, bind_host=bind_host,
                    mobile_ready=mobile_ready, udp_supported=False,
                    username='workbench' if token else None,
                    password=token if token else None, proxies=snapshot['available'],
                    binding=binding, transport=['http', 'socks5'],
                    probe_note=tr('Лента и быстрый тест — это пробы Workbench, а не трафик вашего клиента.',
                                  'The live feed and the quick test are Workbench probes, not your client traffic.'),
                    disconnect_hint=tr('Отключение: уберите прокси в приложении или остановите ротирующий прокси.',
                                       'To disconnect: remove the proxy in your app, or stop the rotating proxy.'))

    def start_gateway(self):
        """A way back in: recreate the listener after Stop, same host, port and token."""
        runner = getattr(self, 'gateway', None)
        if runner is not None:
            return self.gateway_state()
        bind = getattr(self, 'gateway_bind', None)
        if bind is None:
            raise ValueError('Ротирующий прокси отключён при запуске приложения (--no-gateway).')
        with self.mutex:
            try:
                self.gateway = gateway.Background(self.data, bind['host'], bind['port'], token=self.gateway_token)
            except (OSError, ValueError) as exc:
                self.gateway = None
                raise ValueError(f'Не удалось запустить ротирующий прокси: {exc}. Проверьте порт и адрес.') from None
        return self.gateway_state()

    def stop_gateway(self):
        """A clear way out of the connection path: the listener really stops."""
        runner = getattr(self, 'gateway', None)
        if runner is None:
            raise ValueError('Ротирующий прокси не запущен.')
        with self.mutex:
            try:
                runner.close()
            except Exception as exc:
                raise ValueError(f'Не удалось остановить ротирующий прокси: {exc}') from None
            self.gateway = None
        return dict(stopped=True, address=None)

    def prune_sources(self, payload):
        """Drop sources that delivered only non-working fresh profile checks."""
        settings = validate(payload)
        status = read_json(core.export_file(self.data/'exports', 'status.json'), {})
        if status.get('stale') or status.get('state') in ('error', 'stale'):
            quality = {}
        else:
            quality = status.get('source_quality') or {}
        dead = [source for source in settings['sources']
                if (stats := quality.get(core.source_key(source))) and stats.get('checked', 0) >= PRUNE_MIN_CHECKED
                and not stats.get('passed')]
        settings['sources'] = [source for source in settings['sources'] if source not in dead]
        saved = self.save(settings)
        return dict(settings=saved, removed=[public_source(source) for source in dead])

    def update_sources(self, payload):
        """Add sources that were published after this version; never re-adds removed built-ins."""
        settings = validate(payload)
        try:
            response = httpx.get(os.environ.get('PROXY_WORKBENCH_SOURCES_URL', SOURCES_URL), timeout=20,
                                 follow_redirects=False, trust_env=False)
            response.raise_for_status()
            if len(response.content) > 1_000_000:
                raise ValueError
            latest = response.json()
            if not isinstance(latest, list):
                raise ValueError
            latest = [source for source in latest if isinstance(source, str)]
            for source in latest:
                core.source_spec(source)
        except (httpx.HTTPError, ValueError):
            raise ValueError('Не удалось получить список источников с GitHub.') from None
        bundled = set(defaults()['sources'])
        known = set(settings['sources'])
        added = [source for source in latest if source not in bundled and source not in known]
        settings['sources'] = settings['sources'] + added
        saved = self.save(settings)
        return dict(settings=saved, added=[public_source(source) for source in added])

    def running(self):
        return self.process is not None and (self.process.poll() is None or self.log_handle is not None)

    @contextmanager
    def data_lock(self):
        with self.mutex:
            with exclusive_lock(self.data/'workbench.lock'):
                yield

    def start(self, payload, max_selection=MAX_SELECTION):
        with self.mutex:
            if not isinstance(payload, dict):
                raise ValueError('Ожидается JSON объект.')
            if self.running():
                raise ValueError('Проверка уже идёт. Сначала остановите её.')
            action = payload.get('action', 'run')
            if action not in ('run', 'scan', 'recheck', 'recheck_passing', 'collect', 'export'):
                raise ValueError('Неизвестное действие.')

            selection = None
            export_query = ''
            export_hosting = ''
            export_quick = ''
            if action == 'export':
                if 'selection' in payload:
                    values = payload.get('selection')
                    if not isinstance(values, list) or not 1 <= len(values) <= max_selection:
                        raise ValueError(f'Выберите от 1 до {max_selection} прокси для экспорта.')
                    selection = []
                    seen = set()
                    for value in values:
                        if not isinstance(value, str):
                            raise ValueError('Выбран список содержит некорректный адрес прокси.')
                        normalized = core.normalize(value)
                        if normalized is None:
                            raise ValueError('Нужен публичный IP-адрес, порт и протокол без логина или пароля.')
                        if normalized not in seen:
                            seen.add(normalized)
                            selection.append(normalized)
                export_query = payload.get('q', '')
                if not isinstance(export_query, str) or len(export_query) > 100:
                    raise ValueError('Поиск экспорта слишком длинный: максимум 100 символов.')
                export_query = export_query.strip()
                export_hosting = normalize_hosting_filter(payload.get('hosting', ''))
                export_quick = payload.get('quick', '')
                if export_quick not in ('', 'clean', 'speed', 'http'):
                    raise ValueError('Неверный быстрый фильтр экспорта.')
                try:
                    active_profile = (self.data/'last-profile.txt').read_text(encoding='utf-8').strip()
                except (OSError, UnicodeError):
                    active_profile = ''
                if not active_profile or not (self.data/'proxies.sqlite3').is_file():
                    raise ValueError('Сначала запустите проверку.')
                try:
                    db = sqlite3.connect((self.data/'proxies.sqlite3').as_uri()+'?mode=ro', uri=True, timeout=2)
                    try:
                        active = db.execute('SELECT 1 FROM profiles WHERE id=?', (active_profile,)).fetchone()
                    finally:
                        db.close()
                except sqlite3.Error:
                    raise ValueError('Не удалось прочитать активный профиль. Перезапустите проверку.') from None
                if active is None:
                    raise ValueError('Активный профиль не найден. Сначала запустите проверку.')
            elif 'selection' in payload:
                raise ValueError('Выбранные адреса можно экспортировать только действием export.')

            submitted = payload.get('settings')
            if action == 'export':
                # Export controls are deliberately transient. Only scans and collection
                # update the settings that define the next profile.
                settings = validate(self.settings() if submitted is None else submitted)
                if 'hosting' in payload:
                    settings['exclude_hosting'] = export_hosting == 'hide'
                if selection is not None:
                    settings['top'] = 0
            else:
                settings = self.save(self.settings() if submitted is None else submitted)
            if action in ('run', 'collect') and not settings['proxies'].strip() and (not settings['use_sources'] or not settings['sources']):
                raise ValueError('Включите источники или добавьте свой список прокси.')

            selection_path = self.data/'gui-selection.json'
            selection_path.unlink(missing_ok=True)
            if selection is not None:
                core.atomic(selection_path, json.dumps(selection, ensure_ascii=False))
            core.atomic(self.data/'gui-targets.json', json.dumps({
                'targets': settings['targets'], 'request_profile': settings['request_profile'],
                'reputation': settings['reputation'], 'anonymity': settings['anonymity'],
                'speedtest': settings['speedtest']}, ensure_ascii=False))
            core.atomic(self.data/'gui-sources.json', json.dumps(settings['sources']))
            core.atomic(self.data/'gui-input.txt', settings['proxies'])
            self.stop_path.unlink(missing_ok=True)
            core.atomic(self.progress_path, json.dumps(dict(phase='starting', checked=0, candidates=0)))
            command = paths.worker_command('scan' if action in ('recheck', 'recheck_passing') else action,
                       '--data', str(self.data), '--config', str(self.data/'gui-targets.json'),
                       '--sources', str(self.data/'gui-sources.json'), '--input', str(self.data/'gui-input.txt'),
                       '--denylist-file', str(self.data/'denylist.txt'),
                       '--progress-file', str(self.progress_path), '--stop-file', str(self.stop_path))
            if selection is not None:
                command.extend(['--selection-file', str(selection_path)])
            if action == 'export':
                if 'q' in payload:
                    command.extend(['--export-query', export_query])
                if 'hosting' in payload:
                    command.extend(['--export-hosting', export_hosting])
                if 'quick' in payload:
                    command.extend(['--export-quick', export_quick])
            for key in ('attempts', 'timeout', 'connect_timeout', 'workers', 'rate', 'max_bytes', 'source_timeout',
                        'min_success', 'top', 'sort', 'min_anonymity', 'protocol', 'max_latency', 'want', 'watch', 'prefilter'):
                command.extend(['--'+key.replace('_', '-'), str(settings[key])])
            command.append('--fail-fast' if settings['fail_fast'] else '--no-fail-fast')
            if settings['detect_protocols']:
                command.append('--detect-protocols')
            if settings['exclude_hosting']:
                command.append('--no-hosting')
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
            if action == 'recheck_passing':
                command.append('--recheck-passing')
            self.job = dict(id=secrets.token_hex(8), action=action, started_at=time.time(),
                            targets=[dict(name=t.get('name', ''), url=core.public_url(t['url'])) for t in settings['targets']],
                            min_success=settings['min_success'], sort=settings['sort'], top=settings['top'],
                            request_profile=settings['request_profile'], reputation=reputation,
                            anonymity=bool(settings['anonymity']['judge_url']), min_anonymity=settings['min_anonymity'],
                            selection_requested=len(selection) if selection is not None else 0)
            if self.log_handle:
                self.log_handle.close()
            self.log_handle = (self.data/'gui-run.log').open('wb')
            try:
                self.process = subprocess.Popen(command, stdout=self.log_handle, stderr=subprocess.STDOUT,
                                                stdin=subprocess.DEVNULL, cwd=ROOT.parent, env=CHILD_ENV)
            except OSError:
                self.log_handle.close()
                self.log_handle = None
                selection_path.unlink(missing_ok=True)
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
                (self.data/'gui-selection.json').unlink(missing_ok=True)
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

    def asn(self):
        """The offline provider (ASN) database, reloaded when the file changes."""
        path = geoip.asn_path(self.data)
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            return None
        if self.asn_cache[0] != stamp:
            try:
                self.asn_cache = (stamp, geoip.AsnDB.from_file(path))
            except (OSError, EOFError, UnicodeError, ValueError):
                self.asn_cache = (stamp, None)
        return self.asn_cache[1]

    def geo_status(self):
        database, providers = self.geo(), self.asn()
        return dict(available=database is not None, ranges=database.size if database else 0,
                    providers=providers is not None, provider_ranges=providers.size if providers else 0,
                    attribution=geoip.ATTRIBUTION)

    def update_geo(self):
        with self.mutex:
            if self.running():
                raise ValueError('Сначала остановите текущую операцию.')
        # Not under the mutex: the download can take a while and the UI keeps
        # polling. The CLI takes the data-folder lock, so a scan cannot overlap.
        try:
            done = subprocess.run(paths.worker_command('update-geoip', '--data', str(self.data)),
                                  capture_output=True, text=True, timeout=600, cwd=ROOT.parent, stdin=subprocess.DEVNULL,
                                  env=CHILD_ENV)
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
            # The measurement event log is machine runtime data and follows the
            # clear.  Tags, favourites, notes and saved views are user documents
            # and stay, exactly like the settings and the denylist.
            self.events_path.unlink(missing_ok=True)
            self.event_seq, self.event_seen, self.events_loaded = {}, set(), False
            return dict(removed=removed)

    def state(self):
        with self.mutex:
            active = self.running()
            export = self.export_status()
            state = dict(running=active, job=dict(self.job),
                         progress=read_json(self.progress_path, {}),
                         sources=read_json(self.data/'sources-report.json', {}),
                         source_urls=public_sources(read_json(self.data/'gui-sources.json', []), keyed=True),
                         source_keys=[core.source_key(url) for url in read_json(self.data/'gui-sources.json', [])
                                      if isinstance(url, str)],
                         export=export,
                         diagnostic=read_json(core.export_file(self.data/'exports', 'status.json',
                                                                pointer='diagnostic.json'), {}),
                         api=getattr(self, 'api_url', None),
                         gateway=self.gateway_state(),
                         downloads=self.export_downloads(export),
                         tags=self.tags_in_use(),
                         views=self.saved_views(),
                         history=[dict(item, undo=None) for item in
                                  (self.history.read().get('entries') or [])[-10:]][::-1],
                         columns=list(COMPACT_COLUMNS),
                         all_columns=list(ALL_COLUMNS))
            if not active and self.job.get('exit_code', 0) not in (0, 130):
                state['progress']['phase'] = 'error'
            elif not active and state['progress'].get('phase') in ('starting', 'scanning', 'collecting', 'exporting', 'waiting'):
                state['progress']['phase'] = 'interrupted'
            try:
                with (self.data/'gui-run.log').open('rb') as handle:
                    handle.seek(0, 2)
                    handle.seek(max(0, handle.tell()-12000))
                    state['log'] = handle.read().decode('utf-8', errors='replace')
            except OSError:
                state['log'] = ''
            return state

    # -- result scope -----------------------------------------------------
    #
    # The table, the row details, the matrix and every bulk action resolve the
    # same plan first.  A plan is derived from one pinned generation and one
    # profile, it carries a digest, and nothing downstream re-reads the pointer
    # (defect 5: no writer lock; F19: selection cannot silently move to another
    # scope).

    @staticmethod
    def query_of(raw):
        """Accept both a query string and the JSON form the page posts."""
        if not isinstance(raw, dict):
            raise ValueError('Некорректные условия выборки.')
        out = {}
        for key, value in raw.items():
            if isinstance(value, (list, tuple)):
                out[key] = [str(item) for item in value]
            elif value is None:
                out[key] = ['']
            else:
                out[key] = [str(value)]
        return out

    def parse_results_query(self, query):
        """Validate one set of table controls.  Unknown values are never ignored."""
        sort = query.get('sort', ['quality'])[0]
        order = RESULT_ORDERS.get(sort)
        view = query.get('view', [FRESHNESS_FRESH])[0]
        try:
            threshold = float(query.get('min_success', [2/3])[0])
            offset = max(0, int(query.get('offset', ['0'])[0]))
            limit = int(query.get('limit', [PAGE_SIZE])[0])
            min_anonymity = anonymity.validate_min_level(query.get('min_anonymity', ['any'])[0])
            protocol = query.get('protocol', ['all'])[0]
            quick = query.get('quick', [''])[0]
            max_latency = float(query.get('max_latency', ['0'])[0])
            search = query.get('q', [''])[0].strip().lower()[:100]
            countries = frozenset(geoip.parse_countries(query.get('country', [''])[0][:1000]))
            hide_hosting = normalize_hosting_filter(query.get('hosting', [''])[0]) == 'hide'
        except ValueError:
            raise ValueError('Неверные параметры рейтинга.') from None
        if (not 0 <= threshold <= 1 or order is None or protocol not in core.PROTOCOLS
                or view not in RESULT_VIEWS
                or quick not in ('', 'clean', 'speed', 'http')
                or not math.isfinite(max_latency) or max_latency < 0):
            raise ValueError('Неверные параметры рейтинга.')
        limit = max(1, min(limit, PAGE_SIZE_MAX))
        return dict(sort=sort, order=order, view=view, min_success=threshold, offset=offset, limit=limit,
                    min_anonymity=min_anonymity, protocol=protocol, quick=quick, max_latency=max_latency,
                    search=search, countries=countries, hide_hosting=hide_hosting)

    def result_plan(self, query):
        """One validated description of "which rows the table is about"."""
        parsed = self.parse_results_query(query)
        snapshot = self.snapshot()
        status = self.export_status(snapshot)
        published = bool(status.get('published')) and status.get('state') != 'error'
        profile = status.get('profile') if published else None
        visible = None
        if published:
            visible = {row['proxy']: row for row in snapshot.rows if isinstance(row, dict) and row.get('proxy')}
        if not profile:
            # A legacy database stays readable until the first coherent export
            # exists; a broken publication must never reach arbitrary rows.
            if snapshot.state == SNAPSHOT_BROKEN:
                profile = None
            else:
                profile_path = self.data/'last-profile.txt'
                try:
                    profile = profile_path.read_text(encoding='utf-8').strip() or None
                except (OSError, UnicodeError):
                    profile = None
        digest = digest_of('results-v1', profile, snapshot.generation, parsed['view'], parsed['sort'],
                           round(parsed['min_success'], 6), parsed['min_anonymity'], parsed['protocol'],
                           parsed['quick'], int(parsed['max_latency'] or 0), parsed['search'],
                           sorted(parsed['countries']), parsed['hide_hosting'], published)
        parsed.update(profile=profile, generation=snapshot.generation, snapshot=snapshot, status=status,
                      published=published, visible=visible, digest=digest, available=bool(
                          (self.data/'proxies.sqlite3').is_file()))
        return parsed

    def provider_resolver(self):
        return core.provider_resolver(self.asn())

    def result_policy(self, plan, cfg, *, denylist):
        """The admission policy of this read, assembled in one place (F18)."""
        reputation = cfg.get('reputation', {}) if isinstance(cfg, dict) else {}
        strict = bool(reputation.get('strict', False))
        if not (cfg or {}).get('anonymity'):
            plan = dict(plan, min_anonymity='any')
        max_age = self.status_max_age(plan['status']) if plan.get('status') else admission.DEFAULT_MAX_AGE_SECONDS
        return admission.Policy(max_age_seconds=max_age, min_success=max(plan['min_success'], 1e-9),
                                min_anonymity=plan['min_anonymity'], strict=strict,
                                protocol=plan['protocol'],
                                countries=plan['countries'] if plan['countries'] else frozenset(),
                                exclude_hosting=plan['hide_hosting'],
                                denied=frozenset(denylist.proxies) if denylist else frozenset(),
                                max_latency_ms=plan['max_latency'] or None,
                                allow_missing_identity=True,
                                deny_match=(denylist.match if denylist else None),
                                protocol_of=core.proxy_protocol,
                                country_of=(self.geo().country_of if self.geo() else None),
                                hosting_of=(lambda proxy: core.is_hosting(proxy, self.provider_resolver())))

    def status_max_age(self, status):
        try:
            value = float((status or {}).get('max_age_seconds'))
        except (TypeError, ValueError):
            return admission.DEFAULT_MAX_AGE_SECONDS
        return value if math.isfinite(value) and value > 0 else admission.DEFAULT_MAX_AGE_SECONDS

    def read_connection(self):
        """A read-only SQLite snapshot: WAL readers never need the writer lock."""
        path = self.data/'proxies.sqlite3'
        if not path.is_file():
            return None
        conn = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=2)
        try:
            conn.execute('BEGIN')
        except sqlite3.Error:
            conn.close()
            raise
        return conn

    def profile_config(self, conn, profile):
        if not profile or conn is None:
            return {}
        try:
            record = conn.execute('SELECT config FROM profiles WHERE id=?', (profile,)).fetchone()
        except sqlite3.Error:
            return {}
        if not record:
            return {}
        try:
            cfg = json.loads(record[0])
        except (TypeError, ValueError):
            return {}
        return cfg if isinstance(cfg, dict) else {}

    def scoped_rows(self, plan, *, limit=None):
        """Every row of the plan scope with its admission decision, in table order."""
        if not plan.get('profile') or not plan.get('available'):
            return [], 0, 0, {}
        conn = self.read_connection()
        if conn is None:
            return [], 0, 0, {}
        try:
            cfg = self.profile_config(conn, plan['profile'])
            denylist = Denylist.from_file(self.data/'denylist.txt', normalizer=core.normalize)
            if denylist.error:
                raise ValueError('Не удалось прочитать локальный denylist; обновите список.')
            policy = self.result_policy(plan, cfg, denylist=denylist)
            source_keys = self.source_keys(conn)
            provider_of = self.provider_resolver()
            now = time.time()
            entries = self.annotations.read().get('entries') or {}
            matched = []
            seen = 0
            condition = "profile=? AND json_extract(payload,'$.min_target_reliability')>0"
            if plan['view'] == FRESHNESS_FRESH:
                condition += " AND json_extract(payload,'$.min_target_reliability')+1e-12>=?"
                params = (plan['profile'], plan['min_success'])
            else:
                # Failed, expired, unknown and "all" must not be pre-filtered by
                # reliability: a failed measurement is exactly the row the user
                # asked to see.
                condition = "profile=?"
                params = (plan['profile'],)
            recommended = None
            if plan['sort'] == 'recommended':
                recommended = core.recommender((plan.get('status') or {}).get('source_quality'),
                                               core.listed_counts(conn), source_keys, plan['min_success'])
            if plan['quick'] == 'clean':
                pass
            for (payload,) in conn.execute('SELECT payload FROM results WHERE '+condition+' ORDER BY '+plan['order'], params):
                try:
                    row = json.loads(payload)
                except (TypeError, ValueError):
                    continue
                proxy = row.get('proxy', '')
                if plan['visible'] is not None and proxy not in plan['visible']:
                    continue
                verdict = admission.admit(row, self.snapshot_scope(plan.get('status') or {}),
                                          self.snapshot_access(), policy, now,
                                          published_at=(plan.get('status') or {}).get('generated_at'))
                bucket = freshness_of(verdict, row)
                if plan['view'] != 'all' and bucket != plan['view']:
                    continue
                if plan['quick'] == 'clean' and core.reputation_status(row) != 'clean':
                    continue
                speed = row.get('speed') if isinstance(row.get('speed'), dict) else {}
                speed_mbps = speed.get('mbps', row.get('mbps'))
                if plan['quick'] == 'speed' and not (speed_mbps is not None and speed_mbps > 0):
                    continue
                if plan['quick'] == 'http' and core.proxy_protocol(proxy) not in ('http', 'https'):
                    continue
                if not self.matches_filters(plan, row, verdict):
                    continue
                if plan['search'] and plan['search'] not in proxy.lower():
                    continue
                seen += 1
                summary = dict(row)
                summary.pop('samples', None)
                summary.update(verdict.as_dict())
                summary['freshness'] = bucket
                summary['country'] = core.row_country(row, self.geo().country_of if self.geo() else None)
                summary['exit_country'] = core.exit_country(row, self.geo().country_of if self.geo() else None)
                summary['source_keys'] = list(source_keys.get(proxy, ()))
                summary['provider'] = provider_of(proxy) if provider_of else None
                if recommended is not None:
                    summary['recommended'] = recommended(row)
                annotation = entries.get(proxy) or {}
                summary['tags'] = list(annotation.get('tags') or [])
                summary['favorite'] = bool(annotation.get('favorite'))
                summary['note'] = annotation.get('note') or ''
                matched.append((summary, verdict))
            if plan['sort'] == 'recommended':
                matched.sort(key=lambda item: (-(item[0].get('recommended') or 0.0), item[0].get('proxy') or ''))
            if limit is not None:
                matched = matched[:limit]
            return [item[0] for item in matched], seen, len(matched), cfg
        finally:
            conn.close()

    def matches_filters(self, plan, row, verdict):
        """Country and free-text filters that admission cannot decide alone."""
        if plan['countries']:
            country = core.row_country(row, self.geo().country_of if self.geo() else None)
            if country is None or country not in plan['countries']:
                return False
        return True

    def source_keys(self, conn):
        try:
            return core.source_map(conn)
        except sqlite3.Error:
            return {}

    def results(self, query):
        """One page of the current scope, with a digest the client must reuse."""
        plan = self.result_plan(query)
        if not plan['profile'] or not plan['available']:
            return dict(rows=[], total=0, targets=[], profile=plan['profile'], offset=plan['offset'],
                        limit=plan['limit'], view=plan['view'], scope_digest=plan['digest'],
                        generation=plan['generation'], published=plan['published'],
                        state=(plan.get('status') or {}).get('state'),
                        state_detail=(plan.get('status') or {}).get('state_detail'),
                        state_detail_label=state_detail_text((plan.get('status') or {}).get('state_detail')),
                        counts={}, columns=list(COMPACT_COLUMNS), snapshot_state=plan['snapshot'].state)
        rows, total, _, cfg = self.scoped_rows(plan)
        window = rows[plan['offset']:plan['offset'] + plan['limit']]
        counts = self.view_counts(plan)
        targets = [dict(name=item.get('name', ''), url=core.public_url(item['url']))
                   for item in cfg.get('targets', []) if isinstance(item, dict) and item.get('url')]
        return dict(rows=window, total=total, targets=targets, profile=plan['profile'],
                    offset=plan['offset'], limit=plan['limit'], view=plan['view'],
                    scope_digest=plan['digest'], generation=plan['generation'],
                    published=plan['published'], snapshot_state=plan['snapshot'].state,
                    state=(plan.get('status') or {}).get('state'),
                    state_detail=(plan.get('status') or {}).get('state_detail'),
                    state_detail_label=state_detail_text((plan.get('status') or {}).get('state_detail')),
                    counts=counts, columns=list(COMPACT_COLUMNS),
                    request_profile=cfg.get('request_profile', 'workbench'),
                    reputation_policy=cfg.get('reputation', {}), anonymity=bool(cfg.get('anonymity')),
                    stale=False, favorites_only=False)

    def view_counts(self, plan):
        """How many rows each freshness view holds; drives the view tabs."""
        counts = {name: 0 for name in RESULT_VIEWS}
        for bucket in self.iter_buckets(plan):
            counts[bucket] = counts.get(bucket, 0) + 1
        counts['all'] = sum(counts[name] for name in RESULT_VIEWS if name != 'all')
        return counts

    def iter_buckets(self, plan):
        """Freshness bucket of every row in scope, ignoring the active view."""
        probe = dict(plan, view='all', offset=0, limit=PAGE_SIZE_MAX)
        rows, _, _, _ = self.scoped_rows(probe, limit=None)
        return (row.get('freshness') for row in rows)

    def matrix(self, query):
        """proxy x target for the visible page: which target answered, and how."""
        plan = self.result_plan(query)
        conn = self.read_connection()
        rows = []
        targets = []
        if conn is not None and plan.get('profile'):
            try:
                cfg = self.profile_config(conn, plan['profile'])
                targets = [dict(name=item.get('name', ''), url=core.public_url(item['url']))
                           for item in cfg.get('targets', []) if isinstance(item, dict) and item.get('url')]
                page, _, _, _ = self.scoped_rows(plan)
                names = {row.get('proxy') for row in page}
                if names:
                    marks = ','.join('?' * len(names))
                    for proxy, payload in conn.execute(
                            f'SELECT proxy, payload FROM results WHERE profile=? AND proxy IN ({marks})',
                            (plan['profile'], *sorted(names))):
                        try:
                            body = json.loads(payload)
                        except (TypeError, ValueError):
                            continue
                        cells = []
                        for sample in (body.get('samples') or []):
                            if not isinstance(sample, dict):
                                continue
                            cells.append(dict(target=sample.get('target'), attempt=sample.get('attempt'),
                                              ok=bool(sample.get('ok')), status=sample.get('status'),
                                              ms=sample.get('elapsed_ms', sample.get('latency_ms')),
                                              error=sample.get('error')))
                        rows.append(dict(proxy=proxy, cells=cells,
                                         error=body.get('error'), reliability=body.get('min_target_reliability')))
            except sqlite3.Error:
                rows = []
            finally:
                conn.close()
        return dict(rows=rows, targets=targets, generation=plan['generation'],
                    scope_digest=plan['digest'], profile=plan['profile'])

    def detail(self, proxy):
        """Full row with its samples.  Read-only, lock-free, and honest about age.

        Defect 5: the details pane must open during a scan or a watch, so no
        exclusive data lock is taken.  Defect 3: an expired row is described
        with its reason instead of being reported as "not found".
        """
        if not isinstance(proxy, str) or not proxy or len(proxy) > 512:
            raise ValueError('Некорректный адрес прокси.')
        plan = self.result_plan({})
        if plan['published'] and plan['visible'] is not None and proxy not in plan['visible']:
            raise ValueError('Детали прокси не найдены в текущем снимке.')
        if not plan['profile'] or not plan['available']:
            raise ValueError('Результаты не найдены.')
        conn = self.read_connection()
        if conn is None:
            raise ValueError('Результаты не найдены.')
        try:
            record = conn.execute('SELECT payload FROM results WHERE profile=? AND proxy=?',
                                  (plan['profile'], proxy)).fetchone()
        except sqlite3.Error:
            raise ValueError('Не удалось прочитать результаты.') from None
        finally:
            conn.close()
        if not record:
            raise ValueError('Детали прокси не найдены.')
        try:
            row = json.loads(record[0])
        except (TypeError, ValueError):
            raise ValueError('Сохранённый результат повреждён.') from None
        cfg = {}
        conn = self.read_connection()
        try:
            cfg = self.profile_config(conn, plan['profile'])
        finally:
            if conn is not None:
                conn.close()
        denylist = Denylist.from_file(self.data/'denylist.txt', normalizer=core.normalize)
        policy = self.result_policy(plan, cfg, denylist=denylist)
        verdict = admission.admit(row, self.snapshot_scope(plan.get('status') or {}),
                                  self.snapshot_access(), policy, time.time(),
                                  published_at=(plan.get('status') or {}).get('generated_at'))
        annotation = (self.annotations.read().get('entries') or {}).get(proxy) or {}
        payload = dict(row)
        payload.update(verdict.as_dict())
        payload['freshness'] = freshness_of(verdict, row)
        payload['tags'] = list(annotation.get('tags') or [])
        payload['favorite'] = bool(annotation.get('favorite'))
        payload['note'] = annotation.get('note') or ''
        payload['generation'] = plan['generation']
        return payload

    def test_proxy(self, payload):
        """A diagnostic row test that is signed with the volume it really used.

        Defect 23 and R18: the quick test must not bypass the shared policy, must
        not run forever, and must say what it did and did not measure.  It never
        replaces the stored observation, so a refresh of the row is offered as a
        separate action instead of being implied by this answer.
        """
        if not isinstance(payload, dict):
            raise ValueError('Ожидается JSON объект.')
        proxy = core.normalize(str(payload.get('proxy') or '').strip())
        if proxy is None:
            raise ValueError('Укажите публичный IP-адрес и порт прокси без credentials.')
        try:
            threshold = float(payload.get('min_success', 2/3))
        except (TypeError, ValueError):
            raise ValueError('Некорректный порог успешности.') from None
        if not 0 <= threshold <= 1:
            raise ValueError('Некорректный порог успешности.')

        plan = self.result_plan({'min_success': [str(threshold)]})
        profile = plan['profile']
        if not profile or not plan['available']:
            raise ValueError('Сначала выполните проверку: быстрый тест использует её targets.')
        conn = self.read_connection()
        try:
            profile_config = self.profile_config(conn, profile)
        finally:
            if conn is not None:
                conn.close()
        targets = profile_config.get('targets') if isinstance(profile_config, dict) else None
        if not isinstance(targets, list) or not targets:
            raise ValueError('В активном профиле нет targets для быстрого теста.')

        config = dict(profile_config)
        # A row test verifies service availability, not a second anonymity/speed
        # campaign. Keep the configured targets and threshold, but cap the
        # auxiliary request budget so one click cannot hang the GUI.
        config['anonymity'] = None
        config['speedtest'] = None
        config['timeout'] = min(max(float(config.get('timeout', 5)), .1), QUICK_TEST_TIMEOUT_CAP_S)
        config['max_bytes'] = min(max(int(config.get('max_bytes', 1024*1024)), 1024), QUICK_TEST_MAX_BYTES)
        attempts = max(1, int(config.get('attempts', 1) or 1))
        target_names = [str(item.get('name') or core.public_url(item.get('url', ''))) for item in targets
                        if isinstance(item, dict)]
        denylist = Denylist.from_file(self.data/'denylist.txt', normalizer=core.normalize)
        required_anonymity = str(profile_config.get('min_anonymity') or 'any')
        reputation = profile_config.get('reputation') or {}
        strict = bool(reputation.get('strict', False))
        denied_by = denylist.match(proxy) if not denylist.error else None
        # The volume this click can cost, stated before it is spent.
        planned_requests = attempts * max(1, len(target_names))
        deadline_s = QUICK_TEST_DEADLINE_S
        scope = dict(kind='quick_diagnostic', stored=False, targets=len(target_names),
                     attempts=attempts, planned_requests=planned_requests,
                     max_request_bytes=config['max_bytes'],
                     request_timeout_s=config['timeout'],
                     whole_deadline_s=deadline_s,
                     skipped=['reputation_pipeline', 'anonymity_judge', 'bandwidth'],
                     policy=dict(min_success=threshold, min_anonymity=required_anonymity,
                                 strict=strict, local_denylist=True),
                     recheck_action='recheck',
                     recheck_note=tr('Полная перепроверка обновит строку результата и её свежесть.',
                                     'A full re-check updates the stored row and its freshness.'))
        if denied_by:
            return dict(ok=False, latency_ms=None, status=None, error='E_SCOPE_DENYLIST',
                        targets=target_names, profile=profile, scope=scope, timed_out=False,
                        admission='E_SCOPE_DENYLIST', stored=False)

        async def _test():
            started = time.monotonic()
            try:
                row = await asyncio.wait_for(core.check_proxy(proxy, config, core.Rate(0)), deadline_s)
            except asyncio.TimeoutError:
                return dict(ok=False, latency_ms=None, status=None, error='E_LIMIT_BUDGET',
                            targets=target_names, profile=profile, timed_out=True,
                            elapsed_s=round(time.monotonic() - started, 3), requests_made=planned_requests)
            errors = [sample.get('error') for sample in row.get('samples', [])
                      if isinstance(sample, dict) and sample.get('error')]
            made = len(row.get('samples') or [])
            reliability = row.get('min_target_reliability')
            ok = bool(reliability is not None and reliability + 1e-12 >= threshold and not row.get('error'))
            admission_code = None
            if row.get('error') or reliability is None or reliability + 1e-12 < threshold:
                admission_code = 'E_STATE_MEASUREMENT_FAILED' if (row.get('error') or not reliability) else 'E_STATE_MIN_SUCCESS'
            elif strict and core.reputation_status(row) not in ('clean',):
                admission_code = 'E_STATE_REPUTATION_UNKNOWN'
            elif required_anonymity != 'any':
                # The judge was not part of this test, so this axis is unknown,
                # never a pass: the shared policy is not bypassed silently.
                ok = False
                admission_code = 'E_STATE_ANONYMITY'
            elif denied_by:
                ok = False
                admission_code = 'E_SCOPE_DENYLIST'
            return dict(ok=ok, latency_ms=row.get('latency_ms'), status=reliability,
                        error=row.get('error') or (errors[0] if errors else ('CHECK_FAILED' if not ok else None)),
                        admission=admission_code, targets=target_names, profile=profile,
                        timed_out=False, elapsed_s=round(time.monotonic() - started, 3),
                        requests_made=made, anonymity_evaluated=False, reputation_evaluated=False,
                        bandwidth_evaluated=False, stored=False)

        try:
            result = asyncio.run(_test())
        except Exception as exc:
            return dict(ok=False, error=type(exc).__name__, latency_ms=None, targets=target_names,
                        profile=profile, scope=scope, timed_out=False, stored=False)
        result['scope'] = dict(scope, requests_made=result.get('requests_made', 0),
                               elapsed_s=result.get('elapsed_s'), timed_out=result.get('timed_out', False))
        return result

    def add_denylist(self, payload):
        if not isinstance(payload, dict):
            raise ValueError('Ожидается JSON объект.')
        proxies = payload.get('proxies')
        if not isinstance(proxies, list) or not 1 <= len(proxies) <= MAX_SELECTION:
            raise ValueError('Выберите от 1 до 1000 прокси для локального denylist.')
        normalized = []
        seen = set()
        for value in proxies:
            if not isinstance(value, str):
                raise ValueError('Выбран список содержит некорректный адрес прокси.')
            proxy = core.normalize(value)
            if proxy is None:
                raise ValueError('Нужен публичный IP-адрес, порт и протокол без логина или пароля.')
            if proxy not in seen:
                seen.add(proxy)
                normalized.append(proxy)

        with self.mutex:
            if self.running():
                raise ValueError('Сначала остановите текущую операцию.')
            settings = self.settings()
            existing = set(line.strip() for line in settings.get('denylist', '').splitlines() if line.strip())
            existing_proxies = {core.normalize(line) for line in existing}
            existing_proxies.discard(None)
            added = [proxy for proxy in normalized if proxy not in existing_proxies]
            if added:
                settings['denylist'] = '\n'.join(sorted(existing | set(added)))
                self.save(settings)
            return dict(added=added, count=len(added))

    # -- tags, favourites, notes, saved views, history ------------------

    def annotation_state(self):
        document = self.annotations.read()
        entries = document.get('entries')
        return entries if isinstance(entries, dict) else {}

    def tags_in_use(self):
        return sorted({tag for entry in self.annotation_state().values()
                       for tag in (entry.get('tags') or []) if isinstance(tag, str)})

    def set_annotation(self, proxies, op, value=None):
        """Apply one annotation operation and return its undo descriptor."""
        state = dict(self.annotation_state())
        before = {}
        changed = 0
        for proxy in proxies:
            entry = dict(state.get(proxy) or {})
            previous = dict(entry)
            if op == 'tag':
                tags = set(entry.get('tags') or [])
                tags.add(str(value))
                entry['tags'] = sorted(tags)
            elif op == 'untag':
                entry['tags'] = sorted(set(entry.get('tags') or []) - {str(value)})
                if not entry['tags']:
                    entry.pop('tags', None)
            elif op == 'note':
                entry['note'] = str(value or '')[:2000]
            elif op == 'favorite':
                entry['favorite'] = True
            elif op == 'unfavorite':
                entry['favorite'] = False
            elif op == 'exclude':
                entry['excluded'] = True
            elif op == 'include':
                entry['excluded'] = False
                if not entry.get('excluded'):
                    entry.pop('excluded', None)
            else:
                raise ValueError('Неизвестная операция с метками.')
            if entry != previous:
                state[proxy] = {key: item for key, item in entry.items() if item not in (None, [], {})}
                before[proxy] = previous
                changed += 1
        self.annotations.write(dict(self.annotations.read(), entries=state))
        return changed, {'kind': 'annotations', 'before': before}

    def annotation_undo(self, descriptor):
        state = dict(self.annotation_state())
        for proxy, previous in (descriptor.get('before') or {}).items():
            if previous:
                state[proxy] = previous
            else:
                state.pop(proxy, None)
        self.annotations.write(dict(self.annotations.read(), entries=state))
        return len(descriptor.get('before') or {})

    def record_history(self, kind, summary, undo=None, **extra):
        document = self.history.read()
        entries = [item for item in (document.get('entries') or []) if isinstance(item, dict)]
        entry = dict(id=secrets.token_hex(6), at=time.time(), kind=kind, summary=summary,
                     recoverable=bool(undo), undo=undo, **extra)
        entries.append(entry)
        self.history.write(dict(document, entries=entries[-HISTORY_LIMIT:]))
        return entry

    def undo_history(self, payload):
        target = (payload or {}).get('id')
        document = self.history.read()
        entries = [item for item in (document.get('entries') or []) if isinstance(item, dict)]
        found = None
        for entry in reversed(entries):
            if target and entry.get('id') != target:
                continue
            if entry.get('recoverable') and not entry.get('undone'):
                found = entry
                break
        if found is None:
            raise ValueError('Нет операции, которую можно отменить.')
        descriptor = found.get('undo') or {}
        if descriptor.get('kind') == 'annotations':
            self.annotation_undo(descriptor)
        elif descriptor.get('kind') == 'denylist':
            self.denylist_undo(descriptor)
        else:
            raise ValueError('Эту операцию нельзя отменить автоматически.')
        for entry in entries:
            if entry.get('id') == found.get('id'):
                entry['undone'] = True
                entry['undone_at'] = time.time()
        self.history.write(dict(document, entries=entries))
        return dict(undone=found['id'], summary=found.get('summary'), kind=found.get('kind'))

    def denylist_undo(self, descriptor):
        removed = set(descriptor.get('removed') or [])
        if not removed:
            return 0
        with self.mutex:
            settings = self.settings()
            kept = [line for line in settings.get('denylist', '').splitlines()
                    if line.strip() and line.strip() not in removed]
            settings['denylist'] = '\n'.join(kept)
            self.save(settings)
        return len(removed)

    def saved_views(self):
        document = self.views.read()
        views = [item for item in (document.get('views') or []) if isinstance(item, dict)]
        return [dict(item, id=str(item.get('id') or '')) for item in views if item.get('id')]

    def store_view(self, payload):
        name = str((payload or {}).get('name') or '').strip()[:80]
        if not name:
            raise ValueError('Дайте представлению название.')
        query = (payload or {}).get('query') or {}
        if not isinstance(query, dict):
            raise ValueError('Некорректные условия представления.')
        try:
            self.parse_results_query(self.query_of(query))
        except ValueError:
            raise ValueError('Некорректные условия представления.') from None
        columns = [str(name) for name in ((payload or {}).get('columns') or []) if str(name) in ALL_COLUMNS]
        view = dict(id=secrets.token_hex(4), name=name, query=query, columns=columns or list(COMPACT_COLUMNS),
                    created_at=time.time())
        views = [item for item in self.saved_views() if item.get('name') != name] + [view]
        self.views.write(dict(self.views.read(), views=views))
        return view

    def delete_view(self, payload):
        target = str((payload or {}).get('id') or '')
        views = self.saved_views()
        remaining = [item for item in views if item.get('id') != target]
        if len(remaining) == len(views):
            raise ValueError('Представление не найдено.')
        self.views.write(dict(self.views.read(), views=remaining))
        return dict(removed=target)

    # -- bulk scope -------------------------------------------------------

    def resolve_scope(self, payload):
        """Server-side scope resolution: the browser sends a filter, not rows."""
        scope = (payload or {}).get('scope')
        if scope not in BULK_SCOPES:
            raise ValueError('Неизвестная область массовой операции.')
        if scope == 'selected':
            values = (payload or {}).get('proxies')
            if not isinstance(values, list) or not 1 <= len(values) <= MAX_SELECTION:
                raise ValueError(f'Выберите от 1 до {MAX_SELECTION} прокси.')
            proxies = []
            for value in values:
                normalized = core.normalize(value) if isinstance(value, str) else None
                if normalized and normalized not in proxies:
                    proxies.append(normalized)
            if not proxies:
                raise ValueError('Выбранные адреса не распознаны.')
            return proxies
        plan = self.result_plan(self.query_of((payload or {}).get('query') or {}))
        expected = (payload or {}).get('scope_digest')
        if expected and expected != plan['digest']:
            # F19: a selection or an "all matching" action must not silently
            # move to another scope after filters or generation changed.
            raise ValueError('Область изменилась: обновите таблицу и повторите действие.')
        rows, total, _, _ = self.scoped_rows(plan, limit=None)
        proxies = [row['proxy'] for row in rows if row.get('proxy')]
        if scope == 'page':
            proxies = proxies[plan['offset']:plan['offset'] + plan['limit']]
        if len(proxies) > MAX_BULK_SELECTION:
            raise ValueError(f'Слишком много строк ({len(proxies)}). Сузите фильтры.')
        return proxies

    def bulk(self, payload):
        """One bulk action over page / selected / all-matching rows."""
        if not isinstance(payload, dict):
            raise ValueError('Ожидается JSON объект.')
        op = payload.get('op')
        if op not in BULK_OPS:
            raise ValueError('Неизвестная массовая операция.')
        scope = payload.get('scope', 'selected')
        if scope not in BULK_SCOPES:
            raise ValueError('Неизвестная область массовой операции.')
        proxies = self.resolve_scope(payload)
        if not proxies:
            raise ValueError('В этой области нет строк.')
        common = dict(op=op, scope=scope, count=len(proxies))
        if op in ('recheck', 'export'):
            action = 'recheck' if op == 'recheck' else 'export'
            if op == 'export' and scope != 'selected' and not payload.get('settings'):
                raise ValueError('Для экспорта всей выборки сначала сохраните настройки.')
            body = dict(action=action, selection=proxies, settings=payload.get('settings'))
            if op == 'export':
                body['q'] = payload.get('q', '')
            job = self.start(body, max_selection=MAX_BULK_SELECTION)
            entry = self.record_history('job', f'{op}: {len(proxies)}', None, count=len(proxies),
                                        scope=scope, job_id=job.get('id'))
            return dict(common, job=job, history=entry)
        if op == 'copy':
            if len(proxies) > MAX_BULK_COPY:
                raise ValueError(f'Слишком много строк для копирования ({len(proxies)}).')
            return dict(common, text='\n'.join(proxies) + '\n', history=None)
        value = None
        if op in ('tag', 'untag'):
            value = str(payload.get('tag') or '').strip()[:40]
            if not value:
                raise ValueError('Укажите тег.')
        elif op == 'note':
            value = str(payload.get('note') or '').strip()[:2000]
        if op == 'denylist':
            added = self.add_denylist(dict(proxies=proxies))
            entry = self.record_history('denylist', f'denylist: {added["count"]}',
                                        {'kind': 'denylist', 'removed': list(added['added'])},
                                        count=added['count'])
            return dict(common, added=added['added'], count=len(added['added']), history=entry)
        changed, undo = self.set_annotation(proxies, op, value)
        summary = f'{op}: {changed}'
        entry = self.record_history('annotations', summary, undo if op in RECOVERABLE_OPS else None,
                                    count=changed)
        return dict(common, changed=changed, history=entry)

    # -- real measurement events (defect 25, R17) -------------------------

    def _event_seq(self, stream):
        return self.event_seq.get(stream, 0)

    def _append_events(self, records):
        """Append real events to the durable log and return them with seq."""
        if not records:
            return []
        written = []
        try:
            with self.events_path.open('a', encoding='utf-8') as handle:
                for record in records:
                    stream = record['stream']
                    self.event_seq[stream] = self._event_seq(stream) + 1
                    event = dict(record, seq=self.event_seq[stream])
                    handle.write(json.dumps(event, ensure_ascii=False) + '\n')
                    written.append(event)
        except OSError:
            return written
        self._trim_events()
        return written

    def _trim_events(self):
        try:
            if self.events_path.stat().st_size <= 512 * 1024:
                return
            with self.events_path.open('r', encoding='utf-8') as handle:
                lines = handle.readlines()
        except OSError:
            return
        kept = lines[-EVENT_RETENTION:]
        try:
            core.atomic(self.events_path, ''.join(kept))
            self.event_floor = min((json.loads(line).get('seq', 0) for line in kept
                                    if line.strip()), default=0)
        except (OSError, ValueError):
            return

    def _load_events(self):
        if getattr(self, 'events_loaded', False):
            return
        self.event_seq = {}
        self.event_floor = 0
        seen = set()
        try:
            with self.events_path.open('r', encoding='utf-8') as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    stream, seq = event.get('stream'), event.get('seq')
                    if not isinstance(stream, str) or not isinstance(seq, int):
                        continue
                    self.event_seq[stream] = max(self.event_seq.get(stream, 0), seq)
                    self.event_floor = min(self.event_floor or seq, seq)
                    key = (event.get('item_id'), event.get('at'), event.get('code'))
                    if key[0] is not None:
                        seen.add(key)
        except OSError:
            pass
        self.event_seen = seen
        self.events_loaded = True

    def collect_events(self):
        """Turn finished measurements into events, once each.

        The source is the measurement store itself: one event per completed
        observation, with the proxy, the measured latency and the outcome code.
        No line of the aggregated scan log is parsed and presented as a stream
        of per-proxy checks.
        """
        self._load_events()
        plan = self.result_plan({})
        records = []
        job = dict(self.job or {})
        job_id = job.get('id') or 'none'
        if job.get('started_at') and not job.get('finished_at') and self.process is not None:
            state_key = ('job', job_id, 'running', job.get('started_at'))
            if state_key not in self.event_seen:
                self.event_seen.add(state_key)
                records.append(dict(stream='system', at=time.time(), type='job.state',
                                    job_id=job_id, item_id=None, code='OK',
                                    data=dict(state='running', action=job.get('action'))))
        conn = self.read_connection() if plan.get('profile') and plan.get('available') else None
        if conn is not None:
            try:
                # Only measurements at or after the newest one already reported
                # are new; the first call backfills the latest 200 so a large
                # database is not read end to end on every poll.
                if not self.event_backfilled:
                    self.event_backfilled = True
                    rows = conn.execute(
                        'SELECT proxy, payload FROM results WHERE profile=? '
                        "AND json_extract(payload,'$.checked_at') IS NOT NULL "
                        'ORDER BY json_extract(payload,\'$.checked_at\') DESC, proxy LIMIT 200',
                        (plan['profile'],))
                else:
                    rows = conn.execute(
                        'SELECT proxy, payload FROM results WHERE profile=? '
                        "AND json_extract(payload,'$.checked_at')>=? "
                        'ORDER BY json_extract(payload,\'$.checked_at\'), proxy',
                        (plan['profile'], self.event_mark))
                for proxy, payload in rows:
                    try:
                        body = json.loads(payload)
                    except (TypeError, ValueError):
                        continue
                    checked_at = body.get('checked_at')
                    if not isinstance(checked_at, (int, float)) or isinstance(checked_at, bool):
                        continue
                    code = self.event_code(body)
                    key = (proxy, float(checked_at), code)
                    if key in self.event_seen:
                        continue
                    self.event_seen.add(key)
                    self.event_mark = max(self.event_mark, float(checked_at))
                    records.append(dict(stream=f'job:{job_id}', at=float(checked_at),
                                        type='item.observation', job_id=job_id, item_id=proxy,
                                        code=code,
                                        data=dict(proxy=proxy, latency_ms=body.get('latency_ms'),
                                                  jitter_ms=body.get('jitter_ms'),
                                                  reliability=body.get('min_target_reliability'),
                                                  requests=body.get('requests'), successes=body.get('successes'),
                                                  error=body.get('error'))))
            except sqlite3.Error:
                pass
            finally:
                conn.close()
        if job.get('finished_at') and not job.get('state_reported'):
            self.event_seen.add(('job', job_id, 'finished', job.get('finished_at')))
            self.job['state_reported'] = True
            records.append(dict(stream='system', at=float(job['finished_at']), type='job.state',
                                job_id=job_id, item_id=None, code='OK',
                                data=dict(state='succeeded' if job.get('exit_code') in (0, 130) else 'failed',
                                          exit_code=job.get('exit_code'))))
        return self._append_events(records)

    @staticmethod
    def event_code(body):
        """The outcome of one measurement, as a stable machine code."""
        error = body.get('error')
        if isinstance(error, dict):
            error = error.get('code') or error.get('stage')
        if error:
            return str(error)[:64]
        reliability = body.get('min_target_reliability')
        if isinstance(reliability, (int, float)) and not isinstance(reliability, bool):
            return 'OK' if reliability > 0 else 'E_STATE_MEASUREMENT_FAILED'
        return 'E_STATE_NO_OBSERVATION'

    def events(self, query):
        """New events after a cursor, in cursor order (CONTRACTS §5.7)."""
        self._load_events()
        stream = query.get('stream', [''])[0]
        cursor = str(query.get('after', ['0'])[0] or '0')
        try:
            limit = max(1, min(int(query.get('limit', ['200'])[0]), 1000))
        except ValueError:
            limit = 200
        after = 0
        if ':' in cursor:
            name, _, tail = cursor.rpartition(':')
            if tail.isdigit():
                stream, after = stream or name, int(tail)
            else:
                raise ValueError('Некорректный курсор событий.')
        elif cursor.isdigit():
            after = int(cursor)
        self.collect_events()
        found = []
        oldest = None
        try:
            with self.events_path.open('r', encoding='utf-8') as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if stream and event.get('stream') != stream:
                        continue
                    seq = event.get('seq')
                    if not isinstance(seq, int):
                        continue
                    if oldest is None or seq < oldest:
                        oldest = seq
                    if seq > after:
                        found.append(event)
        except OSError:
            return dict(events=[], cursor=cursor, stream=stream)
        found.sort(key=lambda item: (item.get('stream', ''), item.get('seq', 0)))
        truncated = found[:limit]
        last = truncated[-1] if truncated else None
        return dict(events=truncated, stream=stream,
                    cursor=(f'{last["stream"]}:{last["seq"]}' if last else cursor),
                    oldest_seq=oldest, source='measurements', truncated=len(found) > limit)

    def close(self):
        self.stop()
        process = self.process
        if process and hasattr(process, 'wait'):
            try:
                process.wait(timeout=30)
            except (subprocess.TimeoutExpired, OSError):
                if hasattr(process, 'terminate'):
                    with suppress(OSError):
                        process.terminate()
                try:
                    process.wait(timeout=10)
                except (subprocess.TimeoutExpired, OSError):
                    # A worker stuck in native I/O must not keep the GUI lock forever.
                    if hasattr(process, 'kill'):
                        with suppress(OSError):
                            process.kill()
                        with suppress(subprocess.TimeoutExpired, OSError):
                            process.wait(timeout=5)
        with suppress(OSError):
            (self.data/'gui-selection.json').unlink(missing_ok=True)
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
        # Language packs are public static strings (no user data) and are loaded
        # via a plain <script> tag, which cannot carry the session header.
        public = path.path in ('/', '/app.js', '/style.css', '/favicon.ico') or path.path.startswith('/i18n/')
        if not self.allowed(auth=not public):
            return
        try:
            if path.path == '/':
                content = (ROOT/'ui/index.html').read_text(encoding='utf-8').replace('__TOKEN__', self.app.token)
                content = content.replace('__PRODUCT_VERSION__', PRODUCT_VERSION)
                return self.respond(200, content.encode(), 'text/html; charset=utf-8')
            if path.path.startswith('/i18n/'):
                # Lazy-loaded UI language packs: ui/i18n/<code>.js.
                # The code is validated strictly, so no path parts can escape ui/i18n.
                code = path.path[len('/i18n/'):-3] if path.path.endswith('.js') else ''
                if not re.fullmatch(r'[a-z]{2,3}(-[A-Za-z]{2,4})?', code or ''):
                    return self.respond(404, dict(error='Нет такого языкового пакета.'))
                pack = ROOT/'ui'/'i18n'/(code + '.js')
                if not pack.is_file():
                    return self.respond(404, dict(error='Нет такого языкового пакета.'))
                return self.respond(200, pack.read_bytes(), 'text/javascript; charset=utf-8')
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
            query = parse_qs(path.query)
            if path.path == '/api/results':
                return self.respond(200, self.app.results(query))
            if path.path == '/api/results/matrix':
                return self.respond(200, self.app.matrix(query))
            if path.path == '/api/events':
                return self.respond(200, self.app.events(query))
            if path.path == '/api/annotations':
                return self.respond(200, dict(entries=self.app.annotation_state(),
                                              tags=self.app.tags_in_use()))
            if path.path == '/api/views':
                return self.respond(200, dict(views=self.app.saved_views()))
            if path.path == '/api/history':
                # The undo descriptor is internal: the page needs to know that
                # an action can be reversed, not how.
                entries = [{key: value for key, value in item.items() if key != 'undo'}
                           for item in (self.app.history.read().get('entries') or [])]
                return self.respond(200, dict(entries=entries[::-1][:20]))
            if path.path == '/api/result-detail':
                proxy = query.get('proxy', [''])[0]
                return self.respond(200, self.app.detail(proxy))
            if path.path == '/api/source-catalog':
                return self.respond(200, self.app.source_view(query))
            if path.path.startswith('/api/source-catalog/'):
                source_id = path.path[len('/api/source-catalog/'):]
                return self.respond(200, self.app.source_row(source_id))
            if path.path == '/api/sources/scope':
                return self.respond(200, self.app.scope_exclusions())
            if path.path == '/api/sources/update-status':
                return self.respond(200, self.app.catalog_update_status())
            if path.path.startswith('/api/download/'):
                name = path.path.rsplit('/', 1)[1]
                if name not in DOWNLOADS:
                    return self.respond(404, dict(error='Файл не найден.'))
                # Stream exports so a large JSON does not fill server memory.
                # Defect 5: no writer lock here.  The generation is resolved once
                # and the file is immutable, so a concurrent publication cannot
                # mix two snapshots and a running scan cannot block the download.
                snapshot = self.app.snapshot()
                status = self.app.export_status(snapshot)
                if not status or not status.get('published'):
                    return self.respond(409, dict(error='Нет опубликованного снимка экспорта.'))
                if status.get('state') == 'error':
                    return self.respond(409, dict(error='Снимок экспорта завершился ошибкой.'))
                empty_at_publish = (status.get('empty_export') is True and status.get('exported') == 0)
                if status.get('stale') and not (empty_at_publish and name in EMPTY_SAFE_DOWNLOADS):
                    return self.respond(410, dict(error='Снимок экспорта устарел; выполните перепроверку.'))
                try:
                    target = core.export_file(self.app.data/'exports', name, generation=snapshot.generation)
                    with target.open('rb') as handle:
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/octet-stream')
                        self.send_header('Content-Length', str(os.fstat(handle.fileno()).st_size))
                        self.send_header('Content-Disposition', f'attachment; filename="{name}"')
                        self.send_header('Cache-Control', 'no-store')
                        self.end_headers()
                        while chunk := handle.read(DOWNLOAD_CHUNK):
                            self.wfile.write(chunk)
                except OSError:
                    return self.respond(409, dict(error='Файл снимка недоступен; выполните проверку заново.'))
                return
            self.respond(404, dict(error='Не найдено.'))
        except ValueError as exc:
            # A rejected control is a message with an action in it, not a
            # generic "try again" (F25).
            message = str(exc) if str(exc) and not str(exc).startswith(('0x', 'not')) else \
                'Неверные параметры запроса.'
            self.respond(400, dict(error=message))
        except (OSError, sqlite3.Error):
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
            if path == '/api/settings/export':
                return self.respond(200, self.app.export_settings(payload))
            if path == '/api/start':
                return self.respond(200, self.app.start(payload))
            if path == '/api/sources/prune':
                return self.respond(200, self.app.prune_sources(payload))
            if path == '/api/sources/update':
                return self.respond(200, self.app.update_sources(payload))
            if path == '/api/sources/set':
                return self.respond(200, self.app.apply_set(payload))
            if path == '/api/sources/toggle':
                return self.respond(200, self.app.toggle_source(payload))
            if path == '/api/sources/select':
                return self.respond(200, self.app.select_source(payload))
            if path == '/api/sources/recover':
                return self.respond(200, self.app.recover_source(payload))
            if path == '/api/sources/add':
                return self.respond(200, self.app.add_source_url(payload))
            if path in ('/api/sources/preview', '/api/sources/check'):
                return self.respond(200, self.app.preview_source(payload))
            if path == '/api/sources/exclude-scope':
                return self.respond(200, self.app.exclude_source_scope(payload))
            if path == '/api/sources/scope/clear':
                return self.respond(200, self.app.clear_scope_exclusions(payload))
            if path == '/api/sources/refresh':
                return self.respond(200, self.app.refresh_catalog(payload))
            if path == '/api/geoip/update':
                return self.respond(200, self.app.update_geo())
            if path == '/api/clear-data':
                return self.respond(200, self.app.clear_data())
            if path == '/api/stop':
                return self.respond(200, self.app.stop())
            if path == '/api/test-proxy':
                return self.respond(200, self.app.test_proxy(payload))
            if path == '/api/denylist/add':
                return self.respond(200, self.app.add_denylist(payload))
            if path == '/api/results/bulk':
                return self.respond(200, self.app.bulk(payload))
            if path == '/api/views':
                return self.respond(200, self.app.store_view(payload))
            if path == '/api/views/delete':
                return self.respond(200, self.app.delete_view(payload))
            if path == '/api/history/undo':
                return self.respond(200, self.app.undo_history(payload))
            if path == '/api/gateway/start':
                return self.respond(200, self.app.start_gateway())
            if path == '/api/gateway/stop':
                return self.respond(200, self.app.stop_gateway())
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


def main(argv=None):
    utf8_output()
    parser = argparse.ArgumentParser(description=tr(f'Локальный интерфейс {PRODUCT_NAME}', f'{PRODUCT_NAME} local interface'))
    parser.add_argument('--version', action='version', version=f'{PRODUCT_NAME} {PRODUCT_VERSION}')
    parser.add_argument('--data', type=Path, default=paths.default_data())
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--api-port', type=int, default=api.DEFAULT_PORT,
                        help=tr('порт локального API для своих программ (только этот компьютер)', 'port of the local API for your programs (this computer only)'))
    parser.add_argument('--no-api', action='store_true', help=tr('не запускать локальное API', 'do not start the local API'))
    parser.add_argument('--gateway-port', type=int, default=gateway.DEFAULT_PORT,
                        help=tr('порт ротирующего прокси (только этот компьютер)', 'port of the rotating proxy (this computer only)'))
    parser.add_argument('--gateway-host', default=os.environ.get('PROXY_WORKBENCH_GATEWAY_HOST', '127.0.0.1'),
                        help=tr('адрес ротирующего прокси; по умолчанию только этот компьютер',
                                'rotating proxy bind address; this computer only by default'))
    parser.add_argument('--lan', action='store_true',
                        help=tr('открыть ротирующий прокси в локальной сети для телефона (явное действие)',
                                'expose the rotating proxy on the local network for a phone (explicit opt-in)'))
    parser.add_argument('--gateway-token', default=os.environ.get('PROXY_WORKBENCH_GATEWAY_TOKEN'),
                        help=tr('пароль ротирующего прокси; свой для каждого запуска, не пароль интерфейса',
                                'rotating proxy password; its own per run, never the interface password'))
    parser.add_argument('--no-gateway', action='store_true', help=tr('не запускать ротирующий прокси', 'do not start the rotating proxy'))
    args = parser.parse_args(argv)
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
                    print(tr(f'Приложение уже запущено: {url}', f'The application is already running: {url}'), flush=True)
                    if not args.no_browser:
                        webbrowser.open(url)
                    return
            except httpx.HTTPError:
                pass
        raise SystemExit('Не удалось открыть интерфейс: папка data или порт уже используются.')
    core.atomic(args.data/'gui-address.json', json.dumps(dict(port=server.server_port)))
    url = f'http://127.0.0.1:{server.server_port}/'
    print(tr(f'{PRODUCT_NAME} {PRODUCT_VERSION}: {url}\nНе закрывайте это окно, пока работает приложение. Ctrl+C — закрыть.', f'{PRODUCT_NAME} {PRODUCT_VERSION}: {url}\nKeep this window open while you use the application. Ctrl+C closes it.'), flush=True)
    api_server = None
    if not args.no_api:
        try:
            api_server = api.make_api_server(args.data, '127.0.0.1', args.api_port)
        except (OSError, ValueError):
            print(tr(f'Локальное API не запущено: порт {args.api_port} занят.', f'Local API not started: port {args.api_port} is busy.'), flush=True)
        else:
            server.app.api_url = f'http://127.0.0.1:{api_server.server_port}'
            threading.Thread(target=api_server.serve_forever, daemon=True).start()
            print(tr(f'API для своих программ: {server.app.api_url}/proxies', f'API for your programs: {server.app.api_url}/proxies'), flush=True)
    if not args.no_gateway:
        server.app.gateway_bind = dict(host=args.gateway_host, port=args.gateway_port)
        gateway_token = args.gateway_token
        if not gateway_token:
            # The gateway password is generated per GUI instance and is never
            # persisted in settings, files or logs.  It is a different identity
            # from the GUI session token, so a phone profile and a QR code can
            # never carry the secret that administers this interface.
            gateway_token = server.app.gateway_token
        if args.lan and api.is_loopback(args.gateway_host):
            # LAN mode is an explicit opt-in with a visible state (defect 18).
            print(tr('Внимание: ротирующий прокси открыт в локальной сети; пароль доступа есть в QR.',
                     'Warning: the rotating proxy is open on the local network; its password is in the QR.'), flush=True)
        try:
            server.app.gateway = gateway.Background(args.data, args.gateway_host, args.gateway_port, token=gateway_token)
        except (OSError, ValueError) as exc:
            print(tr(f'Ротирующий прокси не запущен: {exc}. Проверьте порт и адрес.',
                     f'Rotating proxy not started: {exc}. Check the port and bind address.'), flush=True)
        else:
            gateway_state = server.app.gateway_state()
            print(tr(f'Ротирующий прокси: {gateway_state["address"]} (HTTP и SOCKS5, только TCP)',
                     f'Rotating proxy: {gateway_state["address"]} (HTTP and SOCKS5, TCP only)'), flush=True)
            if gateway_state['mobile_ready']:
                print(tr('LAN-шлюз включён: QR содержит пароль; разрешите порт в брандмауэре.',
                         'LAN gateway enabled: the QR contains the password; allow the port in the firewall.'), flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if api_server:
            api_server.shutdown()
            api_server.server_close()
        if getattr(server.app, 'gateway', None):
            server.app.gateway.close()
        server.app.close()
        server.server_close()


if __name__ == '__main__':
    main()
