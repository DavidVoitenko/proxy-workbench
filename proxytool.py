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

from branding import DEFAULT_REQUEST_PROFILE, PRODUCT_NAME, PRODUCT_VERSION, REQUEST_PROFILES, merge_headers, profile_digest, validate_profile
from reputation import Denylist, make_policy, result_allowed, screen_proxy, verdict_blocks
from maintenance import clear_runtime, exclusive_lock
import anonymity
import geoip
import socks4
import formats
from i18n import tr

ROOT = Path(__file__).resolve().parent
TLS = ssl.create_default_context()
SCHEMES = {'http', 'https', 'socks4', 'socks5', 'socks5h'}
SAFE_TARGET_HEADERS = {'accept', 'accept-encoding', 'accept-language', 'cache-control', 'pragma', 'user-agent', 'x-client-version', 'x-request-id'}

# Source fetching is deliberately bounded before a response is handed to a parser.
# These defaults are finite so a public list cannot consume unbounded memory or CPU.
DEFAULT_SOURCE_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_SOURCE_MAX_LINE_BYTES = 64 * 1024
DEFAULT_SOURCE_MAX_CANDIDATES = 100_000
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
        headers = dict(request.headers)
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
PROTOCOL_EXPORTS = {'http': 'http.txt', 'https': 'https.txt', 'socks4': 'socks4.txt', 'socks5': 'socks5.txt'}
PROTOCOL_ALIASES = {'socks5h': 'socks5'}


def current_generation_name(directory):
    try:
        manifest = json.loads((Path(directory)/'current.json').read_text(encoding='utf-8'))
        generation = manifest.get('generation') if isinstance(manifest, dict) else None
        if isinstance(generation, str) and generation not in ('', '.', '..') and '/' not in generation and '\\' not in generation:
            return generation
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    return None


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


def export_file(directory, name):
    directory = Path(directory)
    pointer = directory/'current.json'
    try:
        manifest = json.loads(pointer.read_text(encoding='utf-8'))
        generation = current_generation_name(directory)
        if generation:
            candidate = directory/'generations'/generation/name
            if candidate.is_file():
                return candidate
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    return directory/name


