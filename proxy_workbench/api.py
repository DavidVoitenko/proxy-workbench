"""Read-only local HTTP API over the latest export, for scripts and other programs.

Started with ``proxytool.py serve``. It reads ``data/exports/ranked.json`` and
re-reads it whenever a run (including ``run --watch``) publishes a new export,
so it never locks the data folder and can run next to a scan.
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import random
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import anonymity
from . import formats
from . import geoip
from . import source_catalog
from . import source_management
from .branding import PRODUCT_NAME, PRODUCT_VERSION
from .i18n import tr
from .proxytool import (PROTOCOLS, export_file as proxytool_export_file,
                        export_manifest as proxytool_export_manifest, proxy_protocol, row_fresh, row_history)

DEFAULT_PORT = 8765
TOKEN_ENV = 'PROXY_WORKBENCH_API_TOKEN'
FORMATS = ('json', 'txt', 'hostport')
MAX_LIMIT = 1_000_000
ENDPOINTS = {
    '/proxies': 'working proxies, best first; filters: protocol, country, max_latency, min_mbps, anonymity, hosting, limit, format',
    '/random': 'random working proxies (limit, default 1); same filters',
    '/pac': 'proxy auto-config for browsers with the best matching proxies; same filters',
    '/clash': 'Clash / Mihomo config with the best matching proxies; same filters',
    '/singbox': 'sing-box config with the best matching proxies; same filters',
    '/status': 'summary of the latest export',
    '/sources': 'source catalog; filters: q, set, category, protocol, format, access, state, limit, offset',
    '/sources/{id}': 'one catalog source: evidence, rights, history and cache age',
    '/source-sets': 'source sets and how many of their members are selected',
}


def read_selection(data):
    """The user's stored selection, read only.  Never migrated or rewritten here."""
    try:
        value = json.loads((Path(data) / 'gui-settings.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    if isinstance(value.get('source_selection'), dict):
        return value['source_selection']
    try:
        return source_catalog.migrate_settings(value)['source_selection']
    except (ValueError, source_catalog.CatalogError, OSError):
        return {}


def read_catalog(data):
    """Last accepted catalog when the GUI stored one, otherwise the bundled file."""
    stored = Path(data) / 'source-catalog.json'
    if stored.is_file():
        try:
            return source_catalog.load_catalog(stored, allow_research=False, allow_unsafe=True)
        except (OSError, ValueError, source_catalog.CatalogError):
            pass
    return source_catalog.load_bundled()


class SourceReader:
    """Read-only catalog reader: a short WAL snapshot, no workbench lock, no writes."""

    def __init__(self, data):
        self.data = Path(data)
        self.path = self.data / 'proxies.sqlite3'

    def _connect(self):
        if not self.path.is_file():
            return None
        try:
            return sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=2)
        except Exception:
            return None

    def view(self, query):
        catalog = read_catalog(self.data)
        selection = read_selection(self.data)
        db = self._connect()
        try:
            runtime = source_management.runtime_snapshot(db)
            return source_management.build_view(catalog, selection, runtime, query, redact=True, db=db)
        finally:
            if db is not None:
                db.close()

    def detail(self, source_id):
        catalog = read_catalog(self.data)
        selection = read_selection(self.data)
        db = self._connect()
        try:
            runtime = source_management.runtime_snapshot(db)
            return source_management.detail_view(catalog, selection, source_id, runtime, db, redact=True)
        finally:
            if db is not None:
                db.close()


def is_loopback(host):
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host.strip('[]')).is_loopback
    except ValueError:
        return False


def public_row(row):
    """Stable, compact record of one exported proxy."""
    proxy = row['proxy']
    address = proxy.partition('://')[2]
    host, _, port = address.rpartition(':')
    history = row_history(row)
    return {
        'proxy': proxy,
        'protocol': proxy_protocol(proxy),
        'host': host.strip('[]'),
        'port': int(port),
        'country': row.get('country') or None,
        'anonymity': (row.get('anonymity') or {}).get('level'),
        'exit_ip': row.get('exit_ip') or None,
        'exit_country': row.get('exit_country') or None,
        'asn': row.get('asn') or None,
        'provider': row.get('provider') or None,
        'hosting': row.get('hosting'),
        'latency_ms': row.get('latency_ms'),
        'mbps': (row.get('speed') or {}).get('mbps'),
        'jitter_ms': row.get('jitter_ms'),
        'reliability': row.get('reliability'),
        'uptime': round(history['passes'] / history['checks'], 4) if history['checks'] else None,
        'checks': history['checks'],
        'score': row.get('score'),
        'checked_at': row.get('checked_at'),
        'valid_until': row.get('valid_until'),
        'stale': not row_fresh(row),
    }


