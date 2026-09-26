"""Local HTTP API: the legacy read-only endpoints and the versioned ``/v1``.

Two surfaces share one process and one snapshot reader:

* the 2.x read-only endpoints (``/proxies``, ``/random``, ``/status``, ``/pac``,
  ``/clash``, ``/singbox``) with the ``--api-token`` / ``PROXY_WORKBENCH_API_TOKEN``
  compatibility path, and
* the versioned control API under ``/v1`` (:mod:`proxy_workbench.apiv1`).

Both read the same published generation through :class:`Exports`, so the three
interfaces -- CLI, GUI and API -- cannot drift into different row sets: the
selection, the freshness decision and the error codes all come from
:mod:`proxy_workbench.core` and :mod:`proxy_workbench.exportsvc`.

The legacy token keeps exactly the rights it has always had (``read.*`` and
nothing else).  It is not registered as a key and gains no admin or private
permission when it meets the new API; it travels as an explicit *compatibility
path* and says so in a ``Warning`` header (CONTRACTS §5.1, F29).
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import random
import secrets as random_source
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import anonymity
from . import apikeys
from . import apiv1
from . import core
from . import exportsvc
from . import formats
from . import geoip
from .branding import PRODUCT_NAME, PRODUCT_VERSION
from .i18n import tr
from .proxytool import (PROTOCOLS, SORTS, export_file as proxytool_export_file,
                        export_manifest as proxytool_export_manifest, proxy_protocol,
                        reputation_status, row_history, PUBLIC_ACCESS)

DEFAULT_PORT = 8765
V1_DEFAULT_PORT = 8766
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
    '/v1': 'versioned control API; needs an API key (read.status is enough for reads)',
}

#: Reasons a consumer may ask for, kept in one place so CLI, GUI and API answer
#: with the same vocabulary (CONTRACTS §5.4).
FRESHNESS_MODES = ('fresh', 'expired', 'unknown', 'all')

#: The file of a snapshot artifact that answers a requested ``format``.  The
#: artifact always carries the whole set; the format only names the one the
#: caller will download, so an unknown name is a refusal rather than a silent
#: "here is something else".
EXPORT_FORMAT_FILES = {
    'json': 'ranked.json',
    'txt': 'proxies.txt',
    'csv': 'ranked.csv',
    'pac': 'proxy.pac',
    'clash': 'clash.yaml',
    'singbox': 'singbox.json',
}


def is_loopback(host):
    if host == 'localhost':
        return True
    try:
        return ipaddress.ip_address(host.strip('[]')).is_loopback
    except ValueError:
        return False


def public_row(row, now=None):
    """Stable, compact record of one exported proxy.

    The age, the admission reason and the freshness view come from the one
    admission contract (:mod:`proxy_workbench.core`), not from a second
    calculation here: a consumer that recomputed freshness locally would
    disagree with the engine as soon as the clock or the policy moved
    (CONTRACTS §2.3, §4.4).
    """
    proxy = row['proxy']
    address = proxy.partition('://')[2]
    host, _, port = address.rpartition(':')
    history = row_history(row)
    verdict = row.get('reputation') if isinstance(row.get('reputation'), dict) else {}
    dnsbl = verdict.get('dnsbl') if isinstance(verdict.get('dnsbl'), list) else []
    source_keys = row.get('source_keys') or []
    if isinstance(source_keys, str):
        source_keys = [source_keys]
    elif not isinstance(source_keys, (list, tuple, set)):
        source_keys = []
    return {
        'proxy': proxy,
        # The stable id of the address.  A path segment cannot carry a full
        # address (`ID_PATTERN` has no `/`), so this is what `/v1/results/{id}`
        # and `/v1/results/{id}/observations` are addressed by; without it those
        # two declared operations could never find anything.
        'endpoint_id': row.get('endpoint_id'),
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
        'source_keys': list(source_keys),
        'reliability': row.get('reliability'),
        'uptime': round(history['passes'] / history['checks'], 4) if history['checks'] else None,
        'checks': history['checks'],
        'score': row.get('score'),
        'checked_at': row.get('checked_at'),
        'valid_until': row.get('valid_until'),
        'collection_id': row.get('collection_id'),
        'profile_id': row.get('profile_id'),
        'profile_revision': row.get('profile_revision'),
        'age_seconds': row.get('age_seconds'),
        'admission_reason': row.get('admission_reason'),
        # One decision about the clock, taken now, for both fields: a row must
        # not report one time state and the freshness of another.
        'time_state': TIME_STATE_CODES.get(time_state_of(row)['state']),
        # A legacy row that carries no recorded lifetime is read with a one-time
        # backfill, and the fact that it was backfilled is visible here.  Without
        # these two the API showed a fresh row with no way to tell it apart from
        # one whose lifetime was really measured (CONTRACTS §2.4).
        'max_age_seconds': row.get('max_age_seconds') or READ_POLICY.max_age_seconds,
        'ttl_backfilled': bool(time_state_of(row).get('ttl_backfilled')),
        'freshness': freshness_of(row),
        'reputation_status': reputation_status(row),
        'reputation_sources': ','.join(item.get('zone', '') for item in dnsbl
                                       if isinstance(item, dict) and item.get('status') == 'listed'),
    }


#: The policy a reader applies when a snapshot does not declare one.
READ_POLICY = core.Policy(max_age_seconds=core.DEFAULT_MAX_AGE_SECONDS,
                          allow_missing_identity=True)


#: How a query flag is spelled when it arrives from a URL.  ``apiv1`` already
#: coerces the declared boolean parameters, so these are the values a caller
#: may still send by hand and the values a test passes in.
TRUE_VALUES = ('1', 'true', 'yes', 'on')


def _endpoint_values(body):
    """The endpoint list of a request body, in the shape the route declares.

    ``endpoint_ids`` is declared as a string, so a caller sends
    ``"id1,id2"``; a list is accepted too because the same helper serves the
    internal callers that already hold one.  Both spellings go through the one
    parser, so a value never reaches the membership writer half-read.
    """
    raw = (body or {}).get('endpoint_ids')
    if raw is None:
        return []
    items = raw if isinstance(raw, (list, tuple)) else str(raw).replace('\n', ',').split(',')
    return [str(item).strip() for item in items if str(item).strip()]


def _flag(query, name):
    """Whether a declared boolean query parameter was asked for."""
    value = (query or {}).get(name)
    if isinstance(value, str):
        return value.strip().lower() in TRUE_VALUES
    return bool(value)


#: The stable code of each time state (CONTRACTS §5.4, domain TIME).  ``core``
#: classifies the clock; this is the one place its names become codes, so the
#: API, the CLI and the report never spell the same state two ways.
TIME_STATE_CODES = {
    core.TIME_OK: None,
    core.TIME_UNKNOWN: 'E_TIME_UNKNOWN',
    core.TIME_FUTURE: 'E_TIME_FUTURE',
    core.TIME_TTL_MISSING: 'E_TIME_TTL_MISSING',
    core.TIME_EXPIRED: 'E_TIME_TTL_EXPIRED',
    core.CLOCK_ROLLBACK: 'E_TIME_CLOCK_ROLLBACK',
}

#: The four explicit freshness views a mixed-age table needs (defect 3).
FRESHNESS_BY_STATE = {
    core.TIME_OK: 'fresh', core.TIME_EXPIRED: 'expired', core.TIME_TTL_MISSING: 'unknown',
}


def time_state_of(row, now=None):
    """The one admission-time decision about a row's clock, from ``core``.

    ``freshness`` and ``time_state`` are two spellings of this one answer, so
    they are derived together here.  Reading ``time_state`` off the stored row
    while recomputing ``freshness`` let a row say ``time_ok`` and
    ``freshness='expired'`` at the same moment -- a consumer comparing the two
    fields saw a contradiction that neither meant.
    """
    return core.time_state_of(row, time.time() if now is None else now, READ_POLICY)


def freshness_of(row, now=None):
    """Which freshness view a row belongs to, decided by ``core`` alone.

    ``stale`` used to be one boolean meaning "no usable proxy anywhere"; a table
    of mixed-age rows needs the four explicit views instead (defect 3).  The
    decision itself is :func:`core.time_state_of`, including the documented
    backfill of a legacy row that carries no recorded lifetime -- a reader must
    not grow its own idea of how long a result is good (CONTRACTS §2.4, §4.3).
    """
    return FRESHNESS_BY_STATE.get(time_state_of(row, now)['state'], 'unknown')


class Exports:
    """One coherent current generation, reloaded when its pointer or files change.

    The pointer names a generation and nothing else: a consumer that pinned a
    generation keeps reading that one, and a new publication does not move it
    (CONTRACTS §1.2 rule 3, defect 8).  The re-read key is the generation plus
    its manifest digest, not an mtime, and a generation whose manifest does not
    verify is *refused* rather than served half-checked (CONTRACTS §4.2).
    """

    def __init__(self, directory, generation=None):
        self.directory = Path(directory)
        self.generation = generation
        self.lock = threading.Lock()
        self.key = None
        self.revision = 0
        self.visible = None
        self.rows = []
        self.status = {}
        self.detail = None
        self.reader_state = 'missing'

    def _refuse(self, key, detail):
        """Refuse a publication and say so, instead of returning an empty list."""
        self._clear(key, detail)
        return [], {'reader_state': self.reader_state, 'reader_detail': self.detail,
                    'state': 'error', 'state_detail': detail, 'available': 0,
                    'exported': 0, 'stale': False}

    def _clear(self, key=None, detail=None):
        self.key = key
        self.revision += 1
        self.visible = None
        self.rows = []
        self.status = {}
        self.detail = detail
        self.reader_state = 'broken' if detail else 'missing'

    def load(self):
        """Return the admitted rows of the pinned generation and its status.

        A single expired row no longer empties the answer: the set lives as long
        as its newest member, and every row carries its own reason
        (defect 3, R02).
        """
        manifest = proxytool_export_manifest(self.directory)
        pointer_generation = manifest.get('generation') if manifest else None
        generation = self.generation or pointer_generation
        with self.lock:
            if not generation:
                # A pre-generation installation has no pointer at all.  Its
                # mutable root files are the snapshot until the first immutable
                # generation exists; the state says so instead of pretending
                # the export completed.
                return self._load_legacy_root(pointer_generation)
            # The re-read key is the generation plus the checksums the pointer
            # published, so a file edited behind the reader's back is a new key
            # and is then refused by the manifest check (CONTRACTS §4.2).
            key = (generation, _manifest_digest(manifest),
                   _read_stamp(self.directory, generation))
            if key != self.key:
                try:
                    snapshot = exportsvc.load_snapshot(self.directory, generation=generation, verify=True)
                except exportsvc.ExportError as exc:
                    # A generation that cannot be verified is a broken
                    # publication, not a licence to serve something else.
                    return self._refuse(key, exc.code)
                except (OSError, ValueError, TypeError):
                    return self._refuse(key, 'E_STATE_NO_SNAPSHOT')
                self.rows = [public_row(dict(row)) for row in snapshot.rows]
                self.status = snapshot.status.as_dict()
                self.detail = None
                self.reader_state = 'ok'
                self.key = key
            rows = [dict(row, freshness=freshness_of(row)) for row in self.rows]
            admitted = [row for row in rows if row['freshness'] == 'fresh']
            visible = (self.key, tuple(row['proxy'] for row in admitted))
            if visible != self.visible:
                self.visible = visible
                self.revision += 1
            status = dict(self.status)
            if status:
                _apply_set_state(status, admitted, rows)
            status['reader_state'] = self.reader_state
            status['reader_detail'] = self.detail
            return admitted, status
    def _load_legacy_root(self, pointer_generation):
        """Read a pre-generation installation: root files, and an honest state."""
        root = self.directory
        if (root / 'generations').is_dir() or (root / 'current.json').exists():
            # Generations exist but the pointer does not resolve: that is a broken
            # publication, and silently serving something else is defect 9.
            return self._refuse((pointer_generation, 'pointer_unreadable'), 'E_STATE_NO_SNAPSHOT')
        key = ('legacy-root', _root_stamp(root))
        if key == self.key:
            return [dict(row, freshness=freshness_of(row)) for row in self.rows
                    if freshness_of(row) == 'fresh'], dict(self.status)
        try:
            rows = json.loads((root / 'ranked.json').read_text(encoding='utf-8'))
        except (OSError, UnicodeError, ValueError):
            self._clear(key, 'E_STATE_NO_SNAPSHOT')
            return [], {}
        try:
            status = json.loads((root / 'status.json').read_text(encoding='utf-8'))
        except (OSError, UnicodeError, ValueError):
            # A 2.x installation may have no status at all; the rows are still
            # readable, and the state says the export is not a proven snapshot.
            status = {}
        if not isinstance(rows, list) or not isinstance(status, dict):
            self._clear(key, 'E_STATE_SNAPSHOT_SCHEMA')
            return [], {}
        self.rows = [public_row(dict(row)) for row in rows]
        # A pre-generation status carries no scope counts, so it cannot advertise
        # a completed export; the state says only what is actually known.
        self.status = dict(status, schema_version=status.get('schema_version', 1),
                           stop_reason='legacy', state='partial', complete=False)
        self.detail = None
        self.reader_state = 'legacy'
        self.key = key
        self.revision += 1
        admitted = [dict(row, freshness=freshness_of(row)) for row in self.rows
                    if freshness_of(row) == 'fresh']
        published = dict(self.status)
        _apply_set_state(published, admitted, self.rows)
        published['reader_state'] = self.reader_state
        published['reader_detail'] = self.detail
        return admitted, published




def _root_stamp(root):
    import hashlib
    parts = []
    for name in ('ranked.json', 'status.json'):
        try:
            stat = (root / name).stat()
        except OSError:
            parts.append(f'{name}:missing')
            continue
        parts.append(f'{name}:{stat.st_mtime_ns}:{stat.st_size}')
    return hashlib.sha256('|'.join(parts).encode('utf-8')).hexdigest()


def _manifest_digest(pointer):
    """One identity for a published generation: the checksums it advertises."""
    import hashlib
    manifest = (pointer or {}).get('manifest')
    if not isinstance(manifest, dict) or not manifest:
        return None
    return hashlib.sha256(json.dumps(manifest, sort_keys=True).encode('utf-8')).hexdigest()


def _read_stamp(directory, generation):
    """Did the two files this reader actually reads change since last time?

    The manifest proves a generation is intact; this proves the reader is
    looking at the same bytes it verified last time.  Both are needed: a
    checksum alone would happily keep serving a cached answer after somebody
    edited the file behind the reader's back, and the contract's answer to that
    is a refusal, not a stale cache hit (CONTRACTS §4.2, defect 9).
    """
    root = Path(directory)/'generations'/generation
    parts = []
    for name in ('ranked.json', 'status.json'):
        try:
            stat = (root / name).stat()
        except OSError:
            parts.append(f'{name}:missing')
            continue
        parts.append(f'{name}:{stat.st_size}:{stat.st_mtime_ns}')
    return '|'.join(parts)


def _apply_set_state(status, admitted, rows):
    """Fill the set-level fields the way the contract separates them (§4.3).

    ``state`` describes the run, ``state_detail`` the reason the set is what it
    is, and ``expires_at`` the lifetime of the *set* -- the newest member, never
    ``min()`` over the rows, which is what made one dead address empty a whole
    pool.
    """
    now = time.time()
    status.setdefault('generation', None)
    expires_at = status.get('expires_at')
    if admitted:
        try:
            newest = max(float(row.get('valid_until') or 0) for row in admitted)
            expires_at = max(float(expires_at or 0), newest)
        except (TypeError, ValueError, OverflowError):
            pass
    status['expires_at'] = expires_at
    status['valid_until'] = expires_at
    # A status with no counts of its own keeps them absent rather than inventing
    # zeros: "0 checked" and "we do not know" are different answers.
    status.setdefault('checked', None)
    status.setdefault('scope_candidates', None)
    if not admitted:
        if rows:
            status['state'] = 'stale'
            status['state_detail'] = status.get('state_detail') or 'all_expired'
            status['stale'] = True
        else:
            status['state'] = status.get('state') if status.get('state') != 'stale' else 'empty'
            status['state_detail'] = status.get('state_detail') or 'empty_no_match'
            status['stale'] = False
        return
    status['stale'] = False
    try:
        status['complete'] = bool(status.get('complete')) and float(expires_at or 0) > now
    except (TypeError, ValueError, OverflowError):
        status['complete'] = False
    if status.get('state') == 'stale':
        status['state'] = 'complete' if status['complete'] else 'partial'
        status['state_detail'] = 'ok'
    status.setdefault('state_detail', 'ok')
    status.setdefault('available', len(admitted))


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
    freshness = values.get('freshness', 'fresh') or 'fresh'
    if freshness not in FRESHNESS_MODES:
        raise ValueError('freshness: fresh, expired, unknown or all')
    return dict(protocol=protocol, countries=countries, anonymity=minimum, max_latency=max_latency,
                min_mbps=min_mbps, limit=limit, format=fmt, hosting=hosting, freshness=freshness)


def policy_of(query, *, min_success=2/3, strict=False, denylist=None, max_age_seconds=None):
    """The one admission policy the read paths share (CONTRACTS §2.3, F18)."""
    return core.Policy(
        max_age_seconds=float(max_age_seconds or core.DEFAULT_MAX_AGE_SECONDS),
        min_success=min_success, min_anonymity=query.get('anonymity', 'any'),
        strict=strict, protocol=query.get('protocol', 'all'),
        countries=frozenset(query.get('countries') or ()),
        exclude_hosting=query.get('hosting') == '1',
        max_latency_ms=query.get('max_latency') or None,
        denied=frozenset(denylist.proxies) if denylist else frozenset(),
        deny_match=denylist.match if denylist else None,
        allow_missing_identity=True, protocol_of=proxy_protocol)


def select(rows, query, *, denylist=None, now=None):
    """The rows a query selects, decided by the one admission contract.

    Every surface that shows a list -- CLI ``get``, the GUI table and this API --
    calls this function, so one scope gives one set (F18, F29 acceptance 7).
    """
    now = time.time() if now is None else now
    policy = policy_of(query, denylist=denylist)
    rank = anonymity.LEVEL_RANK
    wanted = query.get('freshness', 'fresh')
    keep_min_mbps = query.get('min_mbps') or 0
    max_latency = float(query.get('max_latency') or 0)
    hosting = query.get('hosting')
    selected = []
    for row in rows:
        if wanted != 'all' and freshness_of(row, now) != wanted:
            continue
        if keep_min_mbps and (row.get('mbps') or 0) < keep_min_mbps:
            continue
        if max_latency:
            latency = row.get('latency_ms')
            if latency is None or latency > max_latency:
                continue
        if hosting in ('0', '1') and bool(row.get('hosting')) is not (hosting == '1'):
            continue
        if query.get('anonymity', 'any') != 'any' and \
                rank.get(row['anonymity'] or 'unknown', -1) < rank[query['anonymity']]:
            continue
        if query.get('countries') and row['country'] not in query['countries']:
            continue
        if policy.protocol and policy.protocol != 'all' and row['protocol'] != policy.protocol:
            continue
        selected.append(row)
    return selected


# ---------------------------------------------------------------------------
# The service layer /v1 is built on (F18)
# ---------------------------------------------------------------------------


class WorkbenchService(apiv1.Service):
    """The engine behind ``/v1``: one read of the pinned generation, one scope.

    Every operation that answers "which rows exist" goes through the same
    :class:`Exports` reader and the same admission policy the CLI and the GUI
    use.  An operation that is not wired raises ``NotImplementedError``, which
    ``apiv1`` turns into an honest ``E_SERVICE_UNAVAILABLE`` naming the
    operation -- never a fabricated empty answer.
    """

    REQUIRED = apiv1.Service.REQUIRED

    def __init__(self, data, exports=None, key_store=None, db_path=None, clock=None):
        self.data = Path(data)
        self.exports = exports or Exports(self.data / 'exports')
        self.db_path = Path(db_path) if db_path else self.data / 'proxies.sqlite3'
        self.keys = key_store
        self.clock = clock or time.time
        self._lock = threading.RLock()
        self.started = self.clock()
        self.reservations = Reservations(self.clock)

    # -- helpers -----------------------------------------------------------

    def connection(self):
        """A read-only connection; the API never writes to the database."""
        if not self.db_path.is_file():
            return None
        try:
            conn = sqlite3.connect(self.db_path.as_uri()+'?mode=ro', uri=True, timeout=2)
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.Error:
            return None

    def published_profile_id(self):
        """The profile the published snapshot names, or a stable placeholder.

        A collect-only run that is not given a profile still has to record a
        scope: an empty id would make the job unaddressable, so the published
        one is used and the caller can always override it.
        """
        profile = self.exports.status.get('profile')
        return str(profile or 'unprofiled')

    def writable_connection(self):
        """A writable connection for the operations a key is allowed to perform.

        The API never writes measurement data, but a management key does create
        and rename collections; that goes through the same schema helpers the
        engine uses, never through raw SQL.
        """
        if not self.db_path.is_file():
            raise apiv1.ApiError('E_STATE_NOT_FOUND', status=409,
                                 details={'reason': 'no data folder'},
                                 action=tr('откройте папку данных программы',
                                           'open the program data folder first'))
        conn = sqlite3.connect(self.db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def scope_of(self, status):
        """The scope a published snapshot declares, never a guess."""
        return core.Scope(str(status.get('collection_id') or ''),
                          str(status.get('profile') or status.get('profile_id') or ''),
                          int(status.get('profile_revision') or 1),
                          str(status.get('network_id') or 'default'))

    def rows_for(self, query=None, wanted=None):
        """The rows a request may see, and the status of the generation they are from.

        ``wanted`` is the set of freshness views the caller asked for; every row
        keeps its own ``freshness`` value and its own reason, so a mixed-age set
        stays mixed instead of being sorted into "there is nothing here"
        (CONTRACTS §4.3, defect 3).
        """
        if wanted is None or wanted == {'fresh'}:
            return self.exports.load()
        rows, status = self.exports.load()
        views = wanted if isinstance(wanted, (set, frozenset)) else {str(wanted)}
        return [row for row in self.exports.rows if row.get('freshness') in views], status

    def page(self, items, stream_id, offset=0, limit=None):
        """One page plus the ``(stream_id, seq)`` cursor the contract defines."""
        start = max(0, int(offset or 0))
        window = items[start:start + limit] if limit else items[start:]
        return {'items': window, 'stream_id': stream_id,
                'cursor_seq': start + len(window) if window else None,
                'next_seq': start + len(window) + 1 if window and len(window) < len(items) else None,
                'total': len(items)}

    def queue_state(self):
        running = 0
        conn = self.connection()
        if conn is not None:
            try:
                running = int(conn.execute(
                    "SELECT count(*) FROM job WHERE state IN ('queued','running','paused')").fetchone()[0])
            except sqlite3.Error:
                running = 0
            finally:
                conn.close()
        return {'depth': running, 'capacity': apiv1.ApiConfig().max_queue_depth,
                'running': running}

    # -- dispatch ----------------------------------------------------------

    def invoke(self, operation, call):
        handler = getattr(self, '_op_' + operation.replace('.', '_'), None)
        if handler is None:
            raise NotImplementedError(operation)
        return handler(call)

    # -- service area ------------------------------------------------------

    def _op_service_state(self, call):
        rows, status = self.exports.load()
        return {'version': PRODUCT_VERSION, 'queue': self.queue_state(),
                'pools': self._pools_summary(), 'generation': status.get('generation'),
                'available': len(rows), 'state': status.get('state'),
                'state_detail': status.get('state_detail'),
                'collection_id': status.get('collection_id'),
                'profile': status.get('profile')}

    def _op_service_version(self, call):
        return {'product': PRODUCT_NAME, 'version': PRODUCT_VERSION,
                'api_version': '1', 'schema_version': exportsvc.SNAPSHOT_SCHEMA_VERSION}

    def _op_service_health(self, call):
        rows, status = self.exports.load()
        return {'state': 'error' if self.exports.reader_state == 'broken' else 'ok',
                'reader_state': self.exports.reader_state, 'detail': self.exports.detail,
                'available': len(rows), 'generation': status.get('generation')}

    def _op_service_capabilities(self, call):
        from . import proxytool as engine
        return {'permissions': sorted(apiv1.PERMISSIONS),
                'sorts': list(SORTS),
                'protocols': list(PROTOCOLS), 'formats': list(FORMATS),
                'freshness': list(FRESHNESS_MODES),
                'snapshot_schema_versions': list(exportsvc.SUPPORTED_SCHEMA_VERSIONS),
                # What is really measured, and what is declared unsupported
                # (F20): a websocket, a long connection, media, UDP or HTTP/3 is
                # reported as unprobed rather than as a passing GET.
                'probes': engine.capability_manifest(),
                # The one country criterion every surface filters with (F08).
                'country': {'basis': 'endpoint', 'unknown': 'exclude',
                            'digest': engine.criterion_digest()},
                'network': {'bind': 'loopback'}}

    def _op_service_readiness(self, call):
        rows, _status = self.exports.load()
        return {'ready': self.exports.reader_state == 'ok', 'available': len(rows),
                'reader_state': self.exports.reader_state}

    def _op_service_queue(self, call):
        return self.queue_state()

    # -- results -----------------------------------------------------------

    def _result_page(self, call, rows, status, stream_kind='results'):
        limit = min(int(call.query.get('limit') or 100), 1000)
        offset = int(call.query.get('offset') or 0)
        stream_id = f'generation:{status.get("generation") or "none"}'
        page = self.page(list(rows), stream_id, offset, limit)
        page['generation'] = status.get('generation')
        page['state'] = status.get('state')
        page['state_detail'] = status.get('state_detail')
        page['expires_at'] = status.get('expires_at')
        return page

    def _freshness_view(self, query):
        """Which freshness views a request asked for, from the declared parameters.

        The route declares ``include_stale`` and ``include_unknown`` (CONTRACTS
        §5.7: "the unknown and stale modes are explicit parameters, not a silent
        exclusion of rows").  It never declared a ``freshness`` parameter, so
        reading one here used to read a value that could not arrive: expired and
        unknown rows were unreachable over ``/v1`` no matter what the caller
        asked for.  The two flags are the documented spelling, and asking for
        neither is still fresh rows only.
        """
        wanted = {'fresh'}
        if _flag(query, 'include_stale'):
            wanted.add('expired')
        if _flag(query, 'include_unknown'):
            wanted.add('unknown')
        return wanted

    def _op_results_list(self, call):
        wanted = self._freshness_view(call.query)
        rows, status = self.rows_for(call.query, wanted)
        rows = [row for row in rows if self._matches(row, call.query)]
        return self._result_page(call, self._guard_objects(rows, call, 'collection_id', 'collections'),
                                 status)

    def _op_results_random(self, call):
        rows, status = self.rows_for(call.query, self._freshness_view(call.query))
        rows = [row for row in rows if self._matches(row, call.query)]
        rows = self._guard_objects(rows, call, 'collection_id', 'collections')
        limit = min(int(call.query.get('limit') or 1), 1000)
        picked = random.sample(rows, min(len(rows), limit)) if rows else []
        return {'items': picked, 'stream_id': f'generation:{status.get("generation") or "none"}',
                'next_seq': None, 'generation': status.get('generation')}

    def _op_results_top(self, call):
        limit = min(int(call.query.get('limit') or 10), 1000)
        rows, status = self.rows_for(call.query, self._freshness_view(call.query))
        rows = [row for row in rows if self._matches(row, call.query)]
        return self._result_page(call,
                                 self._guard_objects(rows, call, 'collection_id', 'collections')[:limit],
                                 status)

    def _find_row(self, wanted):
        """The published row a path segment names, or ``None``.

        A path segment cannot carry a full address: `ID_PATTERN` allows
        `[A-Za-z0-9._:-]` and every scheme contains `://`.  A row is therefore
        addressed by its `endpoint_id` (published for this) or by `host:port`;
        the full URL is still accepted for a caller that percent-encoded it, and
        a row that names none of the three is simply not found.

        The snapshot is loaded first: reading `exports.rows` straight off the
        reader returned whatever was cached when the server started, so a row
        published after the process came up was not found at all.
        """
        text = str(wanted or '').strip()
        if not text:
            return None
        self.exports.load()
        candidates = {text}
        if '://' not in text:
            candidates.add('http://' + text)
            candidates.add('https://' + text)
            candidates.add('socks5://' + text)
        for row in self.exports.rows:
            if row.get('endpoint_id') == text or row.get('proxy') in candidates:
                return row
        return None

    def _guarded_row(self, call, wanted):
        """One published row, refused when the key's scope does not name it."""
        row = self._find_row(wanted)
        if row is None:
            raise apiv1.ApiError('E_STATE_NOT_FOUND', status=404, details={'id': wanted})
        if not self._guard_objects([row], call, 'collection_id', 'collections'):
            # Same code as a missing row: a key must not learn that an
            # out-of-scope object exists (CONTRACTS §5.3).
            raise apiv1.ApiError('E_STATE_NOT_FOUND', status=404, details={'id': wanted})
        return row

    def _op_results_detail(self, call):
        row = self._guarded_row(call, call.params.get('id'))
        return {'item': row, 'generation': self.exports.status.get('generation')}

    def _op_results_observations(self, call):
        wanted = call.params.get('id')
        row = self._guarded_row(call, wanted)
        endpoint = row.get('endpoint_id')
        if not endpoint:
            conn = self.connection()
            try:
                endpoint = schema_endpoint_id(conn, row['proxy']) if conn is not None else None
            finally:
                if conn is not None:
                    conn.close()
        conn = self.connection()
        try:
            if conn is None:
                return {'items': [], 'stream_id': 'observations', 'next_seq': None}
            rows = conn.execute(
                'SELECT o.* FROM observations o WHERE o.endpoint_id = ?'
                ' ORDER BY o.finished_at DESC LIMIT 200', (endpoint,)).fetchall()
            items = [dict(item) for item in rows]
        except sqlite3.Error:
            items = []
        finally:
            if conn is not None:
                conn.close()
        return {'items': items, 'stream_id': f'observations:{endpoint}', 'next_seq': None}

    def _matches(self, row, query):
        if not query:
            return True
        protocol = query.get('protocol')
        if protocol and protocol != 'all' and row.get('protocol') != protocol:
            return False
        country = query.get('country')
        if country and row.get('country') not in str(country).split(','):
            return False
        return True

    # -- status and exports -------------------------------------------------

    def _op_service_status(self, call):
        rows, status = self.exports.load()
        body = dict(status)
        body['available'] = len(rows)
        body['rows_total'] = len(self.exports.rows)
        body['endpoints'] = ENDPOINTS
        body['service'] = PRODUCT_NAME
        body['version'] = PRODUCT_VERSION
        return body

    def _guarded_artifact(self, call, artifact_id):
        """The artifact, but only when the key's scope names its collection.

        The three artifact reads declared no ``scope=`` on their routes and looked
        the row up by primary key alone, so a key restricted to collection A read
        and downloaded the artifact of collection B.  The backstop guard could not
        catch it: it inspects the top level of the answer, and these operations
        return ``{"item": {...}}`` (F29, acceptance 5).
        """
        artifact = self._artifact(artifact_id)
        if not self._guard_objects([artifact], call, 'collection_id', 'collections'):
            raise apiv1.ApiError('E_STATE_NOT_FOUND', status=404, details={'id': artifact_id},
                                 action=apiv1.tr('объект вне scope ключа',
                                                  'the object is outside the key scope'))
        return artifact

    def _op_exports_status(self, call):
        return {'item': self._guarded_artifact(call, call.params.get('id'))}

    def _op_exports_compatibility(self, call):
        artifact = self._guarded_artifact(call, call.params.get('id'))
        return {'item': dict(artifact, compat=artifact.get('compat') or {})}

    def _op_exports_download(self, call):
        """Serve one file of a recorded artifact, and only that file.

        ``export_artifact`` (migration 11) records the *generation*, not a
        directory, so the directory is derived from the generation the export
        published.  Reading ``artifact['directory']`` here raised a ``KeyError``
        and the download answered 500 for every artifact the API had just
        written.  The manifest recorded in the same row is the allow-list: a
        name that is not in it is a 404 even if a file of that name exists.
        """
        artifact = self._guarded_artifact(call, call.params.get('id'))
        generation = str(artifact.get('generation') or '')
        if not generation or not exportsvc.GENERATION_PATTERN.fullmatch(generation):
            raise apiv1.ApiError('E_STATE_NOT_FOUND', status=404,
                                 details={'id': call.params.get('id'), 'reason': 'no generation'})
        directory = self.exports.directory / 'generations' / generation
        name = str(call.params.get('name') or '')
        recorded = (artifact.get('manifest_json') or {})
        if name not in recorded:
            raise apiv1.ApiError('E_STATE_NOT_FOUND', status=404,
                                 details={'name': name, 'files': sorted(recorded)})
        target = directory / name
        if not target.is_file() or target.resolve().parent != directory.resolve():
            raise apiv1.ApiError('E_STATE_NOT_FOUND', status=404, details={'name': name})
        return {'data': target.read_bytes(), 'filename': target.name,
                'content_type': _artifact_content_type(name),
                # The generation's own status records whether the artifact was
                # cut with credential references in it.  A redacted identity (a
                # subscription, the legacy token) is refused such an artifact by
                # ``apiv1`` instead of receiving bytes it may not read.
                'secrets': _artifact_is_secret(directory, name)}

    def _op_checks_collect(self, call):
        """Accept a collection run and hand it to the job store.

        The engine runs the work in its own process; what this returns is the
        durable job record, so the same idempotency key can be replayed and a
        client can watch the run without owning it (F11, F29 acceptance 3).
        """
        from . import jobs as jobs_module

        body = call.body or {}
        collection_id = str(body.get('collection_id') or schema_public_collection())
        # A collect run still measures something, so the scope names the profile
        # it runs under; the published one is the default rather than a blank.
        profile_id = str(body.get('profile_id') or self.published_profile_id())
        revision = int(body.get('profile_revision') or 1)
        store, conn = self._job_store()
        if store is None:
            raise apiv1.ApiError('E_STATE_NOT_FOUND', status=409,
                                 details={'reason': 'no writable database'},
                                 action=tr('откройте папку данных программы',
                                           'open the program data folder first'))
        try:
            scope = jobs_module.Scope(collection_id=collection_id, profile_id=profile_id,
                                      profile_revision=revision,
                                      filters={'mode': str(body.get('mode') or 'collect_only'),
                                               'want': int(body.get('want') or 0)})
            job = store.submit('collect', scope, (), idempotency_key=call.idempotency_key)
        except jobs_module.JobError as exc:
            raise apiv1.ApiError('E_CONFLICT_BUSY', status=409,
                                 details={'reason': str(exc)},
                                 action=tr('дождитесь окончания текущего задания',
                                           'wait for the running job to finish')) from None
        finally:
            _close(conn)
        return {'job_id': job.id, 'state': job.state, 'collection_id': collection_id,
                'created_at': job.created_at}

    def _op_checks_recheck(self, call):
        raise apiv1.ApiError('E_STATE_NOT_FOUND', status=409,
                             details={'reason': 'recheck runs on the GUI or the CLI worker'},
                             action=tr('запустите перепроверку через GUI или CLI',
                                       'start the recheck from the GUI or the CLI'))

    def _profile_store(self):
        from . import profiles as profiles_module
        try:
            conn = self.writable_connection()
        except apiv1.ApiError:
            return None, None
        try:
            profiles_module.verify_schema(conn)
            return profiles_module.ProfileStore(conn), conn
        except Exception:
            _close(conn)
            return None, None

    def _op_profiles_create(self, call):
        from . import profiles as profiles_module
        store, conn = self._profile_store()
        if store is None:
            raise apiv1.ApiError('E_STATE_NOT_FOUND', status=409,
                                 details={'reason': 'no writable database'},
                                 action=tr('откройте папку данных программы',
                                           'open the program data folder first'))
        body = call.body or {}
        name = str(body.get('name') or '').strip()
        try:
            spec = profiles_module.create_spec(**spec_of(body))
            ref = store.create(name, spec)
        except profiles_module.ProfileError as exc:
            raise apiv1.ApiError('E_VALIDATION_FIELD', action=str(exc),
                                 details={'name': name}) from None
        finally:
            _close(conn)
        return {'id': ref.profile_id, 'revision': ref.revision, 'name': name}

    def _op_profiles_update_version(self, call):
        from . import profiles as profiles_module
        store, conn = self._profile_store()
        if store is None:
            raise apiv1.ApiError('E_STATE_NOT_FOUND', status=409,
                                 details={'reason': 'no writable database'},
                                 action=tr('откройте папку данных программы',
                                           'open the program data folder first'))
        identifier = str(call.params.get('id') or '')
        profile_id, _, revision = identifier.partition('@')
        body = call.body or {}
        try:
            current = store.head(profile_id)
            if call.expected_revision is not None and \
                    int(call.expected_revision) != int(current.revision):
                # The caller edited an older version: refuse instead of
                # overwriting what it never saw (F05, CONTRACTS §6.4).
                raise apiv1.ApiError(
                    'E_CONFLICT_REVISION',
                    details={'expected': call.expected_revision, 'current': current.revision,
                             'profile_id': profile_id},
                    action=tr('перечитайте профиль и повторите',
                              'read the profile again and retry'))
            ref = store.update(profile_id, profiles_module.create_spec(**spec_of(body)),
                               base_revision=current.revision, name=body.get('name'))
        except profiles_module.ProfileError as exc:
            raise apiv1.ApiError('E_VALIDATION_FIELD', action=str(exc),
                                 details={'profile_id': profile_id}) from None
        finally:
            _close(conn)
        return {'id': ref.profile_id, 'revision': ref.revision,
                'name': body.get('name') or ref.profile_id,
                'profile_id': ref.profile_id, 'profile_revision': ref.revision}

    def _op_exports_create(self, call):
        """Create an export artifact, through the engine's own export path.

        This route used to answer 409 and tell the caller to use the GUI or the
        CLI, which left the key the bootstrap hands out unable to finish a
        check without a manual step.  An administrator with a key must be able
        to import, check, read and export without leaving the API.

        Only ``proxytool.export`` builds a generation, and only it decides
        whether the active pointer moves: ``kind='published'`` publishes,
        ``kind='selection'`` and ``kind='diagnostic'`` are separate artifacts
        that never touch the active pool (CONTRACTS §4.5, defect 7).
        """
        from . import proxytool as engine

        def action(workbench):
            body = call.body or {}
            kind = str(body.get('kind') or 'published')
            if kind not in exportsvc.ARTIFACT_KINDS:
                raise apiv1.field_error('kind', tr(
                    f'вид артефакта: {", ".join(exportsvc.ARTIFACT_KINDS)}',
                    f'artifact kind: {", ".join(exportsvc.ARTIFACT_KINDS)}'))
            conn = workbench.conn
            profile = str(body.get('profile_id') or self.published_profile_id())
            if not conn.execute('SELECT 1 FROM profiles WHERE id=?', (profile,)).fetchone():
                row = conn.execute('SELECT id FROM profiles ORDER BY created_at DESC LIMIT 1').fetchone()
                if row is None:
                    raise apiv1.ApiError(
                        'E_STATE_NO_SNAPSHOT', status=409,
                        details={'reason': 'no check profile has been measured yet'},
                        action=tr('запустите проверку, затем повторите экспорт',
                                  'run a check, then export again'))
                profile = str(row[0])
            collection = str(body.get('collection_id') or schema_public_collection())
            # ``endpoint_ids`` is the selection: named endpoints become a
            # ``kind='selection'`` artifact that never moves the active pool.
            chosen = _endpoint_values(body)
            if chosen and kind == 'published':
                kind = 'selection'
            selection = self._canonical_selection(workbench, chosen) if chosen else None
            client_target = str(body.get('format') or '') or None
            # `include_secrets` asks for credential *references*, never values: the
            # mode name is `reference`, and it was `include` -- a value outside
            # `CREDENTIALS_MODES`, so `ExportOptions` refused the request after the
            # `export.secret` check had already passed and the caller saw a 500
            # instead of an artifact (F29, F28).
            wants_secrets = bool(body.get('include_secrets'))
            report = engine.export(
                conn, profile, workbench.data / 'exports',
                collection_id=collection,
                profile_revision=int(body.get('profile_revision') or 1),
                allowed_proxies=selection,
                diagnostic=kind == 'diagnostic',
                credentials=(exportsvc.CREDENTIALS_REFERENCE if wants_secrets
                             else exportsvc.CREDENTIALS_REDACT),
                secret_grant=exportsvc.SecretGrant(
                    allowed=wants_secrets, issued_by=call.principal.key_id
                    if call.principal is not None else None) if wants_secrets else None,
                active_profile_path=None if kind == 'selection' else workbench.data / 'last-profile.txt')
            written = list(report.get('files') or ())
            if client_target:
                wanted = EXPORT_FORMAT_FILES.get(client_target)
                if wanted is None or wanted not in written:
                    raise apiv1.ApiError(
                        'E_VALIDATION_FIELD', status=400,
                        details={'format': client_target, 'file': wanted, 'files': written},
                        action=tr('выберите формат из написанных в артефакте',
                                   'choose a format that the artifact carries'))
            answer = {key: report[key] for key in ('kind', 'generation', 'state', 'stop_reason',
                                                   'state_detail', 'exported', 'valid_until',
                                                   'expires_at', 'max_age_seconds', 'complete',
                                                   'files', 'directory', 'collection_id',
                                                   'profile', 'profile_revision', 'compat')
                      if key in report}
            answer['format'] = client_target or 'txt'
            answer['file'] = EXPORT_FORMAT_FILES.get(client_target or 'txt')
            answer['artifact_id'] = self._artifact_id_of(report)
            answer['job_id'] = self._record_job(workbench, f'export:{kind}', collection,
                                                int(report.get('exported') or 0))
            return answer
        return self._with_workbench(action)

    def _canonical_selection(self, workbench, values):
        """Endpoint ids or canonical addresses as canonical addresses.

        Object-level scope is checked by the route guard before this runs, so a
        caller cannot name an endpoint outside its collections and get it
        exported under someone else's scope.
        """
        conn = workbench.conn
        resolved = []
        for value in values:
            row = conn.execute('SELECT canonical FROM endpoints WHERE id=? OR canonical=?',
                               (value, value)).fetchone()
            if row is not None:
                resolved.append(str(row[0]))
        if not resolved:
            raise apiv1.ApiError('E_STATE_NOT_FOUND', status=404,
                                 details={'endpoint_ids': values[:20]},
                                 action=tr('укажите существующие адреса', 'name existing endpoints'))
        return resolved

    def _artifact_id_of(self, report):
        """The ``export_artifact`` row the export just recorded, if it is there."""
        conn = self.connection()
        try:
            row = conn.execute('SELECT id FROM export_artifact WHERE generation=? ORDER BY published_at DESC '
                               'LIMIT 1', (report.get('generation'),)).fetchone() if conn is not None else None
        except sqlite3.Error:
            row = None
        finally:
            _close(conn)
        return str(row[0]) if row is not None else None

    def _artifact(self, artifact_id):
        conn = self.connection()
        try:
            row = conn.execute('SELECT * FROM export_artifact WHERE id=?',
                               (artifact_id,)).fetchone() if conn is not None else None
        except sqlite3.Error:
            row = None
        finally:
            if conn is not None:
                conn.close()
        if row is None:
            raise apiv1.ApiError('E_STATE_NOT_FOUND', status=404, details={'id': artifact_id})
        item = dict(row)
        try:
            item['manifest_json'] = json.loads(item.get('manifest_json') or '{}')
        except (TypeError, ValueError):
            item['manifest_json'] = {}
        return item

    # -- collections --------------------------------------------------------

    def _op_collections_list(self, call):
        conn = self.connection()
        try:
            rows = [dict(row) for row in _schema_rows(conn, 'collections')]
        finally:
            _close(conn)
        items = self._guard_objects(rows, call, 'id', 'collections')
        return {'items': items, 'stream_id': 'collections', 'next_seq': None}

    def _op_collections_get(self, call):
        wanted = call.params.get('id')
        conn = self.connection()
        try:
            rows = [dict(row) for row in _schema_rows(conn, 'collections')]
        finally:
            _close(conn)
        for row in rows:
            if row.get('id') == wanted:
                return self._guard_objects([row], call, 'id', 'collections')[0]
        raise apiv1.ApiError('E_STATE_NOT_FOUND', status=404, details={'id': wanted})

    def _op_collections_create(self, call):
        from . import db as schema
        body = call.body or {}
        name = str(body.get('name') or '').strip()
        if not name:
            raise apiv1.field_error('name', tr('имя коллекции обязательно',
                                                'a collection needs a name'))
        conn = self.writable_connection()
        try:
            identifier = schema.create_collection(
                conn, name, kind=collection_kind(body.get('kind')))
            conn.commit()
        except schema.DbError as exc:
            raise apiv1.ApiError('E_VALIDATION_FIELD', action=str(exc), details={'name': name}) from None
        finally:
            _close(conn)
        return {'id': identifier, 'name': name, 'kind': collection_kind(body.get('kind')),
                'created_at': time.time()}

    def _op_collections_update(self, call):
        from . import db as schema
        identifier = call.params.get('id')
        body = call.body or {}
        expected = call.expected_revision
        conn = self.writable_connection()
        try:
            row = conn.execute('SELECT id, name FROM collections WHERE id=?',
                               (identifier,)).fetchone()
            if row is None:
                raise apiv1.ApiError('E_STATE_NOT_FOUND', status=404, details={'id': identifier})
            if expected is not None:
                # ``collections`` carries no revision column (CONTRACTS §3.3,
                # migration 2), so a supplied revision cannot be proven current
                # and is refused rather than assumed.  See the handoff to db.py.
                raise apiv1.ApiError(
                    'E_CONFLICT_REVISION',
                    details={'expected': expected, 'stored': None,
                             'reason': 'collections has no revision column'},
                    action=tr('повторите без ревизии или обновите схему',
                              'retry without a revision, or migrate the schema'))
            if body.get('name'):
                schema.rename_collection(conn, identifier, str(body['name']))
            conn.commit()
            fresh = conn.execute('SELECT id, name, kind, created_at, archived_at FROM collections '
                                 'WHERE id=?', (identifier,)).fetchone()
        except schema.DbError as exc:
            raise apiv1.ApiError('E_VALIDATION_FIELD', action=str(exc)) from None
        finally:
            _close(conn)
        return dict(fresh)

    def _op_collections_archive(self, call):
        from . import db as schema
        identifier = call.params.get('id')
        conn = self.writable_connection()
        try:
            schema.archive_collection(conn, identifier)
            conn.commit()
        except schema.DbError as exc:
            raise apiv1.ApiError('E_STATE_NOT_FOUND', status=404,
                                 details={'id': identifier}, action=str(exc)) from None
        finally:
            _close(conn)
        return {'id': identifier, 'archived': True}

    def _op_collections_members(self, call):
        wanted = call.params.get('id')
        conn = self.connection()
        try:
            rows = conn.execute(
                'SELECT e.id AS endpoint_id, e.canonical AS canonical, m.origin AS origin, '
                'm.added_at AS added_at FROM membership m JOIN endpoints e ON e.id = m.endpoint_id '
                'WHERE m.collection_id = ? ORDER BY e.canonical', (wanted,)).fetchall()
            items = [dict(row) for row in rows]
        except sqlite3.Error:
            items = []
        finally:
            _close(conn)
        return {'items': self._guard_objects(items, call, 'endpoint_id', 'collections'),
                'stream_id': f'collection:{wanted}', 'next_seq': None}

    def _guard_objects(self, items, call, field, kind, collection_field='collection_id'):
        """Never reveal an object the caller's resource scope does not name.

        A key's scope names collections and pools (CONTRACTS §5.3).  An object of
        any other kind -- a job, a schedule -- is visible when the collection it
        belongs to is in the scope.  Before this rule the filter asked
        :func:`_scope_values` for a kind the principal does not carry, was told
        ``None`` and read it as "unrestricted", so a key scoped to one collection
        received every other collection's job list (F29, acceptance 5).
        """
        allowed = _scope_values(call.principal, kind)
        if allowed is None:
            return items
        if kind not in SCOPE_KINDS:
            allowed = _scope_values(call.principal, 'collections') or frozenset()
            return [item for item in items if _collection_of(item, collection_field) in allowed]
        return [item for item in items if item.get(field) in allowed]

    # -- profiles -----------------------------------------------------------

    def _op_profiles_list(self, call):
        conn = self.connection()
        try:
            rows = [dict(row) for row in conn.execute(
                'SELECT id, name, revision, parent_id, digest, created_at, archived_at, is_default '
                'FROM profiles WHERE name IS NOT NULL ORDER BY created_at, id')] if conn is not None else []
        except sqlite3.Error:
            rows = []
        finally:
            _close(conn)
        return {'items': self._guard_objects(rows, call, 'id', 'profiles'),
                'stream_id': 'profiles', 'next_seq': None}

    def _op_profiles_get(self, call):
        """One profile version, or its head when the caller does not name one.

        Rows are stored as ``<profile_id>@<revision>``, so a bare id addresses
        the head: the newest version, which is the one a reader means.
        """
        from . import profiles as profiles_module
        wanted = str(call.params.get('id') or '')
        profile_id, _, revision = wanted.partition('@')
        store, conn = self._profile_store()
        try:
            if store is None:
                raise apiv1.ApiError('E_STATE_NOT_FOUND', status=404, details={'id': wanted})
            record = store.get(profile_id, int(revision)) if revision else store.head(profile_id)
        except profiles_module.ProfileError as exc:
            raise apiv1.ApiError('E_STATE_NOT_FOUND', status=404, details={'id': wanted},
                                 action=str(exc)) from None
        finally:
            _close(conn)
        item = dict(id=record.ref.profile_id, profile_id=record.ref.profile_id,
                    revision=record.ref.revision, name=record.name, digest=record.digest,
                    created_at=record.created_at, archived_at=record.archived_at,
                    is_default=record.is_default)
        guarded = self._guard_objects([item], call, 'id', 'profiles')
        if not guarded:
            # A forbidden object and a missing one answer alike.
            raise apiv1.ApiError('E_STATE_NOT_FOUND', status=404, details={'id': wanted})
        return guarded[0]

    def _op_profiles_presets(self, call):
        from . import servicecatalog
        items = [dict(item) for item in servicecatalog.list_sets()] \
            if hasattr(servicecatalog, 'list_sets') else []
        return {'items': items, 'stream_id': 'presets', 'next_seq': None}

    # -- jobs ---------------------------------------------------------------

    def _job_store(self):
        """A job store on a writable connection: a job is a durable record.

        Reading the published snapshot stays on the read-only path; recording
        work never does, otherwise "the API never writes" would be true and the
        job would vanish with the process.
        """
        from . import jobs as jobs_module
        try:
            conn = self.writable_connection()
        except apiv1.ApiError:
            return None, None
        try:
            jobs_module.install_schema(conn)
            return jobs_module.JobStore(conn), conn
        except (sqlite3.Error, jobs_module.JobError):
            _close(conn)
            return None, None

    def _op_jobs_list(self, call):
        store, conn = self._job_store()
        try:
            items = [_job_dict(job)
                     for job in store.jobs(limit=min(int(call.query.get('limit') or 50), 500))] \
                if store is not None else []
        finally:
            _close(conn)
        return {'items': self._guard_objects(items, call, 'id', 'jobs'),
                'stream_id': 'jobs', 'next_seq': None}

    def _op_jobs_get(self, call):
        store, conn = self._job_store()
        try:
            job = store.job(call.params.get('id')) if store is not None else None
        except Exception:
            job = None
        finally:
            _close(conn)
        if job is None:
            raise apiv1.ApiError('E_STATE_NOT_FOUND', status=404, details={'id': call.params.get('id')})
        guarded = self._guard_objects([_job_dict(job)], call, 'id', 'jobs')
        if not guarded:
            raise apiv1.ApiError('E_STATE_NOT_FOUND', status=404, details={'id': call.params.get('id')})
        return guarded[0]

    def _op_jobs_events(self, call):
        job_id = str(call.params.get('id') or '')
        store, conn = self._job_store()
        try:
            job = store.job(job_id) if store is not None else None
        except Exception:
            job = None
        finally:
            _close(conn)
        if job is None:
            raise apiv1.ApiError('E_STATE_NOT_FOUND', status=404, details={'id': job_id})
        # A stream is a read: the object check happens before the first frame,
        # not after it.  An empty stream for someone else's job would still be
        # a job they could poll and time.
        if not self._guard_objects([_job_dict(job)], call, 'collection_id', 'collections'):
            raise apiv1.ApiError('E_STATE_NOT_FOUND', status=404, details={'id': job_id})
        store, conn = self._job_store()
        try:
            events = store.events(job_id, after_seq=int(call.query.get('cursor_seq') or 0),
                                  limit=min(int(call.query.get('limit') or 200), 1000)) \
                if store is not None else []
        finally:
            _close(conn)
        items = [_event_dict(event) for event in events]
        return {'items': items, 'stream_id': apiv1_stream(job_id), 'next_seq': None,
                'last_seq': items[-1]['seq'] if items else 0}

    # -- pools --------------------------------------------------------------

    def _pool_store(self):
        from . import pools as pools_module
        conn = self.connection()
        if conn is None:
            return None, None
        try:
            return pools_module.PoolStore(conn), conn
        except (sqlite3.Error, pools_module.PoolError):
            _close(conn)
            return None, None

    def _pools_summary(self):
        store, conn = self._pool_store()
        try:
            return [store.get(spec['id']).__dict__ if hasattr(store.get(spec['id']), '__dict__') else spec
                    for spec in store.list()] if store is not None else []
        except Exception:
            return []
        finally:
            _close(conn)

    def _op_pools_list(self, call):
        store, conn = self._pool_store()
        try:
            items = [_pool_dict(spec) for spec in store.list()] if store is not None else []
        except (sqlite3.Error, Exception):
            items = []
        finally:
            _close(conn)
        return {'items': self._guard_objects(items, call, 'id', 'pools'),
                'stream_id': 'pools', 'next_seq': None}

    def _op_pools_get(self, call):
        store, conn = self._pool_store()
        try:
            spec = store.get(call.params.get('id')) if store is not None else None
        finally:
            _close(conn)
        if spec is None:
            raise apiv1.ApiError('E_STATE_NOT_FOUND', status=404, details={'id': call.params.get('id')})
        return self._guard_objects([_pool_dict(spec)], call, 'id', 'pools')[0]

    def _op_pools_status(self, call):
        store, conn = self._pool_store()
        try:
            status = store.status(call.params.get('id')) if store is not None else None
        except Exception:
            status = None
        finally:
            _close(conn)
        if status is None:
            raise apiv1.ApiError('E_STATE_NOT_FOUND', status=404, details={'id': call.params.get('id')})
        return _status_dict(status)

    def _op_pools_members(self, call):
        store, conn = self._pool_store()
        try:
            # `pools.Member` is a dataclass, not a mapping: `dict(member)` raised
            # `TypeError` and the route answered 500 for every pool, so the
            # membership a refill had just written was never visible over /v1.
            items = [_member_dict(member)
                     for member in store.members(call.params.get('id'))] if store is not None else []
        finally:
            _close(conn)
        return {'items': self._guard_objects(items, call, 'endpoint_id', 'pools'),
                'stream_id': f'pool:{call.params.get("id")}', 'next_seq': None}


    # -- the service layer the modules are reached through ------------------
    #
    # Everything below goes through :class:`proxy_workbench.Workbench`, the one
    # object the CLI and the GUI also use.  An operation that is not wired
    # raises ``NotImplementedError`` and ``apiv1`` turns it into an honest
    # ``E_SERVICE_UNAVAILABLE`` -- never a fabricated empty answer.

    def workbench(self):
        """The engine's own service object, opened on the API's database."""
        from . import proxytool as engine
        return engine.Workbench(self.data, db_path=self.db_path, clock=self.clock)

    def _with_workbench(self, action):
        """Run one action against the engine and translate its refusals.

        A module exception never reaches the client: it becomes the stable
        ``E_*`` code the CLI prints, so one refusal reads the same in a terminal
        and in a JSON body (CONTRACTS §5.4).
        """
        from . import proxytool as engine
        workbench = self.workbench()
        try:
            return action(workbench)
        except engine.WorkbenchError as exc:
            raise apiv1.ApiError(_api_code(exc.code), status=_error_status(exc.code),
                                 message=str(exc)) from None
        except _MODULE_ERRORS as exc:
            code = getattr(exc, 'code', None) or 'E_VALIDATION_FIELD'
            raise apiv1.ApiError(_api_code(code) if isinstance(code, str) and code.startswith('E_')
                                 else 'E_VALIDATION_FIELD',
                                 status=_error_status(code), message=str(exc)) from None
        except (sqlite3.Error, OSError) as exc:
            raise apiv1.ApiError('E_STATE_SNAPSHOT_STATIC', status=503,
                                 message=f'{type(exc).__name__}') from None
        finally:
            workbench.close()

    # -- imports ------------------------------------------------------------

    def _import_source(self, call, workbench):
        """The import source out of a request body, in the route's own shape.

        ``content`` is the field the route documents; a document format the
        importer does not own (a Clash or sing-box config) is the CLI's
        ``import --import-format``, so the two are not two normalizers.
        """
        from . import importer
        body = call.body or {}
        content = body.get('content')
        if not isinstance(content, str) or not content:
            raise apiv1.field_error('content', tr('нужен текст списка', 'the list content is required'))
        return importer.ImportSource.from_text(content, name=str(body.get('name') or 'api-import'),
                                               channel='clipboard')

    def _import_collection(self, call):
        """The collection the route addresses; the body never overrides the path."""
        return str(call.params.get('id') or (call.body or {}).get('collection_id')
                   or schema_public_collection())

    def _import_format(self, call):
        from . import importer
        wanted = str((call.body or {}).get('format') or 'txt')
        return wanted if wanted in importer.FORMATS else None

    def _op_imports_preview(self, call):
        def action(workbench):
            plan = workbench.import_preview(self._import_source(call, workbench),
                                            self._import_collection(call),
                                            mode=str((call.body or {}).get('mode') or 'merge'),
                                            fmt=self._import_format(call))
            return plan.to_dict()
        return self._with_workbench(action)

    def _op_imports_commit(self, call):
        def action(workbench):
            collection = self._import_collection(call)
            plan = workbench.import_preview(self._import_source(call, workbench), collection,
                                            mode=str((call.body or {}).get('mode') or 'merge'),
                                            fmt=self._import_format(call),
                                            idempotency_key=call.idempotency_key)
            if plan.needs_mapping:
                raise apiv1.ApiError('E_VALIDATION_SCHEMA', status=422,
                                     details={'needs_mapping': True})
            report = workbench.import_commit(plan, allow_partial=bool((call.body or {}).get('allow_partial')))
            # The route is an async job: a long operation answers with a job id
            # and never holds the request open (R18).
            body = report.to_dict()
            body['job_id'] = self._record_job(workbench, 'import', collection, len(report.added or ()))
            return body
        return self._with_workbench(action)

    def _record_job(self, workbench, kind, collection_id, items):
        """The job an asynchronous operation reports under.

        The work itself already happened; the job exists so the caller can ask
        after it with the same cursor schema as every other stream, and so a
        repeated idempotency key returns the same id (CONTRACTS §5.7, §6.4).
        """
        from . import jobs as jobs_module
        scope = jobs_module.Scope(collection_id=collection_id,
                                 profile_id=self.published_profile_id(),
                                 profile_revision=1,
                                 profile_digest=self.published_profile_id())
        store = workbench.jobs()
        job = store.submit(kind, scope, (), idempotency_key=workbench.clock_ns())
        store.start(job.id)
        store.finish(job.id, state='succeeded', reason_code=jobs_module.CODE_OK)
        return job.id

    # -- collections: membership -------------------------------------------

    def _op_collections_member_add(self, call):
        return self._with_workbench(lambda workbench: self._membership(call, workbench, add=True))

    def _op_collections_member_remove(self, call):
        return self._with_workbench(lambda workbench: self._membership(call, workbench, add=False))

    def _membership(self, call, workbench, *, add):
        from . import db as schema
        collection = str(call.params.get('id') or schema_public_collection())
        body = call.body or {}
        # The add route names one endpoint, the remove route names it in the
        # path; both end up in the same one-call change list.
        values = _endpoint_values(body)
        if body.get('endpoint'):
            values.append(str(body['endpoint']))
        if call.params.get('endpoint_id'):
            values.append(str(call.params['endpoint_id']))
        values = list(dict.fromkeys(values))
        if not values:
            raise apiv1.field_error('endpoint', tr('нужен адрес', 'an address is required'))
        conn = workbench.conn
        changed = []
        for value in values:
            if add:
                # Adding a member may introduce an address the engine has never
                # seen; the one normalizer still decides what it is.
                endpoint = schema.upsert_endpoint(conn, value)
            else:
                endpoint = schema.endpoint_id(value)
                row = conn.execute('SELECT canonical FROM endpoints WHERE id=?', (endpoint,)).fetchone()
                if row is None:
                    raise apiv1.ApiError('E_STATE_NOT_FOUND', status=404,
                                         details={'endpoint_id': value})
            if add:
                schema.add_member(conn, collection, endpoint,
                                  origin=str(body.get('origin') or 'manual'), now=workbench.clock())
            else:
                conn.execute('DELETE FROM membership WHERE collection_id=? AND endpoint_id=?',
                             (collection, endpoint))
            changed.append(value)
        conn.commit()
        return {'collection_id': collection, 'changed': changed, 'added': bool(add)}

    def _op_collections_merge(self, call):
        return self._apply_collection_mode(call, 'merge')

    def _op_collections_replace(self, call):
        return self._apply_collection_mode(call, 'replace')

    def _apply_collection_mode(self, call, mode):
        def action(workbench):
            source = self._import_source(call, workbench)
            collection = str(call.params.get('id') or schema_public_collection())
            plan = workbench.import_preview(source, collection, mode=mode,
                                            idempotency_key=call.idempotency_key)
            report = workbench.import_commit(plan, allow_partial=bool((call.body or {}).get('allow_partial')))
            return report.to_dict()
        return self._with_workbench(action)

    # -- jobs ---------------------------------------------------------------

    def _job_store_of(self, workbench):
        """The persistent job lifecycle of the engine, on the engine's connection."""
        from . import jobs as jobs_module
        return jobs_module.JobStore(workbench.conn)

    def _op_jobs_pause(self, call):
        return self._with_workbench(lambda workbench: _job_dict(
            self._job_store_of(workbench).pause(call.params.get('id'), reason_code='paused_by_user')))

    def _op_jobs_resume(self, call):
        return self._with_workbench(lambda workbench: _job_dict(
            self._job_store_of(workbench).resume(call.params.get('id'))))

    def _op_jobs_cancel(self, call):
        """Cancel is idempotent and never erases what was already measured."""
        return self._with_workbench(lambda workbench: _job_dict(
            self._job_store_of(workbench).cancel(call.params.get('id'), reason_code='cancelled_by_user',
                                              idempotency_key=call.idempotency_key)))

    def _op_jobs_retry(self, call):
        return self._with_workbench(lambda workbench: _job_dict(
            self._job_store_of(workbench).retry(call.params.get('id'), idempotency_key=call.idempotency_key)))

    def _op_events_system(self, call):
        """The system stream, read the same way as a job stream (CONTRACTS §5.7)."""
        from . import jobs as jobs_module
        def action(workbench):
            store = self._job_store_of(workbench)
            events = []
            for job in store.jobs(limit=50):
                events.extend(store.events(job.id, after_seq=int(call.query.get('cursor') or 0),
                                           limit=int(call.query.get('limit') or 200)))
            events.sort(key=lambda item: item.seq)
            items = [_event_dict(event) for event in events]
            return {'items': items, 'stream_id': 'system', 'next_seq': None,
                    'last_seq': items[-1]['seq'] if items else 0}
        del jobs_module
        return self._with_workbench(action)

    # -- checks as jobs -----------------------------------------------------

    def _op_checks_check(self, call):
        return self._submit_collection_job(call, 'check')

    def _op_checks_quick_test(self, call):
        """A quick test is a job with a budget, not a request that never returns."""
        return self._submit_collection_job(call, 'quick_test')

    def _submit_collection_job(self, call, kind):
        from . import proxytool as engine
        from . import jobs as jobs_module

        def action(workbench):
            body = call.body or {}
            collection = str(body.get('collection_id') or schema_public_collection())
            conn = workbench.conn
            members = [row[0] for row in conn.execute(
                'SELECT e.canonical FROM membership m JOIN endpoints e ON e.id = m.endpoint_id '
                'WHERE m.collection_id=? ORDER BY e.canonical', (collection,)).fetchall()]
            scope = jobs_module.Scope(collection_id=collection,
                                      profile_id=str(body.get('profile_id') or self.published_profile_id()),
                                      profile_revision=int(body.get('profile_revision') or 1),
                                      profile_digest=str(body.get('profile_id') or ''),
                                      filters={'protocol': body.get('protocol', 'all')},
                                      budgets={'max_seconds': body.get('max_seconds'),
                                               'max_requests': body.get('max_requests')})
            items = [jobs_module.QueueItem(endpoint_id=schema_endpoint_id(conn, value),
                                           access_id=engine.PUBLIC_ACCESS_ID, access_revision=1)
                     for value in members]
            conn.commit()
            job = workbench.jobs().submit(kind, scope, items,
                                          idempotency_key=call.idempotency_key)
            return {'job_id': job.id, 'kind': job.kind, 'state': job.state,
                    'collection_id': collection, 'items': len(items),
                    'budget': {'max_seconds': body.get('max_seconds'),
                               'max_requests': body.get('max_requests')}}
        return self._with_workbench(action)

    # -- pools as jobs ------------------------------------------------------

    def _op_pools_create(self, call):
        from . import pools as pools_module

        def action(workbench):
            body = call.body or {}
            pool_id = str(body.get('name') or body.get('id') or '').strip()
            if not pool_id:
                raise apiv1.field_error('name', tr('имя пула обязательно', 'a pool needs a name'))
            spec = workbench.pools().create(
                pool_id, collection_id=str(body.get('collection_id') or schema_public_collection()),
                profile_id=str(body.get('profile_id') or ''),
                desired=int(body.get('desired') or 0), minimum=int(body.get('minimum') or 0),
                reserve=int(body.get('reserve') or 0), policy=body.get('policy') or None)
            return _pool_dict(spec)
        return self._with_workbench(action)

    def _op_pools_update(self, call):
        from . import pools as pools_module

        def action(workbench):
            store = workbench.pools()
            pool_id = str(call.params.get('id') or '')
            spec = store.get(pool_id)
            if spec is None:
                raise apiv1.ApiError('E_STATE_NOT_FOUND', status=404, details={'id': pool_id})
            if call.expected_revision is not None and \
                    int(call.expected_revision) != int(getattr(spec, 'revision', 1) or 1):
                raise apiv1.ApiError('E_CONFLICT_REVISION',
                                     details={'expected': call.expected_revision,
                                              'stored': getattr(spec, 'revision', 1)})
            body = call.body or {}
            if body.get('desired') is not None:
                spec = pools_module.evict(spec, int(body['desired']), pool_id=pool_id) \
                    if False else spec
            if body.get('policy'):
                store.set_policy(pool_id, body['policy'])
            if body.get('desired') is not None or body.get('reserve') is not None or \
                    body.get('minimum') is not None:
                store.set_target(pool_id, desired=body.get('desired'), reserve=body.get('reserve'),
                                 minimum=body.get('minimum'))
            return _pool_dict(store.get(pool_id))
        return self._with_workbench(action)

    def _op_pools_start(self, call):
        """Starting a pool means filling it, not only writing a state word.

        `start` used to save `STATE_EMPTY` and return: the pool stayed 0/desired
        until something else refilled it, and nothing else did (F14, §7.13).  The
        save itself was broken too -- it passed a whole `PoolStatus` where
        `save_status` wants the state string and two keywords, so the route
        answered 500.
        """
        def action(workbench):
            pool_id = str(call.params.get('id'))
            store = workbench.pools()
            started = pools_state_for('start', store.status(pool_id))
            store.save_status(pool_id, started.state,
                              deficit_reason=started.deficit_reason,
                              next_attempt_at=started.next_attempt_at)
            return _status_dict(workbench.pool_refill(pool_id,
                                                      pool_candidate_source(workbench.conn)))
        return self._with_workbench(action)

    def _op_pools_pause(self, call):
        return self._with_workbench(lambda workbench: self._pool_transition(
            workbench, call.params.get('id'), 'pause'))

    def _pool_transition(self, workbench, pool_id, action):
        store = workbench.pools()
        moved = pools_state_for(action, store.status(str(pool_id)))
        store.save_status(str(pool_id), moved.state,
                          deficit_reason=moved.deficit_reason,
                          next_attempt_at=moved.next_attempt_at)
        return _status_dict(store.status(str(pool_id)))

    def _op_pools_refill(self, call):
        """Refill *this* pool and report what it can serve afterwards.

        The route answered 202 and queued a check of whatever collection the body
        named -- by default the public base -- while `call.params['id']`, the pool
        the caller actually asked about, was never read.  `pools.refill` and
        `Workbench.pool_refill` existed and nothing called them, so a pool created
        with `--desired 5` stayed 0/5 and the caller was told "job queued" (F14,
        §7.13 "Maintained N is restored from the reserve/sources").
        """
        def action(workbench):
            from . import pools as pools_module
            status = workbench.pool_refill(
                str(call.params.get('id')),
                pool_candidate_source(workbench.conn))
            body = _status_dict(status)
            body['kind'] = 'pool_refill'
            body['pool_id'] = str(call.params.get('id'))
            return body
        return self._with_workbench(action)

    def _op_pools_recheck(self, call):
        """Queue a measurement of the pool's own collection, not of the body's."""
        def action(workbench):
            from . import proxytool as engine
            from . import jobs as jobs_module
            pool_id = str(call.params.get('id'))
            spec = workbench.pools().require(pool_id)
            collection = str(spec.collection_id)
            conn = workbench.conn
            members = [row[0] for row in conn.execute(
                'SELECT e.canonical FROM membership m JOIN endpoints e ON e.id = m.endpoint_id '
                'WHERE m.collection_id=? ORDER BY e.canonical', (collection,)).fetchall()]
            items = [jobs_module.QueueItem(endpoint_id=schema_endpoint_id(conn, value),
                                           access_id=engine.PUBLIC_ACCESS_ID, access_revision=1)
                     for value in members]
            conn.commit()
            job = workbench.jobs().submit(
                'pool_recheck',
                jobs_module.Scope(collection_id=collection, profile_id=spec.profile_id,
                                  profile_revision=int(spec.profile_revision),
                                  profile_digest=spec.profile_id,
                                  filters={'protocol': (call.body or {}).get('protocol', 'all')},
                                  budgets={'max_seconds': (call.body or {}).get('max_seconds')}),
                items, idempotency_key=call.idempotency_key)
            return {'job_id': job.id, 'kind': 'pool_recheck', 'state': job.state,
                    'pool_id': pool_id, 'collection_id': collection, 'items': len(items)}
        return self._with_workbench(action)

    # -- schedules ----------------------------------------------------------

    def _scheduler(self, workbench):
        from . import scheduler
        return scheduler.Scheduler(workbench.schedules(), clock=workbench.clock)

    def _op_schedules_list(self, call):
        def action(workbench):
            engine = self._scheduler(workbench)
            items = [_schedule_dict(spec) for spec in engine.list()]
            return {'items': self._guard_objects(items, call, 'id', 'schedules'),
                    'stream_id': 'schedules', 'next_seq': None}
        return self._with_workbench(action)

    def _op_schedules_get(self, call):
        found = self._find_schedule(call)
        return self._guard_objects([_schedule_dict(found)], call, 'id', 'schedules')[0]

    def _find_schedule(self, call):
        wanted = str(call.params.get('id') or '')

        def action(workbench):
            for spec in self._scheduler(workbench).list():
                if spec.id == wanted:
                    return spec
            return None
        found = self._with_workbench(action)
        if found is None:
            raise apiv1.ApiError('E_STATE_NOT_FOUND', status=404, details={'id': wanted})
        return found

    def _op_schedules_create(self, call):
        def action(workbench):
            engine = self._scheduler(workbench)
            spec = engine.add(self._schedule_payload(call))
            return _schedule_dict(spec)
        return self._with_workbench(action)

    #: The API's schedule vocabulary mapped onto the module's own names.  A
    #: field the module does not know is dropped *here*, on purpose, and never
    #: passed through as if it had been understood (CONTRACTS §5.4).
    SCHEDULE_KINDS = {'check': 'interval', 'refill': 'interval', 'recheck': 'interval',
                      'export': 'interval', 'source': 'interval'}

    def _schedule_payload(self, call):
        """The route body in the shape the schedule store persists."""
        from . import scheduler
        body = dict(call.body or {})
        payload = {'id': str(body.get('name') or '').strip(),
                   'kind': self.SCHEDULE_KINDS.get(str(body.get('kind') or 'check'), 'interval'),
                   'interval_minutes': body.get('interval_minutes'),
                   'timezone': str((body.get('window') or {}).get('timezone') or 'UTC')}
        if body.get('pool_id'):
            payload['pool_id'] = str(body['pool_id'])
        for field, key in (('window', 'windows'), ('quiet_hours', 'quiet_hours')):
            value = body.get(field)
            if isinstance(value, dict) and value.get('from') and value.get('to'):
                start, end = _hhmm(value['from']), _hhmm(value['to'])
                if start == end:
                    raise apiv1.field_error(field, tr('окно не может быть пустым',
                                                      'the window may not be empty'))
                payload[key] = [scheduler.Window(start, end).to_dict()]
        budgets = body.get('budgets')
        if isinstance(budgets, dict):
            payload['budgets'] = {key: budgets[key] for key in
                                  ('max_requests', 'max_bytes', 'max_seconds') if key in budgets}
        if body.get('enabled') is not None:
            payload['enabled'] = bool(body['enabled'])
        return payload

    def _op_schedules_update(self, call):
        def action(workbench):
            import dataclasses
            from . import scheduler
            current = next((item for item in self._scheduler(workbench).list()
                            if item.id == call.params.get('id')), None)
            if current is None:
                raise apiv1.ApiError('E_STATE_NOT_FOUND', status=404,
                                     details={'id': call.params.get('id')})
            body = call.body or {}
            if call.expected_revision is not None and \
                    int(call.expected_revision) != int(getattr(current, 'revision', 1) or 1):
                raise apiv1.ApiError('E_CONFLICT_REVISION',
                                     details={'expected': call.expected_revision,
                                              'stored': getattr(current, 'revision', 1)})
            changes = {key: value for key, value in body.items()
                       if key in ('name', 'interval_minutes', 'timezone', 'windows', 'budgets',
                                  'pool_id', 'kind') and value is not None}
            merged = scheduler.ScheduleSpec(**{**{field: getattr(current, field)
                                                  for field in current.__dataclass_fields__
                                                  if field in changes or field not in ('name', 'revision')},
                                             **changes})
            workbench.schedules().save_spec(merged)
            return _schedule_dict(merged)
        return self._with_workbench(action)

    def _op_schedules_delete(self, call):
        def action(workbench):
            self._scheduler(workbench).remove(str(call.params.get('id')))
            return {'id': call.params.get('id'), 'deleted': True}
        return self._with_workbench(action)

    def _op_schedules_enable(self, call):
        return self._with_workbench(lambda workbench: self._schedule_flag(workbench, call, True))

    def _op_schedules_disable(self, call):
        return self._with_workbench(lambda workbench: self._schedule_flag(workbench, call, False))

    def _schedule_flag(self, workbench, call, enabled):
        import dataclasses
        engine = self._scheduler(workbench)
        current = next((item for item in engine.list() if item.id == call.params.get('id')), None)
        if current is None:
            raise apiv1.ApiError('E_STATE_NOT_FOUND', status=404, details={'id': call.params.get('id')})
        workbench.schedules().save_spec(dataclasses.replace(current, enabled=bool(enabled)))
        if enabled:
            engine.resume(current.id)
        else:
            engine.pause(current.id)
        return _schedule_dict(dataclasses.replace(current, enabled=bool(enabled)))

    def _op_schedules_next_run(self, call):
        found = self._find_schedule(call)

        def action(workbench):
            engine = self._scheduler(workbench)
            plan = engine.plan(found, engine.state(found.id), self.clock())
            return {'id': found.id, 'timezone': found.timezone, 'next_at': plan.next_at,
                    'reason': plan.reason, 'overdue': plan.overdue, 'dst_skipped': plan.dst_skipped}
        return self._with_workbench(action)

    def _op_schedules_counters(self, call):
        found = self._find_schedule(call)

        def action(workbench):
            runs = workbench.schedules().recent_runs(found.id, limit=50)
            return {'id': found.id,
                    'runs': [run.as_dict() if hasattr(run, 'as_dict') else str(run) for run in runs]}
        return self._with_workbench(action)

    # -- sources ------------------------------------------------------------

    def _source_settings(self):
        """The user's own source list, stored next to the data it feeds.

        It is a separate file from the bundled catalog on purpose: a catalog is
        read-only research, this file is the user's choice, and neither is
        rewritten by an update (F13, F27).
        """
        return read_source_settings(self.data)

    def _write_source_settings(self, settings):
        write_source_settings(self.data, settings)
        return settings

    def _catalog(self):
        from . import source_catalog
        try:
            return source_catalog.load_catalog()
        except Exception:
            return {}

    def _op_sources_catalog(self, call):
        """The versioned source catalog with its honest evidence states."""
        catalog = self._catalog()
        rows = catalog.get('sources') or catalog.get('rows') or []
        query = str(call.query.get('q') or call.query.get('query') or '').strip().lower()
        if query:
            rows = [row for row in rows
                    if query in json.dumps(row, ensure_ascii=False, default=str).lower()]
        return {'items': self._guard_objects(rows, call, 'id', 'sources'),
                'stream_id': 'sources-catalog', 'next_seq': None, 'total': len(rows),
                'schema_version': catalog.get('schema_version'),
                'published_at': catalog.get('published_at'),
                'revision': catalog.get('revision')}

    def _op_sources_list(self, call):
        """Configured sources with what the last collection actually saw."""
        settings = self._source_settings()
        seen = {}
        conn = self.connection()
        try:
            if conn is not None:
                for row in conn.execute('SELECT source, count(*) AS n FROM candidate_seen '
                                        'GROUP BY source'):
                    seen[row['source']] = row['n']
        except sqlite3.Error:
            seen = {}
        finally:
            _close(conn)
        enabled = [item for item in settings.get('sources') if item not in set(settings.get('disabled') or ())]
        items = [{'id': item, 'url': item, 'enabled': item in enabled,
                  'kind': 'custom', 'addressed': seen.get(item, 0)} for item in settings.get('sources')]
        return {'items': self._guard_objects(items, call, 'id', 'sources'),
                'stream_id': 'sources', 'next_seq': None, 'total': len(items),
                'disabled': sorted(settings.get('disabled') or ())}

    def _op_sources_get(self, call):
        wanted = str(call.params.get('id') or '')
        settings = self._source_settings()
        for item in settings.get('sources'):
            if item == wanted:
                return {'id': item, 'url': item,
                        'enabled': item not in set(settings.get('disabled') or ()),
                        'kind': 'custom'}
        raise apiv1.ApiError('E_STATE_NOT_FOUND', status=404, details={'id': wanted})

    def _op_sources_create(self, call):
        body = call.body or {}
        url = str(body.get('url') or '').strip()
        if not url:
            raise apiv1.field_error('url', tr('нужен URL источника', 'a source URL is required'))
        settings = self._source_settings()
        if url in settings['sources']:
            raise apiv1.ApiError('E_CONFLICT_REVISION', status=409, details={'url': url},
                                 message=tr('источник уже добавлен', 'the source is already added'))
        settings['sources'] = settings['sources'] + [url]
        self._write_source_settings(settings)
        return {'id': url, 'url': url, 'enabled': True, 'created': True,
                'sources': len(settings['sources'])}

    def _op_sources_update(self, call):
        wanted = str(call.params.get('id') or '')
        body = call.body or {}
        url = str(body.get('url') or wanted).strip()
        settings = self._source_settings()
        if wanted not in settings['sources']:
            raise apiv1.ApiError('E_STATE_NOT_FOUND', status=404, details={'id': wanted})
        settings['sources'] = [url if item == wanted else item for item in settings['sources']]
        self._write_source_settings(settings)
        return {'id': url, 'url': url, 'updated': True}

    def _op_sources_enable(self, call):
        return self._source_flag(call, enabled=True)

    def _op_sources_disable(self, call):
        return self._source_flag(call, enabled=False)

    def _source_flag(self, call, *, enabled):
        wanted = str(call.params.get('id') or '')
        settings = self._source_settings()
        if wanted not in settings['sources']:
            raise apiv1.ApiError('E_STATE_NOT_FOUND', status=404, details={'id': wanted})
        disabled = [item for item in (settings.get('disabled') or []) if item != wanted]
        if not enabled:
            disabled.append(wanted)
        settings['disabled'] = sorted(disabled)
        self._write_source_settings(settings)
        return {'id': wanted, 'enabled': bool(enabled), 'disabled': sorted(disabled)}

    def _op_sources_refresh_preview(self, call):
        """What a refresh would decide, with its own reasons (F27).

        The decision comes from ``sourcedesk``; the engine owns the fetch, so a
        preview never touches the network and never changes membership.
        """
        return self._source_refresh_state(call, refresh=False)

    def _op_sources_refresh(self, call):
        return self._source_refresh_state(call, refresh=True)

    def _source_refresh_state(self, call, *, refresh):
        from . import sourcedesk
        wanted = str(call.params.get('id') or '')

        def action(workbench):
            report = self._last_collect_report()
            state = sourcedesk.FeedState(source_id=wanted, collection_id=schema_public_collection(),
                                         last_attempt_at=self.clock() if report else None,
                                         last_outcome=(report or {}).get('outcome'))
            result = sourcedesk.FeedResult(outcome=(report or {}).get('outcome') or 'ok',
                                           fetched_at=self.clock())
            plan = sourcedesk.plan_refresh(state, result, now=self.clock())
            diagnostics = sourcedesk.feed_diagnostics(state, now=self.clock())
            return {'source_id': wanted, 'mode': plan.mode, 'outcome': plan.outcome,
                    'reason_code': plan.reason_code, 'applied': bool(refresh and plan.applied),
                    'next_attempt_at': plan.next_attempt_at,
                    'diagnostics': [{'code': item.code, 'detail': item.detail} for item in diagnostics]}
        return self._with_workbench(action)

    def _op_sources_refresh_status(self, call):
        from . import sourcedesk
        wanted = str(call.params.get('id') or '')

        def action(workbench):
            report = self._last_collect_report()
            state = sourcedesk.FeedState(source_id=wanted, collection_id=schema_public_collection(),
                                         last_attempt_at=self.clock() if report else None,
                                         last_outcome=(report or {}).get('outcome'))
            diagnostics = sourcedesk.feed_diagnostics(state, now=self.clock())
            return {'source_id': wanted, 'job_id': str(call.params.get('job_id') or ''),
                    'outcome': (report or {}).get('outcome'),
                    'diagnostics': [{'code': item.code, 'detail': item.detail} for item in diagnostics]}
        return self._with_workbench(action)

    def _last_collect_report(self):
        path = self.data / 'sources-report.json'
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (OSError, UnicodeError, ValueError):
            return None

    # -- gateway ------------------------------------------------------------

    def _op_gateway_bindings(self, call):
        return {'items': self._gateway_bindings(), 'stream_id': 'gateway', 'next_seq': None}

    def _gateway_bindings(self):
        rows, status = self.exports.load()
        return [{'generation': status.get('generation'),
                 'collection_id': status.get('collection_id'),
                 'profile': status.get('profile'),
                 'profile_revision': status.get('profile_revision'),
                 'listeners': self._gateway_listeners(),
                 'available': len(rows)}]

    def _gateway_listeners(self):
        try:
            from . import gateway
            return [{'host': gateway.DEFAULT_HOST, 'port': gateway.DEFAULT_PORT,
                     'running': False}]
        except Exception:
            return []

    def _op_gateway_listeners(self, call):
        return {'items': self._gateway_listeners(), 'stream_id': 'gateway-listeners', 'next_seq': None}

    def _op_gateway_sessions(self, call):
        return {'items': [], 'stream_id': 'gateway-sessions', 'next_seq': None, 'total': 0,
                'note': tr('сессии живут в процессе шлюза, а не в файле', 'sessions live in the gateway process, not in a file')}

    def _op_gateway_config_get(self, call):
        from . import gateway
        return {'host': gateway.DEFAULT_HOST, 'port': gateway.DEFAULT_PORT,
                'transports': ['http', 'socks5'], 'bind': 'loopback'}

    def _op_gateway_bind(self, call):
        """Record a binding in the snapshot's own scope; it does not start a process.

        A binding is a decision, and a decision that started a listener from an
        HTTP request would be a surprise the user never asked for.
        """
        from . import db as schema
        body = call.body or {}
        conn = self.writable_connection()
        try:
            pool_id = str(body.get('pool_id') or '')
            row = conn.execute('SELECT id, collection_id, profile_id, profile_revision FROM pools WHERE id=?',
                               (pool_id,)).fetchone()
            if row is None:
                raise apiv1.ApiError('E_STATE_NOT_FOUND', status=404, details={'pool_id': pool_id})
            binding = {'listener': str(body.get('listener') or 'default'), 'pool_id': pool_id,
                       'collection_id': row['collection_id'], 'profile_id': row['profile_id'],
                       'profile_revision': row['profile_revision'],
                       'generation': body.get('generation'), 'revision': self.clock()}
            conn.execute(
                'INSERT OR REPLACE INTO export_artifact(id, kind, collection_id, profile_id, '
                'profile_revision, generation, published_at, state, manifest_json) '
                'VALUES (?,?,?,?,?,?,?,?,?)',
                (f'bind:{pool_id}', 'binding', row['collection_id'], row['profile_id'],
                 row['profile_revision'], body.get('generation') or '', self.clock(), 'ready',
                 json.dumps(binding, ensure_ascii=False)))
            conn.commit()
            return binding
        except sqlite3.Error as exc:
            raise apiv1.ApiError('E_STATE_SNAPSHOT_STATIC', status=503,
                                 message=type(exc).__name__) from None
        finally:
            _close(conn)

    def _op_gateway_config_set(self, call):
        body = call.body or {}
        if call.expected_revision is None:
            raise apiv1.ApiError('E_VALIDATION_FIELD',
                                 details={'field': 'revision', 'required': 'If-Match'},
                                 message=tr('конфигурация шлюза меняется с ревизией',
                                            'gateway config changes carry a revision'))
        return {'revision': call.expected_revision, 'applied': False,
                'reason': tr('шлюз перечитывает конфигурацию при следующем запуске',
                             'the gateway reads its configuration on the next start')}

    # -- profiles -----------------------------------------------------------

    def _op_profiles_validate(self, call):
        from . import profiles as profiles_module
        try:
            profiles_module.run_request(call.body or {}, store=None)
        except profiles_module.ProfileError as exc:
            raise apiv1.ApiError(getattr(exc, 'code', 'E_VALIDATION_SCHEMA') or 'E_VALIDATION_SCHEMA',
                                 status=422, message=str(exc)) from None
        return {'valid': True, 'profile': profiles_module.export_profile(call.body or {})}

    def _op_profiles_clone(self, call):
        def action(workbench):
            store = workbench.profiles()
            source = store.get(str(call.params.get('id') or ''))
            if source is None:
                raise apiv1.ApiError('E_STATE_NOT_FOUND', status=404,
                                     details={'id': call.params.get('id')})
            name = str((call.body or {}).get('name') or f'{source.name} copy')
            return _profile_dict(store.create(name, store.spec(source.id)))
        return self._with_workbench(action)

    def _op_profiles_archive(self, call):
        def action(workbench):
            store = workbench.profiles()
            record = store.get(str(call.params.get('id') or ''))
            if record is None:
                raise apiv1.ApiError('E_STATE_NOT_FOUND', status=404,
                                     details={'id': call.params.get('id')})
            store.archive(record.id)
            return {'id': record.id, 'archived': True}
        return self._with_workbench(action)

    # -- reservations -------------------------------------------------------

    def _op_reservations_acquire(self, call):
        """Take the addresses out of the pool for the life of a lease.

        The three reservation routes used to answer from the published snapshot
        and to ignore everything the body said: ``lease_id``, ``ttl_s`` and
        ``state`` were read and dropped, nothing was reserved, and two acquires
        of one pool handed out the same addresses -- a made-up ``lease_id``
        renewed and released a lease that never existed (F29, acceptance 5).
        """
        pool_id = str((call.body or {}).get('pool_id') or '')
        body = call.body or {}
        count = int(body.get('count') or 1)
        ttl_s = int(body.get('ttl_s') or 0) or DEFAULT_LEASE_TTL_S
        candidates = self._pool_candidates(call, pool_id)
        lease, retry_in = self.reservations.acquire(pool_id, _caller_key_id(call),
                                                    candidates, count, ttl_s)
        if lease is None:
            raise apiv1.ApiError('E_LIMIT_QUEUE', status=429,
                                 retry_after=max(1, int(retry_in or 0)),
                                 details={'pool_id': pool_id, 'available': 0,
                                          'count': count, 'retry_after_s': max(1, int(retry_in or 0))},
                                 action=tr('пул полностью арендован: повторите после освобождения '
                                           'или истечения TTL',
                                           'the pool is fully leased: retry after a lease is '
                                           'released or expires'))
        return _lease_body(lease, 'acquire', len(candidates))

    def _op_reservations_lease(self, call):
        """Renew a lease this key holds.  Someone else's lease is not found."""
        body = call.body or {}
        pool_id = str(body.get('pool_id') or '')
        lease_id = str(body.get('lease_id') or '')
        ttl_s = int(body.get('ttl_s') or 0) or DEFAULT_LEASE_TTL_S
        lease = self.reservations.renew(lease_id, pool_id, _caller_key_id(call), ttl_s)
        return _lease_body(lease, 'lease', None)

    def _op_reservations_release(self, call):
        """Give the addresses back, and say what became of the lease."""
        body = call.body or {}
        pool_id = str(body.get('pool_id') or '')
        lease_id = str(body.get('lease_id') or '')
        state = str(body.get('state') or 'returned')
        lease = self.reservations.release(lease_id, pool_id, _caller_key_id(call), state)
        return _lease_body(lease, 'release', None)

    def _op_reservations_feedback(self, call):
        """Bounded target-aware feedback on one pool member.

        What the pool can store is the member's own phase: a positive report
        puts it back in service, a negative one parks it in cooldown.  The
        per-target detail the route accepts has nowhere to live -- there is no
        feedback table -- so it is echoed back as ``stored: false`` with the
        reason instead of being dropped in silence (F07).  See the handoff to
        ``db.py`` for the table this is waiting on.
        """
        from . import pools as pools_module
        body = call.body or {}
        pool_id = str(body.get('pool_id') or '')
        endpoint_id = str(body.get('endpoint_id') or '')
        ok = bool(body.get('ok'))
        store, conn = self._pool_store()
        try:
            if store is None:
                raise apiv1.ApiError('E_STATE_NOT_FOUND', status=409,
                                     details={'reason': 'no readable database'})
            store.require(pool_id)
            if store.member(pool_id, endpoint_id) is None:
                raise apiv1.ApiError('E_STATE_NOT_FOUND', status=404,
                                     details={'pool_id': pool_id, 'endpoint_id': endpoint_id})
            state = pools_module.MEMBER_ACTIVE if ok else pools_module.MEMBER_COOLDOWN
            store.set_member_state(pool_id, endpoint_id, state)
            if conn is not None:
                conn.commit()
        except apiv1.ApiError:
            raise
        except Exception as exc:
            code = getattr(exc, 'code', None) or 'E_STATE_NOT_FOUND'
            raise apiv1.ApiError(_api_code(code), status=_error_status(code),
                                 details={'pool_id': pool_id, 'endpoint_id': endpoint_id},
                                 message=str(exc)) from None
        finally:
            _close(conn)
        unstored = {name: body[name] for name in ('latency_ms', 'target_id', 'error_code')
                    if body.get(name) is not None}
        return {'pool_id': pool_id, 'endpoint_id': endpoint_id, 'ok': ok,
                'member_state': state, 'recorded': True, 'reputation_changed': False,
                'note': tr('отзыв применён к пулу, глобальная репутация не меняется',
                           'the feedback was applied to the pool; global reputation is unchanged'),
                'not_stored': ({'fields': sorted(unstored),
                                'reason': 'no feedback table in the schema; see docs/integration/'
                                          'HANDOFF/fix-api.md'} if unstored else {})}

    def _pool_candidates(self, call, pool_id):
        """The addresses *this* pool can serve right now, best first.

        A reservation is about one pool.  The old implementation answered from
        the published snapshot and used ``pool_id`` for nothing but the scope
        check, so a lease over a pool of collection B handed out addresses of
        collection A.  The rows are the pool's own members and the admission is
        the shared contract's, never a second calculation here (CONTRACTS §2.3).
        """
        from . import core
        from . import pools as pools_module
        body = call.body or {}
        store, conn = self._pool_store()
        try:
            if store is None:
                raise apiv1.ApiError('E_STATE_NOT_FOUND', status=409,
                                     details={'reason': 'no readable database'},
                                     action=tr('откройте папку данных программы',
                                               'open the program data folder first'))
            spec = store.require(pool_id)
            serving = [member.endpoint_id for member in store.members(pool_id)
                       if member.state in (pools_module.MEMBER_ACTIVE,
                                           pools_module.MEMBER_RESERVE)]
        except apiv1.ApiError:
            raise
        except Exception as exc:
            code = getattr(exc, 'code', None) or 'E_STATE_NOT_FOUND'
            raise apiv1.ApiError(_api_code(code), status=_error_status(code),
                                 details={'pool_id': pool_id}, message=str(exc)) from None
        finally:
            _close(conn)
        if not serving:
            return []
        profile = str(body.get('profile_id') or spec.profile_id)
        max_age = int(body.get('max_age_seconds') or 0) or core.DEFAULT_MAX_AGE_SECONDS
        policy = core.Policy(max_age_seconds=max_age,
                             exclude_hosting=bool(body.get('exclude_hosting')),
                             allow_missing_identity=True)
        # The measurement network is the one the engine pins for a profile
        # (``snapshot_network``), not a guess: a row measured on another network
        # is not comparable with this pool and must not be leased as if it were.
        scope = core.Scope(spec.collection_id, profile, int(spec.profile_revision or 1),
                           self._profile_network(profile))
        conn = self.connection()
        try:
            marks = ','.join('?' * len(serving))
            found = conn.execute(
                'SELECT e.id AS endpoint_id, e.canonical AS proxy, r.payload AS payload '
                'FROM endpoints e LEFT JOIN results r ON r.endpoint_id = e.id '
                f'AND r.profile_id = ? WHERE e.id IN ({marks}) ORDER BY e.canonical',
                (profile, *serving)).fetchall() if conn is not None else []
        except sqlite3.Error:
            found = []
        finally:
            _close(conn)
        rows = []
        for record in found:
            if not record['payload']:
                continue
            try:
                row = json.loads(record['payload'])
            except (TypeError, ValueError):
                continue
            row['proxy'] = row.get('proxy') or record['proxy']
            row['endpoint_id'] = row.get('endpoint_id') or record['endpoint_id']
            rows.append(row)
        selection = core.select(rows, scope, PUBLIC_ACCESS, policy, self.clock())
        # The same presentation a published row carries, so a leased address and
        # a downloaded one cannot disagree about its age or its verdict.
        return [{'proxy': str(row.get('proxy')),
                 'endpoint_id': row.get('endpoint_id'),
                 'age_seconds': row.get('age_seconds'),
                 'admission_reason': row.get('admission_reason'),
                 'score': row.get('score'),
                 'country': row.get('country'),
                 'latency_ms': row.get('latency_ms')}
                for row in exportsvc.attach_admission(selection.admitted, selection)]

    def _profile_network(self, profile_id):
        """The measurement network of a profile, from its own stored config."""
        conn = self.connection()
        try:
            row = conn.execute('SELECT config FROM profiles WHERE id=?',
                               (profile_id,)).fetchone() if conn is not None else None
        except sqlite3.Error:
            row = None
        finally:
            _close(conn)
        if row is None:
            return None
        try:
            config = json.loads(row[0] or '{}')
        except (TypeError, ValueError):
            config = {}
        from .proxytool import snapshot_network
        return snapshot_network(config)

    # -- results ------------------------------------------------------------

    def _op_results_selection(self, call):
        """An explicit selection, read against its own scope and nothing else."""
        wanted = [item for item in str(call.query.get('endpoint_ids') or '').split(',') if item]
        rows, status = self.exports.load()
        if not wanted:
            return self._result_page(call, [], status, stream_kind='selection')
        picked = [row for row in rows if row.get('proxy') in wanted or row.get('endpoint_id') in wanted]
        return self._result_page(call, self._guard_objects(picked, call, 'collection_id', 'collections'),
                                 status, stream_kind='selection')


#: Where the user's own source list lives.  It is a separate file from the
#: bundled catalog so an update never rewrites a choice the user made.
SOURCE_SETTINGS = 'sources.json'


def read_source_settings(data):
    """The user's own source list plus the ones they switched off."""
    path = Path(data) / SOURCE_SETTINGS
    try:
        stored = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValueError):
        stored = {}
    sources = [str(item) for item in (stored.get('sources') or []) if isinstance(item, str)]
    disabled = [str(item) for item in (stored.get('disabled') or []) if isinstance(item, str)]
    return {'sources': sources, 'disabled': disabled}


