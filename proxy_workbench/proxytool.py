#!/usr/bin/env python3
"""Independent public proxy collector and resumable service benchmark."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
from contextlib import asynccontextmanager
import csv
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import random
import re
import shutil
import socket
import sqlite3
import ssl
import statistics
import sys
import tempfile
import time
from urllib.parse import urlsplit, urlunsplit, urljoin, parse_qsl, urlencode

import httpx

from .branding import DEFAULT_REQUEST_PROFILE, PRODUCT_NAME, PRODUCT_VERSION, REQUEST_PROFILES, SOURCES_URL, merge_headers, profile_digest, validate_profile
from .reputation import Denylist, make_policy, result_allowed, screen_proxy, verdict_blocks
from .maintenance import clear_runtime, exclusive_lock
from . import anonymity
from . import geoip
from . import socks4
from . import formats
from . import source_catalog
from . import source_adapters
from . import source_management
from .i18n import tr, utf8_output
from . import paths

ROOT = paths.PACKAGE
TLS = ssl.create_default_context()
SCHEMES = {'http', 'https', 'socks4', 'socks5', 'socks5h'}
SAFE_TARGET_HEADERS = {'accept', 'accept-encoding', 'accept-language', 'cache-control', 'pragma', 'user-agent', 'x-client-version', 'x-request-id'}

# Source fetching is deliberately bounded before a response is handed to a parser.
# These defaults are finite so a public list cannot consume unbounded memory or CPU.
DEFAULT_SOURCE_MAX_BYTES = 32 * 1024 * 1024
# A preview answers "is it reachable and what format is it", so it reads a
# bounded prefix of the body instead of the whole list.  Both callers (the GUI
# "Проверить" action and ``sources check``) go through :func:`preview_collect`
# and report the bound in the result, so a truncated preview never reads as a
# complete one.
# The budget is the collector's own byte budget: a list the collector reads must
# not become un-previewable because a check allows less, and the records already
# read are still counted.  Beyond it the preview keeps the prefix it has instead
# of discarding the whole body.
PREVIEW_MAX_BYTES = DEFAULT_SOURCE_MAX_BYTES
# A dense list hits the record cap long before the byte cap.
PREVIEW_MAX_CANDIDATES = 200_000
#: Adapter refusals that mean "the read hit a bound", not "the source is bad".
ADAPTER_LIMIT_CODES = frozenset((
    'SOURCE_RECORD_LIMIT', 'SOURCE_HTML_NODE_LIMIT', 'SOURCE_HTML_DEPTH', 'SOURCE_HTML_STRING_TOO_LARGE',
    'SOURCE_JSON_DEPTH', 'SOURCE_COLUMN_LIMIT', 'SOURCE_PAGE_LIMIT', 'SOURCE_DEADLINE',
))
DEFAULT_SOURCE_MAX_LINE_BYTES = 64 * 1024
DEFAULT_SOURCE_MAX_CANDIDATES = 500_000
DEFAULT_SOURCE_MAX_REDIRECTS = 5
MAX_SOURCE_BYTES = 512 * 1024 * 1024
MAX_SOURCE_LINE_BYTES = 16 * 1024 * 1024
MAX_SOURCE_CANDIDATES = 10_000_000
MAX_SOURCE_REDIRECTS = 20
MAX_SOURCE_PARALLELISM = 32
MAX_SOURCE_HOST_PARALLELISM = 4
DEFAULT_SOURCE_PARALLELISM = 8
DEFAULT_SOURCE_HOST_PARALLELISM = 1
DEFAULT_SOURCE_HOST_INTERVAL = 0.0
DEFAULT_SOURCE_DEADLINE = 300
MAX_SOURCE_DEADLINE = 3600
DEFAULT_SOURCE_PAGES = 500
MAX_SOURCE_PAGES = 5000
DEFAULT_CACHE_BYTES = 256 * 1024 * 1024
MAX_CACHE_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_SOURCE_CACHE_BYTES = 32 * 1024 * 1024
SOURCE_DB_SCHEMA_VERSION = 1
SOURCE_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
SOURCE_METADATA_HOSTS = frozenset({
    'metadata', 'metadata.google.internal', 'metadata.goog', 'metadata.azure.internal',
    'metadata.azure.com', 'metadata.oraclecloud.com', 'metadata.tencentyun.com',
    'metadata.hetzner.cloud', 'metadata.platformequinix.com', 'metadata.packet.net',
    'instance-data', 'instance-data.ec2.internal', 'host.docker.internal',
    'kubernetes.default.svc', '169.254.169.254', '168.63.129.16',
    '169.254.0.23', '169.254.42.42', '169.254.170.2', '100.100.100.200',
    '147.75.207.207',
})


class SourceFetchError(ValueError):
    """A source error that can be recorded without exposing response contents."""

    def __init__(self, code, *, retryable=False, retry_after=None):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after


class PinnedSourceTransport(httpx.AsyncBaseTransport):
    """Resolve once, then connect to the validated IP while preserving Host/SNI."""

    def __init__(self, address, hostname):
        self.address = address
        self.hostname = hostname
        self.transport = httpx.AsyncHTTPTransport(verify=TLS)

    async def handle_async_request(self, request):
        # httpx keys headers in lower case; dropping every Host variant avoids sending two.
        headers = {key: value for key, value in request.headers.items() if key.lower() != 'host'}
        host_header = f'[{self.hostname}]' if ':' in self.hostname else self.hostname
        port = request.url.port
        if port and port not in (80, 443):
            host_header += f':{port}'
        headers['Host'] = host_header
        url = request.url.copy_with(host=self.address)
        extensions = dict(request.extensions)
        if request.url.scheme == 'https':
            extensions['sni_hostname'] = self.hostname
        pinned = httpx.Request(request.method, url, headers=headers, stream=request.stream, extensions=extensions)
        return await self.transport.handle_async_request(pinned)

    async def aclose(self):
        await self.transport.aclose()


def _source_ip_literal(value):
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        try:
            return ipaddress.ip_address(socket.inet_aton(value))
        except (OSError, TypeError, ValueError, OverflowError):
            return None


def _parse_source_url(value):
    """Parse and syntactically validate an HTTP(S) source URL."""
    if not isinstance(value, str):
        raise ValueError('нужен HTTP/HTTPS URL')
    raw = value.strip()
    if not raw or '\\' in raw or any(ord(char) < 0x20 or ord(char) == 0x7f or char.isspace() for char in raw):
        raise ValueError('некорректный URL')
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname
        port = parsed.port
        username = parsed.username
        password = parsed.password
    except (TypeError, ValueError):
        raise ValueError('некорректный URL или порт') from None
    if parsed.scheme not in ('http', 'https') or not hostname or username is not None or password is not None:
        raise ValueError('нужен HTTP/HTTPS URL без логина и пароля')
    if '#' in raw or parsed.fragment:
        raise ValueError('fragment в URL источника запрещен')

    # urlsplit accepts an empty explicit port (for example ``host:``); reject it
    # instead of silently treating it as the scheme default.
    authority = parsed.netloc.rsplit('@', 1)[-1]
    if authority.startswith('['):
        closing = authority.find(']')
        suffix = authority[closing + 1:] if closing >= 0 else ''
        if closing < 0 or suffix == ':' or (suffix and not suffix.startswith(':')):
            raise ValueError('некорректный порт')
    elif authority.count(':') == 1 and authority.endswith(':'):
        raise ValueError('некорректный порт')

    if hostname.startswith('.') or '..' in hostname:
        raise ValueError('некорректный hostname')
    host = hostname[:-1].lower() if hostname.endswith('.') else hostname.lower()
    if not host or '%' in host:
        raise ValueError('некорректный hostname')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if any(char in host for char in '[]:'):
            raise ValueError('некорректный hostname') from None
        try:
            host = host.encode('idna').decode('ascii').lower()
        except (UnicodeError, ValueError):
            raise ValueError('некорректный hostname') from None
    if not host or len(host) > 253:
        raise ValueError('слишком длинный hostname')
    effective_port = port if port is not None else (443 if parsed.scheme == 'https' else 80)
    if not 1 <= effective_port <= 65535:
        raise ValueError('некорректный порт')
    return parsed, host, effective_port


def _is_blocked_source_ip(value):
    try:
        address = ipaddress.ip_address(str(value).split('%', 1)[0])
    except (TypeError, ValueError):
        return True
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return (address.is_loopback or address.is_private or address.is_link_local
            or address.is_reserved or address.is_unspecified or address.is_multicast
            or getattr(address, 'is_site_local', False) or not address.is_global)


def _is_blocked_source_hostname(host):
    host = host.rstrip('.').lower()
    return (host in SOURCE_METADATA_HOSTS or host == 'localhost' or host.endswith('.localhost')
            or host.endswith(('.local', '.internal', '.lan', '.home.arpa')))


async def _validate_source_destination(value, allow_private=False):
    """Validate a source URL and all addresses returned for its hostname."""
    try:
        parsed, host, port = _parse_source_url(value)
    except ValueError as exc:
        raise SourceFetchError('SOURCE_URL_INVALID') from exc
    if allow_private:
        return parsed, host, port, None
    if _is_blocked_source_hostname(host):
        raise SourceFetchError('SOURCE_PRIVATE_DESTINATION')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        alternate = _source_ip_literal(host)
        if alternate is not None:
            # Do not let alternate numeric IPv4 spellings diverge between the
            # validator and httpx/libc URL parsers.
            if _is_blocked_source_ip(alternate):
                raise SourceFetchError('SOURCE_PRIVATE_DESTINATION')
            raise SourceFetchError('SOURCE_URL_INVALID')
        address = None
    if address is not None:
        if _is_blocked_source_ip(address):
            raise SourceFetchError('SOURCE_PRIVATE_DESTINATION')
        return parsed, host, port, str(address)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, ValueError) as exc:
        raise SourceFetchError('SOURCE_DNS_ERROR', retryable=True) from exc
    addresses = [entry[4][0] for entry in infos if entry[4]]
    if not addresses or any(_is_blocked_source_ip(address) for address in addresses):
        raise SourceFetchError('SOURCE_PRIVATE_DESTINATION' if addresses else 'SOURCE_DNS_ERROR', retryable=not addresses)
    return parsed, host, port, str(addresses[0])


@asynccontextmanager
async def _source_stream(client, url, allow_private=False, headers=None):
    _, hostname, _, address = await _validate_source_destination(url, allow_private)
    if address is None:
        async with client.stream('GET', url, headers=headers) as response:
            yield response
        return
    transport = PinnedSourceTransport(address, hostname)
    pinned_client = httpx.AsyncClient(transport=transport, trust_env=False, verify=TLS,
                                       follow_redirects=False, timeout=15)
    try:
        async with pinned_client.stream('GET', url, headers=headers) as response:
            yield response
    finally:
        await pinned_client.aclose()


def _source_limit(value, name, *, minimum=1, maximum=None):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f'{name}: ожидается целое число от {minimum} до {maximum or "∞"}')
    return value


def _declared_response_length(response):
    try:
        value = response.headers.get('content-length')
    except AttributeError:
        return None
    if value is None:
        return None
    try:
        length = int(value)
    except (TypeError, ValueError):
        return None
    return length if length >= 0 else None


async def _read_bounded_body(response, budget, max_bytes, *, prefix=False):
    """Read at most ``max_bytes`` of the body into memory.

    ``prefix=True`` is the preview's mode: a body larger than the bound is cut
    at the bound and reported through ``budget['truncated']`` instead of being
    rejected, so a check still says what the part it read contains.  A collect
    keeps refusing the whole body — an unbounded read is exactly what the bound
    exists to prevent.
    """
    remaining = max_bytes - budget['used']
    declared = _declared_response_length(response)
    if declared is not None and declared > remaining:
        if not prefix:
            raise SourceFetchError('SOURCE_TOO_LARGE')
        budget['truncated'] = True
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(chunk) > remaining:
            if not prefix:
                raise SourceFetchError('SOURCE_TOO_LARGE')
            body.extend(chunk[:remaining])
            budget['used'] += remaining
            budget['truncated'] = True
            break
        body.extend(chunk)
        budget['used'] += len(chunk)
        remaining -= len(chunk)
    return bytes(body)


async def _read_bounded_lines(response, budget, max_bytes, max_line_bytes, on_line, *, prefix=False):
    remaining = max_bytes - budget['used']
    declared = _declared_response_length(response)
    if declared is not None and declared > remaining:
        if not prefix:
            raise SourceFetchError('SOURCE_TOO_LARGE')
        budget['truncated'] = True
    pending = bytearray()
    chunks = response.aiter_bytes()
    async for chunk in chunks:
        if len(chunk) > remaining:
            if not prefix:
                raise SourceFetchError('SOURCE_TOO_LARGE')
            pending.extend(chunk[:remaining])
            budget['used'] += remaining
            remaining = 0
            budget['truncated'] = True
        else:
            pending.extend(chunk)
            budget['used'] += len(chunk)
            remaining -= len(chunk)
        start = 0
        while True:
            newline = pending.find(b'\n', start)
            carriage_return = pending.find(b'\r', start)
            positions = [position for position in (newline, carriage_return) if position >= 0]
            if not positions:
                if len(pending) - start > max_line_bytes:
                    raise SourceFetchError('SOURCE_LINE_TOO_LARGE')
                break
            end = min(positions)
            delimiter_length = 1
            if pending[end:end + 1] == b'\r' and pending[end + 1:end + 2] == b'\n':
                delimiter_length = 2
            line = bytes(pending[start:end])
            start = end + delimiter_length
            if len(line) > max_line_bytes:
                raise SourceFetchError('SOURCE_LINE_TOO_LARGE')
            on_line(line)
        if start:
            del pending[:start]
        if budget.get('truncated'):
            break
    # A record the bound cut in half is not a record: it is dropped with the
    # rest of the unread list rather than reported as a malformed line.
    if pending and not budget.get('truncated'):
        if len(pending) > max_line_bytes:
            raise SourceFetchError('SOURCE_LINE_TOO_LARGE')
        on_line(bytes(pending))


def normalize(value):
    raw = value.strip()
    if not raw or raw.startswith('#') or any(c.isspace() for c in raw):
        return None
    try:
        p = urlsplit(raw if '://' in raw else 'http://' + raw)
        if p.scheme not in SCHEMES or p.username is not None or p.password is not None:
            return None
        if p.path not in ('', '/') or p.query or p.fragment or not p.port:
            return None
        ip = ipaddress.ip_address(p.hostname)
        if not ip.is_global or (p.scheme == 'socks4' and ip.version != 4):
            return None
        host = f'[{ip.compressed}]' if ip.version == 6 else ip.compressed
        # Explicit schemes are authoritative; bare HTTPS/CONNECT lists use http.
        scheme = p.scheme
        return f'{scheme}://{host}:{p.port}'
    except (ValueError, TypeError):
        return None


def public_url(value):
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except (TypeError, ValueError):
        return ''
    host = parsed.hostname or ''
    if ':' in host and not host.startswith('['):
        host = '[' + host + ']'
    authority = host + (f':{port}' if port else '')
    return urlunsplit((parsed.scheme, authority, '/', '', ''))


ATOMIC_REPLACE_ATTEMPTS = 40


def atomic(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(content, encoding='utf-8')
    # Windows refuses to replace a file another process has open for reading
    # (the GUI polls progress while the CLI writes it); such locks are brief.
    for attempt in range(ATOMIC_REPLACE_ATTEMPTS):
        try:
            temp.replace(path)
            return
        except PermissionError:
            if attempt == ATOMIC_REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(0.05)


EXPORT_GENERATION_RETENTION = 3
SNAPSHOT_SCHEMA_VERSION = 1
MIN_FRESHNESS_SECONDS = 2 * 60 * 60
PROTOCOL_EXPORTS = {'http': 'http.txt', 'https': 'https.txt', 'socks4': 'socks4.txt', 'socks5': 'socks5.txt'}
PROTOCOL_ALIASES = {'socks5h': 'socks5'}


def export_manifest(directory, pointer='current.json'):
    """Read one export pointer without ever accepting a path outside ``generations``."""
    try:
        manifest = json.loads((Path(directory)/pointer).read_text(encoding='utf-8'))
        generation = manifest.get('generation') if isinstance(manifest, dict) else None
        if (isinstance(generation, str) and generation not in ('', '.', '..')
                and '/' not in generation and '\\' not in generation):
            return manifest
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    return None


def current_generation_name(directory, pointer='current.json'):
    manifest = export_manifest(directory, pointer)
    return manifest.get('generation') if manifest else None


def prune_export_generations(directory, keep=EXPORT_GENERATION_RETENTION, current=None):
    root = Path(directory)/'generations'
    if not root.is_dir():
        return [], [], 0
    if current is None:
        current = current_generation_name(directory)
    entries = [path for path in root.iterdir() if path.is_dir() and path.name.startswith('.generation-')]
    entries.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    others = [path for path in entries if path.name != current]
    keep_others = max(0, keep - (1 if current else 0))
    removed, failed = [], []
    for path in others[keep_others:]:
        try:
            shutil.rmtree(path)
            removed.append(path.name)
        except OSError:
            failed.append(path.name)
    remaining = len(entries) - len(removed)
    return removed, failed, remaining


def export_file(directory, name, generation=None, pointer='current.json'):
    """Resolve a file from one export generation, with a legacy-root fallback.

    Once ``generations/`` exists, falling back to mutable root files after a
    broken/deleted pointer could mix two snapshots.  Legacy installations with
    no generation directory continue to work unchanged.
    """
    directory = Path(directory)
    try:
        name = os.fspath(name)
    except TypeError:
        return directory/'generations'/'.missing'/'invalid-export-name'
    if (not isinstance(name, str) or not name or name in ('.', '..')
            or '/' in name or '\\' in name or Path(name).name != name):
        return directory/'generations'/'.missing'/'invalid-export-name'
    if generation is None:
        generation = current_generation_name(directory, pointer)
    if generation:
        return directory/'generations'/generation/name
    if (directory/pointer).exists() or (directory/'generations').is_dir():
        return directory/'generations'/'.missing'/name
    return directory/name


def open_db(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute('PRAGMA journal_mode=WAL')
    db.executescript('''
        CREATE TABLE IF NOT EXISTS candidates(proxy TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS candidate_meta(proxy TEXT PRIMARY KEY, country TEXT);
        CREATE TABLE IF NOT EXISTS profiles(id TEXT PRIMARY KEY, config TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS candidate_seen(proxy TEXT NOT NULL, source TEXT NOT NULL,
            PRIMARY KEY(proxy, source)) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS candidate_seen_meta(
            proxy TEXT NOT NULL, source TEXT NOT NULL, first_seen_at REAL, last_seen_at REAL,
            first_observation_id INTEGER, last_observation_id INTEGER,
            legacy INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(proxy, source)) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS results(
            profile TEXT NOT NULL, proxy TEXT NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY(profile, proxy));
        CREATE TABLE IF NOT EXISTS result_run(
            profile TEXT NOT NULL, proxy TEXT NOT NULL, run_id TEXT NOT NULL,
            checked_at REAL NOT NULL, PRIMARY KEY(profile, proxy)) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS catalog_state(
            key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS source_observation(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL, source_id TEXT NOT NULL, endpoint_id TEXT NOT NULL,
            started_at REAL NOT NULL, ended_at REAL, http_state TEXT NOT NULL,
            parse_state TEXT NOT NULL, cache_state TEXT NOT NULL, outcome TEXT NOT NULL,
            status INTEGER, attempts INTEGER NOT NULL DEFAULT 0, pages INTEGER NOT NULL DEFAULT 0,
            bytes INTEGER NOT NULL DEFAULT 0, received INTEGER NOT NULL DEFAULT 0,
            recognized INTEGER NOT NULL DEFAULT 0, accepted INTEGER NOT NULL DEFAULT 0,
            rejected INTEGER NOT NULL DEFAULT 0, duplicate INTEGER NOT NULL DEFAULT 0,
            duplicates_existing INTEGER NOT NULL DEFAULT 0, blocked INTEGER NOT NULL DEFAULT 0,
            new_endpoints INTEGER NOT NULL DEFAULT 0, partial INTEGER NOT NULL DEFAULT 0,
            error TEXT, retryable INTEGER NOT NULL DEFAULT 0, body_sha256 TEXT,
            fallback_used INTEGER NOT NULL DEFAULT 0, profile_digest TEXT, retry_after REAL,
            UNIQUE(run_id, source_id, endpoint_id));
        CREATE TABLE IF NOT EXISTS source_generation(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id TEXT NOT NULL, observation_id INTEGER, state TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 0, last_good INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL, record_count INTEGER NOT NULL DEFAULT 0,
            estimated_bytes INTEGER NOT NULL DEFAULT 0, profile_digest TEXT,
            endpoint_url TEXT);
        CREATE TABLE IF NOT EXISTS source_generation_entry(
            generation_id INTEGER NOT NULL, proxy TEXT NOT NULL, metadata_json TEXT,
            PRIMARY KEY(generation_id, proxy)) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS source_state(
            source_id TEXT NOT NULL, endpoint_id TEXT NOT NULL,
            final_url TEXT, etag TEXT, last_modified TEXT,
            last_attempt_at REAL, last_success_at REAL, last_body_at REAL,
            last_304_at REAL, current_generation INTEGER, last_good_generation INTEGER,
            consecutive_failures INTEGER NOT NULL DEFAULT 0, backoff_until REAL,
            quarantine_until REAL, retry_after REAL, last_error TEXT,
            profile_digest TEXT, PRIMARY KEY(source_id, endpoint_id)) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS source_metadata(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proxy TEXT NOT NULL, source_id TEXT NOT NULL, generation_id INTEGER,
            country TEXT, asn INTEGER, asn_org TEXT, anonymity TEXT,
            source_last_checked REAL, claimed_exit_ip TEXT, supports_https INTEGER,
            extra_json TEXT, subject TEXT NOT NULL DEFAULT 'endpoint',
            origin TEXT NOT NULL DEFAULT 'source_claimed', declared_at REAL,
            ingested_at REAL NOT NULL, valid_until REAL,
            UNIQUE(proxy, source_id, generation_id, subject));
        CREATE TABLE IF NOT EXISTS source_identity(
            source_id TEXT PRIMARY KEY, family_id TEXT, publisher_id TEXT,
            metadata_json TEXT);
        CREATE TABLE IF NOT EXISTS source_scan_stat(
            app_run_id TEXT NOT NULL, profile_digest TEXT NOT NULL,
            scope_digest TEXT NOT NULL, source_id TEXT NOT NULL,
            checked_by_app INTEGER NOT NULL DEFAULT 0,
            passed_profile INTEGER NOT NULL DEFAULT 0,
            global_unique_checked INTEGER NOT NULL DEFAULT 0,
            global_unique_passed INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(app_run_id, profile_digest, scope_digest, source_id)) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS candidate_scope_exclusion(
            proxy TEXT NOT NULL, reason TEXT NOT NULL, source_id TEXT,
            scope_digest TEXT, created_at REAL NOT NULL, expires_at REAL,
            PRIMARY KEY(proxy, scope_digest)) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS source_db_meta(
            key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE VIEW IF NOT EXISTS candidate_seen_provenance AS
            SELECT c.proxy,c.source,m.first_seen_at,m.last_seen_at,m.first_observation_id,
                   m.last_observation_id,m.legacy
            FROM candidate_seen c LEFT JOIN candidate_seen_meta m
              ON m.proxy=c.proxy AND m.source=c.source;
    ''')
    # The two-column candidate_seen shape is a public compatibility contract:
    # older callers insert into it without naming columns.  Provenance dates
    # live in a sibling table instead of changing that insert contract.
    columns = {row[1] for row in db.execute('PRAGMA table_info(candidate_meta)')}
    if 'source' not in columns:
        db.execute('ALTER TABLE candidate_meta ADD COLUMN source TEXT')
    observation_columns = {row[1] for row in db.execute('PRAGMA table_info(source_observation)')}
    if 'retry_after' not in observation_columns:
        db.execute('ALTER TABLE source_observation ADD COLUMN retry_after REAL')
    exclusion_columns = {row[1] for row in db.execute('PRAGMA table_info(candidate_scope_exclusion)')}
    if 'expires_at' not in exclusion_columns:
        db.execute('ALTER TABLE candidate_scope_exclusion ADD COLUMN expires_at REAL')
    seen_columns = {row[1] for row in db.execute('PRAGMA table_info(candidate_seen)')}
    if len(seen_columns) > 2:
        db.execute('DROP VIEW IF EXISTS candidate_seen_provenance')
        extended = {'first_seen_at', 'last_seen_at', 'first_observation_id', 'last_observation_id', 'legacy'}
        if extended <= seen_columns:
            db.execute('''INSERT OR IGNORE INTO candidate_seen_meta(
                            proxy,source,first_seen_at,last_seen_at,first_observation_id,last_observation_id,legacy)
                          SELECT proxy,source,first_seen_at,last_seen_at,first_observation_id,last_observation_id,legacy
                          FROM candidate_seen''')
        db.executescript('''
            CREATE TABLE candidate_seen_compact(proxy TEXT NOT NULL, source TEXT NOT NULL,
                PRIMARY KEY(proxy, source)) WITHOUT ROWID;
            INSERT OR IGNORE INTO candidate_seen_compact(proxy,source)
                SELECT proxy,source FROM candidate_seen;
            DROP TABLE candidate_seen;
            ALTER TABLE candidate_seen_compact RENAME TO candidate_seen;
        ''')
    db.executescript('''
        CREATE TRIGGER IF NOT EXISTS candidate_seen_meta_insert AFTER INSERT ON candidate_seen
        BEGIN
            INSERT OR IGNORE INTO candidate_seen_meta(proxy,source,legacy) VALUES (NEW.proxy,NEW.source,1);
        END;
        CREATE TRIGGER IF NOT EXISTS candidate_seen_meta_delete AFTER DELETE ON candidate_seen
        BEGIN
            DELETE FROM candidate_seen_meta WHERE proxy=OLD.proxy AND source=OLD.source;
        END;
    ''')
    if len(seen_columns) > 2:
        db.executescript('''
            CREATE TRIGGER IF NOT EXISTS candidate_seen_meta_insert AFTER INSERT ON candidate_seen
            BEGIN
                INSERT OR IGNORE INTO candidate_seen_meta(proxy,source,legacy) VALUES (NEW.proxy,NEW.source,1);
            END;
            CREATE TRIGGER IF NOT EXISTS candidate_seen_meta_delete AFTER DELETE ON candidate_seen
            BEGIN
                DELETE FROM candidate_seen_meta WHERE proxy=OLD.proxy AND source=OLD.source;
            END;
        ''')
    if len(seen_columns) > 2:
        db.execute('''CREATE VIEW IF NOT EXISTS candidate_seen_provenance AS
                      SELECT c.proxy,c.source,m.first_seen_at,m.last_seen_at,m.first_observation_id,
                             m.last_observation_id,m.legacy
                      FROM candidate_seen c LEFT JOIN candidate_seen_meta m
                        ON m.proxy=c.proxy AND m.source=c.source''')
    try:
        db.execute('BEGIN')
        db.execute('''INSERT OR IGNORE INTO candidate_seen_meta(proxy,source,legacy)
                      SELECT proxy,source,1 FROM candidate_seen''')
        # candidate_meta.source was the pre-catalog first-source compatibility
        # field.  Preserve it as one legacy membership, without assigning a
        # fabricated observation timestamp.
        db.execute('''INSERT OR IGNORE INTO candidate_seen(proxy, source)
                      SELECT proxy, source FROM candidate_meta WHERE source IS NOT NULL''')
        db.execute('''INSERT OR IGNORE INTO candidate_seen_meta(proxy,source,legacy)
                      SELECT proxy,source,1 FROM candidate_seen''')
        db.execute('''INSERT INTO source_metadata(proxy,source_id,country,subject,origin,declared_at,ingested_at)
                      SELECT proxy,COALESCE(source,'legacy'),country,'endpoint',
                             CASE WHEN source IS NOT NULL THEN 'source_claimed' ELSE 'legacy_unattributed' END,
                             NULL,?
                      FROM candidate_meta WHERE country IS NOT NULL
                      AND NOT EXISTS (SELECT 1 FROM source_metadata m WHERE m.proxy=candidate_meta.proxy
                                      AND m.source_id=COALESCE(candidate_meta.source,'legacy') AND m.subject='endpoint'
                                      AND m.origin IN ('source_claimed','legacy_unattributed'))''', (time.time(),))
        db.execute("INSERT OR REPLACE INTO source_db_meta(key,value) VALUES ('schema_version',?)",
                   (str(SOURCE_DB_SCHEMA_VERSION),))
        db.commit()
    except Exception:
        db.rollback()
        db.close()
        raise
    return db


def catalog_state(db, key='current', default=None):
    row = db.execute('SELECT value FROM catalog_state WHERE key=?', (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row[0])
    except (TypeError, ValueError):
        return default


def store_catalog_state(db, key, value):
    db.execute('INSERT OR REPLACE INTO catalog_state(key,value,updated_at) VALUES (?,?,?)',
               (key, json.dumps(value, ensure_ascii=False, sort_keys=True), time.time()))
    db.commit()


def source_key(entry):
    """Stable short id of a sources.json entry, so reports never contain URLs."""
    return hashlib.sha256(str(entry).strip().encode()).hexdigest()[:16]


class Rate:
    def __init__(self, rate):
        self.interval = 1 / rate if rate else 0
        self.lock = asyncio.Lock()
        self.next = 0.0

    async def wait(self):
        if not self.interval:
            return
        async with self.lock:
            delay = self.next - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self.next = time.monotonic() + self.interval


SOURCE_KINDS = ('http', 'https', 'socks4', 'socks5', 'socks5h', 'auto', 'text', 'geonode', 'http-fields',
                'json-records', 'fields', 'page-json', 'html-table')
DETECT_PROTOCOLS = ('http', 'socks4', 'socks5')
# ip:port inside free text: "1.2.3.4:8080", "1.2.3.4 8080", CSV and HTML table cells.
LOOSE_ADDRESS = re.compile(r'(?<![\d.])(?:(https?|socks[45]h?)://)?(\d{1,3}(?:\.\d{1,3}){3})'
                           r'(?::|\s*(?:</t[dh]>\s*<t[dh][^>]*>|[\s,;|])\s*)(\d{2,5})(?!\d)', re.I)


def loose_addresses(line):
    """Every proxy-looking address in a line of arbitrary text."""
    return [f'{(scheme or "http").lower()}://{host}:{port}' for scheme, host, port in LOOSE_ADDRESS.findall(line)]


def source_spec(value):
    if not isinstance(value, str):
        raise ValueError('Источник должен быть строкой URL или «socks4 URL» / «socks5 URL» / «geonode URL» / «json-records URL».')
    parts = value.strip().split(None, 1)
    kind, url = (parts if len(parts) == 2 else ('http', parts[0] if parts else ''))
    if kind not in SOURCE_KINDS:
        raise ValueError('Неизвестный формат источника.')
    try:
        _parse_source_url(url)
    except ValueError as exc:
        raise ValueError(f'Источник: {exc}.') from None
    return kind, url


async def _collect_legacy(db, urls, inputs, timeout=60, on_progress=None, denylist=None,
                  allow_private_sources=False, detect_protocols=False,
                  max_source_bytes=DEFAULT_SOURCE_MAX_BYTES,
                  max_source_line_bytes=DEFAULT_SOURCE_MAX_LINE_BYTES,
                  max_source_candidates=DEFAULT_SOURCE_MAX_CANDIDATES,
                  max_source_redirects=DEFAULT_SOURCE_MAX_REDIRECTS, bounded_prefix=False):
    if not isinstance(allow_private_sources, bool):
        raise ValueError('allow_private_sources: ожидается bool')
    max_source_bytes = _source_limit(max_source_bytes, 'max_source_bytes', maximum=MAX_SOURCE_BYTES)
    max_source_line_bytes = _source_limit(max_source_line_bytes, 'max_source_line_bytes', maximum=MAX_SOURCE_LINE_BYTES)
    max_source_candidates = _source_limit(max_source_candidates, 'max_source_candidates', maximum=MAX_SOURCE_CANDIDATES)
    max_source_redirects = _source_limit(max_source_redirects, 'max_source_redirects', minimum=0, maximum=MAX_SOURCE_REDIRECTS)
    line_limit = min(max_source_line_bytes, max_source_bytes)

    reports = []
    total_rows = 0
    denylist = denylist or Denylist.empty()
    urls = list(dict.fromkeys(urls))
    specs = [source_spec(url) for url in urls]

    def publish():
        if on_progress:
            on_progress(dict(phase="collecting", sources_done=sum("source" in r for r in reports),
                             sources_total=len(urls), sources=reports, raw_rows=total_rows,
                             blocked=sum(r.get('blocked', 0) for r in reports),
                             candidates=db.execute("SELECT count(*) FROM candidates").fetchone()[0]))

    def add(value, protocol='http', country=None, source=None):
        value = value.strip()
        proxy = normalize(value if '://' in value else protocol+'://'+value)
        if not proxy:
            return 'invalid'
        if denylist.match(proxy):
            return 'blocked'
        db.execute('INSERT OR IGNORE INTO candidates VALUES (?)', (proxy,))
        if not (isinstance(country, str) and geoip.COUNTRY_CODE.fullmatch(country.upper())):
            country = None
        if source:
            # How many lists offer an address: rare ones are less crowded and tend to live longer.
            db.execute('INSERT OR IGNORE INTO candidate_seen VALUES (?, ?)', (proxy, source))
            db.execute('''INSERT INTO candidate_seen_meta(proxy,source,first_seen_at,last_seen_at,legacy)
                          VALUES (?, ?, ?, ?, 0)
                          ON CONFLICT(proxy,source) DO UPDATE SET
                            first_seen_at=COALESCE(candidate_seen_meta.first_seen_at,excluded.first_seen_at),
                            last_seen_at=excluded.last_seen_at,legacy=0''',
                       (proxy, source, time.time(), time.time()))
        if country or source:
            # The first source that delivered an address keeps the credit.
            db.execute('''INSERT INTO candidate_meta(proxy, country, source) VALUES (?, ?, ?)
                ON CONFLICT(proxy) DO UPDATE SET country=COALESCE(excluded.country, candidate_meta.country),
                source=COALESCE(candidate_meta.source, excluded.source)''',
                       (proxy, country and country.upper(), source))
        return 'accepted'

    def add_detected(value, source=None):
        """Unlabeled addresses are tried as every protocol; the checks show which one works."""
        value = value.strip()
        if '://' in value:
            return add(value, source=source)
        outcomes = [add(value, protocol, source=source) for protocol in DETECT_PROTOCOLS]
        return next((o for o in ('accepted', 'blocked') if o in outcomes), 'invalid')

    publish()
    for input_index, path in enumerate(inputs, 1):
        count = invalid = blocked = 0
        with Path(path).open(encoding='utf-8') as handle:
            for line in handle:
                if not line.strip() or line.lstrip().startswith('#'):
                    continue
                if len(line.encode('utf-8')) > line_limit:
                    raise SourceFetchError('SOURCE_LINE_TOO_LARGE')
                if count >= max_source_candidates:
                    raise SourceFetchError('SOURCE_CANDIDATE_LIMIT')
                count += 1
                outcome = add_detected(line, source='local') if detect_protocols else add(line, source='local')
                invalid += outcome == 'invalid'
                blocked += outcome == 'blocked'
        reports.append(dict(input=f'local-input-{input_index}', rows=count, invalid=invalid,
                            blocked=blocked, complete=True))
        total_rows += count
        db.commit()
    gate = asyncio.Semaphore(8)
    async with httpx.AsyncClient(trust_env=False, verify=TLS, follow_redirects=False,
                                 timeout=15) as client:
        async def fetch(index, kind, url):
            nonlocal total_rows
            key = source_key(urls[index - 1])
            _ensure_state(db, key, 'primary')
            saved_state = _state_row(db, key, 'primary')
            saved_state = dict(zip([column[0] for column in db.execute('SELECT * FROM source_state LIMIT 0').description], saved_state)) if saved_state else {}
            conditional = {}
            if (not saved_state.get('final_url') or saved_state.get('final_url') == url):
                if saved_state.get('etag'):
                    conditional['If-None-Match'] = saved_state['etag']
                if saved_state.get('last_modified'):
                    conditional['If-Modified-Since'] = saved_state['last_modified']
            count = invalid = blocked = pages = attempts = 0
            candidate_count = 0
            endpoint_count = 0
            budget = {'used': 0}
            error = None
            page = 1
            expected_total = None
            signatures = set()

            def consume_candidate():
                nonlocal candidate_count
                if candidate_count >= max_source_candidates:
                    raise SourceFetchError('SOURCE_CANDIDATE_LIMIT')
                candidate_count += 1

            def consume_line(raw):
                nonlocal count, invalid, blocked
                if kind == 'text':
                    # Web pages may use any encoding and are mostly markup: keep only addresses.
                    for address in loose_addresses(raw.decode('utf-8', errors='replace')):
                        consume_candidate()
                        count += 1
                        outcome = add(address, source=key)
                        invalid += outcome == 'invalid'
                        blocked += outcome == 'blocked'
                    return
                try:
                    line = raw.decode('utf-8')
                except UnicodeDecodeError as exc:
                    raise SourceFetchError('SOURCE_INVALID_UTF8') from exc
                if not line.strip() or line.lstrip().startswith('#'):
                    return
                consume_candidate()
                count += 1
                if kind == 'http-fields':
                    match = re.fullmatch(r"(\d{1,3}(?:\.\d{1,3}){3}:\d{1,5}):[A-Za-z][A-Za-z .'-]*", line.strip())
                    outcome = add(match[1], source=key) if match else 'invalid'
                elif kind == 'auto':
                    outcome = add_detected(line, source=key)
                else:
                    outcome = add(line, kind, source=key)
                invalid += outcome == 'invalid'
                blocked += outcome == 'blocked'

            def consume_record(record):
                nonlocal count, invalid, blocked, endpoint_count
                protocols = []
                if isinstance(record, dict) and isinstance(record.get('protocols', []), list):
                    protocols = [protocol for protocol in record['protocols']
                                 if protocol in ('http', 'https', 'socks4', 'socks5')]
                if endpoint_count + len(protocols) > max_source_candidates:
                    raise SourceFetchError('SOURCE_CANDIDATE_LIMIT')
                consume_candidate()
                count += 1
                accepted = False
                blocked_here = False
                if isinstance(record, dict):
                    host = str(record.get('ip', ''))
                    if ':' in host and not host.startswith('['):
                        host = '['+host+']'
                    for protocol in protocols:
                        # GeoNode https denotes CONNECT capability.
                        endpoint_count += 1
                        outcome = add(f"{host}:{record.get('port')}", 'http' if protocol == 'https' else protocol,
                                      record.get('country'), key)
                        accepted = accepted or outcome == 'accepted'
                        blocked_here = blocked_here or outcome == 'blocked'
                if not accepted:
                    blocked += blocked_here
                    invalid += not blocked_here

            not_modified = False
            last_etag = last_modified = final_url = url

            async def request_source(request_url, expected_page=None, send_conditional=False):
                nonlocal not_modified, last_etag, last_modified, final_url
                current_url = request_url
                for redirect_count in range(max_source_redirects + 1):
                    async with _source_stream(client, current_url, allow_private_sources,
                                              conditional if send_conditional and not redirect_count else None) as response:
                        status = response.status_code
                        if status in SOURCE_REDIRECT_STATUSES:
                            location = response.headers.get('location')
                            location = location.strip() if isinstance(location, str) else location
                            if not location:
                                raise SourceFetchError('SOURCE_REDIRECT_INVALID')
                            if redirect_count >= max_source_redirects:
                                raise SourceFetchError('SOURCE_REDIRECT_TOO_MANY')
                            try:
                                next_url = urljoin(current_url, location)
                                current_parsed, _, _ = _parse_source_url(current_url)
                                next_parsed, _, _ = _parse_source_url(next_url)
                            except (TypeError, ValueError) as exc:
                                raise SourceFetchError('SOURCE_URL_INVALID') from exc
                            if current_parsed.scheme == 'https' and next_parsed.scheme != 'https':
                                raise SourceFetchError('SOURCE_REDIRECT_DOWNGRADE')
                            current_url = next_url
                            continue
                        if status == 304:
                            not_modified = True
                            final_url = current_url
                            return None
                        if 300 <= status < 400:
                            raise SourceFetchError('SOURCE_REDIRECT_INVALID')
                        last_etag = response.headers.get('etag') or last_etag
                        last_modified = response.headers.get('last-modified') or last_modified
                        final_url = current_url
                        response.raise_for_status()
                        if kind == 'geonode':
                            body = await _read_bounded_body(response, budget, max_source_bytes,
                                                             prefix=bounded_prefix)
                            if budget.get('truncated'):
                                raise SourceFetchError('SOURCE_TRUNCATED')
                            try:
                                data = json.loads(body.decode('utf-8'))
                            except UnicodeDecodeError as exc:
                                raise SourceFetchError('SOURCE_INVALID_UTF8') from exc
                            except RecursionError as exc:
                                raise ValueError('Invalid JSON page') from exc
                            if not isinstance(data, dict) or not isinstance(data.get('data'), list):
                                raise ValueError('Invalid JSON page')
                            if int(data.get('page', expected_page)) != expected_page:
                                raise ValueError('Wrong page returned')
                            return data
                        if kind == 'text':
                            # Whole page at once: HTML tables often split host and port across lines.
                            consume_line(await _read_bounded_body(response, budget, max_source_bytes,
                                                                 prefix=bounded_prefix))
                            return None
                        await _read_bounded_lines(response, budget, max_source_bytes, line_limit, consume_line,
                                                  prefix=bounded_prefix)
                        return None
                raise SourceFetchError('SOURCE_REDIRECT_TOO_MANY')

            async with gate:
                while True:
                    data = None
                    succeeded = False
                    page_url = url
                    if kind == 'geonode':
                        parsed = urlsplit(url)
                        query = dict(parse_qsl(parsed.query))
                        query.update(page=str(page))
                        query.setdefault('limit', '500')
                        page_url = urlunsplit(parsed._replace(query=urlencode(query)))
                    for retry in range(2):
                        attempts += 1
                        try:
                            async with asyncio.timeout(timeout):
                                data = await request_source(page_url, page, send_conditional=(page == 1 and not not_modified))
                            succeeded = True
                            # The rows already recognised stay, but a read the
                            # check's own byte bound cut short is not a
                            # complete list and must not be reported as one.
                            error = 'SOURCE_TRUNCATED' if budget.get('truncated') else None
                            break
                        except asyncio.CancelledError:
                            raise
                        except (httpx.HTTPError, TimeoutError, OSError, ValueError, OverflowError) as exc:
                            error = exc.code if isinstance(exc, SourceFetchError) else type(exc).__name__
                            if isinstance(exc, SourceFetchError) and not exc.retryable:
                                break
                            if retry == 0:
                                await asyncio.sleep(1)
                    if not succeeded:
                        break
                    if not_modified:
                        error = None
                        # A 304 is not an empty answer: report the stored set and
                        # its age rather than counting zero rows for this run.
                        stored = _cache_entries(db, key)
                        if stored:
                            count = len(stored)
                            invalid = 0
                            pages = max(pages, 1)
                        db.execute('''UPDATE source_state SET last_304_at=?,last_attempt_at=?,
                                      consecutive_failures=0,backoff_until=NULL,quarantine_until=NULL,
                                      retry_after=NULL,last_error=NULL WHERE source_id=? AND endpoint_id=?''',
                                   (time.time(), time.time(), key, 'primary'))
                        break
                    pages += 1
                    if kind != 'geonode' or error == 'SOURCE_TRUNCATED':
                        break
                    batch = data['data']
                    try:
                        if expected_total is None:
                            expected_total = max(0, int(data['total']))
                        signature = hashlib.sha256(json.dumps(batch, sort_keys=True).encode()).hexdigest()
                        if batch and signature in signatures:
                            error = 'REPEATED_PAGE'
                            break
                        signatures.add(signature)
                        for record in batch:
                            consume_record(record)
                        if count >= expected_total:
                            break
                        if not batch:
                            error = 'INCOMPLETE_PAGINATION'
                            break
                    except SourceFetchError as exc:
                        error = exc.code
                        break
                    except (KeyError, ValueError, TypeError, OverflowError, RecursionError):
                        error = 'INVALID_PAGINATION'
                        break
                    page += 1
                    db.commit()
                    await asyncio.sleep(.1)
            total_rows += count
            truncated = error == 'SOURCE_TRUNCATED'
            if not not_modified:
                db.execute('''UPDATE source_state SET last_attempt_at=?,final_url=?,etag=COALESCE(?,etag),
                              last_modified=COALESCE(?,last_modified),last_error=?,last_success_at=?
                              WHERE source_id=? AND endpoint_id=?''',
                           (time.time(), final_url, last_etag, last_modified, error,
                            time.time() if error is None else None, key, 'primary'))
            reports.append(dict(source=index, rows=count, invalid=invalid, blocked=blocked, pages=pages,
                                attempts=attempts, complete=error is None, error=error, format=kind,
                                http_state='not_modified' if not_modified else
                                           ('http_2xx_nonempty' if error is None or truncated else 'http_error'),
                                final_url=final_url,
                                served_from_cache=bool(not_modified and stored),
                                cache_age_seconds=(_last_good_age(db, key) if not_modified else None)))
            db.commit()
            publish()
            print(tr(f'Источник {index}: строк {count}, заблокировано {blocked}, страниц {pages}, ошибка {error or "нет"}',
                     f'Source {index}: rows {count}, blocked {blocked}, pages {pages}, error {error or "none"}'), flush=True)
        async with asyncio.TaskGroup() as group:
            for index, (kind, url) in enumerate(specs, 1):
                group.create_task(fetch(index, kind, url))
    db.commit()
    publish()
    return dict(raw_rows=total_rows, unique=db.execute('SELECT count(*) FROM candidates').fetchone()[0],
                blocked=sum(r.get('blocked', 0) for r in reports), denylist_error=denylist.error,
                sources_total=len(specs), sources=reports)


def _catalog_cached():
    global _BUNDLED_CATALOG
    if _BUNDLED_CATALOG is None:
        _BUNDLED_CATALOG = source_catalog.load_bundled()
    return _BUNDLED_CATALOG


_BUNDLED_CATALOG = None


def resolve_collect_sources(args):
    """What ``collect`` and ``run`` actually fetch.

    An explicit ``--sources`` file stays authoritative.  Otherwise the user's
    own selection wins: whatever was chosen with ``sources set`` is what gets
    collected, with each source's own adapter and limits.  Only a user who has
    never chosen anything keeps the pre-catalog behaviour, so the app never
    changes what it downloads behind someone's back.
    """
    bundled = args.sources.resolve() == (ROOT / 'sources.json').resolve()
    if getattr(args, 'no_sources', False):
        return []
    settings = source_management.read_settings(args.data)
    selection = source_management.selection_of(settings or {})
    if bundled and selection.get('selected_ids'):
        return source_catalog.materialize_selection(settings)
    return resolve_sources_file(args.sources, bundled=bundled)


def resolve_sources_file(path, *, bundled=False):
    """Read the collector's source list while accepting both catalog shapes.

    An arbitrary legacy file remains an authoritative flat list.  The bundled
    object contributes only the 55 pre-catalog specs until a user explicitly
    materializes a new selection, so a catalog update never silently expands a
    user's scope.
    """
    path = Path(path)
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('Не удалось прочитать список источников.') from exc
    if isinstance(value, list):
        return value
    if not isinstance(value, dict):
        raise ValueError('sources: ожидается JSON-массив или каталог источников')
    catalog = source_catalog.validate_catalog(value, allow_research=True, allow_unsafe=True)
    selection = value.get('source_selection')
    if isinstance(selection, dict) and isinstance(selection.get('selected_ids'), list):
        return source_catalog.materialize_selection({'source_selection': selection}, catalog)
    if bundled:
        result = []
        for source in catalog['sources']:
            if source.get('relation_to_current') == 'already_used':
                result.extend(source.get('legacy_specs') or source.get('data_urls', []))
        return result
    return catalog['sources']


async def fetch_catalog(url, *, current=None, timeout=20, max_bytes=8 * 1024 * 1024,
                        allow_private_sources=False, etag=None, last_modified=None):
    """The single bounded owner for fetching a remote catalog.

    It reuses the same URL validation, DNS pinning, redirect and downgrade
    policy as proxy sources.  The body is decoded only by the pure catalog
    validator; no response data is imported or evaluated.
    """
    current_url = url
    try:
        allowed_catalog_host = urlsplit(url).hostname
    except ValueError:
        allowed_catalog_host = None
    validators = {}
    if etag:
        validators['If-None-Match'] = etag
    if last_modified:
        validators['If-Modified-Since'] = last_modified
    for redirect_count in range(DEFAULT_SOURCE_MAX_REDIRECTS + 1):
        try:
            if allowed_catalog_host and urlsplit(current_url).hostname != allowed_catalog_host:
                raise SourceFetchError('SOURCE_REDIRECT_HOST')
            async with httpx.AsyncClient(trust_env=False, verify=TLS, follow_redirects=False,
                                         timeout=timeout) as client:
                async with _source_stream(client, current_url, allow_private_sources,
                                          validators if not redirect_count else None) as response:
                    if response.status_code == 304:
                        return dict(state='not_modified', catalog=current, etag=etag, last_modified=last_modified)
                    if response.status_code in SOURCE_REDIRECT_STATUSES:
                        location = response.headers.get('location')
                        if not isinstance(location, str) or not location.strip():
                            raise SourceFetchError('SOURCE_REDIRECT_INVALID')
                        if redirect_count >= DEFAULT_SOURCE_MAX_REDIRECTS:
                            raise SourceFetchError('SOURCE_REDIRECT_TOO_MANY')
                        next_url = urljoin(current_url, location.strip())
                        old_parsed, _, _ = _parse_source_url(current_url)
                        new_parsed, _, _ = _parse_source_url(next_url)
                        if old_parsed.scheme == 'https' and new_parsed.scheme != 'https':
                            raise SourceFetchError('SOURCE_REDIRECT_DOWNGRADE')
                        current_url = next_url
                        continue
                    if response.status_code >= 400:
                        raise SourceFetchError('SOURCE_HTTP_ERROR', retryable=response.status_code >= 500)
                    body = await _read_bounded_body(response, {'used': 0}, max_bytes)
                    catalog = source_catalog.decode_catalog_bytes(body, current, max_bytes=max_bytes)
                    return dict(state='available', catalog=catalog,
                                etag=response.headers.get('etag'), last_modified=response.headers.get('last-modified'),
                                body_sha256=hashlib.sha256(body).hexdigest())
        except SourceFetchError:
            raise
    raise SourceFetchError('SOURCE_REDIRECT_TOO_MANY')


def _rich_plan(value):
    if isinstance(value, dict):
        if value.get('endpoints') and value.get('adapter'):
            raw = dict(value)
            if 'id' not in raw:
                raw['id'] = source_catalog.custom_id(raw.get('endpoints', [{}])[0].get('url', 'custom'))
            if raw.get('endpoints'):
                endpoint_urls = [endpoint.get('url') for endpoint in raw['endpoints']
                                 if isinstance(endpoint, dict) and endpoint.get('url')]
                if endpoint_urls:
                    raw['data_urls'] = endpoint_urls[:1]
                    raw['fallback_urls'] = endpoint_urls[1:]
            try:
                return source_catalog.normalize_source(raw)
            except source_catalog.CatalogError:
                # A custom descriptor may intentionally use a registered
                # profile without carrying research metadata.
                adapter = raw['adapter']
                return dict(raw, id=raw['id'], family_id=raw.get('family_id', raw['id']),
                            endpoints=raw['endpoints'], adapter=adapter)
        if value.get('id'):
            catalog = _catalog_cached()
            item = source_catalog.source_by_id(catalog, value['id'])
            if item is not None and not item.get('endpoints'):
                adapter = item.get('adapter') or {}
                kind = ((adapter.get('config') or {}).get('legacy_kind') or 'http')
                prefix = '' if kind in ('http', 'https') else kind + ' '
                item = source_catalog._custom_record(value['id'], prefix + item.get('url', ''))
                item['adapter'] = adapter
            if item is not None:
                return item
        raise ValueError('Некорректный план источника.')
    if not isinstance(value, str):
        raise ValueError('Источник должен быть строкой URL или формата.')
    text = value.strip()
    parts = text.split(None, 1)
    if len(parts) == 2 and parts[0] in ('json-records', 'fields', 'page-json', 'html-table'):
        kind, url = parts
        try:
            source_spec((kind + ' ' + url))
        except ValueError:
            raise
        return {'id': source_catalog.custom_id(text), 'name': source_catalog.custom_id(text),
                'publisher': {'id': 'local', 'name': 'Пользователь'}, 'family_id': source_catalog.custom_id(text),
                'category': 'custom', 'endpoints': [{'id': 'primary', 'url': url, 'role': 'primary', 'relation': 'custom'}],
                'data_urls': [url], 'fallback_urls': [], 'protocols': [], 'protocol_hints': [],
                'adapter': {'kind': kind, 'profile': 'generic-v1', 'config': {}}, 'access': {'kind': 'public'},
                'rights': {}, 'evidence': {}, 'limits': {}, 'collection_allowed': True,
                'legacy_specs': [text], 'publisher_id': 'local', 'publisher_name': 'Пользователь'}
    catalog = _catalog_cached()
    item = source_catalog.source_by_id(catalog, text)
    if item is not None:
        return item
    raise ValueError('Неизвестный формат источника или ID.')


def _rich_kind(value):
    if isinstance(value, str):
        parts = value.strip().split(None, 1)
        return parts[0] if len(parts) == 2 and parts[0] in source_adapters.ADAPTER_KINDS else None
    if isinstance(value, dict):
        adapter = value.get('adapter')
        if isinstance(adapter, dict):
            return adapter.get('kind')
        if isinstance(adapter, str):
            return adapter
    return None


def _parse_retry_after(value, now=None):
    if not isinstance(value, str) or not value.strip():
        return None
    now = time.time() if now is None else now
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        parsed = parsedate_to_datetime(value.strip())
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=__import__('datetime').timezone.utc)
        return max(0.0, parsed.timestamp() - now)
    except (TypeError, ValueError, OverflowError):
        return None


async def _sleep_value(value, sleep):
    result = sleep(value)
    if result is not None:
        await result


def _state_row(db, source_id, endpoint_id):
    return db.execute('SELECT * FROM source_state WHERE source_id=? AND endpoint_id=?',
                      (source_id, endpoint_id)).fetchone()


def _ensure_state(db, source_id, endpoint_id):
    db.execute('INSERT OR IGNORE INTO source_state(source_id,endpoint_id) VALUES (?,?)', (source_id, endpoint_id))


def _record_observation(db, run_id, source_id, endpoint_id, started, report, profile_digest=''):
    now = time.time()
    db.execute('''INSERT OR REPLACE INTO source_observation(
        run_id,source_id,endpoint_id,started_at,ended_at,http_state,parse_state,cache_state,
        outcome,status,attempts,pages,bytes,received,recognized,accepted,rejected,duplicate,
        duplicates_existing,blocked,new_endpoints,partial,error,retryable,body_sha256,
        fallback_used,profile_digest,retry_after)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (run_id, source_id, endpoint_id, started, now, report.get('http_state', 'not_attempted'),
         report.get('parse_state', 'not_run'), report.get('cache_state', 'none'), report.get('outcome', 'unavailable'),
         report.get('status'), report.get('attempts', 0), report.get('pages', 0), report.get('bytes', 0),
         report.get('received', 0), report.get('recognized', 0), report.get('accepted', 0),
         report.get('rejected', 0), report.get('duplicate', 0), report.get('duplicates_existing', 0),
         report.get('blocked', 0), report.get('new_endpoints', 0), int(bool(report.get('partial'))),
         report.get('error'), int(bool(report.get('retryable'))), report.get('body_sha256'),
         int(bool(report.get('fallback_used'))), profile_digest, report.get('retry_after')))
    return db.execute(
        'SELECT id FROM source_observation WHERE run_id=? AND source_id=? AND endpoint_id=?',
        (run_id, source_id, endpoint_id)).fetchone()[0]


def _update_observation(db, observation_id, report):
    db.execute('''UPDATE source_observation SET ended_at=?,http_state=?,parse_state=?,cache_state=?,outcome=?,
                  status=?,attempts=?,pages=?,bytes=?,received=?,recognized=?,accepted=?,rejected=?,duplicate=?,
                  duplicates_existing=?,blocked=?,new_endpoints=?,partial=?,error=?,retryable=?,body_sha256=?,
                  fallback_used=?,retry_after=? WHERE id=?''',
               (time.time(), report.get('http_state', 'not_attempted'), report.get('parse_state', 'not_run'),
                report.get('cache_state', 'none'), report.get('outcome', 'unavailable'), report.get('status'),
                report.get('attempts', 0), report.get('pages', 0), report.get('bytes', 0), report.get('received', 0),
                report.get('recognized', 0), report.get('accepted', 0), report.get('rejected', 0),
                report.get('duplicate', 0), report.get('duplicates_existing', 0), report.get('blocked', 0),
                report.get('new_endpoints', 0), int(bool(report.get('partial'))), report.get('error'),
                int(bool(report.get('retryable'))), report.get('body_sha256'), int(bool(report.get('fallback_used'))),
                report.get('retry_after'), observation_id))


def _last_good_age(db, source_id, now=None):
    row = db.execute('SELECT last_success_at FROM source_state WHERE source_id=? AND last_success_at IS NOT NULL ORDER BY last_success_at DESC LIMIT 1', (source_id,)).fetchone()
    if not row or not row[0]:
        return None
    return max(0, (time.time() if now is None else now) - row[0])


def _cache_entries(db, source_id):
    state = db.execute('''SELECT last_good_generation FROM source_state
                          WHERE source_id=? AND last_good_generation IS NOT NULL
                          ORDER BY last_good_generation DESC LIMIT 1''', (source_id,)).fetchone()
    if not state or not state[0]:
        return []
    return [row[0] for row in db.execute('SELECT proxy FROM source_generation_entry WHERE generation_id=?', (state[0],))]


def _source_identity(db, source):
    publisher = source.get('publisher') if isinstance(source.get('publisher'), dict) else {}
    db.execute('''INSERT OR REPLACE INTO source_identity(source_id,family_id,publisher_id,metadata_json)
                  VALUES (?,?,?,?)''',
               (source.get('id', ''), source.get('family_id'), publisher.get('id'),
                json.dumps({'name': publisher.get('name')}, ensure_ascii=False)))


def _add_rich_record(db, record, source_id, observation_id, generation_id, denylist, counters):
    values = record.get('values') or [record.get('value', '')]
    accepted = 0
    for value in values:
        counters['endpoint_attempts'] += 1
        proxy = normalize(value)
        if not proxy:
            counters['rejected'] += 1
            counters.setdefault('reject_reasons', {})['not_a_public_proxy'] = \
                counters.setdefault('reject_reasons', {}).get('not_a_public_proxy', 0) + 1
            continue
        if denylist.match(proxy):
            counters['blocked'] += 1
            counters.setdefault('reject_reasons', {})['denylisted'] = \
                counters.setdefault('reject_reasons', {}).get('denylisted', 0) + 1
            continue
        existed = db.execute('SELECT 1 FROM candidates WHERE proxy=?', (proxy,)).fetchone() is not None
        membership_existed = db.execute('SELECT 1 FROM candidate_seen WHERE proxy=? AND source=?', (proxy, source_id)).fetchone() is not None
        seen_here = proxy in counters.setdefault('seen_proxies', set())
        if seen_here:
            counters['duplicate'] = counters.get('duplicate', 0) + 1
        elif membership_existed:
            counters['duplicates_existing'] = counters.get('duplicates_existing', 0) + 1
        db.execute('INSERT OR IGNORE INTO candidates VALUES (?)', (proxy,))
        if not existed:
            counters['new_endpoints'] = counters.get('new_endpoints', 0) + 1
        counters.setdefault('seen_proxies', set()).add(proxy)
        db.execute('INSERT OR IGNORE INTO candidate_seen VALUES (?, ?)', (proxy, source_id))
        db.execute('''INSERT INTO candidate_seen_meta(proxy,source,first_seen_at,last_seen_at,
                      first_observation_id,last_observation_id,legacy)
                      VALUES (?,?,?,?,?,?,0)
                      ON CONFLICT(proxy,source) DO UPDATE SET
                        first_seen_at=COALESCE(candidate_seen_meta.first_seen_at,excluded.first_seen_at),
                        last_seen_at=excluded.last_seen_at,last_observation_id=excluded.last_observation_id,legacy=0''',
                   (proxy, source_id, time.time(), time.time(), observation_id, observation_id))
        declared = record.get('declared') if isinstance(record.get('declared'), dict) else {}
        country = declared.get('country') or declared.get('country_code')
        if isinstance(country, str) and geoip.COUNTRY_CODE.fullmatch(country.upper()):
            country = country.upper()
        else:
            country = None
        asn = declared.get('asn')
        try:
            asn = int(str(asn).upper().removeprefix('AS')) if asn is not None else None
        except (TypeError, ValueError):
            asn = None
        extra = {key: value for key, value in declared.items()
                 if key not in ('country', 'country_code', 'asn', 'asn_org', 'anonymity', 'last_checked', 'exit_ip', 'supports_https')}
        claimed_checked = source_adapters.normalize_timestamp(declared.get('last_checked'))
        if declared.get('last_checked') is not None and claimed_checked is None:
            extra['last_checked_raw'] = str(declared['last_checked'])[:256]
        try:
            extra_json = json.dumps(extra, ensure_ascii=False, separators=(',', ':')) if extra else None
        except (TypeError, ValueError):
            extra_json = None
        db.execute('''INSERT OR REPLACE INTO source_metadata(
            proxy,source_id,generation_id,country,asn,asn_org,anonymity,source_last_checked,
            claimed_exit_ip,supports_https,extra_json,subject,origin,declared_at,ingested_at,valid_until)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                   (proxy, source_id, generation_id, country, asn, declared.get('asn_org'),
                    declared.get('anonymity'), claimed_checked, declared.get('exit_ip'),
                    int(bool(declared.get('supports_https'))) if declared.get('supports_https') is not None else None,
                    extra_json, 'endpoint', 'source_claimed', time.time(), time.time(), None))
        db.execute('''INSERT OR IGNORE INTO candidate_meta(proxy,country,source) VALUES (?,?,?)
                      ON CONFLICT(proxy) DO UPDATE SET country=COALESCE(excluded.country,candidate_meta.country),
                      source=COALESCE(candidate_meta.source,excluded.source)''',
                   (proxy, country, source_id))
        accepted += 1
        counters['accepted'] += 1
        if generation_id:
            db.execute('INSERT OR IGNORE INTO source_generation_entry(generation_id,proxy,metadata_json) VALUES (?,?,?)',
                       (generation_id, proxy, extra_json))
    return accepted


def _cache_generation(db, source_id, observation_id, report, records, state, *, complete, now=None, cache_bytes=DEFAULT_CACHE_BYTES):
    if not records and complete:
        return None
    estimated = int(report.get('bytes', 0) or 0)
    active_bytes = db.execute('SELECT COALESCE(sum(estimated_bytes),0) FROM source_generation WHERE active=1 OR last_good=1').fetchone()[0]
    if estimated > DEFAULT_SOURCE_CACHE_BYTES or int(active_bytes) + estimated > cache_bytes:
        report['cache_state'] = 'cache_evicted'
        return None
    # Keep active and one rollback generation per source.  Eviction is
    # explicit in the observation and never removes the active last-good row.
    db.execute('''INSERT INTO source_generation(source_id,observation_id,state,active,last_good,
                    created_at,record_count,estimated_bytes,profile_digest,endpoint_url)
                  VALUES (?,?,?,?,?,?,?,?,?,?)''',
               (source_id, observation_id, 'complete' if complete else 'partial', 0, int(complete),
                time.time() if now is None else now, len(records), estimated,
                report.get('profile_digest', ''), report.get('final_url', '')))
    generation = db.execute('SELECT last_insert_rowid()').fetchone()[0]
    for record in records:
        for value in record.get('values') or [record.get('value', '')]:
            proxy = normalize(value)
            if proxy:
                db.execute('INSERT OR IGNORE INTO source_generation_entry(generation_id,proxy,metadata_json) VALUES (?,?,?)',
                           (generation, proxy, json.dumps(record.get('declared', {}), ensure_ascii=False)))
    if complete:
        db.execute('UPDATE source_generation SET active=1,last_good=1 WHERE id=?', (generation,))
        db.execute('UPDATE source_generation SET active=0,last_good=0 WHERE source_id=? AND id!=?', (source_id, generation))
        for old in db.execute('SELECT id FROM source_generation WHERE source_id=? AND id!=? ORDER BY id DESC LIMIT 20', (source_id, generation)).fetchall():
            db.execute('UPDATE source_generation SET active=0,last_good=0 WHERE id=?', (old[0],))
        stale = db.execute('''SELECT id FROM source_generation WHERE source_id=? AND active=0 AND last_good=0
                              AND id NOT IN (SELECT current_generation FROM source_state WHERE current_generation IS NOT NULL
                                             UNION SELECT last_good_generation FROM source_state WHERE last_good_generation IS NOT NULL)
                              ORDER BY id DESC LIMIT -1 OFFSET 1''', (source_id,)).fetchall()
        for old in stale:
            db.execute('DELETE FROM source_generation_entry WHERE generation_id=?', (old[0],))
            db.execute('DELETE FROM source_metadata WHERE generation_id=?', (old[0],))
            db.execute('DELETE FROM source_generation WHERE id=?', (old[0],))
        db.execute('''UPDATE source_state SET current_generation=?,last_good_generation=?,
                      last_success_at=?,consecutive_failures=0,backoff_until=NULL,
                      quarantine_until=NULL,retry_after=NULL,last_error=NULL
                      WHERE source_id=? AND endpoint_id=?''',
                   (generation, generation, time.time(), source_id, state.get('endpoint_id', 'primary')))
    else:
        db.execute('''UPDATE source_state SET current_generation=COALESCE(current_generation,?)
                      WHERE source_id=? AND endpoint_id=?''',
                   (generation, source_id, state.get('endpoint_id', 'primary')))
    return generation


async def _collect_rich_sources(db, plans, inputs, timeout, on_progress, denylist, *,
                                 allow_private_sources, max_source_bytes, max_source_candidates,
                                 max_source_redirects, detect_protocols=False, preview=False,
                                 bounded_prefix=False,
                                 parallelism=DEFAULT_SOURCE_PARALLELISM,
                                 host_parallelism=DEFAULT_SOURCE_HOST_PARALLELISM,
                                 host_interval=DEFAULT_SOURCE_HOST_INTERVAL,
                                 deadline=DEFAULT_SOURCE_DEADLINE, cache_bytes=DEFAULT_CACHE_BYTES,
                                 max_pages=DEFAULT_SOURCE_PAGES,
                                 clock=None, sleep=None, run_id=None):
    clock = clock or time.time
    sleep = sleep or asyncio.sleep
    parallelism = _source_limit(parallelism, 'parallelism', maximum=MAX_SOURCE_PARALLELISM)
    host_parallelism = _source_limit(host_parallelism, 'host_parallelism', maximum=MAX_SOURCE_HOST_PARALLELISM)
    if (isinstance(host_interval, bool) or not isinstance(host_interval, (int, float))
            or not math.isfinite(host_interval) or host_interval < 0 or host_interval > 3600):
        raise ValueError('host_interval: ожидается число от 0 до 3600 секунд')
    if (isinstance(deadline, bool) or not isinstance(deadline, (int, float))
            or not math.isfinite(deadline) or deadline <= 0 or deadline > MAX_SOURCE_DEADLINE):
        raise ValueError('deadline: ожидается число от 0 до 3600 секунд')
    cache_bytes = _source_limit(cache_bytes, 'cache_bytes', maximum=MAX_CACHE_BYTES)
    max_pages = _source_limit(max_pages, 'max_pages', maximum=MAX_SOURCE_PAGES)
    run_id = run_id or hashlib.sha256(f'{clock()}:{id(db)}'.encode()).hexdigest()[:20]
    bundled = _catalog_cached()
    store_catalog_state(db, 'bundled', dict(revision=bundled['revision'], digest=source_catalog.catalog_digest(bundled),
                                           source_count=len(bundled['sources'])))
    denylist = denylist or Denylist.empty()
    reports = []
    total_rows = 0
    total_gate = asyncio.Semaphore(parallelism)
    host_gates = {}
    host_times = {}
    identity = {}
    for plan in plans:
        _source_identity(db, plan)
        identity[plan['id']] = plan
        for spec in plan.get('legacy_specs', []):
            old_id = source_key(spec)
            if old_id == plan['id']:
                continue
            db.execute('INSERT OR REPLACE INTO source_identity(source_id,family_id,publisher_id) VALUES (?,?,?)',
                       (old_id, plan.get('family_id'), (plan.get('publisher') or {}).get('id')))
            old_rows = db.execute('SELECT proxy,first_seen_at,last_seen_at,first_observation_id,last_observation_id,legacy '
                                 'FROM candidate_seen_meta WHERE source=?', (old_id,)).fetchall()
            for proxy, first_seen, last_seen, first_obs, last_obs, legacy in old_rows:
                db.execute('INSERT OR IGNORE INTO candidate_seen VALUES (?,?)', (proxy, plan['id']))
                db.execute('''INSERT OR IGNORE INTO candidate_seen_meta(proxy,source,first_seen_at,last_seen_at,
                              first_observation_id,last_observation_id,legacy) VALUES (?,?,?,?,?,?,?)''',
                           (proxy, plan['id'], first_seen, last_seen, first_obs, last_obs, legacy))
                db.execute('''UPDATE candidate_meta SET source=COALESCE(source,?) WHERE proxy=?''', (plan['id'], proxy))

    def publish():
        if on_progress:
            on_progress(dict(phase='collecting', sources_done=len(reports), sources_total=len(plans),
                             sources=reports, raw_rows=sum(item.get('rows', 0) for item in reports),
                             candidates=db.execute('SELECT count(*) FROM candidates').fetchone()[0]))

    def state_for(source_id, endpoint_id):
        _ensure_state(db, source_id, endpoint_id)
        row = _state_row(db, source_id, endpoint_id)
        return dict(zip([description[0] for description in db.execute('SELECT * FROM source_state LIMIT 0').description], row)) if row else {}

    def cached_add(source_id):
        values = _cache_entries(db, source_id)
        for proxy in values:
            db.execute('INSERT OR IGNORE INTO candidates VALUES (?)', (proxy,))
        return len(values)

    async def request_body(client, url, headers, budget, *, allowed_host=None, allowed_path_prefix=None):
        current_url = url
        sent_validators = False
        for redirect_count in range(max_source_redirects + 1):
            if allowed_host or allowed_path_prefix:
                parsed_current = urlsplit(current_url)
                if allowed_host and parsed_current.hostname != allowed_host:
                    raise SourceFetchError('SOURCE_REDIRECT_HOST')
                if allowed_path_prefix and not parsed_current.path.startswith(allowed_path_prefix):
                    raise SourceFetchError('SOURCE_REDIRECT_PATH')
            async with _source_stream(client, current_url, allow_private_sources,
                                      headers if not sent_validators else None) as response:
                status = response.status_code
                if status in SOURCE_REDIRECT_STATUSES:
                    location = response.headers.get('location')
                    if not isinstance(location, str) or not location.strip():
                        raise SourceFetchError('SOURCE_REDIRECT_INVALID')
                    if redirect_count >= max_source_redirects:
                        raise SourceFetchError('SOURCE_REDIRECT_TOO_MANY')
                    next_url = urljoin(current_url, location.strip())
                    current_parsed, _, _ = _parse_source_url(current_url)
                    next_parsed, _, _ = _parse_source_url(next_url)
                    if current_parsed.scheme == 'https' and next_parsed.scheme != 'https':
                        raise SourceFetchError('SOURCE_REDIRECT_DOWNGRADE')
                    current_url = next_url
                    sent_validators = False
                    continue
                if status == 304:
                    return response, b'', current_url, True
                if 300 <= status < 400:
                    raise SourceFetchError('SOURCE_REDIRECT_INVALID')
                if status == 429:
                    retry = _parse_retry_after(response.headers.get('retry-after'), clock())
                    raise SourceFetchError('SOURCE_RATE_LIMITED', retryable=True, retry_after=retry) from None
                if status >= 400:
                    raise SourceFetchError('SOURCE_HTTP_ERROR', retryable=status in (408, 425, 500, 502, 503, 504))
                body = await _read_bounded_body(response, budget, max_source_bytes, prefix=bounded_prefix)
                return response, body, current_url, False
        raise SourceFetchError('SOURCE_REDIRECT_TOO_MANY')

    async def fetch(index, plan):
        nonlocal total_rows
        source_id = plan['id']
        adapter = plan.get('adapter') or {}
        kind = adapter.get('kind')
        if kind == 'unsupported' or not plan.get('collection_allowed', True):
            report = dict(source=index, source_id=source_id, rows=0, invalid=0, blocked=0, pages=0,
                          attempts=0, complete=False, error='SOURCE_NOT_ELIGIBLE', format=kind or 'unsupported',
                          http_state='not_attempted', parse_state='not_run', cache_state='none', outcome='unavailable',
                          reject_reasons={})
            reports.append(report)
            db.commit(); publish()
            return
        endpoints = plan.get('endpoints') or [{'id': 'primary', 'url': plan.get('data_urls', [''])[0]}]
        endpoint_reports = []
        source_deadline_at = clock() + deadline
        for endpoint_index, endpoint in enumerate(endpoints):
            endpoint_id = str(endpoint.get('id') or ('primary' if endpoint_index == 0 else f'fallback-{endpoint_index}'))
            url = endpoint.get('url')
            report = dict(source=index, source_id=source_id, endpoint=endpoint_id, rows=0, invalid=0, blocked=0,
                          pages=0, attempts=0, complete=False, error=None, format=kind,
                          http_state='not_attempted', parse_state='not_run', cache_state='none', outcome='unavailable',
                          received=0, recognized=0, accepted=0, rejected=0, duplicate=0,
                          duplicates_existing=0, new_endpoints=0, partial=False, retryable=False,
                          fallback_used=endpoint_index > 0, final_url=url, bytes=0, status=None,
                          retry_after=None, reject_reasons={},
                          profile_digest=hashlib.sha256(json.dumps(adapter, sort_keys=True).encode()).hexdigest()[:20])
            report['_recorded'] = False
            started = clock()
            deadline_at = source_deadline_at
            budget = {'used': 0}
            truncated = False
            state = state_for(source_id, endpoint_id)
            now = clock()
            retry_after = _parse_retry_after(state.get('retry_after'), now)
            if state.get('quarantine_until') and state['quarantine_until'] > now:
                report.update(error='SOURCE_QUARANTINED', http_state='not_attempted', cache_state='stale_last_good' if _cache_entries(db, source_id) else 'none')
                if report['cache_state'] != 'none':
                    report['rows'] = cached_add(source_id)
                    report['cache_state'] = 'stale_last_good'
                _record_observation(db, run_id, source_id, endpoint_id, started, report, report['profile_digest'])
                report.pop('_recorded', None)
                endpoint_reports.append(report)
                continue
            if state.get('backoff_until') and state['backoff_until'] > now:
                report.update(error='SOURCE_BACKOFF', http_state='not_attempted', cache_state='stale_last_good' if _cache_entries(db, source_id) else 'none')
                _record_observation(db, run_id, source_id, endpoint_id, started, report, report['profile_digest'])
                report.pop('_recorded', None)
                endpoint_reports.append(report)
                continue
            if retry_after and retry_after > now:
                report.update(error='SOURCE_RETRY_AFTER', http_state='not_attempted',
                              cache_state='stale_last_good' if _cache_entries(db, source_id) else 'none')
                _record_observation(db, run_id, source_id, endpoint_id, started, report, report['profile_digest'])
                report.pop('_recorded', None)
                endpoint_reports.append(report)
                continue
            headers = {}
            same_endpoint = not state.get('final_url') or state.get('final_url') == url
            if same_endpoint and state.get('etag'):
                headers['If-None-Match'] = state['etag']
            if same_endpoint and state.get('last_modified'):
                headers['If-Modified-Since'] = state['last_modified']
            host = urlsplit(url).hostname if isinstance(url, str) else None
            allowed_host = host if kind == 'html-table' else None
            allowed_path_prefix = (adapter.get('config') or {}).get('path_prefix') if kind == 'html-table' else None
            gate = host_gates.setdefault(host, asyncio.Semaphore(host_parallelism))
            retry_delay = 0.0
            retry_after_value = retry_after
            body = b''
            response = None
            final_url = url
            try:
                async with total_gate, gate:
                    if host_interval:
                        wait = host_interval - (clock() - host_times.get(host, 0.0))
                        if wait > 0:
                            await _sleep_value(wait, sleep)
                        host_times[host] = clock()
                    last_error = None
                    for attempt in range(2):
                        report['attempts'] = attempt + 1
                        if clock() >= deadline_at:
                            raise SourceFetchError('SOURCE_DEADLINE', retryable=True)
                        if retry_delay:
                            await _sleep_value(retry_delay, sleep)
                            retry_delay = 0
                        try:
                            async with asyncio.timeout(min(timeout, max(0.001, deadline_at - clock()))):
                                async with httpx.AsyncClient(trust_env=False, verify=TLS, follow_redirects=False,
                                                             timeout=timeout) as client:
                                    response, body, final_url, not_modified = await request_body(
                                        client, url, headers, budget, allowed_host=allowed_host,
                                        allowed_path_prefix=allowed_path_prefix)
                            report['status'] = response.status_code
                            report['http_state'] = 'http_2xx_nonempty'
                            report['final_url'] = final_url
                            if not_modified:
                                # 304 is not an empty answer: the stored set is what
                                # this source still offers, and the report says so —
                                # with its age — instead of counting zero rows.
                                stored = _cache_entries(db, source_id)
                                age = _last_good_age(db, source_id, clock())
                                report.update(http_state='not_modified', parse_state='not_run',
                                              cache_state='not_modified' if stored else 'none',
                                              complete=True, outcome='available', last_304_at=clock())
                                if stored:
                                    report.update(rows=len(stored), accepted=len(stored), recognized=len(stored),
                                                  new_endpoints=0, duplicate=0, duplicates_existing=0,
                                                  rejected=0, blocked=0, cache_age_seconds=age,
                                                  served_from_cache=True)
                                _ensure_state(db, source_id, endpoint_id)
                                db.execute('''UPDATE source_state SET last_304_at=?,last_attempt_at=?,
                                              consecutive_failures=0,backoff_until=NULL,quarantine_until=NULL,
                                              retry_after=NULL,last_error=NULL WHERE source_id=? AND endpoint_id=?''',
                                           (clock(), clock(), source_id, endpoint_id))
                                _record_observation(db, run_id, source_id, endpoint_id, started, report, report['profile_digest'])
                                report['_recorded'] = True
                                db.commit()
                                break
                            break
                        except asyncio.CancelledError:
                            raise
                        except SourceFetchError as exc:
                            last_error = exc
                            retry_after_value = exc.retry_after
                            if not exc.retryable or attempt:
                                break
                            await _sleep_value(min(float(exc.retry_after or 1), 5), sleep)
                        except (httpx.TimeoutException, TimeoutError):
                            last_error = SourceFetchError('SOURCE_TIMEOUT', retryable=True)
                            if attempt:
                                break
                            await _sleep_value(1, sleep)
                        except (httpx.HTTPError, OSError, ValueError, OverflowError) as exc:
                            last_error = exc
                            break
                    if response is None and last_error is not None:
                        raise last_error
                    # A read the check's own byte bound cut short is a prefix,
                    # not a source that cannot be read: the records below come
                    # from that prefix and the report says so.
                    truncated = bool(budget.get('truncated'))
                    if not report.get('complete') and response is not None and response.status_code != 304:
                        report['bytes'] = len(body)
                        report['body_sha256'] = hashlib.sha256(body).hexdigest() if body else None
                        if not body:
                            report.update(http_state='empty_body', parse_state='empty', cache_state='last_good' if _cache_entries(db, source_id) else 'none',
                                          complete=True, outcome='empty')
                            _record_observation(db, run_id, source_id, endpoint_id, started, report, report['profile_digest'])
                            report['_recorded'] = True
                        else:
                            if truncated:
                                # What the parser sees below is a prefix, so the
                                # observation is recorded as a partial one.
                                report.update(partial=True, error='SOURCE_TRUNCATED')
                            observed_profile = {**adapter, 'kind': kind}
                            page_records = []
                            page_number = 1
                            expected_total = None
                            signatures = set()
                            page_url = url
                            def persist_records(records, complete):
                                limited = []
                                endpoint_attempts = 0
                                for record in records:
                                    values = record.get('values') or [record.get('value', '')]
                                    if endpoint_attempts + len(values) > max_source_candidates:
                                        report['error'] = 'SOURCE_CANDIDATE_LIMIT'
                                        report['partial'] = True
                                        complete = False
                                        break
                                    endpoint_attempts += len(values)
                                    limited.append(record)
                                records = limited
                                report['parse_state'] = 'confirmed' if complete else 'partial'
                                report['outcome'] = ('available' if complete and records else 'empty') if complete else 'partial'
                                observation_id = _record_observation(db, run_id, source_id, endpoint_id, started, report, report['profile_digest'])
                                generation_id = _cache_generation(db, source_id, observation_id, report, records,
                                                                 {'endpoint_id': endpoint_id}, complete=complete, cache_bytes=cache_bytes)
                                counters = dict(endpoint_attempts=0, rejected=report['rejected'], blocked=0, accepted=0,
                                                new_endpoints=0, reject_reasons={})
                                for record in records:
                                    values = record.get('values') or [record.get('value', '')]
                                    if counters['endpoint_attempts'] + len(values) > max_source_candidates:
                                        report['error'] = 'SOURCE_CANDIDATE_LIMIT'
                                        report['partial'] = True
                                        report['complete'] = False
                                        break
                                    _add_rich_record(db, record, source_id, observation_id, generation_id, denylist, counters)
                                report['accepted'] = counters['accepted']
                                report['rows'] = counters['accepted']
                                report['rejected'] = counters['rejected']
                                report['blocked'] = counters['blocked']
                                report['duplicate'] = counters.get('duplicate', 0)
                                report['duplicates_existing'] = counters.get('duplicates_existing', 0)
                                report['new_endpoints'] = counters['new_endpoints']
                                for reason, count in counters.get('reject_reasons', {}).items():
                                    report['reject_reasons'][reason] = report['reject_reasons'].get(reason, 0) + count
                                report['complete'] = complete
                                if report.get('cache_state') != 'cache_evicted':
                                    report['cache_state'] = 'last_good' if complete and generation_id else ('partial' if generation_id else 'none')
                                _update_observation(db, observation_id, report)
                                report['_recorded'] = True
                                return counters
                            while page_number <= max_pages:
                                report['pages'] = page_number
                                budget_page = {'used': budget['used']}
                                try:
                                    if clock() >= deadline_at:
                                        raise SourceFetchError('SOURCE_DEADLINE', retryable=True)
                                    if page_number == 1:
                                        page_body = body
                                    else:
                                        # The endpoint-level gate is already
                                        # held for this source; acquiring it
                                        # again for a page would deadlock.
                                        async with asyncio.timeout(min(timeout, max(0.001, deadline_at - clock()))):
                                            async with httpx.AsyncClient(trust_env=False, verify=TLS, follow_redirects=False, timeout=timeout) as client:
                                                response, page_body, page_url, not_modified = await request_body(
                                                    client, page_url, {}, {'used': budget['used']},
                                                    allowed_host=allowed_host, allowed_path_prefix=allowed_path_prefix)
                                        if not_modified:
                                            raise SourceFetchError('SOURCE_NOT_MODIFIED_WITHOUT_PAGE_CACHE')
                                    if page_number > 1:
                                        budget['used'] += len(page_body)
                                    parsed = source_adapters.parse_page(page_body, observed_profile,
                                                                       {'page': page_number, 'url': page_url},
                                                                       {'max_records': max_source_candidates, 'max_bytes': max_source_bytes})
                                    parsed_records = parsed.get('records', [])
                                    report['received'] += len(parsed_records)
                                    report['recognized'] += len(parsed_records)
                                    report['rejected'] += sum(parsed.get('rejects', {}).values())
                                    # Reasons are a per-run observation, not durable
                                    # provenance: a preview must be able to say why
                                    # records were dropped without a second parser.
                                    for reason, count in (parsed.get('rejects') or {}).items():
                                        report['reject_reasons'][reason] = report['reject_reasons'].get(reason, 0) + count
                                    page_records.extend(parsed_records)
                                    if kind == 'page-json':
                                        try:
                                            envelope = json.loads(page_body.decode('utf-8-sig'))
                                            info = source_adapters.page_info(envelope, observed_profile)
                                            if info['page'] != page_number:
                                                raise SourceFetchError('SOURCE_PAGE_MISMATCH')
                                            if expected_total is None and info['total'] is not None:
                                                expected_total = info['total']
                                            signature = hashlib.sha256(json.dumps(info['records'], sort_keys=True).encode()).hexdigest()
                                            if signature in signatures:
                                                raise SourceFetchError('SOURCE_PAGINATION_REPEAT')
                                            signatures.add(signature)
                                            if not info['records'] and expected_total is not None and len(page_records) < expected_total:
                                                raise SourceFetchError('SOURCE_PAGINATION_EMPTY')
                                            if expected_total is not None and len(page_records) >= expected_total:
                                                break
                                            if info['has_more'] is False:
                                                break
                                            next_url = source_adapters.next_page_url(url, info, observed_profile, page_number + 1)
                                            if not next_url:
                                                raise SourceFetchError('SOURCE_PAGINATION_EMPTY')
                                            page_url = next_url
                                            page_number += 1
                                            continue
                                        except SourceFetchError:
                                            raise
                                        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                                            raise SourceFetchError('SOURCE_INVALID_PAGINATION')
                                    break
                                except asyncio.CancelledError:
                                    if page_records:
                                        report['partial'] = True
                                        report['error'] = 'SOURCE_CANCELED'
                                        persist_records(page_records, False)
                                        db.commit()
                                    raise
                                except SourceFetchError as exc:
                                    report['error'] = exc.code
                                    report['retryable'] = exc.retryable
                                    if page_records:
                                        report['partial'] = True
                                        persist_records(page_records, False)
                                        db.commit()
                                    raise
                                except source_adapters.AdapterError as exc:
                                    report['error'] = exc.code
                                    report['partial'] = bool(page_records) or exc.partial
                                    if page_records:
                                        persist_records(page_records, False)
                                        db.commit()
                                    raise
                            if kind == 'page-json' and page_number > max_pages and not report.get('error'):
                                report['error'] = 'SOURCE_PAGE_LIMIT'
                                report['partial'] = bool(page_records)
                            persist_records(page_records, not report.get('partial') and not report.get('error'))
                            db.commit()
            except asyncio.CancelledError:
                raise
            except SourceFetchError as exc:
                report['error'] = exc.code
                report['retryable'] = exc.retryable
                report['retry_after'] = getattr(exc, 'retry_after', None)
                report['http_state'] = {'SOURCE_RATE_LIMITED': 'http_429', 'SOURCE_TIMEOUT': 'timeout'}.get(exc.code, 'http_error')
                report['outcome'] = {'SOURCE_RATE_LIMITED': 'rate_limited', 'SOURCE_TIMEOUT': 'timeout',
                                     'SOURCE_DEADLINE': 'partial', 'SOURCE_TOO_LARGE': 'limit_exceeded', 'SOURCE_LINE_TOO_LARGE': 'limit_exceeded',
                                     'SOURCE_CANDIDATE_LIMIT': 'limit_exceeded'}.get(exc.code, 'unavailable')
            except source_adapters.AdapterError as exc:
                report['error'] = exc.code
                report['retryable'] = False
                report['http_state'] = 'http_2xx_nonempty' if response is not None else 'http_error'
                report['parse_state'] = 'invalid'
                report['outcome'] = 'html_placeholder' if exc.code == 'SOURCE_HTML_PLACEHOLDER' else 'invalid_json' if exc.code == 'SOURCE_INVALID_JSON' else 'limit_exceeded' if exc.code in ADAPTER_LIMIT_CODES else 'unavailable'
            except (httpx.HTTPError, TimeoutError, OSError, ValueError, OverflowError) as exc:
                timed_out = isinstance(exc, (TimeoutError, httpx.TimeoutException))
                report['error'] = 'SOURCE_TIMEOUT' if timed_out else type(exc).__name__
                report['http_state'] = 'timeout' if timed_out else 'http_error'
                report['outcome'] = 'timeout' if timed_out else 'unavailable'
            if truncated:
                # Whatever the read or the parser said about the rest of the
                # body, the body the check saw stopped at its own bound: that
                # is a partial read, not a verdict on the source.
                report.update(error='SOURCE_TRUNCATED', retryable=False, retry_after=None,
                              http_state='http_2xx_nonempty', parse_state='partial',
                              outcome='partial', partial=True, complete=False)
            if not report.get('_recorded'):
                if not report.get('parse_state') or report['parse_state'] == 'not_run':
                    report['parse_state'] = 'invalid' if report.get('error') else 'not_run'
                _record_observation(db, run_id, source_id, endpoint_id, started, report, report['profile_digest'])
                report['_recorded'] = True
            if report.get('error') and not truncated:
                if report.get('http_state') == 'not_attempted':
                    report['http_state'] = 'http_error'
                failures = int(state.get('consecutive_failures') or 0) + 1
                delay = (60, 300, 1800, 7200, 21600)[min(failures - 1, 4)] * random.uniform(.8, 1.2)
                backoff_at = clock() + delay
                quarantine_at = backoff_at if failures >= 3 else None
                db.execute('''UPDATE source_state SET last_attempt_at=?,final_url=?,etag=COALESCE(?,etag),
                              last_modified=COALESCE(?,last_modified),last_error=?,consecutive_failures=consecutive_failures+1,
                              backoff_until=?,quarantine_until=?,retry_after=? WHERE source_id=? AND endpoint_id=?''',
                           (clock(), final_url, response.headers.get('etag') if response is not None else None,
                            response.headers.get('last-modified') if response is not None else None,
                            report['error'], backoff_at, quarantine_at,
                            retry_after_value if response is None else _parse_retry_after(response.headers.get('retry-after'), clock()),
                            source_id, endpoint_id))
                if _cache_entries(db, source_id):
                    report['cache_state'] = 'stale_last_good'
                    report['served_from'] = 'last_good'
                    report['last_good_age_seconds'] = _last_good_age(db, source_id, clock())
                    report['rows'] = cached_add(source_id)
                    if report.get('fallback_used'):
                        report['outcome'] = 'fallback_used'
            else:
                db.execute('''UPDATE source_state SET last_attempt_at=?,last_body_at=?,final_url=?,etag=COALESCE(?,etag),
                              last_modified=COALESCE(?,last_modified),last_error=NULL WHERE source_id=? AND endpoint_id=?''',
                           (clock(), clock() if body else None, final_url, response.headers.get('etag') if response is not None else None,
                            response.headers.get('last-modified') if response is not None else None, source_id, endpoint_id))
            if report.get('complete') and not report.get('error'):
                report['outcome'] = report.get('outcome') or 'available'
            report.pop('_recorded', None)
            endpoint_reports.append(report)
            db.commit()
            # A valid complete primary is authoritative; malformed/empty/
            # failed primary may use a declared fallback URL.
            if report.get('complete') and report.get('outcome') in ('available', 'empty') and not report.get('error'):
                break
        reports.append(endpoint_reports[-1] if endpoint_reports else dict(source=index, source_id=source_id, error='SOURCE_NO_ENDPOINT', complete=False))
        total_rows += sum(item.get('rows', 0) for item in endpoint_reports)
        db.commit(); publish()

    publish()
    # Fault isolation is explicit: an unexpected adapter exception becomes a
    # source observation and cannot cancel unrelated source tasks.
    async def safe_fetch(index, plan):
        try:
            await fetch(index, plan)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Only a source that actually has stored rows can serve them again;
            # a crash on a source that never cached anything is not "stale".
            cached = _cache_entries(db, plan.get('id'))
            report = dict(source=index, source_id=plan.get('id'), rows=len(cached) if cached else 0, invalid=0, blocked=0, pages=0,
                          attempts=0, complete=False, error='SOURCE_ADAPTER_ERROR', format=plan.get('adapter', {}).get('kind'),
                          http_state='http_error', parse_state='invalid',
                          cache_state='stale_last_good' if cached else 'none', outcome='unavailable',
                          partial=True, retryable=False, bytes=0, received=0, recognized=0, accepted=0,
                          rejected=0, duplicate=0, duplicates_existing=0, new_endpoints=0,
                          reject_reasons={})
            if cached:
                report['served_from'] = 'last_good'
            _record_observation(db, run_id, plan.get('id'), 'primary', clock(), report, '')
            reports.append(report)
            db.commit(); publish()
    async with httpx.AsyncClient(trust_env=False, verify=TLS, follow_redirects=False, timeout=timeout) as client:
        # The client is intentionally created here as the sole network owner;
        # request_body uses short-lived clients for DNS-pinned destinations.
        del client
        async with asyncio.TaskGroup() as group:
            for index, plan in enumerate(plans, 1):
                group.create_task(safe_fetch(index, plan))
    db.commit(); publish()
    return dict(raw_rows=total_rows, unique=db.execute('SELECT count(*) FROM candidates').fetchone()[0],
                blocked=sum(item.get('blocked', 0) for item in reports), sources_total=len(plans),
                sources=reports, run_id=run_id, partial=any(item.get('partial') for item in reports))


async def preview_collect(db, urls, timeout=60, denylist=None, allow_private_sources=False, **kwargs):
    """Bounded availability/format check on a throwaway database.

    Same fetch, adapter, validation and limit path as a real collection run,
    but capped so one click on a multi-megabyte list cannot pull hundreds of
    megabytes or build a full candidate set in memory.  A body past the byte
    budget is read as a prefix and reported as a prefix, never as a source that
    cannot be read.
    """
    return await collect(db, urls, [], timeout, denylist=denylist,
                         allow_private_sources=allow_private_sources, preview=True,
                         bounded_prefix=True,
                         max_source_bytes=PREVIEW_MAX_BYTES,
                         max_source_candidates=PREVIEW_MAX_CANDIDATES, **kwargs)


def _record_legacy_provenance(db, reports, source_values, run_id):
    """Give the built-in line formats the same observable provenance tables.

    Their network reader stays untouched (and their historical 16-hex report
    keys stay compatible); only the runtime observation/generation layer is
    added here.
    """
    for report in reports:
        if 'source' not in report and 'input' not in report:
            continue
        if 'source' in report:
            index = int(report['source'])
            source_id = source_key(source_values[index - 1]) if index <= len(source_values) else 'local'
        else:
            source_id = 'local'
        db.execute('INSERT OR IGNORE INTO source_identity(source_id,family_id,publisher_id) VALUES (?,?,?)',
                   (source_id, source_id, None))
        started = time.time()
        normalized = dict(report)
        # A read the check's own byte bound cut short is neither a complete list
        # nor a broken one: it is a prefix, and it says so.
        partial = report.get('error') == 'SOURCE_TRUNCATED'
        normalized.update(http_state=report.get('http_state', 'http_2xx_nonempty' if report.get('complete') else 'http_error'),
                          parse_state='not_run' if report.get('http_state') == 'not_modified' else
                                      ('partial' if partial else ('confirmed' if report.get('complete') else 'invalid')),
                          cache_state='not_modified' if report.get('http_state') == 'not_modified' else 'none',
                          outcome='partial' if partial else ('available' if report.get('complete') else 'unavailable'),
                          accepted=max(0, report.get('rows', 0) - report.get('invalid', 0) - report.get('blocked', 0)),
                          recognized=report.get('rows', 0), received=report.get('rows', 0),
                          rejected=report.get('invalid', 0), new_endpoints=0, bytes=0,
                          profile_digest=hashlib.sha256(str(report.get('format', '')).encode()).hexdigest()[:20])
        observation_id = _record_observation(db, run_id, source_id, 'primary', started, normalized, normalized['profile_digest'])
        if report.get('complete') and report.get('rows', 0) > 0:
            records = [{'value': proxy, 'values': [proxy]} for (proxy,) in db.execute(
                'SELECT proxy FROM candidate_seen WHERE source=?', (source_id,))]
            _cache_generation(db, source_id, observation_id, normalized, records, {'endpoint_id': 'primary'}, complete=True)
    db.commit()


async def collect(db, urls, inputs, timeout=60, on_progress=None, denylist=None,
                  allow_private_sources=False, detect_protocols=False,
                  max_source_bytes=DEFAULT_SOURCE_MAX_BYTES,
                  max_source_line_bytes=DEFAULT_SOURCE_MAX_LINE_BYTES,
                  max_source_candidates=DEFAULT_SOURCE_MAX_CANDIDATES,
                  max_source_redirects=DEFAULT_SOURCE_MAX_REDIRECTS,
                  *, preview=False, bounded_prefix=False, parallelism=DEFAULT_SOURCE_PARALLELISM,
                  host_parallelism=DEFAULT_SOURCE_HOST_PARALLELISM,
                  host_interval=DEFAULT_SOURCE_HOST_INTERVAL,
                  deadline=DEFAULT_SOURCE_DEADLINE, cache_bytes=DEFAULT_CACHE_BYTES,
                  max_pages=DEFAULT_SOURCE_PAGES, clock=None, sleep=None, run_id=None):
    values = list(urls or [])
    rich_values = []
    legacy_values = []
    for value in values:
        kind = _rich_kind(value)
        if isinstance(value, dict) or kind in source_adapters.ADAPTER_KINDS or (isinstance(value, str) and value.strip() in {item['id'] for item in _catalog_cached()['sources']}):
            rich_values.append(value)
        else:
            legacy_values.append(value)
    if preview:
        # Preview has the same fetch/adapter/limit path but a private temporary
        # database, so it cannot change candidates, provenance or last-good.
        with tempfile.TemporaryDirectory(prefix='proxy-workbench-preview-') as directory:
            temporary = open_db(Path(directory) / 'preview.sqlite3')
            try:
                preview_report = await collect(
                    temporary, values, inputs, timeout, on_progress, denylist, allow_private_sources,
                    detect_protocols, max_source_bytes, max_source_line_bytes, max_source_candidates,
                    max_source_redirects, preview=False, bounded_prefix=bounded_prefix,
                    parallelism=parallelism,
                    host_parallelism=host_parallelism, host_interval=host_interval,
                    deadline=deadline, cache_bytes=cache_bytes,
                    max_pages=max_pages, clock=clock, sleep=sleep, run_id=run_id)
            finally:
                sample = [row[0] for row in temporary.execute(
                    'SELECT proxy FROM candidates ORDER BY proxy LIMIT 50')]
                temporary.close()
            preview_report['preview'] = True
            preview_report['sample'] = sample
            return preview_report
    legacy_values = list(dict.fromkeys(legacy_values))
    reports = []
    effective_run_id = run_id
    if legacy_values or inputs:
        legacy = await _collect_legacy(db, legacy_values, inputs, timeout, on_progress, denylist,
                                       allow_private_sources, detect_protocols, max_source_bytes,
                                       max_source_line_bytes, max_source_candidates, max_source_redirects,
                                       bounded_prefix=bounded_prefix)
        legacy_reports = legacy.pop('sources', [])
        effective_run_id = run_id or hashlib.sha256(f'{time.time()}:{id(db)}'.encode()).hexdigest()[:20]
        _record_legacy_provenance(db, legacy_reports, legacy_values, effective_run_id)
        reports.extend(legacy_reports)
    if rich_values:
        plans = []
        seen_plans = set()
        for value in rich_values:
            plan = _rich_plan(value)
            key = (plan.get('id'), tuple(endpoint.get('url', '') for endpoint in plan.get('endpoints', [])))
            if key not in seen_plans:
                seen_plans.add(key); plans.append(plan)
        rich = await _collect_rich_sources(db, plans, [], timeout, on_progress, denylist,
                                           allow_private_sources=allow_private_sources,
                                           max_source_bytes=max_source_bytes, max_source_candidates=max_source_candidates,
                                           max_source_redirects=max_source_redirects, detect_protocols=detect_protocols,
                                           preview=preview, bounded_prefix=bounded_prefix,
                                           parallelism=parallelism, host_parallelism=host_parallelism,
                                           host_interval=host_interval, deadline=deadline, cache_bytes=cache_bytes, max_pages=max_pages,
                                           clock=clock, sleep=sleep, run_id=effective_run_id)
        offset = len(reports)
        for item in rich.get('sources', []):
            item['source'] = int(item.get('source', 0)) + offset
        reports.extend(rich.get('sources', []))
        effective_run_id = rich.get('run_id', effective_run_id)
    db.commit()
    return dict(raw_rows=sum(item.get('rows', 0) for item in reports),
                unique=db.execute('SELECT count(*) FROM candidates').fetchone()[0],
                blocked=sum(item.get('blocked', 0) for item in reports),
                denylist_error=(denylist.error if denylist else None), sources_total=len(reports),
                sources=reports, run_id=effective_run_id)


def target_config(args, denylist=None):
    if args.config:
        config = json.loads(Path(args.config).read_text(encoding='utf-8'))
    else:
        config = {'targets': [{'url': args.url or 'https://example.com/'}]}
    if not isinstance(config, dict):
        raise ValueError('config: ожидается объект')
    targets = config.get('targets')
    if not isinstance(targets, list) or not targets:
        raise ValueError('config: нужен непустой список targets')
    judge = {'judge_url': args.judge_url} if getattr(args, 'judge_url', None) else config.get('anonymity')
    speedtest = ({'url': args.speedtest_url, 'max_bytes': args.speedtest_bytes} if getattr(args, 'speedtest_url', None)
                 else config.get('speedtest'))
    return validate_targets(targets, args,
                            request_profile=getattr(args, 'request_profile', None) or config.get('request_profile'),
                            reputation=config.get('reputation'),
                            denylist=denylist, anonymity_config=judge, speedtest_config=speedtest)


SPEEDTEST_BYTES = 5_000_000
MAX_SPEEDTEST_BYTES = 200_000_000


def validate_speedtest(config):
    """A normalized download test ({url, max_bytes}) or None when it is off."""
    if not config or not config.get('url'):
        return None
    url = config['url']
    if not isinstance(url, str) or len(url) > 2048:
        raise ValueError('speedtest.url: ожидается http(s) URL')
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('speedtest.url: нужен http(s) URL без userinfo')
    size = config.get('max_bytes', SPEEDTEST_BYTES)
    if type(size) is not int or not 10_000 <= size <= MAX_SPEEDTEST_BYTES:
        raise ValueError('speedtest.max_bytes: от 10000 до 200000000')
    return {'url': url, 'max_bytes': size}


def validate_targets(targets, args, request_profile=None, reputation=None, denylist=None, anonymity_config=None,
                     speedtest_config=None):
    request_profile = request_profile or getattr(args, 'request_profile', None) or DEFAULT_REQUEST_PROFILE
    validate_profile(request_profile)
    for t in targets:
        if not isinstance(t, dict) or not isinstance(t.get('url'), str):
            raise ValueError('target: нужен объект с URL')
        p = urlsplit(t['url'])
        if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
            raise ValueError('target URL: нужен http(s) URL без userinfo')
        try:
            port = p.port
        except ValueError:
            raise ValueError('target URL: некорректный порт') from None
        if port is not None and not 1 <= port <= 65535:
            raise ValueError('target URL: некорректный порт')
        if not isinstance(t.get('method', 'GET'), str):
            raise ValueError('method: ожидается строка')
        t.setdefault('method', 'GET')
        t['method'] = t['method'].upper()
        if t['method'] not in ('GET', 'HEAD'):
            raise ValueError('Поддерживаются GET и HEAD')
        t.setdefault('statuses', list(range(200, 300)))
        if not isinstance(t['statuses'], list) or not t['statuses'] or any(type(s) is not int or not 100 <= s <= 599 for s in t['statuses']):
            raise ValueError('statuses: ожидается список HTTP-кодов')
        t.setdefault('headers', {})
        if not isinstance(t['headers'], dict) or any(not isinstance(k, str) or not isinstance(v, str) or '\n' in k+v or '\r' in k+v for k, v in t['headers'].items()):
            raise ValueError('headers: ожидается объект со строковыми значениями')
        if any(name.lower() not in SAFE_TARGET_HEADERS for name in t['headers']):
            raise ValueError('headers: разрешены только безопасные HTTP-заголовки без credentials')
        t.setdefault('contains', None)
        t.setdefault('sha256', None)
        if t['contains'] is not None and not isinstance(t['contains'], str):
            raise ValueError('contains: ожидается строка')
        if t['sha256'] is not None and (not isinstance(t['sha256'], str) or not re.fullmatch('[a-fA-F0-9]{64}', t['sha256'])):
            raise ValueError('sha256: ожидается хеш из 64 hex символов')
    args_dnsbl = getattr(args, 'dnsbl', None)
    args_zones = getattr(args, 'dnsbl_zones', None)
    args_timeout = getattr(args, 'reputation_timeout', None)
    args_strict = getattr(args, 'strict_clean', None)
    args_local = getattr(args, 'local_denylist', None)
    policy = make_policy(reputation, denylist or Denylist.empty(),
                         local_override=args_local,
                         dnsbl_override=args_dnsbl,
                         zones_override=args_zones if args_zones else None,
                         timeout_override=args_timeout,
                         strict_override=args_strict)
    config = dict(version=2, targets=targets, attempts=args.attempts,
                  timeout=args.timeout, max_bytes=args.max_bytes,
                  request_profile=request_profile,
                  request_profile_digest=profile_digest(request_profile),
                  reputation=policy)
    # Optional keys are added only when they change measurements, so older
    # profiles keep their identity.
    connect_timeout = getattr(args, 'connect_timeout', None)
    if connect_timeout is not None and connect_timeout < args.timeout:
        config['connect_timeout'] = connect_timeout
    if getattr(args, 'fail_fast', False):
        config['fail_fast'] = {'min_success': args.min_success}
    speedtest = validate_speedtest(speedtest_config)
    if speedtest:
        config['speedtest'] = speedtest
    judge = anonymity.validate_judge(anonymity_config)
    if judge:
        config['anonymity'] = judge
    return config


def proxy_client(proxy, config):
    """A fresh client for one request through `proxy`; SOCKS4 needs its own transport."""
    options = dict(trust_env=False, timeout=request_timeout(config), follow_redirects=False)
    if proxy.startswith('socks4://'):
        return httpx.AsyncClient(transport=socks4.transport(proxy, verify=TLS), **options)
    return httpx.AsyncClient(proxy=proxy, verify=TLS, **options)


def request_timeout(config):
    """Whole-request timeout with an optional shorter limit for connecting."""
    return httpx.Timeout(config['timeout'], connect=config.get('connect_timeout', config['timeout']))


async def request_once(proxy, target, config, rate):
    await rate.wait()
    start = time.monotonic()
    result = dict(ok=False, status=None, ms=None, bytes=0, error=None)
    try:
        async with asyncio.timeout(config['timeout']):
            # Fresh connections make samples comparable (including CONNECT/TLS).
            async with proxy_client(proxy, config) as client:
                headers = merge_headers(config.get('request_profile', DEFAULT_REQUEST_PROFILE), target['headers'])
                async with client.stream(target['method'], target['url'],
                                         headers=headers) as response:
                    result['status'] = response.status_code
                    if response.status_code not in target['statuses']:
                        result['error'] = f'HTTP_{response.status_code}'
                        return result
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > config['max_bytes']:
                            result['error'] = 'BODY_TOO_LARGE'
                            return result
                        body.extend(chunk)
                    result['bytes'] = len(body)
                    if target['contains'] is not None and target['contains'].encode() not in body:
                        result['error'] = 'CONTENT_MISMATCH'
                    elif target['sha256'] and hashlib.sha256(body).hexdigest() != target['sha256'].lower():
                        result['error'] = 'HASH_MISMATCH'
                    else:
                        result['ok'] = True
    except Exception as exc:
        # Broken proxies raise more than httpx errors (socksio parses raw replies).
        result['error'] = type(exc).__name__
    finally:
        result['ms'] = round((time.monotonic() - start) * 1000, 2)
    return result


def summarize(proxy, samples, config):
    successful = [s['ms'] for s in samples if s['ok']]
    reliability = len(successful) / len(samples)
    per_target = []
    for i in range(len(config['targets'])):
        subset = [s for s in samples if s['target'] == i]
        per_target.append(sum(s['ok'] for s in subset) / len(subset) if subset else 0.0)
    median = statistics.median(successful) if successful else None
    jitter = statistics.pstdev(successful) if successful else None
    score = 100 * min(per_target) / (1 + (median + jitter) / 1000) if successful else 0
    return dict(proxy=proxy, reliability=reliability, min_target_reliability=min(per_target),
                latency_ms=median, jitter_ms=jitter, score=round(score, 5),
                successes=len(successful), requests=len(samples), checked_at=time.time(), samples=samples)


def next_history(previous, ok, checked_at):
    """Running record of how often a proxy passed across re-checks."""
    previous = previous or {}
    return {
        'checks': previous.get('checks', 0) + 1,
        'passes': previous.get('passes', 0) + int(ok),
        'first_checked': previous.get('first_checked') or checked_at,
        'last_ok': checked_at if ok else previous.get('last_ok'),
    }


def row_history(row, min_success=2/3):
    """History of a stored row; rows from before 1.6 count as one check."""
    return row.get('history') or next_history(None, result_allowed(row, min_success), row.get('checked_at'))


def freshness_seconds(watch_minutes=0):
    """A result is fresh for two watch intervals, but never less than two hours."""
    try:
        watch = max(0.0, float(watch_minutes))
    except (TypeError, ValueError):
        watch = 0.0
    return max(MIN_FRESHNESS_SECONDS, 2 * watch * 60)


def stamp_freshness(row, watch_minutes=0, fallback_checked_at=None):
    """Add the freshness contract to a row without invalidating pre-1.7 data."""
    checked_at = row.get('checked_at')
    if not isinstance(checked_at, (int, float)) or isinstance(checked_at, bool) or not math.isfinite(checked_at) or checked_at <= 0:
        checked_at = fallback_checked_at or time.time()
        row['checked_at'] = checked_at
    row['valid_until'] = checked_at + freshness_seconds(watch_minutes)
    return row


def row_fresh(row, now=None):
    """Whether a ranked row is currently usable; legacy rows without TTL stay compatible."""
    valid_until = row.get('valid_until')
    if valid_until is None:
        return True
    try:
        return float(valid_until) > (time.time() if now is None else now)
    except (TypeError, ValueError, OverflowError):
        return False


def allowed_failures(config):
    """Failures per target a proxy may have and still pass, or None without fail-fast."""
    fail_fast = config.get('fail_fast')
    if not fail_fast:
        return None
    attempts = config['attempts']
    needed = max(1, math.ceil(fail_fast['min_success'] * attempts - 1e-9))
    return attempts - needed


async def check_proxy(proxy, config, rate, own_ips=None):
    samples = []
    limit = allowed_failures(config)
    failures = [0] * len(config['targets'])
    for attempt in range(config['attempts']):
        for index, target in enumerate(config['targets']):
            sample = await request_once(proxy, target, config, rate)
            samples.append(dict(sample, target=index, attempt=attempt + 1))
            failures[index] += not sample['ok']
            # Once one target can no longer reach the threshold the proxy cannot
            # pass, so the remaining requests would only cost time.
            if limit is not None and failures[index] > limit:
                row = summarize(proxy, samples, config)
                row['aborted'] = True
                return row
    row = summarize(proxy, samples, config)
    # The judge is asked only through proxies that already work for a target.
    if config.get('anonymity') and own_ips and row['successes']:
        row['anonymity'] = await judge_proxy(proxy, config, rate, own_ips)
    # Bandwidth is measured only for proxies that work for every target.
    if config.get('speedtest') and row['min_target_reliability'] > 0:
        row['speed'] = await measure_speed(proxy, config, rate)
    return row


async def measure_speed(proxy, config, rate):
    """Download throughput through the proxy in Mbit/s, counted from the first response byte."""
    await rate.wait()
    test = config['speedtest']
    result = dict(mbps=None, bytes=0, ms=None, error=None)
    # perf_counter: the Windows monotonic clock ticks every ~16 ms, too coarse for fast transfers.
    started = time.perf_counter()
    try:
        async with asyncio.timeout(max(config['timeout'], 30)):
            async with proxy_client(proxy, config) as client:
                headers = merge_headers(config.get('request_profile', DEFAULT_REQUEST_PROFILE))
                async with client.stream('GET', test['url'], headers=headers) as response:
                    if not 200 <= response.status_code < 300:
                        result['error'] = f'HTTP_{response.status_code}'
                        return result
                    first = None
                    async for chunk in response.aiter_raw():
                        first = first or time.perf_counter()
                        result['bytes'] += len(chunk)
                        if result['bytes'] >= test['max_bytes']:
                            break
                    elapsed = max(time.perf_counter() - (first or started), 1e-6)
                    if result['bytes']:
                        result['mbps'] = round(result['bytes'] * 8 / elapsed / 1e6, 2)
    except Exception as exc:
        result['error'] = type(exc).__name__
        # A partial download still says something about the speed.
    finally:
        result['ms'] = round((time.perf_counter() - started) * 1000, 2)
    return result


async def detect_own_ips(config):
    """Public IPs of this machine as seen by the judge; kept in memory only."""
    url = config['anonymity']['judge_url']
    headers = merge_headers(config.get('request_profile', DEFAULT_REQUEST_PROFILE))
    async with httpx.AsyncClient(trust_env=False, verify=TLS, timeout=config['timeout'],
                                 follow_redirects=False) as client:
        body = await anonymity.fetch_judge(client, url, headers)
    own = anonymity.extract_public_ips(body.decode('utf-8', errors='replace'))
    # Judges such as azenv also print their own server address; never treat it as ours.
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(urlsplit(url).hostname, None)
        own -= {ipaddress.ip_address(info[4][0].split('%')[0]).compressed for info in infos}
    except (OSError, ValueError):
        pass
    if not own:
        raise ValueError('judge URL не показал внешний IP этого устройства')
    return own


async def judge_proxy(proxy, config, rate, own_ips):
    await rate.wait()
    started = time.monotonic()
    headers = merge_headers(config.get('request_profile', DEFAULT_REQUEST_PROFILE))
    try:
        async with asyncio.timeout(config['timeout']):
            async with proxy_client(proxy, config) as client:
                body = await anonymity.fetch_judge(client, config['anonymity']['judge_url'], headers)
    except ValueError as exc:
        return anonymity.result('unknown', error=str(exc), started=started)
    except Exception as exc:
        return anonymity.result('unknown', error=type(exc).__name__, started=started)
    verdict = anonymity.classify(body, own_ips)
    return anonymity.result(verdict['level'], verdict['signals'], started=started, exit_address=anonymity.exit_ip(body))


def fit_workers(requested):
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        desired = requested * 3 + 128
        if soft < desired:
            try:
                resource.setrlimit(resource.RLIMIT_NOFILE,
                                   (min(desired, hard) if hard != resource.RLIM_INFINITY else desired, hard))
                soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
            except (OSError, ValueError):
                pass
        return max(1, min(requested, (soft - 128) // 3))
    except (ImportError, OSError, ValueError):
        return min(requested, 128)


def fit_prefilter(workers, requested):
    """Connections left for the reachability pre-check once the full-check workers are counted."""
    if requested <= 0:
        return 0
    if os.name == 'nt':
        # The Windows proactor loop has no per-process select() limit.
        return requested
    return max(1, min(requested, fit_workers(workers + requested) - workers))


async def reachable(proxy, timeout):
    """Whether anything accepts a TCP connection at the proxy's address."""
    _, host, port = formats.split(proxy)
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, asyncio.TimeoutError, ValueError):
        return False
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return True


