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

ROOT = Path(__file__).resolve().parent
TLS = ssl.create_default_context()
SCHEMES = {'http', 'https', 'socks5', 'socks5h'}
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
        if not ip.is_global:
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


def atomic(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(content, encoding='utf-8')
    temp.replace(path)


EXPORT_GENERATION_RETENTION = 3
PROTOCOL_EXPORTS = {'http': 'http.txt', 'https': 'https.txt', 'socks5': 'socks5.txt'}
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
        CREATE TABLE IF NOT EXISTS profiles(id TEXT PRIMARY KEY, config TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS results(
            profile TEXT NOT NULL, proxy TEXT NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY(profile, proxy));
    ''')
    return db


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


def source_spec(value):
    if not isinstance(value, str):
        raise ValueError('Источник должен быть строкой URL или «socks5 URL» / «geonode URL».')
    parts = value.strip().split(None, 1)
    kind, url = (parts if len(parts) == 2 else ('http', parts[0] if parts else ''))
    if kind not in {'http', 'https', 'socks5', 'socks5h', 'geonode', 'http-fields'}:
        raise ValueError('Неизвестный формат источника.')
    try:
        _parse_source_url(url)
    except ValueError as exc:
        raise ValueError(f'Источник: {exc}.') from None
    return kind, url


async def collect(db, urls, inputs, timeout=60, on_progress=None, denylist=None,
                  allow_private_sources=False,
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

    def add(value, protocol='http'):
        value = value.strip()
        proxy = normalize(value if '://' in value else protocol+'://'+value)
        if not proxy:
            return 'invalid'
        if denylist.match(proxy):
            return 'blocked'
        db.execute('INSERT OR IGNORE INTO candidates VALUES (?)', (proxy,))
        return 'accepted'

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
                outcome = add(line)
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
                    outcome = add(match[1]) if match else 'invalid'
                else:
                    outcome = add(line, kind)
                invalid += outcome == 'invalid'
                blocked += outcome == 'blocked'

            def consume_record(record):
                nonlocal count, invalid, blocked, endpoint_count
                protocols = []
                if isinstance(record, dict) and isinstance(record.get('protocols', []), list):
                    protocols = [protocol for protocol in record['protocols']
                                 if protocol in ('http', 'https', 'socks5')]
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
                        outcome = add(f"{host}:{record.get('port')}", 'http' if protocol == 'https' else protocol)
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
            print(f'Источник {index}: строк {count}, заблокировано {blocked}, страниц {pages}, ошибка {error or "нет"}', flush=True)
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
    # Added only when enabled so existing profiles keep their identity.
    judge = anonymity.validate_judge(anonymity_config)
    if judge:
        config['anonymity'] = judge
    return config


async def request_once(proxy, target, config, rate):
    await rate.wait()
    start = time.monotonic()
    result = dict(ok=False, status=None, ms=None, bytes=0, error=None)
    try:
        async with asyncio.timeout(config['timeout']):
            # Fresh connections make samples comparable (including CONNECT/TLS).
            async with httpx.AsyncClient(proxy=proxy, trust_env=False, verify=TLS,
                                         timeout=config['timeout'], follow_redirects=False) as client:
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
        per_target.append(sum(s['ok'] for s in subset) / len(subset))
    median = statistics.median(successful) if successful else None
    jitter = statistics.pstdev(successful) if successful else None
    score = 100 * min(per_target) / (1 + (median + jitter) / 1000) if successful else 0
    return dict(proxy=proxy, reliability=reliability, min_target_reliability=min(per_target),
                latency_ms=median, jitter_ms=jitter, score=round(score, 5),
                successes=len(successful), requests=len(samples), checked_at=time.time(), samples=samples)


async def check_proxy(proxy, config, rate, own_ips=None):
    samples = []
    for attempt in range(config['attempts']):
        for index, target in enumerate(config['targets']):
            sample = await request_once(proxy, target, config, rate)
            samples.append(dict(sample, target=index, attempt=attempt + 1))
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
            async with httpx.AsyncClient(proxy=proxy, trust_env=False, verify=TLS, timeout=config['timeout'],
                                         follow_redirects=False) as client:
                body = await anonymity.fetch_judge(client, config['anonymity']['judge_url'], headers)
    except ValueError as exc:
        return anonymity.result('unknown', error=str(exc), started=started)
    except (httpx.HTTPError, TimeoutError, OSError) as exc:
        return anonymity.result('unknown', error=type(exc).__name__, started=started)
    verdict = anonymity.classify(body, own_ips)
    return anonymity.result(verdict['level'], verdict['signals'], started=started)


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


async def scan(db, config, *, workers=128, rate=100, recheck=False, probe=check_proxy, progress=True, on_progress=None, min_success=2/3, screen=None, denylist=None, min_anonymity='any'):
    denylist = denylist or Denylist.empty()
    policy = config.get('reputation', {})
    strict = bool(policy.get('strict', False))
    active_denylist = denylist if policy.get('local_enabled', True) else None
    if not config.get('anonymity'):
        min_anonymity = 'any'
    encoded = json.dumps(config, sort_keys=True)
    profile = hashlib.sha256(encoded.encode()).hexdigest()[:20]
    db.execute('INSERT OR IGNORE INTO profiles VALUES (?, ?)', (profile, encoded))
    if recheck:
        db.execute('DELETE FROM results WHERE profile=?', (profile,))
    db.commit()
    total = db.execute('SELECT count(*) FROM candidates').fetchone()[0]
    pending = db.execute('''SELECT c.proxy FROM candidates c LEFT JOIN results r
        ON r.proxy=c.proxy AND r.profile=? WHERE r.proxy IS NULL ORDER BY c.proxy''', (profile,))
    completed = db.execute('SELECT count(*) FROM results WHERE profile=?', (profile,)).fetchone()[0]
    passed = 0
    status_counts = {'clean': 0, 'listed': 0, 'unknown': 0, 'local_denied': 0}
    for (payload,) in db.execute('SELECT payload FROM results WHERE profile=?', (profile,)):
        row = json.loads(payload)
        status = (row.get('reputation') or {}).get('status', 'clean')
        status_counts[status] = status_counts.get(status, 0) + 1
        passed += int(result_allowed(row, min_success, denylist=active_denylist, strict=strict, min_anonymity=min_anonymity))
    initial = completed
    limiter = Rate(rate)
    queue = asyncio.Queue(maxsize=workers * 2)
    started = last_commit = time.monotonic()

    async def producer():
        for (proxy,) in pending:
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
                db.execute('INSERT OR REPLACE INTO results VALUES (?, ?, ?)',
                           (profile, proxy, json.dumps(row, ensure_ascii=False)))
                completed += 1
                status = (row.get('reputation') or {}).get('status', 'clean')
                status_counts[status] = status_counts.get(status, 0) + 1
                passed += int(result_allowed(row, min_success, denylist=active_denylist, strict=strict, min_anonymity=min_anonymity))
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
            print(f'Проверено {completed}/{total}; {speed:.1f} прокси/с; осталось ~{eta / 60:.1f} мин', flush=True)

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
        pending.close()
        db.commit()
        publish()
    return profile


def export(db, profile, directory, *, top=0, sort='quality', min_success=2/3, denylist=None, local_override=None, min_anonymity='any'):
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
    db.execute('CREATE TEMP TABLE export_rank(proxy TEXT PRIMARY KEY, score REAL, latency REAL, reliability REAL)')
    checked = passed = local_filtered = 0
    status_counts = {'clean': 0, 'listed': 0, 'unknown': 0, 'local_denied': 0}
    anonymity_counts = {}
    for (payload,) in db.execute('SELECT payload FROM results WHERE profile=?', (profile,)):
        row = json.loads(payload)
        checked += 1
        status = (row.get('reputation') or {}).get('status', 'clean')
        status_counts[status] = status_counts.get(status, 0) + 1
        level = (row.get('anonymity') or {}).get('level')
        if level:
            anonymity_counts[level] = anonymity_counts.get(level, 0) + 1
        eligible = result_allowed(row, min_success, denylist=None, strict=strict, min_anonymity=min_anonymity)
        if eligible and active_denylist is not None and active_denylist.match(row.get('proxy', '')):
            local_filtered += 1
        elif eligible:
            db.execute('INSERT INTO export_rank VALUES (?,?,?,?)',
                       (row['proxy'], row['score'], row['latency_ms'], row['reliability']))
            passed += 1
    order = 'e.latency, e.reliability DESC, e.proxy' if sort == 'speed' else 'e.score DESC, e.latency, e.proxy'
    selected = db.execute(f"""SELECT r.payload FROM export_rank e JOIN results r
        ON r.proxy=e.proxy AND r.profile=? ORDER BY {order} LIMIT ?""", (profile, top or -1))
    fields = ['proxy', 'score', 'latency_ms', 'jitter_ms', 'reliability', 'min_target_reliability',
              'successes', 'requests', 'checked_at', 'reputation_status', 'reputation_sources',
              'anonymity', 'anonymity_signals']
    names = ['proxies.txt', 'ranked.json', 'ranked.csv', *PROTOCOL_EXPORTS.values()]
    exported = 0
    try:
        with (generation/'proxies.txt').open('w', encoding='utf-8') as txt, \
             (generation/'ranked.json').open('w', encoding='utf-8') as js, \
             (generation/'ranked.csv').open('w', encoding='utf-8', newline='') as csv_file, \
             contextlib.ExitStack() as stack:
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
                csv_row = dict(row, anonymity=judged.get('level', ''),
                               anonymity_signals=','.join(judged.get('signals', [])))
                txt.write(row['proxy'] + '\n')
                scheme, _, address = row['proxy'].partition('://')
                by_protocol[PROTOCOL_ALIASES.get(scheme, scheme)].write(address + '\n')
                js.write((',' if exported else '') + '\n' + json.dumps(row, ensure_ascii=False))
                writer.writerow(csv_row)
                exported += 1
            js.write('\n]\n')
        total = db.execute('SELECT count(*) FROM candidates').fetchone()[0]
        report = dict(profile=profile, candidates=total, checked=checked, pending=total-checked,
                      passed=passed, local_filtered=local_filtered, exported=exported, complete=checked == total,
                      generated_at=time.time(), sort=sort, min_success=min_success,
                      targets=[dict(name=t.get('name', ''), url=public_url(t['url'])) for t in cfg.get('targets', [])],
                      request_profile=cfg.get('request_profile', 'workbench'),
                      request_profile_digest=cfg.get('request_profile_digest', ''),
                      reputation=dict(policy, counts=status_counts), generation=generation.name,
                      anonymity=dict(enabled=bool(cfg.get('anonymity')), min_level=min_anonymity,
                                     counts=anonymity_counts))
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


def parser():
    p = argparse.ArgumentParser(description=f'{PRODUCT_NAME}: сбор и полная проверка публичных прокси под HTTP-сервис')
    p.add_argument('--version', action='version', version=f'{PRODUCT_NAME} {PRODUCT_VERSION}')
    p.add_argument('command', choices=['collect', 'scan', 'run', 'export', 'clear-data'])
    p.add_argument('--yes', action='store_true', help='подтвердить удаление локальных результатов')
    p.add_argument('--progress-file', type=Path, help=argparse.SUPPRESS)
    p.add_argument('--stop-file', type=Path, help=argparse.SUPPRESS)
    p.add_argument('--data', type=Path, default=ROOT / 'data')
    p.add_argument('--input', action='append', default=[], help='локальный список прокси; можно повторять')
    p.add_argument('--sources', type=Path, default=ROOT / 'sources.json', help='JSON-массив URL текстовых списков')
    p.add_argument('--no-sources', action='store_true')
    p.add_argument('--source-timeout', type=float, default=60)
    p.add_argument('--allow-private-sources', action='store_true',
                   help='разрешить loopback/private/link-local/reserved/metadata источники (только для локальных mock-сервисов)')
    p.add_argument('--source-max-bytes', '--max-source-bytes', dest='source_max_bytes', type=int,
                   default=DEFAULT_SOURCE_MAX_BYTES, help='максимум байт одного удалённого источника')
    p.add_argument('--source-max-line-bytes', '--max-source-line-bytes', dest='source_max_line_bytes', type=int,
                   default=DEFAULT_SOURCE_MAX_LINE_BYTES, help='максимум байт строки списка')
    p.add_argument('--source-max-candidates', '--max-source-candidates', dest='source_max_candidates', type=int,
                   default=DEFAULT_SOURCE_MAX_CANDIDATES, help='максимум кандидатов одного источника')
    p.add_argument('--source-max-redirects', '--max-source-redirects', dest='source_max_redirects', type=int,
                   default=DEFAULT_SOURCE_MAX_REDIRECTS, help='максимум redirect hops одного запроса')
    target = p.add_mutually_exclusive_group()
    target.add_argument('--url', help='свой URL проверки; по умолчанию https://example.com/')
    target.add_argument('--config', type=Path, help='JSON с targets, HTTP-кодами и проверкой содержимого')
    p.add_argument('--request-profile', choices=sorted(REQUEST_PROFILES), default=None,
                   help='нейтральный HTTP request-профиль')
    p.add_argument('--attempts', type=int, default=3)
    p.add_argument('--timeout', type=float, default=8, help='полный deadline одного запроса, секунд')
    p.add_argument('--workers', type=int, default=128)
    p.add_argument('--rate', type=float, default=100, help='максимум стартов запросов/с, 0 — без лимита')
    p.add_argument('--max-bytes', type=int, default=1048576)
    p.add_argument('--denylist-file', type=Path, default=None, help='локальный IP/CIDR/proxy denylist')
    p.add_argument('--local-denylist', dest='local_denylist', action='store_true', default=None,
                   help='применять локальный denylist')
    p.add_argument('--no-local-denylist', dest='local_denylist', action='store_false', default=None,
                   help='не применять локальный denylist')
    p.add_argument('--dnsbl', dest='dnsbl', action='store_true', default=None,
                   help='включить публичные DNSBL-проверки')
    p.add_argument('--dnsbl-zone', dest='dnsbl_zones', action='append', default=[],
                   help='DNSBL-зона; можно указать несколько раз')
    p.add_argument('--reputation-timeout', type=float, default=None, help='таймаут одной DNSBL-зоны, секунд')
    p.add_argument('--strict-clean', dest='strict_clean', action='store_true', default=None,
                   help='не разрешать прокси с неопределённым DNSBL-результатом')
    p.add_argument('--judge-url', help='echo-endpoint для проверки анонимности (transparent/anonymous/elite)')
    p.add_argument('--min-anonymity', choices=anonymity.MIN_LEVELS, default='any',
                   help='минимальный уровень анонимности для экспорта; нужен --judge-url')
    p.add_argument('--recheck', action='store_true', help='заново проверить все адреса текущего профиля')
    p.add_argument('--top', type=int, default=0, help='сколько сохранить; 0 — все прошедшие')
    p.add_argument('--sort', choices=['speed', 'quality'], default='quality')
    p.add_argument('--min-success', type=float, default=2/3, help='минимальная доля успехов КАЖДОГО target, 0..1')
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


def main(argv=None):
    p = parser()
    args = p.parse_args(argv)
    if (min(args.attempts, args.workers, args.max_bytes) < 1 or args.top < 0
            or not math.isfinite(args.rate) or args.rate < 0
            or not math.isfinite(args.timeout) or args.timeout <= 0
            or not math.isfinite(args.source_timeout) or args.source_timeout <= 0
            or not 1 <= args.source_max_bytes <= MAX_SOURCE_BYTES
            or not 1 <= args.source_max_line_bytes <= MAX_SOURCE_LINE_BYTES
            or not 1 <= args.source_max_candidates <= MAX_SOURCE_CANDIDATES
            or not 0 <= args.source_max_redirects <= MAX_SOURCE_REDIRECTS
            or not 0 <= args.min_success <= 1):
        p.error('Неверные числовые параметры')
    os.umask(0o077)
    args.data.mkdir(parents=True, exist_ok=True)
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
        print('Эта папка data уже используется другим запуском.', file=sys.stderr)
        lock.close()
        return 2
    if args.command == 'clear-data':
        if not args.yes:
            p.error('clear-data требует явного --yes')
        try:
            with exclusive_lock(args.data/'gui-instance.lock'):
                removed = clear_runtime(args.data, keep_lock=True)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            lock.close()
            return 2
        print('Удалено: ' + (', '.join(removed) if removed else 'ничего'), flush=True)
        lock.close()
        return 0
    db = open_db(args.data / 'proxies.sqlite3')
    config = None
    profile = None
    code = 0
    latest = {}

    def update_progress(values):
        latest.update(values, updated_at=time.time())
        if args.progress_file:
            atomic(args.progress_file, json.dumps(latest))

    update_progress(dict(phase='starting', checked=0, candidates=0))
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
                allow_private_sources=args.allow_private_sources,
                max_source_bytes=args.source_max_bytes,
                max_source_line_bytes=args.source_max_line_bytes,
                max_source_candidates=args.source_max_candidates,
                max_source_redirects=args.source_max_redirects), args.stop_file))
            atomic(args.data / 'sources-report.json', json.dumps(report, indent=2) + '\n')
            print(f'Уникальных кандидатов в базе: {report["unique"]}', flush=True)
        if args.command in ('scan', 'run'):
            workers = fit_workers(args.workers)
            print(f'Воркеров: {workers}; полный обход; профиль {profile}', flush=True)
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
                    print(f'Не удалось определить внешний IP через judge URL: {reason}', file=sys.stderr)
                    raise
                print(f'Проверка анонимности: judge {public_url(config["anonymity"]["judge_url"])}', flush=True)

                async def probe(proxy, scan_config, limiter):
                    return await check_proxy(proxy, scan_config, limiter, own_ips=own_ips)

            asyncio.run(stoppable(scan(db, config, workers=workers, rate=args.rate, recheck=args.recheck,
                                       probe=probe, on_progress=update_progress, min_success=args.min_success,
                                       screen=reputation_check, denylist=denylist,
                                       min_anonymity=args.min_anonymity), args.stop_file))
        elif args.command == 'export':
            profile = (args.data / 'last-profile.txt').read_text(encoding='utf-8').strip()
    except (KeyboardInterrupt, asyncio.CancelledError):
        print('Остановлено. Завершённые проверки сохранены; scan продолжит проход.', flush=True)
        code = 130
    except Exception as exc:
        print(f'Ошибка: {type(exc).__name__}: проверьте файлы и параметры.', file=sys.stderr)
        code = 2
    finally:
        try:
            db.commit()
            if profile and db.execute('SELECT 1 FROM profiles WHERE id=?', (profile,)).fetchone():
                update_progress(dict(phase='exporting'))
                try:
                    report = export(db, profile, args.data / 'exports', top=args.top,
                                    sort=args.sort, min_success=args.min_success, denylist=denylist,
                                    local_override=args.local_denylist, min_anonymity=args.min_anonymity)
                except Exception as exc:
                    if not code:
                        print(f'Ошибка экспорта: {type(exc).__name__}: проверьте data/ и denylist.', file=sys.stderr)
                        code = 2
                    update_progress(dict(phase='error', error=type(exc).__name__))
                else:
                    update_progress(report)
                    print(f'Проверено {report["checked"]}/{report["candidates"]}; подходят {report["passed"]}; сохранено {report["exported"]}', flush=True)
        finally:
            update_progress(dict(phase='stopped' if code == 130 else 'error' if code else 'complete', exit_code=code))
            db.close()
            lock.close()
    return code


if __name__ == '__main__':
    raise SystemExit(main())
