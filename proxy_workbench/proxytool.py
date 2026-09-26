#!/usr/bin/env python3
"""Independent public proxy collector and resumable service benchmark."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
from contextlib import asynccontextmanager, contextmanager
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
from . import core
from . import db as schema
from . import diagnostics
from . import geoip
from . import socks4
from . import formats
from .i18n import tr, utf8_output
from . import paths

# ``exportsvc`` imports a handful of constants from this module, so it is
# imported where it is used instead of here.  One contract, one implementation:
# the export path below calls the snapshot service rather than writing its own
# generation directory a second time.

ROOT = paths.PACKAGE
TLS = ssl.create_default_context()
SCHEMES = {'http', 'https', 'socks4', 'socks5', 'socks5h'}
SAFE_TARGET_HEADERS = {'accept', 'accept-encoding', 'accept-language', 'cache-control', 'pragma', 'user-agent', 'x-client-version', 'x-request-id'}

# Source fetching is deliberately bounded before a response is handed to a parser.
# These defaults are finite so a public list cannot consume unbounded memory or CPU.
DEFAULT_SOURCE_MAX_BYTES = 32 * 1024 * 1024
PREVIEW_MAX_BYTES = DEFAULT_SOURCE_MAX_BYTES
DEFAULT_SOURCE_MAX_LINE_BYTES = 64 * 1024
DEFAULT_SOURCE_MAX_CANDIDATES = 500_000
PREVIEW_MAX_CANDIDATES = 200_000
DEFAULT_SOURCE_MAX_REDIRECTS = 5
#: Pages one fetch of a paginated source will walk.  A provider that keeps
#: returning full pages must not be able to hold a collection open forever.
DEFAULT_SOURCE_PAGES = 200
MAX_SOURCE_BYTES = 512 * 1024 * 1024
MAX_SOURCE_LINE_BYTES = 16 * 1024 * 1024
MAX_SOURCE_CANDIDATES = 10_000_000
MAX_SOURCE_REDIRECTS = 20
MAX_SOURCE_PAGES = 5_000
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
async def _source_stream(client, url, allow_private=False, headers=None):
    _, hostname, _, address = await _validate_source_destination(url, allow_private)
    # ``If-None-Match``/``If-Modified-Since`` are how a 304 happens at all, so
    # the conditional headers travel with the very first request of a source
    # whose validators were stored by the previous fetch (F13).
    request_headers = dict(headers or {})
    if address is None:
        async with client.stream('GET', url, headers=request_headers) as response:
            yield response
        return
    transport = PinnedSourceTransport(address, hostname)
    pinned_client = httpx.AsyncClient(transport=transport, trust_env=False, verify=TLS,
                                      follow_redirects=False, timeout=15)
    try:
        async with pinned_client.stream('GET', url, headers=request_headers) as response:
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


async def _read_bounded_body(response, budget, max_bytes, *, keep_prefix=False):
    """Read a body within budget.

    ``keep_prefix`` is the check's mode: past the budget it returns what it read
    and the caller reports ``SOURCE_TRUNCATED`` as a fact about the check.  A
    real collection passes ``keep_prefix=False`` and refuses the whole body, so
    a public list cannot be walked into memory.  The prefix of a JSON array is
    not valid JSON, so the difference is what keeps a truncated check from being
    reported as "this source's format cannot be read".
    """
    remaining = max_bytes - budget['used']
    declared = _declared_response_length(response)
    truncated = False
    # ``Content-Length`` already says the body is past the bound, so the read
    # stops as soon as the bound is reached -- but not before: the flag is set
    # here and the loop below only stops once it has actually filled up.
    full = False
    if declared is not None and declared > remaining:
        if not keep_prefix:
            raise SourceFetchError('SOURCE_TOO_LARGE')
        truncated = True
        full = True
    body = bytearray()
    try:
        async for chunk in response.aiter_bytes():
            if len(chunk) > remaining:
                if not keep_prefix:
                    raise SourceFetchError('SOURCE_TOO_LARGE')
                chunk, truncated = chunk[:remaining], True
            if chunk:
                body.extend(chunk)
                budget['used'] += len(chunk)
                remaining -= len(chunk)
            if full and not remaining:
                break
            if truncated and not full:
                break
    except httpx.HTTPError:
        if not keep_prefix:
            raise
        # A check stops reading at its own bound, and the origin closes the
        # connection when it does; the prefix it kept is the answer.
        truncated = True
    if truncated:
        budget['truncated'] = True
    return bytes(body)


async def _read_bounded_lines(response, budget, max_bytes, max_line_bytes, on_line, *,
                             keep_prefix=False):
    remaining = max_bytes - budget['used']
    declared = _declared_response_length(response)
    truncated = False
    # As in ``_read_bounded_body``: a declared length past the bound means the
    # read stops *at* the bound, not before the first chunk.
    full = False
    if declared is not None and declared > remaining:
        if not keep_prefix:
            raise SourceFetchError('SOURCE_TOO_LARGE')
        truncated = True
        full = True
    pending = bytearray()
    chunks = response.aiter_bytes()
    try:
        async for chunk in chunks:
            if len(chunk) > remaining:
                if not keep_prefix:
                    raise SourceFetchError('SOURCE_TOO_LARGE')
                chunk, truncated = chunk[:remaining], True
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
                        if not keep_prefix:
                            raise SourceFetchError('SOURCE_LINE_TOO_LARGE')
                        truncated = True
                    break
                end = min(positions)
                delimiter_length = 1
                if pending[end:end + 1] == b'\r' and pending[end + 1:end + 2] == b'\n':
                    delimiter_length = 2
                line = bytes(pending[start:end])
                start = end + delimiter_length
                if len(line) > max_line_bytes:
                    if not keep_prefix:
                        raise SourceFetchError('SOURCE_LINE_TOO_LARGE')
                    truncated = True
                on_line(line)
            if start:
                del pending[:start]
            if full and not remaining:
                break
            if truncated and not full:
                break
    except httpx.HTTPError:
        if not keep_prefix:
            raise
        truncated = True
    if pending and not truncated:
        if len(pending) > max_line_bytes:
            if not keep_prefix:
                raise SourceFetchError('SOURCE_LINE_TOO_LARGE')
            truncated = True
        else:
            on_line(bytes(pending))
    if truncated:
        budget['truncated'] = True


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
#: An address with no credentials still has an access identity, and a named one:
#: an empty id is indistinguishable from a row that never recorded who measured
#: it, and admission refuses that (CONTRACTS §1.2 rule 1, §2.3).  The web
#: surface reads the same identity (``gui.App.snapshot_access``); the two have to
#: name the same thing or the table and the engine disagree about which rows
#: exist at all.
PUBLIC_ACCESS_ID = 'default'
PUBLIC_ACCESS = core.Access(PUBLIC_ACCESS_ID, 1)


def as_access(value):
    """One access identity in the shape the admission contract reads.

    The vault's record (`secrets.Access`) and the contract's own `core.Access` are
    two dataclasses with the same meaning, and passing the vault's one used to
    raise `AttributeError: 'Access' object has no attribute 'access_id'` at the
    first row: a caller that had a real credential identity could not measure
    with it.  The credential value is never touched here -- only the identity and
    its revision travel into a row (F04).
    """
    if value is None:
        return PUBLIC_ACCESS
    if isinstance(value, core.Access):
        return value
    identifier = getattr(value, 'access_id', None) or getattr(value, 'id', None)
    if isinstance(identifier, str) and identifier:
        return core.Access(identifier, int(getattr(value, 'access_revision', 0) or 0))
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return core.Access(str(value[0]), int(value[1] or 0))
    raise ValueError('Не удалось прочитать идентичность доступа: ожидается Access, '
                     '(access_id, access_revision) или None')
PROTOCOL_EXPORTS = {'http': 'http.txt', 'https': 'https.txt', 'socks4': 'socks4.txt', 'socks5': 'socks5.txt'}
PROTOCOL_ALIASES = {'socks5h': 'socks5'}

# The measurement row is written with every key column named (CONTRACTS §3.2):
# a positional insert breaks the moment a migration adds a column, which is
# exactly how a stale binary is stopped from writing into a newer schema.
INSERT_RESULT = '''INSERT OR REPLACE INTO results(
    profile, proxy, payload, endpoint_id, access_id, access_revision,
    profile_id, profile_revision, checked_at, valid_until, error_code, error_stage, job_id,
    observation_id)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)'''


def record_candidate_meta(conn, proxy, country=None, source=None):
    """Record where an address came from, in whichever shape the table has.

    ``candidate_meta.source`` was added in 1.6 and the migrator leaves an
    existing two-column table alone, so a database that predates the column is
    still readable and still collectable: the many-to-many ``candidate_seen``
    carries the provenance either way, and this only fills the first-seen
    mirror when the column exists.  (Handoff to ``db.py``: migration 0 could
    backfill this column instead; the engine does not write DDL.)
    """
    if 'source' in meta_columns(conn):
        conn.execute('''INSERT INTO candidate_meta(proxy, country, source) VALUES (?, ?, ?)
            ON CONFLICT(proxy) DO UPDATE SET country=COALESCE(excluded.country, candidate_meta.country),
            source=COALESCE(candidate_meta.source, excluded.source)''',
                       (proxy, country, source))
    elif country:
        conn.execute('''INSERT INTO candidate_meta(proxy, country) VALUES (?, ?)
            ON CONFLICT(proxy) DO UPDATE SET country=COALESCE(excluded.country, candidate_meta.country)''',
                   (proxy, country))
    return True


def meta_columns(conn):
    """Column names of ``candidate_meta`` for *this* connection.

    Cached per connection, not in a module global: the workbench opens several
    databases in one process (tests, the GUI, a tool over a copied file) and a
    global answer from the first one silently describes the others.  The
    attribute lives on the connection, so it dies with it.
    """
    cached = getattr(conn, '_workbench_meta_columns', None)
    if cached is None:
        cached = {row[1] for row in conn.execute('PRAGMA table_info(candidate_meta)')}
        try:
            conn._workbench_meta_columns = cached
        except AttributeError:  # pragma: no cover - a connection without __dict__
            pass
    return cached


def collection_candidates(conn, collection_id):
    """Canonical addresses that belong to one collection, in address order.

    Membership is the scope (CONTRACTS §1.2 rule 2).  A collection with no
    membership rows yields nothing rather than silently falling back to every
    address ever collected -- that fallback is how a private list turned into
    the public base (defect 11).
    """
    return conn.execute(
        'SELECT e.canonical FROM membership m JOIN endpoints e ON e.id = m.endpoint_id '
        'WHERE m.collection_id = ? ORDER BY e.canonical', (collection_id,)).fetchall()


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


def open_db(path, **kwargs):
    """Open the workbench database through the one migrator (``db.py``).

    This function used to own the schema.  It no longer writes DDL: the
    versioned migrator checks ``user_version``/``application_id`` before any
    write, takes a technical backup of a pre-versioning file and refuses a
    database that belongs to another application or to a newer program
    (CONTRACTS §3.2, §3.4, §3.5).  The old contract -- a bare connection -- is
    kept so every existing caller keeps working; callers that want the migration
    report call :func:`open_db_with_report`.
    """
    conn, _report = open_db_with_report(path, **kwargs)
    return conn


def open_db_with_report(path, **kwargs):
    """``open_db`` plus the migration report the CLI and the GUI show once."""
    kwargs.setdefault('app_version', PRODUCT_VERSION)
    conn, report = schema.open_db(path, **kwargs)
    try:
        ensure_public_collection(conn)
    except BaseException:
        conn.close()
        raise
    return conn, report


def ensure_public_collection(conn):
    """The public base always exists; a personal list is never seeded from it.

    Creating it here (and not in a migration) keeps a fresh database usable
    without a second code path: ``db.create_collection`` refuses a duplicate id,
    so this is a no-op on every open after the first (defect 11, F02).
    """
    row = conn.execute('SELECT 1 FROM collections WHERE id=?', (schema.PUBLIC_COLLECTION_ID,)).fetchone()
    if not row:
        conn.execute('INSERT INTO collections(id, name, kind, created_at) VALUES (?,?,?,?)',
                     (schema.PUBLIC_COLLECTION_ID, schema.PUBLIC_COLLECTION_NAME, 'public', time.time()))
        conn.commit()
    return schema.PUBLIC_COLLECTION_ID


def snapshot_network(cfg):
    """The measurement network a snapshot belongs to.

    ``gui.App.snapshot_scope`` reads the same value from
    ``request_profile_digest``, so the engine pins exactly that: a row is
    comparable with a snapshot only when both name the same network.  The status
    also carries an explicit ``network_id`` for readers that prefer it.
    """
    return str((cfg or {}).get('request_profile_digest') or 'default')


def policy_min_success(value):
    """``--min-success 0`` means "no reliability threshold", not "nothing passes".

    The admission policy is defined on ``(0, 1]``, so a zero threshold is carried
    as the smallest positive fraction.  The value the user asked for is still
    what the status reports; only the comparison uses the clamp.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 2 / 3
    return number if number > 0 else 1e-9


_ENDPOINT_COLUMNS = '__pwk_endpoint_columns__'


def endpoint_columns(conn):
    """The writable columns of ``endpoints``, read once per connection.

    ``db.upsert_endpoint`` asks ``PRAGMA table_info`` on *every* call, and a
    collection of 50 000 results makes 500 020 of them: measured at ~7 us for
    the PRAGMA against ~33 us for the whole call, so the column lookup alone is
    a fifth of the most expensive single operation in a scan.

    The cache is keyed on the connection *and* on ``PRAGMA schema_version``, so
    a migration that runs while the connection is open invalidates it by itself
    and cannot let a stale column set be written against a new schema.  The real
    fix belongs in ``db.columns``; until then this is the same answer computed
    once instead of once per row.
    """
    try:
        version = conn.execute('PRAGMA schema_version').fetchone()[0]
    except Exception:  # noqa: BLE001 - a closed or odd connection reads as "no cache"
        return set(schema.columns(conn, 'endpoints')) - {'id', 'canonical'}
    cached = getattr(conn, _ENDPOINT_COLUMNS, None)
    if cached is not None and cached[0] == version:
        return cached[1]
    known = set(schema.columns(conn, 'endpoints')) - {'id', 'canonical'}
    try:
        setattr(conn, _ENDPOINT_COLUMNS, (version, known))
    except AttributeError:  # a Connection subclass that forbids attributes
        pass
    return known


def upsert_endpoint(conn, canonical, **fields):
    """``db.upsert_endpoint`` with its column set cached (see above).

    The statement is the module's own, unchanged: an INSERT that ignores a
    conflict and an UPDATE that names only the columns that exist.
    """
    known = endpoint_columns(conn)
    identifier = schema.endpoint_id(canonical)
    written = {name: value for name, value in fields.items() if name in known}
    conn.execute('INSERT OR IGNORE INTO endpoints(id, canonical) VALUES (?,?)',
                 (identifier, canonical))
    if written:
        names = sorted(written)
        assignments = ', '.join(f'"{name}" = ?' for name in names)
        conn.execute(f'UPDATE endpoints SET {assignments} WHERE id = ?',
                     [written[name] for name in names] + [identifier])
    return identifier


def ensure_collection(conn, collection_id=None):
    """Resolve a scope collection, refusing one that does not exist.

    ``None`` means the public base, which every install has.  A named id that is
    missing is an error rather than a silent fall back: collecting into a list
    the user cannot see is worse than not collecting at all.
    """
    if collection_id in (None, '', schema.PUBLIC_COLLECTION_ID):
        return ensure_public_collection(conn)
    row = conn.execute('SELECT id FROM collections WHERE id=? AND archived_at IS NULL',
                       (str(collection_id),)).fetchone()
    if not row:
        raise ValueError(f'Коллекция не найдена: {collection_id}')
    return str(row['id'] if not isinstance(row, tuple) else row[0])


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


#: The four formats the source catalog knows and the flat ``--sources`` list
#: could not name.  ``source_spec`` accepted nine shapes; 31 catalog entries use
#: one of these four, so ``collect`` could not reach them at all.  They are read
#: by ``source_adapters`` with the profile the catalog carries, never by a
#: second parser here.
CATALOG_ADAPTER_KINDS = ('json-records', 'fields', 'page-json', 'html-table')
#: ``line`` is both: the catalog's own name for a plain list of addresses, and
#: the streaming reader this tree has always used.  It was missing from the
#: accepted kinds, and 77 of the 150 catalog records -- including four of the
#: eight in the ``quick`` set -- name it, so every one of them was dropped from
#: every selection before the collector was ever asked for it.
LINE_KIND = 'line'
SOURCE_KINDS = ('http', 'https', 'socks4', 'socks5', 'socks5h', 'auto', 'text', 'geonode',
                'http-fields', LINE_KIND) + CATALOG_ADAPTER_KINDS
DETECT_PROTOCOLS = ('http', 'socks4', 'socks5')
# ip:port inside free text: "1.2.3.4:8080", "1.2.3.4 8080", CSV and HTML table cells.
LOOSE_ADDRESS = re.compile(r'(?<![\d.])(?:(https?|socks[45]h?)://)?(\d{1,3}(?:\.\d{1,3}){3})'
                           r'(?::|\s*(?:</t[dh]>\s*<t[dh][^>]*>|[\s,;|])\s*)(\d{2,5})(?!\d)', re.I)

#: Backoff after N consecutive failures of one source, and the quarantine that
#: follows when they keep coming.  F13 asks for both: a provider that is down
#: must not be asked every second, and a source that keeps failing must stop
#: being asked at all until something says it is worth trying again.
SOURCE_BACKOFF_S = (60.0, 300.0, 1800.0, 7200.0, 21600.0)
SOURCE_QUARANTINE_AFTER = 3
SOURCE_QUARANTINE_S = 3600.0


class SourcePlan:
    """One remote source: what it is, how its body is read, where it came from.

    ``kind`` is either a flat-list kind (``http``, ``auto``, ``text``,
    ``geonode``) or one of :data:`CATALOG_ADAPTER_KINDS`.  A catalog kind
    carries the adapter profile the catalog record holds, so the catalog's own
    knowledge of the format -- not a guess made at the fetch site -- decides how
    the body is parsed.
    """

    __slots__ = ('source_id', 'url', 'kind', 'profile', 'family_id', 'publisher_id',
                 'dataset_group', 'name', 'protocol', 'custom', 'legacy_specs', 'fallbacks')

    def __init__(self, source_id, url, kind='http', profile=None, *, family_id=None,
                 publisher_id=None, dataset_group=None, name=None, protocol='http', custom=False,
                 legacy_specs=(), fallbacks=()):
        self.source_id = str(source_id)
        self.url = str(url)
        self.kind = str(kind)
        self.profile = profile
        self.family_id = family_id
        self.publisher_id = publisher_id
        self.dataset_group = dataset_group
        self.name = name or self.source_id
        self.protocol = protocol
        self.custom = bool(custom)
        #: The flat specs this record used to be addressed by.  A membership
        #: row written under their hash belongs to this source, not to a
        #: stranger that happens to share its URL.
        self.legacy_specs = tuple(legacy_specs or ())
        #: Other URLs the same publisher serves the same list from.  They are
        #: the same source: a fallback that answered is not a second source,
        #: and the provenance must not split into two.
        self.fallbacks = tuple(fallbacks or ())

    @property
    def identity(self):
        return {'source_id': self.source_id, 'name': self.name, 'kind': self.kind,
                'family_id': self.family_id, 'publisher_id': self.publisher_id,
                'dataset_group': self.dataset_group, 'custom': self.custom}

    def as_dict(self):
        return dict(self.identity, url=self.url)

    def __repr__(self):
        return f'SourcePlan(id={self.source_id!r}, kind={self.kind!r})'


def sources_catalog(data_dir=None, path=None):
    """The accepted source catalog, or the bundled one.

    ``source_catalog.load_catalog`` validates and refuses a manifest that
    promises what it does not carry, so a hand-edited file cannot push an
    unparseable source into the collector.
    """
    from . import source_catalog
    if path is not None:
        return source_catalog.load_catalog(path)
    if data_dir is not None:
        candidate = Path(data_dir) / 'source-catalog.json'
        if candidate.is_file():
            try:
                return source_catalog.load_catalog(candidate)
            except (ValueError, OSError):
                pass
    return source_catalog.load_bundled()


def _source_settings_path(data_dir):
    return Path(data_dir) / 'gui-settings.json'


def _source_settings(data_dir):
    """The stored settings, or an empty document when there are none yet.

    Read without ``gui`` so a headless collection never needs the interface;
    a document that is not an object is refused instead of being replaced,
    because silently overwriting a user's settings is worse than a collection
    that cannot start.
    """
    path = _source_settings_path(data_dir)
    try:
        raw = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError) as exc:
        raise ValueError(f'Не удалось прочитать {path}: {exc}') from None
    try:
        stored = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        raise ValueError(f'Файл {path} повреждён; исправьте его перед продолжением.') from None
    if not isinstance(stored, dict):
        raise ValueError(f'Файл {path} должен содержать объект настроек.')
    return stored


def write_source_settings(data_dir, settings):
    """Persist a selection through the one validator the GUI uses.

    ``source_management.write_settings`` delegates to ``gui.read_settings`` /
    ``gui.save_settings``, which do not exist in this tree (the GUI keeps them as
    ``App.settings`` / ``App.save``), so the CLI writes the file itself -- with
    the same ``gui.validate`` and the same atomic writer the GUI uses, so a
    document written here and one written by the page are indistinguishable.
    """
    from . import gui
    clean = gui.validate(settings)
    atomic(_source_settings_path(data_dir),
           json.dumps(clean, ensure_ascii=False, indent=2) + '\n')
    return clean


def source_selection_view(data_dir=None, catalog=None, *, query='', db=None, now=None, redact=True):
    """One catalog view for the CLI and the API, built by ``source_management``.

    The CLI, the GUI and ``/v1/sources`` all answer from this, so a source's
    support status, dataset group, access kind and runtime state are the same
    numbers everywhere (F13, F21).
    """
    from . import source_management
    catalog = catalog if catalog is not None else sources_catalog(data_dir)
    settings = source_catalog_for(data_dir) if data_dir is not None else {}
    runtime = source_management.runtime_snapshot(db, now=now) if db is not None else None
    return source_management.build_view(catalog, settings, runtime=runtime, query=query,
                                        redact=redact, now=now, db=db)


def source_catalog_for(data_dir):
    """The migrated settings document the catalog selection lives in."""
    from . import source_catalog
    return source_catalog.migrate_settings(_source_settings(data_dir),
                                           sources_catalog(data_dir))


def catalog_source_plans(settings, catalog=None, *, include_disabled=False):
    """The user's selection, resolved into fetchable plans (F13).

    ``source_catalog.migrate_settings`` first, so a tree that still holds the
    old flat URL list keeps every URL and every *pause* -- a source that was in
    the list and switched off used to come back switched on, which silently put
    a disabled list back into the run.
    """
    from . import source_catalog
    if catalog is None:
        catalog = sources_catalog()
    settings = source_catalog.migrate_settings(settings, catalog)
    selection = settings.get('source_selection', {}) if isinstance(settings, dict) else {}
    specs = {}
    # ``specs`` is ``{id: legacy spec}`` -- the exact string the user typed,
    # which outranks what the catalog declares about the same URL.  It was read
    # here as a list of records, so the map produced by ``migrate_settings``
    # iterated as its own keys and every entry was skipped: the format the user
    # chose was silently replaced by the catalog's.
    raw_specs = selection.get('specs') or {}
    if isinstance(raw_specs, dict):
        specs = {key: value for key, value in raw_specs.items() if isinstance(value, str)}
    else:
        for item in raw_specs:
            if isinstance(item, dict) and item.get('id'):
                specs[item['id']] = item
    disabled = set(selection.get('download_disabled_ids') or ())
    plans = []
    for source_id in source_catalog.selection_ids(settings):
        if source_id in disabled and not include_disabled:
            continue
        item = source_catalog.source_by_id(catalog, source_id)
        if item is None or not source_catalog.collectable_source(item):
            continue
        plan = source_plan_of(item, source_id=source_id, spec=specs.get(source_id))
        if plan is not None:
            plans.append(plan)
    return plans, settings


def fetch_catalog(url, *, current=None, timeout=20, max_bytes=8 * 1024 * 1024,
                  allow_private_sources=False, etag=None, last_modified=None):
    """The single bounded owner for fetching a remote source catalog.

    Synchronous, and deliberately so: the caller is a job thread, not a request
    handler.  It reuses the same URL validation, DNS pinning, redirect and
    downgrade policy as a proxy source, sends the stored validators so a
    publisher that has nothing new costs a 304 instead of a body, and decodes
    the answer only through the pure catalog validator -- no response data is
    imported or evaluated.  The caller decides whether to accept it; this
    function never writes anything.
    """
    from . import source_catalog
    current_url = str(url or '').strip()
    if not current_url:
        raise ValueError('Не указан адрес каталога источников.')
    try:
        allowed_host = urlsplit(current_url).hostname
    except ValueError:
        allowed_host = None
    validators = {}
    if etag:
        validators['If-None-Match'] = etag
    if last_modified:
        validators['If-Modified-Since'] = last_modified
    for redirect_count in range(DEFAULT_SOURCE_MAX_REDIRECTS + 1):
        try:
            if allowed_host and urlsplit(current_url).hostname != allowed_host:
                raise SourceFetchError('SOURCE_REDIRECT_HOST')
            with httpx.Client(trust_env=False, verify=TLS, follow_redirects=False,
                              timeout=timeout) as client:
                with _source_stream_sync(client, current_url, allow_private_sources,
                                         validators if not redirect_count else None) as response:
                    if response.status_code == 304:
                        return dict(state='not_modified', catalog=current, etag=etag,
                                    last_modified=last_modified)
                    if response.status_code in SOURCE_REDIRECT_STATUSES:
                        location = response.headers.get('location')
                        if not isinstance(location, str) or not location.strip():
                            raise SourceFetchError('SOURCE_REDIRECT_INVALID')
                        if redirect_count >= DEFAULT_SOURCE_MAX_REDIRECTS:
                            raise SourceFetchError('SOURCE_REDIRECT_TOO_MANY')
                        next_url = urljoin(current_url, location.strip())
                        old_scheme = _parse_source_url(current_url)[0].scheme
                        new_scheme = _parse_source_url(next_url)[0].scheme
                        if old_scheme == 'https' and new_scheme != 'https':
                            raise SourceFetchError('SOURCE_REDIRECT_DOWNGRADE')
                        current_url = next_url
                        continue
                    if response.status_code >= 400:
                        raise SourceFetchError('SOURCE_HTTP_ERROR',
                                               retryable=response.status_code >= 500)
                    body = _read_bounded_sync(response, max_bytes)
                    catalog = source_catalog.decode_catalog_bytes(body, current, max_bytes=max_bytes)
                    return dict(state='available', catalog=catalog,
                                etag=response.headers.get('etag'),
                                last_modified=response.headers.get('last-modified'),
                                body_sha256=hashlib.sha256(body).hexdigest())
        except SourceFetchError:
            raise
    raise SourceFetchError('SOURCE_REDIRECT_TOO_MANY')


def _validate_source_destination_sync(value, allow_private=False):
    """The synchronous twin of :func:`_validate_source_destination`.

    Same rules, same answers, no event loop: a catalog is fetched by a
    background thread, and a second policy is a second way to be wrong.
    """
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
            if _is_blocked_source_ip(alternate):
                raise SourceFetchError('SOURCE_PRIVATE_DESTINATION')
            raise SourceFetchError('SOURCE_URL_INVALID')
        address = None
    if address is not None:
        if _is_blocked_source_ip(address):
            raise SourceFetchError('SOURCE_PRIVATE_DESTINATION')
        return parsed, host, port, str(address)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, ValueError) as exc:
        raise SourceFetchError('SOURCE_DNS_ERROR', retryable=True) from exc
    addresses = [entry[4][0] for entry in infos if entry[4]]
    if not addresses or any(_is_blocked_source_ip(item) for item in addresses):
        raise SourceFetchError('SOURCE_PRIVATE_DESTINATION' if addresses else 'SOURCE_DNS_ERROR',
                               retryable=not addresses)
    return parsed, host, port, str(addresses[0])


@contextmanager
def _source_stream_sync(client, url, allow_private=False, headers=None):
    """The synchronous twin of :func:`_source_stream`, with the same policy.

    A catalog is fetched by a background thread, so it gets the same
    destination validation, the same DNS pinning and the same redirect refusal
    as a source list -- a catalog URL must not be the one route in the app that
    can be pointed anywhere.
    """
    _, hostname, _, address = _validate_source_destination_sync(url, allow_private)
    request_headers = dict(headers or {})
    if address is None:
        with client.stream('GET', url, headers=request_headers) as response:
            yield response
        return
    transport = PinnedSourceTransport(address, hostname)
    pinned = httpx.Client(transport=transport, trust_env=False, verify=TLS,
                          follow_redirects=False, timeout=15)
    try:
        with pinned.stream('GET', url, headers=request_headers) as response:
            yield response
    finally:
        pinned.close()


def _read_bounded_sync(response, max_bytes):
    """Read a response body within ``max_bytes``, or refuse it."""
    declared = _declared_response_length(response)
    if declared is not None and declared > max_bytes:
        raise SourceFetchError('SOURCE_TOO_LARGE')
    body = bytearray()
    for chunk in response.iter_bytes():
        if len(body) + len(chunk) > max_bytes:
            raise SourceFetchError('SOURCE_TOO_LARGE')
        body.extend(chunk)
    return bytes(body)


def bundled_sources_path():
    """The flat URL list the collector has always read by default."""
    return ROOT / 'sources.json'


def resolve_collect_sources(args, *, data=None, catalog=None):
    """What ``collect`` and ``run`` actually fetch.

    Three rules, in this order:

    1. ``--no-sources`` fetches nothing.
    2. An explicit ``--sources FILE`` is authoritative, whatever the file holds.
       A user who names a list gets that list, never the catalog's opinion.
    3. Otherwise the user's *selection* is what is fetched, each source as a
       catalog record so it is read with its own adapter, limits and
       provenance.  A user who has never chosen anything -- a stored document
       with no ``source_selection`` at all -- keeps the pre-catalog flat list,
       so the app never changes what it downloads behind someone's back.

    Rule 3 is the one that was missing.  ``--sources`` defaults to the bundled
    list of 55 URLs, so the flat list was never empty, so the selection was
    never consulted: a source switched on in the catalog screen and nothing
    else was silently never downloaded.  The migration written back here is
    idempotent, which is what turns an old flat-URL tree into a selection once
    and keeps every existing URL and every existing pause.
    """
    from . import source_catalog, source_management
    data = getattr(args, 'data', None) if data is None else data
    catalog = catalog if catalog is not None else sources_catalog(data)
    explicit = getattr(args, 'sources', None)
    if getattr(args, 'no_sources', False):
        return []
    bundled_default = explicit is not None and Path(explicit).resolve() == bundled_sources_path().resolve()
    if not bundled_default:
        return read_sources_file(explicit)
    # The effective settings document: the stored one, or the defaults for an
    # install that has never saved any.  ``read_settings`` migrates on the way,
    # so an old flat-URL tree reads as the 55 catalog records it always meant
    # and a tree that never chose anything reads as the default selection --
    # the same 55 sources, now with adapters and provenance.
    settings = source_management.read_settings(data) if data is not None else None
    selection = settings.get('source_selection') if isinstance(settings, dict) else None
    if not isinstance(selection, dict) or not selection.get('selected_ids'):
        return read_sources_file(explicit)
    _plans, migrated = catalog_source_plans(settings, catalog)
    if data is not None:
        write_source_settings(data, migrated)
    custom = {item.get('id'): item for item in selection.get('custom_sources') or ()
              if isinstance(item, dict)}
    values = []
    for source_id in source_catalog.selection_ids(migrated):
        item = source_catalog.source_by_id(catalog, source_id, list(custom.values()))
        if item is not None:
            values.append(item)
    return values


def read_sources_file(path):
    """The collector's ``--sources`` file, as a de-duplicated list of specs."""
    return list(dict.fromkeys(json.loads(Path(path).read_text(encoding='utf-8'))))