def unreachable_result(proxy):
    return dict(proxy=proxy, reliability=0, min_target_reliability=0,
                latency_ms=None, jitter_ms=None, score=0, successes=0, requests=0,
                checked_at=time.time(), samples=[], error='UNREACHABLE')


def blocked_result(proxy, verdict):
    return dict(proxy=proxy, reliability=0, min_target_reliability=0,
                latency_ms=None, jitter_ms=None, score=0, successes=0, requests=0,
                checked_at=time.time(), samples=[], reputation=verdict)


async def scan(db, config, *, workers=128, rate=100, recheck=False, probe=check_proxy, progress=True, on_progress=None, min_success=2/3, screen=None, denylist=None, min_anonymity='any', protocol='all', max_latency=None,
               countries=(), country_of=None, want=0, recheck_passing=False, prefilter=0, prefilter_timeout=3,
               exclude_hosting=False, provider_of=None, run_state=None, app_run_id=None, scope_digest='default'):
    """Check every pending candidate of the profile.

    With ``prefilter`` connections a cheap TCP connect runs first: most public
    addresses are dead, and dropping them there is far faster than a full
    request with its connect timeout. Only reachable addresses reach the workers.
    """
    denylist = denylist or Denylist.empty()
    policy = config.get('reputation', {})
    strict = bool(policy.get('strict', False))
    active_denylist = denylist if policy.get('local_enabled', True) else None
    if not config.get('anonymity'):
        min_anonymity = 'any'
    encoded = json.dumps(config, sort_keys=True)
    profile = hashlib.sha256(encoded.encode()).hexdigest()[:20]
    db.execute('INSERT OR IGNORE INTO profiles VALUES (?, ?)', (profile, encoded))
    countries = frozenset(countries or ())
    country_of = country_of or (lambda proxy: None)
    excluded_scope = scope_excluded(db, scope_digest or 'default')

    def selected(proxy):
        if proxy in excluded_scope:
            return False
        # Protocol and country narrow which candidates are checked; they are not
        # part of the profile, so a later wider run reuses these results.
        if protocol not in (None, 'all') and proxy_protocol(proxy) != protocol:
            return False
        if exclude_hosting and is_hosting(proxy, provider_of):
            return False
        return not countries or country_of(proxy) in countries

    def counts_as_passed(row):
        return (result_allowed(row, min_success, denylist=active_denylist, strict=strict, min_anonymity=min_anonymity)
                and matches_selection(row, protocol, max_latency))

    # A re-check keeps each proxy's history and its last usable payload until a
    # replacement is actually written.  A cancellation must never erase data.
    previous = {}
    only = None
    if recheck or recheck_passing:
        for proxy, payload in db.execute('SELECT proxy, payload FROM results WHERE profile=?', (profile,)).fetchall():
            row = json.loads(payload)
            if recheck_passing and not (selected(proxy) and counts_as_passed(row)):
                continue
            previous[proxy] = row_history(row, min_success)
        if recheck_passing:
            only = set(previous)
    db.commit()

    done = set() if (recheck or recheck_passing) else {
        proxy for (proxy,) in db.execute('SELECT proxy FROM results WHERE profile=?', (profile,))
    }
    pending, total, completed = [], 0, 0
    for (proxy,) in db.execute('SELECT proxy FROM candidates ORDER BY proxy'):
        if (only is None or proxy in only) and selected(proxy):
            total += 1
            if proxy in done:
                completed += 1
            else:
                pending.append(proxy)
    # Addresses that already worked in another profile go first. In find-N mode
    # the rest is shuffled so early results are not all from one subnet or
    # source; a full sweep keeps key order, because random inserts into a large
    # results index make SQLite commits much slower (worst on Windows).
    proven = {proxy for (proxy,) in db.execute(
        "SELECT DISTINCT proxy FROM results WHERE profile<>? AND json_extract(payload,'$.min_target_reliability')>0",
        (profile,))}
    if want:
        random.shuffle(pending)
    pending.sort(key=lambda proxy: proxy not in proven)
    passed = 0
    status_counts = {'clean': 0, 'listed': 0, 'unknown': 0, 'local_denied': 0}
    if not (recheck or recheck_passing):
        for (payload,) in db.execute('SELECT payload FROM results WHERE profile=?', (profile,)):
            row = json.loads(payload)
            if not selected(row['proxy']) or (only is not None and row['proxy'] not in only):
                continue
            status = (row.get('reputation') or {}).get('status', 'clean')
            status_counts[status] = status_counts.get(status, 0) + 1
            passed += int(counts_as_passed(row))
    initial = completed
    limiter = Rate(rate)
    queue = asyncio.Queue(maxsize=workers * 2)
    incoming = asyncio.Queue(maxsize=max(prefilter, 1) * 2) if prefilter else None
    unreachable = 0
    started = last_commit = time.monotonic()

    enough = asyncio.Event()
    if want and passed >= want:
        enough.set()

    def store(proxy, row):
        nonlocal completed, last_commit, passed
        country = country_of(proxy)
        if country:
            row['country'] = country
        row['history'] = next_history(previous.get(proxy), result_allowed(row, min_success), row.get('checked_at', time.time()))
        db.execute('INSERT OR REPLACE INTO results VALUES (?, ?, ?)',
                   (profile, proxy, json.dumps(row, ensure_ascii=False)))
        if app_run_id:
            db.execute('INSERT OR REPLACE INTO result_run(profile,proxy,run_id,checked_at) VALUES (?,?,?,?)',
                       (profile, proxy, app_run_id, time.time()))
        completed += 1
        status = (row.get('reputation') or {}).get('status', 'clean')
        status_counts[status] = status_counts.get(status, 0) + 1
        passed += int(counts_as_passed(row))
        if want and passed >= want:
            enough.set()
        if completed % 100 == 0 or time.monotonic() - last_commit >= 1:
            db.commit()
            last_commit = time.monotonic()

    async def producer():
        target = incoming if prefilter else queue
        for proxy in pending:
            if enough.is_set():
                break
            await target.put(proxy)
        for _ in range(prefilter if prefilter else workers):
            await target.put(None)

    async def gatekeeper():
        nonlocal unreachable
        while True:
            proxy = await incoming.get()
            if proxy is None:
                return
            if enough.is_set():
                continue
            if await reachable(proxy, prefilter_timeout):
                await queue.put(proxy)
            else:
                unreachable += 1
                store(proxy, unreachable_result(proxy))

    async def prefilter_stage():
        await asyncio.gather(*(gatekeeper() for _ in range(prefilter)))
        for _ in range(workers):
            await queue.put(None)

    async def worker():
        while True:
            proxy = await queue.get()
            try:
                if proxy is None:
                    return
                if enough.is_set():
                    # Left unchecked; a later run of the same profile picks it up.
                    continue
                verdict = None
                if screen is not None:
                    try:
                        verdict = await screen(proxy, config)
                    except Exception:
                        verdict = {'status': 'unknown', 'checked_at': time.time(), 'error': 'SCREEN_ERROR',
                                   'local_rule': None, 'dnsbl': []}
                if verdict is not None and verdict_blocks(verdict, strict):
                    row = blocked_result(proxy, verdict)
                else:
                    try:
                        row = await probe(proxy, config, limiter)
                    except Exception as exc:
                        # One malformed proxy must never stop the whole scan.
                        row = unreachable_result(proxy)
                        row['error'] = type(exc).__name__
                    if verdict is not None:
                        row['reputation'] = verdict
                store(proxy, row)
            finally:
                queue.task_done()

    def publish():
        elapsed = max(0.001, time.monotonic() - started)
        speed = (completed - initial) / elapsed
        eta = (total - completed) / speed if speed else 0
        incomplete = completed < total
        if recheck_passing:
            snapshot_state = 'partial'
            stop_reason = 'recheck_passing'
        elif incomplete and enough.is_set():
            snapshot_state = 'partial'
            stop_reason = 'want_reached'
        elif incomplete:
            snapshot_state = 'partial'
            stop_reason = 'stopped'
        else:
            snapshot_state = 'complete'
            stop_reason = 'complete'
        if run_state is not None:
            run_state.update(profile=profile, state=snapshot_state, stop_reason=stop_reason,
                             scope_candidates=total, checked=completed, pending=max(0, total - completed),
                             passed=passed)
        if on_progress:
            on_progress(dict(phase='scanning', profile=profile, checked=completed, candidates=total, passed=passed,
                             pending=max(0, total - completed), state=snapshot_state, stop_reason=stop_reason,
                             scope_candidates=total, speed=round(speed, 2),
                             eta_seconds=round(eta) if speed else None, workers=workers,
                             reputation=status_counts, unreachable=unreachable))
        if progress:
            print(tr(f'Проверено {completed}/{total}; {speed:.1f} прокси/с; осталось ~{eta / 60:.1f} мин', f'Checked {completed}/{total}; {speed:.1f} proxies/s; ~{eta / 60:.1f} min left'), flush=True)

    async def reporter():
        while True:
            publish()
            await asyncio.sleep(2)

    try:
        async with asyncio.TaskGroup() as group:
            if progress or on_progress:
                reporter_task = group.create_task(reporter())
            group.create_task(producer())
            if prefilter:
                group.create_task(prefilter_stage())
            tasks = [group.create_task(worker()) for _ in range(workers)]
            await asyncio.gather(*tasks)
            if progress or on_progress:
                reporter_task.cancel()
    finally:
        db.commit()
        publish()
    if app_run_id:
        record_source_scan_stats(db, app_run_id, hashlib.sha256(profile.encode()).hexdigest()[:20], scope_digest or 'default', profile,
                                 min_success=min_success, countries=countries, country_of=country_of,
                                 protocol=protocol, max_latency=max_latency)
    return profile