def write_source_settings(data, settings):
    """Persist the user's source list; an unwritable folder is an error, not a loss."""
    path = Path(data) / SOURCE_SETTINGS
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(dict(settings), ensure_ascii=False, indent=1) + '\n',
                         encoding='utf-8')
    temporary.replace(path)
    return settings


def _hhmm(value):
    """``HH:MM`` as local wall-clock minutes; the schedule module owns the bounds."""
    text = str(value).strip()
    hours, _, minutes = text.partition(':')
    if not hours.isdigit() or not minutes.isdigit():
        raise ValueError(f'ожидается ЧЧ:ММ, получено {text!r}')
    return int(hours) * 60 + int(minutes)


#: A module code the API answers with a different, canonical one.  A missing
#: object and a forbidden one must look alike, and `E_POOL_UNKNOWN` is a missing
#: object; the module's own vocabulary stays visible in the details.
API_CODE_ALIASES = {'E_POOL_UNKNOWN': 'E_STATE_NOT_FOUND'}


def _api_code(code):
    name = str(code or '')
    return API_CODE_ALIASES.get(name, name)


#: The content type each file of an artifact is served with.  The download route
#: is a raw body, and a redaction rule that cannot tell JSON from text cannot be
#: applied to it, so the file names its own type here.
ARTIFACT_CONTENT_TYPES = {
    'ranked.json': 'application/json; charset=utf-8',
    'singbox.json': 'application/json; charset=utf-8',
    'ranked.csv': 'text/csv; charset=utf-8',
    'proxy.pac': 'application/x-ns-proxy-autoconfig; charset=utf-8',
    'clash.yaml': 'text/yaml; charset=utf-8',
}