def open_db(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute('PRAGMA journal_mode=WAL')
    db.executescript('''
        CREATE TABLE IF NOT EXISTS candidates(proxy TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS candidate_meta(proxy TEXT PRIMARY KEY, country TEXT);
        CREATE TABLE IF NOT EXISTS profiles(id TEXT PRIMARY KEY, config TEXT NOT NULL);
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
    return validate_targets(targets, args,
                            request_profile=getattr(args, 'request_profile', None) or config.get('request_profile'),
                            reputation=config.get('reputation'),
                            denylist=denylist, anonymity_config=judge)


def validate_targets(targets, args, request_profile=None, reputation=None, denylist=None, anonymity_config=None):
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
    except (httpx.HTTPError, TimeoutError, OSError) as exc:
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
    return row


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
    except (httpx.HTTPError, TimeoutError, OSError) as exc:
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


def blocked_result(proxy, verdict):
    return dict(proxy=proxy, reliability=0, min_target_reliability=0,
                latency_ms=None, jitter_ms=None, score=0, successes=0, requests=0,
                checked_at=time.time(), samples=[], reputation=verdict)


async def scan(db, config, *, workers=128, rate=100, recheck=False, probe=check_proxy, progress=True, on_progress=None, min_success=2/3, screen=None, denylist=None, min_anonymity='any', protocol='all', max_latency=None,
               countries=(), country_of=None, want=0, recheck_passing=False):
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
        return not countries or country_of(proxy) in countries

    def counts_as_passed(row):
        return (result_allowed(row, min_success, denylist=active_denylist, strict=strict, min_anonymity=min_anonymity)
                and matches_selection(row, protocol, max_latency))

    # A re-check keeps each proxy's history so uptime survives new measurements.
    previous = {}
    only = None
    if recheck or recheck_passing:
        for proxy, payload in db.execute('SELECT proxy, payload FROM results WHERE profile=?', (profile,)).fetchall():
            row = json.loads(payload)
            if recheck_passing and not (selected(proxy) and counts_as_passed(row)):
                continue
            previous[proxy] = row_history(row, min_success)
        db.executemany('DELETE FROM results WHERE profile=? AND proxy=?', ((profile, proxy) for proxy in previous))
        if recheck_passing:
            only = set(previous)
    db.commit()

    done = {proxy for (proxy,) in db.execute('SELECT proxy FROM results WHERE profile=?', (profile,))}
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
    started = last_commit = time.monotonic()

    enough = asyncio.Event()
    if want and passed >= want:
        enough.set()

    async def producer():
        for proxy in pending:
            if enough.is_set():
                break
            await queue.put(proxy)
        for _ in range(workers):
            await queue.put(None)

    async def worker():
        nonlocal completed, last_commit, passed
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
                    row = await probe(proxy, config, limiter)
                    if verdict is not None:
                        row['reputation'] = verdict
                country = country_of(proxy)
                if country:
                    row['country'] = country
                row['history'] = next_history(previous.get(proxy), result_allowed(row, min_success), row.get('checked_at', time.time()))
                db.execute('INSERT OR REPLACE INTO results VALUES (?, ?, ?)',
                           (profile, proxy, json.dumps(row, ensure_ascii=False)))
                completed += 1
                status = (row.get('reputation') or {}).get('status', 'clean')
                status_counts[status] = status_counts.get(status, 0) + 1
                passed += int(counts_as_passed(row))
                if want and passed >= want:
                    enough.set()
                if completed % 100 == 0 or time.monotonic() - last_commit >= 1:
                    db.commit()
                    last_commit = time.monotonic()
            finally:
                queue.task_done()

    def publish():
        elapsed = max(0.001, time.monotonic() - started)
        speed = (completed - initial) / elapsed
        eta = (total - completed) / speed if speed else 0
        if on_progress:
            on_progress(dict(phase='scanning', profile=profile, checked=completed, candidates=total, passed=passed,
                             speed=round(speed, 2), eta_seconds=round(eta) if speed else None, workers=workers,
                             reputation=status_counts))
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
            tasks = [group.create_task(worker()) for _ in range(workers)]
            await asyncio.gather(*tasks)
            if progress or on_progress:
                reporter_task.cancel()
    finally:
        db.commit()
        publish()
    return profile


SORTS = ('quality', 'speed', 'stability', 'uptime')
EXPORT_ORDERS = {
    'quality': 'e.score DESC, e.latency, e.proxy',
    'speed': 'e.latency, e.reliability DESC, e.proxy',
    'stability': 'e.jitter, e.latency, e.proxy',
    'uptime': 'e.uptime DESC, e.checks DESC, e.score DESC, e.proxy',
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


def proxy_protocol(proxy):
    scheme = str(proxy).partition('://')[0]
    return PROTOCOL_ALIASES.get(scheme, scheme)


def matches_selection(row, protocol='all', max_latency=None, countries=(), country_of=None):
    """Selection by protocol, maximum median latency (ms) and country codes."""
    if protocol not in (None, 'all') and proxy_protocol(row.get('proxy', '')) != protocol:
        return False
    if countries and row_country(row, country_of) not in countries:
        return False
    if max_latency:
        latency = row.get('latency_ms')
        if latency is None or latency > max_latency:
            return False
    return True


def export(db, profile, directory, *, top=0, sort='quality', min_success=2/3, denylist=None, local_override=None, min_anonymity='any',
           protocol='all', max_latency=None, countries=(), country_of=None):
    if not db.execute('SELECT 1 FROM profiles WHERE id=?', (profile,)).fetchone():
        raise ValueError('Профиль проверки не найден')
    denylist = denylist or Denylist.empty()
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
               'jitter REAL, uptime REAL, checks INTEGER)')
    checked = passed = local_filtered = 0
    sources = dict(db.execute('SELECT proxy, source FROM candidate_meta WHERE source IS NOT NULL'))
    source_quality = {}
    status_counts = {'clean': 0, 'listed': 0, 'unknown': 0, 'local_denied': 0}
    anonymity_counts = {}
    breakdown = {'protocols': {}, 'countries': {}}
    for (payload,) in db.execute('SELECT payload FROM results WHERE profile=?', (profile,)):
        row = json.loads(payload)
        checked += 1
        status = (row.get('reputation') or {}).get('status', 'clean')
        status_counts[status] = status_counts.get(status, 0) + 1
        level = (row.get('anonymity') or {}).get('level')
        if level:
            anonymity_counts[level] = anonymity_counts.get(level, 0) + 1
        eligible = (result_allowed(row, min_success, denylist=None, strict=strict, min_anonymity=min_anonymity)
                    and matches_selection(row, protocol, max_latency, countries, country_of))
        if eligible and active_denylist is not None and active_denylist.match(row.get('proxy', '')):
            local_filtered += 1
            eligible = False
        elif eligible:
            history = row_history(row, min_success)
            for group, value in (('protocols', proxy_protocol(row['proxy'])), ('countries', row_country(row, country_of) or '??')):
                breakdown[group][value] = breakdown[group].get(value, 0) + 1
            db.execute('INSERT INTO export_rank VALUES (?,?,?,?,?,?,?)',
                       (row['proxy'], row['score'], row['latency_ms'], row['reliability'], row.get('jitter_ms'),
                        history['passes'] / history['checks'], history['checks']))
            passed += 1
        quality = source_quality.setdefault(sources.get(row.get('proxy'), 'unknown'), {'checked': 0, 'passed': 0})
        quality['checked'] += 1
        quality['passed'] += int(eligible)
    order = EXPORT_ORDERS[sort]
    selected = db.execute(f"""SELECT r.payload FROM export_rank e JOIN results r
        ON r.proxy=e.proxy AND r.profile=? ORDER BY {order} LIMIT ?""", (profile, top or -1))
    fields = ['proxy', 'score', 'latency_ms', 'jitter_ms', 'reliability', 'min_target_reliability',
              'successes', 'requests', 'checked_at', 'reputation_status', 'reputation_sources',
              'anonymity', 'anonymity_signals', 'country', 'exit_ip', 'exit_country', 'checks', 'passes']
    names = ['proxies.txt', 'ranked.json', 'ranked.csv', *PROTOCOL_EXPORTS.values(), 'hostport.txt', 'proxychains.txt',
             'proxy.pac', 'clash.yaml']
    best = []
    exported = 0
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
                row = json.loads(payload)
                verdict = row.get('reputation') or {}
                row['reputation_status'] = verdict.get('status', 'clean')
                row['reputation_sources'] = ','.join(item.get('zone', '') for item in verdict.get('dnsbl', [])
                                                       if item.get('status') == 'listed')
                judged = row.get('anonymity') or {}
                row['country'] = row_country(row, country_of) or ''
                row['exit_ip'] = judged.get('exit_ip', '')
                row['exit_country'] = exit_country(row, country_of) or ''
                history = row_history(row, min_success)
                csv_row = dict(row, anonymity=judged.get('level', ''),
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
                if len(best) < formats.CLASH_LIMIT:
                    best.append(row)
            js.write('\n]\n')
        (generation/'proxy.pac').write_text(formats.pac(row['proxy'] for row in best), encoding='utf-8')
        (generation/'clash.yaml').write_text(formats.clash(best), encoding='utf-8')
        total = db.execute('SELECT count(*) FROM candidates').fetchone()[0]
        report = dict(profile=profile, candidates=total, checked=checked, pending=total-checked,
                      passed=passed, local_filtered=local_filtered, exported=exported, complete=checked == total,
                      generated_at=time.time(), sort=sort, min_success=min_success,
                      protocol=protocol, max_latency=max_latency, countries=list(countries or ()),
                      targets=[dict(name=t.get('name', ''), url=public_url(t['url'])) for t in cfg.get('targets', [])],
                      request_profile=cfg.get('request_profile', 'workbench'),
                      request_profile_digest=cfg.get('request_profile_digest', ''),
                      reputation=dict(policy, counts=status_counts), generation=generation.name,
                      anonymity=dict(enabled=bool(cfg.get('anonymity')), min_level=min_anonymity,
                                     counts=anonymity_counts),
                      source_quality=source_quality, breakdown=breakdown)
        atomic(generation/'status.json', json.dumps(report, indent=2) + '\n')
        atomic(directory/'current.json', json.dumps({'generation':generation.name, 'files':names}, ensure_ascii=False) + '\n')
        published = True
        # Keep legacy root filenames for scripts that already consume them.
        for name in names:
            temporary = directory/(name+'.tmp')
            shutil.copyfile(generation/name, temporary)
            temporary.replace(directory/name)
        atomic(directory / 'status.json', json.dumps(report, indent=2) + '\n')
        prune_export_generations(directory, current=generation.name)
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
  serve                                             API on http://127.0.0.1:8765
  gateway --protocol socks5 --country DE            rotating proxy on 127.0.0.1:8899 (HTTP and SOCKS5)

set PROXY_WORKBENCH_LANG=ru for Russian messages."""
EPILOG_RU = """примеры:
  run --want 20 --country DE,NL --protocol socks5   быстро найти 20 рабочих SOCKS5 из Германии/Нидерландов
  run --url https://example.org/health              проверить на своём сервисе
  run --want 50 --watch 30                          найти 50 и перепроверять их каждые 30 минут
  serve                                             API на http://127.0.0.1:8765
  gateway --protocol socks5 --country DE            ротирующий прокси на 127.0.0.1:8899 (HTTP и SOCKS5)

PROXY_WORKBENCH_LANG=en — сообщения на английском."""


def parser():
    p = argparse.ArgumentParser(formatter_class=argparse.RawDescriptionHelpFormatter, epilog=tr(EPILOG_RU, EPILOG_EN),
                                description=tr(f'{PRODUCT_NAME}: сбор и полная проверка публичных прокси под HTTP-сервис', f'{PRODUCT_NAME}: collect public proxies and fully check them against your HTTP services'))
    p.add_argument('--version', action='version', version=f'{PRODUCT_NAME} {PRODUCT_VERSION}')
    p.add_argument('command', choices=['collect', 'scan', 'run', 'export', 'serve', 'gateway', 'clear-data', 'update-geoip'],
                   help=tr('run — собрать и проверить; collect — только собрать; scan — только проверить; '
                           'export — пересобрать файлы; serve — локальное API; gateway — ротирующий прокси; '
                           'update-geoip — база стран; '
                           'clear-data — удалить результаты',
                           'run: collect and check; collect: only collect; scan: only check; '
                           'export: rebuild the files; serve: local API; gateway: rotating proxy; '
                           'update-geoip: country database; '
                           'clear-data: delete results'))
    p.add_argument('--yes', action='store_true', help=tr('подтвердить удаление локальных результатов', 'confirm deleting local results'))
    p.add_argument('--progress-file', type=Path, help=argparse.SUPPRESS)
    p.add_argument('--stop-file', type=Path, help=argparse.SUPPRESS)
    p.add_argument('--data', type=Path, default=ROOT / 'data',
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
    p.add_argument('--judge-url', help=tr('echo-endpoint для проверки анонимности (transparent/anonymous/elite)', 'echo endpoint for the anonymity check (transparent/anonymous/elite)'))
    p.add_argument('--min-anonymity', choices=anonymity.MIN_LEVELS, default='any',
                   help=tr('минимальный уровень анонимности для экспорта; нужен --judge-url', 'minimum anonymity level for the export; needs --judge-url'))
    p.add_argument('--recheck', action='store_true', help=tr('заново проверить все адреса текущего профиля', 'check every address of the current profile again'))
    p.add_argument('--recheck-passing', action='store_true',
                   help=tr('заново проверить только прокси, которые сейчас проходят отбор (быстро освежить список)', 're-check only the proxies that currently pass (quick refresh)'))
    p.add_argument('--watch', type=float, default=0,
                   help=tr('после проверки перепроверять рабочие прокси каждые N минут и обновлять экспорт; 0 — выключено', 'after the scan, re-check working proxies every N minutes and refresh the export; 0 = off'))
    p.add_argument('--top', type=int, default=0, help=tr('сколько сохранить; 0 — все прошедшие', 'how many to save; 0 = all that pass'))
    p.add_argument('--sort', choices=list(SORTS), default='quality',
                   help=tr('quality: стабильность + скорость; speed: задержка; stability: разброс', 'quality: reliability + speed; speed: latency; stability: jitter; uptime: survived re-checks'))
    p.add_argument('--protocol', choices=list(PROTOCOLS), default='all', help=tr('экспортировать только этот протокол', 'check and export only this protocol'))
    p.add_argument('--max-latency', type=float, default=0, help=tr('максимальная медианная задержка, мс; 0 — без ограничения', 'maximum median latency, ms; 0 = no limit'))
    p.add_argument('--country', default='', help=tr('только эти страны (ISO-коды через запятую, например DE,NL); '
                   'другие адреса не проверяются', 'only these countries (ISO codes, e.g. DE,NL); '
                   'other addresses are not checked'))
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


async def download_geoip(path, timeout=60):
    """Fetch the latest DB-IP Country Lite CSV; returns the month downloaded."""
    error = None
    async with httpx.AsyncClient(trust_env=False, verify=TLS, timeout=timeout, follow_redirects=True) as client:
        for month in geoip.candidate_months():
            async with client.stream('GET', geoip.DOWNLOAD_URL.format(month=month)) as response:
                if response.status_code == 404:
                    error = 'HTTP_404'
                    continue
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > geoip.MAX_DOWNLOAD_BYTES:
                        raise ValueError('GEOIP_TOO_LARGE')
            geoip.validate_download(bytes(body))
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_name(path.name + '.tmp')
            temp.write_bytes(body)
            temp.replace(path)
            return month
    raise ValueError(error or 'GEOIP_UNAVAILABLE')


def serve(args):
    """Read-only HTTP API over the latest export; runs next to scans without the data lock."""
    import api
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
    import gateway
    port = gateway.DEFAULT_PORT if args.port is None else args.port
    if not 0 <= port <= 65535:
        print(tr('Неверный порт шлюза', 'Invalid gateway port'), file=sys.stderr)
        return 2
    filters = dict(protocol=args.protocol, countries=countries, anonymity=args.min_anonymity,
                   max_latency=args.max_latency)

    async def run():
        server = await gateway.start(args.data, args.host, port, args.api_token, filters, args.rotate)
        pool = server.gateway.pool
        shown = f'[{args.host}]' if ':' in args.host else args.host
        address = f'{shown}:{server.sockets[0].getsockname()[1]}'
        print(tr(f'Ротирующий прокси: {address} (HTTP и SOCKS5), в пуле {len(pool.refresh())} прокси. Ctrl+C — остановить.',
                 f'Rotating proxy: {address} (HTTP and SOCKS5), {len(pool.refresh())} proxies in the pool. Ctrl+C to stop.'),
              flush=True)
        print(f'  curl -x http://{address} https://example.org/', flush=True)
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


def main(argv=None):
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
            or not 0 <= args.min_success <= 1 or args.want < 0
            or not math.isfinite(args.watch) or args.watch < 0):
        p.error(tr('Неверные числовые параметры', 'Invalid numeric options'))
    try:
        countries = geoip.parse_countries(args.country)
    except ValueError as exc:
        p.error(tr(str(exc), 'Countries: use two-letter ISO codes, for example DE,NL.'))
    os.umask(0o077)
    args.data.mkdir(parents=True, exist_ok=True)
    if args.command == 'serve':
        return serve(args)
    if args.command == 'gateway':
        return run_gateway(args, countries)
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
    db = open_db(args.data / 'proxies.sqlite3')
    country_of = country_resolver(db, geo)
    config = None
    profile = None
    code = 0
    latest = {}

    def update_progress(values):
        latest.update(values, updated_at=time.time())
        if args.progress_file:
            atomic(args.progress_file, json.dumps(latest))

    update_progress(dict(phase='starting', checked=0, candidates=0))

    def export_now():
        return export(db, profile, args.data / 'exports', top=args.top,
                      sort=args.sort, min_success=args.min_success, denylist=denylist,
                      local_override=args.local_denylist, min_anonymity=args.min_anonymity,
                      protocol=args.protocol, max_latency=args.max_latency or None,
                      countries=countries, country_of=country_of)

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
            atomic(args.data / 'last-profile.txt', profile)

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

            def run_scan(**options):
                asyncio.run(stoppable(scan(db, config, workers=workers, rate=args.rate,
                                           probe=probe, on_progress=update_progress, min_success=args.min_success,
                                           screen=reputation_check, denylist=denylist,
                                           min_anonymity=args.min_anonymity, protocol=args.protocol,
                                           max_latency=args.max_latency or None, countries=countries,
                                           country_of=country_of, **options), args.stop_file))

            run_scan(recheck=args.recheck, recheck_passing=args.recheck_passing, want=args.want)
            while args.watch:
                # Keep the list fresh: publish, wait, then re-check only the proxies that pass.
                report = export_now()
                update_progress(dict(report, phase='waiting', next_check_at=time.time() + args.watch * 60))
                print(tr(f'Сохранено {report["exported"]}. Следующая перепроверка рабочих прокси через {args.watch:g} мин.', f'Saved {report["exported"]}. Next re-check of working proxies in {args.watch:g} min.'),
                      flush=True)
                asyncio.run(stoppable(asyncio.sleep(args.watch * 60), args.stop_file))
                run_scan(recheck_passing=True)
        elif args.command == 'export':
            profile = (args.data / 'last-profile.txt').read_text(encoding='utf-8').strip()
    except (KeyboardInterrupt, asyncio.CancelledError):
        print(tr('Остановлено. Завершённые проверки сохранены; scan продолжит проход.', 'Stopped. Finished checks are saved; the next scan continues where this one stopped.'), flush=True)
        code = 130
    except Exception as exc:
        print(tr(f'Ошибка: {type(exc).__name__}: проверьте файлы и параметры.', f'Error: {type(exc).__name__}: check the files and options.'), file=sys.stderr)
        code = 2
    finally:
        try:
            db.commit()
            if profile and db.execute('SELECT 1 FROM profiles WHERE id=?', (profile,)).fetchone():
                update_progress(dict(phase='exporting'))
                try:
                    report = export_now()
                except Exception as exc:
                    if not code:
                        print(tr(f'Ошибка экспорта: {type(exc).__name__}: проверьте data/ и denylist.', f'Export error: {type(exc).__name__}: check data/ and the denylist.'), file=sys.stderr)
                        code = 2
                    update_progress(dict(phase='error', error=type(exc).__name__))
                else:
                    update_progress(report)
                    print(tr(f'Проверено {report["checked"]}/{report["candidates"]}; подходят {report["passed"]}; сохранено {report["exported"]}', f'Checked {report["checked"]}/{report["candidates"]}; matching {report["passed"]}; saved {report["exported"]}'), flush=True)
        finally:
            update_progress(dict(phase='stopped' if code == 130 else 'error' if code else 'complete', exit_code=code))
            db.close()
            lock.close()
    return code


if __name__ == '__main__':
    raise SystemExit(main())