SORTS = ('recommended', 'quality', 'speed', 'stability', 'uptime', 'bandwidth')
EXPORT_ORDERS = {
    'quality': 'e.score DESC, e.latency, e.proxy',
    'speed': 'e.latency, e.reliability DESC, e.proxy',
    'stability': 'e.jitter, e.latency, e.proxy',
    'uptime': 'e.uptime DESC, e.checks DESC, e.score DESC, e.proxy',
    'bandwidth': 'e.bandwidth IS NULL, e.bandwidth DESC, e.score DESC, e.proxy',
    'recommended': 'e.recommended DESC, e.score DESC, e.proxy',
}
# Ready-made formats for other tools, next to the per-protocol host:port lists.
PROXYCHAINS_TYPES = {'http': 'http', 'socks4': 'socks4', 'socks5': 'socks5'}
PROTOCOLS = ('all', 'http', 'https', 'socks4', 'socks5')


def row_country(row, country_of=None):
    return row.get('country') or (country_of(row.get('proxy', '')) if country_of else None)


def country_resolver(db, geo=None, origin='effective'):
    """Country by explicit origin: measured, declared, or compatibility effective."""
    if origin not in ('effective', 'measured_endpoint', 'declared_endpoint'):
        raise ValueError('country origin: ожидается measured_endpoint/declared_endpoint/effective')
    declared = dict(db.execute('SELECT proxy, country FROM candidate_meta WHERE country IS NOT NULL'))
    measured = {}
    cache = {}

    def country_of(proxy):
        if proxy not in cache:
            if origin == 'declared_endpoint':
                cache[proxy] = declared.get(proxy)
            elif origin == 'measured_endpoint':
                cache[proxy] = geo.country_of(proxy) if geo else None
            else:
                cache[proxy] = declared.get(proxy) or (geo.country_of(proxy) if geo else None)
        return cache[proxy]
    return country_of