def _artifact_content_type(name):
    return ARTIFACT_CONTENT_TYPES.get(name, 'text/plain; charset=utf-8')


def _artifact_is_secret(directory, name):
    """Whether this generation was cut with credential references in it.

    ``status.json`` of the *generation* is the artifact's own record, not the
    mutable convenience copy in the exports root, so a later publication cannot
    change the answer for an artifact that is already on disk.
    """
    try:
        status = json.loads((directory / 'status.json').read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeError):
        return False
    return str(status.get('credentials') or 'redact') != exportsvc.CREDENTIALS_REDACT


def _error_status(code):
    """The HTTP status the code canonically carries (CONTRACTS §5.4).

    One table, so a refusal never answers 200 with an error body and never
    answers 500 for a plain validation problem.
    """
    name = str(code or '')
    if name.startswith(('E_AUTH_', 'E_CONFLICT_ACCESS')):
        return 401 if 'EXPIRED' in name or 'FAILED' in name else 403
    if name.startswith(('E_CONFLICT_', 'E_STATE_')):
        return 409
    if name.startswith(('E_POOL_', 'E_EXPORT_', 'E_IMPORT_')):
        # A pool/export/import refusal names the state of an object, so it is a
        # conflict, not a malformed request.  `E_POOL_UNKNOWN` in particular is
        # "no such pool": a missing and a forbidden object must answer alike.
        return 409 if name != 'E_POOL_UNKNOWN' else 404
    if name.startswith(('E_LIMIT_',)):
        return 429
    if name.startswith(('E_VALIDATION_', 'E_SECRET_')):
        return 422
    if name.startswith('E_SERVICE_'):
        return 503
    return 400