class Exports:
    """One coherent current generation, reloaded when its pointer or files change."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.lock = threading.Lock()
        self.key = None
        self.revision = 0
        self.visible = None
        self.rows = []
        self.status = {}

    def _clear(self, key=None):
        self.key = key
        self.revision += 1
        self.visible = None
        self.rows = []
        self.status = {}

    def load(self):
        """Return only fresh rows; an expired row disappears without restarting API/gateway."""
        manifest = proxytool_export_manifest(self.directory)
        generation = manifest.get('generation') if manifest else None
        ranked_path = proxytool_export_file(self.directory, 'ranked.json', generation=generation)
        status_path = proxytool_export_file(self.directory, 'status.json', generation=generation)
        try:
            if generation and not status_path.is_file():
                raise OSError('snapshot status missing')
            ranked_stat = ranked_path.stat()
            status_stat = status_path.stat() if status_path.is_file() else None
        except OSError:
            with self.lock:
                self._clear((generation, 'missing'))
                return [], {}
        key = (generation or 'legacy', ranked_stat.st_mtime_ns, ranked_stat.st_size,
               (status_stat.st_mtime_ns, status_stat.st_size) if status_stat else None)
        now = time.time()
        with self.lock:
            if key != self.key:
                try:
                    rows = json.loads(ranked_path.read_text(encoding='utf-8'))
                    status = json.loads(status_path.read_text(encoding='utf-8')) if status_stat else {}
                    if not isinstance(rows, list) or not isinstance(status, dict):
                        raise ValueError('invalid export')
                    if generation and status.get('generation') != generation:
                        raise ValueError('mixed export generations')
                    self.rows = [public_row(row) for row in rows]
                    self.status = status
                    self.key = key
                except (OSError, ValueError, KeyError, TypeError):
                    # Legacy root files can be observed mid-write. A generation is
                    # immutable, so a broken current snapshot is safer to clear.
                    if generation or self.key is None:
                        self._clear(key)
                    return [], self.status
            rows = [dict(row, stale=not row_fresh(row, now)) for row in self.rows if row_fresh(row, now)]
            visible = (key, tuple(row['proxy'] for row in rows), tuple(row.get('valid_until') for row in rows))
            if visible != self.visible:
                self.visible = visible
                self.revision += 1
            status = dict(self.status)
            if status:
                valid_until = status.get('valid_until')
                try:
                    status['stale'] = valid_until is not None and float(valid_until) <= now
                except (TypeError, ValueError, OverflowError):
                    status['stale'] = True
                status.setdefault('state', 'complete' if status.get('complete') else 'partial')
                status.setdefault('stop_reason', 'complete' if status.get('state') == 'complete' else 'stopped')
                status.setdefault('generation', generation)
                if status.get('state') != 'complete' or status.get('checked') != status.get('scope_candidates'):
                    status['complete'] = False
            return rows, status



def parse_query(query):
    """Validated filters from a query string; raises ValueError with a message."""
    values = {key: items[-1] for key, items in parse_qs(query, keep_blank_values=True).items()}
    protocol = values.get('protocol', 'all') or 'all'
    if protocol == 'socks5h':
        protocol = 'socks5'
    if protocol not in PROTOCOLS:
        raise ValueError('protocol: all, http, https or socks5')
    countries = geoip.parse_countries(values.get('country', ''))
    minimum = values.get('anonymity', 'any') or 'any'
    if minimum not in anonymity.MIN_LEVELS:
        raise ValueError('anonymity: any, anonymous or elite')
    fmt = values.get('format', 'json') or 'json'
    if fmt not in FORMATS:
        raise ValueError('format: json, txt or hostport')
    try:
        max_latency = float(values.get('max_latency') or 0)
        min_mbps = float(values.get('min_mbps') or 0)
        limit = int(values.get('limit') or 0)
    except ValueError:
        raise ValueError('max_latency, min_mbps and limit must be numbers') from None
    if not 0 <= max_latency < float('inf') or not 0 <= min_mbps < float('inf') or not 0 <= limit <= MAX_LIMIT:
        raise ValueError('max_latency, min_mbps and limit must not be negative')
    hosting = values.get('hosting', '')
    if hosting not in ('', '0', '1'):
        raise ValueError('hosting: 0 (hide hosting providers) or 1 (only hosting providers)')
    return dict(protocol=protocol, countries=countries, anonymity=minimum, max_latency=max_latency,
                min_mbps=min_mbps, limit=limit, format=fmt, hosting=hosting)


def select(rows, query):
    selected = []
    rank = anonymity.LEVEL_RANK
    for row in rows:
        if query['protocol'] != 'all' and row['protocol'] != query['protocol']:
            continue
        if query['countries'] and row['country'] not in query['countries']:
            continue
        if query['max_latency'] and (row['latency_ms'] is None or row['latency_ms'] > query['max_latency']):
            continue
        if query['anonymity'] != 'any' and rank.get(row['anonymity'] or 'unknown', -1) < rank[query['anonymity']]:
            continue
        if query.get('min_mbps') and (row.get('mbps') or 0) < query['min_mbps']:
            continue
        if query.get('hosting') and bool(row.get('hosting')) != (query['hosting'] == '1'):
            continue
        selected.append(row)
    return selected


def make_api_server(data, host='127.0.0.1', port=DEFAULT_PORT, token=None):
    if not is_loopback(host) and not token:
        raise ValueError(tr(f'API на {host} доступно из сети: задайте токен через --api-token или {TOKEN_ENV}.',
                            f'the API on {host} is reachable from the network: set a token with --api-token or {TOKEN_ENV}'))
    exports = Exports(Path(data) / 'exports')
    sources = SourceReader(data)

    class Handler(BaseHTTPRequestHandler):
        server_version = f'{PRODUCT_NAME}/{PRODUCT_VERSION}'
        sys_version = ''

        def log_message(self, *args):
            pass

        def send(self, status, body, content_type='application/json; charset=utf-8'):
            data = body if isinstance(body, bytes) else body.encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(data)

        def send_json(self, status, value):
            self.send(status, json.dumps(value, ensure_ascii=False, indent=1) + '\n')

        def authorized(self, query):
            if not token:
                return True
            supplied = self.headers.get('Authorization', '').removeprefix('Bearer ').strip()
            supplied = supplied or (parse_qs(query).get('token') or [''])[-1]
            return hmac.compare_digest(supplied.encode(), token.encode())

        def trusted_host(self):
            # Blocks DNS rebinding when the API is only meant for this computer.
            if token or not is_loopback(host):
                return True
            name = self.headers.get('Host', '').rsplit(':', 1)[0].strip('[]').lower()
            return is_loopback(name)

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            url = urlsplit(self.path)
            if not self.trusted_host():
                return self.send_json(403, {'error': 'host not allowed'})
            if not self.authorized(url.query):
                return self.send_json(401, {'error': 'missing or wrong token'})
            if url.path == '/source-sets':
                view = sources.view({'limit': 1})
                return self.send_json(200, {'schema_version': view['schema_version'],
                                            'revision': view['revision'],
                                            'sets': view['sets']})
            if url.path == '/sources' or url.path.startswith('/sources/'):
                # Read-only by construction: no enable, no preview, no update.
                if url.path == '/sources':
                    try:
                        return self.send_json(200, sources.view(url.query))
                    except ValueError as exc:
                        return self.send_json(400, {'error': str(exc)})
                source_id = url.path[len('/sources/'):]
                if not source_id or '/' in source_id:
                    return self.send_json(404, {'error': 'not found', 'endpoints': ENDPOINTS})
                detail = sources.detail(source_id)
                if detail is None:
                    return self.send_json(404, {'error': 'no such source', 'endpoints': ENDPOINTS})
                return self.send_json(200, detail)
            rows, status = exports.load()
            if url.path in ('/', '/status'):
                return self.send_json(200, {
                    'service': PRODUCT_NAME, 'version': PRODUCT_VERSION, 'available': len(rows),
                    'schema_version': status.get('schema_version'), 'generation': status.get('generation'),
                    'profile': status.get('profile'), 'state': status.get('state'),
                    'stop_reason': status.get('stop_reason'), 'scope': status.get('scope'),
                    'scope_candidates': status.get('scope_candidates'), 'checked': status.get('checked'),
                    'pending': status.get('pending'), 'passed': status.get('passed'),
                    'candidates': status.get('candidates'), 'generated_at': status.get('generated_at'),
                    'valid_until': status.get('valid_until'), 'stale': status.get('stale', False),
                    'complete': status.get('complete'), 'sort': status.get('sort'),
                    'targets': status.get('targets', []), 'endpoints': ENDPOINTS})
            if url.path not in ('/proxies', '/random', '/pac', '/clash', '/singbox'):
                return self.send_json(404, {'error': 'not found', 'endpoints': ENDPOINTS})
            try:
                query = parse_query(url.query)
            except ValueError as exc:
                return self.send_json(400, {'error': str(exc)})
            selected = select(rows, query)
            if url.path == '/pac':
                return self.send(200, formats.pac(row['proxy'] for row in selected),
                                 'application/x-ns-proxy-autoconfig')
            if url.path == '/clash':
                return self.send(200, formats.clash(selected), 'text/yaml; charset=utf-8')
            if url.path == '/singbox':
                return self.send(200, formats.singbox(selected), 'application/json; charset=utf-8')
            if url.path == '/random':
                selected = random.sample(selected, min(len(selected), query['limit'] or 1))
            elif query['limit']:
                selected = selected[:query['limit']]
            if url.path == '/random' and not selected:
                return self.send_json(404, {'error': 'no proxy matches these filters', 'available': len(rows)})
            if query['format'] == 'txt':
                return self.send(200, ''.join(row['proxy'] + '\n' for row in selected), 'text/plain; charset=utf-8')
            if query['format'] == 'hostport':
                return self.send(200, ''.join(f"{row['proxy'].partition('://')[2]}\n" for row in selected),
                                 'text/plain; charset=utf-8')
            body = {'count': len(selected), 'generated_at': status.get('generated_at'), 'proxies': selected}
            if status:
                body.update(valid_until=status.get('valid_until'), stale=status.get('stale', False),
                            generation=status.get('generation'), state=status.get('state'))
            return self.send_json(200, body)

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server