def country_origin(country, db=None, proxy=None):
    """Label a country value so a source claim is not presented as a measurement."""
    if not country:
        return 'unknown', None
    if db is not None and proxy:
        try:
            if db.execute('SELECT 1 FROM source_metadata WHERE proxy=? AND country=? LIMIT 1', (proxy, country)).fetchone():
                return 'source_claimed', None
        except sqlite3.Error:
            pass
    return 'app_measured', None


def exit_country(row, country_of=None):
    """Country of the address the judge saw, which can differ from the proxy's own address."""
    address = (row.get('anonymity') or {}).get('exit_ip')
    if not address or not country_of:
        return None
    return country_of(f'http://[{address}]:1' if ':' in address else f'http://{address}:1')


def listed_counts(db, *, group_mirrors=True):
    """Distinct independent lists offering each address.

    Catalog source IDs carry a family identity.  Two mirror feeds in the same
    family are one independent contribution; legacy rows without that identity
    keep the historical count.
    """
    try:
        rows = db.execute('SELECT proxy,source FROM candidate_seen').fetchall()
        families = {}
        if group_mirrors:
            families = dict(db.execute('SELECT source_id,family_id FROM source_identity WHERE family_id IS NOT NULL'))
    except sqlite3.Error:
        return {}
    grouped = {}
    for proxy, source in rows:
        key = families.get(source, source)
        grouped.setdefault(proxy, set()).add(key)
    return {proxy: len(values) for proxy, values in grouped.items()}