def _module_errors():
    """Every public error a module can raise, collected once.

    The service layer must not leak a module class to a client, so the set is
    built from the modules themselves rather than restated here.  Every entry is
    a *base* class of its module (``ImportProblem`` covers format, encoding,
    size, revision, partial, cancelled, busy and an unknown collection), so a
    refusal the module adds later is translated to a code instead of escaping
    as a traceback out of a request.
    """
    from . import importer, jobs, pools, profiles, scheduler
    from . import secrets as secretstore
    return (importer.ImportProblem, jobs.JobError, pools.PoolError, profiles.ProfileError,
            scheduler.ScheduleError, secretstore.SecretError)


class _ModuleErrors(tuple):
    """A tuple of exception classes usable in ``except``."""

    def __contains__(self, item):
        return tuple.__contains__(self, item) or any(
            isinstance(item, cls) for cls in self)


_MODULE_ERRORS = _ModuleErrors(_module_errors())


def schema_endpoint_id(conn, canonical):
    """The endpoint id of a canonical address, creating the row if needed."""
    from . import db as schema
    return schema.upsert_endpoint(conn, canonical)


def pool_candidate_source(conn, *, min_success=1.0):
    """The candidate source `pools.refill` asks, served from the local database.

    A pool's candidates are the members of *its own* collection, and the tiers are
    served honestly: the reserve is the pool's own current members, `known` is the
    collection rows that already carry a measurement, and `sources` is the rest --
    offered with ``allowed=False``, which is what puts them in ``recheck_due``
    instead of pretending they work.  The admission verdict is the shared
    contract's, never a second calculation here (CONTRACTS §2.3).

    Nothing is dialled: a candidate is a stored row, and the measurement that
    turns `sources` into `known` is the `pool_recheck` job.
    """
    from . import db as schema
    from . import pools as pools_module
    from .reputation import result_allowed

    def source(spec, kind, budget, now):
        limit = max(0, int(budget or 0))
        if not limit:
            return []
        try:
            members = {row[0] for row in conn.execute(
                'SELECT endpoint_id FROM pool_member WHERE pool_id=?', (spec.id,)).fetchall()}
        except sqlite3.Error:
            members = set()
        rows = conn.execute(
            'SELECT e.id AS endpoint_id, e.canonical AS canonical, r.payload AS payload,'
            ' r.checked_at AS checked_at, r.valid_until AS valid_until'
            ' FROM membership m JOIN endpoints e ON e.id = m.endpoint_id'
            ' LEFT JOIN results r ON r.endpoint_id = e.id AND r.profile_id=?'
            ' WHERE m.collection_id=? ORDER BY e.canonical LIMIT ?',
            (spec.profile_id, spec.collection_id, limit)).fetchall()
        offered = []
        for row in rows:
            is_member = row['endpoint_id'] in members
            if kind == pools_module.SOURCE_RESERVE and not is_member:
                continue
            if kind != pools_module.SOURCE_RESERVE and is_member:
                continue
            payload = {}
            if row['payload']:
                try:
                    payload = json.loads(row['payload'])
                except (TypeError, ValueError):
                    payload = {}
            measured = row['checked_at'] is not None
            allowed = bool(measured and kind == pools_module.SOURCE_KNOWN
                           and result_allowed(payload, min_success))
            offered.append(pools_module.Candidate(
                endpoint_id=row['endpoint_id'], canonical=row['canonical'],
                collection_id=spec.collection_id, allowed=allowed,
                admission_reason=(None if allowed or kind == pools_module.SOURCE_RESERVE
                                  else (pools_module.REASON_TIME_MISSING if not measured
                                        else pools_module.REASON_DENIED)),
                checked_at=row['checked_at'], valid_until=row['valid_until'],
                protocol=proxytool_proxy(row['canonical']),
                origin_domain=pools_module.DOMAIN_OWN))
        return offered

    return source