def source_plan_of(item, source_id=None, spec=None):
    """Turn one catalog record into a :class:`SourcePlan`, or ``None``."""
    from . import source_catalog
    if spec and spec.get('endpoints'):
        item = spec
    if not isinstance(item, dict):
        return None
    source_id = source_id or item.get('id')
    endpoints = item.get('endpoints') or []
    if not endpoints or not isinstance(endpoints[0], dict):
        return None
    url = endpoints[0].get('url') or ''
    if not url:
        return None
    fallbacks = tuple(str(point.get('url')) for point in endpoints[1:]
                      if isinstance(point, dict) and point.get('url'))
    adapter = item.get('adapter') or {}
    kind = str(adapter.get('kind') or 'line')
    if kind not in SOURCE_KINDS:
        return None
    profile = None
    if kind in CATALOG_ADAPTER_KINDS or kind == LINE_KIND:
        profile = {'kind': kind, 'profile': str(adapter.get('profile') or 'generic-v1'),
                   'config': dict(adapter.get('config') or {})}
    return SourcePlan(
        source_id, url, kind, profile,
        family_id=item.get('family_id'), publisher_id=(item.get('publisher') or {}).get('id'),
        dataset_group=source_catalog.dataset_group_of(item), name=item.get('name'),
        custom=bool(item.get('custom')),
        legacy_specs=tuple(item.get('legacy_specs') or ()),
        fallbacks=fallbacks)


def _dedupe_sources(values):
    """Drop a repeated source without asking a catalog record to be hashable.

    ``dict.fromkeys`` is the obvious way to de-duplicate a source list and it
    works only while every entry is a string.  A catalog record is a dict, and
    one dict in the list raised ``TypeError: unhashable type: 'dict'`` before a
    single byte was fetched, so the whole catalog half of the collector was
    unreachable through its own documented entry point.  Identity is decided by
    the same three things :func:`plan_of_legacy_spec` derives -- the id, the
    URL and the adapter -- so a record and the plain string that names the same
    list collapse into one fetch instead of two.
    """
    result, seen = [], set()
    for value in values:
        if isinstance(value, str):
            key = (value.strip(),)
        elif isinstance(value, SourcePlan):
            key = (value.source_id, value.url, value.kind)
        elif isinstance(value, dict):
            adapter = value.get('adapter') if isinstance(value.get('adapter'), dict) else {}
            url = ''
            endpoints = value.get('endpoints')
            if isinstance(endpoints, list) and endpoints and isinstance(endpoints[0], dict):
                url = str(endpoints[0].get('url') or '')
            key = (str(value.get('id') or ''), url, str(adapter.get('kind') or ''))
        else:
            key = (str(value),)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def plan_of_legacy_spec(value, url, kind, catalog=None):
    """A flat ``kind URL`` string resolved against the catalog, or ``None``.

    Two things a flat spec must not lose on its way into the collector:

    * the *identity*.  ``source_key`` is a hash of the spec text, so the same
      list collected from ``sources.json`` and from the catalog screen would be
      two different sources with two disjoint sets of counters, and every
      per-source view would show each of them half-empty.  A spec the catalog
      owns is therefore fetched under the catalog's own id, and the membership
      rows a previous flat run already wrote under the hash are re-stated under
      the catalog id before the network is touched (see
      :func:`adopt_legacy_membership`).
    * the *format*.  A spec that names one of the four catalog formats and
      carries no profile is still that format: ``source_adapters`` ships a
      documented ``generic-v1`` profile for every one of them, and refusing it
      made ``source add --source-format json-records`` produce a source the
      collector then declined to read.
    """
    from . import source_catalog
    if not isinstance(value, str):
        return None
    text = value.strip()
    catalog = catalog if catalog is not None else sources_catalog()
    aliases = _legacy_aliases(catalog)
    source_id = aliases.get(text) or aliases.get(kind + ' ' + url)
    if source_id is not None:
        item = source_catalog.source_by_id(catalog, source_id)
        if item is not None:
            plan = source_plan_of(item)
            if plan is not None:
                return plan
    if kind not in CATALOG_ADAPTER_KINDS:
        return None
    # A registered format with no profile of its own: the kind's documented
    # generic profile, never a second parser guessed at the fetch site.  The id
    # is the catalog's own ``custom-`` form of the spec, so the same URL named
    # once as a string and once through the catalog is one source, and any
    # membership an older flat run wrote under the spec hash is adopted by
    # :func:`adopt_legacy_membership`.
    custom = source_catalog.custom_id(text)
    return SourcePlan(custom, url, kind,
                      {'kind': kind, 'profile': 'generic-v1', 'config': {}},
                      family_id=custom, publisher_id='local',
                      name=kind, custom=True, legacy_specs=(text,))


def _legacy_aliases(catalog):
    """``{flat spec: catalog id}``, built once per catalog object.

    The map walks all 150 records; rebuilding it per source turned one run into
    ``len(sources) x len(catalog)`` work.  ``source_catalog`` owns the cache so
    the settings reader and the collector share one map instead of two.
    """
    from . import source_catalog
    return source_catalog.legacy_aliases(catalog)


def adopt_legacy_membership(db, plan):
    """Re-state membership a flat run wrote under the hash under the catalog id.

    Additive and idempotent by construction: an ``INSERT OR IGNORE`` for the
    membership pair and an ``UPDATE ... WHERE source IS NULL`` for the metadata
    the hash run left pointing at itself.  Running it twice, or on a database
    that never had a flat run, changes nothing.
    """
    from . import source_catalog
    legacy_specs = getattr(plan, 'legacy_specs', None) or ()
    if not legacy_specs:
        return 0
    moved = 0
    for spec in legacy_specs:
        old_id = source_key(spec)
        if old_id == plan.source_id:
            continue
        try:
            rows = [row[0] for row in
                    db.execute('SELECT proxy FROM candidate_seen WHERE source=?', (old_id,))]
        except sqlite3.Error:
            continue
        if not rows:
            continue
        moved += len(rows)
        for proxy in rows:
            endpoint = db.execute('SELECT id FROM endpoints WHERE canonical=?', (proxy,)).fetchone()
            endpoint_id = endpoint[0] if endpoint is not None else None
            db.execute('INSERT OR IGNORE INTO candidate_seen(proxy, source, endpoint_id) VALUES (?,?,?)',
                       (proxy, plan.source_id, endpoint_id))
        db.execute('UPDATE candidate_meta SET source=? WHERE source=?', (plan.source_id, old_id))
    if moved:
        db.execute('INSERT OR REPLACE INTO source_identity(source_id, family_id, publisher_id, metadata_json)'
                   ' VALUES (?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET'
                   ' family_id=excluded.family_id, publisher_id=excluded.publisher_id',
                   (plan.source_id, plan.family_id, plan.publisher_id, json.dumps(plan.identity, sort_keys=True)))
    return moved


def loose_addresses(line):
    """Every proxy-looking address in a line of arbitrary text."""
    return [f'{(scheme or "http").lower()}://{host}:{port}' for scheme, host, port in LOOSE_ADDRESS.findall(line)]


def source_spec(value):
    """One flat-list source: a ``(kind, url)`` pair.

    The shape is unchanged -- callers across the tree unpack two values -- and a
    :class:`SourcePlan` passes through, so a caller that already resolved the
    catalog does not have to render it back to a string the catalog cannot
    express.  :func:`source_plan_of` is the companion for callers that need the
    identity the plan carries.
    """
    if isinstance(value, SourcePlan):
        return (value.kind, value.url)
    if isinstance(value, dict):
        plan = source_plan_of(value)
        if plan is None:
            raise ValueError('Некорректная запись источника.')
        return (plan.kind, plan.url)
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
    # A catalog kind named as a bare string carries no adapter profile, so it
    # is accepted here (settings validation and the migration round-trip both
    # go through this function) and refused at the one place that would have to
    # guess the format: the fetch.  A settings document that could not be
    # validated would be far worse than a source that says what it needs.
    return kind, url


def _source_backoff_seconds(failures):
    index = max(0, min(len(SOURCE_BACKOFF_S) - 1, int(failures) - 1))
    return SOURCE_BACKOFF_S[index]


def _source_headers(plan, state):
    """Conditional-request headers from the stored state (F13: 304 matters)."""
    headers = {}
    etag = (state or {}).get('etag')
    modified = (state or {}).get('last_modified')
    if etag:
        headers['If-None-Match'] = etag
    if modified:
        headers['If-Modified-Since'] = modified
    return headers


def _source_state_row(db, source_id, endpoint_id=''):
    """The stored state of one source, whatever row factory the caller set.

    ``sqlite3.Row``, a plain tuple and a dict row are all read here, because the
    same connection is handed in by the CLI, the API and the tests and each of
    them configures ``row_factory`` its own way.
    """
    cursor = db.execute('SELECT * FROM source_state WHERE source_id=? AND endpoint_id=?',
                        (source_id, endpoint_id or ''))
    row = cursor.fetchone()
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        return dict(row)
    if isinstance(row, dict):
        return dict(row)
    return dict(zip([item[0] for item in cursor.description], row))


def record_source_observation(db, run_id, plan, endpoint_url, *, started_at, ended_at,
                              http_state, parse_state, cache_state, outcome, status=None,
                              attempts=0, pages=0, nbytes=0, received=0, recognized=0,
                              accepted=0, rejected=0, duplicate=0, blocked=0,
                              new_endpoints=0, partial=0, error=None, retryable=False,
                              body_sha256=None, profile_digest=None, retry_after=None):
    """One fetch of one source, with the counters F21 compares sources by.

    Counters are about *this* fetch, not about the source in general: "raw",
    "valid", "duplicate", "new".  A source that offered nothing is recorded as
    a run that delivered nothing, which is a different statement from one that
    was never tried.
    """
    db.execute(
        'INSERT OR REPLACE INTO source_observation(run_id, source_id, endpoint_id, started_at, ended_at,'
        ' http_state, parse_state, cache_state, outcome, status, attempts, pages, bytes, received,'
        ' recognized, accepted, rejected, duplicate, duplicates_existing, blocked, new_endpoints,'
        ' partial, error, retryable, body_sha256, fallback_used, profile_digest, retry_after)'
        ' VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (run_id, plan.source_id, endpoint_url, started_at, ended_at, http_state, parse_state,
         cache_state, outcome, status, int(attempts), int(pages), int(nbytes), int(received),
         int(recognized), int(accepted), int(rejected), int(duplicate), 0, int(blocked),
         int(new_endpoints), 1 if partial else 0, error, 1 if retryable else 0, body_sha256,
         0, profile_digest, retry_after))


def stored_generation(db, source_id, now=None, endpoint_id=''):
    """The last good answer a source gave, with its age, or ``None``.

    Read by the collector when the origin answers ``304 Not Modified``: the
    conditional request proved the stored answer is still current, so the run
    reports that answer instead of an empty one.  ``record_count`` is the number
    the run counted when it was fetched; the age is measured from when it was
    written, so "delivered 51 936, 4 minutes old" is a statement about the
    source and not about this run.
    """
    clock = now or time.time
    try:
        state = _source_state_row(db, source_id, endpoint_id)
        if state is not None and state.get('last_good_generation'):
            row = db.execute('SELECT id, record_count, created_at FROM source_generation WHERE id=?',
                             (state['last_good_generation'],)).fetchone()
        else:
            row = db.execute('SELECT id, record_count, created_at FROM source_generation'
                             ' WHERE source_id=? AND last_good=1 ORDER BY id DESC LIMIT 1',
                             (source_id,)).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    created = float(row['created_at'] if isinstance(row, sqlite3.Row) else row[2] or 0)
    return {'generation_id': int(row['id'] if isinstance(row, sqlite3.Row) else row[0]),
            'rows': int(row['record_count'] if isinstance(row, sqlite3.Row) else row[1] or 0),
            'age_seconds': max(0.0, clock() - created) if created else None}


def record_source_generation(db, plan, observation_id, endpoints, *, state, now, endpoint_url=None,
                             profile_digest=None, estimated_bytes=0):
    """An immutable snapshot of what one source offered this time.

    A generation is a record of an answer, not a mutable list: a later fetch
    that fails leaves it alone, which is what "last good" means and what keeps
    a provider outage from wiping a working collection.
    """
    if state in ('304', 'not_modified'):
        # A 304 is a validation of the transport, not a new answer.  Creating a
        # generation here is what made "not modified" look like "delivered
        # nothing" in the source views.
        return None
    cursor = db.execute(
        'INSERT INTO source_generation(source_id, observation_id, state, active, last_good,'
        ' created_at, record_count, estimated_bytes, profile_digest, endpoint_url)'
        ' VALUES (?,?,?,0,0,?,?,?,?,?)',
        (plan.source_id, observation_id, state, now, len(endpoints), int(estimated_bytes),
         profile_digest, endpoint_url))
    generation = cursor.lastrowid
    db.executemany('INSERT OR IGNORE INTO source_generation_entry(generation_id, endpoint_id,'
                   ' metadata_json) VALUES (?,?,?)',
                   [(generation, value, None) for value in dict.fromkeys(endpoints)])
    db.execute('UPDATE source_generation SET active=1, last_good=1 WHERE id=?', (generation,))
    return generation


def record_source_state(db, plan, endpoint_url, *, now, etag=None, last_modified=None,
                        final_url=None, success=False, error=None, retry_after=None,
                        previous=None, generation=None, not_modified=False):
    """Cache validators, backoff and quarantine of one source (F13).

    A successful fetch clears the backoff and the quarantine; three failures in
    a row quarantine the source, so a provider that has gone away is asked
    hourly instead of every collection.  The previous generation is kept and
    only promoted to ``last_good`` on a real answer -- a 304 is a validation of
    the transport, not a new one.
    """
    previous = previous or {}
    # 304 is a working transport with a current answer, so it must not spend the
    # failure budget: counting it as a failure quarantined a perfectly healthy
    # source after three collections, and every collection after that stopped
    # asking it at all.
    healthy = bool(success or not_modified)
    failures = 0 if healthy else int(previous.get('consecutive_failures') or 0) + 1
    backoff_until = None
    quarantine_until = previous.get('quarantine_until')
    if not healthy:
        backoff_until = now + _source_backoff_seconds(failures)
        if failures >= SOURCE_QUARANTINE_AFTER:
            quarantine_until = now + SOURCE_QUARANTINE_S
    current = previous.get('current_generation')
    last_good = previous.get('last_good_generation')
    if success and generation:
        current, last_good = generation, generation
    values = (plan.source_id, '', final_url or endpoint_url, etag, last_modified, now,
              now if healthy else previous.get('last_success_at'),
              previous.get('last_body_at'),
              now if not_modified else previous.get('last_304_at'),
              current, last_good, failures, backoff_until, quarantine_until, retry_after, error)
    db.execute(
        'INSERT INTO source_state(source_id, endpoint_id, final_url, etag, last_modified,'
        ' last_attempt_at, last_success_at, last_body_at, last_304_at, current_generation,'
        ' last_good_generation, consecutive_failures, backoff_until, quarantine_until, retry_after,'
        ' last_error)'
        ' VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)'
        ' ON CONFLICT(source_id, endpoint_id) DO UPDATE SET'
        ' final_url=excluded.final_url, etag=excluded.etag, last_modified=excluded.last_modified,'
        ' last_attempt_at=excluded.last_attempt_at,'
        ' last_success_at=excluded.last_success_at, last_body_at=excluded.last_body_at,'
        ' last_304_at=excluded.last_304_at, current_generation=excluded.current_generation,'
        ' last_good_generation=excluded.last_good_generation,'
        ' consecutive_failures=excluded.consecutive_failures, backoff_until=excluded.backoff_until,'
        ' quarantine_until=excluded.quarantine_until, retry_after=excluded.retry_after,'
        ' last_error=excluded.last_error',
        values)


def record_source_identity(db, plan):
    """Family, publisher and dataset of one source.

    ``dataset_group`` is what F21 needs to tell "the same list published twice"
    from "two independent publishers"; without it two byte-identical lists look
    like independent corroboration.
    """
    db.execute('INSERT INTO source_identity(source_id, family_id, publisher_id, metadata_json)'
               ' VALUES (?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET'
               ' family_id=excluded.family_id, publisher_id=excluded.publisher_id,'
               ' metadata_json=excluded.metadata_json',
               (plan.source_id, plan.family_id, plan.publisher_id,
                json.dumps(plan.identity, sort_keys=True)))


def source_contributions(db, source_ids=()):
    """What each source actually contributed, for the source views (F21).

    ``source_management.attach_contributions`` calls this by name and checks
    that it exists, so an exclusive set and a shared set are computed from the
    same numbers on every surface.
    """
    if not source_ids:
        return {}
    result = {}
    for source_id in source_ids:
        rows = db.execute('SELECT count(*) FROM candidate_seen WHERE source=?', (source_id,)).fetchone()[0]
        result[source_id] = {'seen': int(rows)}
    return result


def _write_source_provenance(db, run_id, plan, url, *, started_at, clock, http_state, parse_state,
                             outcome, status, attempts, pages, nbytes, received, recognized,
                             accepted, rejected, duplicate, blocked, new_endpoints, partial,
                             error, body_sha256, delivered, values, previous, final_url, not_modified,
                             etag=None, last_modified=None, stale=False):
    """Observation, generation and cache state of one fetch, in that order.

    The order matters and is the whole of "last good": the observation is
    written first because the generation points at it, and the state is written
    last because a generation id is what it has to store.  A failed or empty
    fetch writes an observation and touches no generation, so the previous
    answer survives a provider outage -- and reports itself as
    ``stale_last_good`` rather than as a source that delivered nothing.
    """
    ended = clock()
    cache_state = 'not_modified' if not_modified else (
        'last_good' if delivered else ('stale_last_good' if stale else 'none'))
    record_source_observation(
        db, run_id, plan, final_url or url, started_at=started_at, ended_at=ended,
        http_state=http_state, parse_state=parse_state, cache_state=cache_state, outcome=outcome,
        status=status, attempts=attempts, pages=pages, nbytes=nbytes, received=received,
        recognized=recognized, accepted=accepted, rejected=rejected, duplicate=duplicate,
        blocked=blocked, new_endpoints=new_endpoints, partial=partial, error=error,
        body_sha256=body_sha256)
    observation = db.execute('SELECT id FROM source_observation WHERE run_id=? AND source_id=?'
                             ' AND endpoint_id=?', (run_id, plan.source_id, final_url or url)).fetchone()
    observation_id = observation[0] if observation is not None else None
    generation = None
    if delivered:
        generation = record_source_generation(
            db, plan, observation_id, sorted(values), state=outcome, now=ended,
            endpoint_url=final_url or url, estimated_bytes=nbytes)
    record_source_state(db, plan, url, now=ended, etag=etag, last_modified=last_modified,
                        final_url=final_url, success=delivered, error=error, previous=previous,
                        generation=generation, not_modified=not_modified)
    return {'observation_id': observation_id, 'generation_id': generation,
            'cache_state': cache_state, 'outcome': outcome}


async def collect(db, urls, inputs, timeout=60, on_progress=None, denylist=None,
                  allow_private_sources=False, detect_protocols=False,
                  max_source_bytes=DEFAULT_SOURCE_MAX_BYTES,
                  max_source_line_bytes=DEFAULT_SOURCE_MAX_LINE_BYTES,
                  max_source_candidates=DEFAULT_SOURCE_MAX_CANDIDATES,
                  max_source_redirects=DEFAULT_SOURCE_MAX_REDIRECTS,
                  collection_id=None, origin='public', allow_private_endpoints=False,
                  plans=None, now=None, record_provenance=True,
                  *, preview=False, bounded_prefix=False, max_pages=DEFAULT_SOURCE_PAGES,
                  sleep=None, sample_limit=50):
    """Download every source and put the addresses into one collection.

    ``urls`` is the flat ``--sources`` list; ``plans`` is the same thing after
    the source catalog resolved the user's selection.  Both go through
    :func:`source_spec`, so one fetch path serves both and a catalog entry is
    read by the adapter the catalog names for it.

    What a source offered is recorded, not just consumed: an observation with
    its counters, a generation (an immutable answer), and the cache validators
    plus backoff and quarantine of the next attempt (F13, F21).

    ``preview`` runs the identical fetch/adapter/limit path against a private
    temporary database, so a check of a source can answer every question a real
    collection would and still not touch candidates, provenance or last-good.
    ``bounded_prefix`` is that check's read mode: past the byte budget it keeps
    the prefix it read and reports the truncation as a fact about the *check*,
    where a real collection still refuses the whole body so a public list
    cannot fill memory.
    """
    if not isinstance(allow_private_sources, bool):
        raise ValueError('allow_private_sources: ожидается bool')
    if not isinstance(allow_private_endpoints, bool):
        raise ValueError('allow_private_endpoints: ожидается bool')
    max_source_bytes = _source_limit(max_source_bytes, 'max_source_bytes', maximum=MAX_SOURCE_BYTES)
    max_source_line_bytes = _source_limit(max_source_line_bytes, 'max_source_line_bytes', maximum=MAX_SOURCE_LINE_BYTES)
    max_source_candidates = _source_limit(max_source_candidates, 'max_source_candidates', maximum=MAX_SOURCE_CANDIDATES)
    max_source_redirects = _source_limit(max_source_redirects, 'max_source_redirects', minimum=0, maximum=MAX_SOURCE_REDIRECTS)
    if max_pages is not None:
        max_pages = _source_limit(max_pages, 'max_pages', minimum=1, maximum=MAX_SOURCE_PAGES)
    line_limit = min(max_source_line_bytes, max_source_bytes)

    reports = []
    total_rows = 0
    denylist = denylist or Denylist.empty()
    clock = now or time.time
    if preview:
        with tempfile.TemporaryDirectory(prefix='proxy-workbench-preview-') as directory:
            temporary = open_db(Path(directory) / 'preview.sqlite3')
            try:
                answer = await collect(
                    temporary, urls, inputs, timeout, on_progress, denylist, allow_private_sources,
                    detect_protocols, max_source_bytes, max_source_line_bytes, max_source_candidates,
                    max_source_redirects, collection_id=None, origin=origin,
                    allow_private_endpoints=allow_private_endpoints, plans=plans, now=now,
                    record_provenance=record_provenance, bounded_prefix=bounded_prefix,
                    max_pages=max_pages, sleep=sleep)
            finally:
                sample = [row[0] for row in temporary.execute(
                    'SELECT proxy FROM candidates ORDER BY proxy LIMIT ?', (int(sample_limit),))]
                temporary.close()
        answer['preview'] = True
        answer['sample'] = sample
        answer['limits'] = {'max_bytes': max_source_bytes, 'max_candidates': max_source_candidates}
        return answer
    specs = []
    for value in _dedupe_sources(list(urls or ()) + list(plans or ())):
        kind, url = source_spec(value)
        if isinstance(value, SourcePlan):
            specs.append((kind, url, value))
        elif isinstance(value, dict):
            specs.append((kind, url, source_plan_of(value)))
        else:
            plan = plan_of_legacy_spec(value, url, kind)
            if plan is not None:
                specs.append((kind, url, plan))
            else:
                specs.append((kind, url, None))
    run_id = f'src-{int(clock() * 1000)}-{os.getpid()}'
    # Collecting into a named list writes membership there; the public source
    # lists keep filling the public base.  Nothing is ever copied between the two.
    collection_id = ensure_collection(db, collection_id)
    if origin not in schema.COLLECTION_ORIGINS:
        raise ValueError('origin: неизвестное происхождение членства коллекции')

    def publish():
        if on_progress:
            on_progress(dict(phase="collecting", sources_done=sum("source" in r for r in reports),
                             sources_total=len(specs), sources=reports, raw_rows=total_rows,
                             blocked=sum(r.get('blocked', 0) for r in reports),
                             candidates=db.execute("SELECT count(*) FROM candidates").fetchone()[0]))

    def add(value, protocol='http', country=None, source=None, public_only=True, seen=None):
        value = value.strip()
        raw = value if '://' in value else protocol+'://'+value
        # A remote source never gets to name a hostname or a private address,
        # whatever the flag says: only a list the user handed over locally does.
        proxy = normalize(raw) if public_only else normalize_custom(raw)
        if not proxy:
            return 'invalid'
        if denylist.match(proxy):
            return 'blocked'
        if not (isinstance(country, str) and geoip.COUNTRY_CODE.fullmatch(country.upper())):
            country = None
        # One endpoint entity per canonical address; the collection membership
        # is the scope, and the legacy ``candidates`` row keeps the old readers
        # working (CONTRACTS §1.2 rule 2, §3.3 migrations 1-2).
        existed = db.execute('SELECT 1 FROM endpoints WHERE canonical=?', (proxy,)).fetchone() is not None
        endpoint = upsert_endpoint(db, proxy, country=country,
                                   country_at=time.time() if country else None,
                                   country_source='source' if country else None)
        db.execute('INSERT OR IGNORE INTO candidates(proxy, endpoint_id) VALUES (?, ?)', (proxy, endpoint))
        schema.add_member(db, collection_id, endpoint, origin=origin)
        if source:
            # How many lists offer an address: rare ones are less crowded and tend to live longer.
            duplicate = db.execute('SELECT 1 FROM candidate_seen WHERE proxy=? AND source=?',
                                   (proxy, source)).fetchone() is not None
            db.execute('INSERT OR IGNORE INTO candidate_seen(proxy, source, endpoint_id) VALUES (?, ?, ?)',
                       (proxy, source, endpoint))
            db.execute('INSERT OR REPLACE INTO membership_source(collection_id, endpoint_id, source_id,'
                       ' origin, added_at, last_seen_at) VALUES (?,?,?,?,?,?)'
                       ' ON CONFLICT(collection_id, endpoint_id, source_id) DO UPDATE SET'
                       ' last_seen_at=excluded.last_seen_at',
                       (collection_id, endpoint, source, origin, clock(), clock()))
            if seen is not None:
                if proxy in seen:
                    if duplicate:
                        seen['duplicate'] = seen.get('duplicate', 0) + 1
                else:
                    seen['new'] = seen.get('new', 0) + (0 if existed else 1)
                    seen['values'].add(proxy)
        if country or source:
            record_candidate_meta(db, proxy, country and country.upper(), source)
        return 'accepted'


    def add_detected(value, source=None, public_only=True, seen=None):
        """Unlabeled addresses are tried as every protocol; the checks show which one works."""
        value = value.strip()
        if '://' in value:
            return add(value, source=source, public_only=public_only, seen=seen)
        outcomes = [add(value, protocol, source=source, public_only=public_only, seen=seen)
                    for protocol in DETECT_PROTOCOLS]
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
                outcome = (add_detected(line, source='local', public_only=not allow_private_endpoints)
                           if detect_protocols else add(line, source='local',
                                                        public_only=not allow_private_endpoints))
                invalid += outcome == 'invalid'
                blocked += outcome == 'blocked'
        reports.append(dict(input=f'local-input-{input_index}', rows=count, invalid=invalid,
                            blocked=blocked, complete=True))
        total_rows += count
        db.commit()
    gate = asyncio.Semaphore(8)
    async with httpx.AsyncClient(trust_env=False, verify=TLS, follow_redirects=False,
                                 timeout=15) as client:
        async def fetch(index, kind, url, plan=None):
            from . import source_adapters
            nonlocal total_rows
            plan = plan or SourcePlan(source_key(url), url, kind, custom=True)
            if kind in CATALOG_ADAPTER_KINDS and not plan.profile:
                # The string named a catalog format but carried no profile.
                # ``source_adapters`` ships a documented ``generic-v1`` profile
                # for each of the four, so the format is readable and the only
                # thing missing was a name; refusing here made
                # ``source add --source-format json-records`` produce a source
                # the collector then declined to fetch.  A format the adapters
                # do not know still ends up here with no profile, because
                # ``source_spec`` refuses unknown kinds earlier.
                plan.profile = {'kind': kind, 'profile': 'generic-v1', 'config': {}}
            if record_provenance:
                # The identity a previous flat run wrote under the spec hash
                # belongs to this catalog source, and the move is made before
                # the network so it is visible even when the fetch fails.
                adopt_legacy_membership(db, plan)
            key = plan.source_id
            count = invalid = blocked = pages = attempts = 0
            candidate_count = 0
            endpoint_count = 0
            budget = {'used': 0}
            error = None
            page = 1
            expected_total = None
            signatures = set()
            started_at = clock()
            state = _source_state_row(db, key) if record_provenance else None
            seen = {'values': set(), 'new': 0, 'duplicate': 0}
            received = recognized = status_code = 0
            reject_reasons = {}
            body_digest = None
            final_url = url
            not_modified = False
            fallback_used = False
            retry_after = None
            parse_state = 'pending'
            http_state = 'not_run'
            response_etag = None
            response_modified = None
            if record_provenance:
                record_source_identity(db, plan)
            # A quarantined source is not asked at all: three failures in a row
            # means the provider is gone, and asking again every collection
            # spends the budget of the sources that are alive.
            if state and (state.get('quarantine_until') or 0) > clock():
                reports.append(dict(source=index, source_id=key, rows=0, invalid=0, blocked=0,
                                    pages=0, attempts=0, complete=False, format=kind,
                                    error='SOURCE_QUARANTINED',
                                    quarantine_until=state.get('quarantine_until'),
                                    next_attempt_at=state.get('quarantine_until')))
                db.commit()
                publish()
                return

            def consume_candidate():
                nonlocal candidate_count
                if candidate_count >= max_source_candidates:
                    raise SourceFetchError('SOURCE_CANDIDATE_LIMIT')
                candidate_count += 1

            def consume_line(raw):
                nonlocal count, invalid, blocked, recognized
                if kind == 'text':
                    # Web pages may use any encoding and are mostly markup: keep only addresses.
                    for address in loose_addresses(raw.decode('utf-8', errors='replace')):
                        consume_candidate()
                        count += 1
                        recognized += 1
                        outcome = add(address, source=key, seen=seen)
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
                recognized += 1
                if kind == 'http-fields':
                    match = re.fullmatch(r"(\d{1,3}(?:\.\d{1,3}){3}:\d{1,5}):[A-Za-z][A-Za-z .'-]*", line.strip())
                    outcome = add(match[1], source=key, seen=seen) if match else 'invalid'
                elif kind == 'auto':
                    outcome = add_detected(line, source=key, seen=seen)
                elif kind in SCHEMES:
                    outcome = add(line, kind, source=key, seen=seen)
                elif kind == 'line':
                    # The catalog's `line` adapter means a plain list of
                    # addresses, and its own record says how to read one: the
                    # protocol the list publishes, whether the address is only
                    # the first token of a line that also carries a note, and
                    # whether the list is unlabelled.  Reading the record
                    # instead of assuming HTTP is what makes a SOCKS5 list
                    # arrive as SOCKS5 rather than as 5000 unusable HTTP rows.
                    config = (plan.profile or {}).get('config') or {}
                    if config.get('line_address') == 'first-token':
                        # "address<tab>free-form note" is the one shape a line
                        # list uses; only the leading token is the address.
                        line = line.split(None, 1)[0]
                    if config.get('legacy_kind') == 'auto' or config.get('default_protocol') == 'auto':
                        outcome = add_detected(line, source=key, seen=seen)
                    else:
                        outcome = add(line, config.get('default_protocol') or 'http',
                                      source=key, seen=seen)
                else:
                    outcome = add(line, kind, source=key, seen=seen)
                invalid += outcome == 'invalid'
                blocked += outcome == 'blocked'

            def consume_record(record):
                nonlocal count, invalid, blocked, recognized, endpoint_count
                protocols = []
                if isinstance(record, dict) and isinstance(record.get('protocols', []), list):
                    protocols = [protocol for protocol in record['protocols']
                                 if protocol in ('http', 'https', 'socks4', 'socks5')]
                if endpoint_count + len(protocols) > max_source_candidates:
                    raise SourceFetchError('SOURCE_CANDIDATE_LIMIT')
                consume_candidate()
                count += 1
                recognized += 1
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
                                      record.get('country'), key, seen=seen)
                        accepted = accepted or outcome == 'accepted'
                        blocked_here = blocked_here or outcome == 'blocked'
                if not accepted:
                    blocked += blocked_here
                    invalid += not blocked_here

            def consume_adapter_records(result):
                """Feed ``source_adapters`` records into the one membership writer.

                The adapter decides what an address is (and what the publisher
                *claims* about it); normalization, the denylist and the
                collection stay here, so an adapter can never write straight
                into the database or add a private address.
                """
                nonlocal count, invalid, blocked, recognized
                for record in result.get('records') or ():
                    if isinstance(record, str):
                        candidates, country = [record], None
                    elif isinstance(record, dict):
                        # `source_adapters` returns a normalized record: `values`
                        # are the addresses it read and `value` is the same
                        # address without its protocol.  Only `values` are
                        # stored.  Adding `value` as well would invent an HTTP
                        # proxy in front of every SOCKS record, so a SOCKS-only
                        # list would deliver twice its size and half of it would
                        # not exist.  `declared` is what the *publisher* claimed;
                        # a claim is stored as a claim
                        # (`country_source='source'`), never as a measurement.
                        candidates = [item for item in (record.get('values') or ()) if item]
                        if not candidates and record.get('value'):
                            candidates = [record['value']]
                        declared = record.get('declared') if isinstance(record.get('declared'), dict) else {}
                        country = declared.get('country') or record.get('country')
                    else:
                        continue
                    if not candidates:
                        continue
                    consume_candidate()
                    count += 1
                    recognized += 1
                    outcomes = []
                    for candidate in candidates:
                        scheme = str(candidate).split('://')[0] if '://' in str(candidate) else None
                        outcomes.append(add(candidate, scheme if scheme in SCHEMES else 'http',
                                            country, key, seen=seen))
                    outcome = next((o for o in ('accepted', 'blocked') if o in outcomes), 'invalid')
                    invalid += outcome == 'invalid'
                    blocked += outcome == 'blocked'
                # Records the adapter read and refused on sight -- a URL with
                # credentials, a row without a port -- are rejections too.  A
                # report that counted only what the barrier refused would say a
                # list of unusable rows was empty.
                for name, number in (result.get('rejects') or {}).items():
                    if isinstance(number, int) and number > 0:
                        reject_reasons[name] = reject_reasons.get(name, 0) + number
                        invalid += number

            async def request_source(request_url, expected_page=None, headers=None):
                current_url = request_url
                for redirect_count in range(max_source_redirects + 1):
                    async with _source_stream(client, current_url, allow_private_sources,
                                              headers=headers) as response:
                        status = response.status_code
                        # The validators the *response* carries are what make the
                        # next conditional request possible; a digest of the body
                        # is a different thing and would never match.
                        nonlocal response_etag, response_modified, status_code
                        response_etag = response.headers.get('etag')
                        response_modified = response.headers.get('last-modified')
                        if status == 304:
                            # A 304 is a validation of the transport, not an
                            # answer: nothing is parsed and, further down, no
                            # new generation is written.
                            nonlocal not_modified, final_url
                            not_modified = True
                            final_url = current_url
                            return None
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
                        if kind in CATALOG_ADAPTER_KINDS:
                            # The catalog formats are documents, not streams: the
                            # adapter needs the whole page, and its own byte
                            # budget is applied by `_read_bounded_body`.
                            nonlocal parse_state
                            body = await _read_bounded_body(response, budget, max_source_bytes, keep_prefix=bounded_prefix)
                            if not body.strip():
                                # A 200 with no bytes is its own outcome, not a
                                # broken format (area-sources §2.2).
                                parse_state = 'empty'
                                return None
                            return body
                        if kind == 'geonode':
                            body = await _read_bounded_body(response, budget, max_source_bytes, keep_prefix=bounded_prefix)
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
                                                                 keep_prefix=bounded_prefix))
                            return None
                        await _read_bounded_lines(response, budget, max_source_bytes, line_limit, consume_line,
                                                   keep_prefix=bounded_prefix)
                        return None
                raise SourceFetchError('SOURCE_REDIRECT_TOO_MANY')

            async with gate:
                page_url = url
                while True:
                    data = None
                    succeeded = False
                    # The stored validators describe page one only: sending
                    # them on page two would ask a provider whether *that* URL
                    # changed, which it never said anything about.
                    headers = _source_headers(plan, state) if (record_provenance and page == 1) else {}
                    if kind == 'geonode':
                        parsed = urlsplit(page_url)
                        query = dict(parse_qsl(parsed.query))
                        query.update(page=str(page))
                        query.setdefault('limit', '500')
                        page_url = urlunsplit(parsed._replace(query=urlencode(query)))
                    # A mirror of the same list is the same source: it is tried
                    # once, after the primary has failed its retry, and whatever
                    # it delivers is credited to the same id -- two URLs, one
                    # source, and the report says which one answered.
                    candidates = [page_url]
                    if page == 1 and plan.fallbacks:
                        candidates.append(plan.fallbacks[0])
                    for candidate in candidates:
                        if candidate is not candidates[0]:
                            fallback_used = True
                            final_url = candidate
                        for retry in range(2):
                            attempts += 1
                            try:
                                async with asyncio.timeout(timeout):
                                    data = await request_source(candidate, page, headers)
                                page_url = candidate
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
                                    # Injected so a test can exercise a retry
                                    # without spending a wall-clock second.
                                    await (sleep(1) if sleep is not None else asyncio.sleep(1))
                        if succeeded:
                            break
                    if not succeeded:
                        break
                    pages += 1
                    if not_modified:
                        # 304: nothing was parsed and nothing changed.  The run
                        # ends here so a "not modified" answer can never be
                        # mistaken for a page that delivered nothing.
                        http_state = 'not_modified'
                        break
                    if kind in CATALOG_ADAPTER_KINDS:
                        if data is None:
                            # A 200 with no bytes: its own outcome, already
                            # recorded as `empty` by `request_source`.
                            break
                        received = budget.get('used', 0)
                        status_code = 200
                        body_digest = hashlib.sha256(data).hexdigest()
                        limits = {'max_bytes': max_source_bytes,
                                  'max_records': max_source_candidates}
                        if budget.get('truncated'):
                            # The check stopped at its own bound.  The prefix of
                            # a JSON document is not a valid document, so
                            # whatever the parser makes of it, the report is
                            # the check's truncation and never a verdict on the
                            # source's format.
                            try:
                                consume_adapter_records(source_adapters.parse_page(
                                    data, plan.profile or {'kind': kind},
                                    page_context={'page': page}, limits=limits))
                            except source_adapters.AdapterError:
                                pass
                            error = 'SOURCE_TRUNCATED'
                            parse_state = 'partial'
                            break
                        try:
                            result = source_adapters.parse_page(
                                data, plan.profile or {'kind': kind},
                                page_context={'page': page}, limits=limits)
                        except source_adapters.AdapterError as exc:
                            error = exc.args[0] if exc.args else 'SOURCE_ADAPTER_ERROR'
                            # A document that is refused for exceeding its
                            # budget is not a broken document, and saying so
                            # is the difference between "raise the limit" and
                            # "the source is malformed".
                            parse_state = ('budget_exceeded'
                                           if error in ('SOURCE_RECORD_LIMIT', 'SOURCE_TOO_LARGE')
                                           else 'invalid')
                            break
                        except Exception:  # noqa: BLE001 - one bad source must not cancel the others
                            # An adapter that raises something it never declared
                            # is a broken adapter, not a broken source.  Letting
                            # it out would tear down the whole run and lose
                            # every other source's answer with it.
                            error, parse_state = 'SOURCE_ADAPTER_ERROR', 'invalid'
                            break
                        parse_state = str(result.get('state') or 'complete')
                        consume_adapter_records(result)
                        if result.get('truncated'):
                            error = result.get('reason') or 'SOURCE_RECORD_LIMIT'
                            break
                        # The adapter's own pagination.  ``page_info`` and
                        # ``next_page_url`` were in the tree from the start and
                        # nothing called them, so a paginated catalog source
                        # was read as its first page and reported as complete:
                        # a source that publishes 162 records over two pages
                        # contributed 100 and looked finished.
                        try:
                            info = source_adapters.page_info(
                                json.loads(data.decode('utf-8')), plan.profile or {'kind': kind})
                        except (source_adapters.AdapterError, UnicodeDecodeError, ValueError):
                            info = {}
                        if max_pages is not None and pages >= max_pages:
                            if info.get('total') is not None and count < int(info['total']):
                                error = 'SOURCE_PAGE_LIMIT'
                            break
                        if info.get('total') is not None and count >= int(info['total']):
                            break
                        if info.get('has_more') is False or not info.get('records'):
                            if info.get('total') is not None and count < int(info['total']):
                                error = 'SOURCE_PAGINATION_EMPTY'
                            break
                        try:
                            following = source_adapters.next_page_url(
                                page_url, info, plan.profile or {'kind': kind}, page + 1)
                        except source_adapters.AdapterError as exc:
                            error = exc.args[0] if exc.args else 'SOURCE_PAGINATION_NEXT_INVALID'
                            break
                        if not following:
                            break
                        page_url = following
                        page += 1
                        db.commit()
                        await asyncio.sleep(.1)
                        continue
                    if kind != 'geonode':
                        break
                    if max_pages is not None and pages >= max_pages:
                        # The caller's own page budget, reported as a budget
                        # rather than as a provider that stopped answering.
                        error = 'SOURCE_PAGE_LIMIT'
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
            if bounded_prefix and budget.get('truncated'):
                # A streaming read the check's own bound cut short.  The lines
                # already recognised stay, and the list is reported as partial
                # rather than complete.
                error, parse_state = 'SOURCE_TRUNCATED', 'partial'
            elif not not_modified and error is None and parse_state == 'pending':
                parse_state = 'complete'
            # The three fetch states the source views know
            # (``source_management.FETCH_STATES``) are named here, not invented
            # per report: a 200 with no addresses and a 200 that failed to parse
            # are different statements about a source.
            if not_modified:
                http_state = 'not_modified'
            elif error is None:
                http_state = 'empty_body' if parse_state == 'empty' else 'http_2xx_nonempty'
            elif parse_state == 'budget_exceeded' or status_code == 429 or error in (
                    'SOURCE_TRUNCATED', 'SOURCE_PAGE_LIMIT', 'SOURCE_RECORD_LIMIT'):
                # The transport worked; the budget did not fit.  Reporting it as
                # `http_error` would send the user to look at a provider that
                # answered perfectly.
                http_state = 'http_429' if status_code == 429 else 'http_2xx_nonempty'
            else:
                http_state = 'http_error' if status_code else 'error'
            delivered = not not_modified and error is None and count > 0
            served = stored_generation(db, key, clock) if not_modified else None
            if served is not None:
                # 304 is an answer, not the absence of one.  The source still
                # offers everything its last good generation holds, so the run
                # reports that set, with its age and an explicit marker, instead
                # of counting zero rows and leaving the operator to wonder why a
                # source that was fine yesterday delivered nothing today.  The
                # counters this run measured stay at zero on purpose: nothing
                # was read, and only what was read may be counted.
                count = served['rows']
                total_rows += served['rows']
                parse_state = 'not_modified'
            report = dict(source=index, source_id=key, rows=count, invalid=invalid, blocked=blocked,
                          pages=pages, attempts=attempts, complete=error is None, error=error,
                          format=kind, http_state=http_state, parse_state=parse_state,
                          cache_state='none', new=seen['new'], duplicate=seen['duplicate'],
                          accepted=len(seen['values']), recognized=recognized,
                          rejected=invalid, reject_reasons=reject_reasons,
                          bytes=budget.get('used', 0),
                          partial=error in ('SOURCE_TRUNCATED', 'SOURCE_PAGE_LIMIT',
                                            'SOURCE_RECORD_LIMIT'),
                          truncated=bool(budget.get('truncated')),
                          fallback_used=fallback_used,
                          endpoint_url=final_url or url)
            if served is not None:
                report['served_from_cache'] = True
                report['cache_age_seconds'] = served['age_seconds']
                report['cache_generation'] = served['generation_id']
                report['complete'] = True
            if record_provenance:
                # A fetch that failed while a previous answer is still on disk
                # serves that answer rather than reporting an empty source.
                # "Stale" is the whole point: the data is real, its age is
                # stated, and the failure is not hidden behind it.
                served_after_failure = served is not None or (
                    not delivered and stored_generation(db, key, clock) is not None)
                observation = _write_source_provenance(
                    db, run_id, plan, url, started_at=started_at, clock=clock,
                    http_state=http_state, parse_state=parse_state, outcome=(
                        'not_modified' if not_modified else
                        'empty' if parse_state == 'empty' else
                        'partial' if report.get('error') == 'SOURCE_RECORD_LIMIT' else
                        'delivered' if delivered else 'failed'),
                    status=status_code or None, attempts=attempts, pages=pages,
                    nbytes=budget.get('used', 0), received=received, recognized=recognized,
                    accepted=len(seen['values']), rejected=invalid, duplicate=seen['duplicate'],
                    blocked=blocked, new_endpoints=seen['new'],
                    partial=error == 'SOURCE_RECORD_LIMIT', error=error,
                    body_sha256=body_digest, delivered=delivered, values=seen['values'],
                    etag=response_etag, last_modified=response_modified,
                    previous=state, final_url=final_url, not_modified=not_modified,
                    stale=served_after_failure)
                report.update(observation)
            # Last, because the observation above is what names the outcome the
            # source views filter on and it must agree with the error.
            report['outcome'] = source_outcome(report)
            reports.append(report)
            db.commit()
            publish()
            print(tr(f'Источник {index}: строк {count}, заблокировано {blocked}, страниц {pages}, ошибка {error or "нет"}',
                     f'Source {index}: rows {count}, blocked {blocked}, pages {pages}, error {error or "none"}'), flush=True)
        async with asyncio.TaskGroup() as group:
            for index, (kind, url, plan) in enumerate(specs, 1):
                group.create_task(fetch(index, kind, url, plan))
    db.commit()
    publish()
    return dict(raw_rows=total_rows, unique=db.execute('SELECT count(*) FROM candidates').fetchone()[0],
                blocked=sum(r.get('blocked', 0) for r in reports), denylist_error=denylist.error,
                sources_total=len(specs), sources=reports)