def source_contributions(db, source_ids=None):
    """Exclusive/leave-one-out contribution over the active generations."""
    ids = list(source_ids or [row[0] for row in db.execute('SELECT source_id FROM source_generation GROUP BY source_id')])
    sets = {}
    for source_id in ids:
        row = db.execute('''SELECT id FROM source_generation
                            WHERE source_id=? ORDER BY active DESC,last_good DESC,id DESC LIMIT 1''', (source_id,)).fetchone()
        sets[source_id] = {item[0] for item in db.execute('SELECT proxy FROM source_generation_entry WHERE generation_id=?', (row[0],))} if row else set()
    union = set().union(*sets.values()) if sets else set()
    result = {}
    for source_id, values in sets.items():
        other = set().union(*(candidate for key, candidate in sets.items() if key != source_id)) if len(sets) > 1 else set()
        result[source_id] = dict(accepted=len(values), exclusive=len(values - other),
                                 leave_one_out=len(union - other),
                                 share=(len(values & other) / len(values)) if values else 0.0)
    return result


def recover_source(db, source_id, endpoint_id=None):
    """Clear transport quarantine without deleting cache or last-good data."""
    if endpoint_id is None:
        cursor = db.execute('''UPDATE source_state SET backoff_until=NULL,quarantine_until=NULL,retry_after=NULL,
                               consecutive_failures=0,last_error=NULL WHERE source_id=?''', (source_id,))
    else:
        cursor = db.execute('''UPDATE source_state SET backoff_until=NULL,quarantine_until=NULL,retry_after=NULL,
                               consecutive_failures=0,last_error=NULL WHERE source_id=? AND endpoint_id=?''',
                            (source_id, endpoint_id))
    db.commit()
    return cursor.rowcount