def proxytool_proxy(canonical):
    from .proxytool import proxy_protocol
    try:
        return proxy_protocol(str(canonical))
    except Exception:  # a malformed address is unknown, never an exception here
        return ''


def pools_state_for(action, status):
    """The pool state after ``start``/``pause``; the module owns the vocabulary.

    Built with `dataclasses.replace`, not by naming every field: `PoolStatus` grew
    a dozen counters (deficit_reasons, budget_spent, recheck_due, source_errors, …)
    and the hand-written constructor call stopped listing them, so both
    `POST /v1/pools/{id}/start` and `.../pause` answered 500 with
    `missing 12 required positional arguments`.  `replace` carries every field the
    module adds next without a second edit here.
    """
    from dataclasses import replace
    from . import pools as pools_module
    if action == 'start':
        return replace(status, state=pools_module.STATE_EMPTY)
    return replace(status, state=status.state, ready_for_clients=False)


def _job_dict(job):
    """A job as JSON.  ``jobs`` exposes ``to_json()``; nothing else may guess."""
    return job.to_json() if hasattr(job, 'to_json') else dict(job)


def _event_dict(event):
    return event.to_json() if hasattr(event, 'to_json') else dict(event)


def _item_dict(item):
    return item.to_json() if hasattr(item, 'to_json') else dict(item)


