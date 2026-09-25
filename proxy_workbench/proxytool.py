#!/usr/bin/env python3
"""Independent public proxy collector and resumable service benchmark."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
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

from .branding import DEFAULT_REQUEST_PROFILE, PRODUCT_NAME, PRODUCT_VERSION, REQUEST_PROFILES, merge_headers, profile_digest, validate_profile
from .reputation import Denylist, make_policy, result_allowed, screen_proxy, verdict_blocks
from .maintenance import clear_runtime, exclusive_lock
from . import anonymity
from . import geoip
from . import socks4
from . import formats
from .i18n import tr, utf8_output
from . import paths

ROOT = paths.PACKAGE
TLS = ssl.create_default_context()
SCHEMES = {'http', 'https', 'socks4', 'socks5', 'socks5h'}
SAFE_TARGET_HEADERS = {'accept', 'accept-encoding', 'accept-language', 'cache-control', 'pragma', 'user-agent', 'x-client-version', 'x-request-id'}

# Source fetching is deliberately bounded before a response is handed to a parser.
# These defaults are finite so a public list cannot consume unbounded memory or CPU.
DEFAULT_SOURCE_MAX_BYTES = 32 * 1024 * 1024
DEFAULT_SOURCE_MAX_LINE_BYTES = 64 * 1024
DEFAULT_SOURCE_MAX_CANDIDATES = 500_000
DEFAULT_SOURCE_MAX_REDIRECTS = 5
MAX_SOURCE_BYTES = 512 * 1024 * 1024
MAX_SOURCE_LINE_BYTES = 16 * 1024 * 1024
MAX_SOURCE_CANDIDATES = 10_000_000
MAX_SOURCE_REDIRECTS = 20
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

    def __init__(self, code, *, retryable=False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


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
async def _source_stream(client, url, allow_private=False):
    _, hostname, _, address = await _validate_source_destination(url, allow_private)
    if address is None:
        async with client.stream('GET', url) as response:
            yield response
        return
    transport = PinnedSourceTransport(address, hostname)
    pinned_client = httpx.AsyncClient(transport=transport, trust_env=False, verify=TLS,
                                       follow_redirects=False, timeout=15)
    try:
        async with pinned_client.stream('GET', url) as response:
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


async def _read_bounded_body(response, budget, max_bytes):
    remaining = max_bytes - budget['used']
    declared = _declared_response_length(response)
    if declared is not None and declared > remaining:
        raise SourceFetchError('SOURCE_TOO_LARGE')
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(chunk) > remaining:
            raise SourceFetchError('SOURCE_TOO_LARGE')
        body.extend(chunk)
        budget['used'] += len(chunk)
        remaining -= len(chunk)
    return bytes(body)


async def _read_bounded_lines(response, budget, max_bytes, max_line_bytes, on_line):
    remaining = max_bytes - budget['used']
    declared = _declared_response_length(response)
    if declared is not None and declared > remaining:
        raise SourceFetchError('SOURCE_TOO_LARGE')
    pending = bytearray()
    chunks = response.aiter_bytes()
    async for chunk in chunks:
        if len(chunk) > remaining:
            raise SourceFetchError('SOURCE_TOO_LARGE')
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
    if pending:
        if len(pending) > max_line_bytes:
            raise SourceFetchError('SOURCE_LINE_TOO_LARGE')
        on_line(bytes(pending))


def _normalize_proxy(value, *, public_only):
    """Shared proxy URL parser for public collection and private GUI imports."""
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw or raw.startswith('#') or any(c.isspace() for c in raw):
        return None
    try:
        parsed = urlsplit(raw if '://' in raw else 'http://' + raw)
        if parsed.scheme not in SCHEMES or parsed.username is not None or parsed.password is not None:
            return None
        if parsed.path not in ('', '/') or parsed.query or parsed.fragment or not parsed.port:
            return None
        host = parsed.hostname
        if not host or len(host) > 253:
            return None
        try:
            ip = ipaddress.ip_address(host)
            if public_only and (not ip.is_global or (parsed.scheme == 'socks4' and ip.version != 4)):
                return None
            if ':' in host and ip.version != 6:
                return None
            canonical = f'[{ip.compressed}]' if ip.version == 6 else ip.compressed
            if parsed.scheme == 'socks4' and ip.version != 4:
                return None
        except ValueError:
            if public_only:
                return None
            # Hostnames are not resolved here. They are valid input for a
            # user's own gateway, but credentials and URL paths still are not.
            labels = host.rstrip('.').split('.')
            if (not labels or any(not label or len(label) > 63 or
                                   not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?', label)
                                   for label in labels)):
                return None
            canonical = host.rstrip('.').lower()
        # Explicit schemes are authoritative; bare addresses use HTTP.
        return f'{parsed.scheme}://{canonical}:{parsed.port}'
    except (ValueError, TypeError):
        return None


def normalize(value):
    return _normalize_proxy(value, public_only=True)


def normalize_custom(value):
    """Normalize a user proxy without retaining URL credentials.

    The public collector intentionally accepts only globally routable IPs. A
    GUI import may also name a private mock or a hostname, but it follows the
    same syntax and credential checks.
    """
    return _normalize_proxy(value, public_only=False)


def normalize_custom_list(value):
    """Return a canonical, credential-free newline-separated import list."""
    if not isinstance(value, str):
        raise ValueError('Список прокси должен быть строкой.')
    result, seen = [], set()
    for raw in value.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        proxy = normalize_custom(line)
        if proxy is None:
            raise ValueError('Импорт содержит некорректный адрес или логин/пароль; '
                             'укажите IP или имя хоста, порт и протокол без credentials.')
        if proxy not in seen:
            seen.add(proxy)
            result.append(proxy)
    return '\n'.join(result)


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


def _publish_legacy_files(directory, generation, names, report, before_commit=None):
    """Publish compatibility root files before switching ``current.json``.

    Root files are legacy conveniences, while API/GUI readers use the
    immutable generation.  Staging every copy first means an I/O failure
    cannot expose a new pointer with half-written compatibility files.  Small
    rename backups provide rollback if a later replace fails (notably on
    Windows when a reader briefly holds a file open).  ``before_commit`` is
    called while those backups still exist, so a pointer failure restores the
    complete previous root set.
    """
    directory = Path(directory)
    legacy_names = list(dict.fromkeys([*names, 'status.json']))
    staged, backups, installed = {}, {}, []
    try:
        for name in legacy_names:
            source = generation/name
            temporary = directory/(name+'.publish-tmp')
            temporary.unlink(missing_ok=True)
            if name == 'status.json':
                temporary.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
            else:
                shutil.copyfile(source, temporary)
            staged[name] = temporary
        for name in legacy_names:
            target = directory/name
            if target.exists() or target.is_symlink():
                backup = directory/('.'+name+'.publish-backup')
                backup.unlink(missing_ok=True)
                target.replace(backup)
                backups[name] = backup
        for name, temporary in staged.items():
            temporary.replace(directory/name)
            installed.append(directory/name)
        if before_commit is not None:
            before_commit()
    except BaseException:
        for path in reversed(installed):
            with contextlib.suppress(OSError):
                path.unlink()
        for name, backup in reversed(list(backups.items())):
            with contextlib.suppress(OSError):
                backup.replace(directory/name)
        raise
    finally:
        for temporary in staged.values():
            with contextlib.suppress(OSError):
                temporary.unlink()
        for backup in backups.values():
            with contextlib.suppress(OSError):
                backup.unlink()
    return True


def _restore_bytes(path, content):
    path = Path(path)
    if content is None:
        path.unlink(missing_ok=True)
        return
    temporary = path.with_name(path.name+'.restore-tmp')
    temporary.write_bytes(content)
    temporary.replace(path)


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
        CREATE TABLE IF NOT EXISTS results(
            profile TEXT NOT NULL, proxy TEXT NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY(profile, proxy));
    ''')
    columns = {row[1] for row in db.execute('PRAGMA table_info(candidate_meta)')}
    if 'source' not in columns:
        # Added in 1.6: which source list first delivered each candidate.
        db.execute('ALTER TABLE candidate_meta ADD COLUMN source TEXT')
        db.commit()
    return db


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


SOURCE_KINDS = ('http', 'https', 'socks4', 'socks5', 'socks5h', 'auto', 'text', 'geonode', 'http-fields')
DETECT_PROTOCOLS = ('http', 'socks4', 'socks5')
# ip:port inside free text: "1.2.3.4:8080", "1.2.3.4 8080", CSV and HTML table cells.
LOOSE_ADDRESS = re.compile(r'(?<![\d.])(?:(https?|socks[45]h?)://)?(\d{1,3}(?:\.\d{1,3}){3})'
                           r'(?::|\s*(?:</t[dh]>\s*<t[dh][^>]*>|[\s,;|])\s*)(\d{2,5})(?!\d)', re.I)


def loose_addresses(line):
    """Every proxy-looking address in a line of arbitrary text."""
    return [f'{(scheme or "http").lower()}://{host}:{port}' for scheme, host, port in LOOSE_ADDRESS.findall(line)]


def source_spec(value):
    if not isinstance(value, str):
        raise ValueError('Источник должен быть строкой URL или «socks4 URL» / «socks5 URL» / «geonode URL».')
    parts = value.strip().split(None, 1)
    kind, url = (parts if len(parts) == 2 else ('http', parts[0] if parts else ''))
    if kind not in SOURCE_KINDS:
        raise ValueError('Неизвестный формат источника.')
    try:
        _parse_source_url(url)
    except ValueError as exc:
        raise ValueError(f'Источник: {exc}.') from None
    return kind, url


async def collect(db, urls, inputs, timeout=60, on_progress=None, denylist=None,
                  allow_private_sources=False, detect_protocols=False,
                  max_source_bytes=DEFAULT_SOURCE_MAX_BYTES,
                  max_source_line_bytes=DEFAULT_SOURCE_MAX_LINE_BYTES,
                  max_source_candidates=DEFAULT_SOURCE_MAX_CANDIDATES,
                  max_source_redirects=DEFAULT_SOURCE_MAX_REDIRECTS):
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

            async def request_source(request_url, expected_page=None):
                current_url = request_url
                for redirect_count in range(max_source_redirects + 1):
                    async with _source_stream(client, current_url, allow_private_sources) as response:
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
                        if 300 <= status < 400:
                            raise SourceFetchError('SOURCE_REDIRECT_INVALID')
                        response.raise_for_status()
                        if kind == 'geonode':
                            body = await _read_bounded_body(response, budget, max_source_bytes)
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
                            consume_line(await _read_bounded_body(response, budget, max_source_bytes))
                            return None
                        await _read_bounded_lines(response, budget, max_source_bytes, line_limit, consume_line)
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
                                data = await request_source(page_url, page)
                            succeeded = True
                            error = None
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
                    pages += 1
                    if kind != 'geonode':
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
            reports.append(dict(source=index, rows=count, invalid=invalid, blocked=blocked, pages=pages,
                                attempts=attempts, complete=error is None, error=error, format=kind))
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
    """Add the freshness contract without resurrecting an already expired row."""
    checked_at = row.get('checked_at')
    if not isinstance(checked_at, (int, float)) or isinstance(checked_at, bool) or not math.isfinite(checked_at) or checked_at <= 0:
        checked_at = fallback_checked_at or time.time()
        row['checked_at'] = checked_at
    existing = row.get('valid_until')
    try:
        valid = float(existing)
        preserve = math.isfinite(valid) and valid > 0
    except (TypeError, ValueError, OverflowError):
        preserve = False
    if preserve:
        row['valid_until'] = valid
    else:
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


REPUTATION_STATUSES = frozenset({'clean', 'listed', 'unknown', 'local_denied'})


def reputation_status(row):
    """Return a truthful quality label when a legacy/malformed row has no verdict."""
    verdict = row.get('reputation') if isinstance(row, dict) else None
    status = verdict.get('status') if isinstance(verdict, dict) else None
    return status if status in REPUTATION_STATUSES else 'unknown'


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
               exclude_hosting=False, provider_of=None, run_state=None):
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

    def selected(proxy):
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
    stored_rows = {}
    only = None
    for proxy, payload in db.execute('SELECT proxy, payload FROM results WHERE profile=?', (profile,)).fetchall():
        row = json.loads(payload)
        stored_rows[proxy] = row
        if recheck_passing and not (selected(proxy) and counts_as_passed(row)):
            continue
        previous[proxy] = row_history(row, min_success)
    if recheck_passing:
        only = set(previous)
    db.commit()

    # A normal continuation may skip a result only while its verdict is still
    # trustworthy.  In particular, UNREACHABLE and expired rows are pending on
    # the next scan; treating every row as done made a dead proxy permanent.
    if recheck or recheck_passing:
        done = set()
    else:
        now = time.time()
        done = {proxy for proxy, row in stored_rows.items()
                if not row.get('error') and row_fresh(row, now)}
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
        for proxy in done:
            row = stored_rows[proxy]
            if not selected(proxy):
                continue
            status = reputation_status(row)
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
        completed += 1
        status = reputation_status(row)
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


def country_resolver(db, geo=None):
    """Country from source metadata first, then the offline GeoIP database."""
    meta = dict(db.execute('SELECT proxy, country FROM candidate_meta WHERE country IS NOT NULL'))
    cache = {}

    def country_of(proxy):
        if proxy not in cache:
            cache[proxy] = meta.get(proxy) or (geo.country_of(proxy) if geo else None)
        return cache[proxy]
    return country_of


def exit_country(row, country_of=None):
    """Country of the address the judge saw, which can differ from the proxy's own address."""
    address = (row.get('anonymity') or {}).get('exit_ip')
    if not address or not country_of:
        return None
    return country_of(f'http://[{address}]:1' if ':' in address else f'http://{address}:1')