def record_source_scan_stats(db, app_run_id, profile_digest, scope_digest, profile, *, min_success=2/3,
                             countries=(), country_of=None, protocol='all', max_latency=None):
    """Store per-run source statistics without making pass=0 a health verdict."""
    if not app_run_id:
        return {}
    sources = [row[0] for row in db.execute('SELECT DISTINCT source FROM candidate_seen')]
    try:
        rows = {row[0]: json.loads(row[1]) for row in db.execute('SELECT proxy,payload FROM results WHERE profile=?', (profile,))}
        run_rows = {row[0] for row in db.execute('SELECT proxy FROM result_run WHERE profile=? AND run_id=?', (profile, app_run_id))}
    except sqlite3.Error:
        rows, run_rows = {}, set()
    per_source = {}
    for source_id in sources:
        checked = set()
        passed = set()
        for proxy, source in db.execute('SELECT proxy,source FROM candidate_seen WHERE source=?', (source_id,)):
            row = rows.get(proxy)
            if not row or proxy not in run_rows:
                continue
            checked.add(proxy)
            if result_allowed(row, min_success) and matches_selection(row, protocol, max_latency, countries, country_of):
                passed.add(proxy)
        per_source[source_id] = (checked, passed)
    global_checked = set().union(*(value[0] for value in per_source.values())) if per_source else set()
    global_passed = set().union(*(value[1] for value in per_source.values())) if per_source else set()
    result = {}
    for source_id, (checked, passed) in per_source.items():
        db.execute('''INSERT OR REPLACE INTO source_scan_stat(
            app_run_id,profile_digest,scope_digest,source_id,checked_by_app,passed_profile,
            global_unique_checked,global_unique_passed)
            VALUES (?,?,?,?,?,?,?,?)''', (app_run_id, profile_digest, scope_digest, source_id,
                                          len(checked), len(passed), len(global_checked), len(global_passed)))
        result[source_id] = dict(checked_by_app=len(checked), passed_profile=len(passed),
                                 global_unique_checked=len(global_checked), global_unique_passed=len(global_passed))
    db.commit()
    return result


def scope_digest_for(protocol='all', countries=(), max_latency=0, exclude_hosting=False, query=''):
    """Stable id of one check scope; shared by the CLI, the GUI and the API.

    Country codes are normalized here because the three callers hand the same
    scope over in different shapes: ``geoip.parse_countries('')`` is an empty
    tuple while ``''.split(',')`` is ``['']``.  Without normalization the two
    would compute different digests and an exclusion made by one of them would
    be invisible to the others.
    """
    codes = sorted({str(value).strip().upper() for value in (countries or ()) if str(value).strip()})
    return hashlib.sha256(json.dumps({
        'protocol': protocol, 'countries': codes, 'max_latency': max_latency,
        'exclude_hosting': bool(exclude_hosting), 'query': query}, sort_keys=True).encode()).hexdigest()[:20]


def exclude_scope(db, proxies, *, reason='user', source_id=None, scope_digest='default', ttl=None):
    values = [normalize(value) for value in proxies or []]
    now = time.time()
    expires = now + float(ttl) if ttl is not None else None
    for proxy in values:
        if proxy:
            db.execute('''INSERT OR REPLACE INTO candidate_scope_exclusion(proxy,reason,source_id,scope_digest,created_at,expires_at)
                          VALUES (?,?,?,?,?,?)''', (proxy, reason, source_id, scope_digest, now, expires))
    db.commit()
    return len([value for value in values if value])


def scope_excluded(db, scope_digest='default', now=None):
    try:
        current = time.time() if now is None else now
        return {row[0] for row in db.execute('''SELECT proxy FROM candidate_scope_exclusion
                                               WHERE scope_digest=? AND (expires_at IS NULL OR expires_at>?)''',
                                            (scope_digest, current))}
    except sqlite3.Error:
        return set()



def recommender(source_quality, listed, sources, min_success=2/3):
    """Recommended score: quality, survival across re-checks, rarity across lists and the source's record.

    A proxy offered by one list is used by fewer people than one in twenty lists; a list whose
    proxies keep working earns trust. Both only reorder proxies that already passed.
    """
    rates = {key: (stats.get('passed', 0) + 1) / (stats.get('checked', 0) + 10)
             for key, stats in (source_quality or {}).items()}
    best = max(rates.values(), default=0) or 1

    def score(row):
        history = row_history(row, min_success)
        uptime = history['passes'] / history['checks'] if history['checks'] else 0
        rarity = 1 / math.sqrt(max(1, listed.get(row['proxy'], 1)))
        source = rates.get(sources.get(row['proxy']), best / 2) / best
        return round((row.get('score') or 0) * (0.5 + 0.5 * uptime) * (0.5 + 0.5 * rarity) * (0.5 + 0.5 * source), 5)
    return score


def provider_resolver(asn_db):
    """Provider (AS number, organisation, hosting flag) of a proxy address, or None without a database."""
    if asn_db is None:
        return None
    cache = {}

    def provider_of(proxy):
        if proxy not in cache:
            cache[proxy] = asn_db.provider_of(proxy)
        return cache[proxy]
    return provider_of


def is_hosting(proxy, provider_of):
    return bool(provider_of and (provider_of(proxy) or {}).get('hosting'))


def proxy_protocol(proxy):
    scheme = str(proxy).partition('://')[0]
    return PROTOCOL_ALIASES.get(scheme, scheme)


def matches_selection(row, protocol='all', max_latency=None, countries=(), country_of=None,
                      exclude_hosting=False, provider_of=None):
    """Selection by protocol, maximum median latency (ms), country codes and hosting providers."""
    if protocol not in (None, 'all') and proxy_protocol(row.get('proxy', '')) != protocol:
        return False
    if exclude_hosting and is_hosting(row.get('proxy', ''), provider_of):
        return False
    if countries and row_country(row, country_of) not in countries:
        return False
    if max_latency:
        latency = row.get('latency_ms')
        if latency is None or latency > max_latency:
            return False
    return True


def export(db, profile, directory, *, top=0, sort='quality', min_success=2/3, denylist=None, local_override=None, min_anonymity='any',
           protocol='all', max_latency=None, countries=(), country_of=None, exclude_hosting=False, provider_of=None,
           watch_minutes=0, allowed_proxies=None, run_state=None, diagnostic=False, query='', scope_digest='default'):
    if not db.execute('SELECT 1 FROM profiles WHERE id=?', (profile,)).fetchone():
        raise ValueError('Профиль проверки не найден')
    denylist = denylist or Denylist.empty()
    selection_requested = None
    if allowed_proxies is not None:
        try:
            requested = list(allowed_proxies)
        except TypeError:
            raise ValueError('Некорректный список выбранных прокси.') from None
        if not 1 <= len(requested) <= 1000:
            raise ValueError('Выберите от 1 до 1000 прокси.')
        normalized = [normalize(value) for value in requested]
        if any(value is None for value in normalized):
            raise ValueError('Выбранный список содержит неподдерживаемый адрес или credentials.')
        selection_requested = set(normalized)
    if not isinstance(query, str) or len(query) > 100:
        raise ValueError('Некорректный поиск экспорта.')
    query = query.strip().lower()
    cfg = json.loads(db.execute('SELECT config FROM profiles WHERE id=?', (profile,)).fetchone()[0])
    policy = cfg.get('reputation', {})
    strict = bool(policy.get('strict', False))
    # A minimum anonymity level only applies to profiles that asked a judge.
    if not cfg.get('anonymity'):
        min_anonymity = 'any'
    if local_override is False:
        active_denylist = None
    elif local_override is True or policy.get('local_enabled', True):
        active_denylist = denylist
    else:
        active_denylist = None
    if active_denylist is not None and active_denylist.error:
        raise ValueError('Не удалось прочитать локальный denylist; экспорт остановлен.')
    directory = Path(directory)
    excluded_scope = scope_excluded(db, scope_digest or 'default')
    directory.mkdir(parents=True, exist_ok=True)
    published_at = time.time()
    generations = directory/'generations'
    generations.mkdir(exist_ok=True)
    _, failed, remaining = prune_export_generations(directory)
    if failed and remaining >= EXPORT_GENERATION_RETENTION:
        raise RuntimeError('Закрытые старые export generations не удаляются; повторите после завершения загрузок.')
    generation = Path(tempfile.mkdtemp(prefix='.generation-', dir=generations))
    published = False
    # Keep full samples on disk, including when hundreds of thousands pass.
    db.execute('DROP TABLE IF EXISTS temp.export_rank')
    db.execute('CREATE TEMP TABLE export_rank(proxy TEXT PRIMARY KEY, score REAL, latency REAL, reliability REAL, '
               'jitter REAL, uptime REAL, checks INTEGER, bandwidth REAL, recommended REAL)')
    stored_checked = passed = local_filtered = 0
    sources = dict(db.execute('SELECT proxy, source FROM candidate_meta WHERE source IS NOT NULL'))
    source_quality = {}
    status_counts = {'clean': 0, 'listed': 0, 'unknown': 0, 'local_denied': 0}
    anonymity_counts = {}
    breakdown = {'protocols': {}, 'countries': {}}
    recommend_later = []
    selection_eligible = set()

    def in_scope(proxy):
        if proxy in excluded_scope:
            return False
        if protocol not in (None, 'all') and proxy_protocol(proxy) != protocol:
            return False
        if countries and (country_of(proxy) if country_of else None) not in countries:
            return False
        return not (exclude_hosting and is_hosting(proxy, provider_of))

    scope_candidates = sum(1 for (proxy,) in db.execute('SELECT proxy FROM candidates') if in_scope(proxy))
    for (payload,) in db.execute('SELECT payload FROM results WHERE profile=?', (profile,)):
        row = json.loads(payload)
        if not in_scope(row.get('proxy', '')):
            continue
        stored_checked += 1
        status = (row.get('reputation') or {}).get('status', 'clean')
        status_counts[status] = status_counts.get(status, 0) + 1
        level = (row.get('anonymity') or {}).get('level')
        if level:
            anonymity_counts[level] = anonymity_counts.get(level, 0) + 1
        stamp_freshness(row, watch_minutes, published_at)
        eligible = (row_fresh(row, published_at)
                    and result_allowed(row, min_success, denylist=None, strict=strict, min_anonymity=min_anonymity)
                    and matches_selection(row, protocol, max_latency, countries, country_of, exclude_hosting, provider_of)
                    and (not query or query in row.get('proxy', '').lower()))
        if eligible and active_denylist is not None and active_denylist.match(row.get('proxy', '')):
            local_filtered += 1
            eligible = False
        if eligible:
            passed += 1
            selection_eligible.add(row['proxy'])
            if selection_requested is None or row['proxy'] in selection_requested:
                history = row_history(row, min_success)
                for group, value in (('protocols', proxy_protocol(row['proxy'])),
                                     ('countries', row_country(row, country_of) or '??')):
                    breakdown[group][value] = breakdown[group].get(value, 0) + 1
                db.execute('INSERT INTO export_rank VALUES (?,?,?,?,?,?,?,?,NULL)',
                           (row['proxy'], row['score'], row['latency_ms'], row['reliability'], row.get('jitter_ms'),
                            history['passes'] / history['checks'], history['checks'],
                            (row.get('speed') or {}).get('mbps')))
                recommend_later.append(row)
        quality = source_quality.setdefault(sources.get(row.get('proxy'), 'unknown'), {'checked': 0, 'passed': 0})
        quality['checked'] += 1
        quality['passed'] += int(eligible)
    # Source records are complete only after the loop, so recommended scores come second.
    listed = listed_counts(db)
    recommended = recommender(source_quality, listed, sources, min_success)
    db.executemany('UPDATE export_rank SET recommended=? WHERE proxy=?',
                   ((recommended(row), row['proxy']) for row in recommend_later))
    recommend_later.clear()
    order = EXPORT_ORDERS[sort]
    selected = db.execute(f"""SELECT r.payload FROM export_rank e JOIN results r
        ON r.proxy=e.proxy AND r.profile=? ORDER BY {order} LIMIT ?""", (profile, top or -1))
    fields = ['proxy', 'score', 'latency_ms', 'jitter_ms', 'reliability', 'min_target_reliability',
              'successes', 'requests', 'checked_at', 'valid_until', 'reputation_status', 'reputation_sources',
              'anonymity', 'anonymity_signals', 'country', 'exit_ip', 'exit_country', 'asn', 'provider', 'hosting', 'mbps', 'listed_in', 'recommended', 'checks', 'passes']
    names = ['proxies.txt', 'ranked.json', 'ranked.csv', *PROTOCOL_EXPORTS.values(), 'hostport.txt', 'proxychains.txt',
             'proxy.pac', 'clash.yaml', 'singbox.json']
    best = []
    exported = 0
    exported_valid_until = []
    try:
        with (generation/'proxies.txt').open('w', encoding='utf-8') as txt, \
             (generation/'ranked.json').open('w', encoding='utf-8') as js, \
             (generation/'ranked.csv').open('w', encoding='utf-8', newline='') as csv_file, \
             (generation/'hostport.txt').open('w', encoding='utf-8') as hostport, \
             (generation/'proxychains.txt').open('w', encoding='utf-8') as chains, \
             contextlib.ExitStack() as stack:
            chains.write('# Paste into the [ProxyList] section of proxychains.conf (HTTPS proxies are not supported there).\n')
            # host:port lists per protocol, the format most proxy-consuming tools expect.
            by_protocol = {scheme: stack.enter_context((generation/name).open('w', encoding='utf-8'))
                           for scheme, name in PROTOCOL_EXPORTS.items()}
            writer = csv.DictWriter(csv_file, fieldnames=fields, extrasaction='ignore')
            writer.writeheader()
            js.write('[')
            for (payload,) in selected:
                row = stamp_freshness(json.loads(payload), watch_minutes, published_at)
                verdict = row.get('reputation') or {}
                row['reputation_status'] = verdict.get('status', 'clean')
                row['reputation_sources'] = ','.join(item.get('zone', '') for item in verdict.get('dnsbl', [])
                                                       if item.get('status') == 'listed')
                judged = row.get('anonymity') or {}
                row['country'] = row_country(row, country_of) or ''
                row['exit_ip'] = judged.get('exit_ip', '')
                row['listed_in'] = listed.get(row['proxy'], 1)
                row['recommended'] = recommended(row)
                row['exit_country'] = exit_country(row, country_of) or ''
                provider = (provider_of(row['proxy']) if provider_of else None) or {}
                row['asn'], row['provider'], row['hosting'] = provider.get('asn'), provider.get('org'), provider.get('hosting')
                history = row_history(row, min_success)
                csv_row = dict(row, mbps=(row.get('speed') or {}).get('mbps') or '', anonymity=judged.get('level', ''),
                               anonymity_signals=','.join(judged.get('signals', [])),
                               checks=history['checks'], passes=history['passes'])
                txt.write(row['proxy'] + '\n')
                scheme, _, address = row['proxy'].partition('://')
                protocol_name = proxy_protocol(row['proxy'])
                by_protocol[protocol_name].write(address + '\n')
                hostport.write(address + '\n')
                if protocol_name in PROXYCHAINS_TYPES:
                    host, _, port = address.rpartition(':')
                    chains.write(f'{PROXYCHAINS_TYPES[protocol_name]} {host.strip("[]")} {port}\n')
                js.write((',' if exported else '') + '\n' + json.dumps(row, ensure_ascii=False))
                writer.writerow(csv_row)
                exported += 1
                exported_valid_until.append(row['valid_until'])
                if len(best) < formats.CLASH_LIMIT:
                    best.append(row)
            js.write('\n]\n')
        (generation/'proxy.pac').write_text(formats.pac(row['proxy'] for row in best), encoding='utf-8')
        (generation/'clash.yaml').write_text(formats.clash(best), encoding='utf-8')
        (generation/'singbox.json').write_text(formats.singbox(best), encoding='utf-8')
        total = db.execute('SELECT count(*) FROM candidates').fetchone()[0]
        if run_state:
            checked = max(0, int(run_state.get('checked', stored_checked)))
            scope_candidates = max(0, int(run_state.get('scope_candidates', scope_candidates)))
            snapshot_state = run_state.get('state', 'partial')
            stop_reason = run_state.get('stop_reason', 'stopped')
            reported_passed = max(0, int(run_state.get('passed', passed)))
        else:
            checked = stored_checked
            snapshot_state = 'complete' if checked == scope_candidates else 'partial'
            stop_reason = 'complete' if snapshot_state == 'complete' else 'stopped'
            reported_passed = passed
        if snapshot_state not in ('complete', 'partial', 'error'):
            snapshot_state = 'error' if stop_reason == 'error' else 'partial'
        if stop_reason not in ('complete', 'want_reached', 'recheck_passing', 'stopped', 'error'):
            stop_reason = 'stopped' if snapshot_state != 'complete' else 'complete'
        freshness = freshness_seconds(watch_minutes)
        valid_until = min(exported_valid_until, default=published_at + freshness)
        missing = sorted(selection_requested - selection_eligible) if selection_requested is not None else []
        report = dict(
            schema_version=SNAPSHOT_SCHEMA_VERSION, profile=profile, generation=generation.name,
            state=snapshot_state, stop_reason=stop_reason,
            scope=dict(protocol=protocol, countries=list(countries or ()), exclude_hosting=bool(exclude_hosting)),
            scope_candidates=scope_candidates, checked=checked,
            pending=max(0, scope_candidates - checked), passed=reported_passed,
            candidates=total, local_filtered=local_filtered, exported=exported,
            complete=snapshot_state == 'complete' and checked == scope_candidates,
            generated_at=published_at, valid_until=valid_until, stale=False,
            sort=sort, min_success=min_success, protocol=protocol, max_latency=max_latency,
            countries=list(countries or ()), exclude_hosting=bool(exclude_hosting and provider_of), query=query,
            watch_minutes=float(watch_minutes),
            selection_requested=len(selection_requested) if selection_requested is not None else 0,
            selection_exported=exported if selection_requested is not None else 0,
            selection_missing=missing,
            selection_truncated=max(0, len(selection_eligible) - exported) if selection_requested is not None else 0,
            targets=[dict(name=t.get('name', ''), url=public_url(t['url'])) for t in cfg.get('targets', [])],
            request_profile=cfg.get('request_profile', 'workbench'),
            request_profile_digest=cfg.get('request_profile_digest', ''),
            reputation=dict(policy, counts=status_counts),
            anonymity=dict(enabled=bool(cfg.get('anonymity')), min_level=min_anonymity,
                           counts=anonymity_counts),
            source_quality=source_quality, breakdown=breakdown)
        atomic(generation/'status.json', json.dumps(report, indent=2) + '\n')
        pointer_name = 'diagnostic.json' if diagnostic else 'current.json'
        atomic(directory/pointer_name, json.dumps({'generation':generation.name, 'files':names,
                                                   'state':snapshot_state}, ensure_ascii=False) + '\n')
        published = True
        if not diagnostic:
            # Keep legacy root filenames for scripts that already consume them.
            for name in names:
                temporary = directory/(name+'.tmp')
                shutil.copyfile(generation/name, temporary)
                temporary.replace(directory/name)
            atomic(directory/'status.json', json.dumps(report, indent=2) + '\n')
        prune_export_generations(directory, current=current_generation_name(directory))
    finally:
        selected.close()
        if not published:
            shutil.rmtree(generation, ignore_errors=True)
        db.execute('DROP TABLE temp.export_rank')
        db.commit()
    return report


EPILOG_EN = """examples:
  run --want 20 --country DE,NL --protocol socks5   20 working German/Dutch SOCKS5 proxies, fast
  run --url https://example.org/health              check against your own service
  run --want 50 --watch 30                          find 50 and re-check them every 30 minutes
  get --protocol socks5 --country DE --top 5        print 5 working German SOCKS5 proxies
  test socks5://203.0.113.7:1080 --url https://example.org/   check your own proxies
  serve                                             API on http://127.0.0.1:8765
  gateway --protocol socks5 --country DE            rotating proxy on 127.0.0.1:8899 (HTTP and SOCKS5)

set PROXY_WORKBENCH_LANG=ru for Russian messages."""
EPILOG_RU = """примеры:
  run --want 20 --country DE,NL --protocol socks5   быстро найти 20 рабочих SOCKS5 из Германии/Нидерландов
  run --url https://example.org/health              проверить на своём сервисе
  run --want 50 --watch 30                          найти 50 и перепроверять их каждые 30 минут
  get --protocol socks5 --country DE --top 5        вывести 5 рабочих SOCKS5 из Германии
  test socks5://203.0.113.7:1080 --url https://example.org/   проверить свои прокси
  serve                                             API на http://127.0.0.1:8765
  gateway --protocol socks5 --country DE            ротирующий прокси на 127.0.0.1:8899 (HTTP и SOCKS5)

PROXY_WORKBENCH_LANG=en — сообщения на английском."""


SOURCE_SUBCOMMANDS = ('list', 'show', 'sets', 'set', 'enable', 'disable', 'add', 'remove',
                       'check', 'update', 'recover', 'status', 'exclude-scope')
SOURCE_ACTIONS = ('set', 'enable', 'disable', 'add', 'remove', 'update', 'recover', 'exclude-scope')


def _sources_catalog(args):
    """The accepted remote catalog when one is stored, else the bundled file."""
    path = Path(args.data) / 'source-catalog.json'
    if path.is_file():
        try:
            return source_catalog.load_catalog(path, allow_research=False, allow_unsafe=True)
        except (OSError, ValueError, source_catalog.CatalogError):
            pass
    return source_catalog.load_bundled()


def _sources_read_db(args):
    path = Path(args.data) / 'proxies.sqlite3'
    if not path.is_file():
        return None
    try:
        return sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)
    except sqlite3.Error:
        return None


def _sources_settings(args, parser):
    settings = source_management.read_settings(args.data)
    if settings is None:
        parser.error(tr('Файл gui-settings.json повреждён или недоступен; исправьте его перед продолжением.',
                        'gui-settings.json is damaged or unreadable; fix it before continuing.'))
    return settings


def _sources_save(args, settings, parser):
    try:
        with exclusive_lock(Path(args.data) / 'gui-instance.lock'):
            return source_management.write_settings(args.data, settings)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))