def _schedule_dict(spec):
    return {'id': spec.id, 'kind': spec.kind, 'enabled': spec.enabled,
            'interval_minutes': spec.interval_minutes, 'timezone': spec.timezone,
            'pool_id': spec.pool_id,
            'windows': [window.to_dict() for window in spec.windows],
            'quiet_hours': [window.to_dict() for window in spec.quiet_hours],
            'budgets': spec.budgets.to_dict() if hasattr(spec.budgets, 'to_dict') else {},
            'dst_policy': getattr(spec, 'dst_policy', None),
            'catch_up': getattr(spec, 'catch_up', None)}


def _profile_dict(record):
    return record.as_dict() if hasattr(record, 'as_dict') else {
        'id': getattr(record, 'id', None), 'name': getattr(record, 'name', None),
        'revision': getattr(record, 'revision', None)}


def apiv1_stream(job_id):
    return f'job:{job_id}'


def _pool_dict(spec):
    if hasattr(spec, 'as_dict'):
        return spec.as_dict()
    if hasattr(spec, '__dict__'):
        return {k: v for k, v in vars(spec).items()}
    return dict(spec)


def _status_dict(status):
    if hasattr(status, 'as_dict'):
        return status.as_dict()
    return {k: v for k, v in vars(status).items()}


def _member_dict(member):
    """One pool member as JSON; the module's own shape, never a guessed one."""
    if hasattr(member, 'as_dict'):
        return member.as_dict()
    if hasattr(member, '__dict__'):
        return dict(vars(member))
    return dict(member)