def listed_counts(db):
    """How many distinct lists offered each address (databases before 2.2 have no record: 1)."""
    try:
        return dict(db.execute('SELECT proxy, count(*) FROM candidate_seen GROUP BY proxy'))
    except sqlite3.Error:
        return {}


def source_map(db):
    """Return every source key associated with a candidate.

    ``candidate_meta.source`` is retained as a first-seen compatibility
    field, but it is not authoritative: the many-to-many ``candidate_seen``
    table is what prevents source health and recommendations from depending
    on collection order.
    """
    result = {}
    try:
        for proxy, source in db.execute('SELECT proxy, source FROM candidate_meta WHERE source IS NOT NULL'):
            if source:
                result.setdefault(proxy, []).append(source)
    except sqlite3.Error:
        pass
    try:
        for proxy, source in db.execute('SELECT proxy, source FROM candidate_seen'):
            if not source:
                continue
            values = result.setdefault(proxy, [])
            if source not in values:
                values.append(source)
    except sqlite3.Error:
        pass
    return {proxy: tuple(values) for proxy, values in result.items()}


def _source_values(value):
    if isinstance(value, (list, tuple, set)):
        return tuple(item for item in value if isinstance(item, str) and item)
    return (value,) if isinstance(value, str) and value else ()


def recommender(source_quality, listed, sources, min_success=2/3):
    """Recommended score: quality, survival across re-checks, rarity across lists and the source's record.

    A proxy offered by one list is used by fewer people than one in twenty lists; a list whose
    proxies keep working earns trust. Both only reorder proxies that already passed.  A proxy
    present in several lists gets the mean of those lists' records, rather than an order-dependent
    credit to whichever collector happened to finish first.
    """
    rates = {key: (stats.get('passed', 0) + 1) / (stats.get('checked', 0) + 10)
             for key, stats in (source_quality or {}).items()
             if isinstance(stats, dict)}
    best = max(rates.values(), default=0) or 1

    def score(row):
        history = row_history(row, min_success)
        uptime = history['passes'] / history['checks'] if history['checks'] else 0
        rarity = 1 / math.sqrt(max(1, listed.get(row['proxy'], 1)))
        keys = _source_values(sources.get(row['proxy']))
        known = [rates[key] for key in keys if key in rates]
        source_rate = (sum(known) / len(known)) if known else best / 2
        source = source_rate / best
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
           watch_minutes=0, allowed_proxies=None, run_state=None, diagnostic=False, query='', quick='', active_profile_path=None):
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
    quick = quick.strip().lower() if isinstance(quick, str) else quick
    if quick not in ('', 'clean', 'speed', 'http'):
        raise ValueError('Неизвестный быстрый фильтр экспорта.')
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
    directory.mkdir(parents=True, exist_ok=True)
    published_at = time.time()
    generations = directory/'generations'
    generations.mkdir(exist_ok=True)
    _, failed, remaining = prune_export_generations(directory)
    if failed and remaining >= EXPORT_GENERATION_RETENTION:
        raise RuntimeError('Закрытые старые export generations не удаляются; повторите после завершения загрузок.')
    generation = Path(tempfile.mkdtemp(prefix='.generation-', dir=generations))
    published = False
    profile_path = Path(active_profile_path) if active_profile_path and not diagnostic else None
    old_profile = None
    had_profile = False
    if profile_path is not None:
        try:
            old_profile = profile_path.read_bytes()
            had_profile = True
        except FileNotFoundError:
            pass
    # Keep full samples on disk, including when hundreds of thousands pass.
    db.execute('DROP TABLE IF EXISTS temp.export_rank')
    db.execute('CREATE TEMP TABLE export_rank(proxy TEXT PRIMARY KEY, score REAL, latency REAL, reliability REAL, '
               'jitter REAL, uptime REAL, checks INTEGER, bandwidth REAL, recommended REAL)')
    stored_checked = passed = local_filtered = 0
    sources = source_map(db)
    source_quality = {}
    status_counts = {'clean': 0, 'listed': 0, 'unknown': 0, 'local_denied': 0}
    anonymity_counts = {}
    breakdown = {'protocols': {}, 'countries': {}}
    recommend_later = []
    selection_eligible = set()

    def in_scope(proxy):
        if protocol not in (None, 'all') and proxy_protocol(proxy) != protocol:
            return False
        if countries and (country_of(proxy) if country_of else None) not in countries:
            return False
        return not (exclude_hosting and is_hosting(proxy, provider_of))

    scope_candidates = sum(1 for (proxy,) in db.execute('SELECT proxy FROM candidates') if in_scope(proxy))
    for (payload,) in db.execute('SELECT payload FROM results WHERE profile=?', (profile,)):
        row = json.loads(payload)
        proxy = row.get('proxy', '')
        stamp_freshness(row, watch_minutes, published_at)
        # Source health is an independent measurement.  Count only fresh rows
        # and the basic target verdict, never the current export's search,
        # latency, country, hosting or selected-proxy filters.  Otherwise a
        # filtered empty export could make a healthy source look dead.
        verdict = row.get('reputation') if isinstance(row.get('reputation'), dict) else {}
        if (row_fresh(row, published_at)
                and verdict.get('status') not in ('local_denied', 'listed')):
            source_passed = int(result_allowed(row, min_success, denylist=None, strict=False, min_anonymity='any'))
            for source in (_source_values(sources.get(proxy)) or ('unknown',)):
                quality = source_quality.setdefault(source, {'checked': 0, 'passed': 0})
                quality['checked'] += 1
                quality['passed'] += source_passed
        if not in_scope(proxy):
            continue
        stored_checked += 1
        status = reputation_status(row)
        status_counts[status] = status_counts.get(status, 0) + 1
        level = (row.get('anonymity') or {}).get('level')
        if level:
            anonymity_counts[level] = anonymity_counts.get(level, 0) + 1
        speed_data = row.get('speed') if isinstance(row.get('speed'), dict) else {}
        speed_mbps = speed_data.get('mbps', row.get('mbps'))
        quick_ok = (quick != 'clean' or reputation_status(row) == 'clean')
        quick_ok = quick_ok and (quick != 'speed' or (speed_mbps is not None and speed_mbps > 0))
        quick_ok = quick_ok and (quick != 'http' or proxy_protocol(proxy) in ('http', 'https'))
        eligible = (row_fresh(row, published_at)
                    and result_allowed(row, min_success, denylist=None, strict=strict, min_anonymity=min_anonymity)
                    and matches_selection(row, protocol, max_latency, countries, country_of, exclude_hosting, provider_of)
                    and (not query or query in proxy.lower())
                    and quick_ok)
        if eligible and active_denylist is not None and active_denylist.match(proxy):
            local_filtered += 1
            eligible = False
        if eligible:
            passed += 1
            selection_eligible.add(proxy)
            if selection_requested is None or proxy in selection_requested:
                history = row_history(row, min_success)
                for group, value in (('protocols', proxy_protocol(proxy)),
                                     ('countries', row_country(row, country_of) or '??')):
                    breakdown[group][value] = breakdown[group].get(value, 0) + 1
                db.execute('INSERT INTO export_rank VALUES (?,?,?,?,?,?,?,?,NULL)',
                           (proxy, row['score'], row['latency_ms'], row['reliability'], row.get('jitter_ms'),
                            history['passes'] / history['checks'], history['checks'],
                            (row.get('speed') or {}).get('mbps')))
                recommend_later.append(row)
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
              'anonymity', 'anonymity_signals', 'country', 'exit_ip', 'exit_country', 'asn', 'provider', 'hosting', 'mbps', 'listed_in', 'source_keys', 'recommended', 'checks', 'passes']
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
                verdict = row.get('reputation') if isinstance(row.get('reputation'), dict) else {}
                dnsbl = verdict.get('dnsbl') if isinstance(verdict.get('dnsbl'), list) else []
                row['reputation_status'] = reputation_status(row)
                row['reputation_sources'] = ','.join(item.get('zone', '') for item in dnsbl
                                                       if isinstance(item, dict) and item.get('status') == 'listed')
                judged = row.get('anonymity') or {}
                row['country'] = row_country(row, country_of) or ''
                row['exit_ip'] = judged.get('exit_ip', '')
                row['listed_in'] = listed.get(row['proxy'], 1)
                row['source_keys'] = list(_source_values(sources.get(row['proxy'])))
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
        # An export with no published row has no useful lifetime.  Do not give
        # it a future default TTL: that made a failed/empty selection look
        # fresh while the API and gateway had no usable proxy to serve.
        valid_until = min(exported_valid_until, default=published_at)
        has_fresh_export = bool(exported) and valid_until > published_at
        if not has_fresh_export and snapshot_state == 'complete':
            snapshot_state = 'stale'
            stop_reason = 'expired'
        missing = sorted(selection_requested - selection_eligible) if selection_requested is not None else []
        report = dict(
            schema_version=SNAPSHOT_SCHEMA_VERSION, profile=profile, generation=generation.name,
            state=snapshot_state, stop_reason=stop_reason,
            scope=dict(protocol=protocol, countries=list(countries or ()), exclude_hosting=bool(exclude_hosting)),
            scope_candidates=scope_candidates, checked=checked,
            pending=max(0, scope_candidates - checked), passed=reported_passed,
            candidates=total, local_filtered=local_filtered, exported=exported,
            complete=snapshot_state == 'complete' and checked == scope_candidates and has_fresh_export,
            generated_at=published_at, valid_until=valid_until, stale=not has_fresh_export,
            empty_export=not has_fresh_export,
            sort=sort, min_success=min_success, protocol=protocol, max_latency=max_latency,
            countries=list(countries or ()), exclude_hosting=bool(exclude_hosting and provider_of), query=query, quick=quick,
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
            source_quality=source_quality, source_health_basis='fresh_profile_checks',
            breakdown=breakdown)
        atomic(generation/'status.json', json.dumps(report, indent=2) + '\n')
        pointer_name = 'diagnostic.json' if diagnostic else 'current.json'
        pointer_value = json.dumps({'generation': generation.name, 'files': names,
                                    'state': snapshot_state}, ensure_ascii=False) + '\n'
        if diagnostic:
            atomic(directory/pointer_name, pointer_value)
            published = True
        else:
            # Finish every fallible legacy copy and the active-profile update
            # before switching the current pointer.  If any step fails, the
            # old pointer/profile remain the only published generation.
            try:
                if profile_path is not None:
                    profile_path.parent.mkdir(parents=True, exist_ok=True)
                    atomic(profile_path, profile + '\n')
                _publish_legacy_files(
                    directory, generation, names, report,
                    before_commit=lambda: atomic(directory/pointer_name, pointer_value))
            except BaseException:
                if profile_path is not None:
                    with contextlib.suppress(OSError):
                        if had_profile:
                            _restore_bytes(profile_path, old_profile)
                        else:
                            profile_path.unlink(missing_ok=True)
                raise
            published = True
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