#: How a fetch error becomes the one-word outcome a source view can filter on.
SOURCE_OUTCOMES = {
    'SOURCE_TOO_LARGE': 'limit_exceeded', 'SOURCE_LINE_TOO_LARGE': 'limit_exceeded',
    'SOURCE_RECORD_LIMIT': 'limit_exceeded', 'SOURCE_CANDIDATE_LIMIT': 'limit_exceeded',
    'SOURCE_TRUNCATED': 'partial', 'SOURCE_PAGE_LIMIT': 'partial',
    'SOURCE_QUARANTINED': 'unavailable', 'SOURCE_RATE_LIMITED': 'rate_limited',
    'SOURCE_TIMEOUT': 'timeout', 'SOURCE_DEADLINE': 'partial',
    'SOURCE_HTML_PLACEHOLDER': 'html_placeholder', 'SOURCE_INVALID_JSON': 'invalid_json',
}


def source_outcome(entry):
    """The outcome word for one report row, in one place.

    The catalog screen, the CLI and ``source_management.preview_view`` all have
    to say the same thing about the same fetch, so the mapping lives beside the
    collector that produces the errors and not in each reader.
    """
    if not isinstance(entry, dict):
        return 'unavailable'
    if entry.get('served_from_cache'):
        return 'not_modified'
    error = entry.get('error')
    if not error:
        return 'empty' if entry.get('parse_state') == 'empty' else 'available'
    return SOURCE_OUTCOMES.get(error, 'unavailable')


def run_preview(db, values, **kwargs):
    """Check a list of sources (strings, plans or catalog records) and report.

    The synchronous entry point the GUI uses: :func:`preview_collect` wrapped so
    a request handler does not have to own an event loop.  ``db`` is only used
    for its schema -- the check itself runs against a private temporary
    database, so nothing it finds can reach the user's collection.
    """
    return asyncio.run(preview_collect(db, values, **kwargs))


async def preview_collect(db, urls, timeout=60, denylist=None, allow_private_sources=False,
                          *, sample_limit=50, max_source_bytes=PREVIEW_MAX_BYTES,
                          max_source_candidates=PREVIEW_MAX_CANDIDATES, **kwargs):
    """Check sources without writing anything a real collection would keep.

    The same fetch, the same adapters and the same limits run against a private
    temporary database, so a check can answer for every list the collector can
    read and cannot leave candidates, provenance, observations or last-good
    behind.  Past the byte budget the check keeps the prefix it managed to read
    and says so; a real collection still refuses the whole body.
    """
    return await collect(db, urls, [], timeout, None, denylist, allow_private_sources,
                         preview=True, bounded_prefix=True, sample_limit=sample_limit,
                         max_source_bytes=max_source_bytes,
                         max_source_candidates=max_source_candidates, **kwargs)


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


def _tunnel_host_port(url):
    parsed = urlsplit(url)
    if not parsed.hostname:
        raise ValueError('URL без хоста')
    return parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80)


def _probe_stage_of(exc, stage='target'):
    """The stage and stable code of a transport failure, from the one classifier."""
    return diagnostics.classification(exc)


def _ws_frame(payload, opcode=0x1):
    """One masked client frame (RFC 6455 §5.3)."""
    import os as _os
    mask = _os.urandom(4)
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    length = len(payload)
    if length < 126:
        head = bytes((0x80 | opcode, 0x80 | length))
    elif length < 65536:
        head = bytes((0x80 | opcode, 0x80 | 126)) + length.to_bytes(2, 'big')
    else:
        head = bytes((0x80 | opcode, 0x80 | 127)) + length.to_bytes(8, 'big')
    return head + mask + masked


def _ws_frames(buffer):
    """Decode as many complete frames as the buffer holds; the rest is returned."""
    out = []
    while True:
        if len(buffer) < 2:
            break
        first, second = buffer[0], buffer[1]
        opcode = first & 0x0F
        length = second & 0x7F
        offset = 2
        if length == 126:
            if len(buffer) < offset + 2:
                break
            length = int.from_bytes(buffer[offset:offset + 2], 'big')
            offset += 2
        elif length == 127:
            if len(buffer) < offset + 8:
                break
            length = int.from_bytes(buffer[offset:offset + 8], 'big')
            offset += 8
        if second & 0x80:
            if len(buffer) < offset + 4:
                break
            offset += 4
        if len(buffer) < offset + length:
            break
        out.append((opcode, buffer[offset:offset + length]))
        buffer = buffer[offset + length:]
    return out, buffer


class Transport:
    """The network seam ``probes.py`` measures through (F20, F01).

    ``probes`` never opens a socket: it builds a request, hands it to one of
    these four methods and interprets what comes back.  Everything the probe
    ladder can measure has an entry point here, so an endpoint that cannot
    carry a transfer is never recorded as if it did:

    * :meth:`send` -- one request, bounded by ``ProbeRequest.max_bytes``;
    * :meth:`download` -- a chunk-level :class:`probes.TransferTrace`, which is
      what the throughput window is computed from;
    * :meth:`websocket` -- a real RFC 6455 upgrade plus ping/pong;
    * :meth:`hold` -- a connection that must stay open.

    ``send`` and ``download`` go through ``httpx`` (the same client, the same
    SOCKS4 transport and the same TLS context as the rest of the product);
    ``websocket`` and ``hold`` need a raw socket, so the CONNECT is issued by
    hand -- ``asyncio.open_connection`` has no proxy argument.
    """

    def __init__(self, proxy=None, config=None):
        self.proxy = proxy
        self.config = config or {}
        self.calls = 0
        self._keepalive = None

    def _client(self, timeout, connect=None):
        options = dict(trust_env=False, verify=TLS, follow_redirects=False,
                       timeout=httpx.Timeout(timeout, connect=connect or timeout))
        if self.proxy and self.proxy.startswith('socks4://'):
            return httpx.AsyncClient(transport=socks4.transport(self.proxy, verify=TLS), **options)
        if self.proxy:
            options['proxy'] = self.proxy
        return httpx.AsyncClient(**options)

    # -- 1. a single bounded request -------------------------------------

    async def send(self, request, *, options):
        from . import probes
        self.calls += 1
        started = time.perf_counter()
        try:
            async with self._client(options.read_timeout_s, options.connect_timeout_s) as client:
                async with client.stream(request.method, request.url,
                                         headers=request.header_map(),
                                         content=request.body) as response:
                    first = time.perf_counter()
                    body = bytearray()
                    # A bounded chunk size is what makes the byte budget real: a
                    # transport that only checked after a 64 KiB chunk would
                    # overshoot the ceiling by that much.
                    size = max(1, min(8192, request.max_bytes)) if request.max_bytes else 8192
                    async for chunk in response.aiter_bytes(chunk_size=size):
                        body.extend(chunk)
                        if len(body) >= request.max_bytes:
                            break
                    finished = time.perf_counter()
                    return probes.ProbeResponse(
                        status=response.status_code, headers=tuple(response.headers.items()),
                        body=bytes(body), url=str(response.url),
                        connect_ms=round((first - started) * 1000, 2),
                        handshake_ms=round((first - started) * 1000, 2),
                        ttfb_ms=round((first - started) * 1000, 2),
                        transfer_ms=round((finished - first) * 1000, 2),
                        total_ms=round((finished - started) * 1000, 2))
        except Exception as exc:  # a broken proxy raises more than httpx errors
            stage, code = _probe_stage_of(exc)
            return probes.ProbeResponse(code=code, stage=request.stage if stage is None else stage)

    async def send_stage(self, request, *, options):
        """``run_stage`` uses the same seam; the proxy address is the target."""
        return await self.send(request, options=options)

    # -- 2. a bounded download -------------------------------------------

    async def download(self, target, *, options, reuse=False):
        from . import probes
        trace = probes.TransferTrace(url=target.url,
                                     connection='reused' if reuse else 'cold')
        started = time.perf_counter()
        trace.begin(started)
        try:
            async with self._client(options.read_timeout_s, options.connect_timeout_s) as client:
                headers = merge_headers(self.config.get('request_profile', DEFAULT_REQUEST_PROFILE),
                                        dict(target.headers))
                async with client.stream('GET', target.url, headers=headers) as response:
                    trace.status = response.status_code
                    if response.status_code != 200:
                        return trace.fail(f'HTTP_{response.status_code}', time.perf_counter())
                    async for chunk in response.aiter_bytes():
                        trace.add(len(chunk), time.perf_counter())
                        if trace.bytes >= target.max_bytes:
                            break
                    trace.connect_ms = round((time.perf_counter() - started) * 1000, 2)
                    return trace.finish(time.perf_counter())
        except Exception as exc:
            return trace.fail(diagnostics.classification(exc)[1], time.perf_counter())

    # -- raw socket behind the proxy --------------------------------------

    async def _open(self, url, timeout):
        host, port = _tunnel_host_port(url)
        if not self.proxy:
            return await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        parsed = urlsplit(self.proxy)
        phost, pport = parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80)
        reader, writer = await asyncio.wait_for(asyncio.open_connection(phost, pport), timeout)
        authority = f'{host}:{port}'
        lines = [f'CONNECT {authority} HTTP/1.1', f'Host: {authority}']
        for name, value in merge_headers(self.config.get('request_profile',
                                                          DEFAULT_REQUEST_PROFILE)).items():
            lines.append(f'{name}: {value}')
        writer.write(('\r\n'.join(lines) + '\r\n\r\n').encode('latin-1'))
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), timeout)
        fields = head.split(b' ')
        status = int(fields[1]) if len(fields) > 1 and fields[1].isdigit() else 0
        if status != 200:
            with contextlib.suppress(Exception):
                writer.close()
            raise OSError(f'CONNECT {authority} отвечает {status}')
        if url.startswith('https://'):
            # `StreamWriter.start_tls` upgrades the same connection in place,
            # so `reader` stays bound to it and the tunnel is not reopened.
            await writer.start_tls(TLS, server_hostname=host)
        return reader, writer

    # -- 3. websocket ------------------------------------------------------

    async def websocket(self, request, *, options, spec):
        from . import probes
        trace = probes.WsTrace(connection=request.connection)
        started = time.perf_counter()
        writer = None
        budget = spec.budget
        try:
            reader, writer = await self._open(request.url, float(budget['handshake_timeout_s']))
            parsed = urlsplit(request.url)
            host = parsed.netloc
            path = parsed.path or '/'
            if parsed.query:
                path += '?' + parsed.query
            import base64
            import os as _os
            key = base64.b64encode(_os.urandom(16)).decode('ascii')
            lines = [f'GET {path} HTTP/1.1', f'Host: {host}', 'Upgrade: websocket',
                     'Connection: Upgrade', f'Sec-WebSocket-Key: {key}',
                     'Sec-WebSocket-Version: 13']
            for name, value in request.header_map().items():
                lines.append(f'{name}: {value}')
            writer.write(('\r\n'.join(lines) + '\r\n\r\n').encode('latin-1'))
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'),
                                          float(budget['handshake_timeout_s']))
            fields = head.split(b' ')
            trace.status = int(fields[1]) if len(fields) > 1 and fields[1].isdigit() else 0
            trace.handshake_ms = round((time.perf_counter() - started) * 1000, 2)
            headers = {}
            for line in head.decode('latin-1').split('\r\n')[1:]:
                if ':' in line:
                    name, _, value = line.partition(':')
                    headers[name.strip().lower()] = value.strip()
            trace.upgrade = headers.get('upgrade')
            trace.subprotocol = headers.get('sec-websocket-protocol')
            trace.extensions = headers.get('sec-websocket-extensions')
            if trace.status != 101:
                return trace
            wanted = int(budget.get('max_pings') or 0)
            buffer = b''
            ping_started = time.perf_counter()
            for index in range(max(1, wanted)):
                writer.write(_ws_frame(b'ping%d' % index, opcode=0x9))
                await writer.drain()
                trace.pings_sent += 1
                while True:
                    remaining = float(budget['ping_timeout_s']) - (time.perf_counter() - ping_started)
                    if remaining <= 0:
                        return trace
                    try:
                        chunk = await asyncio.wait_for(reader.read(4096), remaining)
                    except (asyncio.TimeoutError, TimeoutError):
                        # No frame inside the ping budget: the trace is finished
                        # and the summarizer names it `no_pong` or `closed`
                        # from what did arrive, which is the honest verdict.
                        return trace
                    if not chunk:
                        trace.closed_by_peer = True
                        return trace
                    buffer += chunk
                    frames, buffer = _ws_frames(buffer)
                    for opcode, payload in frames:
                        trace.frames += 1
                        trace.bytes_in += len(payload)
                        if opcode == 0x9:  # a ping from the server, answered
                            writer.write(_ws_frame(payload, opcode=0xA))
                            await writer.drain()
                        elif opcode == 0xA:
                            trace.pongs += 1
                            trace.rtt_ms = round((time.perf_counter() - ping_started) * 1000, 2)
                            break
                    if trace.pongs > index:
                        break
            return trace
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            trace.error = type(exc).__name__
            trace.detail = diagnostics.classification(exc)[1]
            return trace
        finally:
            if writer is not None:
                with contextlib.suppress(Exception):
                    writer.close()

    # -- 4. a connection that must stay open -------------------------------

    async def hold(self, request, *, options, spec):
        from . import probes
        trace = probes.HoldTrace(connection=request.connection)
        budget = spec.budget
        want = float(budget.get('hold_s') or 0)
        minimum = float(budget.get('min_sustained_s') or 0)
        writer = None
        try:
            opened = time.perf_counter()
            reader, writer = await self._open(request.url, options.connect_timeout_s)
            parsed = urlsplit(request.url)
            path = parsed.path or '/'
            if parsed.query:
                path += '?' + parsed.query
            lines = [f'GET {path} HTTP/1.1', f'Host: {parsed.netloc}', 'Connection: keep-alive']
            for name, value in request.header_map().items():
                lines.append(f'{name}: {value}')
            writer.write(('\r\n'.join(lines) + '\r\n\r\n').encode('latin-1'))
            await writer.drain()
            trace.opened_at = opened
            buffer = await asyncio.wait_for(reader.read(4096), options.read_timeout_s)
            trace.first_byte_at = time.perf_counter()
            trace.status = 200
            trace.bytes, trace.chunks = len(buffer), 1
            deadline = trace.first_byte_at + want
            while time.perf_counter() < deadline:
                remaining = min(deadline - time.perf_counter(), options.read_timeout_s)
                if remaining <= 0:
                    break
                try:
                    chunk = await asyncio.wait_for(reader.read(4096), remaining)
                except (asyncio.TimeoutError, TimeoutError):
                    # The hold window ended with the connection still open and
                    # nothing more to deliver.  That is the measurement the
                    # caller asked for, not a failure of the connection.
                    break
                if not chunk:
                    trace.closed_by_peer = True
                    break
                trace.bytes += len(chunk)
                trace.chunks += 1
                trace.last_byte_at = time.perf_counter()
            trace.closed_at = time.perf_counter()
            sustained = trace.sustained_s or 0.0
            if not trace.closed_by_peer and sustained >= minimum and trace.chunks > 1:
                trace.error = None
            elif trace.closed_by_peer or sustained < minimum:
                trace.error = 'CONNECTION_CLOSED'
                trace.detail = f'соединение прожило {sustained:.2f} с при минимуме {minimum:.2f} с'
            return trace
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            trace.error = 'CONNECTION_CLOSED'
            trace.detail = diagnostics.classification(exc)[1]
            trace.closed_at = time.perf_counter()
            return trace
        finally:
            if writer is not None:
                with contextlib.suppress(Exception):
                    writer.close()


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
        # The stage and the stable code come from the one classifier, so the
        # funnel in a diagnostic packet can tell "the proxy died at the TCP
        # stage" from "the target answered 503" (F10, F25).
        result['error'] = type(exc).__name__
        result['error_stage'], result['error_code'] = diagnostics.classification(exc)
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


def probe_plan(config):
    """The check mode and its limits, from the modes module (F01, F07).

    One place decides what "recheck" or "monitor" means and how long a single
    probe may take; the engine asks it instead of re-deriving the numbers, so a
    mode behaves the same from the CLI, the GUI and the API.
    """
    from . import probes
    mode = probes.resolve_mode(str(config.get('mode') or 'services'))
    connect = float(config.get('connect_timeout') or 4.0)
    read = float(config.get('timeout') or 8.0)
    attempts = max(1, int(config.get('attempts') or 1))
    targets = max(1, len(config.get('targets') or ()))
    options = probes.validate_options({
        'attempts': attempts,
        'connect_timeout_s': connect,
        # A handshake cannot outlive the connect it grows out of, so it is the
        # same bound -- stated once, measured once.
        'handshake_timeout_s': connect,
        'read_timeout_s': read,
        # The whole-probe budget is what one endpoint may cost in this run:
        # every attempt of every target, worst case each.  It is not
        # ``attempts x targets`` implicit in the per-request timeouts -- it is
        # one number, and it is the job's own (defect 23, F12).
        'whole_probe_timeout_s': (connect + connect + read) * attempts * targets,
        'max_body_bytes': int(config.get('max_bytes') or 262144),
    })
    return mode, options


def capability_manifest():
    """What is really measured, and what is declared unsupported (F20).

    A websocket, a long connection, a media segment, UDP or HTTP/3 is not
    measured here, so it is reported as unsupported rather than as a passing
    probe that only ever did a GET.
    """
    from . import probes
    return probes.probes_manifest()


def plan_outcome_of(proxy, config, mode, samples, *, own_ips=None):
    """The ``probes`` verdict of what ``request_once`` already measured.

    The samples are the network work; the *level of evidence* is a rule of
    :mod:`probes`, so it is asked there instead of being re-derived here.  That
    is what keeps the promise of F01: a row measured in ``tcp``/``handshake``
    can never carry ``transfer_ok`` even when a target answered on a later
    attempt, because ``evidence_for`` reads the mode's own ladder.
    """
    from . import probes
    outcomes = []
    for index, target in enumerate(config['targets']):
        subset = [item for item in samples if item.get('target') == index]
        ok = bool(subset) and all(item['ok'] for item in subset)
        last = subset[-1] if subset else {}
        outcomes.append(probes.TargetOutcome(
            target_id=str(target.get('name') or index), ok=ok,
            code=None if ok else (last.get('error_code') or last.get('error')),
            stage=last.get('error_stage'), status=last.get('status'),
            bytes=sum(item.get('bytes') or 0 for item in subset),
            attempts=len(subset) or 1, url=str(target.get('url') or ''),
            total_ms=last.get('ms'), body_limit=int(config.get('max_bytes') or 0) or None,
            connection='cold'))
    ok = bool(outcomes) and all(item.ok for item in outcomes)
    span = max((item.get('ms') or 0.0 for item in samples), default=0.0)
    finished = time.time()
    return probes.PlanOutcome(
        endpoint=proxy, mode=mode.name, base=mode.base or mode.name,
        evidence='none', ok=ok, targets=tuple(outcomes),
        code=None if ok else next((item.code for item in outcomes if not item.ok), None),
        stage=None if ok else next((item.stage for item in outcomes if not item.ok), None),
        requested_mode=mode.requested or mode.name,
        started_at=finished - span / 1000.0, finished_at=finished)