def _sources_out(args, value, lines, parser, found=None):
    """Exit 0 when something matched, 1 when nothing did, 2 on a usage error."""
    if args.format == 'json':
        print(json.dumps(value, ensure_ascii=False, indent=1))
        return 0
    if args.format != 'txt':
        parser.error(tr('Для sources доступен только --format txt или --format json.',
                        'sources supports --format txt or --format json only.'))
    for line in lines:
        print(line, flush=True)
    return 0 if (bool(value) if found is None else found) else 1


def _age_text(seconds):
    if not seconds:
        return tr('нет данных', 'no data')
    if seconds < 3600:
        return tr(f'{int(seconds // 60)} мин назад', f'{int(seconds // 60)} min ago')
    if seconds < 86400:
        return tr(f'{int(seconds // 3600)} ч назад', f'{int(seconds // 3600)} h ago')
    return tr(f'{int(seconds // 86400)} дн назад', f'{int(seconds // 86400)} d ago')


def _source_lines(rows):
    lines = []
    for row in rows:
        flags = []
        if row['selection_state'] == 'selected':
            flags.append(tr('в наборе', 'in set'))
        elif row['selection_state'] == 'disabled':
            flags.append(tr('загрузка выключена', 'download paused'))
        if row['access_group'] in source_management.PROVIDER_GROUPS:
            flags.append(tr(f'условия: {row["access"]}', f'access: {row["access"]}'))
        if not row['collectable']:
            flags.append(tr('не список прокси-адресов', 'not a list of proxy addresses'))
        runtime = row['runtime']
        detail = [row['state']]
        if runtime.get('http_state'):
            detail.append(f"HTTP {runtime['http_state']}")
        if runtime.get('parse_state'):
            detail.append(f"формат {runtime['parse_state']}")
        if runtime.get('cache_state') and runtime['cache_state'] != 'none':
            detail.append(tr(f"кэш {runtime['cache_state']}", f"cache {runtime['cache_state']}"))
        if runtime.get('last_good_age_seconds'):
            detail.append(tr(f"данные {_age_text(runtime['last_good_age_seconds'])}",
                             f"data {_age_text(runtime['last_good_age_seconds'])}"))
        if runtime.get('error'):
            detail.append(runtime['error'])
        contribution = runtime.get('contribution') or {}
        if contribution:
            detail.append(tr(f"принято {contribution.get('accepted')}, уникально в срезе {contribution.get('exclusive')}",
                             f"accepted {contribution.get('accepted')}, unique in this snapshot {contribution.get('exclusive')}"))
        lines.append(f"{row['id']:24} {row['name'][:44]:44} {'; '.join(flags)} | {'; '.join(detail)}")
    return lines


def sources_command(args, parser):
    """Catalog, sets, preview and recovery.  Same ids and words as the GUI."""
    subcommand = args.items[0] if args.items else 'list'
    rest = args.items[1:]
    if subcommand not in SOURCE_SUBCOMMANDS:
        parser.error(tr('Неизвестная команда sources. Доступно: ' + ', '.join(SOURCE_SUBCOMMANDS),
                        'Unknown sources command. Available: ' + ', '.join(SOURCE_SUBCOMMANDS)))
    catalog = _sources_catalog(args)
    settings = _sources_settings(args, parser)
    selection = settings.get('source_selection') or {}
    custom = {item.get('id'): item for item in selection.get('custom_sources', []) if isinstance(item, dict)}

    def require_ids():
        if not rest:
            parser.error(tr('Укажите ID источника.', 'Give a source id.'))
        return list(rest)

    if subcommand in ('list', 'status'):
        query = dict(q=args.source_query, set=args.source_set, category=args.source_category,
                     protocol=args.source_protocol, format=args.source_format_filter,
                     access=args.source_access, state=args.source_state, limit=args.source_limit,
                     offset=args.source_offset)
        db = _sources_read_db(args)
        try:
            runtime = source_management.runtime_snapshot(db)
            view = source_management.build_view(catalog, settings, runtime, query, db=db)
        finally:
            if db is not None:
                db.close()
        if args.format != 'json':
            print(tr(f'Каталог {view["revision"]}, опубликован {view["published_at"]}, записей: {view["total"]}',
                     f'Catalog {view["revision"]}, published {view["published_at"]}, entries: {view["total"]}'), flush=True)
        return _sources_out(args, view, _source_lines(view['sources']), parser, found=bool(view['sources']))
    if subcommand == 'sets':
        view = source_management.sets_view(catalog, selection)
        lines = [f"{item['id']:18} {item['name'][:34]:34} {item['kind']:9} записей: {item['members']}"
                 + (tr(', применён', ', applied') if item['applied'] else '')
                 + (tr(f", в наборе {item['selected_members']}", f", {item['selected_members']} in set") if item['selected_members'] else '')
                 for item in view]
        return _sources_out(args, view, lines, parser)
    if subcommand == 'show':
        source_id = rest[0] if rest else ''
        db = _sources_read_db(args)
        try:
            runtime = source_management.runtime_snapshot(db)
            detail = source_management.detail_view(catalog, settings, source_id, runtime, db)
        finally:
            if db is not None:
                db.close()
        if detail is None:
            parser.error(tr(f'Неизвестный источник: {source_id}', f'Unknown source: {source_id}'))
        lines = [
            f"{detail['id']} — {detail['name']}",
            tr(f"  издатель: {detail['publisher'].get('name') or '—'}", f"  publisher: {detail['publisher'].get('name') or '—'}"),
            tr(f"  категория: {detail['category']}; протоколы: {', '.join(detail['protocols']) or '—'}; формат: {detail['adapter']}",
               f"  category: {detail['category']}; protocols: {', '.join(detail['protocols']) or '—'}; adapter: {detail['adapter']}"),
            tr(f"  условия доступа: {detail['access']}; дата проверки: {detail['checked_at'] or '—'}",
               f"  access: {detail['access']}; checked at: {detail['checked_at'] or '—'}"),
            tr(f"  первоисточник условий: {detail['terms_url'] or '—'}", f"  terms: {detail['terms_url'] or '—'}"),
        ]
        for key, value in detail['evidence'].items():
            lines.append(f"  {key}: {value.get('state')} ({value.get('checked_at') or '—'})")
        lines.append('  ' + tr('прокси проверены: нет — это приложение не проверяет прокси источника',
                                'proxies verified: no — this application does not check a source\'s proxies'))
        return _sources_out(args, detail, lines, parser)
    if subcommand == 'set':
        set_id = rest[0] if rest else ''
        if set_id not in {item['id'] for item in catalog['sets']}:
            parser.error(tr(f'Неизвестный набор: {set_id}', f'Unknown set: {set_id}'))
        result = source_management.apply_set(settings, set_id, catalog)
        saved = _sources_save(args, result, parser)
        ids = saved['source_selection']['selected_ids']
        print(tr(f'Набор {set_id} применён: выбрано {len(ids)} источников. Ничего не добавлялось молча.',
                 f'Set {set_id} applied: {len(ids)} sources selected. Nothing was added implicitly.'), flush=True)
        for source_id in ids:
            print(source_id, flush=True)
        return 0
    if subcommand in ('enable', 'disable'):
        ids = require_ids()
        for source_id in ids:
            if source_catalog.source_by_id(catalog, source_id) is None and source_id not in custom:
                parser.error(tr(f'Неизвестный источник: {source_id}', f'Unknown source: {source_id}'))
        disabled = subcommand == 'disable'
        saved = _sources_save(args, source_management.set_downloads(settings, ids, disabled, catalog), parser)
        print(tr('Загрузка выключена: ' if disabled else 'Загрузка включена: ',
                 'Download paused: ' if disabled else 'Download enabled: ') + ', '.join(ids), flush=True)
        return 0 if ids else 1
    if subcommand == 'remove':
        ids = require_ids()
        saved = _sources_save(args, source_management.remove_sources(settings, ids, catalog), parser)
        print(tr('Убраны из набора (кэш и история сохранены): ', 'Removed from the set (cache and history kept): ')
              + ', '.join(ids), flush=True)
        return 0 if saved else 1
    if subcommand == 'add':
        url = rest[0] if rest else ''
        kind = args.source_format or 'http'
        try:
            descriptor = source_catalog.custom_source(url, kind, allow_unsafe=args.allow_private_sources)
        except source_catalog.CatalogError as exc:
            parser.error(tr(str(exc), str(exc)))
        updated = copy_settings_with_custom(settings, descriptor, catalog)
        saved = _sources_save(args, updated, parser)
        print(tr(f'Источник добавлен: {descriptor["id"]} ({kind}) — {saved["sources"][-1]}',
                 f'Source added: {descriptor["id"]} ({kind}) — {saved["sources"][-1]}'), flush=True)
        return 0
    if subcommand == 'check':
        ids = require_ids()
        db = _sources_read_db(args)
        try:
            plans = []
            for source_id in ids:
                item = source_catalog.source_by_id(catalog, source_id)
                if item is None or not item.get('endpoints'):
                    # A stored custom entry has no endpoint yet; rebuild its plan.
                    item = source_catalog._custom_plan(source_id, custom[source_id], []) if source_id in custom else None
                if item is None:
                    parser.error(tr(f'Неизвестный источник: {source_id}', f'Unknown source: {source_id}'))
                if not source_catalog.collectable_source(item):
                    print(tr(f'{source_id}: не список прокси-адресов, формат не поддерживается сборщиком',
                             f'{source_id}: not a list of proxy addresses, unsupported by the collector'), flush=True)
                    continue
                plans.append(item)
            results = [asyncio.run(preview_collect(db, [plan], args.source_timeout,
                                                   allow_private_sources=args.allow_private_sources))
                       for plan in plans]
        finally:
            if db is not None:
                db.close()
        rows = [source_management.preview_view(report, plan.get('id'), plan.get('name'))
                for report, plan in zip(results, plans)]
        labels = (tr('распознано', 'recognized'), tr('принято', 'accepted'), tr('отклонено', 'rejected'))
        lines = []
        for row in rows:
            parts = [row['http_state'], row['parse_state'],
                     f"{labels[0]} {row['recognized']}", f"{labels[1]} {row['accepted']}",
                     f"{labels[2]} {row['rejected']}"]
            for reason, count in sorted(row['reject_reasons'].items()):
                parts.append(f"{reason}={count}")
            if row['error']:
                parts.append(row['error'])
            if row['truncated']:
                parts.append(tr(f'предпросмотр ограничен: {row["limits"]["max_bytes"]} байт, '
                                f'{row["limits"]["max_candidates"]} записей',
                                f'preview bounded: {row["limits"]["max_bytes"]} bytes, '
                                f'{row["limits"]["max_candidates"]} records'))
            parts.append(tr('прокси не проверялись', 'no proxy was checked'))
            lines.append(f"{row['source_id']:24} " + '; '.join(str(part) for part in parts))
        return _sources_out(args, rows, lines, parser)
    if subcommand == 'update':
        url = os.environ.get('PROXY_WORKBENCH_SOURCES_URL', SOURCES_URL)
        try:
            fetched = asyncio.run(fetch_catalog(url, current=source_catalog.load_bundled(),
                                                timeout=args.source_timeout,
                                                allow_private_sources=args.allow_private_sources))
        except (SourceFetchError, httpx.HTTPError, TimeoutError, OSError, ValueError) as exc:
            reason = str(exc) if isinstance(exc, (SourceFetchError, ValueError)) else type(exc).__name__
            print(tr(f'Каталог не обновлён: {reason}. Локальная копия сохранена.',
                     f'Catalog not updated: {reason}. The local copy is kept.'), file=sys.stderr)
            return 2
        if fetched.get('state') == 'not_modified':
            print(tr('Каталог не изменился на сервере.', 'The published catalog did not change.'), flush=True)
            return 0
        incoming = fetched.get('catalog')
        diff = source_catalog.catalog_diff(_sources_catalog(args), incoming)
        atomic(Path(args.data) / 'source-catalog.json', json.dumps(incoming, ensure_ascii=False))
        selected = set(selection.get('selected_ids', []))
        unselected = [value for value in diff['added'] if value not in selected]
        print(tr(f"Каталог обновлён до {incoming['revision']}: добавлено {len(diff['added'])}, "
                 f"изменено {len(diff['changed'])}, ушло {len(diff['retired'])}.",
                 f"Catalog updated to {incoming['revision']}: {len(diff['added'])} added, "
                 f"{len(diff['changed'])} changed, {len(diff['retired'])} retired."), flush=True)
        print(tr(f'Из них не выбрано: {len(unselected)}. Ничего не включается автоматически — '
                 f'включите нужные источники вручную.',
                 f'Of those, unselected: {len(unselected)}. Nothing is enabled automatically — '
                 f'turn on the ones you need by hand.'), flush=True)
        for source_id in unselected[:50]:
            print(source_id, flush=True)
        return 0
    if subcommand == 'recover':
        ids = require_ids()
        if not (Path(args.data) / 'proxies.sqlite3').is_file():
            print(tr('Локальной базы ещё нет: восстанавливать нечего.', 'No local database yet: nothing to recover.'), flush=True)
            return 0
        with exclusive_lock(Path(args.data) / 'workbench.lock'):
            db = open_db(Path(args.data) / 'proxies.sqlite3')
            try:
                cleared = {source_id: recover_source(db, source_id) for source_id in ids}
            finally:
                db.close()
        print(tr('Снята пауза и карантин (кэш и последние данные сохранены): ',
                 'Backoff and quarantine cleared (cache and last data kept): '), flush=True)
        for source_id in ids:
            print(f'{source_id:24} {cleared[source_id]}', flush=True)
        return 0
    # exclude-scope
    source_id = rest[0] if rest else ''
    if not source_id:
        parser.error(tr('Укажите ID источника.', 'Give a source id.'))
    if not args.yes:
        parser.error(tr('exclude-scope требует явного --yes', 'exclude-scope needs an explicit --yes'))
    if source_catalog.source_by_id(catalog, source_id) is None and source_id not in custom:
        parser.error(tr(f'Неизвестный источник: {source_id}', f'Unknown source: {source_id}'))
    with exclusive_lock(Path(args.data) / 'workbench.lock'):
        db = open_db(Path(args.data) / 'proxies.sqlite3')
        try:
            addresses = source_management.source_addresses(db, source_id, exclusive=not args.include_shared)
            total = source_management.source_addresses(db, source_id, exclusive=False)
            digest = scope_digest_for(settings['protocol'], settings['countries'].split(','),
                                      settings['max_latency'], settings['exclude_hosting'])
            written = exclude_scope(db, addresses, reason='source', source_id=source_id, scope_digest=digest)
        finally:
            db.close()
    print(tr(f'Исключено из текущего scope: {written} из {len(total)} адресов источника {source_id}. '
             f'Данные и provenance сохранены; отменить: удалить scope_exclusions.',
             f'Excluded from the current scope: {written} of {len(total)} addresses delivered by {source_id}. '
             f'Data and provenance are kept; undo by clearing the scope exclusions.'), flush=True)
    return 0


def copy_settings_with_custom(settings, descriptor, catalog=None):
    """Add one user URL to the selection; the adapter choice is explicit."""
    result = copy.deepcopy(settings)
    selection = copy.deepcopy(result.get('source_selection') or {})
    custom = {item.get('id'): item for item in selection.get('custom_sources', []) if isinstance(item, dict)}
    custom[descriptor['id']] = {'id': descriptor['id'], 'url': descriptor['url'],
                                'name': descriptor['url'], 'adapter': descriptor['adapter']}
    selection['custom_sources'] = list(custom.values())
    selection['selected_ids'] = list(dict.fromkeys(list(selection.get('selected_ids', [])) + [descriptor['id']]))
    selection['download_disabled_ids'] = [value for value in selection.get('download_disabled_ids', [])
                                          if value != descriptor['id']]
    result['source_selection'] = selection
    result['settings_version'] = 3
    return source_catalog.migrate_settings(result, catalog)


def parser():
    # Installed and `python -m` runs show the command people actually type.
    prog = None if Path(sys.argv[0]).name == 'proxytool.py' else 'proxy-workbench'
    p = argparse.ArgumentParser(prog=prog, formatter_class=argparse.RawDescriptionHelpFormatter, epilog=tr(EPILOG_RU, EPILOG_EN),
                                description=tr(f'{PRODUCT_NAME}: сбор и полная проверка публичных прокси под HTTP-сервис', f'{PRODUCT_NAME}: collect public proxies and fully check them against your HTTP services'))
    p.add_argument('--version', action='version', version=f'{PRODUCT_NAME} {PRODUCT_VERSION}')
    p.add_argument('command', choices=['collect', 'scan', 'run', 'export', 'get', 'test', 'serve', 'gateway', 'clear-data', 'update-geoip', 'sources'],
                   help=tr('run — собрать и проверить; collect — только собрать; scan — только проверить; '
                           'export — пересобрать файлы; get — вывести готовые прокси; test — проверить свои прокси; '
                           'serve — локальное API; gateway — ротирующий прокси; '
                           'update-geoip — база стран; '
                           'clear-data — удалить результаты; '
                           'sources — каталог источников, наборы, проверка и восстановление',
                           'run: collect and check; collect: only collect; scan: only check; '
                           'export: rebuild the files; get: print working proxies; test: check given proxies; '
                           'serve: local API; gateway: rotating proxy; '
                           'update-geoip: country database; '
                           'clear-data: delete results; '
                           'sources: source catalog, sets, availability check and recovery'))
    p.add_argument('items', nargs='*', metavar='PROXY',
                   help=tr('test: прокси для проверки, например socks5://1.2.3.4:1080', 'test: proxies to check, e.g. socks5://1.2.3.4:1080'))
    p.add_argument('--format', choices=['txt', 'hostport', 'json'], default='txt',
                   help=tr('get: формат вывода', 'get: output format'))
    p.add_argument('--random', action='store_true', help=tr('get: в случайном порядке', 'get: in random order'))
    p.add_argument('--yes', action='store_true', help=tr('подтвердить удаление локальных результатов', 'confirm deleting local results'))
    p.add_argument('--progress-file', type=Path, help=argparse.SUPPRESS)
    p.add_argument('--stop-file', type=Path, help=argparse.SUPPRESS)
    p.add_argument('--selection-file', type=Path, help=argparse.SUPPRESS)
    p.add_argument('--export-query', default='', help=argparse.SUPPRESS)
    p.add_argument('--export-hosting', choices=('', 'hide'), default='', help=argparse.SUPPRESS)
    p.add_argument('--data', type=Path, default=paths.default_data(),
                   help=tr('папка для базы, настроек и экспорта', 'folder for the database, settings and exports'))
    p.add_argument('--input', action='append', default=[], help=tr('локальный список прокси; можно повторять', 'local proxy list file; can be repeated'))
    p.add_argument('--sources', type=Path, default=ROOT / 'sources.json', help=tr('JSON-массив URL текстовых списков', 'JSON array of source list URLs'))
    p.add_argument('--detect-protocols', action='store_true',
                   help=tr('адреса без протокола из --input пробовать как HTTP, SOCKS4 и SOCKS5',
                           'try addresses without a protocol from --input as HTTP, SOCKS4 and SOCKS5'))
    p.add_argument('--no-sources', action='store_true', help=tr('не загружать публичные списки', 'do not download public lists'))
    p.add_argument('--source-timeout', type=float, default=60, help=tr('таймаут загрузки списка, секунд', 'list download timeout, seconds'))
    p.add_argument('--allow-private-sources', action='store_true',
                   help=tr('разрешить loopback/private/link-local/reserved/metadata источники (только для локальных mock-сервисов)', 'allow loopback/private/link-local/reserved/metadata sources (local mock services only)'))
    p.add_argument('--source-max-bytes', '--max-source-bytes', dest='source_max_bytes', type=int,
                   default=DEFAULT_SOURCE_MAX_BYTES, help=tr('максимум байт одного удалённого источника', 'maximum bytes per remote source'))
    p.add_argument('--source-max-line-bytes', '--max-source-line-bytes', dest='source_max_line_bytes', type=int,
                   default=DEFAULT_SOURCE_MAX_LINE_BYTES, help=tr('максимум байт строки списка', 'maximum bytes per list line'))
    p.add_argument('--source-max-candidates', '--max-source-candidates', dest='source_max_candidates', type=int,
                   default=DEFAULT_SOURCE_MAX_CANDIDATES, help=tr('максимум кандидатов одного источника', 'maximum candidates per source'))
    p.add_argument('--source-max-redirects', '--max-source-redirects', dest='source_max_redirects', type=int,
                   default=DEFAULT_SOURCE_MAX_REDIRECTS, help=tr('максимум redirect hops одного запроса', 'maximum redirect hops per request'))
    target = p.add_mutually_exclusive_group()
    target.add_argument('--url', help=tr('свой URL проверки; по умолчанию https://example.com/', 'your URL to check against; default https://example.com/'))
    target.add_argument('--config', type=Path, help=tr('JSON с targets, HTTP-кодами и проверкой содержимого', 'JSON with targets, status codes and content checks'))
    p.add_argument('--request-profile', choices=sorted(REQUEST_PROFILES), default=None,
                   help=tr('нейтральный HTTP request-профиль', 'neutral HTTP request profile'))
    p.add_argument('--attempts', type=int, default=3, help=tr('попыток на каждый сервис', 'attempts per service'))
    p.add_argument('--timeout', type=float, default=8, help=tr('полный deadline одного запроса, секунд', 'full deadline of one request, seconds'))
    p.add_argument('--connect-timeout', type=float, default=4,
                   help=tr('лимит на подключение к прокси, секунд; мёртвые адреса отсеиваются быстрее', 'limit for connecting to a proxy, seconds; dead addresses are dropped sooner'))
    p.add_argument('--fail-fast', dest='fail_fast', action='store_true', default=True,
                   help=tr('прекращать попытки, когда прокси уже не может пройти порог (по умолчанию)', 'stop the attempts once a proxy can no longer pass (default)'))
    p.add_argument('--no-fail-fast', dest='fail_fast', action='store_false',
                   help=tr('всегда выполнять все попытки', 'always run every attempt'))
    p.add_argument('--workers', type=int, default=128, help=tr('одновременных проверок', 'parallel checks'))
    p.add_argument('--prefilter', type=int, default=512,
                   help=tr('сколько адресов одновременно проверять быстрым TCP-подключением до полной проверки; 0 — выключено',
                           'parallel quick TCP connects that drop dead addresses before the full check; 0 = off'))
    p.add_argument('--prefilter-timeout', type=float, default=3,
                   help=tr('таймаут быстрого TCP-подключения, секунд', 'timeout of the quick TCP connect, seconds'))
    p.add_argument('--rate', type=float, default=100, help=tr('максимум стартов запросов/с, 0 — без лимита', 'maximum request starts per second, 0 = unlimited'))
    p.add_argument('--max-bytes', type=int, default=1048576, help=tr('максимум байт ответа', 'maximum response bytes'))
    p.add_argument('--denylist-file', type=Path, default=None, help=tr('локальный IP/CIDR/proxy denylist', 'local IP/CIDR/proxy denylist file'))
    p.add_argument('--local-denylist', dest='local_denylist', action='store_true', default=None,
                   help=tr('применять локальный denylist', 'apply the local denylist'))
    p.add_argument('--no-local-denylist', dest='local_denylist', action='store_false', default=None,
                   help=tr('не применять локальный denylist', 'do not apply the local denylist'))
    p.add_argument('--dnsbl', dest='dnsbl', action='store_true', default=None,
                   help=tr('включить публичные DNSBL-проверки', 'enable public DNSBL checks'))
    p.add_argument('--dnsbl-zone', dest='dnsbl_zones', action='append', default=[],
                   help=tr('DNSBL-зона; можно указать несколько раз', 'DNSBL zone; can be repeated'))
    p.add_argument('--reputation-timeout', type=float, default=None, help=tr('таймаут одной DNSBL-зоны, секунд', 'timeout per DNSBL zone, seconds'))
    p.add_argument('--strict-clean', dest='strict_clean', action='store_true', default=None,
                   help=tr('не разрешать прокси с неопределённым DNSBL-результатом', 'reject proxies with an unknown DNSBL result'))
    p.add_argument('--speedtest-url',
                   help=tr('файл для замера скорости через каждый рабочий прокси (Мбит/с), например '
                           'https://speed.cloudflare.com/__down?bytes=5000000',
                           'file downloaded through every working proxy to measure Mbit/s, e.g. '
                           'https://speed.cloudflare.com/__down?bytes=5000000'))
    p.add_argument('--speedtest-bytes', type=int, default=SPEEDTEST_BYTES,
                   help=tr('сколько байт скачивать при замере скорости', 'bytes to download for the speed test'))
    p.add_argument('--judge-url', help=tr('echo-endpoint для проверки анонимности (transparent/anonymous/elite)', 'echo endpoint for the anonymity check (transparent/anonymous/elite)'))
    p.add_argument('--min-anonymity', choices=anonymity.MIN_LEVELS, default='any',
                   help=tr('минимальный уровень анонимности для экспорта; нужен --judge-url', 'minimum anonymity level for the export; needs --judge-url'))
    p.add_argument('--recheck', action='store_true', help=tr('заново проверить все адреса текущего профиля', 'check every address of the current profile again'))
    p.add_argument('--recheck-passing', action='store_true',
                   help=tr('заново проверить только прокси, которые сейчас проходят отбор (быстро освежить список)', 're-check only the proxies that currently pass (quick refresh)'))
    p.add_argument('--watch', type=float, default=0,
                   help=tr('после проверки перепроверять рабочие прокси каждые N минут и обновлять экспорт; 0 — выключено', 'after the scan, re-check working proxies every N minutes and refresh the export; 0 = off'))
    p.add_argument('--top', type=int, default=0, help=tr('сколько сохранить; 0 — все прошедшие', 'how many to save; 0 = all that pass'))
    p.add_argument('--sort', choices=list(SORTS), default='recommended',
                   help=tr('quality: стабильность + скорость; speed: задержка; stability: разброс; uptime: живучесть; '
                           'bandwidth: Мбит/с (нужен --speedtest-url); recommended: качество + живучесть + '
                           'редкость в списках + надёжность источника',
                           'quality: reliability + speed; speed: latency; stability: jitter; uptime: survived re-checks; '
                           'bandwidth: Mbit/s (needs --speedtest-url); recommended: quality + survival + '
                           'rarity across lists + source track record'))
    p.add_argument('--protocol', choices=list(PROTOCOLS), default='all', help=tr('экспортировать только этот протокол', 'check and export only this protocol'))
    p.add_argument('--max-latency', type=float, default=0, help=tr('максимальная медианная задержка, мс; 0 — без ограничения', 'maximum median latency, ms; 0 = no limit'))
    p.add_argument('--country', default='', help=tr('только эти страны (ISO-коды через запятую, например DE,NL); '
                   'другие адреса не проверяются', 'only these countries (ISO codes, e.g. DE,NL); '
                   'other addresses are not checked'))
    p.add_argument('--no-hosting', action='store_true',
                   help=tr('пропускать адреса хостинг-провайдеров и дата-центров (нужна база провайдеров: update-geoip)',
                           'skip addresses of hosting providers and data centres (needs the provider database: update-geoip)'))
    p.add_argument('--want', type=int, default=0,
                   help=tr('остановиться, когда найдено столько подходящих прокси; 0 — проверить все', 'stop once this many matching proxies are found; 0 = check everything'))
    p.add_argument('--geoip-db', type=Path, default=None,
                   help=tr('CSV-база DB-IP Country Lite; по умолчанию data/geoip/' + geoip.DB_NAME, 'DB-IP Country Lite CSV; default data/geoip/' + geoip.DB_NAME))
    p.add_argument('--min-success', type=float, default=2/3, help=tr('минимальная доля успехов КАЖДОГО target, 0..1', 'minimum success share for EACH target, 0..1'))
    p.add_argument('--host', default='127.0.0.1', help=tr('serve: адрес локального API; по умолчанию только этот компьютер', 'serve: API address; default is this computer only'))
    p.add_argument('--port', type=int, default=None,
                   help=tr('serve/gateway: порт; по умолчанию 8765 для API и 8899 для шлюза',
                           'serve/gateway: port; default 8765 for the API and 8899 for the gateway'))
    p.add_argument('--rotate', choices=['round-robin', 'random'], default='round-robin',
                   help=tr('gateway: порядок выбора прокси', 'gateway: how the next proxy is chosen'))
    p.add_argument('--max-per-proxy', type=int, default=0,
                   help=tr('gateway: максимум одновременных соединений через один прокси; 0 — без лимита',
                           'gateway: most simultaneous connections through one proxy; 0 = no limit'))
    p.add_argument('--session-ttl', type=float, default=10,
                   help=tr('gateway: сколько минут сессия (session-… в имени пользователя) держит один прокси',
                           'gateway: minutes a session (session-… in the user name) keeps one proxy'))
    p.add_argument('--source-query', default='', help=tr('sources list: поиск по названию, ID, издателю или хосту',
                                                         'sources list: search by name, id, publisher or host'))
    p.add_argument('--source-set', default='', help=tr('sources list: фильтр по набору', 'sources list: filter by set'))
    p.add_argument('--source-category', default='', help=tr('sources list: фильтр по категории', 'sources list: filter by category'))
    p.add_argument('--source-protocol', default='', help=tr('sources list: фильтр по протоколу', 'sources list: filter by protocol'))
    p.add_argument('--source-format-filter', default='', help=tr('sources list: фильтр по формату данных',
                                                                 'sources list: filter by data format'))
    p.add_argument('--source-access', default='', help=tr('sources list: фильтр по условиям доступа',
                                                         'sources list: filter by access conditions'))
    p.add_argument('--source-state', default='', choices=list(source_management.FILTER_STATES),
                   help=tr('sources list: фильтр по состоянию проверки', 'sources list: filter by observed state'))
    p.add_argument('--source-limit', type=int, default=50, help=tr('sources list: сколько строк показать',
                                                                   'sources list: how many rows to show'))
    p.add_argument('--source-offset', type=int, default=0, help=tr('sources list: смещение', 'sources list: offset'))
    p.add_argument('--source-format', default='http', choices=list(source_catalog.USER_SOURCE_FORMATS),
                   help=tr('sources add: формат данных по этому адресу',
                           'sources add: the data format at that address'))
    p.add_argument('--include-shared', action='store_true',
                   help=tr('sources exclude-scope: включить адреса, которые дают и другие источники',
                           'sources exclude-scope: include addresses other sources also offer'))
    p.add_argument('--api-token', default=os.environ.get('PROXY_WORKBENCH_API_TOKEN') or None,
                   help=tr('serve: токен доступа к API (или переменная PROXY_WORKBENCH_API_TOKEN); '
                        'обязателен, если API слушает не loopback-адрес', 'serve: API access token (or PROXY_WORKBENCH_API_TOKEN); '
                        'required when the API listens on a non-loopback address'))
    return p