def parser():
    # Installed and `python -m` runs show the command people actually type.
    prog = None if Path(sys.argv[0]).name == 'proxytool.py' else 'proxy-workbench'
    p = argparse.ArgumentParser(prog=prog, formatter_class=argparse.RawDescriptionHelpFormatter, epilog=tr(EPILOG_RU, EPILOG_EN),
                                description=tr(f'{PRODUCT_NAME}: сбор и полная проверка публичных прокси под HTTP-сервис', f'{PRODUCT_NAME}: collect public proxies and fully check them against your HTTP services'))
    p.add_argument('--version', action='version', version=f'{PRODUCT_NAME} {PRODUCT_VERSION}')
    p.add_argument('command', choices=['collect', 'scan', 'run', 'export', 'get', 'test', 'serve', 'gateway', 'clear-data', 'update-geoip'],
                   help=tr('run — собрать и проверить; collect — только собрать; scan — только проверить; '
                           'export — пересобрать файлы; get — вывести готовые прокси; test — проверить свои прокси; '
                           'serve — локальное API; gateway — ротирующий прокси; '
                           'update-geoip — база стран; '
                           'clear-data — удалить результаты',
                           'run: collect and check; collect: only collect; scan: only check; '
                           'export: rebuild the files; get: print working proxies; test: check given proxies; '
                           'serve: local API; gateway: rotating proxy; '
                           'update-geoip: country database; '
                           'clear-data: delete results'))
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
    p.add_argument('--export-quick', choices=('', 'clean', 'speed', 'http'), default='', help=argparse.SUPPRESS)
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
        print(tr(f'Ротирующий прокси: {address} (HTTP и SOCKS5 TCP), в пуле {len(pool.refresh())} прокси. Ctrl+C — остановить.',
                 f'Rotating proxy: {address} (HTTP and SOCKS5 TCP), {len(pool.refresh())} proxies in the pool. Ctrl+C to stop.'),
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
    if args.command != 'export' and (args.selection_file is not None or args.export_query or args.export_hosting or args.export_quick):
        p.error('--selection-file/--export-query/--export-hosting/--export-quick доступны только для export')
    if len(args.export_query) > 100:
        p.error('Поиск экспорта слишком длинный: максимум 100 символов.')
    export_query = args.export_query.strip().lower()
    export_quick = args.export_quick.strip().lower()
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
                      provider_of=provider_of, watch_minutes=args.watch, query=export_query, quick=export_quick,
                      allowed_proxies=selected, run_state=run_state, diagnostic=diagnostic,
                      active_profile_path=None if diagnostic else args.data / 'last-profile.txt')

    try:
        if args.command in ('scan', 'run'):
            config = target_config(args, denylist=denylist)
            if not config['reputation'].get('local_enabled', True):
                collect_denylist = Denylist.empty()
            profile = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:20]
        if args.command in ('collect', 'run'):
            urls = [] if args.no_sources else json.loads(args.sources.read_text(encoding='utf-8'))
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
                                               run_state=state, **options), args.stop_file))
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
            # The export transaction publishes the active profile together
            # with current.json; a cancelled scan leaves the previous pair
            # visible and never advances only one of them.
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