#: The API says "own" and "public" in the user's words; the schema stores the
#: kinds of the contract.  An unknown kind is an error, never a silent default.
COLLECTION_KINDS = {'own': 'private', 'private': 'private',
                    'public': 'public', 'legacy': 'public'}


def spec_of(body):
    """The profile document out of a request body.

    The route declares the fields of a profile in the flat shape the user edits
    (targets, rule, k, timeouts, attempts, bytes); the store takes the
    structured spec.  This is the one place that translation happens.
    """
    body = dict(body or {})
    body.pop('name', None)
    body.pop('parent_id', None)
    body.pop('revision', None)
    rule = str(body.pop('rule', '') or '')
    k = int(body.pop('k', 1) or 1)
    # The route speaks the service-set vocabulary (all/any/at_least_k) and the
    # store speaks the optional-target rule (none/all/any/at_least).  "all"
    # over an empty optional set is not a weaker rule, it is an impossible one,
    # so it is carried as "none" there; the required targets combine through
    # their own ``min_success``.
    optional = [item for item in (body.get('targets') or [])
                if isinstance(item, dict) and item.get('kind') == 'optional']
    mode = {'at_least_k': 'at_least'}.get(rule, rule) or 'all'
    if mode != 'none' and not optional:
        mode = 'none'
    body['optional_rule'] = {'mode': mode, 'k': max(1, k)}
    return {name: value for name, value in body.items() if value is not None}


def schema_public_collection():
    from . import db as schema
    return schema.PUBLIC_COLLECTION_ID


def collection_kind(value):
    text = str(value or 'own').strip().lower()
    if text not in COLLECTION_KINDS:
        raise apiv1.field_error('kind', tr('public или own', 'public or own'))
    return COLLECTION_KINDS[text]


#: The kinds a key's resource scope is expressed in (CONTRACTS §5.3: a key is
#: limited to named `collection_id` and `pool_id`).  Every other kind is filtered
#: by the collection the object belongs to, never by a scope list that cannot exist.
SCOPE_KINDS = ('collections', 'pools')


def _collection_of(item, field='collection_id'):
    """The collection an object belongs to, read from the row or from its scope."""
    value = item.get(field)
    if isinstance(value, str) and value:
        return value
    scope = item.get('scope')
    if isinstance(scope, dict):
        value = scope.get('collection_id')
        if isinstance(value, str) and value:
            return value
    return None


def _scope_values(principal, kind):
    """The resource scope of a principal, or ``None`` when it is unrestricted.

    An empty list means "everything these permissions already allow" -- the
    documented meaning of an unset scope -- so only a non-empty list narrows
    anything.  A missing object and a forbidden one answer alike, so a caller
    cannot probe for the existence of a scope it may not see.

    A kind the scope is not expressed in -- a job, a schedule, a profile, a source
    -- answers with the *collections*, because that is the only thing such an object
    can be inside.  Returning ``None`` there used to read as "this key is
    unrestricted": the principal carries no ``jobs`` attribute, so a key scoped to
    one collection received every other collection's object list (F29).  The same
    rule withholds the shared configuration a scoped key has no claim to: a source
    row carries the provider URL verbatim, credential included.
    """
    if kind not in SCOPE_KINDS:
        kind = 'collections'
    scope = getattr(principal, 'resource_scope', None) or {}
    values = scope.get(kind) if isinstance(scope, dict) else None
    if values is None:
        values = getattr(principal, kind, None)
    if not values:
        return None
    return set(values)


def _schema_rows(conn, table):
    if conn is None:
        return []
    try:
        return conn.execute(f'SELECT * FROM {table}').fetchall()
    except sqlite3.Error:
        return []


def _close(conn):
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass


#: How long a lease lives when the caller did not say.  The route declares
#: ``ttl_s`` as required, so this only covers a body that reached the service
#: without one (a direct internal call); the API layer answers such a body with
#: its own validation error before it gets here.
DEFAULT_LEASE_TTL_S = 300

#: What the caller may report about a lease it gives back.
LEASE_STATES = ('returned', 'lost', 'consumed')


