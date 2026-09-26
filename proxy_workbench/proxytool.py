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
#: An address with no credentials still has an access identity, and a named one:
#: an empty id is indistinguishable from a row that never recorded who measured
#: it, and admission refuses that (CONTRACTS §1.2 rule 1, §2.3).  The web
#: surface reads the same identity (``gui.App.snapshot_access``); the two have to
#: name the same thing or the table and the engine disagree about which rows
#: exist at all.
PUBLIC_ACCESS_ID = 'default'
PUBLIC_ACCESS = core.Access(PUBLIC_ACCESS_ID, 1)
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
                  max_source_redirects=DEFAULT_SOURCE_MAX_REDIRECTS,
                  collection_id=None, origin='public', allow_private_endpoints=False):
    if not isinstance(allow_private_sources, bool):
        raise ValueError('allow_private_sources: ожидается bool')
    if not isinstance(allow_private_endpoints, bool):
        raise ValueError('allow_private_endpoints: ожидается bool')
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
    # Collecting into a named list writes membership there; the public source
    # lists keep filling the public base.  Nothing is ever copied between the two.
    collection_id = ensure_collection(db, collection_id)
    if origin not in schema.COLLECTION_ORIGINS:
        raise ValueError('origin: неизвестное происхождение членства коллекции')

    def publish():
        if on_progress:
            on_progress(dict(phase="collecting", sources_done=sum("source" in r for r in reports),
                             sources_total=len(urls), sources=reports, raw_rows=total_rows,
                             blocked=sum(r.get('blocked', 0) for r in reports),
                             candidates=db.execute("SELECT count(*) FROM candidates").fetchone()[0]))

    def add(value, protocol='http', country=None, source=None, public_only=True):
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
        endpoint = schema.upsert_endpoint(db, proxy, country=country,
                                          country_at=time.time() if country else None,
                                          country_source='source' if country else None)
        db.execute('INSERT OR IGNORE INTO candidates(proxy, endpoint_id) VALUES (?, ?)', (proxy, endpoint))
        schema.add_member(db, collection_id, endpoint, origin=origin)
        if source:
            # How many lists offer an address: rare ones are less crowded and tend to live longer.
            db.execute('INSERT OR IGNORE INTO candidate_seen(proxy, source, endpoint_id) VALUES (?, ?, ?)',
                       (proxy, source, endpoint))
        if country or source:
            record_candidate_meta(db, proxy, country and country.upper(), source)
        return 'accepted'


    def add_detected(value, source=None, public_only=True):
        """Unlabeled addresses are tried as every protocol; the checks show which one works."""
        value = value.strip()
        if '://' in value:
            return add(value, source=source, public_only=public_only)
        outcomes = [add(value, protocol, source=source, public_only=public_only)
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


async def check_proxy(proxy, config, rate, own_ips=None):
    samples = []
    limit = allowed_failures(config)
    mode, _options = probe_plan(config)
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
    row['mode'] = mode.name
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
        result['error_stage'], result['error_code'] = diagnostics.classification(exc)
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
        # An anonymity check that failed is unknown, never a pass: the level the
        # user asked for is not granted on a failed request (defect 13).
        return anonymity.result('unknown', error=diagnostics.classification(exc)[1], started=started)
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
               exclude_hosting=False, provider_of=None, run_state=None,
               collection_id=None, profile_revision=1, max_age_seconds=None,
               access=None, job_id=None, job_store=None, deadline_s=None,
               max_requests=None, max_bytes=None, count_what='endpoint'):
    """Check every pending candidate of the profile.

    With ``prefilter`` connections a cheap TCP connect runs first: most public
    addresses are dead, and dropping them there is far faster than a full
    request with its connect timeout. Only reachable addresses reach the workers.

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
    access = access or PUBLIC_ACCESS
    network_id = snapshot_network(config)
    admission_policy = core.Policy(
        max_age_seconds=float(max_age_seconds if max_age_seconds else MIN_FRESHNESS_SECONDS),
        min_success=policy_min_success(min_success), min_anonymity=min_anonymity, strict=strict,
        countries=countries, denied=frozenset(active_denylist.proxies) if active_denylist else frozenset(),
        max_latency_ms=max_latency,
        deny_match=active_denylist.match if active_denylist is not None else None,
        protocol_of=proxy_protocol, country_of=country_of,
        hosting_of=(lambda proxy: is_hosting(proxy, provider_of)) if exclude_hosting else None,
        exclude_hosting=bool(exclude_hosting))
    engine = core.AdmissionEngine(admission_policy)

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
        previous[proxy] = row
    if recheck_passing:
        only = set(previous)
    db.commit()

    # A normal continuation may skip a result only while its verdict is still
    # trustworthy.  In particular, UNREACHABLE and expired rows are pending on
    # the next scan; treating every row as done made a dead proxy permanent.
    # "Trustworthy" is the one admission contract, not a local freshness test:
    # a row with no recorded lifetime is never assumed fresh (defects 1, 2, 4).
    if recheck or recheck_passing:
        done = set()
    else:
        now = time.time()
        done = {proxy for proxy, row in stored_rows.items()
                if core.observation_state(row) != core.OBSERVATION_MISSING
                and not row.get('error')
                and core.time_state_of(row, now, admission_policy)['state'] == core.TIME_OK}
    pending, total, completed = [], 0, 0
    for (proxy,) in collection_candidates(db, collection_id):
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

    # The find-N policy, the ledger that refuses to check one address twice and
    # the resource gate are the pipeline's own objects, not a local copy of
    # their rules: one chain decides what "enough" means and what the whole run
    # is allowed to spend (F12).
    from . import pipeline as chain
    budgets = chain.Budgets(max_inflight=max(1, int(workers)), deadline_s=deadline_s,
                            max_requests=max_requests, max_bytes=max_bytes)
    gate = chain.ResourceGate(budgets, chain.SystemClock(), deadline=deadline_s)
    ledger = chain.Ledger()
    # ``--want`` counts endpoints unless the user asks for another unit: N
    # endpoints, N IPs and N confirmed exit IPs are different numbers, and the
    # caller names the one it means (F12, CONTRACTS §1.1).
    find = chain.FindPolicy(n=want, what=count_what)
    budget_stop = {'reason': ''}

    def over_budget():
        """Whether a *total* budget is spent; the gate owns the decision.

        Waiting for a free slot is not being out of budget, so only
        :meth:`ResourceGate.check_totals` is consulted here -- the same call the
        pipeline makes, so a CLI run and a pipeline run stop for the same reason.
        """
        if budget_stop['reason']:
            return True
        try:
            gate.check_totals()
        except chain.BudgetExhausted as exc:
            budget_stop['reason'] = exc.code
            return True
        return False

    def counted(row):
        """What ``--want`` counts: endpoints, IPs or confirmed exit IPs.

        They are different numbers and the user picks one (F12, CONTRACTS §1.1).
        A row without a confirmed exit is never counted as one.
        """
        if find.what == 'endpoint':
            return 1
        return 1 if row.get('exit_ip') else 0

    found = {'items': passed}
    observation_id = [None]

    def store(proxy, row):
        nonlocal completed, last_commit, passed
        observation_id[0] = None
        country = country_of(proxy)
        if country:
            row['country'] = country
        endpoint = schema.upsert_endpoint(db, proxy)
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
        status = reputation_status(merged)
        status_counts[status] = status_counts.get(status, 0) + 1
        if counts_as_passed(merged):
            found['items'] = found['items'] + counted(merged)
            passed += 1
        if find.enabled and found['items'] >= find.n:
            enough.set()
        if observation_id[0] is not None:
            finish_job_item(job_store, job_id, endpoint, merged, observation_id[0])
        if completed % 100 == 0 or time.monotonic() - last_commit >= 1:
            db.commit()
            last_commit = time.monotonic()

    async def producer():
        target = incoming if prefilter else queue
        for proxy in pending:
            if enough.is_set() or over_budget():
                break
            # The ledger is what makes "one address is never checked twice" a
            # property of the run rather than of this loop (F12).
            if not ledger.admit(proxy):
                continue
            await target.put(proxy)
        for _ in range(prefilter if prefilter else workers):
            await target.put(None)

    async def gatekeeper():
        nonlocal unreachable
        while True:
            proxy = await incoming.get()
            if proxy is None:
                return
            if enough.is_set() or over_budget():
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
                if enough.is_set() or over_budget():
                    # Left unchecked; a later run of the same profile picks it up.
                    # The address goes back to the ledger's pending list, so a
                    # resumed run measures it instead of losing it.
                    ledger.release(proxy)
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
                        # The resource gate is the single place that decides
                        # whether the next request is still affordable, in RAM,
                        # in open descriptors and against the global deadline.
                        await gate.acquire()
                        try:
                            row = await probe(proxy, config, limiter)
                        finally:
                            await gate.release()
                    except Exception as exc:
                        # One malformed proxy must never stop the whole scan, and
                        # the reason it died is recorded with a stage so the
                        # funnel can count it (F10, F25).
                        row = unreachable_result(proxy)
                        row['error'] = type(exc).__name__
                        row['error_stage'], row['error_code'] = diagnostics.classification(exc)
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
        if budget_stop['reason']:
            snapshot_state = 'partial'
            stop_reason = budget_stop['reason']
        elif recheck_passing:
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
                             passed=passed, found=found['items'], count_what=find.what)
        if on_progress:
            on_progress(dict(phase='scanning', profile=profile, checked=completed, candidates=total, passed=passed,
                             pending=max(0, total - completed), state=snapshot_state, stop_reason=stop_reason,
                             scope_candidates=total, speed=round(speed, 2), found=found['items'],
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
                      exclude_hosting=False, provider_of=None):
    """Selection by protocol, maximum median latency (ms), country codes and hosting providers."""
    if protocol not in (None, 'all') and proxy_protocol(row.get('proxy', '')) != protocol:
        return False
    if exclude_hosting and is_hosting(row.get('proxy', ''), provider_of):
        return False
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
           protocol='all', max_latency=None, countries=(), country_of=None, exclude_hosting=False, provider_of=None,
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
    access = access or PUBLIC_ACCESS
    max_age_seconds = float(max_age_seconds if max_age_seconds else MIN_FRESHNESS_SECONDS)
    countries = frozenset(countries or ())
    network_id = snapshot_network(cfg)
    policy = core.Policy(
        max_age_seconds=max_age_seconds, min_success=policy_min_success(min_success),
        min_anonymity=min_anonymity, strict=strict, protocol=protocol or None,
        countries=countries, max_latency_ms=max_latency,
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
            endpoint = schema.upsert_endpoint(self.conn, value)
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
                                      'import', 'source', 'api-key', 'pool', 'schedule', 'profile', 'preset', 'backup', 'geo'],
                   help=tr('run — собрать и проверить; collect — только собрать; scan — только проверить; '
                           'export — пересобрать файлы; get — вывести готовые прокси; test — проверить свои прокси; '
                           'serve — локальное API; gateway — ротирующий прокси; '
                           'update-geoip — база стран; '
                           'import/source — свои списки и подписки; api-key — ключи локального API; '
                           'pool/schedule — постоянные пулы и расписания; profile/preset — профили и наборы сервисов; '
                           'backup — резервные копии; geo — состояние базы стран; '
                           'clear-data — удалить результаты',
                           'run: collect and check; collect: only collect; scan: only check; '
                           'export: rebuild the files; get: print working proxies; test: check given proxies; '
                           'serve: local API; gateway: rotating proxy; '
                           'update-geoip: country database; '
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
    p.add_argument('--text', action='append', default=[],
                   help=tr('значение для проверки, например source redact URL; можно повторять',
                           'value to inspect, e.g. source redact URL; may be repeated'))
    p.add_argument('--include-secret', action='store_true',
                   help=tr('source redact: показать искомую строку, если она утёкла; по умолчанию только факт',
                           'source redact: show the searched string if it leaked; by default only the fact'))
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
                       'preset', 'backup', 'geo')


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


def _cmd_source(workbench, args, action):
    """``source list|health|redact`` -- sources, subscriptions and redaction (F27)."""
    from . import sourcedesk
    if action in ('', 'list'):
        rows = workbench.conn.execute(
            'SELECT source, count(*) AS seen FROM candidate_seen GROUP BY source ORDER BY seen DESC').fetchall()
        return emit(args, [dict(row) for row in rows],
                    '\n'.join(f"{row['source']}: {row['seen']}" for row in rows)
                    or tr('Источники ещё не собирались.', 'no sources collected yet'))
    if action == 'health':
        source_id = args.name or (args.text[0] if args.text else '') \
            or ((args.items or ['', ''])[1] if len(args.items) > 1 else '')
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
    if action == 'redact':
        # ``--text`` is the option form because argparse keeps a positional run
        # only while the options do not interrupt it; both forms are accepted.
        values = list(args.text) + list(args.items[1:])
        redacted = [sourcedesk.redact_url(value) for value in values if value]
        leaks = sourcedesk.find_secret_leaks(redacted, values) if redacted else []
        return emit(args, {'redacted': redacted, 'leaks': list(leaks)},
                    '\n'.join(redacted) or tr('Нечего редактировать.', 'nothing to redact'))
    raise WorkbenchError(tr(f'Неизвестное действие source: {action}',
                            f'unknown source action: {action}'))


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


def _cmd_pool(workbench, args, action):
    """``pool list|create|status|members`` -- a steady pool of proxies (F14)."""
    from . import pools
    store = workbench.pools()
    if action in ('', 'list'):
        rows = []
        for spec in store.list():
            status = store.status(spec.id)
            served = sum((status.counts or {}).values())
            rows.append({'id': spec.id, 'collection_id': spec.collection_id,
                         'profile_id': spec.profile_id, 'desired': spec.desired,
                         'state': status.state, 'served': served,
                         'ready_for_clients': status.ready_for_clients,
                         'deficit_reason': status.deficit_reason})
        return emit(args, rows, '\n'.join(
            f"{row['id']} {row['state']} {row['served']}/{row['desired']} {row['deficit_reason'] or ''}"
            for row in rows) or tr('Пулов нет.', 'no pools'))
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
    raise WorkbenchError(tr(f'Неизвестное действие pool: {action}', f'unknown pool action: {action}'))


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
    """``backup create|list`` -- a restorable copy with a manifest (F24)."""
    if action in ('create', 'now'):
        manifest = workbench.backup_create(reason='manual')
        return emit(args, manifest.to_dict() if hasattr(manifest, 'to_dict') else str(manifest),
                    tr(f'Резервная копия: {manifest.path} (sha256 {manifest.sha256[:16]}…)',
                       f'backup: {manifest.path} (sha256 {manifest.sha256[:16]}…)'))
    rows = workbench.backup_list()
    return emit(args, rows, '\n'.join(f"{row['name']} {row['size']} байт" for row in rows)
                or tr('Резервных копий нет.', 'no backups yet'))


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
                      countries=countries, country_of=country_of,
                      exclude_hosting=args.no_hosting or args.export_hosting == 'hide',
                      provider_of=provider_of, watch_minutes=args.watch, query=export_query, quick=export_quick,
                      allowed_proxies=selected, run_state=run_state, diagnostic=diagnostic,
                      collection_id=args.collection or None,
                      credentials=credentials,
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
            urls = [] if args.no_sources else json.loads(args.sources.read_text(encoding='utf-8'))
            if not isinstance(urls, list):
                raise ValueError('sources: ожидается JSON-массив http(s) URL')
            report = asyncio.run(stoppable(collect(
                db, urls, args.input, args.source_timeout, update_progress, denylist=collect_denylist,
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
                                               collection_id=args.collection or None,
                                               deadline_s=args.deadline or None,
                                               max_requests=args.max_requests or None,
                                               max_bytes=args.run_max_bytes or None,
                                               count_what=args.count_what,
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