async def stoppable(coro, stop_file):
    task = asyncio.create_task(coro)
    try:
        while not task.done():
            if stop_file and stop_file.exists():
                task.cancel()
                break
            await asyncio.wait({task}, timeout=0.25)
        return await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def download_geoip(path, timeout=60, url=None, validate=None):
    """Fetch the latest DB-IP Lite CSV (country by default); returns the month downloaded."""
    error = None
    url = url or geoip.DOWNLOAD_URL
    validate = validate or geoip.validate_download
    async with httpx.AsyncClient(trust_env=False, verify=TLS, timeout=timeout, follow_redirects=True) as client:
        for month in geoip.candidate_months():
            async with client.stream('GET', url.format(month=month)) as response:
                if response.status_code == 404:
                    error = 'HTTP_404'
                    continue
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > geoip.MAX_DOWNLOAD_BYTES:
                        raise ValueError('GEOIP_TOO_LARGE')
            validate(bytes(body))
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_name(path.name + '.tmp')
            temp.write_bytes(body)
            temp.replace(path)
            return month
    raise ValueError(error or 'GEOIP_UNAVAILABLE')


def serve(args):
    """Read-only HTTP API over the latest export; runs next to scans without the data lock."""
    from . import api
    port = api.DEFAULT_PORT if args.port is None else args.port
    if not 0 <= port <= 65535:
        print(tr('Неверный порт API', 'Invalid API port'), file=sys.stderr)
        return 2
    try:
        server = api.make_api_server(args.data, args.host, port, args.api_token)
    except (ValueError, OSError) as exc:
        print(tr(f'API не запущено: {exc}', f'API not started: {exc}'), file=sys.stderr)
        return 2
    shown = f'[{args.host}]' if ':' in args.host else args.host
    base = f'http://{shown}:{server.server_port}'
    print(tr(f'API: {base}  (Ctrl+C — остановить)', f'API: {base}  (Ctrl+C to stop)'), flush=True)
    print(f'  {base}/proxies?protocol=socks5&country=DE&limit=10&format=txt', flush=True)
    print(f'  {base}/random?max_latency=1500', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def run_gateway(args, countries):
    """Rotating local proxy over the latest export; like serve, it never takes the data lock."""
    from . import gateway
    port = gateway.DEFAULT_PORT if args.port is None else args.port
    if not 0 <= port <= 65535:
        print(tr('Неверный порт шлюза', 'Invalid gateway port'), file=sys.stderr)
        return 2
    filters = dict(protocol=args.protocol, countries=countries, anonymity=args.min_anonymity,
                   max_latency=args.max_latency)

    async def run():
        server = await gateway.start(args.data, args.host, port, args.api_token, filters, args.rotate,
                                     max(0, args.max_per_proxy), max(0.0, args.session_ttl) * 60)
        pool = server.gateway.pool
        shown = f'[{args.host}]' if ':' in args.host else args.host
        address = f'{shown}:{server.sockets[0].getsockname()[1]}'
        print(tr(f'Ротирующий прокси: {address} (HTTP и SOCKS5), в пуле {len(pool.refresh())} прокси. Ctrl+C — остановить.',
                 f'Rotating proxy: {address} (HTTP and SOCKS5), {len(pool.refresh())} proxies in the pool. Ctrl+C to stop.'),
              flush=True)
        print(f'  curl -x http://{address} https://example.org/', flush=True)
        print(f'  curl -x http://country-de-session-1:x@{address} https://example.org/', flush=True)
        print(f'  curl http://{address}/status', flush=True)
        async with server:
            await server.serve_forever()
    try:
        asyncio.run(run())
    except (ValueError, OSError) as exc:
        print(tr(f'Шлюз не запущен: {exc}', f'Gateway not started: {exc}'), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        pass
    return 0


def print_proxies(args, countries):
    """Working proxies from the latest export for shell scripts; like serve, no data lock."""
    from . import api
    rows, _ = api.Exports(args.data / 'exports').load()
    query = dict(protocol=args.protocol, countries=countries, anonymity=args.min_anonymity,
                 max_latency=args.max_latency, min_mbps=0, hosting='0' if args.no_hosting else '')
    selected = api.select(rows, query)
    if args.random:
        random.shuffle(selected)
    if args.top:
        selected = selected[:args.top]
    if not selected:
        print(tr('Нет подходящих прокси: запустите проверку или ослабьте фильтры.',
                 'No matching proxies: run a check or relax the filters.'), file=sys.stderr)
        return 1
    if args.format == 'json':
        print(json.dumps(selected, ensure_ascii=False, indent=1))
    else:
        for row in selected:
            print(row['proxy'] if args.format == 'txt' else row['proxy'].partition('://')[2])
    return 0


def test_proxies(args):
    """Check proxies given on the command line against the usual targets; nothing is stored."""
    proxies = []
    for value in args.items:
        proxy = normalize(value)
        if not proxy:
            print(tr(f'Пропущено: {value} (нужен публичный IP и порт)', f'Skipped: {value} (a public IP and port are needed)'),
                  file=sys.stderr)
            continue
        proxies.append(proxy)
    if not proxies:
        print(tr('Укажите прокси, например: test socks5://203.0.113.7:1080', 'Give proxies, e.g.: test socks5://203.0.113.7:1080'),
              file=sys.stderr)
        return 2
    config = target_config(args)

    async def run():
        rate = Rate(args.rate)
        return await asyncio.gather(*(check_proxy(proxy, config, rate) for proxy in proxies))
    passed = 0
    for row in asyncio.run(run()):
        ok = result_allowed(row, args.min_success)
        passed += ok
        latency = f"{row['latency_ms']:.0f} ms" if row.get('latency_ms') is not None else '—'
        errors = sorted({sample['error'] for sample in row['samples'] if sample.get('error')})
        speed = f"; {row['speed']['mbps']} Mbit/s" if (row.get('speed') or {}).get('mbps') else ''
        print(f"{'OK  ' if ok else 'FAIL'} {row['proxy']}  {row['successes']}/{row['requests']}  {latency}{speed}"
              + (f"  {', '.join(errors)}" if errors else ''))
    return 0 if passed == len(proxies) else 1


def read_selection_file(path):
    """Read and remove the short-lived GUI allowlist passed to an export worker."""
    path = Path(path)
    try:
        if path.stat().st_size > 1_000_000:
            raise ValueError('Слишком большой список выбранных прокси.')
        values = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        raise ValueError('Файл выбранных прокси не найден.') from None
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError('Не удалось прочитать список выбранных прокси.') from None
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    if not isinstance(values, list) or not 1 <= len(values) <= 1000:
        raise ValueError('Выберите от 1 до 1000 прокси.')
    normalized = [normalize(value) for value in values]
    if any(value is None for value in normalized):
        raise ValueError('Выбранный список содержит неподдерживаемый адрес или credentials.')
    return list(dict.fromkeys(normalized))


def main(argv=None):
    utf8_output()
    p = parser()
    args = p.parse_args(argv)
    if (min(args.attempts, args.workers, args.max_bytes) < 1 or args.top < 0
            or not math.isfinite(args.rate) or args.rate < 0
            or not math.isfinite(args.timeout) or args.timeout <= 0
            or not math.isfinite(args.connect_timeout) or args.connect_timeout <= 0
            or not math.isfinite(args.max_latency) or args.max_latency < 0
            or not math.isfinite(args.source_timeout) or args.source_timeout <= 0
            or not 1 <= args.source_max_bytes <= MAX_SOURCE_BYTES
            or not 1 <= args.source_max_line_bytes <= MAX_SOURCE_LINE_BYTES
            or not 1 <= args.source_max_candidates <= MAX_SOURCE_CANDIDATES
            or not 0 <= args.source_max_redirects <= MAX_SOURCE_REDIRECTS
            or not 0 <= args.min_success <= 1 or args.want < 0 or args.prefilter < 0
            or not math.isfinite(args.prefilter_timeout) or args.prefilter_timeout <= 0
            or not math.isfinite(args.watch) or args.watch < 0):
        p.error(tr('Неверные числовые параметры', 'Invalid numeric options'))
    try:
        countries = geoip.parse_countries(args.country)
    except ValueError as exc:
        p.error(tr(str(exc), 'Countries: use two-letter ISO codes, for example DE,NL.'))
    if args.command != 'export' and (args.selection_file is not None or args.export_query or args.export_hosting):
        p.error('--selection-file/--export-query/--export-hosting доступны только для export')
    if len(args.export_query) > 100:
        p.error('Поиск экспорта слишком длинный: максимум 100 символов.')
    export_query = args.export_query.strip().lower()
    allowed_proxies = None
    if args.selection_file is not None:
        if args.command != 'export':
            p.error('--selection-file доступен только для export')
        try:
            allowed_proxies = read_selection_file(args.selection_file)
        except ValueError as exc:
            p.error(str(exc))
    os.umask(0o077)
    args.data.mkdir(parents=True, exist_ok=True)
    if args.command == 'sources':
        return sources_command(args, p)
    if args.command == 'serve':
        return serve(args)
    if args.command == 'gateway':
        return run_gateway(args, countries)
    if args.command == 'get':
        return print_proxies(args, countries)
    if args.command == 'test':
        return test_proxies(args)
    denylist_path = args.denylist_file or args.data / 'denylist.txt'
    denylist = Denylist.from_file(denylist_path, normalizer=normalize)
    collect_denylist = Denylist.empty() if args.local_denylist is False else denylist
    # Exclusive OS lock is released even after a crash; read-only exports also lock.
    lock = (args.data / 'workbench.lock').open('a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            lock.write(b'0'); lock.flush(); lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(tr('Эта папка data уже используется другим запуском.', 'This data folder is already used by another run.'), file=sys.stderr)
        lock.close()
        return 2
    if args.command == 'clear-data':
        if not args.yes:
            p.error(tr('clear-data требует явного --yes', 'clear-data needs an explicit --yes'))
        try:
            with exclusive_lock(args.data/'gui-instance.lock'):
                removed = clear_runtime(args.data, keep_lock=True)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            lock.close()
            return 2
        print(tr('Удалено: ', 'Deleted: ') + (', '.join(removed) if removed else tr('ничего', 'nothing')), flush=True)
        lock.close()
        return 0
    geo_path = args.geoip_db or geoip.default_path(args.data)
    if args.command == 'update-geoip':
        try:
            month = asyncio.run(download_geoip(geo_path, args.source_timeout))
        except (httpx.HTTPError, TimeoutError, OSError, ValueError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            print(tr(f'Не удалось скачать базу стран: {reason}', f'Could not download the country database: {reason}'), file=sys.stderr)
            lock.close()
            return 2
        print(tr(f'База стран обновлена: DB-IP {month}. {geoip.ATTRIBUTION}', f'Country database updated: DB-IP {month}. {geoip.ATTRIBUTION}'), flush=True)
        try:
            month = asyncio.run(download_geoip(geoip.asn_path(args.data), args.source_timeout, geoip.ASN_URL,
                                               geoip.validate_asn_download))
        except (httpx.HTTPError, TimeoutError, OSError, ValueError) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            # Countries already work; providers are an extra and must not fail the update.
            print(tr(f'Не удалось скачать базу провайдеров: {reason}', f'Could not download the provider database: {reason}'),
                  file=sys.stderr)
        else:
            print(tr(f'База провайдеров обновлена: DB-IP {month}.', f'Provider database updated: DB-IP {month}.'), flush=True)
        lock.close()
        return 0
    try:
        geo = geoip.CountryDB.load_optional(geo_path)
    except (OSError, EOFError, UnicodeError, csv.Error):
        print(tr('База стран повреждена; выполните update-geoip.', 'The country database is damaged; run update-geoip.'), file=sys.stderr)
        geo = None
    if countries and geo is None:
        print(tr('База стран не найдена: страна известна только для адресов из Geonode. '
              'Скачайте базу командой update-geoip.', 'No country database: countries are known only for Geonode addresses. '
              'Download it with the update-geoip command.'), file=sys.stderr)
    try:
        provider_of = provider_resolver(geoip.AsnDB.load_optional(geoip.asn_path(args.data)))
    except (OSError, EOFError, UnicodeError, csv.Error):
        provider_of = None
    if args.no_hosting and provider_of is None:
        print(tr('База провайдеров не найдена: --no-hosting не действует. Скачайте её командой update-geoip.',
                 'No provider database: --no-hosting has no effect. Download it with the update-geoip command.'),
              file=sys.stderr)
    db = open_db(args.data / 'proxies.sqlite3')
    country_of = country_resolver(db, geo)
    config = None
    profile = None
    code = 0
    latest = {}
    last_report = None
    last_scan_state = None
    scan_in_progress = False
    current_published = False
    scan_interrupted = False
    app_run_id = hashlib.sha256(f'{time.time()}:{os.getpid()}'.encode()).hexdigest()[:20]
    scope_digest = scope_digest_for(args.protocol, countries, args.max_latency, args.no_hosting, export_query)

    def update_progress(values):
        latest.update(values, updated_at=time.time())
        if args.progress_file:
            atomic(args.progress_file, json.dumps(latest))

    update_progress(dict(phase='starting', checked=0, candidates=0))

    def export_now(*, run_state=None, diagnostic=False, selected=None):
        return export(db, profile, args.data / 'exports', top=args.top,
                      sort=args.sort, min_success=args.min_success, denylist=denylist,
                      local_override=args.local_denylist, min_anonymity=args.min_anonymity,
                      protocol=args.protocol, max_latency=args.max_latency or None,
                      countries=countries, country_of=country_of,
                      exclude_hosting=args.no_hosting or args.export_hosting == 'hide',
                      provider_of=provider_of, watch_minutes=args.watch, query=export_query,
                      allowed_proxies=selected, run_state=run_state, diagnostic=diagnostic,
                      scope_digest=scope_digest)

    try:
        if args.command in ('scan', 'run'):
            config = target_config(args, denylist=denylist)
            if not config['reputation'].get('local_enabled', True):
                collect_denylist = Denylist.empty()
            profile = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:20]
        if args.command in ('collect', 'run'):
            urls = [] if args.no_sources else resolve_collect_sources(args)
            if not isinstance(urls, list):
                raise ValueError('sources: ожидается JSON-массив http(s) URL')
            report = asyncio.run(stoppable(collect(
                db, urls, args.input, args.source_timeout, update_progress, denylist=collect_denylist,
                allow_private_sources=args.allow_private_sources, detect_protocols=args.detect_protocols,
                max_source_bytes=args.source_max_bytes,
                max_source_line_bytes=args.source_max_line_bytes,
                max_source_candidates=args.source_max_candidates,
                max_source_redirects=args.source_max_redirects), args.stop_file))
            atomic(args.data / 'sources-report.json', json.dumps(report, indent=2) + '\n')
            print(tr(f'Уникальных кандидатов в базе: {report["unique"]}', f'Unique candidates in the database: {report["unique"]}'), flush=True)
        if args.command in ('scan', 'run'):
            workers = fit_workers(args.workers)
            print(tr(f'Воркеров: {workers}; полный обход; профиль {profile}', f'Workers: {workers}; full pass; profile {profile}'), flush=True)

            dnsbl_gate = asyncio.Semaphore(8)
            async def reputation_check(proxy, scan_config):
                policy = scan_config.get('reputation', {})
                if policy.get('dnsbl_enabled'):
                    async with dnsbl_gate:
                        return await screen_proxy(proxy, policy, denylist)
                return await screen_proxy(proxy, policy, denylist)

            probe = check_proxy
            if config.get('anonymity'):
                try:
                    own_ips = asyncio.run(detect_own_ips(config))
                except (httpx.HTTPError, TimeoutError, OSError, ValueError) as exc:
                    reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                    print(tr(f'Не удалось определить внешний IP через judge URL: {reason}', f'Could not detect the external IP through the judge URL: {reason}'), file=sys.stderr)
                    raise
                print(tr(f'Проверка анонимности: judge {public_url(config["anonymity"]["judge_url"])}', f'Anonymity check: judge {public_url(config["anonymity"]["judge_url"])}'), flush=True)

                async def probe(proxy, scan_config, limiter):
                    return await check_proxy(proxy, scan_config, limiter, own_ips=own_ips)

            prefilter = fit_prefilter(workers, args.prefilter)

            def run_scan(**options):
                nonlocal last_scan_state, scan_in_progress, scan_interrupted
                state = {}
                scan_interrupted = False
                scan_in_progress = True
                try:
                    asyncio.run(stoppable(scan(db, config, workers=workers, rate=args.rate,
                                               probe=probe, on_progress=update_progress, min_success=args.min_success,
                                               screen=reputation_check, denylist=denylist,
                                               min_anonymity=args.min_anonymity, protocol=args.protocol,
                                               max_latency=args.max_latency or None, countries=countries,
                                               country_of=country_of, prefilter=prefilter,
                                               exclude_hosting=args.no_hosting, provider_of=provider_of,
                                               prefilter_timeout=min(args.prefilter_timeout, args.connect_timeout),
                                               run_state=state, app_run_id=app_run_id,
                                               scope_digest=scope_digest, **options), args.stop_file))
                except (KeyboardInterrupt, asyncio.CancelledError):
                    scan_interrupted = True
                    state.update(state='partial', stop_reason='stopped')
                    last_scan_state = state
                    raise
                except Exception:
                    scan_interrupted = True
                    state.update(state='error', stop_reason='error')
                    last_scan_state = state
                    raise
                finally:
                    scan_in_progress = False
                last_scan_state = state
                return state

            last_scan_state = run_scan(recheck=args.recheck, recheck_passing=args.recheck_passing, want=args.want)
            last_report = export_now(run_state=last_scan_state)
            current_published = True
            # Publish the active profile only after its generation is complete;
            # a cancelled scan leaves the previous results/current export visible.
            atomic(args.data / 'last-profile.txt', profile)
            while args.watch:
                # Keep the list fresh: publish, wait, then re-check only the proxies that pass.
                update_progress(dict(last_report, phase='waiting', next_check_at=time.time() + args.watch * 60))
                print(tr(f'Сохранено {last_report["exported"]}. Следующая перепроверка рабочих прокси через {args.watch:g} мин.', f'Saved {last_report["exported"]}. Next re-check of working proxies in {args.watch:g} min.'),
                      flush=True)
                asyncio.run(stoppable(asyncio.sleep(args.watch * 60), args.stop_file))
                last_scan_state = run_scan(recheck_passing=True)
                last_report = export_now(run_state=last_scan_state)
                current_published = True
        elif args.command == 'export':
            profile = (args.data / 'last-profile.txt').read_text(encoding='utf-8').strip()
            last_report = export_now(selected=allowed_proxies)
            current_published = True
    except (KeyboardInterrupt, asyncio.CancelledError):
        print(tr('Остановлено. Завершённые проверки сохранены; scan продолжит проход.', 'Stopped. Finished checks are saved; the next scan continues where this one stopped.'), flush=True)
        code = 130
    except Exception as exc:
        print(tr(f'Ошибка: {type(exc).__name__}: проверьте файлы и параметры.', f'Error: {type(exc).__name__}: check the files and options.'), file=sys.stderr)
        code = 2
    finally:
        try:
            db.commit()
            if code and scan_interrupted and last_scan_state and profile \
                    and db.execute('SELECT 1 FROM profiles WHERE id=?', (profile,)).fetchone():
                update_progress(dict(phase='exporting_diagnostic'))
                try:
                    last_report = export_now(run_state=last_scan_state, diagnostic=True)
                except Exception as exc:
                    print(tr(f'Не удалось сохранить диагностический snapshot: {type(exc).__name__}.',
                             f'Could not save the diagnostic snapshot: {type(exc).__name__}.'), file=sys.stderr)
                else:
                    update_progress(last_report)
            if current_published and last_report:
                print(tr(f'Проверено {last_report["checked"]}/{last_report["scope_candidates"]}; подходят {last_report["passed"]}; сохранено {last_report["exported"]}',
                         f'Checked {last_report["checked"]}/{last_report["scope_candidates"]}; matching {last_report["passed"]}; saved {last_report["exported"]}'), flush=True)
        finally:
            update_progress(dict(phase='stopped' if code == 130 else 'error' if code else 'complete', exit_code=code))
            db.close()
            lock.close()
    return code


if __name__ == '__main__':
    raise SystemExit(main())