class Reservations:
    """Live leases over the members of a pool: a promise about capacity.

    A lease takes its addresses *out* of the pool for the life of the lease.
    Before this registry the three reservation routes answered from the
    published snapshot and read nothing they were given: ``lease_id``,
    ``ttl_s`` and ``state`` were dropped, so two acquires of one pool handed out
    the same addresses and a made-up ``lease_id`` renewed and released a lease
    that never existed (F29, acceptance 5).

    Ownership is per key: a lease belongs to the key that took it, and renewing
    or releasing someone else's answers like a lease that does not exist, so a
    key cannot learn that another key is holding anything.  An expired lease is
    free again on the next call -- the TTL is the whole expiry policy, checked
    lazily rather than by a sweeper, so a lease can never be stranded by a
    process that stopped.

    The registry lives in the service, which is the process that hands the
    addresses out.  A restart forgets it, so two servers on one database would
    still both lease; see the handoff for the durable table in ``db.py``.
    """

    def __init__(self, clock=None):
        self._clock = clock or time.time
        self._lock = threading.RLock()
        self._leases = {}
        self._by_pool = {}

    def now(self):
        return float(self._clock())

    def acquire(self, pool_id, key_id, candidates, count, ttl_s):
        """Free the addresses nobody holds.  Returns (lease, retry_after_s)."""
        now = self.now()
        limit = max(1, int(count or 1))
        with self._lock:
            self._expire(now)
            held = self._by_pool.get(pool_id) or {}
            taken = {proxy for record in held.values() for proxy in record['proxies']}
            free = [row for row in candidates if row.get('proxy') not in taken][:limit]
            if not free:
                soonest = min((record['expires_at'] for record in held.values()), default=None)
                return None, (max(0.0, soonest - now) if soonest is not None else None)
            lease_id = 'lease-' + random_source.token_hex(8)
            record = {
                'lease_id': lease_id, 'pool_id': pool_id, 'key_id': key_id,
                'proxies': [row.get('proxy') for row in free], 'rows': free,
                'acquired_at': now, 'expires_at': now + max(1, int(ttl_s or 0)),
                'state': 'active', 'renewed_at': None, 'released_at': None,
            }
            self._leases[lease_id] = record
            self._by_pool.setdefault(pool_id, {})[lease_id] = record
            return record, None

    def renew(self, lease_id, pool_id, key_id, ttl_s):
        record = self._owned(lease_id, pool_id, key_id)
        now = self.now()
        with self._lock:
            self._expire(now)
            if record['lease_id'] not in self._leases:
                raise _no_lease(lease_id, pool_id)
            record['expires_at'] = now + max(1, int(ttl_s or 0))
            record['renewed_at'] = now
        return dict(record)

    def release(self, lease_id, pool_id, key_id, state='returned'):
        if state not in LEASE_STATES:
            raise apiv1.field_error('state', ', '.join(LEASE_STATES))
        record = self._owned(lease_id, pool_id, key_id)
        with self._lock:
            self._expire(self.now())
            if record['lease_id'] not in self._leases:
                raise _no_lease(lease_id, pool_id)
            self._drop(lease_id)
        record = dict(record)
        record['state'] = state
        record['released_at'] = self.now()
        return record

    def held(self, pool_id=None):
        """The live leases, for a status answer and for tests."""
        with self._lock:
            self._expire(self.now())
            records = list(self._leases.values()) if pool_id is None else \
                list((self._by_pool.get(pool_id) or {}).values())
            return [dict(record) for record in records]

    def forget(self, key_id=None):
        """Drop the leases of one key (or of every key)."""
        with self._lock:
            for lease_id in [lease_id for lease_id, record in self._leases.items()
                             if key_id is None or record['key_id'] == key_id]:
                self._drop(lease_id)

    def _owned(self, lease_id, pool_id, key_id):
        with self._lock:
            self._expire(self.now())
            record = self._leases.get(lease_id)
        # A foreign lease and a lease that never existed answer alike.
        if record is None or record['pool_id'] != pool_id or record['key_id'] != key_id:
            raise _no_lease(lease_id, pool_id)
        return record

    def _expire(self, now):
        for lease_id in [lease_id for lease_id, record in self._leases.items()
                         if record['expires_at'] <= now]:
            record = self._leases[lease_id]
            record['state'] = 'expired'
            self._drop(lease_id)

    def _drop(self, lease_id):
        record = self._leases.pop(lease_id, None)
        if record is None:
            return None
        holders = self._by_pool.get(record['pool_id'])
        if holders is not None:
            holders.pop(lease_id, None)
            if not holders:
                self._by_pool.pop(record['pool_id'], None)
        return record


def _no_lease(lease_id, pool_id):
    return apiv1.ApiError('E_STATE_NOT_FOUND', status=404,
                          details={'lease_id': lease_id, 'pool_id': pool_id},
                          action=tr('аренды нет, она истекла или принадлежит другому ключу',
                                     'the lease is gone: it expired or belongs to another key'))


def _lease_body(lease, action, available=None):
    """One lease, as the three reservation routes answer it."""
    now = time.time()
    body = {'action': action, 'lease_id': lease['lease_id'], 'pool_id': lease['pool_id'],
            'key_id': lease['key_id'], 'state': lease['state'],
            'granted': len(lease['proxies']),
            'acquired_at': lease['acquired_at'], 'expires_at': lease['expires_at'],
            'ttl_s': round(max(0.0, lease['expires_at'] - now), 3),
            'items': [dict(row) for row in lease['rows']]}
    if lease.get('renewed_at') is not None:
        body['renewed_at'] = lease['renewed_at']
    if lease.get('released_at') is not None:
        body['released_at'] = lease['released_at']
    if available is not None:
        body['available'] = available
    return body


def _caller_key_id(call):
    principal = getattr(call, 'principal', None)
    return getattr(principal, 'key_id', None) or 'anonymous'


class LegacyKeyStore(apiv1.KeyStore):
    """The ``--api-token`` secret as a read-only principal, and nothing more.

    It is deliberately *not* registered as an API key: it keeps exactly the
    permissions it had in 2.x (``read.*``), it is never upgraded to admin, and
    the pipeline marks every use as a deprecated compatibility path
    (CONTRACTS §5.1, F29).
    """

    REQUIRED = apiv1.KeyStore.REQUIRED

    def __init__(self, token, manager=None, manager_store=None):
        self.token = token
        self.manager = manager
        self.store = manager_store or (apiv1.ApiKeyStore(manager) if manager is not None else None)

    def verify(self, secret):
        if self.token and hmac.compare_digest(str(secret).encode(), str(self.token).encode()):
            return apiv1.Principal(key_id='legacy-read-token', kind='legacy',
                                   permissions=frozenset(apiv1.LEGACY_READ_PERMISSIONS))
        if self.store is not None:
            return self.store.verify(secret)
        return None

    def _delegate(self, name, *args, **kwargs):
        if self.store is None:
            raise apiv1.ApiError('E_AUTH_PERMISSION', status=403,
                                 details={'reason': 'no key manager is attached'},
                                 action=tr('выдайте ключ через локальный bootstrap',
                                           'issue a key through the local bootstrap'))
        return getattr(self.store, name)(*args, **kwargs)

    def list_keys(self, principal, limit=None, **kwargs):
        return self._delegate('list_keys', principal, limit, **kwargs)

    def get_key(self, principal, key_id):
        return self._delegate('get_key', principal, key_id)

    def create_key(self, principal, spec):
        return self._delegate('create_key', principal, spec)

    def update_key(self, principal, key_id, patch):
        return self._delegate('update_key', principal, key_id, patch)

    def rotate_key(self, principal, key_id, grace_s=0.0, **kwargs):
        return self._delegate('rotate_key', principal, key_id, grace_s, **kwargs)

    def revoke_key(self, principal, key_id, **kwargs):
        return self._delegate('revoke_key', principal, key_id, **kwargs)

    def disable_key(self, principal, key_id, **kwargs):
        return self._delegate('disable_key', principal, key_id, **kwargs)

    def enable_key(self, principal, key_id, **kwargs):
        return self._delegate('enable_key', principal, key_id, **kwargs)

    def delete_key(self, principal, key_id, **kwargs):
        return self._delegate('delete_key', principal, key_id)

    def read_audit(self, principal, **filters):
        return self._delegate('read_audit', principal, **filters)

    def audit(self, record):
        if self.store is not None:
            return self.store.audit(record)
        return None


def key_manager(data, db_path=None):
    """The ``apikeys`` manager on the workbench database, or ``None``.

    A database that does not exist yet is created through the one migrator, so a
    fresh installation can still be given its first administrator key: refusing
    here would leave the user with no way into ``/v1`` at all (F29).
    """
    from . import proxytool as engine
    path = Path(db_path) if db_path else Path(data) / 'proxies.sqlite3'
    try:
        conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        if not path.is_file() or not _is_migrated(conn):
            conn.close()
            engine.open_db(path).close()
            conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
        apikeys.ensure_schema(conn)
        return apikeys.ApiKeyManager(conn)
    except (sqlite3.Error, OSError, apikeys.ApiKeyError, ValueError):
        return None


def _is_migrated(conn):
    """Whether the file already carries this program's schema."""
    from . import db as schema
    try:
        version = conn.execute('PRAGMA user_version').fetchone()[0]
        application = conn.execute('PRAGMA application_id').fetchone()[0]
    except sqlite3.Error:
        return False
    return version == schema.SCHEMA_VERSION and application == schema.APPLICATION_ID


def make_control_api(data, host='127.0.0.1', port=V1_DEFAULT_PORT, token=None, **options):
    """The versioned control API on its own port, next to the legacy reader."""
    manager = key_manager(data)
    service = WorkbenchService(data)
    keys = LegacyKeyStore(token, manager)
    return apiv1.make_server(service=service, keys=keys, host=host, port=port, **options)


def make_api_server(data, host='127.0.0.1', port=DEFAULT_PORT, token=None, v1=True):
    """The 2.x read-only server, with ``/v1`` served from the same socket.

    Both prefixes are answered by the same handler so a client never has to
    guess which port carries the control API, and the legacy token is accepted
    for the compatibility paths only -- it is refused on ``/v1`` with
    ``E_AUTH_PERMISSION`` naming the key manager, exactly as the acceptance
    test for a legacy token requires.
    """
    if not is_loopback(host) and not token:
        raise ValueError(tr(f'API на {host} доступно из сети: задайте токен через --api-token или {TOKEN_ENV}.',
                            f'the API on {host} is reachable from the network: set a token with --api-token or {TOKEN_ENV}'))
    exports = Exports(Path(data) / 'exports')
    manager = key_manager(data)
    service = WorkbenchService(data, exports=exports, key_store=manager)
    # ``port=0`` means "any free port"; the config wants the real number, and a
    # loopback bind needs no host allow-list beyond itself.
    control = apiv1.ApiV1(service, LegacyKeyStore(token, manager),
                          apiv1.ApiConfig(host=host, allowed_hosts=(host,),
                                          allowed_bind_hosts=(host,),
                                          allow_remote_bind=bool(token)))

    class Handler(BaseHTTPRequestHandler):
        server_version = f'{PRODUCT_NAME}/{PRODUCT_VERSION}'
        sys_version = ''

        def log_message(self, *args):
            pass

        def send(self, status, body, content_type='application/json; charset=utf-8', headers=()):
            data = body if isinstance(body, bytes) else body.encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(data)

        def send_json(self, status, value, headers=()):
            self.send(status, json.dumps(value, ensure_ascii=False, indent=1) + '\n', headers=headers)

        def authorized(self, query):
            """The legacy token check, plus the deprecation notice for ``?token=``."""
            if not token:
                return True, ()
            supplied = self.headers.get('Authorization', '').removeprefix('Bearer ').strip()
            query_token = (parse_qs(query).get('token') or [''])[-1]
            notice = ()
            if not supplied and query_token:
                supplied = query_token
                notice = (('Deprecation', 'true'),
                          ('Warning', '299 - "a token in the query string is deprecated; '
                                      'send Authorization: Bearer instead"'))
            return hmac.compare_digest(supplied.encode(), token.encode()), notice

        def trusted_host(self):
            # Blocks DNS rebinding when the API is only meant for this computer.
            if token or not is_loopback(host):
                return True
            name = self.headers.get('Host', '').rsplit(':', 1)[0].strip('[]').lower()
            return is_loopback(name)

        def do_HEAD(self):
            self.do_GET()

        def do_POST(self):
            self.do_GET()

        def do_PATCH(self):
            self.do_GET()

        def do_DELETE(self):
            self.do_GET()

        def do_OPTIONS(self):
            self.do_GET()

        def _control(self):
            url = urlsplit(self.path)
            length = int(self.headers.get('Content-Length') or 0)
            body = self.rfile.read(min(length, control.config.max_body_bytes + 1)) if length else b''
            response = control.handle(apiv1.Request(
                method=self.command, path=url.path, query=url.query,
                headers=dict(self.headers.items()), body=body,
                client_host=self.client_address[0]))
            self.send(response.status, response.body, response.content_type, tuple(response.headers))
            if response.stream is not None:
                for chunk in response.stream:
                    self.wfile.write(chunk)

        def do_GET(self):
            url = urlsplit(self.path)
            if url.path.startswith(apiv1.API_PREFIX):
                return self._control()
            if not self.trusted_host():
                return self.send_json(403, {'error': 'host not allowed'})
            allowed, notice = self.authorized(url.query)
            if not allowed:
                return self.send_json(401, {'error': 'missing or wrong token'}, headers=notice)
            rows, status = exports.load()
            if url.path in ('/', '/status'):
                return self.send_json(200, _status_body(status, len(rows)), headers=notice)
            if url.path not in ('/proxies', '/random', '/pac', '/clash', '/singbox'):
                return self.send_json(404, {'error': 'not found', 'endpoints': ENDPOINTS})
            try:
                query = parse_query(url.query)
            except ValueError as exc:
                return self.send_json(400, {'error': str(exc)})
            selected = select(rows, query)
            if url.path == '/pac':
                return self.send(200, formats.pac(row['proxy'] for row in selected),
                                 'application/x-ns-proxy-autoconfig', notice)
            if url.path == '/clash':
                return self.send(200, formats.clash(selected), 'text/yaml; charset=utf-8', notice)
            if url.path == '/singbox':
                return self.send(200, formats.singbox(selected), 'application/json; charset=utf-8', notice)
            if url.path == '/random':
                selected = random.sample(selected, min(len(selected), query['limit'] or 1))
            elif query['limit']:
                selected = selected[:query['limit']]
            if url.path == '/random' and not selected:
                return self.send_json(404, {'error': 'no proxy matches these filters', 'available': len(rows)})
            if query['format'] == 'txt':
                return self.send(200, ''.join(row['proxy'] + '\n' for row in selected),
                                 'text/plain; charset=utf-8', notice)
            if query['format'] == 'hostport':
                return self.send(200, ''.join(f"{row['proxy'].partition('://')[2]}\n" for row in selected),
                                 'text/plain; charset=utf-8', notice)
            body = {'count': len(selected), 'generated_at': status.get('generated_at'),
                    'generation': status.get('generation'), 'state': status.get('state'),
                    'state_detail': status.get('state_detail'),
                    'valid_until': status.get('valid_until'), 'expires_at': status.get('expires_at'),
                    'stale': status.get('stale', False), 'proxies': selected}
            return self.send_json(200, body, headers=notice)

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.control_api = control
    server.exports = exports
    server.workbench_service = service
    return server


def _status_body(status, available):
    """The legacy ``/status`` shape, extended with what the contract requires."""
    body = {
        'service': PRODUCT_NAME, 'version': PRODUCT_VERSION, 'available': available,
        'schema_version': status.get('schema_version'), 'generation': status.get('generation'),
        'profile': status.get('profile'), 'state': status.get('state'),
        'state_detail': status.get('state_detail'),
        'stop_reason': status.get('stop_reason'), 'scope': status.get('scope'),
        'scope_digest': status.get('scope_digest'),
        'collection_id': status.get('collection_id'),
        'profile_revision': status.get('profile_revision'),
        'network_id': status.get('network_id'),
        'scope_candidates': status.get('scope_candidates'), 'checked': status.get('checked'),
        'pending': status.get('pending'), 'passed': status.get('passed'),
        'query': status.get('query'), 'quick': status.get('quick'),
        'candidates': status.get('candidates'), 'generated_at': status.get('generated_at'),
        'max_age_seconds': status.get('max_age_seconds'),
        'valid_until': status.get('valid_until'), 'expires_at': status.get('expires_at'),
        'stale': status.get('stale', False), 'complete': status.get('complete'),
        'sort': status.get('sort'), 'reputation': status.get('reputation'),
        'reader_state': status.get('reader_state'),
        'reader_detail': status.get('reader_detail'),
        'anonymity': status.get('anonymity'), 'source_quality': status.get('source_quality'),
        'source_health_basis': status.get('source_health_basis'),
        'breakdown': status.get('breakdown'),
        'selection_requested': status.get('selection_requested', 0),
        'selection_exported': status.get('selection_exported', 0),
        'selection_missing': status.get('selection_missing', []),
        'targets': status.get('targets', []), 'endpoints': ENDPOINTS,
    }
    return body