async def check_proxy(proxy, config, rate, own_ips=None):
    from . import probes
    samples = []
    limit = allowed_failures(config)
    mode, _options = probe_plan(config)
    failures = [0] * len(config['targets'])
    transport = Transport(proxy, config)
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
                _stamp_evidence(row, plan_outcome_of(proxy, config, mode, samples))
                return row
    row = summarize(proxy, samples, config)
    row['mode'] = mode.name
    _stamp_evidence(row, plan_outcome_of(proxy, config, mode, samples))
    # The judge is asked only through proxies that already work for a target.
    if config.get('anonymity') and own_ips and row['successes']:
        row['anonymity'] = await judge_proxy(proxy, config, rate, own_ips)
    # Bandwidth is measured only for proxies that work for every target.
    if config.get('speedtest') and row['min_target_reliability'] > 0:
        row['speed'] = await measure_speed(proxy, config, rate)
    # The capability kinds F20 added are measured only where the module says
    # they can be, and each one files its own state; a transport that cannot do
    # them says so instead of silently reporting the basic ladder.
    if config.get('capabilities'):
        row['capabilities'] = await check_capabilities(proxy, config, transport)
    return row


def _stamp_evidence(row, outcome):
    """Put the ladder's verdict into the row, so the level is not re-derived."""
    from . import probes
    row['evidence'] = probes.evidence_for(outcome)
    row['is_working'] = probes.is_working(outcome)
    row['code'] = outcome.code
    row['stage'] = outcome.stage
    row['probe_targets'] = [item.to_public() for item in outcome.targets]
    return row


async def check_capabilities(proxy, config, transport=None):
    """Run the declared capability kinds and file one state per kind (F20).

    Each kind is measured by :mod:`probes` against the very same
    :class:`Transport` the basic ladder uses, so "websocket supported" means a
    real 101 and a real pong came back through this proxy -- never a promise.
    """
    from . import probes
    transport = transport or Transport(proxy, config)
    settings = {'mode': 'collect_only', 'capabilities': config.get('capabilities')}
    try:
        plan = probes.build_plan(settings)
    except probes.ProbeError as exc:
        return [{'kind': 'unknown', 'id': 'capabilities', 'state': 'error', 'code': exc.code,
                 'detail': exc.detail, 'connection': 'cold', 'metrics': {}}]
    results = []
    for spec in plan.capabilities:
        outcome = await probes.run_capability(spec, plan.options, transport)
        results.append(outcome.to_public())
    return results


async def measure_speed(proxy, config, rate):
    """Download throughput through the proxy in Mbit/s, counted from the first response byte.

    The window, the minimums and the "one chunk is not a measurement" rule are
    :mod:`probes`' -- this function only supplies the bytes.  A transfer that is
    too small, too short or unfinished therefore comes back
    ``state='insufficient'`` **without a number**, instead of the old hand-made
    arithmetic that divided a single chunk by an arbitrary window (defect 15).
    """
    from . import probes
    await rate.wait()
    test = config['speedtest']
    target = probes.validate_speed_target({'url': test['url'], 'max_bytes': int(test.get('max_bytes') or 5_000_000)})
    mode, options = probe_plan(config)
    transport = Transport(proxy, config)
    measurement = await probes.run_speed_test(target, options, transport)
    result = dict(measurement.to_public())
    # The historical keys stay: the row, the profile evaluation and the export
    # all read them.  `ms` is the whole-probe time the old row carried.
    result['mbps'] = measurement.mbps
    result['bytes'] = measurement.bytes
    result['ms'] = measurement.total_ms if measurement.total_ms is not None else measurement.transfer_ms
    result['error'] = None if measurement.state == 'ok' else (measurement.code or measurement.state)
    return result


async def detect_own_ips(config):
    """IPs of this machine as the judge sees them; kept in memory only.

    ``global_only=False`` is deliberate: a self-hosted judge echoes the
    loopback or LAN address it saw, and dropping it left the baseline empty --
    which then made every endpoint look "elite" for want of a comparison, or
    "unknown" because the judge could not be verified.  The judge host's own
    addresses are still subtracted, so its server address never becomes ours.
    """
    url = config['anonymity']['judge_url']
    headers = merge_headers(config.get('request_profile', DEFAULT_REQUEST_PROFILE))
    async with httpx.AsyncClient(trust_env=False, verify=TLS, timeout=config['timeout'],
                                 follow_redirects=False) as client:
        body = await anonymity.fetch_judge(client, url, headers)
    own = anonymity.extract_public_ips(body.decode('utf-8', errors='replace'), global_only=False)
    # Judges such as azenv also print their own server address; never treat it as ours.
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(urlsplit(url).hostname, None)
        own -= {ipaddress.ip_address(info[4][0].split('%')[0]).compressed for info in infos}
    except (OSError, ValueError):
        pass
    if not own:
        raise ValueError('judge URL не показал IP этого устройства')
    return own


async def judge_proxy(proxy, config, rate, own_ips):
    """The anonymity verdict, with the code that explains an ``unknown``.

    ``classify_detail`` and not ``classify``: the two-key form throws away the
    very thing the user needs when the level is ``unknown`` -- whether the
    answer was empty, a CAPTCHA, or an unverified judge -- and it throws away
    the exit address the judge itself labelled.  Both now travel into the row.
    """
    from . import probes
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
        # An anonymity check that failed is unknown, never a pass: the level the
        # user asked for is not granted on a failed request (defect 13).
        return anonymity.result('unknown', error=diagnostics.classification(exc)[1], started=started)
    # ``judge_verified`` says the direct bootstrap really did show one of our
    # addresses.  Without that baseline an echo that leaks nothing proves
    # nothing, and the level stays ``unknown`` rather than ``elite``.
    detail = anonymity.classify_detail(body, own_ips, judge_verified=bool(own_ips))
    return anonymity.result(detail['level'], detail['signals'], started=started,
                            exit_address=detail.get('exit_ip') or anonymity.exit_ip(body),
                            code=detail.get('code'))


def fit_workers(requested):
    """How many full checks this process can really run at once.

    The same descriptor arithmetic the chain derives its own worker count from
    (``open_fd_budget``), so the pre-check sizing and the run agree.
    """
    try:
        return max(1, min(requested, (open_fd_budget(requested) - 32) // FDS_PER_REQUEST))
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


def exit_address_of(row):
    """The exit address a row confirms, or ``''``.

    The judge writes `anonymity.exit_ip`; a top-level `exit_ip` is accepted too,
    because a row imported from elsewhere may carry it there.  A row without a
    confirmed exit confirms nothing, and "no exit IP" is not "exit == proxy IP".
    """
    if not isinstance(row, dict):
        return ''
    anonymity = row.get('anonymity')
    if isinstance(anonymity, dict):
        value = anonymity.get('exit_ip')
        if isinstance(value, str) and value:
            return value
    value = row.get('exit_ip')
    return value if isinstance(value, str) else ''


def measurement_requests(row):
    """How many HTTP requests one measurement spent, for the resource budget.

    A row without samples spent nothing known: a refused verdict and a proxy that
    died before the first request are not charged with a request they never made.
    """
    if not isinstance(row, dict):
        return 0
    try:
        return max(0, int(row.get('requests') or 0))
    except (TypeError, ValueError):
        return 0


def measurement_bytes(row):
    """How many response bytes one measurement read, for the resource budget."""
    if not isinstance(row, dict):
        return 0
    total = 0
    for sample in row.get('samples') or ():
        if isinstance(sample, dict):
            try:
                total += max(0, int(sample.get('bytes') or 0))
            except (TypeError, ValueError):
                continue
    speed = row.get('speed')
    if isinstance(speed, dict):
        try:
            total += max(0, int(speed.get('bytes') or 0))
        except (TypeError, ValueError):
            pass
    return total


#: One page of the candidate source.  The corpus is read page by page instead of
#: with a single ``fetchall``: a half-million addresses is a list of a hundred
#: megabytes the engine would hold for the whole run, and the front half of a
#: scan is supposed to be bounded (F12).  Each page is a fresh statement, so the
#: commit the store path makes every hundred rows cannot invalidate a half-read
#: cursor of the corpus.
CANDIDATE_PAGE = 4096

#: One page of the scope, as an ordered range over the address index with
#: membership as a test — see :func:`candidate_pages` for why it is not a join.
_CANDIDATE_PAGE_SQL = (
    'SELECT e.canonical FROM endpoints e WHERE e.canonical > ? '
    'AND EXISTS (SELECT 1 FROM membership m WHERE m.endpoint_id = e.id AND m.collection_id = ?) '
    'ORDER BY e.canonical LIMIT ?')

#: What one in-flight measurement really holds open: the socket, the TLS session
#: and the connection a redirect chain may still open.
FDS_PER_REQUEST = 3

#: Descriptors the process keeps for everything that is not a probe: the
#: database, the report file, the standard streams, the loop's own fd.
RESERVED_FDS = 128

#: The RAM one in-flight measurement may hold, and the ceiling over all of them.
#: The share follows the body's own cap, so a profile that allows a bigger
#: response reserves a bigger share instead of the ceiling being exceeded in
#: silence.
DEFAULT_RAM_PER_INFLIGHT = 256 * 1024
DEFAULT_MAX_RAM_BYTES = 64 * 1024 * 1024


def _number(value):
    """A finite number, or 0.0 for anything else — for ordering, never for math."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _recency(row):
    """How recent a stored verdict is: its measurement time, then its lifetime.

    Both are part of the order so that two rows stamped alike still have one
    answer, and a row without a time is the oldest rather than a random one.
    """
    return (_number(row.get('checked_at')), _number(row.get('valid_until')))


def newest_measurements(conn, profile):
    """The most recent verdict of every address of one profile.

    ``results`` is keyed by ``(profile_id, profile_revision, access_id,
    access_revision, endpoint_id)``, so one address owns several rows of the same
    profile as soon as the profile revision or the access revision moved.
    Reading them in table order and letting the last row win made the base of the
    next measurement an arbitrary one: the history counters were accumulated on
    top of a verdict that was not the newest, and every field the new verdict did
    not carry was inherited from it.  The newest measurement wins here, so
    ``--recheck`` builds on the verdict the user would see in the list.
    """
    newest = {}
    for proxy, payload in conn.execute('SELECT proxy, payload FROM results WHERE profile=?', (profile,)):
        try:
            row = json.loads(payload)
        except (TypeError, ValueError):
            continue
        if not isinstance(row, dict):
            continue
        known = newest.get(proxy)
        if known is not None and _recency(known) >= _recency(row):
            continue
        newest[proxy] = row
    return newest


def candidate_pages(conn, collection_id, *, page=CANDIDATE_PAGE):
    """The addresses of one collection, in address order, one page at a time.

    Membership is the scope (CONTRACTS §1.2 rule 2) and the order is the address
    order, which is also the order that makes inserting the results cheapest.
    Pagination is by address, not by offset, so a page stays correct however many
    rows the sweep has written since.

    The page is read as an ordered range over ``endpoints`` with membership as a
    membership *test*.  Joining from ``membership`` instead would satisfy the
    collection filter first and then sort every remaining row to apply
    ``ORDER BY`` — once per page, so a half-million addresses would be sorted
    thirty times over.
    """
    last = ''
    while True:
        rows = conn.execute(_CANDIDATE_PAGE_SQL, (last, collection_id, page)).fetchall()
        if not rows:
            return
        last = rows[-1][0]
        for (canonical,) in rows:
            yield canonical


def _raise_open_fds(requested):
    """Ask the kernel for the descriptors ``requested`` probes need and report
    what the process really has."""
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        desired = requested * FDS_PER_REQUEST + RESERVED_FDS
        if soft < desired:
            try:
                resource.setrlimit(resource.RLIMIT_NOFILE,
                                   (min(desired, hard) if hard != resource.RLIM_INFINITY else desired, hard))
                soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
            except (OSError, ValueError):
                pass
        return soft
    except (ImportError, OSError, ValueError):
        return 256


def open_fd_budget(requested):
    """The descriptors this run may spend on probes.

    What the kernel grants, not what the user typed: a worker count nobody can
    open the sockets for is a number the run only discovers at EMFILE.  The
    chain derives its worker count from this (F12, resources not workers).
    """
    return max(FDS_PER_REQUEST * 4, _raise_open_fds(requested) - RESERVED_FDS)


def ip_literal(host):
    """The compressed IP a host really is, or ``None`` for a hostname.

    "N unique IPs" is a number about addresses, so an endpoint named by a
    hostname is not one of them — and the chain's own counters refuse to count
    it, which is what makes the two agree.
    """
    if not host:
        return None
    try:
        return ipaddress.ip_address(host).compressed
    except ValueError:
        return None


def _worked(row):
    """Whether the measurement itself worked, before the run's own policy.

    A proxy that answered is a different fact from a proxy this run is willing
    to publish, and the expensive stage needs the first to decide the second.
    """
    if not isinstance(row, dict) or row.get('error'):
        return False
    return _number(row.get('min_target_reliability')) > 0


#: The target the expensive stage spends on: somebody else's judge service and a
#: body-sized download.  It has its own limits so one slow or rate-limited
#: third party cannot eat the run (pipeline.TargetPolicy).
EXPENSIVE_TARGET = 'judge'


def _already_canonical(value):
    """The normaliser the chain uses for a candidate from the collection.

    ``collect`` already ran the project's one normaliser, and the collection
    stores what it returned; normalising a second time would be a second
    grammar for the same address (CONTRACTS §1.2).  The chain still refuses
    anything it cannot split into a scheme, a host and a port, so a corrupt
    candidate is rejected and counted rather than measured.
    """
    return value or None


def exit_address_possible(config, probe, expensive_probe):
    """Whether this run can produce a confirmed exit address at all.

    Only the judge reports one.  Without it the unit "N confirmed exit IPs" is
    not a small target, it is an impossible one, and a run that sweeps the whole
    corpus to discover that is a run that spent everything to say nothing.  A
    caller that injects its own probe may put the address there itself, so only
    the product's own probe is asked.
    """
    return bool(config.get('anonymity')) or expensive_probe is not None or probe is not check_proxy



async def scan(db, config, *, workers=128, rate=100, recheck=False, probe=check_proxy, progress=True, on_progress=None, min_success=2/3, screen=None, denylist=None, min_anonymity='any', protocol='all', max_latency=None,
               countries=(), country_exclude=(), country_basis='endpoint', country_unknown='exclude',
               country_of=None, want=0, recheck_passing=False, prefilter=0, prefilter_timeout=3,
               exclude_hosting=False, provider_of=None, run_state=None,
               collection_id=None, profile_revision=1, max_age_seconds=None,
               access=None, job_id=None, job_store=None, deadline_s=None,
               max_requests=None, max_bytes=None, count_what='endpoint',
               max_per_host=1, min_host_interval_s=0.0, target_inflight=2, expensive_probe=None):
    """Check every pending candidate of the profile.

    The run *is* the chain of ``pipeline.py`` — the same ``Pipeline``,
    ``Budgets``, ``ResourceGate``, ``AdaptiveConcurrency``, ``HostLimiter``,
    ``TargetPolicy``, ``Ledger`` and ``FindPolicy`` the module documents, driven
    by ``run``'s own sources and runners.  Nothing here re-implements a rule that
    already has one owner: the worker count is derived from the descriptor and
    RAM budgets rather than taken from ``--workers`` as a constant, the request
    and byte totals are reserved before a measurement and charged with what it
    really spent, per-host and per-target limits hold, and the three units of
    ``--want`` are three different numbers (F12).

    The stages, from cheap to expensive:

    * ``cheap`` — a TCP connect, when ``prefilter`` is on.  Most public
      addresses are dead, and dropping them there is far faster than a full
      request with its own connect timeout.  ``prefilter`` says *whether* the
      stage runs; how many connects it makes at once is a consequence of the
      budgets like everything else.
    * ``basic`` — the reputation screen and then one request through every
      target of the profile.  This is ``probe``, unchanged, so a caller that
      injects its own probe still gets exactly the measurements it injected.
    * ``expensive`` — ``expensive_probe``, when the caller supplies one: the
      judge and the bandwidth download, the two things only a proxy that already
      works may cost.  They are a separate stage so that somebody else's service
      and a body-sized download are bounded by the per-target limits and by the
      run's byte budget instead of sitting outside them.

    ``job_store`` hands the run to the persistent lifecycle of ``jobs.py``: the
    queue, the per-item states and the events come from there, so a cancel, a
    crash or a restart keeps the last completed result and the queue of
    unfinished items (defect 6).  ``deadline_s``, ``max_requests`` and
    ``max_bytes`` are the resource budgets of ``pipeline.py`` -- one global
    time and one request/byte ceiling instead of ``attempts x targets`` per
    address (F12, defect 23).
    """
    denylist = denylist or Denylist.empty()
    policy = config.get('reputation', {})
    strict = bool(policy.get('strict', False))
    active_denylist = denylist if policy.get('local_enabled', True) else None
    if not config.get('anonymity'):
        min_anonymity = 'any'
    encoded = json.dumps(config, sort_keys=True)
    profile = hashlib.sha256(encoded.encode()).hexdigest()[:20]
    db.execute('INSERT OR IGNORE INTO profiles(id, config, digest, created_at) VALUES (?, ?, ?, ?)',
               (profile, encoded, profile, time.time()))
    countries = frozenset(countries or ())
    country_of = country_of or (lambda proxy: None)
    # The scope is fixed here and pinned into every row: the same collection, the
    # same profile revision and the same access revision must be visible to the
    # GUI, the API and the gateway, or a consumer could mix two measurements
    # (CONTRACTS §1.2 rules 1-2).
    collection_id = ensure_collection(db, collection_id)
    access = as_access(access)
    network_id = snapshot_network(config)
    # The same criterion the export uses, so a row the check narrows and a row
    # the export drops are the same row for the same reason (F08).
    criterion = country_criterion(countries, country_exclude, country_basis, country_unknown)
    admission_policy = core.Policy(
        max_age_seconds=float(max_age_seconds if max_age_seconds else MIN_FRESHNESS_SECONDS),
        min_success=policy_min_success(min_success), min_anonymity=min_anonymity, strict=strict,
        countries=countries, denied=frozenset(active_denylist.proxies) if active_denylist else frozenset(),
        max_latency_ms=max_latency,
        country_criterion=criterion, exit_country_of=country_of,
        deny_match=active_denylist.match if active_denylist is not None else None,
        protocol_of=proxy_protocol, country_of=country_of,
        hosting_of=(lambda proxy: is_hosting(proxy, provider_of)) if exclude_hosting else None,
        exclude_hosting=bool(exclude_hosting))
    engine = core.AdmissionEngine(admission_policy)

    def selected(proxy):
        # Protocol and country narrow which candidates are checked; they are not
        # part of the profile, so a later wider run reuses these results.
        # The country half goes through the same criterion the admission
        # contract uses, so `basis=exit` and an exclude set are honoured here
        # too instead of only at export time.
        if protocol not in (None, 'all') and proxy_protocol(proxy) != protocol:
            return False
        if exclude_hosting and is_hosting(proxy, provider_of):
            return False
        if getattr(criterion, 'active', False):
            return core._country_criterion_reason({'proxy': proxy}, admission_policy) is None
        return not countries or country_of(proxy) in countries

    def counts_as_passed(row):
        return (result_allowed(row, min_success, denylist=active_denylist, strict=strict, min_anonymity=min_anonymity)
                and matches_selection(row, protocol, max_latency))

    from . import pipeline as chain

    # ``--want`` counts endpoints unless the user asks for another unit: N
    # endpoints, N IPs and N confirmed exit IPs are different numbers, and the
    # caller names the one it means (F12, CONTRACTS §1.1).  The chain owns the
    # arithmetic of the three units; what this function does is fill the other
    # two so the report can show them beside the one that was asked for.
    find = chain.FindPolicy(n=want, what=count_what)
    unique_ips: set = set()
    unique_exits: set = set()
    passed = 0
    status_counts = {'clean': 0, 'listed': 0, 'unknown': 0, 'local_denied': 0}

    def counted(row):
        """What the store has already confirmed of ``--want``'s unit.

        Only used to *seed* the chain with what the database already holds; the
        chain counts everything this run measures.  The two never overlap: a row
        that is already confirmed is not measured again, and a row this run
        measures is not in the store yet.

        ``ip`` counts an address that really is one.  A hostname endpoint has an
        address and no IP, so reporting it as an IP — here or in the counter the
        chain keeps — would make "N unique IPs" a number nothing can satisfy.
        """
        host = str(row.get('proxy') or '').partition('://')[2].rpartition(':')[0]
        host = ip_literal(host.strip('[]').lower())
        fresh_ip = bool(host) and host not in unique_ips
        if host:
            unique_ips.add(host)
        exit_ip = exit_address_of(row)
        fresh_exit = bool(exit_ip) and exit_ip not in unique_exits
        if exit_ip:
            unique_exits.add(exit_ip)
        if find.what == 'endpoint':
            return 1
        return int(fresh_ip) if find.what == 'ip' else int(fresh_exit)

    # A re-check keeps each proxy's history and its last usable payload until a
    # replacement is actually written.  A cancellation must never erase data.
    # A verdict that is already trustworthy is not kept: the run will not measure
    # it, so its payload is only needed for the counters below, and holding every
    # stored row of a large profile for the whole sweep is what the front half of
    # a scan must not do.
    previous = {}
    only = None
    seeded = 0
    done = set()
    now = time.time()
    for proxy, row in newest_measurements(db, profile).items():
        if recheck_passing:
            if not (selected(proxy) and counts_as_passed(row)):
                continue
            previous[proxy] = row
            continue
        if recheck:
            previous[proxy] = row
            continue
        # A normal continuation may skip a result only while its verdict is still
        # trustworthy.  In particular, UNREACHABLE and expired rows are pending on
        # the next scan; treating every row as done made a dead proxy permanent.
        # "Trustworthy" is the one admission contract, not a local freshness test:
        # a row with no recorded lifetime is never assumed fresh (defects 1, 2, 4).
        if (core.observation_state(row) != core.OBSERVATION_MISSING and not row.get('error')
                and core.time_state_of(row, now, admission_policy)['state'] == core.TIME_OK):
            done.add(proxy)
            if not selected(proxy):
                continue
            status = reputation_status(row)
            status_counts[status] = status_counts.get(status, 0) + 1
            if counts_as_passed(row):
                passed += 1
                seeded += counted(row)
            continue
        previous[proxy] = row
    if recheck_passing:
        only = set(previous)
    db.commit()

    # The scope, counted without materialising it: the progress of a sweep of a
    # half-million addresses has to have a denominator before the first probe.
    total = 0
    initial = 0
    for proxy in candidate_pages(db, collection_id):
        if (only is not None and proxy not in only) or not selected(proxy):
            continue
        total += 1
        if proxy in done:
            initial += 1

    # Addresses that already worked in another profile go first. In find-N mode
    # the rest is shuffled so early results are not all from one subnet or
    # source; a full sweep keeps key order, because random inserts into a large
    # results index make SQLite commits much slower (worst on Windows).  The
    # known-good index is the one set this run still materialises: it is what
    # makes the first results of a run the ones most likely to work.
    proven = {proxy for (proxy,) in db.execute(
        "SELECT DISTINCT proxy FROM results WHERE profile<>? AND json_extract(payload,'$.min_target_reliability')>0",
        (profile,))}

    if find.enabled and find.what == 'exit' and not exit_address_possible(config, probe, expensive_probe):
        # Asked for a unit this profile cannot produce at all.  Saying so before
        # the first probe is the difference between a run that costs a minute and
        # a run that costs the whole budget to report `found=0`.
        message = tr(
            'Нельзя набрать подтверждённые выходные IP: в профиле нет judge. '
            'Добавьте --judge-url (он же включает проверку анонимности) или считайте '
            'адреса через --count-what ip.',
            'Cannot reach the requested number of confirmed exit IPs: this profile has no judge. '
            'Add --judge-url (it also turns the anonymity check on) or count addresses with '
            '--count-what ip.')
        if progress:
            print(message, file=sys.stderr, flush=True)
        counts = {'endpoints': passed, 'unique_ips': len(unique_ips), 'exit_ips': len(unique_exits)}
        if run_state is not None:
            run_state.update(profile=profile, state='partial', stop_reason='want_unreachable_exit',
                             scope_candidates=total, checked=initial, pending=max(0, total - initial),
                             passed=passed, found=seeded, count_what=find.what, workers=0,
                             peak_inflight=0, requests=0, concurrency={}, **counts)
        if on_progress:
            on_progress(dict(phase='scanning', profile=profile, checked=initial, candidates=total,
                             passed=passed, pending=max(0, total - initial), state='partial',
                             stop_reason='want_unreachable_exit', scope_candidates=total, speed=0.0,
                             found=seeded, eta_seconds=None, workers=0, peak_inflight=0,
                             concurrency={}, reputation=status_counts, unreachable=0, **counts))
        return profile

    limiter = Rate(rate)
    unreachable = 0
    started = last_commit = time.monotonic()

    # The budgets of the run.  ``--workers`` is the ceiling the user asks for, not
    # the number of workers: the chain derives its own from the descriptors the
    # process really has and from the RAM each in-flight measurement may hold,
    # and moves that number with the observed success rate (F12).  ``--max-requests``
    # and ``--run-max-bytes`` are the totals a stage is charged against, and the
    # chain reserves one request before every stage instead of noticing the
    # overshoot afterwards.
    ram_per_inflight = max(int(config.get('max_bytes') or 0), DEFAULT_RAM_PER_INFLIGHT)
    budgets = chain.Budgets(
        max_inflight=max(1, int(workers)),
        max_open_fds=open_fd_budget(workers),
        fds_per_request=FDS_PER_REQUEST,
        max_ram_bytes=max(DEFAULT_MAX_RAM_BYTES, ram_per_inflight * 8),
        ram_per_inflight_bytes=ram_per_inflight,
        max_queue_items=max(2 * max(1, int(workers)), 64),
        max_results_pending=64,
        # The source is the scope itself, so the byte cap follows the scope
        # instead of a constant that a large collection would silently overrun.
        max_source_bytes=(total + 16) * 80,
        max_requests=max_requests, max_bytes=max_bytes, deadline_s=deadline_s)
    # One target: the expensive stage's.  The judge is somebody else's service
    # and the bandwidth test reads a body, so the share it may take is bounded
    # separately from the run's own total.
    targets = ()
    if expensive_probe is not None:
        judge_requests = max_requests if max_requests is None else max(1, max_requests // 2)
        targets = (chain.TargetPolicy(EXPENSIVE_TARGET, max_inflight=max(1, int(target_inflight)),
                                      max_requests=judge_requests),)
    limits = chain.Limits(max_per_host=max(1, int(max_per_host)),
                          min_host_interval_s=max(0.0, float(min_host_interval_s)),
                          targets=targets)
    observation_id = [None]
    # What the scope already holds when the run starts.  ``completed`` counts a
    # row from the moment the scope is satisfied, so "how much of the scope is
    # done" is one number throughout the run and not one that jumps when the
    # first new verdict is written.
    completed = initial

    def store(proxy, row, passed_ok):
        """Write one measurement, and count it.

        ``passed_ok`` is the verdict the stage that decided this item already
        made.  It is passed in rather than recomputed so that the stored row, the
        ``--want`` count and the pass counter cannot disagree about the same
        measurement: one decision, one number, three places.
        """
        nonlocal completed, last_commit, passed
        observation_id[0] = None
        country = country_of(proxy)
        if country:
            row['country'] = country
        endpoint = upsert_endpoint(db, proxy)
        # One measurement folded into the row by the one admission contract:
        # ``valid_until`` is written here, once, from the measurement time and
        # the policy of this profile revision, and is never recomputed at export
        # time (defects 1 and 2, CONTRACTS §2.1).
        measurement = core.Measurement(
            endpoint_id=endpoint, checked_at=row.get('checked_at'), verdict=row,
            error=row.get('error'), job_id=job_id, network_id=network_id,
            ok=result_allowed(row, min_success))
        merged = core.apply_measurement(previous.get(proxy), measurement, admission_policy,
                                        access=access, collection_id=collection_id,
                                        profile=(profile, int(profile_revision or 1)))
        merged.setdefault('proxy', proxy)
        # The measurement of record is written first and the result row points
        # at it, so no stop can leave a result without an observation behind it
        # (CONTRACTS §2.1).  It exists only when the run is a tracked job.
        if job_store is not None and job_id:
            observation_id[0] = record_observation(
                db, merged, job_id=job_id, endpoint_id=endpoint, access=access,
                profile=profile, profile_revision=profile_revision)
        db.execute(INSERT_RESULT, (
            profile, proxy, json.dumps(merged, ensure_ascii=False), endpoint,
            access.access_id or '', int(access.access_revision or 0), profile,
            int(profile_revision or 1),
            merged.get('checked_at'), merged.get('valid_until'),
            merged.get('error_code'), merged.get('error_stage'), job_id or '',
            observation_id[0]))
        completed += 1
        previous.pop(proxy, None)
        status = reputation_status(merged)
        status_counts[status] = status_counts.get(status, 0) + 1
        # The three units of the report beside the one the chain counts: the
        # host of every address that passed and the exit address the judge
        # confirmed.  A proxy nobody measured through a target has no exit IP,
        # and "no exit IP" is not "the exit is the proxy's own address".
        if passed_ok:
            passed += 1
            counted(merged)
        if completed % 100 == 0 or time.monotonic() - last_commit >= 1:
            db.commit()
            last_commit = time.monotonic()
        if observation_id[0] is not None:
            finish_job_item(job_store, job_id, endpoint, merged, observation_id[0])

    # -- the chain's own source, stages and result stream --------------------

    async def candidates():
        """The scope as a bounded byte stream, one page at a time.

        This is the source half of the chain: the corpus is read a page at a
        time and handed to the pipeline as a stream, so the first probes start
        while the last addresses are still being read, and a run holds a page of
        the collection instead of all of it.  ``collect`` still fills the
        collection first — the addresses have to be in the database to be a
        scope — but from here the bytes flow into the measurements directly.
        """
        # Proven addresses first, so the first results of a run are the ones
        # most likely to work.  With an empty known-good index the order is the
        # address order, which is also the cheapest order to insert into SQLite.
        phases = (True, False) if proven else (None,)
        for phase in phases:
            last = ''
            while True:
                page = [proxy for (proxy,) in db.execute(
                    _CANDIDATE_PAGE_SQL, (last, collection_id, CANDIDATE_PAGE))]
                if not page:
                    break
                last = page[-1]
                if want:
                    # find-N mode shuffles so early results are not all from one
                    # subnet or source.  Per page, so the shuffle is still spread
                    # over the corpus without materialising the corpus to do it.
                    random.shuffle(page)
                body = []
                for proxy in page:
                    if (only is not None and proxy not in only) or not selected(proxy):
                        continue
                    if proxy in done:
                        # Its verdict is still trustworthy, so this run does not
                        # measure it again — the same rule the whole continuation
                        # is built on, applied where the addresses are read.
                        continue
                    if phase is not None and (proxy in proven) is not phase:
                        continue
                    body.append(proxy)
                if not body:
                    continue
                yield ''.join(f'{proxy}\n' for proxy in body).encode('utf-8')

    rows: dict[str, dict] = {}
    store_failures: list[str] = []

    def finish(proxy, passed_ok):
        """Write the verdict the chain has just decided, before the next item.

        Durability belongs here, inside the chain, and not in the consumer of
        the result stream: a run interrupted between the decision and the
        hand-off would otherwise lose a measurement it had already paid for and
        re-measure the address next time.  A write that fails is counted rather
        than raised — one unwritable row must not take the run down — and the
        count is reported with the rest of the run.
        """
        row = rows.pop(proxy, None)
        if row is None:
            return
        try:
            store(proxy, row, passed_ok)
        except Exception as exc:  # noqa: BLE001 - one row must not end the run
            store_failures.append(type(exc).__name__)

    async def cheap_stage(item, *, stage, limit):
        """The cheap stage: does anything accept a connection at this address?

        A TCP connect is a fraction of a full request and no HTTP request at all,
        so it is charged no request — only the descriptors it holds.  Most public
        addresses are dead, and dropping them here is what keeps a sweep of a
        large corpus affordable.
        """
        nonlocal unreachable
        proxy = item.endpoint
        if await reachable(proxy, prefilter_timeout):
            return chain.StageOutcome(stage, True)
        unreachable += 1
        rows[proxy] = unreachable_result(proxy)
        finish(proxy, False)
        return chain.StageOutcome(stage, False, code='UNREACHABLE', failed_stage='tcp')

    def _verdict(stage, row, *, final):
        """One stage's own answer, and the verdict the whole run will record.

        ``final`` is the stage that applies the run's own policy (denylist,
        anonymity level, latency) and therefore the last word; an earlier stage
        only reports whether the measurement itself worked.  The verdict is
        returned as well as the stage outcome, because it is the one number the
        store and the chain's find-N must agree on — decided once, here.
        """
        ok = counts_as_passed(row) if final else _worked(row)
        if ok:
            return chain.StageOutcome(stage, True, exit_ip=exit_address_of(row) or None,
                                      latency_ms=_number(row.get('latency_ms')) or None,
                                      requests=measurement_requests(row), bytes=measurement_bytes(row)), True
        if row.get('error'):
            return chain.StageOutcome(stage, False, code=row.get('error') or 'PROBE_FAILED',
                                      failed_stage=row.get('error_stage') or 'target',
                                      latency_ms=_number(row.get('latency_ms')) or None,
                                      requests=measurement_requests(row), bytes=measurement_bytes(row)), False
        return chain.StageOutcome(stage, False, code='POLICY_REJECTED', failed_stage='policy',
                                  exit_ip=exit_address_of(row) or None,
                                  requests=measurement_requests(row), bytes=measurement_bytes(row)), False

    async def basic_stage(item, *, stage, limit):
        """The basic stage: the reputation screen, then one request per target.

        ``probe`` is the caller's own seam and is called exactly as before, with
        the profile and the rate limiter, so a caller that injects a probe gets
        the measurements it injected and no more.
        """
        proxy = item.endpoint
        if screen is not None:
            try:
                verdict = await screen(proxy, config)
            except Exception:
                verdict = {'status': 'unknown', 'checked_at': time.time(), 'error': 'SCREEN_ERROR',
                           'local_rule': None, 'dnsbl': []}
            if verdict_blocks(verdict, strict):
                rows[proxy] = blocked_result(proxy, verdict)
                finish(proxy, False)
                return chain.StageOutcome(stage, False,
                                          code=str(verdict.get('status') or 'BLOCKED').upper(),
                                          failed_stage='reputation')
        try:
            row = await probe(proxy, config, limiter)
        except Exception as exc:
            # One malformed proxy must never stop the whole scan, and the reason
            # it died is recorded with a stage so the funnel can count it
            # (F10, F25).
            row = unreachable_result(proxy)
            row['error'] = type(exc).__name__
            row['error_stage'], row['error_code'] = diagnostics.classification(exc)
        if not isinstance(row, dict):
            row = unreachable_result(proxy)
            row['error'] = 'BAD_PROBE_RESULT'
        rows[proxy] = row
        outcome, ok = _verdict(stage, row, final=expensive_probe is None)
        if expensive_probe is None:
            finish(proxy, ok)
        return outcome

    async def expensive_stage(item, *, stage, limit):
        """The expensive stage: what only a proxy that already works may cost.

        The judge is somebody else's service and the bandwidth test reads a body:
        this is where the per-target limits and the run's byte budget apply, which
        is exactly what did not happen while both were folded into one call.
        """
        proxy = item.endpoint
        row = rows.get(proxy)
        if row is None:  # pragma: no cover - the basic stage always stores a row
            return chain.StageOutcome(stage, True)
        try:
            row = await expensive_probe(proxy, config, limiter, row)
        except Exception as exc:
            # A judge that failed is unknown, never a pass, and it must not throw
            # away a working measurement: the basic verdict stands and the reason
            # is recorded on the row.
            row = dict(row)
            row['expensive_error'] = type(exc).__name__
        if not isinstance(row, dict):
            row = dict(rows.get(proxy) or unreachable_result(proxy))
        rows[proxy] = row
        outcome, ok = _verdict(stage, row, final=True)
        finish(proxy, ok)
        return outcome

    config_chain = chain.PipelineConfig(
        sources=(chain.SourceSpec(source_id=f'collection:{collection_id}', fetch=candidates),),
        budgets=budgets, limits=limits, find=find,
        # What the store already proves in the requested unit.  Those verdicts are
        # not measured again, so the chain cannot count them a second time.
        initial_met=seeded,
        runners=chain.Runners(cheap=cheap_stage if prefilter else None,
                               basic=basic_stage,
                               expensive=expensive_stage if expensive_probe is not None else None),
        normalize=_already_canonical, parse=chain.parse_lines,
        run_cheap=bool(prefilter), run_basic=True,
        run_expensive=expensive_probe is not None,
        # Deliberate decision, not a default: every proxy this run is willing to
        # publish pays for the expensive stage.
        #
        # `EXPENSIVE_UNTIL_N` stops paying for the judge and the bandwidth
        # download as soon as the requested N is met.  That is the right policy
        # for a run whose *only* goal is "give me N", and it is wrong for this
        # one: `--min-anonymity` is a filter, not a goal.  A row that skipped the
        # expensive stage has no anonymity verdict at all, so `core.Policy`
        # ranks it `unknown` and drops it from the export -- and a corpus of
        # 4000 addresses measured with `--want 5` would yield 5 elite proxies
        # and 3995 silent rejections, with the filter looking like it had
        # rejected 3995 addresses.  Under `ALL_PASSING` every passing address
        # gets a real level, and the export is "the elite ones" instead of "the
        # first five measured".
        #
        # The price is real -- one judge request and one body-sized download per
        # passing address -- and it is what `--max-requests`, `--deadline`,
        # `--judge-concurrency` and the per-target budgets exist to bound.  A
        # run that stops early leaves the unmeasured rows with no verdict at all,
        # which is the honest state; it does not leave them with a wrong one.
        expensive_policy=chain.EXPENSIVE_ALL_PASSING,
        carry_fresh_prior=False)
    engine = chain.Pipeline(config_chain, clock=chain.SystemClock(),
                            concurrency=chain.AdaptiveConcurrency(
                                minimum=1, maximum=budgets.worker_ceiling(),
                                target_success=0.8, window=16, increase=2, decrease_factor=0.5))

    def publish():
        elapsed = max(0.001, time.monotonic() - started)
        speed = (completed - initial) / elapsed
        eta = (total - completed) / speed if speed else 0
        incomplete = completed < total
        live = engine.progress()
        found_now = live.met
        state, reason, detail = engine.stopped()
        if reason in ('budget_exhausted', 'deadline_exceeded'):
            # The budget ended the run, and the budget's own code is reported in
            # place of the bare reason: "stopped" does not say which ceiling was
            # reached or how much of it was spent.
            snapshot_state = 'partial'
            stop_reason = detail or reason
        elif recheck_passing:
            snapshot_state = 'partial'
            stop_reason = 'recheck_passing'
        elif reason in ('cancelled', 'paused', 'stopped'):
            # A run that did not finish is "stopped" to everything downstream —
            # the job, the export, the report.  The chain's finer reason is kept
            # beside it rather than replacing it.
            snapshot_state = 'partial'
            stop_reason = 'stopped'
        elif reason == 'want_reached':
            snapshot_state = 'partial'
            stop_reason = 'want_reached'
        elif reason == 'items_exhausted':
            snapshot_state, stop_reason = 'complete', 'complete'
        elif incomplete and want and found_now >= want:
            snapshot_state = 'partial'
            stop_reason = 'want_reached'
        elif incomplete:
            snapshot_state = 'partial'
            stop_reason = 'stopped'
        else:
            snapshot_state = 'complete'
            stop_reason = 'complete'
        if find.enabled and not incomplete and found_now < find.n:
            # The sweep finished and the requested unit is still short.  Saying
            # `complete` alone left "N unique IPs" indistinguishable from a
            # corpus of nothing; the reason names the unit that was unreachable.
            stop_reason = ('want_unreachable_' + find.what if not found_now
                           else 'want_short_' + find.what)
        counts = {'endpoints': passed, 'unique_ips': len(unique_ips),
                  'exit_ips': len(unique_exits)}
        metrics = engine.live_metrics()
        spent = engine.resources()
        if run_state is not None:
            run_state.update(profile=profile, state=snapshot_state, stop_reason=stop_reason,
                             chain_stop=reason, chain_state=state,
                             expensive_policy=config_chain.expensive_policy,
                             run_expensive=config_chain.expensive_required,
                             scope_candidates=total, checked=completed, pending=max(0, total - completed),
                             passed=passed, found=found_now, count_what=find.what,
                             workers=metrics.workers, peak_inflight=metrics.peak_inflight,
                             requests=spent.requests, bytes=spent.bytes, peak_fds=metrics.peak_fds,
                             concurrency=metrics.concurrency, **counts)
        if on_progress:
            on_progress(dict(phase='scanning', profile=profile, checked=completed, candidates=total, passed=passed,
                             pending=max(0, total - completed), state=snapshot_state, stop_reason=stop_reason,
                             chain_stop=reason,
                             scope_candidates=total, speed=round(speed, 2), found=found_now,
                             eta_seconds=round(eta) if speed else None, workers=metrics.workers,
                             peak_inflight=metrics.peak_inflight, peak_fds=metrics.peak_fds,
                             requests=spent.requests, bytes=spent.bytes,
                             concurrency=metrics.concurrency,
                             reputation=status_counts, unreachable=unreachable, **counts))
        if progress:
            print(tr(f'Проверено {completed}/{total}; {speed:.1f} прокси/с; осталось ~{eta / 60:.1f} мин', f'Checked {completed}/{total}; {speed:.1f} proxies/s; ~{eta / 60:.1f} min left'), flush=True)

    async def reporter():
        while True:
            publish()
            await asyncio.sleep(2)

    def unmeasured(result):
        """Whether a result is a measurement the run must not record as one.

        A spent budget ends the run; it does not make the addresses it did not
        reach dead.  Such an item never reaches a runner at all — the gate
        refuses the stage before the socket is opened — so nothing was written
        for it and nothing has to be taken back.  The check stays as the one place
        that says so, in case a stage ever fails for a budget reason after it
        started.
        """
        codes = {outcome.code for outcome in result.stages if not outcome.ok}
        if codes & {chain.E_LIMIT_BUDGET, chain.DEADLINE_EXCEEDED, chain.E_LIMIT_BODY}:
            rows.pop(result.endpoint, None)
            return True
        return False

    reporter_task = None

    try:
        async def consume():
            """Drain the result stream; the verdicts are already written.

            Each item is written the moment the chain decides it, inside the
            stage that decided it, so an interrupted run keeps what it measured
            and never writes it twice.  The stream is still read here, because a
            chain whose results nobody takes blocks once its queue is full.
            """
            try:
                async for result in engine.stream():
                    unmeasured(result)
            finally:
                # The chain is over, so the live report is over with it.  Left
                # running it would keep the run alive after the last result.
                if reporter_task is not None:
                    reporter_task.cancel()

        async with asyncio.TaskGroup() as group:
            if progress or on_progress:
                reporter_task = group.create_task(reporter())
            group.create_task(consume())
    finally:
        db.commit()
        rows.clear()
        publish()
    if store_failures and run_state is not None:
        # A verdict the run decided and could not write is not a verdict the user
        # can rely on, so it is named rather than counted away.
        run_state['store_failures'] = len(store_failures)
        run_state['store_failure_kinds'] = sorted(set(store_failures))
    return profile


def anonymous_access(db, endpoint_id, scheme):
    """The credential-free access identity of one endpoint (``secrets``).

    An address without a login is still measured *as somebody*, and the schema
    says so: ``observations.access_id`` points at a real ``accesses`` row.  The
    identity is created through the secret store, so a credentialed endpoint and
    a public one differ only by the mode recorded there -- not by two parallel
    notions of "who measured this" (CONTRACTS §1.2 rule 1).
    """
    from . import secrets as secretstore
    store = secretstore.AccessStore(db, secretstore.SessionVault())
    for access in store.list_for_endpoint(endpoint_id):
        if access.mode == secretstore.MODE_NONE:
            return access
    return store.create(endpoint_id, scheme, mode=secretstore.MODE_NONE)


def record_observation(db, row, *, job_id, endpoint_id, access, profile, profile_revision):
    """Write one finished measurement as an ``observations`` row and return its id.

    The observation is the measurement of record: the row the item refers to and
    the payload the result keeps are the same instant (CONTRACTS §2.1).  It is
    written before the job item is finished so a stop between "measured" and
    "judged" still leaves the measurement recoverable.

    ``access`` is the *consumer* identity the result row carries (the shared
    public one for the built-in scan).  The observation stores the *credential*
    identity, because that is what the foreign key names; for an address
    without a login the two are the same revision, and a rotation changes only
    the credential identity.
    """
    import uuid
    scheme = (formats.split(row.get('proxy', ''))[0] if row.get('proxy') else 'http')
    stored = anonymous_access(db, endpoint_id, scheme)
    access = core.Access(stored.id, stored.access_revision)
    observation_id = uuid.uuid4().hex
    db.execute(
        '''INSERT OR REPLACE INTO observations(
               id, job_id, endpoint_id, access_id, access_revision,
               profile_id, profile_revision, started_at, finished_at, verdict,
               error_code, error_stage)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
        (observation_id, job_id or None, endpoint_id, access.access_id or '',
         int(access.access_revision or 0), profile, int(profile_revision or 1),
         row.get('checked_at'), time.time(), json.dumps(row, ensure_ascii=False),
         row.get('error_code'), row.get('error_stage')))
    return observation_id


def finish_job_item(store, job_id, endpoint_id, row, observation_id):
    """Claim the item, attach its observation and finish it.

    The order is the contract: the item leaves the queue only when a measurement
    with an explicit time exists, so a cancelled re-check keeps the previous
    payload and the queue of unfinished items survives a crash (defect 6,
    CONTRACTS §6.3).  A lifecycle that refuses the transition must never cost
    the measurement itself -- it is already written -- so the refusal is
    contained here and the job state stays the honest record of what happened.
    """
    from . import jobs
    try:
        item = store.claim(job_id, item_id=endpoint_id)
    except (jobs.JobError, sqlite3.Error):
        return False
    if item is None:
        return False
    try:
        store.record_observation(job_id, item.item_id, observation_id)
        state = 'unreachable' if row.get('error') == 'UNREACHABLE' else 'done'
        store.finish_item(job_id, item.item_id, state, observation_id=observation_id,
                          error_code=row.get('error_code'))
    except (jobs.JobError, sqlite3.Error, KeyError, AttributeError, TypeError):
        return False
    return True


def submit_scan_job(workbench, db, kind, *, profile, profile_revision, collection_id,
                    candidates, filters=None, budgets=None, idempotency_key=None):
    """Register a scan in the persistent lifecycle and return its job id.

    ``idempotency_key`` makes a repeated request return the same job instead of
    starting a second one (CONTRACTS §6.4).  Every queue item is created here, so
    a restart knows what was still unfinished.
    """
    from . import jobs
    store = workbench.jobs()
    scope = jobs.Scope(collection_id=collection_id, profile_id=profile,
                      profile_revision=int(profile_revision or 1), profile_digest=profile,
                      filters=filters or {}, budgets=budgets or {})
    items = [jobs.QueueItem(endpoint_id=schema.upsert_endpoint(db, value),
                            access_id=PUBLIC_ACCESS_ID, access_revision=1)
             for value in candidates]
    db.commit()
    job = store.submit(kind, scope, items, idempotency_key=idempotency_key)
    # ``queued`` -> ``running`` before the first claim; refused with
    # E_CONFLICT_BUSY while another job is already measuring (CONTRACTS §6.4).
    store.start(job.id)
    return job.id


def finish_scan_job(store, job_id, state, reason_code):
    """Close a job with its honest state; a refusal is not an error here."""
    from . import jobs
    if not job_id:
        return None
    try:
        return store.finish(job_id, state=state, reason_code=reason_code)
    except jobs.JobError:
        return None


def run_pool_recheck(workbench, pool_id, *, job_id, protocol='all', max_seconds=None,
                     deadline_s=None, on_progress=None):
    """Run a queued ``pool_recheck`` job: measure the pool's own collection.

    The route ``POST /v1/pools/{id}/recheck`` used to create this job and
    nothing ever executed it -- no consumer of the kind existed anywhere in the
    tree, so the client got a ``job_id`` for a run that could not start.  This
    is that consumer: the same :func:`scan`, the same profile and revision the
    pool was created with, and the job is the one the observations are written
    into, so progress, recovery and the per-item states all belong to it.

    What the pool asks for travels into the run as ``want``/``count_what`` from
    :meth:`pools.PoolSpec.find_request` -- the unit is the pool's own, not a
    hard-coded one, so a pool capped per exit-IP is measured in exits.
    """
    from . import jobs
    store = workbench.jobs()
    spec = workbench.pools().require(pool_id)
    collection = str(spec.collection_id)
    profile_row = workbench.conn.execute('SELECT config FROM profiles WHERE id=?',
                                         (spec.profile_id,)).fetchone()
    if profile_row is None:
        raise WorkbenchError(
            tr(f'Профиль пула не найден: {spec.profile_id}', f'pool profile not found: {spec.profile_id}'),
            'E_STATE_PROFILE_UNKNOWN')
    config = json.loads(profile_row[0])
    status = workbench.pools().status(pool_id)
    want = int((getattr(status, 'find', None) or {}).get('n') or 0)
    count_what = str((getattr(status, 'find', None) or {}).get('what') or spec.count_unit)
    store.start(job_id)
    # `scan` answers with the profile id; the run's own state travels in
    # `run_state`, so that is where the numbers are read from.
    state = {}
    try:
        asyncio.run(scan(
            workbench.conn, config, protocol=protocol, recheck=True,
            collection_id=collection, profile_revision=int(spec.profile_revision),
            want=want, count_what=count_what, job_id=job_id, job_store=store,
            deadline_s=max_seconds or deadline_s, on_progress=on_progress,
            progress=bool(on_progress), run_state=state))
    except Exception as exc:  # noqa: BLE001 - the job must close honestly
        from . import jobs as joblifecycle
        finish_scan_job(store, job_id, 'failed', getattr(exc, 'code', None)
                        or joblifecycle.CODE_INTERRUPTED)
        raise
    terminal = 'succeeded' if (state or {}).get('state') == core.SELECTION_COMPLETE else 'partial'
    finish_scan_job(store, job_id, terminal, (state or {}).get('stop_reason') or 'E_VERDICT_OK')
    return {'job_id': job_id, 'state': state or {}, 'pool_id': pool_id,
            'collection_id': collection, 'profile_id': spec.profile_id,
            'profile_revision': int(spec.profile_revision),
            'want': want, 'count_what': count_what, 'job_state': terminal}


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


def country_criterion(countries=(), exclude=(), basis='endpoint', unknown='exclude'):
    """The one country criterion every surface filters with (F08).

    The object is built by :mod:`proxy_workbench.geo` and nowhere else, so a
    country chosen by name, and the explicit "unknown" policy, mean the same
    thing in the CLI, in the GUI and in the API.  ``unknown`` defaults to
    excluding an address whose country is not known, because an unknown country
    is not evidence of the one that was asked for.
    """
    from . import geo
    return geo.CountryCriterion(include=tuple(countries or ()), exclude=tuple(exclude or ()),
                                basis=basis, unknown=unknown)


def criterion_digest(countries=(), exclude=(), basis='endpoint', unknown='exclude'):
    """The identity of the country criterion a snapshot was filtered with.

    It travels in the status so a reader can tell two generations apart by the
    rule that selected them, not only by the rows that survived it.
    """
    return country_criterion(countries, exclude, basis, unknown).digest()


def matches_selection(row, protocol='all', max_latency=None, countries=(), country_of=None,
                      exclude_hosting=False, provider_of=None, criterion=None, exit_country_of=None):
    """Selection by protocol, maximum median latency (ms), country codes and hosting providers.

    ``criterion`` is the whole rule (``geo.CountryCriterion``): an include set,
    an "exclude these" set, which country's country is compared and what an
    unknown one does.  Without it the historical ``countries=`` shortcut is
    used, so every existing caller keeps its behaviour.
    """
    if protocol not in (None, 'all') and proxy_protocol(row.get('proxy', '')) != protocol:
        return False
    if exclude_hosting and is_hosting(row.get('proxy', ''), provider_of):
        return False
    if criterion is not None and getattr(criterion, 'active', False):
        policy = core.Policy(country_criterion=criterion, country_of=country_of,
                             exit_country_of=exit_country_of)
        return core._country_criterion_reason(row, policy) is None
    if countries:
        # The same criterion object the GUI and the API build; ``unknown``
        # excludes an address whose country was never established.
        verdict = geo_country_verdict(country_criterion(countries), row, country_of)
        if not verdict.matched:
            return False
    if max_latency:
        latency = row.get('latency_ms')
        if latency is None or latency > max_latency:
            return False
    return True


def geo_country_verdict(criterion, row, country_of=None, now=None):
    """Evaluate one row against the country criterion, through ``geo``.

    The row's own country and the database's answer each become a
    ``CountryFact`` with their own source, so the verdict can say which one it
    compared and whether the fact was stale -- a country is never silently
    accepted.
    """
    from . import geo
    moment = time.time() if now is None else now
    declared = row.get('country')
    resolved = country_of(row.get('proxy', '')) if country_of else None
    fact = None
    if declared or resolved:
        fact = geo.CountryFact(code=(declared or resolved),
                               source=geo.SOURCE_SOURCE if declared else geo.SOURCE_RESOLVED,
                               at=moment, address=row.get('proxy', '').partition('://')[2])
    return geo.evaluate(criterion, endpoint=fact, now=moment)


def export(db, profile, directory, *, top=0, sort='quality', min_success=2/3, denylist=None, local_override=None, min_anonymity='any',
           protocol='all', max_latency=None, countries=(), country_exclude=(),
           country_basis='endpoint', country_unknown='exclude',
           country_of=None, exclude_hosting=False, provider_of=None,
           watch_minutes=0, allowed_proxies=None, run_state=None, diagnostic=False, query='', quick='',
           active_profile_path=None, collection_id=None, profile_revision=1, max_age_seconds=None,
           access=None, credentials='redact', client_target=None, client_binary=None,
           secret_grant=None):
    """Write one immutable generation and, for a real run, publish it.

    The files, the status and the pointer are produced by the snapshot service
    (``exportsvc``).  This function only decides *what* goes into the artifact:
    the scope, the policy and the rows the current filters select.  Three kinds
    of artifact exist and only one of them may switch the active pool -- an
    exported selection, a top-N slice and a search result all stay separate
    files until the user publishes them explicitly (defects 3, 7, R04).

    ``diagnostic=True`` never touches the active pointer; ``allowed_proxies``
    makes the artifact a ``selection``.  A re-export with a different ``--watch``
    cannot change the lifetime of an already measured address, because
    ``valid_until`` was written at measurement time (defects 1, 2).
    """
    from . import exportsvc

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
    reputation_policy = cfg.get('reputation', {})
    strict = bool(reputation_policy.get('strict', False))
    # A minimum anonymity level only applies to profiles that asked a judge.
    if not cfg.get('anonymity'):
        min_anonymity = 'any'
    else:
        # Defect 13: a level asked of an export without a judge is not a
        # preference, it is an unprovable claim.  `probes.require_anonymity`
        # owns the rule; the old behaviour silently dropped the requirement and
        # wrote the rows out as if the user had got what they asked for.
        from . import probes
        judge_url = str((cfg.get('anonymity') or {}).get('judge_url') or '').strip()
        probes.require_anonymity(min_anonymity,
                                 probes.validate_judge_spec({'url': judge_url} if judge_url else None))
    if local_override is False:
        active_denylist = None
    elif local_override is True or reputation_policy.get('local_enabled', True):
        active_denylist = denylist
    else:
        active_denylist = None
    if active_denylist is not None and active_denylist.error:
        raise ValueError('Не удалось прочитать локальный denylist; экспорт остановлен.')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    published_at = time.time()
    collection_id = ensure_collection(db, collection_id)
    access = as_access(access)
    max_age_seconds = float(max_age_seconds if max_age_seconds else MIN_FRESHNESS_SECONDS)
    countries = frozenset(countries or ())
    network_id = snapshot_network(cfg)
    # F08 parity: the export now filters with the *same* criterion object the
    # GUI and the API build, so `basis=exit`, the deliberate "never here" set
    # and the three `unknown` policies mean the same thing on every surface.
    # Before this only `basis=endpoint` with an include list was expressible,
    # and the export dropped a row the table kept (or the other way round).
    criterion = country_criterion(countries, country_exclude, country_basis, country_unknown)
    policy = core.Policy(
        max_age_seconds=max_age_seconds, min_success=policy_min_success(min_success),
        min_anonymity=min_anonymity, strict=strict, protocol=protocol or None,
        countries=countries, max_latency_ms=max_latency,
        country_criterion=criterion, exit_country_of=country_of,
        exclude_hosting=bool(exclude_hosting),
        denied=frozenset(active_denylist.proxies) if active_denylist is not None else frozenset(),
        deny_match=active_denylist.match if active_denylist is not None else None,
        protocol_of=proxy_protocol, country_of=country_of,
        hosting_of=(lambda proxy: is_hosting(proxy, provider_of)) if exclude_hosting else None)
    scope = core.Scope(collection_id, profile, int(profile_revision or 1), network_id)
    export_scope = exportsvc.ExportScope(identity=scope, protocol=protocol or 'all',
                                         countries=tuple(sorted(countries)),
                                         exclude_hosting=bool(exclude_hosting), query=query, quick=quick,
                                         selection=tuple(sorted(selection_requested or ())),
                                         top=int(top or 0))
    kind = 'diagnostic' if diagnostic else ('selection' if selection_requested is not None else 'published')

    stored_checked = passed = local_filtered = 0
    sources = source_map(db)
    listed = listed_counts(db)
    source_quality = {}
    status_counts = {'clean': 0, 'listed': 0, 'unknown': 0, 'local_denied': 0}
    anonymity_counts = {}
    breakdown = {'protocols': {}, 'countries': {}}
    scope_candidates = len(collection_candidates(db, collection_id))
    rows = []
    # The endpoint id comes from the column, not from the payload: it is the
    # identity of the address, and a row written before the column existed has
    # none in its JSON.  Without it a published row cannot be addressed at all --
    # `GET /v1/results/{id}` had nothing stable to compare the path segment with.
    for payload, endpoint_id in db.execute(
            'SELECT payload, endpoint_id FROM results WHERE profile=?', (profile,)):
        try:
            row = json.loads(payload)
        except (TypeError, ValueError):
            continue
        if endpoint_id:
            row['endpoint_id'] = endpoint_id
        proxy = row.get('proxy', '')
        # Source health is an independent measurement.  Count only rows that
        # would be handed to a client on their own, never the current export's
        # search, latency, country, hosting or selected-proxy filters.
        source_passed = int(result_allowed(row, min_success, denylist=None, strict=False, min_anonymity='any'))
        for source in (_source_values(sources.get(proxy)) or ('unknown',)):
            quality = source_quality.setdefault(source, {'checked': 0, 'passed': 0})
            quality['checked'] += 1
            quality['passed'] += source_passed
        stored_checked += 1
        status = reputation_status(row)
        status_counts[status] = status_counts.get(status, 0) + 1
        level = (row.get('anonymity') or {}).get('level')
        if level:
            anonymity_counts[level] = anonymity_counts.get(level, 0) + 1
        rows.append(row)

    selection = core.select(rows, scope, access, policy, published_at, published_at=published_at)
    recommended = recommender(source_quality, listed, sources, min_success)
    selected = []
    selection_eligible = set()
    for row in selection.admitted:
        proxy = row.get('proxy', '')
        if quick == 'clean' and reputation_status(row) != 'clean':
            continue
        speed_mbps = (row.get('speed') or {}).get('mbps', row.get('mbps')) if isinstance(row.get('speed'), dict) else row.get('mbps')
        if quick == 'speed' and not (speed_mbps is not None and speed_mbps > 0):
            continue
        if quick == 'http' and proxy_protocol(proxy) not in ('http', 'https'):
            continue
        if query and query not in proxy.lower():
            continue
        if selection_requested is not None and proxy not in selection_requested:
            continue
        if active_denylist is not None and active_denylist.match(proxy):
            local_filtered += 1
            continue
        published_row = dict(row)
        verdict = row.get('reputation') if isinstance(row.get('reputation'), dict) else {}
        dnsbl = verdict.get('dnsbl') if isinstance(verdict.get('dnsbl'), list) else []
        published_row['reputation_status'] = reputation_status(row)
        published_row['reputation_sources'] = ','.join(item.get('zone', '') for item in dnsbl
                                                       if isinstance(item, dict) and item.get('status') == 'listed')
        published_row['country'] = row_country(row, country_of) or ''
        published_row['exit_ip'] = (row.get('anonymity') or {}).get('exit_ip', '')
        published_row['listed_in'] = listed.get(proxy, 1)
        published_row['source_keys'] = list(_source_values(sources.get(proxy)))
        published_row['recommended'] = recommended(row)
        published_row['exit_country'] = exit_country(row, country_of) or ''
        provider = (provider_of(proxy) if provider_of else None) or {}
        published_row['asn'] = provider.get('asn')
        published_row['provider'] = provider.get('org')
        published_row['hosting'] = provider.get('hosting')
        history = row_history(row, min_success)
        published_row['checks'] = history['checks']
        published_row['passes'] = history['passes']
        published_row['mbps'] = (row.get('speed') or {}).get('mbps') if isinstance(row.get('speed'), dict) else None
        # An access identity is published only when it points at a credential
        # (CONTRACTS §4.4).  The credential-free public identity is how the row
        # was *measured*; carrying it as an access reference would make every
        # plain proxy look password-protected and the compatibility report would
        # drop it from PAC, Clash and sing-box.  The stored row keeps it either
        # way, so re-admitting the measurement still matches the scope.
        if not (row.get('access_ref') or row.get('credentials_state') == 'reference'):
            published_row.pop('access_id', None)
            published_row.pop('access_revision', None)
        selected.append(published_row)
        selection_eligible.add(proxy)
    passed = len(selection.admitted)
    if sort not in EXPORT_ORDERS:
        raise ValueError('Неизвестный порядок сортировки экспорта.')
    selected = sort_rows(selected, sort)
    for row in selected:
        for group, value in (('protocols', proxy_protocol(row['proxy'])),
                             ('countries', row.get('country') or '??')):
            breakdown[group][value] = breakdown[group].get(value, 0) + 1
    if top:
        selected = selected[:top]

    run_state = dict(run_state or {})
    if not run_state:
        run_state = dict(state='complete' if stored_checked == scope_candidates else 'partial',
                         stop_reason='complete' if stored_checked == scope_candidates else 'stopped',
                         checked=stored_checked, scope_candidates=scope_candidates, passed=passed)
    if selection.state != core.SELECTION_COMPLETE:
        # The state describes the set, not the sweep: nothing matched is
        # "empty", everything expired is "stale", and a set that lost members
        # is "partial".  Calling all three "complete" is what made one dead
        # address indistinguishable from a finished run (CONTRACTS §4.3, defect 3).
        run_state['state'] = selection.state
        run_state.setdefault('state_detail', selection.state_detail)
        run_state.setdefault('stop_reason', 'complete')
    run_state.setdefault('checked', stored_checked)
    run_state.setdefault('scope_candidates', scope_candidates)
    run_state.setdefault('passed', passed)
    run_state['candidates'] = int(db.execute('SELECT count(*) FROM candidates').fetchone()[0])
    run_state['local_filtered'] = local_filtered

    options = exportsvc.ExportOptions(sort=sort, top=int(top or 0), credentials=credentials,
                                      client_target=client_target, client_binary=client_binary,
                                      keep_generations=EXPORT_GENERATION_RETENTION,
                                      published_at=published_at, set_ttl_seconds=max_age_seconds)
    extra = dict(
        targets=[dict(name=t.get('name', ''), url=public_url(t['url'])) for t in cfg.get('targets', [])],
        request_profile=cfg.get('request_profile', 'workbench'),
        request_profile_digest=cfg.get('request_profile_digest', ''),
        reputation=dict(reputation_policy, counts=status_counts),
        anonymity=dict(enabled=bool(cfg.get('anonymity')), min_level=min_anonymity, counts=anonymity_counts),
        network_id=network_id,
        source_quality=source_quality, source_health_basis='fresh_profile_checks',
        breakdown=breakdown, watch_minutes=float(watch_minutes),
        min_success=min_success, protocol=protocol, max_latency=max_latency,
        countries=list(countries or ()), exclude_hosting=bool(exclude_hosting and provider_of),
        query=query, quick=quick, sort=sort, top=int(top or 0))
    artifact = exportsvc.write_snapshot(directory, selected, scope=export_scope, options=options,
                                        kind=kind, policy=policy, selection=selection, now=published_at,
                                        run_state=run_state, extra=extra, grant=secret_grant)
    report = artifact.status.as_dict()
    report['exported'] = len(selected)
    # ``complete`` answers a different question from ``state``: it says the run
    # covered its whole scope.  A sweep that finished and rejected a few rows is
    # still complete -- conflating the two is what made "everything expired" and
    # "every check finished" look alike (CONTRACTS §4.3, defect 3).
    report['complete'] = bool(
        run_state.get('state') in (core.SELECTION_COMPLETE, core.SELECTION_PARTIAL)
        and int(run_state.get('checked') or 0) >= int(run_state.get('scope_candidates') or 0)
        and not report.get('stale'))
    report['selection_requested'] = len(selection_requested) if selection_requested is not None else 0
    report['selection_exported'] = len(selected) if selection_requested is not None else 0
    report['selection_missing'] = sorted(selection_requested - selection_eligible) if selection_requested is not None else []
    report['selection_truncated'] = max(0, len(selection_eligible) - len(selected)) if selection_requested is not None else 0
    report['kind'] = kind
    report['directory'] = str(artifact.directory)
    report['files'] = list(artifact.files)
    try:
        exportsvc.record_artifact(db, artifact)
        db.commit()
    except sqlite3.Error:
        # The artifact is already on disk; refusing to publish because the
        # bookkeeping table is unavailable would lose the user's work.
        db.rollback()
    # Only a published run may repoint an already connected client.  A selected
    # slice, a top-N cut and a search result are files the user downloads; they
    # never move the active pool, and they never touch ``last-profile.txt``
    # (defect 7, R04, CONTRACTS §4.5).  ``exportsvc.publish`` refuses a
    # selection outright -- the branch below exists so a published run still
    # gets the legacy compatibility files and the pointer in one rollback-safe
    # step.
    pointer_name = exportsvc.DIAGNOSTIC_POINTER_NAME if diagnostic else exportsvc.POINTER_NAME
    profile_path = Path(active_profile_path) if active_profile_path and not diagnostic else None
    if kind == 'selection':
        pass
    elif not diagnostic:
        _publish_legacy_files(directory, artifact.directory, artifact.files, report,
                              before_commit=lambda: exportsvc.publish(artifact, directory, confirm=True,
                                                                     pointer_name=pointer_name))
    else:
        exportsvc.publish(artifact, directory, confirm=True, pointer_name=pointer_name)

    if profile_path is not None:
        try:
            profile_path.parent.mkdir(parents=True, exist_ok=True)
            atomic(profile_path, profile + '\n')
        except OSError:
            # The pointer already moved; failing the whole export here would hide
            # a published generation behind an error.
            pass
    return report


def sort_rows(rows, order):
    """Order rows exactly as the SQL statement of the same name used to.

    The published order is part of the artifact's contract (``recommended``
    first, quality next), so it is applied here in one place instead of being
    left to a temporary SQLite table.
    """
    keys = {
        'quality': lambda row: (-(row.get('score') or 0), row.get('latency_ms') if row.get('latency_ms') is not None else math.inf, row.get('proxy') or ''),
        'speed': lambda row: (row.get('latency_ms') if row.get('latency_ms') is not None else math.inf, -(row.get('reliability') or 0), row.get('proxy') or ''),
        'stability': lambda row: (row.get('jitter_ms') if row.get('jitter_ms') is not None else math.inf, row.get('latency_ms') if row.get('latency_ms') is not None else math.inf, row.get('proxy') or ''),
        'uptime': lambda row: (-(uptime_of(row)), -(row.get('checks') or 0), -(row.get('score') or 0), row.get('proxy') or ''),
        # The published order is ``bandwidth IS NULL`` ascending, so an
        # unmeasured speed sorts last -- the same order the SQL statement gave.
        'bandwidth': lambda row: (1 if row.get('mbps') is None else 0, -(row.get('mbps') or 0), -(row.get('score') or 0), row.get('proxy') or ''),
        'recommended': lambda row: (-(row.get('recommended') or 0), -(row.get('score') or 0), row.get('proxy') or ''),
    }
    return sorted(rows, key=keys.get(order, keys['quality']))


def uptime_of(row):
    checks = row.get('checks') or 0
    return (row.get('passes') or 0) / checks if checks else 0.0


class Workbench:
    """The one service layer behind the CLI, the API and the GUI (F18).

    Every module built in this wave -- ``jobs``, ``importer``, ``profiles``,
    ``probes``, ``pools``, ``scheduler``, ``sourcedesk``, ``geo``, ``pipeline``,
    ``secrets``, ``servicecatalog``, ``apikeys`` -- is reached through this
    object.  A module that only its own tests import is dead code, so each
    store below is a named method rather than an import in a test file: the
    command, the route and the GUI all arrive here.

    The layer owns no second engine.  It opens the database through
    :func:`open_db` (one migrator, one writer), hands out the module stores and
    translates their contract errors into :class:`WorkbenchError`, which the
    CLI prints and the API turns into a stable ``E_*`` code.  No public
    exception of a module leaks past this boundary.
    """

    def __init__(self, data, *, db_path=None, clock=None):
        self.data = Path(data)
        self.db_path = Path(db_path) if db_path else self.data / 'proxies.sqlite3'
        self.clock = clock or time.time
        self._conn = None
        self._report = None
        self._stores = {}
        self._counter = 0

    # -- database ----------------------------------------------------------

    def connect(self):
        """Open (and migrate) the workbench database, once per object."""
        if self._conn is None:
            self._conn, self._report = open_db_with_report(self.db_path, app_version=PRODUCT_VERSION)
        return self._conn

    @property
    def conn(self):
        return self.connect()

    def clock_ns(self):
        """A value no two calls in one run return twice (an idempotency key)."""
        self._counter += 1
        return f'{int(self.clock() * 1000)}-{self._counter}'

    @property
    def migration_report(self):
        self.connect()
        return self._report

    def close(self):
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.commit()
            finally:
                conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def _store(self, name, factory):
        store = self._stores.get(name)
        if store is None:
            store = factory()
            self._stores[name] = store
        return store

    # -- module stores -----------------------------------------------------

    def jobs(self):
        """Persistent job and item lifecycle (``jobs.JobStore``)."""
        from . import jobs
        return self._store('jobs', lambda: jobs.JobStore(self.conn))

    def pools(self):
        from . import pools
        return self._store('pools', lambda: pools.PoolStore(self.conn))

    def schedules(self):
        """Schedules, quiet hours and budgets (``scheduler.SqliteScheduleStore``)."""
        from . import scheduler
        return self._store('schedules', lambda: scheduler.SqliteScheduleStore(self.conn))

    def keys(self):
        """API keys, permissions and audit (``apikeys.ApiKeyManager``)."""
        from . import apikeys
        apikeys.ensure_schema(self.conn)
        return self._store('keys', lambda: apikeys.ApiKeyManager(self.conn))

    def profiles(self):
        from . import profiles
        # ``open_store`` accepts a path and a live connection; the connection is
        # already migrated by :func:`open_db`, so nothing is migrated twice.
        return self._store('profiles', lambda: profiles.ProfileStore(self.conn, verify=True))

    def accesses(self, vault=None):
        """Secret store and access identities (``secrets.AccessStore``)."""
        from . import secrets as secretstore
        if vault is not None:
            return secretstore.AccessStore(self.conn, vault)
        return self._store('accesses', lambda: secretstore.AccessStore(
            self.conn, secretstore.SessionVault()))

    def sources(self):
        """User sources, subscriptions and feed lifecycle (``sourcedesk``)."""
        from . import sourcedesk
        return self._store('sources', lambda: sourcedesk.SourceDesk(self.conn))

    def catalog(self, path=None):
        """Versioned service presets (``servicecatalog``)."""
        from . import servicecatalog
        return self._store('catalog', lambda: servicecatalog.load_catalog(path))

    def geo_resolver(self, country_db=None, provider_db=None, declared=None):
        from . import geo
        return geo.Resolver(country_db=country_db, provider_db=provider_db,
                            declared=declared, now=self.clock())

    # -- country criterion (geo) -------------------------------------------

    def country_criterion(self, include=(), exclude=(), basis='endpoint',
                          unknown='exclude', max_age_seconds=None):
        """One country criterion for CLI, GUI and API (F08, geo.CountryCriterion).

        The three surfaces build the same object through this method, so a
        country chosen by name and ``unknown`` behave identically everywhere
        and one filter answers for all of them.
        """
        from . import geo
        return geo.CountryCriterion(include=tuple(include or ()), exclude=tuple(exclude or ()),
                                    basis=basis, unknown=unknown,
                                    max_age_seconds=max_age_seconds)

    def filter_by_country(self, rows, criterion, resolver=None, now=None):
        from . import geo
        return geo.filter_rows(rows, criterion, resolver=resolver, now=now or self.clock())

    # -- import (importer + sourcedesk) ------------------------------------

    def import_source(self, data, name=None, channel='file'):
        """A named import source for the transactionally committing importer."""
        from . import importer
        if isinstance(data, (str, Path)) and Path(data).is_file():
            return importer.ImportSource.from_path(Path(data), channel=channel)
        return importer.ImportSource.from_text(str(data), name=name or 'clipboard', channel=channel)

    def import_preview(self, source, collection_id, mode='merge', fmt=None, policy=None,
                       idempotency_key=None):
        from . import importer
        return importer.preview(self.conn, source, collection_id=collection_id, mode=mode,
                                fmt=fmt, policy=policy, idempotency_key=idempotency_key,
                                now=self.clock())

    def import_commit(self, plan, *, allow_partial=False, busy=None, should_cancel=None):
        """Commit a preview, deferring while the collection is being checked.

        ``busy`` answers with a reason while a job is still measuring that
        collection: the commit is refused with ``E_CONFLICT_BUSY`` instead of
        writing membership underneath a running scan (defect: R1).
        """
        from . import importer
        return importer.commit(self.conn, plan, allow_partial=allow_partial, busy=busy,
                               should_cancel=should_cancel, now=self.clock())

    def import_client_config(self, document, fmt, *, policy=None, limits=None):
        """Endpoints of a Clash/sing-box/subscription document (``sourcedesk``).

        This is the *format* half of an import: ``importer`` owns plain lists
        (TXT, URI, CSV, JSON) and this path owns proxy-client documents, so the
        two never become a second normalizer for the same input.  Credentials in
        a document are refused here exactly as they are in a flat list.
        """
        from . import sourcedesk
        if fmt == 'clash':
            return sourcedesk.import_clash(document, normalize=normalize_custom, limits=limits)
        if fmt == 'singbox':
            return sourcedesk.import_singbox(document, normalize=normalize_custom, limits=limits)
        raise WorkbenchError(tr(f'Неизвестный формат конфигурации: {fmt}',
                                f'unknown configuration format: {fmt}'))

    def add_members(self, collection_id, canonical_values, origin='import'):
        """Add canonical addresses to a collection through the one membership writer."""
        added = 0
        for value in canonical_values:
            endpoint = upsert_endpoint(self.conn, value)
            schema.add_member(self.conn, collection_id, endpoint, origin=origin, now=self.clock())
            self.conn.execute('INSERT OR IGNORE INTO candidates(proxy, endpoint_id) VALUES (?, ?)',
                              (value, endpoint))
            added += 1
        self.conn.commit()
        return added

    # -- profiles and presets ----------------------------------------------

    def profile_list(self):
        return list(self.profiles().list())

    def presets(self, query='', category=None, capability=None):
        from . import servicecatalog
        return servicecatalog.search_presets(self.catalog(), query, category, capability)

    # -- pools -------------------------------------------------------------

    def pool_status(self, pool_id):
        return self.pools().status(pool_id)

    def pool_refill(self, pool_id, source, budget=None, now=None):
        from . import pools
        return pools.refill(self.pools(), pool_id, source, budget=budget,
                            now=now if now is not None else self.clock())

    # -- schedules ---------------------------------------------------------

    def schedule_plan(self, spec_id, now=None):
        """Next run of one schedule, with its timezone and window (F15)."""
        from . import scheduler
        engine = scheduler.Scheduler(self.schedules(), clock=self.clock)
        spec = next((item for item in engine.list() if item.id == spec_id), None)
        if spec is None:
            raise WorkbenchError(tr(f'Расписание не найдено: {spec_id}',
                                    f'schedule not found: {spec_id}'))
        moment = self.clock() if now is None else now
        return engine.plan(spec, engine.state(spec.id), moment)

    # -- probes ------------------------------------------------------------

    def probe_options(self, **fields):
        from . import probes
        return probes.validate_options(fields)

    def probe_one(self, proxy, targets, options, transport):
        """Check one endpoint with the modes-and-evidence probe (``probes``).

        ``transport`` is the caller-owned callable that opens a connection
        through ``proxy``; the engine supplies it, so this module never becomes
        a second network entry point.
        """
        from . import probes
        return probes.check_targets(transport, tuple(targets), options)

    def probe_mode(self, name, base=None, monitoring=False):
        from . import probes
        return probes.resolve_mode(name, base=base, monitoring=monitoring)

    # -- pipeline ----------------------------------------------------------

    def pipeline_priors(self, collection_id, profile_id, max_age_s):
        """Known-good rows of one scope as pipeline priors, ages kept intact.

        A carried-over prior keeps its own ``checked_at``: re-exporting or
        changing ``--watch`` must not mint a new measurement time (defect 2).
        """
        from . import pipeline
        now = self.clock()
        rows = self.conn.execute(
            'SELECT endpoint_id, proxy, checked_at, valid_until FROM results '
            'WHERE profile_id=? ORDER BY checked_at DESC', (profile_id,)).fetchall()
        entries = []
        for row in rows:
            checked = row['checked_at'] if isinstance(row, sqlite3.Row) else row[2]
            value = row['valid_until'] if isinstance(row, sqlite3.Row) else row[3]
            if checked is None or value is None or value < now:
                continue
            proxy = row['proxy'] if isinstance(row, sqlite3.Row) else row[1]
            entries.append(pipeline.PriorItem(proxy, float(checked), value=float(value)))
        return pipeline.PriorIndex(entries, now=now, max_age_s=max_age_s)

    # -- keys --------------------------------------------------------------

    def key_bootstrap(self, name='administrator', permissions=None, ttl_s=None):
        """Issue the first administrative key through a local trusted bootstrap.

        This is the only way an administrator comes into existence, and it needs
        a locally trusted caller, not a secret from the network (F29).

        The default permission set is *this machine's* administrator: key
        administration plus every write the control API offers.  Anything less
        would hand the user a key that cannot create a collection, import a
        list, submit a check or export -- the key would exist and still answer
        403 for everything they asked it for.
        """
        return self.keys().bootstrap_admin(local_trusted=True, name=name,
                                           permissions=permissions or local_admin_permissions(),
                                           ttl_s=ttl_s)

    def key_admin(self, secret):
        """The administrative identity a presented secret resolves to, or ``None``."""
        from . import apikeys
        try:
            return self.keys().authenticate(secret, permission='admin.keys')
        except apikeys.ApiKeyError:
            return None

    def key_actor(self, secret):
        from . import apikeys
        return self.keys().authenticate(secret)

    # -- backups and layout (desktop) --------------------------------------

    def layout(self):
        from . import desktop
        return desktop.resolve_layout()

    def backup_create(self, reason='manual', name=None):
        """A technical backup of the database with its manifest and checksum."""
        if not self.db_path.is_file():
            raise WorkbenchError(tr('Нет базы данных для резервной копии.',
                                    'there is no database to back up'))
        return schema.create_backup(self.db_path, self.backup_dir, reason=reason, name=name,
                                    app_version=PRODUCT_VERSION, now=self.clock())

    @property
    def backup_dir(self):
        """Where backups live: inside the data folder the desktop layout names."""
        return self.data / 'backups'

    def backup_list(self):
        """Existing backups with their manifest, newest first."""
        target = self.backup_dir
        if not target.is_dir():
            return []
        found = []
        for path in sorted(target.iterdir(), reverse=True):
            if not path.is_file() or path.name.endswith(schema.MANIFEST_SUFFIX):
                continue
            manifest = path.with_name(path.name + schema.MANIFEST_SUFFIX)
            info = {'name': path.name, 'path': str(path), 'manifest': str(manifest),
                    'present': path.is_file(), 'size': path.stat().st_size}
            if manifest.is_file():
                try:
                    info['manifest_data'] = schema.as_manifest(manifest)
                except (OSError, UnicodeError, ValueError, schema.DbError):
                    info['manifest_data'] = None
            found.append(info)
        return found


def local_admin_permissions():
    """What an administrator of *this* installation may do.

    Key administration on its own is not administration: a key that may only
    read keys cannot create a collection, import a list, submit a check or
    export, and the user would see 403 for every action they took it for (F29).
    """
    from . import apikeys
    return tuple(sorted(set(apikeys.ADMIN_PERMISSIONS) | set(apikeys.WRITE_PERMISSIONS)
                        | set(apikeys.READ_PERMISSIONS)))


class WorkbenchError(RuntimeError):
    """A management operation refused; the CLI prints it, the API codes it.

    Module exceptions stay inside :class:`Workbench`; this is the single
    boundary both surfaces share, so a refusal reads the same in a terminal and
    in a JSON body (CONTRACTS §5.4).
    """

    def __init__(self, message, code='E_STATE_SNAPSHOT_STATIC'):
        super().__init__(message)
        self.code = code


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
    p.add_argument('command', choices=['collect', 'scan', 'run', 'export', 'get', 'test', 'serve', 'gateway', 'clear-data', 'update-geoip',
                                      'import', 'source', 'api-key', 'pool', 'schedule', 'profile',
                                      'preset', 'backup', 'geo', 'diagnose', 'bench'],
                   help=tr('run — собрать и проверить; collect — только собрать; scan — только проверить; '
                           'export — пересобрать файлы; get — вывести готовые прокси; test — проверить свои прокси; '
                           'serve — локальное API; gateway — ротирующий прокси; '
                           'update-geoip — база стран; bench — замер конвейера на локальной фикстуре; '
                           'import/source — свои списки и подписки; api-key — ключи локального API; '
                           'pool/schedule — постоянные пулы и расписания; profile/preset — профили и наборы сервисов; '
                           'backup — резервные копии; geo — состояние базы стран; '
                           'clear-data — удалить результаты',
                           'run: collect and check; collect: only collect; scan: only check; '
                           'export: rebuild the files; get: print working proxies; test: check given proxies; '
                           'serve: local API; gateway: rotating proxy; '
                           'update-geoip: country database; bench: pipeline benchmark on a local fixture; '
                           'import/source: your own lists and subscriptions; api-key: local API keys; '
                           'pool/schedule: steady pools and schedules; profile/preset: profiles and service sets; '
                           'backup: database backups; geo: country database state; '
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
    # `exportsvc` imports constants from this module, so it is imported inside a
    # function -- at parse time the module is fully loaded and the cycle is closed.
    from . import exportsvc as _exportsvc
    p.add_argument('--credentials', choices=list(_exportsvc.CREDENTIALS_MODES), default='redact',
                   help=tr('что писать для адресов, измеренных через учётные данные: redact — без доступа, '
                           'reference — ссылка на запись хранилища (само значение секрета в артефакт не попадает)',
                           'what to write for addresses measured with credentials: redact writes nothing, '
                           'reference writes a reference to the vault entry (never the secret value)'))
    # F20/area-export §2: the sing-box file has two shapes depending on the
    # client version (rule actions from 1.11, the deprecated `block` outbound
    # before it).  Without a target the versioned compatibility check cannot
    # run and the user receives an unverified file that exportsvc itself
    # refuses for a pinned client.
    p.add_argument('--client-target', default=os.environ.get('PROXY_WORKBENCH_SINGBOX_TARGET') or None,
                   help=tr('версия клиента sing-box, под которую проверяется файл: 1.11.0, latest, legacy-block',
                           'sing-box client version the export is checked against: 1.11.0, latest, legacy-block'))
    p.add_argument('--client-binary', default=os.environ.get('PROXY_WORKBENCH_SINGBOX_BIN') or None,
                   help=tr('путь к sing-box, которым проверяется сгенерированный файл',
                           'path to the sing-box binary used to check the generated file'))
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
    # F08 parity: the same rule the GUI and the API express, now expressible
    # from the command line too -- an exit country, a "never here" set, and
    # what an unknown country does.
    p.add_argument('--country-basis', choices=('endpoint', 'exit', 'either'), default='endpoint',
                   help=tr('чью страну сравнивать: адреса, выхода или любую из двух',
                           'which country to compare: the endpoint, the exit, or either'))
    p.add_argument('--country-exclude', default='',
                   help=tr('страны, которых быть не должно ни при каком основании (ISO-коды через запятую)',
                           'countries that must never appear, whatever the basis (ISO codes, comma separated)'))
    p.add_argument('--country-unknown', choices=('exclude', 'include_unverified', 'require_measurement'),
                   default='exclude',
                   help=tr('что делать с адресом, чья страна неизвестна',
                           'what to do with an address whose country is unknown'))
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
    # The gateway password is a different identity from the API token (CONTRACTS
    # §5.1, defect 18).  `gateway` used to read `--api-token`, so one value was
    # both the rotating-proxy password and the read-only bearer token: whoever
    # knew the password from a phone in the LAN could read the whole published
    # snapshot, and a leaked API token was a working proxy.
    p.add_argument('--gateway-token', default=os.environ.get('PROXY_WORKBENCH_GATEWAY_TOKEN') or None,
                   help=tr('gateway: пароль клиентов шлюза (или переменная PROXY_WORKBENCH_GATEWAY_TOKEN); '
                           'не связан с --api-token; на сетевом bind генерируется, если не задан',
                           'gateway: client password for the gateway (or PROXY_WORKBENCH_GATEWAY_TOKEN); '
                           'unrelated to --api-token; generated on a network bind when not given'))
    # F24: the second half of the requirement was a library nothing could reach.
    # Every action that changes data is a two-call contract: it prints what would
    # happen, and only `--apply` performs it.
    p.add_argument('--apply', action='store_true',
                   help=tr('backup: выполнить предпросмотренное действие (восстановление, откат, '
                           'retention, очистка, перепривязка секретов)',
                           'backup: run the previewed action (restore, rollback, retention, cleanup, '
                           'secret rebind)'))
    p.add_argument('--to-data', type=Path, default=None,
                   help=tr('backup: папка назначения для восстановления или отката',
                           'backup: target folder for a restore or a rollback'))
    p.add_argument('--target', default='',
                   help=tr('backup retention: через запятую observations,results',
                           'backup retention: observations,results, comma separated'))
    p.add_argument('--retention-hours', dest='retention_hours', type=float, default=0,
                   help=tr('backup retention: удалять строки старше N часов',
                           'backup retention: delete rows older than N hours'))
    p.add_argument('--include-fresh', action='store_true',
                   help=tr('backup retention: удалять и ещё не истёкшие строки по возрасту',
                           'backup retention: delete rows that have not expired yet, by age'))
    p.add_argument('--keep-newest', type=int, default=0,
                   help=tr('backup retention: сохранить N самых свежих строк',
                           'backup retention: keep the N newest rows'))
    p.add_argument('--vacuum', action='store_true',
                   help=tr('backup retention: выполнить VACUUM после удаления',
                           'backup retention: run VACUUM after deleting'))
    p.add_argument('--keep-lock', action='store_true',
                   help=tr('backup cleanup: не удалять файл блокировки', 'backup cleanup: keep the lock file'))
    p.add_argument('--mapping', type=Path, default=None,
                   help=tr('backup rebind: JSON-файл соответствия access_id -> secret_ref',
                           'backup rebind: JSON file mapping access_id -> secret_ref'))

    # Management commands.  These are the user path to the modules the engine is
    # built on: an import, a source, a key, a pool, a schedule, a profile, a
    # preset, a backup, the country database state.  Without them those modules
    # would be reachable only from their own tests.
    p.add_argument('--collection', default='', help=tr('import/pool/profile: имя или id своей коллекции; '
                   'по умолчанию публичная база', 'import/pool/profile: name or id of your collection; '
                   'default is the public base'))
    p.add_argument('--name', default='', help=tr('своё имя объекта: коллекции, ключа, пула, расписания, профиля',
                                                 'your name for a collection, key, pool, schedule or profile'))
    p.add_argument('--permission', action='append', default=[],
                   help=tr('api-key create: право ключа; можно повторять (см. proxy-workbench api-key create --help)',
                           'api-key create: one permission of the key; may be repeated'))
    p.add_argument('--scope-collection', action='append', default=[],
                   help=tr('api-key create: коллекция в области видимости ключа',
                           'api-key create: a collection in the key scope'))
    p.add_argument('--key-id', default='', help=tr('api-key rotate/revoke/disable/enable/delete: id ключа',
                                                   'api-key rotate/revoke/disable/enable/delete: the key id'))
    p.add_argument('--admin-token', default=os.environ.get(ADMIN_KEY_ENV) or None,
                   help=tr('административный ключ для команд api-key/pool/schedule; '
                        'или переменная ' + ADMIN_KEY_ENV,
                        'administrator key for the api-key/pool/schedule commands; '
                        'or the ' + ADMIN_KEY_ENV + ' variable'))
    p.add_argument('--grace', type=float, default=0.0,
                   help=tr('api-key rotate: секунд, когда старый секрет ещё работает', 'api-key rotate: seconds the old secret still works'))
    p.add_argument('--ttl-days', dest='ttl_days', type=float, default=None,
                   help=tr('api-key create: срок жизни ключа, дней; 0 — без срока', 'api-key create: key lifetime in days; 0 = no expiry'))
    p.add_argument('--profile-id', dest='profile_id', default='',
                   help=tr('pool/profile: профиль проверки; по умолчанию активный', 'pool/profile: check profile; default is the active one'))
    p.add_argument('--profile-revision', dest='profile_revision', type=int, default=1,
                   help=tr('pool: ревизия профиля проверки', 'pool: check profile revision'))
    p.add_argument('--desired', type=int, default=0, help=tr('pool create: сколько прокси держать', 'pool create: how many proxies to keep'))
    p.add_argument('--reserve', type=int, default=0, help=tr('pool create: сколько держать в резерве', 'pool create: how many to keep in reserve'))
    p.add_argument('--minimum', type=int, default=0, help=tr('pool create: минимум, ниже которого пул пуст', 'pool create: the minimum below which the pool is empty'))
    p.add_argument('--interval', type=float, default=60.0, help=tr('schedule add: интервал запуска, минут', 'schedule add: interval in minutes'))
    p.add_argument('--timezone', default='UTC', help=tr('schedule add: часовой пояс окна', 'schedule add: timezone of the window'))
    p.add_argument('--window', default='', help=tr('schedule add: окно запуска ЧЧ:ММ-ЧЧ:ММ в --timezone', 'schedule add: run window HH:MM-HH:MM in --timezone'))
    p.add_argument('--budget-requests', type=int, default=None, help=tr('schedule add: потолок запросов в период', 'schedule add: request ceiling per period'))
    p.add_argument('--budget-bytes', type=int, default=None, help=tr('schedule add: потолок байтов в период', 'schedule add: byte ceiling per period'))
    p.add_argument('--json', action='store_true', help=tr('вывести результат как JSON', 'print the result as JSON'))
    p.add_argument('--limit', type=int, default=0, help=tr('сколько строк показать; 0 — все', 'how many rows to show; 0 = all'))
    p.add_argument('--import-format', default='', choices=('', 'auto', 'txt', 'uri', 'csv', 'json', 'clash', 'singbox'),
                   help=tr('import: формат; clash и singbox читаются через адаптер подписки',
                           'import: format; clash and singbox are read through the subscription adapter'))
    p.add_argument('--merge', choices=['merge', 'replace'], default='merge',
                   help=tr('import: как применить список к выбранной коллекции', 'import: how to apply the list to the chosen collection'))
    p.add_argument('--allow-partial', action='store_true',
                   help=tr('import: принять список, часть строк которого отклонена', 'import: accept a list where some rows were rejected'))
    p.add_argument('--allow-private-endpoints', action='store_true',
                   help=tr('collect/import: ваш локальный список может называть имя хоста или непубличный адрес; '
                        'удалённые источники остаются строго публичными, а логин и пароль в URL не принимаются никогда',
                        'collect/import: your local list may name a hostname or a non-public address; remote '
                        'sources stay public-only, and credentials in a URL are never accepted'))
    p.add_argument('--commit', action='store_true', help=tr('import: применить предпросмотр', 'import: commit the preview'))
    p.add_argument('--count-what', choices=['endpoint', 'ip', 'exit'], default='endpoint',
                   help=tr('что считает --want: адреса, IP или подтверждённые выходные IP',
                           'what --want counts: endpoints, IPs or confirmed exit IPs'))
    p.add_argument('--deadline', type=float, default=None,
                   help=tr('общий срок задания, секунд; 0 — без общего срока', 'whole-run deadline in seconds; 0 = no global deadline'))
    p.add_argument('--max-requests', type=int, default=None,
                   help=tr('бюджет запросов на задание; 0 — без потолка', 'request budget for the run; 0 = no ceiling'))
    p.add_argument('--run-max-bytes', dest='run_max_bytes', type=int, default=None,
                   help=tr('бюджет байтов на задание; 0 — без потолка', 'byte budget for the run; 0 = no ceiling'))
    p.add_argument('--max-per-host', dest='max_per_host', type=int, default=1,
                   help=tr('одновременных проверок одного адреса узла; 1 — не долбить одну машину',
                           'simultaneous measurements of one host; 1 = do not hammer one machine'))
    p.add_argument('--min-host-interval', dest='min_host_interval', type=float, default=0.0,
                   help=tr('минимальная пауза между двумя проверками одного узла, секунд',
                           'minimum pause between two measurements of one host, seconds'))
    p.add_argument('--judge-concurrency', dest='judge_concurrency', type=int, default=2,
                   help=tr('одновременных обращений к judge и замеров скорости; это чужая служба',
                           'simultaneous judge requests and speed tests; it is somebody else’s service'))
    p.add_argument('--bench-items', type=int, default=2000,
                   help=tr('bench: адресов в синтетической фикстуре', 'bench: addresses in the synthetic fixture'))
    p.add_argument('--bench-n', dest='bench_n', type=int, default=20,
                   help=tr('bench: какого N достигать в замере', 'bench: which N to reach in the benchmark'))
    p.add_argument('--text', action='append', default=[],
                   help=tr('значение для проверки, например source redact URL; можно повторять',
                           'value to inspect, e.g. source redact URL; may be repeated'))
    p.add_argument('--include-secret', action='store_true',
                   help=tr('source redact: показать искомую строку, если она утёкла; по умолчанию только факт',
                           'source redact: show the searched string if it leaked; by default only the fact'))
    # source catalog (F13).  `--sources` stays the flat URL list and still wins;
    # these are for the catalog half, which addresses a source by its stable id.
    p.add_argument('--id', default='', dest='id',
                   help=tr('source: идентификатор источника в каталоге', 'source: catalog source id'))
    p.add_argument('--set-id', dest='set_id', default='',
                   help=tr('source set: набор каталога', 'source set: catalog set id'))
    p.add_argument('--source-url', dest='source_url', default='',
                   help=tr('source add: URL своего списка', 'source add: URL of your own list'))
    p.add_argument('--path', type=Path, default=None,
                   help=tr('source update: файл нового каталога', 'source update: file of the new catalog'))
    p.add_argument('--shared', action='store_true',
                   help=tr('source exclude-scope: исключить и общие адреса, а не только уникальные',
                           'source exclude-scope: exclude shared addresses too, not only exclusive ones'))
    p.add_argument('--query', dest='query', default='',
                   help=tr('source list: фильтр каталога (имя, набор, категория, формат, состояние)',
                           'source list: catalog filter (name, set, category, format, state)'))
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
    # `args.api_token` is deliberately not read for the gateway: the password is a
    # separate identity (CONTRACTS §5.1, defect 18).  On a LAN bind the gateway makes
    # its own and prints it, so a password a phone was given is never a control
    # secret and a leaked API token is never a working proxy.
    gateway_token = getattr(args, 'gateway_token', None)
    if gateway_token and args.api_token and gateway_token == args.api_token:
        print(tr('Пароль шлюза не должен совпадать с токеном API: задайте --gateway-token.',
                 'the gateway password must not equal the API token: set --gateway-token'),
              file=sys.stderr)
        return 2

    async def run():
        server = await gateway.start(args.data, args.host, port, gateway_token, filters, args.rotate,
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


#: Commands that talk to the modules through :class:`Workbench` instead of
#: driving a scan.  They run under the same data lock, on the same database.
MANAGEMENT_COMMANDS = ('import', 'source', 'api-key', 'pool', 'schedule', 'profile',
                       'preset', 'backup', 'geo', 'diagnose')


def bench_chain(args):
    """Measure the chain on its own fixture and print what it measured.

    This is the only place a user can see the chain's own numbers, and it is a
    local synthetic run: documentation addresses, a stub measurement, no socket
    and no DNS.  The notice travels with every number, because a throughput
    figure without it reads as a claim about live public proxies.
    """
    from . import pipeline as chain
    try:
        report = chain.benchmark(items=max(1, int(args.bench_items)), find_n=max(0, int(args.bench_n)),
                                 what=args.count_what).to_public()
    except chain.PipelineError as exc:
        print(tr(f'Ошибка замера: {exc}', f'Benchmark failed: {exc}'), file=sys.stderr)
        return 2
    emit(args, report)
    if not args.json:
        print(tr(f'Фикстура {report["fixture"]} (digest {report["digest"]}), адресов {report["items"]}, '
                 f'состояние {report["state"]}, {report["measured"]} измерений, '
                 f'{report["items_per_s"]:.1f} в секунду, первый результат {report["time_to_first_s"]:.3f} с, '
                 f'до N {report["time_to_n_s"] if report["time_to_n_s"] is not None else "—"} с.',
                 f'Fixture {report["fixture"]} (digest {report["digest"]}), {report["items"]} addresses, '
                 f'state {report["state"]}, {report["measured"]} measured, {report["items_per_s"]:.1f}/s, '
                 f'first result {report["time_to_first_s"]:.3f}s, '
                 f'to N {report["time_to_n_s"] if report["time_to_n_s"] is not None else "—"}s.'), flush=True)
        print(report['notice'], file=sys.stderr)
    return 0


def management_command(args):
    """Run one management command and return the process exit code.

    Every branch here is the *user path* to a module: an import, a source, a
    key, a pool, a schedule, a profile, a preset, a backup, the country
    database.  They are grouped here so a command that has no route in the API
    and no button in the GUI still has one honest way in.
    """
    from . import apikeys
    action = (args.items or [''])[0]
    try:
        with Workbench(args.data) as workbench:
            handler = {
                'import': _cmd_import, 'source': _cmd_source, 'api-key': _cmd_api_key,
                'pool': _cmd_pool, 'schedule': _cmd_schedule, 'profile': _cmd_profile,
                'preset': _cmd_preset, 'backup': _cmd_backup, 'geo': _cmd_geo,
                'diagnose': _cmd_diagnose,
            }[args.command]
            return handler(workbench, args, action)
    except WorkbenchError as exc:
        print(tr(f'Ошибка: {exc}', f'Error: {exc}'), file=sys.stderr)
        _print_code(exc.code)
        return 2
    except apikeys.ApiKeyError as exc:
        print(tr(f'Ошибка ключа: {exc}', f'Key error: {exc}'), file=sys.stderr)
        _print_code(getattr(exc, 'code', None))
        return 2
    except (schema.DbError, scheduler_error(), secrets_error()) as exc:
        print(tr(f'Ошибка базы данных: {exc}', f'Database error: {exc}'), file=sys.stderr)
        _print_code(getattr(exc, 'code', None))
        return 2
    except importer_error() as exc:
        # Every refusal of the importer is a subclass of ``ImportProblem``, and
        # each one names its own ``E_*`` code (CONTRACTS §5.4).  Enumerating a
        # few of them here is how a ``ImportPartialBlocked`` or a
        # ``ImportBusy`` escaped as a traceback instead of a message.
        print(tr(f'Ошибка импорта: {exc}', f'Import error: {exc}'), file=sys.stderr)
        _print_code(getattr(exc, 'code', None))
        return 2
    except profiles_error() as exc:
        print(tr(f'Ошибка профиля: {exc}', f'Profile error: {exc}'), file=sys.stderr)
        _print_code(getattr(exc, 'code', None))
        return 2
    except pools_error() as exc:
        print(tr(f'Ошибка пула: {exc}', f'Pool error: {exc}'), file=sys.stderr)
        _print_code(getattr(exc, 'code', None))
        return 2


def _print_code(code):
    """Print the stable ``E_*`` code of a refusal under its message.

    The code is machine-readable and the sentence is localizable, so a script
    can branch on the first line and a person reads the second.
    """
    if isinstance(code, str) and code:
        print(code, file=sys.stderr)


def scheduler_error():
    from . import scheduler
    return scheduler.ScheduleError


def secrets_error():
    from . import secrets as secretstore
    return secretstore.SecretError


def importer_error():
    from . import importer
    return importer.ImportProblem


def profiles_error():
    from . import profiles
    return profiles.ProfileError


def pools_error():
    from . import pools
    return pools.PoolError


def emit(args, value, text=None):
    """Print a management result as JSON or as a human line."""
    if args.json or text is None:
        print(json.dumps(value, ensure_ascii=False, indent=1, default=str))
    else:
        print(text)
    return 0


def _resolve_collection(workbench, name, create=False):
    """A collection by id or by name; the public base when nothing was asked.

    With ``create`` a name that does not exist yet becomes a *private*
    collection, created empty.  It stays empty on purpose: the public base is
    not copied into it, because a personal list that silently starts as the
    public list is defect 11.
    """
    if not name:
        return ensure_public_collection(workbench.conn)
    row = workbench.conn.execute(
        'SELECT id FROM collections WHERE id=? OR name=? AND archived_at IS NULL',
        (name, name)).fetchone()
    if row is None:
        row = workbench.conn.execute(
            'SELECT id FROM collections WHERE name=? AND archived_at IS NULL', (name,)).fetchone()
    if row is None:
        if not create:
            raise WorkbenchError(tr(f'Коллекция не найдена: {name}', f'collection not found: {name}'))
        from . import importer
        return importer.create_collection(workbench.conn, name, kind='private')
    return row['id'] if isinstance(row, sqlite3.Row) else row[0]


def _import_policy(args):
    """The endpoint policy of an import, from the command line.

    The public base takes globally routable addresses only.  A private
    collection may name a hostname or a private address, but only because the
    user said so with ``--allow-private-endpoints`` -- an explicit, recorded
    choice rather than a form that accepts what the collector later drops
    (F04, defect 10).  Credentials are refused either way: the importer refuses
    them structurally, and no flag turns that off.
    """
    from . import importer
    if not getattr(args, 'allow_private_endpoints', False):
        return importer.DEFAULT_POLICY
    return importer.EndpointPolicy(public_only=False)


def _cmd_import(workbench, args, action):
    """``import preview|commit|list`` -- the transactional importer (F03)."""
    if action == 'list':
        rows = workbench.conn.execute(
            'SELECT id, collection_id, state, revision, created_at FROM import_batch '
            'ORDER BY created_at DESC LIMIT ?', (args.limit or 20,)).fetchall()
        return emit(args, [dict(row) for row in rows],
                    '\n'.join(f"{row['id']} {row['state']} {row['collection_id']} rev={row['revision']}"
                              for row in rows) or tr('Импортов пока нет.', 'no imports yet'))
    if not args.input:
        raise WorkbenchError(tr('Укажите файл: --input ФАЙЛ', 'give a file: --input FILE'))
    path = Path(args.input[0])
    if not path.is_file():
        raise WorkbenchError(tr(f'Файл не найден: {path}', f'file not found: {path}'))
    # ``--collection NAME`` is how a user names their own list, so a name that
    # does not exist yet becomes an empty private collection instead of a
    # refusal that leaves them with no CLI way to start one.
    policy = _import_policy(args)
    collection_id = _resolve_collection(workbench, args.collection,
                                        create=bool(args.collection) and not policy.public_only)
    fmt = args.import_format or None
    if fmt in ('clash', 'singbox'):
        # A proxy-client document is parsed by the subscription adapter, and
        # its endpoints go into the same membership table as a flat list.
        document = path.read_text(encoding='utf-8', errors='strict')
        result = workbench.import_client_config(document, fmt)
        added = workbench.add_members(collection_id, [item.endpoint for item in result.endpoints])
        return emit(args, {'outcome': result.outcome, 'collection_id': collection_id,
                           'added': added, 'rejected': [str(item) for item in result.rejected],
                           'ignored': list(result.ignored)},
                    tr(f'Формат {fmt}: принято {added}, отклонено {len(result.rejected)}; коллекция {collection_id}',
                       f'format {fmt}: {added} accepted, {len(result.rejected)} rejected; collection {collection_id}'))
    source = workbench.import_source(path)
    plan = workbench.import_preview(source, collection_id, mode=args.merge, fmt=fmt, policy=policy)
    if action == 'preview':
        return emit(args, plan.to_dict(),
                    tr(f'Предпросмотр: принято {len(plan.valid)}, дубликатов {len(plan.duplicates)}, '
                       f'отклонено {len(plan.rejected)}; ничего не записано',
                       f'preview: {len(plan.valid)} valid, {len(plan.duplicates)} duplicates, '
                       f'{len(plan.rejected)} rejected; nothing was written'))
    if plan.needs_mapping:
        raise WorkbenchError(tr('Файл требует сопоставления колонок; сначала preview.',
                                'the file needs a column mapping; run preview first'),
                             'E_VALIDATION_SCHEMA')
    report = workbench.import_commit(plan, allow_partial=args.allow_partial,
                                     busy=_busy_collection(workbench, collection_id))
    return emit(args, report.to_dict(),
                tr(f'Импорт {report.state}: добавлено {report.counts.get("added", 0)}, '
                   f'удалено {report.counts.get("removed", 0)}; коллекция {collection_id}',
                   f'import {report.state}: {report.counts.get("added", 0)} added, '
                   f'{report.counts.get("removed", 0)} removed; collection {collection_id}'))


def _busy_collection(workbench, collection_id):
    """Why a collection cannot take new members right now, or ``None``."""
    store = workbench.jobs()
    running = [job for job in store.jobs()
               if job.state in ('queued', 'running', 'paused')
               and getattr(job.scope, 'collection_id', '') == collection_id]
    if not running:
        return None
    return f'collection {collection_id} is being checked by job {running[0].id}'


#: Everything ``source`` can do.  The catalog half (F13) and the redaction half
#: (F27) answer different questions about the same objects, so they share one
#: command instead of two that disagree about what a source is.
SOURCE_SUBCOMMANDS = ('list', 'show', 'sets', 'set', 'enable', 'disable', 'add', 'remove',
                      'check', 'update', 'recover', 'status', 'exclude-scope',
                      'health', 'redact')


def _source_flag(args, *names, default=None):
    for name in names:
        value = getattr(args, name, None)
        if value:
            return value
    return default


def _source_text(args, index=1):
    items = list(getattr(args, 'items', None) or ())
    if len(items) > index:
        return items[index]
    text = list(getattr(args, 'text', None) or ())
    return text[0] if text else None


def _cmd_source(workbench, args, action):
    """``source <subcommand>`` -- the source catalog and the subscriptions (F13, F27).

    The catalog half reads and writes the *selection* (``source_catalog``), the
    runtime half reports what the last collection actually did
    (``source_management`` over the tables ``collect`` wrote), and the
    redaction half is F27's.  All three are the same object seen from
    different sides, so they live behind one command.
    """
    from . import source_catalog, source_management, sourcedesk
    if action not in SOURCE_SUBCOMMANDS:
        raise WorkbenchError(tr(f'Неизвестное действие source: {action}. Возможны: '
                                + ', '.join(SOURCE_SUBCOMMANDS),
                                f'unknown source action: {action}. Available: '
                                + ', '.join(SOURCE_SUBCOMMANDS)), 'E_VALIDATION_FIELD')
    data = workbench.data
    catalog = sources_catalog(data)
    settings = source_catalog_for(data)

    if action == 'redact':
        # ``--text`` is the option form because argparse keeps a positional run
        # only while the options do not interrupt it; both forms are accepted.
        values = list(args.text) + list(args.items[1:])
        redacted = [sourcedesk.redact_url(value) for value in values if value]
        leaks = sourcedesk.find_secret_leaks(redacted, values) if redacted else []
        return emit(args, {'redacted': redacted, 'leaks': list(leaks)},
                    '\n'.join(redacted) or tr('Нечего редактировать.', 'nothing to redact'))

    if action == 'list':
        view = source_selection_view(data, catalog, query=args.query or '',
                                     db=workbench.conn, now=workbench.clock())
        rows = view.get('sources') or []
        limit = int(args.limit or 0) or len(rows)
        rows = rows[:limit]
        view = dict(view, shown=len(rows))
        return emit(args, view, '\n'.join(
            f"{item['id']:<12} {item.get('support', '?'):<14} {item.get('state', '?'):<14}"
            f" {item.get('name', '')}" for item in rows)
            or tr('Каталог пуст.', 'the catalog is empty'))

    if action == 'show':
        source_id = _source_flag(args, 'name') or _source_text(args)
        if not source_id:
            raise WorkbenchError(tr('Укажите источник: source show ИД', 'name a source: source show ID'))
        detail = source_management.detail_view(catalog, settings, source_id,
                                               runtime=source_management.runtime_snapshot(
                                                   workbench.conn, now=workbench.clock(),
                                                   source_ids=[source_id]),
                                               db=workbench.conn, now=workbench.clock())
        if detail is None:
            raise WorkbenchError(tr(f'Источник не найден: {source_id}', f'source not found: {source_id}'),
                                 'E_STATE_NOT_FOUND')
        summary = '\n'.join(f"{key}: {_as_json(detail.get(key))}"
                            for key in ('id', 'name', 'support', 'dataset_group', 'access',
                                        'format', 'state', 'runtime')
                            if detail.get(key) is not None)
        return emit(args, detail, summary)

    if action == 'sets':
        settings = source_catalog.migrate_settings(settings, catalog)
        sets = source_management.sets_view(catalog, settings)
        return emit(args, sets, '\n'.join(
            f"{item['id']:<16} {item['members']:>4} {item.get('name', '')}" for item in sets))

    if action in ('set', 'enable', 'disable', 'add', 'remove'):
        settings = source_catalog.migrate_settings(settings, catalog)
        changed = {}
        if action == 'set':
            set_id = _source_flag(args, 'set_id', 'name') or _source_text(args)
            if not set_id:
                raise WorkbenchError(tr('Укажите набор: source set ИМЯ',
                                        'name a set: source set NAME'), 'E_VALIDATION_FIELD')
            try:
                changed = source_management.apply_set(settings, set_id, catalog)
            except ValueError as exc:
                raise WorkbenchError(str(exc), 'E_VALIDATION_FIELD') from None
        elif action in ('enable', 'disable'):
            ids = _source_ids_argument(args)
            if not ids:
                raise WorkbenchError(tr('Укажите источник: source enable|disable ИД',
                                        'name sources: source enable|disable ID'), 'E_VALIDATION_FIELD')
            changed = source_management.set_downloads(settings, ids, action == 'disable', catalog)
        elif action == 'add':
            url = _source_flag(args, 'source_url', 'url') or _source_text(args)
            if not url:
                raise WorkbenchError(tr('Укажите URL: source add URL', 'give a URL: source add URL'),
                                     'E_VALIDATION_FIELD')
            try:
                custom = source_catalog.custom_source(url)
                selection = settings.setdefault('source_selection', {})
                selection.setdefault('custom_sources', []).append(custom)
                selection.setdefault('selected_ids', []).append(custom['id'])
                changed = source_management.select_ids(settings, [custom['id']], catalog)
            except ValueError as exc:
                raise WorkbenchError(str(exc), 'E_VALIDATION_FIELD') from None
        else:
            ids = _source_ids_argument(args)
            if not ids:
                raise WorkbenchError(tr('Укажите источник: source remove ИД',
                                        'name sources: source remove ID'), 'E_VALIDATION_FIELD')
            changed = source_management.remove_sources(settings, ids, catalog)
        write_source_settings(data, changed)
        selection = changed.get('source_selection', {}) if isinstance(changed, dict) else {}
        return emit(args, {'selected': list(selection.get('selected_ids', ())),
                           'disabled': list(selection.get('download_disabled_ids', ())),
                           'custom': [item.get('id') for item in selection.get('custom_sources', ())]},
                    tr(f'Выбрано {len(selection.get("selected_ids", ()))} источников, '
                       f'выключено {len(selection.get("download_disabled_ids", ()))}',
                       f'{len(selection.get("selected_ids", ()))} sources selected, '
                       f'{len(selection.get("download_disabled_ids", ()))} disabled'))

    if action == 'check':
        source_id = _source_flag(args, 'name') or _source_text(args)
        if not source_id:
            raise WorkbenchError(tr('Укажите источник: source check ИД', 'name a source: source check ID'),
                                 'E_VALIDATION_FIELD')
        plan = source_plan_of(source_catalog.source_by_id(catalog, source_id), source_id=source_id)
        if plan is None:
            raise WorkbenchError(tr(f'Источник недоступен для сбора: {source_id}',
                                    f'source is not collectable: {source_id}'), 'E_STATE_NOT_FOUND')
        return emit(args, plan.as_dict(),
                    tr(f'{plan.source_id}: {plan.kind}, {plan.url}',
                       f'{plan.source_id}: {plan.kind}, {plan.url}'))

    if action == 'update':
        incoming = Path(args.path) if args.path else None
        if incoming is None:
            raise WorkbenchError(tr('Укажите файл каталога: source update ФАЙЛ',
                                    'give a catalog file: source update FILE'), 'E_VALIDATION_FIELD')
        body = incoming.read_bytes()
        accepted = source_catalog.decode_catalog_bytes(body, current=catalog)
        verdict = source_catalog.accept_catalog(catalog, accepted)
        if not verdict.get('accepted'):
            return emit(args, verdict, tr('Каталог не принят.', 'the catalog was not accepted'))
        target = Path(data) / 'source-catalog.json'
        atomic(target, body)
        return emit(args, dict(verdict, path=str(target)),
                    tr(f'Каталог обновлён: ревизия {verdict.get("revision", "?")}',
                       f'catalog updated: revision {verdict.get("revision", "?")}'))

    if action == 'recover':
        source_id = _source_flag(args, 'name') or _source_text(args)
        if not source_id:
            raise WorkbenchError(tr('Укажите источник: source recover ИД',
                                    'name a source: source recover ID'), 'E_VALIDATION_FIELD')
        plan = source_plan_of(source_catalog.source_by_id(catalog, source_id), source_id=source_id)
        if plan is None:
            return emit(args, {'source_id': source_id, 'recovered': False, 'cleared': 0,
                               'reason': 'источник не в каталоге или недоступен для сбора'},
                        tr(f'Восстанавливать нечего: {source_id}', f'nothing to recover: {source_id}'))
        state = _source_state_row(workbench.conn, source_id)
        workbench.conn.execute('UPDATE source_state SET consecutive_failures=0, backoff_until=NULL,'
                               ' quarantine_until=NULL, last_error=NULL WHERE source_id=? AND endpoint_id=?',
                               (source_id, ''))
        workbench.conn.commit()
        return emit(args, {'source_id': source_id, 'recovered': True,
                           'quarantine_until': (state or {}).get('quarantine_until')},
                    tr(f'Карантин снят: {source_id}', f'quarantine cleared: {source_id}'))

    if action == 'status':
        from . import source_management as sm
        snapshot = sm.runtime_snapshot(workbench.conn, now=workbench.clock())
        try:
            from . import proxytool as self_module
            sm.attach_contributions(snapshot, workbench.conn,
                                    [key for key in snapshot if key])
        except Exception:  # noqa: BLE001 - contributions are a nicety, not the state
            pass
        rows = [{'source_id': key, **_as_json(value)} for key, value in sorted(snapshot.items())]
        return emit(args, {'sources': rows, 'count': len(rows)},
                    '\n'.join(f"{item['source_id']}: {_as_json(item.get('http_state'))} "
                              f"принято {item.get('accepted', 0)}" for item in rows)
                    or tr('Источники ещё не собирались.', 'no sources collected yet'))

    if action == 'exclude-scope':
        from . import db as store
        source_id = _source_flag(args, 'name') or _source_text(args)
        if not source_id:
            raise WorkbenchError(tr('Укажите источник: source exclude-scope ИД',
                                    'name a source: source exclude-scope ID'), 'E_VALIDATION_FIELD')
        include_shared = bool(_source_flag(args, 'shared', default=False))
        candidates = source_management.source_addresses(
            workbench.conn, source_id, exclusive=not include_shared)
        rows = store.scope_exclusions(workbench.conn, now=workbench.clock())
        already = {item['proxy'] for item in rows}
        added = 0
        for value in candidates:
            if value in already:
                continue
            store.add_scope_exclusion(workbench.conn, value, source_id=source_id,
                                      now=workbench.clock(),
                                      reason='source_scope', shared=not include_shared)
            added += 1
        workbench.conn.commit()
        return emit(args, {'source_id': source_id, 'delivered': len(candidates),
                           'excluded': added, 'already_excluded': len(candidates) - added,
                           'shared': include_shared},
                    tr(f'Исключено {added} адресов источника {source_id}',
                       f'excluded {added} addresses of source {source_id}'))

    if action == 'health':
        source_id = _source_flag(args, 'name') or _source_text(args)
        if not source_id:
            raise WorkbenchError(tr('Укажите источник: source health ИД', 'name a source: source health ID'))
        rows = workbench.conn.execute(
            'SELECT count(*) FROM candidate_seen WHERE source=?', (source_id,)).fetchone()[0]
        state = sourcedesk.FeedState(source_id=source_id, collection_id=ensure_public_collection(workbench.conn),
                                     active=(), last_attempt_at=rows and workbench.clock() or None)
        diagnostics = sourcedesk.feed_diagnostics(state, now=workbench.clock())
        return emit(args, [{'code': item.code, 'detail': item.detail} for item in diagnostics],
                    '\n'.join(f"{item.code}: {item.detail}" for item in diagnostics)
                    or tr('Диагностик нет.', 'no diagnostics'))
    raise WorkbenchError(tr(f'Неизвестное действие source: {action}',
                            f'unknown source action: {action}'), 'E_VALIDATION_FIELD')


def _source_ids_argument(args):
    """The source ids of one subcommand, in every form the parser accepts."""
    values = []
    raw = _source_flag(args, 'id', 'name', 'ids', 'set_id')
    if raw:
        values.extend(part.strip() for part in str(raw).replace(',', ' ').split() if part.strip())
    for extra in (getattr(args, 'items', None) or ())[1:]:
        values.extend(part.strip() for part in str(extra).replace(',', ' ').split() if part.strip())
    return list(dict.fromkeys(values))


def _cmd_api_key(workbench, args, action):
    """``api-key bootstrap|list|create|rotate|revoke|...`` -- F29, the user path.

    ``bootstrap`` is the only way an administrator comes into existence and it
    needs a locally trusted caller, so it prints the secret exactly once and
    never stores it.
    """
    from . import apikeys
    manager = workbench.keys()
    if action in ('bootstrap', 'init'):
        # This is the one branch that needs no key: the caller is the local,
        # file-locked process the user started.  It is therefore also the only
        # branch that must not be able to run twice by accident.
        admins = existing_admin_keys(workbench)
        if admins and not args.yes:
            return emit(args, {'already': [item['name'] for item in admins]},
                        tr(f'Административный ключ уже есть: {admins[0]["name"]}. '
                           'Новый выдаст --yes.', f'an administrator key already exists: {admins[0]["name"]}. '
                           'Use --yes for another one.'))
        issued = manager.bootstrap_admin(local_trusted=True, name=args.name or 'administrator',
                                         permissions=local_admin_permissions(),
                                         ttl_s=(args.ttl_days * 86400) if args.ttl_days else None)
        return emit(args, {'id': issued.info.id, 'prefix': issued.info.prefix, 'secret': issued.secret},
                    tr(f'Ключ {issued.info.id} ({issued.info.prefix}). СЕКРЕТ ПОКАЗЫВАЕТСЯ ОДИН РАЗ:\n{issued.secret}',
                       f'key {issued.info.id} ({issued.info.prefix}). THE SECRET IS SHOWN ONCE:\n{issued.secret}'))
    if action in ('', 'list'):
        rows = [item.as_dict() for item in manager.list_keys(actor=_admin_actor(workbench, args),
                                                             limit=args.limit or 500)]
        return emit(args, rows, '\n'.join(
            f"{item['id']} {item['name']} {item['state']} {','.join(sorted(item.get('permissions') or ()))[:60]}"
            for item in rows) or tr('Ключей нет.', 'no keys'))
    if action == 'create':
        permissions = tuple(args.permission) or apikeys.READ_PERMISSIONS
        unknown = sorted(set(permissions) - set(apikeys.PERMISSIONS))
        if unknown:
            raise WorkbenchError(tr(f'Неизвестные права: {", ".join(unknown)}',
                                    f'unknown permissions: {", ".join(unknown)}'), 'E_VALIDATION_FIELD')
        scope = {'collections': list(args.scope_collection)} if args.scope_collection else None
        issued = manager.create(actor=_admin_actor(workbench, args), name=args.name or 'key',
                                permissions=permissions, scope=scope,
                                ttl_s=(args.ttl_days * 86400) if args.ttl_days else None)
        return emit(args, {'id': issued.info.id, 'prefix': issued.info.prefix, 'secret': issued.secret},
                    tr(f'Ключ {issued.info.id} ({issued.info.prefix}). СЕКРЕТ ПОКАЗЫВАЕТСЯ ОДИН РАЗ:\n{issued.secret}',
                       f'key {issued.info.id} ({issued.info.prefix}). THE SECRET IS SHOWN ONCE:\n{issued.secret}'))
    key_id = args.key_id or ((args.items or ['', ''])[1] if len(args.items) > 1 else '')
    if not key_id:
        raise WorkbenchError(tr('Укажите ключ: --key-id ИД', 'name the key: --key-id ID'))
    actor = _admin_actor(workbench, args)
    if action == 'rotate':
        issued = manager.rotate(key_id, actor=actor, grace_s=max(0.0, args.grace))
        return emit(args, {'id': issued.info.id, 'prefix': issued.info.prefix, 'secret': issued.secret},
                    tr(f'Новый секрет ключа {key_id} (старый живёт {args.grace:g} с):\n{issued.secret}',
                       f'new secret of key {key_id} (old one lives {args.grace:g} s):\n{issued.secret}'))
    simple = {'revoke': manager.revoke, 'disable': manager.disable,
              'enable': manager.enable, 'delete': manager.delete}
    if action in simple:
        info = simple[action](key_id, actor=actor)
        return emit(args, info.as_dict() if hasattr(info, 'as_dict') else {'id': key_id, 'state': action})
    raise WorkbenchError(tr(f'Неизвестное действие api-key: {action}',
                            f'unknown api-key action: {action}'))


def existing_admin_keys(workbench):
    """Active keys that already hold ``admin.keys``, read without a key.

    The bootstrap branch is the only command that acts without a credential,
    so it has to be able to see whether one already exists -- otherwise every
    invocation would mint another administrator.
    """
    from . import apikeys
    found = []
    for row in workbench.conn.execute(
            'SELECT id, name, permissions_json, revoked_at, disabled_at, expires_at FROM api_keys').fetchall():
        if row['revoked_at'] is not None or row['disabled_at'] is not None:
            continue
        if row['expires_at'] is not None and row['expires_at'] <= workbench.clock():
            continue
        try:
            permissions = set(json.loads(row['permissions_json'] or '[]'))
        except (TypeError, ValueError):
            continue
        if 'admin.keys' in permissions:
            found.append({'id': row['id'], 'name': row['name']})
    return found


#: Where the management commands read the administrator secret from.  An
#: environment variable and not ``argv``: a secret in the command line is visible
#: to every process on the machine and lands in the shell history.
ADMIN_KEY_ENV = 'PROXY_WORKBENCH_ADMIN_KEY'


def _admin_actor(workbench, args):
    """The administrator identity the management commands act as.

    Every privileged branch needs a *real* key: ``apikeys`` issues keys, and a
    command that could act without one would be a second way around the
    permission checks.  ``api-key bootstrap`` is the only branch that mints one
    without a key, and it does so because it is locally trusted.
    """
    from . import apikeys
    secret = os.environ.get(ADMIN_KEY_ENV) or args.admin_token or ''
    if not secret:
        raise WorkbenchError(tr(
            f'Нужен административный ключ: задайте {ADMIN_KEY_ENV} или выполните api-key bootstrap',
            f'an administrator key is required: set {ADMIN_KEY_ENV} or run api-key bootstrap'),
            'E_AUTH_PERMISSION')
    try:
        return workbench.keys().authenticate(secret, permission='admin.keys')
    except apikeys.ApiKeyError as exc:
        raise WorkbenchError(tr(f'Административный ключ не принят: {exc}',
                                f'the administrator key was refused: {exc}'), 'E_AUTH_PERMISSION') from None


def active_profile_id(workbench):
    """The profile of the published snapshot, or ``None`` if nothing was run."""
    try:
        text = (workbench.data / 'last-profile.txt').read_text(encoding='utf-8').strip()
    except (OSError, UnicodeError):
        return None
    if not text:
        return None
    row = workbench.conn.execute('SELECT 1 FROM profiles WHERE id=?', (text,)).fetchone()
    return text if row else None


#: Every ``pool`` subcommand.  ``refill`` and ``recheck`` were reachable from
#: ``/v1`` and the GUI while the CLI could only look at a pool, which is the
#: same CLI/API gap F18 asks to close.
POOL_SUBCOMMANDS = ('list', 'create', 'status', 'members', 'refill', 'recheck')


def _cmd_pool(workbench, args, action):
    """``pool list|create|status|members|refill|recheck`` -- a steady pool of proxies (F14).

    ``refill`` and ``recheck`` are the two halves of keeping a pool up: the
    first re-admits from what is already measured, the second measures the
    pool's own collection again.  Both were reachable from ``/v1`` and from the
    GUI while the CLI could only look at the pool, which is the same CLI/API gap
    F18 asks to close.
    """
    from . import pools
    if action not in POOL_SUBCOMMANDS:
        raise WorkbenchError(
            tr(f'Неизвестное действие pool: {action}. Возможны: ' + ', '.join(POOL_SUBCOMMANDS),
               f'unknown pool action: {action}. Available: ' + ', '.join(POOL_SUBCOMMANDS)),
            'E_VALIDATION_FIELD')
    store = workbench.pools()
    if action in ('', 'list'):
        rows = []
        for spec in store.list():
            status = store.status(spec.id)
            rows.append({'id': spec.id, 'collection_id': spec.collection_id,
                         'profile_id': spec.profile_id, 'desired': spec.desired,
                         'state': status.state, 'served': status.served,
                         'count_unit': status.count_unit,
                         'ready_for_clients': status.ready_for_clients,
                         'deficit_reason': status.deficit_reason})
        return emit(args, rows, '\n'.join(
            f"{row['id']} {row['state']} {row['served']}/{row['desired']} {row['count_unit']}"
            f" {row['deficit_reason'] or ''}" for row in rows) or tr('Пулов нет.', 'no pools'))
    pool_id = args.name or ((args.items or ['', ''])[1] if len(args.items) > 1 else '')
    if action == 'create':
        if not pool_id:
            raise WorkbenchError(tr('Укажите имя пула: --name ИМЯ', 'name the pool: --name NAME'))
        # A pool is bound to one scope: a collection *and* a profile revision.
        # Both are required, because a pool that could drift to another profile
        # would serve rows the user never asked for (CONTRACTS §1.2).
        profile_id = args.profile_id or active_profile_id(workbench)
        if not profile_id:
            raise WorkbenchError(tr('Пул привязан к профилю проверки: укажите --profile-id '
                                    'или сначала выполните run',
                                    'a pool is bound to a check profile: give --profile-id '
                                    'or run a check first'), 'E_VALIDATION_FIELD')
        spec = store.create(pool_id, collection_id=_resolve_collection(workbench, args.collection),
                            profile_id=profile_id, profile_revision=args.profile_revision or 1,
                            desired=max(1, args.desired),
                            minimum=max(0, args.minimum), reserve=max(0, args.reserve))
        return emit(args, {'id': spec.id, 'desired': spec.desired},
                    tr(f'Пул {spec.id}: держать {spec.desired}, резерв {spec.reserve}',
                       f'pool {spec.id}: keep {spec.desired}, reserve {spec.reserve}'))
    if not pool_id:
        raise WorkbenchError(tr('Укажите пул: --name ИД', 'name the pool: --name ID'))
    if action == 'status':
        status = store.status(pool_id)
        body = {'id': status.pool_id, 'state': status.state,
                'deficit_reason': status.deficit_reason,
                'deficit_reasons': list(status.deficit_reasons or ()),
                'counts': dict(status.counts or {}), 'desired': status.desired,
                'served': status.served, 'count_unit': status.count_unit,
                'find': dict(status.find or {}),
                'minimum': status.minimum, 'reserve': status.reserve,
                'ready_for_clients': status.ready_for_clients,
                'next_attempt_at': status.next_attempt_at}
        return emit(args, body,
                    tr(f'Пул {status.pool_id}: {status.state} {status.deficit_reason or ""}',
                       f'pool {status.pool_id}: {status.state} {status.deficit_reason or ""}'))
    if action == 'members':
        members = [{'endpoint_id': item.endpoint_id, 'state': item.state,
                    'admitted_at': item.admitted_at} for item in store.members(pool_id)]
        return emit(args, members, '\n'.join(
            f"{item['endpoint_id']} {item['state']}" for item in members)
            or tr('В пуле нет участников.', 'the pool has no members'))
    if action == 'refill':
        # The same call `/v1/pools/{id}/refill` makes, with the same budget
        # semantics: a spent budget is an honest "nothing was admitted", not a
        # second implementation with its own rules.
        from . import api as apisvc
        budget = {'max_requests': int(args.max_requests or 0)} if args.max_requests else None
        report = pools.refill(store, pool_id, apisvc.pool_candidate_source(workbench.conn),
                              budget=budget, now=workbench.clock())
        return emit(args, _as_json(report),
                    tr(f'Пул {pool_id}: обслуживает {report.served}'
                       f' из {report.desired} ({report.state})'
                       + (f' — {report.deficit_reason}' if report.deficit_reason else ''),
                       f'pool {pool_id}: serving {report.served}'
                       f' of {report.desired} ({report.state})'
                       + (f' — {report.deficit_reason}' if report.deficit_reason else '')))
    if action == 'recheck':
        from . import jobs as jobs_module
        spec = store.require(pool_id)
        job_id = submit_scan_job(
            workbench, workbench.conn, 'pool_recheck', profile=spec.profile_id,
            profile_revision=int(spec.profile_revision), collection_id=spec.collection_id,
            candidates=[value for (value,) in collection_candidates(
                workbench.conn, spec.collection_id)],
            filters={'protocol': args.protocol or 'all'},
            budgets={'max_seconds': args.deadline or None},
            idempotency_key=_source_flag(args, 'id') or None)
        report = run_pool_recheck(workbench, pool_id, job_id=job_id,
                                  protocol=args.protocol or 'all',
                                  max_seconds=args.deadline or None)
        state = report.get('state') or {}
        return emit(args, _as_json(report),
                    tr(f'Пул {pool_id}: задание {job_id}, проверено {state.get("checked", 0)}, '
                       f'прошло {state.get("passed", 0)} ({report.get("job_state")})',
                       f'pool {pool_id}: job {job_id}, checked {state.get("checked", 0)}, '
                       f'passed {state.get("passed", 0)} ({report.get("job_state")})'))
    raise WorkbenchError(tr(f'Неизвестное действие pool: {action}',
                            f'unknown pool action: {action}'), 'E_VALIDATION_FIELD')


def parse_window(value):
    """``HH:MM-HH:MM`` as local wall-clock minutes; the module owns the bounds."""
    from . import scheduler
    start, _, end = str(value).partition('-')
    return scheduler.Window(_hhmm(start, 'start'), _hhmm(end, 'end'))


def _hhmm(value, label):
    text = str(value).strip()
    hours, _, minutes = text.partition(':')
    if not hours.isdigit() or not minutes.isdigit():
        raise ValueError(f'{label}: ожидается ЧЧ:ММ, получено {text!r}')
    return int(hours) * 60 + int(minutes)


def _cmd_schedule(workbench, args, action):
    """``schedule list|add|remove|enable|disable|next`` -- F15."""
    from . import scheduler
    engine = scheduler.Scheduler(workbench.schedules(), clock=workbench.clock)
    if action in ('', 'list'):
        rows = [{'id': item.id, 'kind': item.kind, 'enabled': item.enabled,
                 'interval_minutes': item.interval_minutes, 'timezone': item.timezone}
                for item in engine.list()]
        return emit(args, rows, '\n'.join(
            f"{row['id']} {row['kind']} каждые {row['interval_minutes']} мин {'вкл' if row['enabled'] else 'выкл'}"
            for row in rows) or tr('Расписаний нет.', 'no schedules'))
    schedule_id = args.name or ((args.items or ['', ''])[1] if len(args.items) > 1 else '')
    if action == 'add':
        if not schedule_id:
            raise WorkbenchError(tr('Укажите имя расписания: --name ИМЯ', 'name the schedule: --name NAME'))
        windows = ()
        if args.window:
            try:
                windows = (parse_window(args.window),)
            except (ValueError, scheduler.ScheduleError) as exc:
                raise WorkbenchError(tr(f'Окно запуска неверно: {exc}',
                                        f'the run window is invalid: {exc}'), 'E_VALIDATION_FIELD') from None
        # The store owns the serialisation: the schedule is given as the plain
        # mapping its schema defines, never as a runtime object (CONTRACTS §5.5).
        payload = {'id': schedule_id, 'kind': 'interval', 'interval_minutes': args.interval,
                   'timezone': args.timezone,
                   'budgets': {'requests': args.budget_requests, 'bytes': args.budget_bytes}}
        if windows:
            payload['windows'] = [window.to_dict() for window in windows]
        spec = engine.add(payload)
        plan = engine.plan(spec, engine.state(spec.id), workbench.clock())
        return emit(args, {'id': spec.id, 'next_at': plan.next_at, 'timezone': spec.timezone},
                    tr(f'Расписание {spec.id}: следующий запуск в {spec.timezone}',
                       f'schedule {spec.id}: next run in {spec.timezone}'))
    if not schedule_id:
        raise WorkbenchError(tr('Укажите расписание: --name ИД', 'name the schedule: --name ID'))
    if action in ('remove', 'delete'):
        engine.remove(schedule_id)
        return emit(args, {'id': schedule_id, 'removed': True}, tr(f'Расписание {schedule_id} удалено.',
                                                                    f'schedule {schedule_id} removed.'))
    if action in ('enable', 'disable'):
        # ``enabled`` is the stored setting; ``pause``/``resume`` are the runtime
        # state.  Disabling a schedule must survive a restart, so it is written
        # to the spec, not only to the in-memory state.
        import dataclasses
        spec = next((item for item in engine.list() if item.id == schedule_id), None)
        if spec is None:
            raise WorkbenchError(tr(f'Расписание не найдено: {schedule_id}',
                                    f'schedule not found: {schedule_id}'))
        enabled = action == 'enable'
        workbench.schedules().save_spec(dataclasses.replace(spec, enabled=enabled))
        if enabled:
            engine.resume(schedule_id)
        else:
            engine.pause(schedule_id)
    elif action == 'next':
        spec = next((item for item in engine.list() if item.id == schedule_id), None)
        if spec is None:
            raise WorkbenchError(tr(f'Расписание не найдено: {schedule_id}',
                                    f'schedule not found: {schedule_id}'))
        plan = engine.plan(spec, engine.state(schedule_id), workbench.clock())
        return emit(args, {'id': schedule_id, 'next_at': plan.next_at, 'reason': plan.reason,
                           'timezone': spec.timezone},
                    tr(f'Следующий запуск: {plan.next_at} ({plan.reason})',
                       f'next run: {plan.next_at} ({plan.reason})'))
    else:
        raise WorkbenchError(tr(f'Неизвестное действие schedule: {action}',
                                f'unknown schedule action: {action}'))
    return emit(args, {'id': schedule_id, 'action': action}, tr(f'Расписание {schedule_id}: {action}',
                                                                  f'schedule {schedule_id}: {action}'))


def _cmd_profile(workbench, args, action):
    """``profile list|show`` -- named check profiles (F05)."""
    store = workbench.profiles()
    if action in ('', 'list'):
        rows = [item.as_dict() if hasattr(item, 'as_dict') else str(item) for item in store.list()]
        return emit(args, rows, '\n'.join(str(item) for item in rows)
                    or tr('Именованных профилей нет; используйте targets из run.', 'no named profiles yet; use the run targets.'))
    profile_id = args.name or ((args.items or ['', ''])[1] if len(args.items) > 1 else '')
    record = store.get(profile_id)
    return emit(args, record.as_dict() if hasattr(record, 'as_dict') else str(record),
                str(record))


def _cmd_preset(workbench, args, action):
    """``preset [поиск]`` -- versioned service sets and their probes (F06).

    The list is deliberately one line per preset with its verification state:
    a preset whose definition was never checked against a live response says so
    here instead of looking as good as a documented one.
    """
    query = (args.items or [''])[1] if len(args.items) > 1 else args.name
    found = workbench.presets(query or '')
    rows = [{'id': preset.preset_id, 'version': preset.version, 'capability': preset.capability,
             'title': preset.title_ru, 'verification': preset.verification,
             'live_checked_on': preset.live_checked_on,
             'probes': [probe.url for probe in preset.probes]} for preset in found]
    return emit(args, rows, '\n'.join(
        f"{row['id']} v{row['version']} {row['capability']} {row['verification']} "
        f"{'live=' + str(row['live_checked_on']) if row['live_checked_on'] else 'live=never'} "
        f"{len(row['probes'])} проб" for row in rows) or tr('Наборы не найдены.', 'no sets found'))


def _cmd_backup(workbench, args, action):
    """``backup create|list|verify|preview|restore|rollback|retention|cleanup|rebind``.

    Only `create` and `list` existed, so the rest of F24 -- the restore preview
    into a new data path, retention/cleanup with a preview, the rollback of a
    migration and the secret rebind after a restore -- was a library nothing
    could reach: `grep` over the product found no caller of `db.restore`,
    `db.retention_preview`, `db.apply_retention`, `db.cleanup`, `db.rollback` or
    `db.rebind_secrets`.  A `backup create` nobody could use is not a backup.
    Every destructive step is a two-call contract: the first prints what *would*
    happen, the second applies it only with ``--apply``.
    """
    from . import db as store
    backup_dir = workbench.backup_dir
    if action in ('create', 'now'):
        manifest = workbench.backup_create(reason='manual')
        return emit(args, manifest.to_dict() if hasattr(manifest, 'to_dict') else str(manifest),
                    tr(f'Резервная копия: {manifest.path} (sha256 {manifest.sha256[:16]}…)',
                       f'backup: {manifest.path} (sha256 {manifest.sha256[:16]}…)'))
    if action in ('list', ''):
        rows = workbench.backup_list()
        return emit(args, rows, '\n'.join(f"{row['name']} {row['size']} байт" for row in rows)
                    or tr('Резервных копий нет.', 'no backups yet'))
    if action == 'verify':
        found = store.list_backups(backup_dir)
        if not found:
            return emit(args, {'checked': 0}, tr('Резервных копий нет.', 'no backups yet'))
        checked = [{'path': item.path, 'ok': store.verify_backup(item)} for item in found]
        broken = [item['path'] for item in checked if not item['ok']]
        if broken:
            raise WorkbenchError(tr('Копия повреждена или не совпадает: ' + ', '.join(broken),
                                    'a backup is corrupt or does not match: ' + ', '.join(broken)),
                                 'E_DATA_BACKUP_FAILED')
        return emit(args, {'checked': len(checked), 'ok': True},
                    tr(f'Проверено копий: {len(checked)}', f'backups checked: {len(checked)}'))
    if action in ('preview', 'restore', 'rollback'):
        return _cmd_backup_restore(workbench, args, action)
    if action in ('retention', 'cleanup'):
        return _cmd_backup_cleanup(workbench, args, action)
    if action == 'rebind':
        return _cmd_backup_rebind(workbench, args)
    raise WorkbenchError(tr(f'Неизвестное действие backup: {action}', f'unknown backup action: {action}'),
                         'E_VALIDATION_FIELD')


def _backup_target(args, workbench):
    """Where a restore would put its files.  Always explicit: a restore never
    guesses the folder it overwrites."""
    target = getattr(args, 'to_data', None)
    if not target:
        raise WorkbenchError(tr('Укажите папку назначения: --to-data ПАПКА',
                                'name the target folder: --to-data PATH'), 'E_VALIDATION_FIELD')
    return Path(target)


def _cmd_backup_restore(workbench, args, action):
    from . import db as store
    target = _backup_target(args, workbench)
    if action == 'rollback':
        # Rollback restores the file a pre-migration backup recorded, so it takes
        # that backup by name (or the newest one) and the target folder.
        # `args.items[0]` is the action itself; a named backup is the first
        # positional after it.
        wanted = next((item for item in (args.items or [])[1:] if item), '')
        if not wanted:
            found = store.list_backups(workbench.backup_dir)
            if not found:
                raise WorkbenchError(tr('Нет резервной копии для отката.',
                                        'no backup to roll back to.'), 'E_STATE_NOT_FOUND')
            wanted = found[0]
        report = store.rollback(wanted, target, apply=bool(args.apply))
    elif action == 'restore':
        report = store.restore(workbench.data, target, apply=bool(args.apply),
                               reason='restore')
    else:
        preview = store.restore_preview(workbench.data, target, reason='restore')
        return emit(args, preview.to_dict(),
                    tr('Предпросмотр восстановления; добавьте --apply, чтобы выполнить.',
                       'restore preview; add --apply to run it.'))
    if not args.apply:
        return emit(args, report.preview.to_dict(),
                    tr('Предпросмотр отката; добавьте --apply, чтобы выполнить.',
                       'rollback preview; add --apply to run it.'))
    if report.preview.conflicts and not report.verified:
        raise WorkbenchError(
            tr('В папке назначения есть файлы с теми же именами: '
               + ', '.join(report.preview.conflicts),
               'the target folder already holds files with these names: '
               + ', '.join(report.preview.conflicts)), 'E_PATH_CONFLICT')
    return emit(args, report.to_dict(), report.note or tr('Восстановлено.', 'restored.'))


def _retention_policy(args):
    from . import db as store
    include = tuple(name.strip() for name in (getattr(args, 'target', '') or '').split(',')
                    if name.strip()) or ('observations', 'results')
    hours = getattr(args, 'retention_hours', 0) or 0
    return store.RetentionPolicy(
        max_age_seconds=(hours * 3600 if hours > 0 else None),
        expired_only=not bool(getattr(args, 'include_fresh', False)),
        include=include,
        keep_newest=int(getattr(args, 'keep_newest', 0) or 0))


def _cmd_backup_cleanup(workbench, args, action):
    from . import db as store
    if action == 'cleanup':
        if not args.apply:
            return emit(args, store.cleanup_preview(workbench.data).to_dict(),
                        tr('Предпросмотр очистки данных; добавьте --apply, чтобы удалить.',
                           'data cleanup preview; add --apply to delete.'))
        report = store.cleanup(workbench.data, apply=True,
                               keep_lock=bool(getattr(args, 'keep_lock', False)))
        return emit(args, report.to_dict(), tr('Очистка данных выполнена.', 'data cleanup applied.'))
    policy = _retention_policy(args)
    # The connection is the workbench's own: it closes it on exit, and closing it
    # here made `Workbench.close()` fail on an already closed handle.
    conn = workbench.conn
    if not args.apply:
        return emit(args, store.retention_preview(conn, policy).to_dict(),
                    tr('Предпросмотр retention; добавьте --apply, чтобы удалить.',
                       'retention preview; add --apply to delete.'))
    report = store.apply_retention(conn, policy, vacuum=bool(getattr(args, 'vacuum', False)))
    return emit(args, report.to_dict(), tr('Retention применена.', 'retention applied.'))


def _cmd_backup_rebind(workbench, args):
    """Re-point the vault after a restore into a new data path (F24, F18)."""
    from . import db as store
    mapping = json.loads(Path(args.mapping).read_text(encoding='utf-8')) \
        if getattr(args, 'mapping', None) else {}
    report = store.rebind_secrets(workbench.data, mapping, dry_run=not args.apply)
    body = report.to_dict() if hasattr(report, 'to_dict') else {'report': str(report)}
    if not args.apply:
        return emit(args, body, tr('Предпросмотр перепривязки секретов.',
                                   'secret rebind preview.'))
    return emit(args, body, tr('Секреты перепривязаны.', 'secrets rebound.'))


def _diagnose_inputs(workbench, args):
    """The pieces every diagnostic reads: stored rows, export status, source reports."""
    profile = None
    rows = []
    conn = workbench.conn
    latest = conn.execute('SELECT id FROM profiles ORDER BY created_at DESC LIMIT 1').fetchone()
    if latest is not None:
        profile = str(latest[0])
        for (payload,) in conn.execute('SELECT payload FROM results WHERE profile=?',
                                       (profile,)).fetchall():
            try:
                rows.append(json.loads(payload))
            except (TypeError, ValueError):
                continue
    status = _diagnose_status(workbench)
    sources = []
    report = Path(args.data) / 'sources-report.json'
    if report.is_file():
        try:
            sources.append(json.loads(report.read_text(encoding='utf-8')))
        except (OSError, ValueError):
            sources = []
    return profile, rows, status, sources


def _diagnose_status(workbench):
    """The status document of the current export generation.

    `current.json` is a *pointer* -- a generation name and a manifest. The funnel
    reads the run itself (`state`, `stop_reason`, `exported`, `checked`), which
    lives in the generation's `status.json`; handing it the pointer made every
    total empty and `explain_zero` answer "nothing to explain" for a run that had
    measured nothing.
    """
    exports = Path(workbench.data) / 'exports'
    manifest = export_manifest(exports) or {}
    generation = manifest.get('generation')
    if isinstance(generation, str) and generation and '/' not in generation:
        target = exports / 'generations' / generation / 'status.json'
        if target.is_file():
            try:
                loaded = json.loads(target.read_text(encoding='utf-8'))
                if isinstance(loaded, dict):
                    return loaded
            except (OSError, ValueError):
                pass
    legacy = exports / 'status.json'
    if legacy.is_file():
        try:
            loaded = json.loads(legacy.read_text(encoding='utf-8'))
            if isinstance(loaded, dict):
                return loaded
        except (OSError, ValueError):
            pass
    return {}


def _cmd_diagnose(workbench, args, action):
    """``diagnose funnel|zero|control|health|bundle`` -- F10, F25.

    The diagnostic layer was written and tested, and the product never called it:
    `build_bundle`, `health_report`, `build_funnel`, `explain_zero`, `check_control`
    and `fixture_recipe` appeared in `tests/` and nowhere else, and the module's
    only use in the product was `diagnostics.classification(exc)`.  So a user
    asking "why did I get zero proxies" had a traceback and nothing else.  Every
    action is a read; `bundle` writes a redacted file and says where.
    """
    from . import diagnostics
    profile, rows, status, sources = _diagnose_inputs(workbench, args)
    if action in ('', 'funnel'):
        body = diagnostics.build_funnel(rows, status=status, sources=sources).to_dict()
        text = '\n'.join(
            [tr(f'Профиль: {profile or "—"}; состояние: {body.get("set_state") or "—"}; '
                f'остановка: {body.get("stop_reason") or "—"}',
                f'profile: {profile or "—"}; state: {body.get("set_state") or "—"}; '
                f'stop: {body.get("stop_reason") or "—"}')]
            + [f'  {stage}: {json.dumps(value, ensure_ascii=False, default=str)}'
               for stage, value in (body.get('stages') or {}).items()])
        return emit(args, body, text)
    if action == 'zero':
        funnel = diagnostics.build_funnel(rows, status=status, sources=sources)
        zero = diagnostics.explain_zero(funnel)
        # One shape either way: `zero` is the explanation or null, never a body
        # that changes type depending on the outcome.
        if zero is None:
            return emit(args, {'zero': None},
                        tr('Нулевой результат не объясняется: в воронке есть прошедшие строки.',
                           'the empty result is not explained: some rows passed the funnel'))
        return emit(args, {'zero': zero.to_dict()}, zero.render())
    if action == 'control':
        config = target_config(args, denylist=Denylist.empty()) if (args.url or args.config) else None
        targets = (config or {}).get('targets') or ()
        verdict = diagnostics.check_control(targets=targets)
        body = verdict.to_dict()
        return emit(args, body, tr(
            f'Контроль: {body.get("state")} ({body.get("checked")} проверок, {body.get("code")})',
            f'control: {body.get("state")} ({body.get("checked")} checks, {body.get("code")})'))
    if action == 'health':
        body = diagnostics.health_report(version=PRODUCT_VERSION,
                                         schema_version=schema.SCHEMA_VERSION,
                                         max_age_seconds=MIN_FRESHNESS_SECONDS).to_dict()
        # `HealthCheck.to_dict()` answers with the boolean `ok`; there is no
        # `state` field, so asking for it made every check look like a problem
        # and a healthy database reported "5 checks, 5 problems".  `state` is
        # accepted as an alias so a check that grows one keeps working.
        problems = [item for item in body.get('checks', [])
                    if not (item.get('ok', item.get('state') == 'ok'))]
        return emit(args, body, tr(
            f'Здоровье: проверок {len(body.get("checks", []))}, проблем {len(problems)}',
            f'health: {len(body.get("checks", []))} checks, {len(problems)} problems'))
    if action == 'bundle':
        funnel = diagnostics.build_funnel(rows, status=status, sources=sources)
        bundle = diagnostics.build_bundle(
            environment={'product': PRODUCT_NAME, 'version': PRODUCT_VERSION},
            status=status, rows=rows, sources=sources, funnel=funnel,
            zero=diagnostics.explain_zero(funnel))
        target = Path(args.data) / 'diagnostics' / 'bundle.json'
        target.parent.mkdir(parents=True, exist_ok=True)
        # `preview()` is the same document that `save()` would write, so the file
        # on disk is exactly what the caller was shown before anything was kept.
        atomic(target, json.dumps(bundle.preview(), ensure_ascii=False, indent=1, default=str))
        return emit(args, {'path': str(target), 'redactions': bundle.redactions,
                           'schema_version': bundle.schema_version,
                           'sample_size': bundle.sample_size,
                           'total_size': bundle.total_size,
                           'truncated': bundle.truncated},
                    tr(f'Диагностический пакет: {target} (выводов {len(bundle.redactions)})',
                       f'diagnostic bundle: {target} ({len(bundle.redactions)} redactions)'))
    raise WorkbenchError(tr(f'Неизвестное действие diagnose: {action}',
                            f'unknown diagnose action: {action}'), 'E_VALIDATION_FIELD')


def _cmd_geo(workbench, args, action):
    """``geo status`` -- the country and provider databases, honestly (F08)."""
    from . import geo
    layout = workbench.layout()
    country = geo.database_status(geoip.default_path(workbench.data), 'country', now=workbench.clock())
    provider = geo.database_status(geoip.asn_path(workbench.data), 'asn', now=workbench.clock())
    return emit(args, {'country': _as_json(country), 'provider': _as_json(provider),
                       'data': str(layout.data)},
                tr(f'База стран: {_database_phrase(country)}; база провайдеров: {_database_phrase(provider)}',
                   f'country database: {_database_phrase(country)}; provider database: {_database_phrase(provider)}'))


def _database_phrase(status):
    """One honest sentence about a country/provider database (F08).

    A missing or stale database is never reported as a ready one: the age and
    the error, if any, travel with the state.
    """
    if not status.present:
        return tr('не установлена', 'not installed')
    if status.error:
        return f'{status.kind}: {status.error}'
    if status.stale:
        return tr(f'устарела ({status.version}, {int(status.age_seconds or 0)} с)',
                  f'stale ({status.version}, {int(status.age_seconds or 0)} s)')
    return tr(f'готова ({status.version})', f'ready ({status.version})')


def _as_json(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {key: _as_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_as_json(item) for item in value]
    return str(value)


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
            or args.max_per_host < 1 or args.judge_concurrency < 1
            or not math.isfinite(args.min_host_interval) or args.min_host_interval < 0
            or not math.isfinite(args.watch) or args.watch < 0):
        p.error(tr('Неверные числовые параметры', 'Invalid numeric options'))
    try:
        countries = geoip.parse_countries(args.country)
        country_exclude = geoip.parse_countries(getattr(args, 'country_exclude', '') or '')
    except ValueError as exc:
        p.error(tr(str(exc), 'Countries: use two-letter ISO codes, for example DE,NL.'))
    country_basis = getattr(args, 'country_basis', 'endpoint') or 'endpoint'
    country_unknown = getattr(args, 'country_unknown', 'exclude') or 'exclude'
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
    if args.command == 'bench':
        return bench_chain(args)
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
    if args.command in MANAGEMENT_COMMANDS:
        # Management commands run under the same data lock as a scan, on the
        # same migrated database, and reach the modules through Workbench.
        try:
            return management_command(args)
        finally:
            lock.close()
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
    # The run is one job in the persistent lifecycle: a cancel, a crash or a
    # restart keeps the last completed result and the queue of unfinished items
    # (defect 6).  It is optional, so a read-only run still works.
    scan_workbench = None
    current_job_id = ['']
    try:
        scan_workbench = Workbench(args.data, db_path=args.data / 'proxies.sqlite3')
    except (schema.DbError, sqlite3.Error, OSError):
        scan_workbench = None

    def update_progress(values):
        latest.update(values, updated_at=time.time())
        if args.progress_file:
            atomic(args.progress_file, json.dumps(latest))

    update_progress(dict(phase='starting', checked=0, candidates=0))

    def export_now(*, run_state=None, diagnostic=False, selected=None):
        # The published generation is a statement about one scope (F02): a row
        # measured in another collection is not part of it.  Without
        # ``--collection`` the scope is the public base, exactly as before.
        from . import exportsvc  # imported here: it imports constants from this module
        credentials = getattr(args, 'credentials', exportsvc.CREDENTIALS_REDACT) \
            or exportsvc.CREDENTIALS_REDACT
        return export(db, profile, args.data / 'exports', top=args.top,
                      sort=args.sort, min_success=args.min_success, denylist=denylist,
                      local_override=args.local_denylist, min_anonymity=args.min_anonymity,
                      protocol=args.protocol, max_latency=args.max_latency or None,
                      countries=countries, country_exclude=country_exclude,
                      country_basis=country_basis, country_unknown=country_unknown,
                      country_of=country_of,
                      exclude_hosting=args.no_hosting or args.export_hosting == 'hide',
                      provider_of=provider_of, watch_minutes=args.watch, query=export_query, quick=export_quick,
                      allowed_proxies=selected, run_state=run_state, diagnostic=diagnostic,
                      collection_id=args.collection or None,
                      credentials=credentials,
                      client_target=getattr(args, 'client_target', None),
                      client_binary=getattr(args, 'client_binary', None),
                      secret_grant=(exportsvc.SecretGrant(allowed=True, issued_by='local-cli')
                                    if credentials == exportsvc.CREDENTIALS_REFERENCE else None),
                      active_profile_path=None if diagnostic else args.data / 'last-profile.txt')

    try:
        if args.command in ('scan', 'run'):
            config = target_config(args, denylist=denylist)
            if not config['reputation'].get('local_enabled', True):
                collect_denylist = Denylist.empty()
            profile = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:20]
        if args.command in ('collect', 'run'):
            # F13: without an explicit `--sources` the collection follows the
            # user's *selection* in the source catalog, not the bundled flat
            # list of 55 URLs.  Before this, a run always asked for a list file
            # and a source the user had switched on in the catalog was never
            # downloaded at all.
            values = resolve_collect_sources(args, data=args.data)
            report = asyncio.run(stoppable(collect(
                db, values, args.input, args.source_timeout, update_progress, denylist=collect_denylist,
                allow_private_sources=args.allow_private_sources, detect_protocols=args.detect_protocols,
                max_source_bytes=args.source_max_bytes,
                max_source_line_bytes=args.source_max_line_bytes,
                max_source_candidates=args.source_max_candidates,
                max_source_redirects=args.source_max_redirects,
                allow_private_endpoints=args.allow_private_endpoints), args.stop_file))
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

            # The judge and the bandwidth download are the expensive half of one
            # measurement, and they are the half that may only be paid for by a
            # proxy that already works.  They are wired here as the chain's
            # expensive stage instead of being folded into the request stage, so
            # that somebody else's service and a body-sized download are bounded
            # by the per-target limits and by the run's byte budget — which is
            # exactly what did not happen while both were one call.
            probe = check_proxy
            expensive_probe = None
            basic_config = config
            own_ips = ()
            if config.get('anonymity') or config.get('speedtest'):
                if config.get('anonymity'):
                    try:
                        own_ips = asyncio.run(detect_own_ips(config))
                    except (httpx.HTTPError, TimeoutError, OSError, ValueError) as exc:
                        reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                        print(tr(f'Не удалось определить внешний IP через judge URL: {reason}', f'Could not detect the external IP through the judge URL: {reason}'), file=sys.stderr)
                        raise
                    print(tr(f'Проверка анонимности: judge {public_url(config["anonymity"]["judge_url"])}', f'Anonymity check: judge {public_url(config["anonymity"]["judge_url"])}'), flush=True)
                # The profile hash covers the whole profile, judge and speedtest
                # included, so only the copy the request stage reads is trimmed.
                basic_config = {key: value for key, value in config.items()
                                if key not in ('anonymity', 'speedtest')}

                async def probe(proxy, scan_config, limiter):
                    return await check_proxy(proxy, basic_config, limiter)

                async def expensive_probe(proxy, scan_config, limiter, row):
                    """Judge and bandwidth for a proxy that already works.

                    The same two conditions the single call used to apply: the
                    judge is asked only of a proxy that reached a target, and the
                    bandwidth only of a proxy that reached every one of them.
                    """
                    if scan_config.get('anonymity') and own_ips and row.get('successes'):
                        row['anonymity'] = await judge_proxy(proxy, scan_config, limiter, own_ips)
                    if scan_config.get('speedtest') and (row.get('min_target_reliability') or 0) > 0:
                        row['speed'] = await measure_speed(proxy, scan_config, limiter)
                    return row

            # Whether the cheap stage runs at all.  Its size used to be this
            # number of connections; it is now the chain's derived worker count,
            # like everything else (F12, resources not workers).
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
                                               country_exclude=country_exclude,
                                               country_basis=country_basis,
                                               country_unknown=country_unknown,
                                               country_of=country_of, prefilter=prefilter,
                                               exclude_hosting=args.no_hosting, provider_of=provider_of,
                                               prefilter_timeout=min(args.prefilter_timeout, args.connect_timeout),
                                               collection_id=args.collection or None,
                                               deadline_s=args.deadline or None,
                                               max_requests=args.max_requests or None,
                                               max_bytes=args.run_max_bytes or None,
                                               count_what=args.count_what,
                                               max_per_host=args.max_per_host,
                                               min_host_interval_s=args.min_host_interval,
                                               target_inflight=args.judge_concurrency,
                                               expensive_probe=expensive_probe,
                                               job_store=scan_workbench.jobs() if scan_workbench else None,
                                               job_id=current_job_id[0],
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

            if scan_workbench is not None:
                # One job per run, with its queue created up front: a stop in the
                # middle leaves the unfinished items and the finished results
                # both readable (defect 6, F11).
                current_job_id[0] = submit_scan_job(
                    scan_workbench, db, 'check', profile=profile, profile_revision=1,
                    collection_id=ensure_collection(db, args.collection or None),
                    candidates=[value for (value,) in collection_candidates(
                        db, ensure_collection(db, args.collection or None))],
                    filters=dict(protocol=args.protocol, countries=sorted(countries),
                                 min_anonymity=args.min_anonymity, want=args.want,
                                 count_what=args.count_what),
                    budgets=dict(deadline_s=args.deadline or None,
                                 max_requests=args.max_requests or None,
                                 max_bytes=args.run_max_bytes or None))
                print(tr(f'Задание: {current_job_id[0]}', f'job: {current_job_id[0]}'), flush=True)
            last_scan_state = run_scan(recheck=args.recheck, recheck_passing=args.recheck_passing, want=args.want)
            if scan_workbench is not None and current_job_id[0]:
                from . import jobs as joblifecycle
                job_store = scan_workbench.jobs()
                state_now = last_scan_state or {}
                terminal = 'succeeded' if state_now.get('state') == 'complete' else 'partial'
                finish_scan_job(job_store, current_job_id[0], terminal,
                                state_now.get('stop_reason') or joblifecycle.CODE_OK)
            last_report = export_now(run_state=last_scan_state)
            update_progress(last_report)
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
                update_progress(last_report)
                current_published = True
        elif args.command == 'export':
            profile = (args.data / 'last-profile.txt').read_text(encoding='utf-8').strip()
            # A selected slice is its own artifact and leaves the published
            # pointer alone (defect 7), so the report is the only place the GUI
            # learns where that artifact is.
            last_report = export_now(selected=allowed_proxies)
            update_progress(last_report)
            current_published = last_report.get('kind') == 'published'
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
