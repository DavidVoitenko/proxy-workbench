"""User sources, subscriptions and feed lifecycle (F27, F13 acceptance side).

This module is a leaf service library.  It performs no network I/O, writes no
DDL, keeps no proxy normalizer of its own and never executes anything that
arrived in a document.  The boundaries it relies on:

* endpoint canonicalization is injected by the caller; the default resolves to
  ``proxytool.normalize_custom`` so a user's own subscription may name a
  hostname or a private address, while the SSRF gate stays where it already
  is -- in ``proxytool.collect()`` and ``_parse_source_url``;
* HTTP, conditional GET and the last-good cache belong to the collector;
  this module decides what a *result* means for a collection;
* the tables below are created by ``db.migrate()``.  ``REQUESTED_DDL`` is a
  string constant so the migrator and the tests cannot disagree about the
  shape; nothing in this module executes it.

Public API
----------
secrets      :func:`make_ref`, :func:`redact_url`, :func:`redact_headers`,
              :func:`resolve_url`, :func:`resolve_headers`,
              :func:`find_secret_leaks`
sources      :func:`user_source`, :class:`UserSource`
imports      :func:`import_subscription`, :func:`import_clash`,
              :func:`import_singbox`, :class:`ImportResult`
lifecycle    :class:`FeedState`, :class:`FeedResult`, :class:`FeedPolicy`,
              :func:`plan_refresh`, :func:`plan_rotation`,
              :func:`feed_diagnostics`, :func:`quota_from_response`
storage      :class:`SourceDesk`
comparison   :class:`Cohort`, :func:`compare_sources`, :func:`compare_suppliers`,
              :func:`compare_cohorts`, :func:`survival_across_windows`,
              :func:`provider_inventory`, :func:`wilson_interval`,
              :func:`classify_measurement` (F21)
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from dataclasses import dataclass, replace
from email.utils import parsedate_to_datetime
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit

__all__ = [
    'CLASH_TYPES', 'CLASH_UNSUPPORTED', 'DIAGNOSTIC_CODES', 'FAILURE_OUTCOMES',
    'ADAPTER_KINDS', 'FeedPolicy', 'FeedResult', 'FeedState', 'IMPORT_CODES',
    'ImportLimits', 'ImportResult',
    'ImportedEndpoint', 'MERGE_MODES', 'QuotaInfo', 'REQUESTED_DDL', 'RefreshPlan', 'RotationPlan',
    'SCRIPT_SECTIONS', 'Diagnostic', 'SINGBOX_TYPES', 'SourceDesk', 'SourceDeskError',
    'SUBSCRIPTION_FORMATS', 'SUPPORTED_OUTCOMES', 'UserSource', 'find_secret_leaks',
    'feed_diagnostics', 'import_clash', 'import_singbox', 'import_subscription', 'is_ref',
    'make_ref', 'parse_document', 'plan_refresh', 'plan_rotation', 'quota_from_response',
    'redact_headers', 'redact_url', 'resolve_headers', 'resolve_url', 'user_source',
    # F21
    'BIAS_CODES', 'BiasNote', 'Cohort', 'CostPerAdmitted', 'FAMILY_JACCARD_DEFAULT',
    'FetchState', 'Family', 'INERT_ACCESS_KINDS', 'OverlapPair', 'ProviderNote', 'SAMPLE_FLOOR',
    'STATUS_MEASURED', 'STATUS_NO_DATA', 'STATUS_NOT_COLLECTED', 'SourceComparison', 'SourceStats',
    'SupplierComparison', 'SurvivalStep', 'UNKNOWN_CODES', 'UNKNOWN_CODES_NOT_CONCLUSIVE',
    'classify_measurement', 'compare_cohorts', 'compare_sources', 'compare_suppliers',
    'provider_inventory', 'survival_across_windows', 'wilson_interval',
]

#: How many endpoint ids go into one ``IN (...)`` clause.  SQLite's default
#: ``SQLITE_MAX_VARIABLE_NUMBER`` is 999 on older builds, so a comparison never
#: builds a statement that silently overflows it.
_SQL_BATCH = 400

# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class SourceDeskError(ValueError):
    """A refused operation with a stable ``E_*`` code from CONTRACTS.ru.md 5.4."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------
# Secret references and redaction
# --------------------------------------------------------------------------

REF_RE = re.compile(r"\A(ref|href|url|auth)-[0-9a-f]{16}\Z")
SAFE_PATH_SEGMENT_RE = re.compile(r"\A[A-Za-z0-9._~-]{1,32}\Z")
# Anything longer than this in a path is treated as a capability, not a word:
# hex ids, base64 tokens and UUIDs all land here, and a redacted path segment
# costs the user nothing they can see anyway.
SECRET_SEGMENT_LENGTH = 12


def make_ref(prefix, *parts):
    """Return an opaque, stable reference id.

    The digest is taken over the parts so the same logical secret always maps
    to the same reference, and the reference itself reveals nothing: it is a
    prefix plus a truncated hash, never the value.
    """
    if not isinstance(prefix, str) or prefix not in ('ref', 'href', 'url', 'auth'):
        raise SourceDeskError('E_VALIDATION_FIELD', 'Некорректный тип ссылки на секрет.')
    if not parts or any(not isinstance(part, str) or not part for part in parts):
        raise SourceDeskError('E_VALIDATION_FIELD', 'Ссылка на секрет требует непустых частей.')
    digest = hashlib.sha256('\0'.join(parts).encode('utf-8')).hexdigest()[:16]
    return f'{prefix}-{digest}'


def is_ref(value):
    return isinstance(value, str) and bool(REF_RE.match(value))


def redact_url(value):
    """Return a URL that is safe to persist, display and serialize.

    Query, fragment and userinfo are dropped outright.  A path segment that
    looks like a capability is replaced with an ellipsis, because subscription
    URLs routinely carry the token in the path.
    """
    if not isinstance(value, str) or not value.strip():
        return ''
    raw = value.strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except (TypeError, ValueError):
        return ''
    host = (parsed.hostname or '').lower()
    if ':' in host and not host.startswith('['):
        host = f'[{host}]'
    authority = host + (f':{port}' if port else '')
    segments = []
    for segment in parsed.path.split('/'):
        if not segment:
            continue
        if len(segment) >= SECRET_SEGMENT_LENGTH or not SAFE_PATH_SEGMENT_RE.match(segment):
            segments.append('…')
        else:
            segments.append(segment)
    path = '/' + '/'.join(segments) if segments else ''
    return urlunsplit((parsed.scheme.lower(), authority, path, '', ''))


def redact_headers(headers):
    """Header names stay visible, header values never do.

    A value can be a capability (Authorization, Cookie, X-Auth-Token), and
    there is no honest way to tell a harmless one from a harmful one, so the
    contract is unconditional: no header value ever leaves this module.
    """
    if not isinstance(headers, Mapping):
        return {}
    return {str(name): '<redacted>' for name in sorted(headers, key=str)}


def resolve_url(ref, resolver):
    """Materialize a URL reference for the fetch layer only."""
    if not is_ref(ref) or not ref.startswith('url-'):
        raise SourceDeskError('E_VALIDATION_FIELD', 'Ожидалась ссылка вида url-<16 hex>.')
    if not callable(resolver):
        raise SourceDeskError('E_SECRET_VAULT_LOCKED', 'Нужен резолвер секретов.')
    try:
        value = resolver(ref)
    except Exception:
        # A vault that has no entry for the reference and a vault that returns
        # a wrong type are the same situation for the caller: not provided.
        value = None
    if not isinstance(value, str) or not value.strip():
        raise SourceDeskError('E_SECRET_NOT_PROVIDED', 'Ссылка на секрет не разрешилась в значение.')
    return value.strip()


def resolve_headers(refs, resolver):
    """Materialize header references into a request header mapping."""
    if not isinstance(refs, Mapping):
        raise SourceDeskError('E_VALIDATION_FIELD', 'Ожидался словарь ссылок на заголовки.')
    if not callable(resolver):
        raise SourceDeskError('E_SECRET_VAULT_LOCKED', 'Нужен резолвер секретов.')
    headers = {}
    for name in sorted(refs, key=str):
        ref = refs[name]
        if not is_ref(ref) or not ref.startswith('href-'):
            raise SourceDeskError('E_VALIDATION_FIELD', f'Ожидалась ссылка на заголовок: {name}.')
        try:
            value = resolver(ref)
        except Exception:
            value = None
        if not isinstance(value, str) or not value.strip():
            raise SourceDeskError('E_SECRET_NOT_PROVIDED', f'Ссылка на заголовок не разрешилась: {name}.')
        headers[str(name)] = value
    return headers


def _walk_strings(value, path=''):
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, bytes):
        yield path, value.decode('utf-8', 'replace')
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from _walk_strings(key, f'{path}/<key>')
            yield from _walk_strings(item, f'{path}/{key}')
    elif isinstance(value, (list, tuple, set, frozenset)):
        for index, item in enumerate(value):
            yield from _walk_strings(item, f'{path}/{index}')


def find_secret_leaks(value, needles):
    """Return the paths where any needle appears inside a structure.

    Used by the tests as the mechanical form of "a canary secret must not
    reach the database, a log, argv, a file or JSON", and available to the API
    layer as a last-line audit before a payload is published.
    """
    if isinstance(needles, str):
        needles = (needles,)
    wanted = [needle for needle in (needles or ()) if isinstance(needle, str) and needle]
    if not wanted:
        return []
    leaks = []
    for path, text in _walk_strings(value):
        for needle in wanted:
            if needle in text:
                leaks.append((path, needle))
                break
    return leaks


# --------------------------------------------------------------------------
# User sources and subscriptions
# --------------------------------------------------------------------------

SUBSCRIPTION_FORMATS = ('clash', 'singbox')
ADAPTER_KINDS = ('line', 'json-records', 'fields', 'page-json', 'html-table')
USER_SOURCE_FORMATS = ('text', 'line', 'json-records', 'fields', 'page-json', 'html-table') + SUBSCRIPTION_FORMATS
URL_SCHEMES = ('http', 'https')
CONTROL_CHAR_RE = re.compile(r"[\x00-\x20\x7f\\]")


@dataclass(frozen=True)
class UserSource:
    """A user-supplied feed.  ``url_ref`` and ``header_refs`` are references,
    never values; ``public_url`` is the only URL shape allowed out of here."""

    id: str
    name: str
    url_ref: str
    public_url: str
    format: str
    header_refs: tuple = ()
    access_ref: str | None = None
    access_id: str | None = None
    access_revision: int = 1

    def public_view(self):
        """The only representation that may be serialized or shown to a user."""
        return {
            'id': self.id,
            'name': self.name,
            'public_url': self.public_url,
            'url_ref': self.url_ref,
            'header_refs': {name: ref for name, ref in self.header_refs},
            'header_values': redact_headers({name: '' for name, _ in self.header_refs}),
            'format': self.format,
            'access_id': self.access_id,
            'access_revision': self.access_revision,
            'has_credentials': bool(self.access_ref),
        }


def user_source_id(binding_id, url, source_format):
    """Stable id for a user feed: the same URL in the same binding is the same
    source, and editing the URL creates a new one instead of re-pointing the
    old id at different data."""
    return make_ref('ref', 'source', str(binding_id), redact_url(url), str(source_format))


def _validate_source_url(value):
    if not isinstance(value, str) or not value.strip():
        raise SourceDeskError('E_VALIDATION_FIELD', 'Нужен HTTP/HTTPS URL источника.')
    raw = value.strip()
    if CONTROL_CHAR_RE.search(raw):
        raise SourceDeskError('E_VALIDATION_FIELD', 'Некорректный URL источника.')
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except (TypeError, ValueError):
        raise SourceDeskError('E_VALIDATION_FIELD', 'Некорректный URL или порт источника.') from None
    if parsed.scheme.lower() not in URL_SCHEMES:
        raise SourceDeskError('E_VALIDATION_FIELD', 'Нужен HTTP/HTTPS URL источника.')
    if parsed.username is not None or parsed.password is not None or '@' in raw.split('//', 1)[-1]:
        raise SourceDeskError('E_VALIDATION_FIELD', 'Логин и пароль в URL источника запрещены.')
    if '#' in raw or parsed.fragment:
        raise SourceDeskError('E_VALIDATION_FIELD', 'Fragment в URL источника запрещен.')
    host = (parsed.hostname or '').lower().rstrip('.')
    if not host or '..' in host or host.startswith('.'):
        raise SourceDeskError('E_VALIDATION_FIELD', 'Некорректный hostname источника.')
    if port is not None and not 1 <= port <= 65535:
        raise SourceDeskError('E_VALIDATION_FIELD', 'Некорректный порт источника.')
    return raw


def user_source(*, binding_id, url, name=None, source_format='text', headers=None,
                header_put=None, access_ref=None, access_id=None):
    """Build a :class:`UserSource` from a URL the user typed.

    ``headers`` maps a header name to its *value*.  The value is handed to
    ``header_put(name, value)`` -- the secret store -- and only the returned
    reference is kept.  Without a ``header_put`` callable a raw value is
    refused rather than silently retained, because there is no later point at
    which the desk could redact it.
    """
    if not isinstance(binding_id, str) or not binding_id:
        raise SourceDeskError('E_VALIDATION_FIELD', 'Нужен идентификатор привязки источника.')
    if source_format not in USER_SOURCE_FORMATS:
        raise SourceDeskError('E_VALIDATION_FIELD', f'Неподдерживаемый формат источника: {source_format!r}.')
    raw_url = _validate_source_url(url)
    if access_ref is not None and not is_ref(access_ref):
        raise SourceDeskError('E_VALIDATION_FIELD', 'Ожидалась ссылка вида auth-<16 hex> на учётные данные.')
    if access_ref is not None and not access_id:
        raise SourceDeskError('E_VALIDATION_FIELD', 'Учётные данные требуют access_id.')
    refs = []
    for header_name in sorted(headers or {}):
        if not isinstance(header_name, str) or not header_name.strip():
            raise SourceDeskError('E_VALIDATION_FIELD', 'Пустое имя заголовка запрещено.')
        if CONTROL_CHAR_RE.search(header_name):
            raise SourceDeskError('E_VALIDATION_FIELD', 'Некорректное имя заголовка.')
        if not callable(header_put):
            raise SourceDeskError('E_SECRET_VAULT_LOCKED',
                                  'Заголовок со значением требует хранилища секретов (header_put).')
        ref = header_put(header_name, headers[header_name])
        if not is_ref(ref) or not ref.startswith('href-'):
            raise SourceDeskError('E_VALIDATION_FIELD',
                                  f'Хранилище секретов вернуло некорректную ссылку для {header_name}.')
        refs.append((header_name, ref))
    return UserSource(
        id=user_source_id(binding_id, raw_url, source_format),
        name=name if isinstance(name, str) and name.strip() else redact_url(raw_url),
        url_ref=make_ref('url', binding_id, raw_url),
        public_url=redact_url(raw_url),
        format=source_format,
        header_refs=tuple(refs),
        access_ref=access_ref,
        access_id=access_id,
        access_revision=1,
    )


# --------------------------------------------------------------------------
# Supported subscription imports (F27)
# --------------------------------------------------------------------------
#
# A Clash or sing-box document is data, never a program.  Only the endpoint
# list is read: sections that steer traffic (rules, rule-providers, groups,
# providers, scripts, tun, dns, listeners) are recorded as ignored, and a
# group is never recursed into.  No eval, no import, no exec, no subprocess.

CLASH_TYPES = {
    'http': 'http',
    'socks': 'socks5',
    'socks5': 'socks5',
}
# Clash outbounds we deliberately refuse: the address is real, but the
# transport is not one this application can check or export.
CLASH_UNSUPPORTED = ('ss', 'ssr', 'vmess', 'vless', 'trojan', 'snell', 'hysteria',
                     'hysteria2', 'tuic', 'wireguard', 'ssh', 'anytls', 'http-connect',
                     'sniffer', 'mieru', 'juicity')
SINGBOX_TYPES = {
    'http': 'http',
    'socks': 'socks5',
}
SINGBOX_SOCKS_VERSIONS = {'4': 'socks4', '4a': 'socks4', '5': 'socks5'}
SINGBOX_NOT_A_PROXY = ('direct', 'block', 'dns')
# Sections that steer traffic or fetch code.  They are never read and never run.
SCRIPT_SECTIONS = ('script', 'scripts', 'rule-providers', 'proxy-providers', 'script-path',
                   'listeners', 'tun', 'dns', 'sniffer', 'hosts', 'profile')
ALLOWED_META_KEYS = ('country', 'provider', 'anonymity', 'name', 'supports_https')

IMPORT_CODES = (
    'E_IMPORT_FORMAT', 'E_IMPORT_REVISION', 'E_IMPORT_PARTIAL',
    'E_VALIDATION_FIELD', 'E_VALIDATION_SCHEMA', 'E_LIMIT_BODY', 'E_SECRET_CREDENTIALS',
)

MAX_IMPORT_BYTES = 8 * 1024 * 1024
MAX_IMPORT_LINE = 64 * 1024
MAX_IMPORT_DEPTH = 32
MAX_IMPORT_NODES = 400_000
MAX_IMPORT_ENTRIES = 200_000
MAX_IMPORT_STRING = 64 * 1024


@dataclass(frozen=True)
class ImportLimits:
    max_bytes: int = MAX_IMPORT_BYTES
    max_entries: int = MAX_IMPORT_ENTRIES
    max_nodes: int = MAX_IMPORT_NODES
    max_depth: int = MAX_IMPORT_DEPTH
    max_string: int = MAX_IMPORT_STRING

    @classmethod
    def check(cls, value):
        if not isinstance(value, cls):
            raise SourceDeskError('E_VALIDATION_FIELD', 'Ожидался ImportLimits.')
        for name in ('max_bytes', 'max_entries', 'max_nodes', 'max_depth', 'max_string'):
            number = getattr(value, name)
            if not isinstance(number, int) or number < 1:
                raise SourceDeskError('E_VALIDATION_FIELD', f'ImportLimits.{name} должен быть положительным целым.')
        return value


@dataclass(frozen=True)
class ImportedEndpoint:
    """One canonical endpoint plus the metadata the document *declared*.

    Declared values are a claim of the document, kept apart from anything the
    application measures (source-system-design 9.1).
    """

    endpoint: str
    declared: tuple = ()
    label: str = ''


@dataclass(frozen=True)
class ImportResult:
    outcome: str
    endpoints: tuple = ()
    rejected: tuple = ()
    ignored: tuple = ()

    @property
    def canonical(self):
        return tuple(entry.endpoint for entry in self.endpoints)

    def summary(self):
        return {
            'outcome': self.outcome,
            'endpoints': [entry.endpoint for entry in self.endpoints],
            'declared': {entry.endpoint: dict(entry.declared) for entry in self.endpoints},
            'labels': [entry.label for entry in self.endpoints],
            'rejected': [list(item) for item in self.rejected],
            'ignored': list(self.ignored),
        }


def _default_normalizer():
    """The single canonicalization point stays in ``proxytool``."""
    from .proxytool import normalize_custom
    return normalize_custom


def _normalize_endpoint(value, normalize, label, counters):
    counters['records'] += 1
    if not isinstance(value, str) or not value.strip():
        counters['rejected'] += 1
        return None, ('E_VALIDATION_FIELD', label or '?')
    try:
        canonical = normalize(value)
    except Exception:  # a normalizer must not be able to abort the import
        canonical = None
    if not canonical:
        counters['rejected'] += 1
        return None, ('E_VALIDATION_FIELD', label or '?')
    return canonical, None


def _declared_meta(raw, label, supports_https):
    declared = []
    if label:
        declared.append(('name', label))
    if supports_https:
        declared.append(('supports_https', 'true'))
    for key in ('country', 'provider', 'anonymity'):
        value = raw.get(key)
        if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value).strip():
            declared.append((key, str(value).strip()[:64]))
    return tuple(declared)


def _credentials_present(raw):
    return any(isinstance(raw.get(key), str) and raw[key].strip()
               for key in ('username', 'password', 'user', 'pass'))


def _finish(outcome, endpoints, rejected, ignored, counters, limits):
    if counters.get('nodes', 0) > limits.max_nodes:
        outcome = 'partial' if endpoints else 'limit_exceeded'
    if not endpoints:
        if outcome == 'ok':
            outcome = 'empty'
    elif outcome == 'ok' and rejected:
        outcome = 'partial'
    return ImportResult(outcome=outcome, endpoints=tuple(endpoints),
                        rejected=tuple(rejected), ignored=tuple(sorted(set(ignored))))


def _decode(document, limits, counters):
    """Decode a document, or return the import result that reports why not.

    A budget breach is its own outcome: it is not the same as a document that
    is malformed, and the caller has to tell them apart.
    """
    try:
        config = parse_document(document, limits=limits, counters=counters)
    except SourceDeskError as exc:
        outcome = 'limit_exceeded' if exc.code == 'E_LIMIT_BODY' else 'invalid'
        return ImportResult(outcome=outcome, rejected=((exc.code, exc.message),), ignored=())
    if not isinstance(config, Mapping):
        return ImportResult(outcome='invalid',
                            rejected=(('E_VALIDATION_SCHEMA', 'Ожидался объект конфигурации.'),), ignored=())
    return config


def import_clash(document, *, normalize=None, limits=None):
    """Read the ``proxies`` list of a Clash / Mihomo configuration.

    Supported: JSON (valid YAML subset) and a bounded block-YAML subset --
    block mappings, block sequences, plain and quoted scalars.  Anchors,
    aliases, tags, block scalars and multi-line flow collections are refused
    with ``E_IMPORT_FORMAT`` rather than approximated.
    """
    limits = ImportLimits.check(limits or ImportLimits())
    normalize = normalize or _default_normalizer()
    counters = {'records': 0, 'rejected': 0, 'nodes': 0}
    parsed = _decode(document, limits, counters)
    if isinstance(parsed, ImportResult):
        return parsed
    config = parsed
    entries = config.get('proxies')
    ignored = []
    for key in sorted(config):
        if key == 'proxies':
            continue
        ignored.append(f'section:{key}')
        if str(key).lower().split('-')[0] in ('script', 'rule', 'proxy', 'listener', 'tun', 'dns',
                                             'sniffer', 'host', 'profile'):
            ignored.append(f'script:{key}')
    if entries is None:
        return ImportResult(outcome='empty', ignored=tuple(ignored))
    if not isinstance(entries, list):
        return ImportResult(outcome='invalid', rejected=(('E_VALIDATION_SCHEMA', 'proxies должен быть списком.'),),
                            ignored=tuple(ignored))
    endpoints, rejected = [], []
    for item in entries[:limits.max_entries]:
        if not isinstance(item, Mapping):
            counters['rejected'] += 1
            rejected.append(('E_VALIDATION_FIELD', repr(item)[:64]))
            continue
        label = str(item.get('name') or '')[:64]
        proxy_type = str(item.get('type') or '').strip().lower()
        if proxy_type not in CLASH_TYPES:
            counters['rejected'] += 1
            rejected.append(('E_IMPORT_FORMAT', label or proxy_type))
            continue
        if _credentials_present(item):
            counters['rejected'] += 1
            rejected.append(('E_SECRET_CREDENTIALS', label or proxy_type))
            continue
        server, port = item.get('server'), item.get('port')
        if not isinstance(server, str) or not isinstance(port, int) or isinstance(port, bool):
            counters['rejected'] += 1
            rejected.append(('E_VALIDATION_FIELD', label or proxy_type))
            continue
        scheme = CLASH_TYPES[proxy_type]
        supports_https = bool(item.get('tls')) and scheme == 'http'
        if supports_https:
            scheme = 'https'
        canonical, reason = _normalize_endpoint(f'{scheme}://{server}:{port}', normalize, label, counters)
        if reason is not None:
            rejected.append(reason)
            continue
        endpoints.append(ImportedEndpoint(endpoint=canonical,
                                          declared=_declared_meta(item, label, supports_https),
                                          label=label))
    return _finish('ok', endpoints, rejected, ignored, counters, limits)


def import_singbox(document, *, normalize=None, limits=None):
    """Read the ``outbounds`` list of a sing-box configuration.

    Only ``http`` and ``socks`` outbounds become endpoints.  ``selector`` and
    ``urltest`` groups are skipped without recursion, ``route.rules`` and every
    other section are ignored, and an outbound type this application cannot
    check is reported as unsupported rather than silently dropped.
    """
    limits = ImportLimits.check(limits or ImportLimits())
    normalize = normalize or _default_normalizer()
    counters = {'records': 0, 'rejected': 0, 'nodes': 0}
    parsed = _decode(document, limits, counters)
    if isinstance(parsed, ImportResult):
        return parsed
    config = parsed
    outbounds = config.get('outbounds')
    ignored = [f'section:{key}' for key in sorted(config) if key not in ('outbounds',)]
    if outbounds is None:
        return ImportResult(outcome='empty', ignored=tuple(ignored))
    if not isinstance(outbounds, list):
        return ImportResult(outcome='invalid',
                            rejected=(('E_VALIDATION_SCHEMA', 'outbounds должен быть списком.'),),
                            ignored=tuple(ignored))
    endpoints, rejected = [], []
    for item in outbounds[:limits.max_entries]:
        if not isinstance(item, Mapping):
            counters['rejected'] += 1
            rejected.append(('E_VALIDATION_FIELD', repr(item)[:64]))
            continue
        label = str(item.get('tag') or '')[:64]
        outbound_type = str(item.get('type') or '').strip().lower()
        if outbound_type in SINGBOX_NOT_A_PROXY:
            ignored.append(f'outbound:{outbound_type}:{label}' if label else f'outbound:{outbound_type}')
            continue
        if outbound_type in ('selector', 'urltest'):
            # A group references other outbounds.  Reading it would be reading
            # routing intent, and recursing would import members twice.
            ignored.append(f'outbound:{outbound_type}:{label}' if label else f'outbound:{outbound_type}')
            continue
        if outbound_type not in SINGBOX_TYPES:
            # An unsupported transport is reported as unsupported even when it
            # carries credentials: "this cannot be imported" is the honest
            # answer, and the value is dropped either way.
            counters['rejected'] += 1
            rejected.append(('E_IMPORT_FORMAT', label or outbound_type))
            continue
        if _credentials_present(item):
            counters['rejected'] += 1
            rejected.append(('E_SECRET_CREDENTIALS', label or outbound_type))
            continue
        server, port = item.get('server'), item.get('server_port')
        if not isinstance(server, str) or not isinstance(port, int) or isinstance(port, bool):
            counters['rejected'] += 1
            rejected.append(('E_VALIDATION_FIELD', label or outbound_type))
            continue
        if outbound_type == 'socks':
            version = SINGBOX_SOCKS_VERSIONS.get(str(item.get('version') or '').strip())
            if version is None:
                # A default here would be a guess; the document has to say.
                counters['rejected'] += 1
                rejected.append(('E_VALIDATION_FIELD', label or 'socks:version'))
                continue
            scheme = version
            supports_https = False
        else:
            tls = item.get('tls')
            supports_https = isinstance(tls, Mapping) and bool(tls.get('enabled'))
            scheme = 'https' if supports_https else 'http'
        canonical, reason = _normalize_endpoint(f'{scheme}://{server}:{port}', normalize, label, counters)
        if reason is not None:
            rejected.append(reason)
            continue
        endpoints.append(ImportedEndpoint(endpoint=canonical,
                                          declared=_declared_meta(item, label, supports_https),
                                          label=label))
    return _finish('ok', endpoints, rejected, ignored, counters, limits)


_IMPORTERS = {'clash': import_clash, 'singbox': import_singbox}


def import_subscription(document, source_format, *, normalize=None, limits=None):
    """Dispatch on the declared subscription format.  Unknown formats raise."""
    importer = _IMPORTERS.get(source_format)
    if importer is None:
        raise SourceDeskError('E_VALIDATION_FIELD', f'Формат подписки не поддерживается: {source_format!r}.')
    return importer(document, normalize=normalize, limits=limits)


# --------------------------------------------------------------------------
# Bounded YAML subset reader
# --------------------------------------------------------------------------

_NUMBER_RE = re.compile(r"\A[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?\Z")
_UNSUPPORTED_YAML_RE = re.compile(r"^\s*(?:&\S|\*\S|!\S|\||>|\?|:)\s|<<:")


def _scalar(text, limits, counters):
    if len(text) > limits.max_string:
        raise SourceDeskError('E_LIMIT_BODY', 'Слишком длинное значение в документе.')
    counters['nodes'] += 1
    if counters['nodes'] > limits.max_nodes:
        raise SourceDeskError('E_LIMIT_BODY', 'Документ превышает предел узлов.')
    raw = text.strip()
    if not raw or raw in ('~', 'null', 'Null', 'NULL'):
        return None
    if raw[0] == '"' and raw[-1] == '"' and len(raw) >= 2:
        return raw[1:-1]
    if raw[0] == "'" and raw[-1] == "'" and len(raw) >= 2:
        return raw[1:-1].replace("''", "'")
    if raw in ('true', 'True', 'TRUE'):
        return True
    if raw in ('false', 'False', 'FALSE'):
        return False
    if _NUMBER_RE.match(raw):
        return float(raw) if ('.' in raw or 'e' in raw or 'E' in raw) else int(raw)
    if raw[0] in '[{':
        # formats.py itself emits JSON flow mappings, so one-line flow is
        # part of the supported subset rather than a special case.
        try:
            value = json.loads(raw)
        except ValueError:
            raise SourceDeskError('E_IMPORT_FORMAT', 'Неподдерживаемая конструкция YAML.') from None
        counters['nodes'] += 1
        return value
    return raw


def _strip_comment(line):
    out, quote = [], None
    for index, char in enumerate(line):
        if quote:
            out.append(char)
            if char == quote and line[index - 1:index] != '\\':
                quote = None
            continue
        if char in '"\'':
            quote = char
            out.append(char)
            continue
        if char == '#' and (index == 0 or line[index - 1] in ' \t'):
            break
        out.append(char)
    return ''.join(out).rstrip()


def _tokenize(text, limits):
    lines = []
    for number, raw in enumerate(text.split('\n'), start=1):
        if len(raw) > limits.max_string:
            raise SourceDeskError('E_LIMIT_BODY', f'Слишком длинная строка {number} в документе.')
        if '\t' in raw[:len(raw) - len(raw.lstrip(' \t'))]:
            raise SourceDeskError('E_IMPORT_FORMAT', f'Табуляция в отступах, строка {number}.')
        body = _strip_comment(raw)
        if not body.strip() or body.strip() in ('---', '...'):
            continue
        if _UNSUPPORTED_YAML_RE.match(body):
            raise SourceDeskError('E_IMPORT_FORMAT', f'Неподдерживаемая конструкция YAML, строка {number}.')
        indent = len(body) - len(body.lstrip(' '))
        lines.append((indent, body.strip(), number))
        if len(lines) > limits.max_nodes:
            raise SourceDeskError('E_LIMIT_BODY', 'Документ превышает предел узлов.')
    return lines


def _parse_block(lines, index, indent, limits, counters, depth):
    if depth > limits.max_depth:
        raise SourceDeskError('E_LIMIT_BODY', 'Документ слишком глубоко вложен.')
    if index >= len(lines):
        return None, index
    if lines[index][1].startswith('- '):
        return _parse_sequence(lines, index, indent, limits, counters, depth)
    return _parse_mapping(lines, index, indent, limits, counters, depth)


def _parse_sequence(lines, index, indent, limits, counters, depth):
    items = []
    while index < len(lines):
        line_indent, content, number = lines[index]
        if line_indent < indent or not (content == '-' or content.startswith('- ')):
            break
        if line_indent > indent:
            raise SourceDeskError('E_IMPORT_FORMAT', f'Неверный отступ, строка {number}.')
        rest = content[1:].strip()
        index += 1
        if not rest:
            value, index = _parse_block(lines, index, line_indent + 1, limits, counters, depth + 1)
            items.append(value)
            continue
        if ':' in rest and not rest.startswith(('"', "'", '[', '{')):
            # "- key: value" opens a mapping whose first key sits on this line.
            synthetic = [(line_indent + 2, rest, number)]
            while index < len(lines) and lines[index][0] > line_indent:
                synthetic.append(lines[index])
                index += 1
            value, _ = _parse_mapping(synthetic, 0, line_indent + 2, limits, counters, depth + 1)
            items.append(value)
            continue
        items.append(_scalar(rest, limits, counters))
        if len(items) > limits.max_nodes:
            raise SourceDeskError('E_LIMIT_BODY', 'Документ превышает предел узлов.')
    return items, index


def _parse_mapping(lines, index, indent, limits, counters, depth):
    result = {}
    while index < len(lines):
        line_indent, content, number = lines[index]
        if line_indent < indent:
            break
        if line_indent > indent:
            raise SourceDeskError('E_IMPORT_FORMAT', f'Неверный отступ, строка {number}.')
        if content.startswith('- '):
            break
        key, separator, rest = _split_key(content, number)
        index += 1
        rest = rest.strip()
        if rest:
            result[key] = _scalar(rest, limits, counters)
            continue
        if index < len(lines) and lines[index][0] > line_indent:
            value, index = _parse_block(lines, index, lines[index][0], limits, counters, depth + 1)
        elif index < len(lines) and lines[index][0] == line_indent and lines[index][1].startswith('- '):
            value, index = _parse_sequence(lines, index, line_indent, limits, counters, depth + 1)
        else:
            value = None
        result[key] = value
        if len(result) > limits.max_nodes:
            raise SourceDeskError('E_LIMIT_BODY', 'Документ превышает предел узлов.')
    return result, index


def _split_key(content, number):
    quote = None
    for index, char in enumerate(content):
        if quote:
            if char == quote:
                quote = None
            continue
        if char in '"\'':
            quote = char
            continue
        if char == ':' and (index + 1 == len(content) or content[index + 1] in ' \t'):
            key = content[:index].strip()
            if len(key) >= 2 and key[0] == key[-1] and key[0] in '"\'':
                key = key[1:-1]
            if not key:
                raise SourceDeskError('E_IMPORT_FORMAT', f'Пустой ключ, строка {number}.')
            return key, ':', content[index + 1:]
    raise SourceDeskError('E_IMPORT_FORMAT', f'Ожидался "ключ: значение", строка {number}.')


def parse_document(document, *, limits=None, counters=None):
    """Decode a subscription document: JSON first, then the YAML subset."""
    limits = ImportLimits.check(limits or ImportLimits())
    counters = counters if counters is not None else {}
    counters.setdefault('nodes', 0)
    if isinstance(document, (bytes, bytearray)):
        if len(document) > limits.max_bytes:
            raise SourceDeskError('E_LIMIT_BODY', 'Документ превышает предел размера.')
        try:
            text = bytes(document).decode('utf-8')
        except UnicodeDecodeError:
            raise SourceDeskError('E_IMPORT_FORMAT', 'Документ не в UTF-8.') from None
    elif isinstance(document, str):
        text = document
        if len(text.encode('utf-8', 'replace')) > limits.max_bytes:
            raise SourceDeskError('E_LIMIT_BODY', 'Документ превышает предел размера.')
    elif isinstance(document, Mapping):
        return dict(document)
    else:
        raise SourceDeskError('E_VALIDATION_SCHEMA', 'Ожидались байты, строка или объект конфигурации.')
    stripped = text.lstrip()
    if stripped[:1] in ('{', '['):
        try:
            return json.loads(text)
        except ValueError:
            pass
    lines = _tokenize(text, limits)
    if not lines:
        return {}
    value, _ = _parse_block(lines, 0, lines[0][0], limits, counters, 0)
    return value


# --------------------------------------------------------------------------
# Feed lifecycle: refresh, delta, merge/replace, last-good, expiry
# --------------------------------------------------------------------------

SUPPORTED_OUTCOMES = (
    'ok', 'partial', 'empty', 'not_modified',
    'rate_limited', 'quota_exhausted', 'token_expired',
    'invalid', 'too_large', 'unavailable',
)
#: An outcome that proves nothing about the content of the feed.  None of them
#: may remove a membership or move the last-good pointer.
FAILURE_OUTCOMES = ('rate_limited', 'quota_exhausted', 'token_expired', 'invalid', 'too_large', 'unavailable')
MERGE_MODES = ('merge', 'replace')

REASON_CODES = {
    'ok': 'applied',
    'partial': 'applied_partial',
    'empty': 'empty_no_removal',
    'not_modified': 'not_modified',
    'rate_limited': 'rate_limited',
    'quota_exhausted': 'quota_exhausted',
    'token_expired': 'token_expired',
    'invalid': 'invalid_payload',
    'too_large': 'limit_exceeded',
    'unavailable': 'unavailable',
}

DIAGNOSTIC_CODES = (
    'E_SOURCE_QUOTA_EXHAUSTED', 'E_SOURCE_QUOTA_LOW', 'E_SOURCE_QUOTA_UNKNOWN',
    'E_SOURCE_TOKEN_EXPIRED', 'E_SOURCE_TOKEN_EXPIRING', 'E_SOURCE_STALE',
    'E_SOURCE_EXPIRED', 'E_SOURCE_QUARANTINED', 'E_SOURCE_RETRY_AFTER',
    'E_SOURCE_EMPTY_FEED', 'E_SOURCE_UNAVAILABLE', 'E_SOURCE_ACCESS_REVISION_CHANGED',
    'E_IMPORT_FORMAT', 'E_LIMIT_BODY',
)

DEFAULT_TTL_SECONDS = 6 * 3600
DEFAULT_STALE_AFTER_SECONDS = 24 * 3600
DEFAULT_DROP_AFTER_SECONDS = 7 * 24 * 3600
DEFAULT_QUOTA_LOW_THRESHOLD = 0.1
DEFAULT_TOKEN_EXPIRY_SLACK = 3600
BACKOFF_SECONDS = (60, 300, 1800, 7200, 21600)
QUARANTINE_AFTER_FAILURES = 3


@dataclass(frozen=True)
class QuotaInfo:
    """Quota and token evidence as declared by the provider."""

    limit: int | None = None
    remaining: int | None = None
    reset_at: float | None = None
    token_expires_at: float | None = None
    origin: str = 'header'

    @property
    def exhausted(self):
        return self.remaining is not None and self.remaining <= 0


@dataclass(frozen=True)
class FeedState:
    """Everything the desk persists per (source, collection)."""

    source_id: str
    collection_id: str
    mode: str = 'merge'
    active: tuple = ()
    last_good: tuple = ()
    last_attempt_at: float | None = None
    last_success_at: float | None = None
    last_good_at: float | None = None
    expires_at: float | None = None
    next_attempt_at: float | None = None
    retry_after: float | None = None
    quarantine_until: float | None = None
    consecutive_failures: int = 0
    etag: str | None = None
    last_modified: str | None = None
    body_sha256: str | None = None
    access_id: str | None = None
    access_ref: str | None = None
    access_revision: int = 1
    last_outcome: str | None = None
    last_error: str | None = None
    last_validated_at: float | None = None

    def __post_init__(self):
        if self.mode not in MERGE_MODES:
            raise SourceDeskError('E_VALIDATION_FIELD', f'Неизвестный режим слияния: {self.mode!r}.')
        if not isinstance(self.consecutive_failures, int) or self.consecutive_failures < 0:
            raise SourceDeskError('E_VALIDATION_FIELD', 'consecutive_failures должен быть неотрицательным целым.')


@dataclass(frozen=True)
class FeedResult:
    """What the network layer observed.  This module never performs the fetch."""

    outcome: str
    fetched_at: float
    entries: tuple = ()
    status: int | None = None
    etag: str | None = None
    last_modified: str | None = None
    body_sha256: str | None = None
    retry_after: float | None = None
    quota: QuotaInfo | None = None
    error: str | None = None

    def __post_init__(self):
        if self.outcome not in SUPPORTED_OUTCOMES:
            raise SourceDeskError('E_VALIDATION_FIELD', f'Неизвестный результат обновления: {self.outcome!r}.')
        if not isinstance(self.fetched_at, (int, float)) or isinstance(self.fetched_at, bool):
            raise SourceDeskError('E_VALIDATION_FIELD', 'fetched_at должен быть числом (unix seconds).')
        if self.outcome not in ('ok', 'partial') and self.entries:
            raise SourceDeskError('E_VALIDATION_FIELD',
                                  f'Результат {self.outcome!r} не может содержать записи.')


@dataclass(frozen=True)
class FeedPolicy:
    mode: str = 'merge'
    ttl_seconds: float = DEFAULT_TTL_SECONDS
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS
    drop_after_seconds: float | None = DEFAULT_DROP_AFTER_SECONDS
    quota_low_threshold: float = DEFAULT_QUOTA_LOW_THRESHOLD
    token_expiry_slack_seconds: float = DEFAULT_TOKEN_EXPIRY_SLACK

    def __post_init__(self):
        if self.mode not in MERGE_MODES:
            raise SourceDeskError('E_VALIDATION_FIELD', f'Неизвестный режим слияния: {self.mode!r}.')
        for name in ('ttl_seconds', 'stale_after_seconds', 'token_expiry_slack_seconds'):
            value = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                raise SourceDeskError('E_VALIDATION_FIELD', f'{name} должен быть положительным числом.')
        if not isinstance(self.quota_low_threshold, (int, float)) or not 0 <= self.quota_low_threshold <= 1:
            raise SourceDeskError('E_VALIDATION_FIELD', 'quota_low_threshold должен быть долей от 0 до 1.')
        if self.drop_after_seconds is not None and self.drop_after_seconds <= 0:
            raise SourceDeskError('E_VALIDATION_FIELD', 'drop_after_seconds должен быть положительным или None.')


@dataclass(frozen=True)
class Diagnostic:
    code: str
    detail: str = ''
    at: float = 0.0
    retry_after: float | None = None

    def __post_init__(self):
        if self.code not in DIAGNOSTIC_CODES:
            raise SourceDeskError('E_VALIDATION_FIELD', f'Неизвестный код диагностики: {self.code!r}.')


@dataclass(frozen=True)
class RefreshPlan:
    """The decision, not the write.  ``apply_plan`` is what touches the store."""

    source_id: str
    collection_id: str
    mode: str
    outcome: str
    reason_code: str
    at: float
    applied: bool
    applied_removals: bool
    promote_last_good: bool
    serve_from_last_good: bool
    added: tuple = ()
    removed: tuple = ()
    kept: tuple = ()
    retained_shared: tuple = ()
    expires_at: float | None = None
    next_attempt_at: float | None = None
    diagnostics: tuple = ()
    next_state: object = None

    @property
    def delta(self):
        """What actually changes in the collection if this plan is applied."""
        return frozenset(self.added) | frozenset(self.removed)

    @property
    def blocked(self):
        return not self.applied

    def summary(self):
        return {
            'source_id': self.source_id,
            'collection_id': self.collection_id,
            'mode': self.mode,
            'outcome': self.outcome,
            'reason_code': self.reason_code,
            'applied': self.applied,
            'applied_removals': self.applied_removals,
            'promote_last_good': self.promote_last_good,
            'served_from_last_good': self.serve_from_last_good,
            'added': list(self.added),
            'removed': list(self.removed),
            'kept': list(self.kept),
            'retained_shared': list(self.retained_shared),
            'delta': sorted(self.delta),
            'expires_at': self.expires_at,
            'next_attempt_at': self.next_attempt_at,
            'diagnostics': [item.code for item in self.diagnostics],
        }


def _backoff_seconds(failures):
    index = min(max(failures - 1, 0), len(BACKOFF_SECONDS) - 1)
    return BACKOFF_SECONDS[index]


def _quota_diagnostics(quota, state, now, policy):
    found = []
    if quota is None:
        if state.access_ref:
            # A feed that needs a token and reports no quota is unknown, not empty.
            found.append(Diagnostic('E_SOURCE_QUOTA_UNKNOWN', 'Квота источника не объявлена.', now))
        return found
    if quota.exhausted:
        found.append(Diagnostic('E_SOURCE_QUOTA_EXHAUSTED', 'Провайдер сообщил об исчерпании квоты.',
                                now, quota.reset_at))
    elif quota.remaining is not None and quota.limit:
        left = quota.remaining / quota.limit
        if left <= policy.quota_low_threshold:
            found.append(Diagnostic('E_SOURCE_QUOTA_LOW',
                                    f'Остаток квоты {quota.remaining} из {quota.limit}.', now, quota.reset_at))
    if quota.token_expires_at is not None:
        if quota.token_expires_at <= now:
            found.append(Diagnostic('E_SOURCE_TOKEN_EXPIRED', 'Срок действия токена истёк.', now))
        elif quota.token_expires_at <= now + policy.token_expiry_slack_seconds:
            found.append(Diagnostic('E_SOURCE_TOKEN_EXPIRING', 'Срок действия токена скоро истечёт.',
                                    now, quota.token_expires_at))
    return found


def feed_diagnostics(state, *, now, policy=None, quota=None):
    """Explain the state of a feed: expiry, quarantine, quota, token.

    ``unknown`` stays unknown.  A feed that never reported a quota is not
    called exhausted, and a feed with no known expiry is not called fresh.
    """
    policy = policy or FeedPolicy(mode=state.mode)
    found = list(_quota_diagnostics(quota, state, now, policy))
    if state.quarantine_until and state.quarantine_until > now:
        found.append(Diagnostic('E_SOURCE_QUARANTINED', 'Источник в карантине после повторных отказов.',
                                now, state.quarantine_until))
    if state.retry_after and state.retry_after > now:
        found.append(Diagnostic('E_SOURCE_RETRY_AFTER', 'Следующая попытка не раньше ответа провайдера.',
                                now, state.retry_after))
    if state.last_outcome == 'empty' and state.last_good:
        found.append(Diagnostic('E_SOURCE_EMPTY_FEED', 'Источник вернул пустой ответ; показаны последние данные.',
                                now))
    if state.expires_at is not None and state.expires_at <= now:
        dropped = policy.drop_after_seconds is None or now > state.expires_at + policy.drop_after_seconds
        found.append(Diagnostic('E_SOURCE_EXPIRED' if dropped else 'E_SOURCE_STALE',
                                'Данные источника устарели.', now, state.expires_at))
    return tuple(found)


def plan_refresh(state, result, *, policy=None, now=None, foreign=()):
    """Decide what a fetch result does to a collection.

    Invariants, in order of importance:

    1. a failure never removes a membership and never moves last-good;
    2. an empty body never removes a membership either -- a working collection
       survives an empty update (F27 acceptance);
    3. a truncated (``partial``) fetch never removes a membership, because a
       truncated document cannot prove that an endpoint is gone;
    4. a removal only ever drops the rows *this* source owns, so an endpoint
       another source also contributes keeps its foreign membership (F27
       acceptance: intersections do not delete foreign membership).  Such
       endpoints are reported in ``retained_shared`` instead of vanishing
       from the collection;
    5. ``304`` validates transport only: no membership change, no expiry
       extension;
    6. expiry is written once, from a complete success, and never by a
       re-export or a failure.
    """
    policy = policy or FeedPolicy(mode=state.mode)
    if not isinstance(state, FeedState) or not isinstance(result, FeedResult):
        raise SourceDeskError('E_VALIDATION_FIELD', 'Ожидались FeedState и FeedResult.')
    if policy.mode not in MERGE_MODES:
        raise SourceDeskError('E_VALIDATION_FIELD', 'Неизвестный режим слияния.')
    at = float(now if now is not None else result.fetched_at)
    mode = policy.mode if policy.mode in MERGE_MODES else state.mode
    reason = REASON_CODES[result.outcome]
    active = tuple(dict.fromkeys(state.active))
    diagnostics = list(feed_diagnostics(state, now=at, policy=policy, quota=result.quota))
    next_attempt_at = None
    expires_at = state.expires_at

    if result.outcome in FAILURE_OUTCOMES:
        failures = state.consecutive_failures + 1
        backoff = at + _backoff_seconds(failures)
        server_retry = result.retry_after if isinstance(result.retry_after, (int, float)) else None
        next_attempt_at = max(backoff, server_retry) if server_retry else backoff
        quarantine = at + _backoff_seconds(failures) if failures >= QUARANTINE_AFTER_FAILURES else None
        return RefreshPlan(
            source_id=state.source_id, collection_id=state.collection_id, mode=mode,
            outcome=result.outcome, reason_code=reason, at=at, applied=False, applied_removals=False,
            promote_last_good=False, serve_from_last_good=bool(state.last_good),
            expires_at=state.expires_at, next_attempt_at=next_attempt_at,
            diagnostics=tuple(diagnostics) + (Diagnostic(_code_for_outcome(result.outcome),
                                                         result.error or reason, at, server_retry),),
            next_state=replace(state, last_attempt_at=at, consecutive_failures=failures,
                               next_attempt_at=next_attempt_at, quarantine_until=quarantine,
                               retry_after=server_retry, last_outcome=result.outcome,
                               last_error=result.error),
        )

    if result.outcome == 'not_modified':
        # Transport proof only: no membership, no generation, no expiry.
        return RefreshPlan(
            source_id=state.source_id, collection_id=state.collection_id, mode=mode,
            outcome=result.outcome, reason_code=reason, at=at, applied=False, applied_removals=False,
            promote_last_good=False, serve_from_last_good=bool(state.last_good or active),
            expires_at=state.expires_at, next_attempt_at=state.next_attempt_at,
            diagnostics=tuple(diagnostics),
            next_state=replace(state, last_attempt_at=at, last_validated_at=at,
                               consecutive_failures=0, next_attempt_at=None, quarantine_until=None,
                               retry_after=None, last_outcome=result.outcome, last_error=None,
                               etag=result.etag or state.etag,
                               last_modified=result.last_modified or state.last_modified),
        )

    endpoints = tuple(dict.fromkeys(getattr(entry, 'endpoint', entry) for entry in result.entries))
    incoming = set(endpoints)
    previous = set(active)
    added = tuple(sorted(incoming - previous))
    kept = tuple(sorted(incoming & previous))
    gone = previous - incoming
    # Only a complete fetch in replace mode may claim that a row is gone.
    may_remove = mode == 'replace' and result.outcome == 'ok'
    removed = tuple(sorted(gone)) if may_remove else ()
    retained_shared = tuple(sorted(gone & set(foreign))) if gone and foreign else ()
    # Whatever this fetch could not prove is simply kept: the active set may
    # only shrink on a complete fetch in replace mode.
    new_active = tuple(sorted(incoming)) if may_remove else tuple(sorted(previous | incoming))
    promote = result.outcome == 'ok'

    if promote:
        last_good, last_good_at, expires_at = new_active, at, at + policy.ttl_seconds
    else:
        last_good, last_good_at = state.last_good, state.last_good_at
    if result.outcome == 'empty':
        diagnostics.append(Diagnostic('E_SOURCE_EMPTY_FEED',
                                      'Пустой ответ не изменил коллекцию; показаны последние данные.', at))

    return RefreshPlan(
        source_id=state.source_id, collection_id=state.collection_id, mode=mode,
        outcome=result.outcome, reason_code=reason, at=at,
        applied=bool(added or removed or new_active != previous),
        applied_removals=bool(removed), promote_last_good=promote,
        serve_from_last_good=not promote and bool(state.last_good),
        added=added, removed=removed, kept=kept, retained_shared=retained_shared,
        expires_at=expires_at, next_attempt_at=None, diagnostics=tuple(diagnostics),
        next_state=replace(state, active=new_active, last_good=last_good, last_good_at=last_good_at,
                           last_attempt_at=at,
                           last_success_at=at if promote else state.last_success_at,
                           last_validated_at=at, expires_at=expires_at, next_attempt_at=None,
                           quarantine_until=None, retry_after=None, consecutive_failures=0,
                           etag=result.etag or state.etag,
                           last_modified=result.last_modified or state.last_modified,
                           body_sha256=result.body_sha256 or state.body_sha256,
                           last_outcome=result.outcome, last_error=result.error),
    )


def _code_for_outcome(outcome):
    return {
        'rate_limited': 'E_SOURCE_RETRY_AFTER',
        'quota_exhausted': 'E_SOURCE_QUOTA_EXHAUSTED',
        'token_expired': 'E_SOURCE_TOKEN_EXPIRED',
        'invalid': 'E_IMPORT_FORMAT',
        'too_large': 'E_LIMIT_BODY',
        'unavailable': 'E_SOURCE_UNAVAILABLE',
    }[outcome]


@dataclass(frozen=True)
class RotationPlan:
    """A credential change.  The new revision invalidates exactly the proofs
    that were made with the old one (CONTRACTS 1.2(1), F04)."""

    source_id: str
    collection_id: str
    access_id: str
    previous_revision: int
    revision: int
    invalidates: tuple
    at: float
    reason: str = 'credentials_changed'

    def summary(self):
        return {
            'source_id': self.source_id,
            'collection_id': self.collection_id,
            'access_id': self.access_id,
            'previous_revision': self.previous_revision,
            'revision': self.revision,
            'invalidates': [list(item) for item in self.invalidates],
            'reason': self.reason,
        }


def plan_rotation(state, *, access_id, new_access_ref, now=None, reason='credentials_changed'):
    """Bump ``access_revision`` and name the admissions that stop being valid."""
    if not isinstance(state, FeedState):
        raise SourceDeskError('E_VALIDATION_FIELD', 'Ожидался FeedState.')
    if not isinstance(access_id, str) or not access_id:
        raise SourceDeskError('E_VALIDATION_FIELD', 'Нужен access_id.')
    if not is_ref(new_access_ref) or not new_access_ref.startswith('auth-'):
        raise SourceDeskError('E_VALIDATION_FIELD', 'Ожидалась ссылка вида auth-<16 hex> на учётные данные.')
    at = float(now if now is not None else time.time())
    if state.access_id is not None and state.access_id != access_id:
        raise SourceDeskError('E_CONFLICT_REVISION',
                              'access_id источника не совпадает с сохранённым; нужен явный rebind.')
    revision = state.access_revision + 1
    invalidates = ()
    if state.access_id is not None:
        # Every observation taken with the previous revision loses its proof.
        invalidates = ((state.access_id, state.access_revision),)
    diagnostics = (Diagnostic('E_SOURCE_ACCESS_REVISION_CHANGED',
                              f'Учётные данные изменены: ревизия {state.access_revision} → {revision}.', at),)
    return RotationPlan(source_id=state.source_id, collection_id=state.collection_id,
                        access_id=access_id, previous_revision=state.access_revision,
                        revision=revision, invalidates=invalidates, at=at, reason=reason), \
        replace(state, access_id=access_id, access_revision=revision), diagnostics


# --------------------------------------------------------------------------
# Quota and token evidence
# --------------------------------------------------------------------------

RETRY_AFTER_RE = re.compile(r"\A\s*(\d+)\s*\Z")
QUOTA_HEADERS = {
    'x-ratelimit-limit': 'limit',
    'x-ratelimit-remaining': 'remaining',
    'x-ratelimit-reset': 'reset',
    'x-quota-limit': 'limit',
    'x-quota-remaining': 'remaining',
    'x-plan-limit': 'limit',
    'x-plan-remaining': 'remaining',
}
RESET_HEADERS = ('x-ratelimit-reset', 'x-quota-reset', 'ratelimit-reset', 'x-ratelimit-reset-after')


def _parse_retry_after(value, now):
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    match = RETRY_AFTER_RE.match(value)
    if match:
        return now + int(match.group(1))
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if moment is None:
        return None
    return moment.timestamp()


def quota_from_response(headers, *, now=0.0, token_expires_at=None):
    """Read quota and token evidence from response headers.

    A header that is absent yields ``None``, never a zero: an unknown quota is
    unknown, and a fake ``remaining=0`` would switch a working feed off.
    """
    if not isinstance(headers, Mapping):
        raise SourceDeskError('E_VALIDATION_FIELD', 'Ожидался словарь заголовков ответа.')
    found = {}
    reset_at = None
    token_expires = None
    for name, value in headers.items():
        key = str(name).lower()
        text = str(value).strip()
        if key in QUOTA_HEADERS:
            field = QUOTA_HEADERS[key]
            number = int(text) if text.lstrip('-').isdigit() else None
            if field in ('limit', 'remaining'):
                found[field] = number
            else:
                reset_at = _parse_retry_after(text, now)
        if key in RESET_HEADERS:
            reset_at = _parse_retry_after(text, now) or reset_at
        if key in ('x-token-expires-at', 'x-access-token-expires', 'token-expires-at'):
            seconds = _parse_retry_after(text, now)
            if seconds is not None:
                token_expires = seconds
    if token_expires_at is not None:
        token_expires = float(token_expires_at)
    if not found and reset_at is None and token_expires is None:
        return None
    return QuotaInfo(limit=found.get('limit'), remaining=found.get('remaining'),
                     reset_at=reset_at, token_expires_at=token_expires)


# --------------------------------------------------------------------------
# Storage (DML only; the schema belongs to db.migrate())
# --------------------------------------------------------------------------

#: Migration 15 for db.py.  Declared here as a constant so the migrator and the
#: tests cannot drift apart; this module never executes it.
REQUESTED_DDL = (
    """CREATE TABLE source_feed(
        source_id TEXT NOT NULL,
        collection_id TEXT NOT NULL,
        mode TEXT NOT NULL,
        url_ref TEXT NOT NULL,
        public_url TEXT NOT NULL,
        source_format TEXT NOT NULL,
        adapter_kind TEXT,
        adapter_profile TEXT,
        header_refs_json TEXT NOT NULL,
        access_ref TEXT,
        access_id TEXT,
        access_revision INTEGER NOT NULL DEFAULT 1,
        etag TEXT,
        last_modified TEXT,
        body_sha256 TEXT,
        active_json TEXT NOT NULL,
        last_good_json TEXT NOT NULL,
        last_attempt_at REAL,
        last_success_at REAL,
        last_good_at REAL,
        last_validated_at REAL,
        expires_at REAL,
        next_attempt_at REAL,
        retry_after REAL,
        quarantine_until REAL,
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        last_outcome TEXT,
        last_error TEXT,
        PRIMARY KEY(source_id, collection_id))""",
    """CREATE TABLE membership_source(
        collection_id TEXT NOT NULL,
        endpoint_id TEXT NOT NULL,
        source_id TEXT NOT NULL,
        origin TEXT NOT NULL,
        added_at REAL NOT NULL,
        last_seen_at REAL,
        PRIMARY KEY(collection_id, endpoint_id, source_id)) WITHOUT ROWID""",
    "CREATE INDEX membership_source_by_source ON membership_source(collection_id, source_id)",
    "CREATE INDEX source_feed_by_collection ON source_feed(collection_id)",
)

FEED_COLUMNS = (
    'source_id', 'collection_id', 'mode', 'url_ref', 'public_url', 'source_format',
    'adapter_kind', 'adapter_profile', 'access_ref', 'access_id', 'access_revision',
    'etag', 'last_modified', 'body_sha256', 'last_attempt_at', 'last_success_at',
    'last_good_at', 'last_validated_at', 'expires_at', 'next_attempt_at', 'retry_after',
    'quarantine_until', 'consecutive_failures', 'last_outcome', 'last_error',
)
#: The columns :class:`FeedState` owns.  The rest of FEED_COLUMNS describes the
#: binding, and the two JSON columns carry the endpoint sets.
STATE_COLUMNS = tuple(name for name in FEED_COLUMNS if name not in
                      ('url_ref', 'public_url', 'source_format', 'adapter_kind', 'adapter_profile')) + (
    'active_json', 'last_good_json')

ORIGIN_SUBSCRIPTION = 'source_subscription'


def _loads(value, name):
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        raise SourceDeskError('E_DATA_MIGRATION_FAILED', f'Повреждённое поле {name} в source_feed.') from None
    if not isinstance(parsed, list) or any(not isinstance(item, str) for item in parsed):
        raise SourceDeskError('E_DATA_MIGRATION_FAILED', f'Поле {name} должно быть списком строк.')
    return tuple(parsed)


class SourceDesk:
    """Persistence for feed bindings and source-scoped collection membership.

    The connection is the caller's; the tables are the ones ``REQUESTED_DDL``
    describes.  Only the rows this source owns are ever written or removed, so
    an endpoint shared with another source keeps its membership.
    """

    def __init__(self, conn, *, origin=ORIGIN_SUBSCRIPTION):
        if not isinstance(conn, sqlite3.Connection):
            raise SourceDeskError('E_VALIDATION_FIELD', 'SourceDesk требует sqlite3.Connection.')
        if not isinstance(origin, str) or not origin:
            raise SourceDeskError('E_VALIDATION_FIELD', 'Нужно происхождение membership (origin).')
        self._conn = conn
        self.origin = origin

    # -- bindings ---------------------------------------------------------

    def bind(self, source, *, collection_id, mode='merge', adapter_kind=None, adapter_profile=None,
             state=None):
        """Persist a :class:`UserSource` as a feed bound to a collection."""
        if not isinstance(source, UserSource):
            raise SourceDeskError('E_VALIDATION_FIELD', 'Ожидался UserSource.')
        if not isinstance(collection_id, str) or not collection_id:
            raise SourceDeskError('E_VALIDATION_FIELD', 'Нужен collection_id.')
        if mode not in MERGE_MODES:
            raise SourceDeskError('E_VALIDATION_FIELD', f'Неизвестный режим слияния: {mode!r}.')
        current = self.feed_row(source.id, collection_id)
        if current is not None and current['url_ref'] != source.url_ref:
            # Editing a URL is a new source, not a re-pointed old one: the
            # previous feed keeps its id, its generation and its last-good.
            raise SourceDeskError('E_CONFLICT_REVISION',
                                  'Источник уже привязан к другому URL; создайте новый источник.')
        base = state or FeedState(source.id, collection_id, mode=mode)
        values = {
            'source_id': source.id, 'collection_id': collection_id, 'mode': mode,
            'url_ref': source.url_ref, 'public_url': source.public_url, 'source_format': source.format,
            'adapter_kind': adapter_kind, 'adapter_profile': adapter_profile,
            'header_refs_json': json.dumps(dict(source.header_refs), sort_keys=True),
            'access_ref': source.access_ref, 'access_id': source.access_id,
            'access_revision': source.access_revision,
            'active_json': json.dumps(list(base.active)), 'last_good_json': json.dumps(list(base.last_good)),
            'last_attempt_at': None, 'last_success_at': None, 'last_good_at': None,
            'last_validated_at': None, 'expires_at': None, 'next_attempt_at': None,
            'retry_after': None, 'quarantine_until': None, 'consecutive_failures': 0,
            'last_outcome': None, 'last_error': None,
        }
        columns = ', '.join(values)
        updates = ', '.join(f'{name}=excluded.{name}' for name in
                            ('mode', 'public_url', 'source_format', 'adapter_kind', 'adapter_profile',
                             'header_refs_json', 'access_ref', 'access_id', 'access_revision'))
        with self._conn:
            self._conn.execute(
                f'INSERT INTO source_feed ({columns}) VALUES ({", ".join("?" * len(values))}) '
                f'ON CONFLICT(source_id, collection_id) DO UPDATE SET {updates}', tuple(values.values()))
        return self.get(source.id, collection_id)

    def get(self, source_id, collection_id):
        row = self._conn.execute(
            f'SELECT {", ".join(STATE_COLUMNS)} FROM source_feed WHERE source_id=? AND collection_id=?',
            (source_id, collection_id)).fetchone()
        return None if row is None else self._state(dict(zip(STATE_COLUMNS, row)))

    def _state(self, record):
        record = dict(record)
        active = _loads(record.pop('active_json'), 'active_json')
        last_good = _loads(record.pop('last_good_json'), 'last_good_json')
        return FeedState(active=active, last_good=last_good, **record)

    def feed_row(self, source_id, collection_id):
        """The persisted binding including its secret references."""
        row = self._conn.execute(
            'SELECT source_id, collection_id, mode, url_ref, public_url, source_format, adapter_kind, '
            'adapter_profile, header_refs_json, access_ref, access_id, access_revision '
            'FROM source_feed WHERE source_id=? AND collection_id=?', (source_id, collection_id)).fetchone()
        if row is None:
            return None
        keys = ('source_id', 'collection_id', 'mode', 'url_ref', 'public_url', 'source_format',
                'adapter_kind', 'adapter_profile', 'header_refs', 'access_ref', 'access_id', 'access_revision')
        record = dict(zip(keys, row))
        try:
            refs = json.loads(record['header_refs'] or '{}')
        except ValueError:
            raise SourceDeskError('E_DATA_MIGRATION_FAILED', 'Повреждённое поле header_refs_json.') from None
        record['header_refs'] = refs if isinstance(refs, dict) else {}
        return record

    def list_feeds(self, *, collection_id=None):
        if collection_id is None:
            rows = self._conn.execute(
                f'SELECT {", ".join(STATE_COLUMNS)} FROM source_feed ORDER BY collection_id, source_id').fetchall()
        else:
            rows = self._conn.execute(
                f'SELECT {", ".join(STATE_COLUMNS)} FROM source_feed WHERE collection_id=? '
                'ORDER BY source_id', (collection_id,)).fetchall()
        return [self._state(dict(zip(STATE_COLUMNS, row))) for row in rows]

    # -- membership -------------------------------------------------------

    def membership(self, source_id, collection_id):
        rows = self._conn.execute(
            'SELECT endpoint_id FROM membership_source WHERE collection_id=? AND source_id=? '
            'ORDER BY endpoint_id', (collection_id, source_id)).fetchall()
        return tuple(row[0] for row in rows)

    def foreign_membership(self, source_id, collection_id, endpoints=()):
        """Endpoints of this collection contributed by at least one other source."""
        if not endpoints:
            return frozenset()
        placeholders = ', '.join('?' * len(endpoints))
        rows = self._conn.execute(
            f'SELECT DISTINCT endpoint_id FROM membership_source '
            f'WHERE collection_id=? AND source_id<>? AND endpoint_id IN ({placeholders})',
            (collection_id, source_id, *endpoints)).fetchall()
        return frozenset(row[0] for row in rows)

    def contributors(self, endpoint_id, collection_id):
        rows = self._conn.execute(
            'SELECT source_id FROM membership_source WHERE collection_id=? AND endpoint_id=? '
            'ORDER BY source_id', (collection_id, endpoint_id)).fetchall()
        return tuple(row[0] for row in rows)

    def add_membership(self, collection_id, source_id, endpoints, *, now, origin=None):
        """Record this source's contribution; another source's rows are left alone."""
        rows = [(collection_id, endpoint_id, source_id, origin or self.origin, now, now)
                for endpoint_id in dict.fromkeys(endpoints)]
        with self._conn:
            self._conn.executemany(
                'INSERT INTO membership_source (collection_id, endpoint_id, source_id, origin, added_at, '
                'last_seen_at) VALUES (?,?,?,?,?,?) ON CONFLICT(collection_id, endpoint_id, source_id) '
                'DO UPDATE SET last_seen_at=excluded.last_seen_at, origin=excluded.origin', rows)
        return len(rows)

    # -- applying a plan --------------------------------------------------

    def apply_plan(self, plan, *, now=None):
        """Apply a :class:`RefreshPlan` in one transaction.

        Only rows of this source are deleted, so a membership another source
        owns survives a replace refresh.  The returned state is the one that
        was actually persisted, which is what a caller should compare against
        ``plan.next_state`` instead of trusting the prediction.
        """
        if not isinstance(plan, RefreshPlan):
            raise SourceDeskError('E_VALIDATION_FIELD', 'Ожидался RefreshPlan.')
        at = float(now if now is not None else plan.at)
        try:
            with self._conn:
                for endpoint_id in plan.removed:
                    self._conn.execute(
                        'DELETE FROM membership_source WHERE collection_id=? AND source_id=? AND endpoint_id=?',
                        (plan.collection_id, plan.source_id, endpoint_id))
                if plan.added:
                    self.add_membership(plan.collection_id, plan.source_id, plan.added, now=at)
                if plan.kept:
                    self._conn.execute(
                        'UPDATE membership_source SET last_seen_at=? WHERE collection_id=? AND source_id=? '
                        'AND endpoint_id IN ({})'.format(', '.join('?' * len(plan.kept))),
                        (at, plan.collection_id, plan.source_id, *plan.kept))
                membership = self.membership(plan.source_id, plan.collection_id)
                state = replace(plan.next_state, active=membership)
                self._write_state(state)
        except sqlite3.Error as exc:
            raise SourceDeskError('E_DATA_MIGRATION_FAILED', f'Не удалось применить план источника: {exc}') from None
        return state

    def _write_state(self, state):
        values = {'mode': state.mode, 'etag': state.etag, 'last_modified': state.last_modified,
                  'body_sha256': state.body_sha256, 'last_attempt_at': state.last_attempt_at,
                  'last_success_at': state.last_success_at, 'last_good_at': state.last_good_at,
                  'last_validated_at': state.last_validated_at, 'expires_at': state.expires_at,
                  'next_attempt_at': state.next_attempt_at, 'retry_after': state.retry_after,
                  'quarantine_until': state.quarantine_until,
                  'consecutive_failures': state.consecutive_failures,
                  'last_outcome': state.last_outcome, 'last_error': state.last_error,
                  'active_json': json.dumps(list(state.active)),
                  'last_good_json': json.dumps(list(state.last_good))}
        assignments = ', '.join(f'{name}=?' for name in values)
        self._conn.execute(
            f'UPDATE source_feed SET {assignments} WHERE source_id=? AND collection_id=?',
            (*values.values(), state.source_id, state.collection_id))

    def apply_rotation(self, plan, state, *, now=None):
        """Persist a bumped access revision and return the access to revoke."""
        if not isinstance(plan, RotationPlan):
            raise SourceDeskError('E_VALIDATION_FIELD', 'Ожидался RotationPlan.')
        at = float(now if now is not None else plan.at)
        with self._conn:
            self._conn.execute(
                'UPDATE source_feed SET access_id=?, access_revision=? WHERE source_id=? AND collection_id=?',
                (plan.access_id, plan.revision, plan.source_id, plan.collection_id))
        return plan.invalidates

    def apply_import(self, result, *, source_id, collection_id, mode='merge', now=0.0,
                     source_format='text', base_state=None):
        """Turn an :class:`ImportResult` into a refresh and apply it.

        This is the subscription path: a document becomes entries, an entry set
        becomes a plan, and a plan touches only this source's membership.
        """
        if not isinstance(result, ImportResult):
            raise SourceDeskError('E_VALIDATION_FIELD', 'Ожидался ImportResult.')
        if result.outcome in ('invalid', 'unsupported', 'limit_exceeded'):
            outcome = 'invalid' if result.outcome != 'limit_exceeded' else 'too_large'
            feed = FeedResult(outcome=outcome, fetched_at=now, error=result.outcome)
        elif result.outcome == 'empty':
            feed = FeedResult(outcome='empty', fetched_at=now)
        elif result.outcome == 'partial':
            feed = FeedResult(outcome='partial', fetched_at=now, entries=result.endpoints)
        else:
            feed = FeedResult(outcome='ok', fetched_at=now, entries=result.endpoints)
        stored = base_state or self.get(source_id, collection_id)
        state = replace(stored or FeedState(source_id, collection_id, mode=mode), mode=mode,
                        active=self.membership(source_id, collection_id))
        membership = self.membership(source_id, collection_id)
        foreign = self.foreign_membership(source_id, collection_id, membership)
        plan = plan_refresh(state, feed, policy=FeedPolicy(mode=mode), now=now, foreign=foreign)
        self.apply_plan(plan, now=now)
        return plan


# --------------------------------------------------------------------------
# Comparison of sources and suppliers (F21)
# --------------------------------------------------------------------------
#
# Everything in this section is read back out of the rows this application
# itself wrote:
#
#   ``candidate_seen``        which catalog publisher offered an address;
#   ``membership_source``     which user feed (a supplier) offered it;
#   ``observations``          what was actually measured, when, for which
#                             profile revision, and what came back;
#   ``endpoints``             the geography, and -- importantly -- *whose*
#                             observation the country came from;
#   ``job``                   the conditions the run was under (filters,
#                             budgets, find-N), which decide what the numbers
#                             are allowed to mean.
#
# Three rules are load-bearing and are enforced here rather than documented:
#
# 1. *Unknown is unknown.*  A measurement that never concluded -- the target
#    could not answer, the run ran out of budget, the job was stopped, the
#    verdict is missing or unreadable -- is counted as ``unknown`` and stays
#    out of the denominator.  It is never folded into a zero.  An address with
#    no observation at all is ``not measured``, which is a third thing again.
# 2. *Overlap is not independence.*  Two publishers that hand out the same
#    addresses are one publisher counted twice.  :func:`compare_sources` groups
#    them into families and reports what each family adds on top of the others,
#    so a mirror can never look like a second independent contribution.
# 3. *Nothing is estimated.*  With no observations the report says "no data".
#    It does not fall back to the catalog's own claims, and it never invents a
#    price, a pass rate or a proxy-quality figure.

#: Bias labels a comparison may carry.  Every one of them is attached only when
#: the database actually shows the condition, never as boilerplate.
BIAS_FIND_N = 'find_n'                #: the run stopped at N, so the sample is a prefix
BIAS_FILTERS = 'filters'              #: the run applied filters; the sample is not the source's output
BIAS_GEOGRAPHY = 'geography'          #: the compared sources cover different countries
BIAS_ORDER = 'order'                  #: address attribution is order-dependent; overlap is the correction
BIAS_SAMPLE = 'sample_size'           #: the sample is too small to separate the sources
BIAS_PUBLISHER_GEO = 'publisher_geography'  #: the country came from the list, not from us
BIAS_SHARED_COST = 'shared_cost'      #: a measurement of a shared address is charged to every crediting source
BIAS_UNKNOWN = 'unknown_measurements'       #: some measurements did not conclude
BIAS_SCOPE = 'scope'                  #: the sources were not collected under the same conditions

BIAS_CODES = (BIAS_FIND_N, BIAS_FILTERS, BIAS_GEOGRAPHY, BIAS_ORDER, BIAS_SAMPLE,
              BIAS_PUBLISHER_GEO, BIAS_SHARED_COST, BIAS_UNKNOWN, BIAS_SCOPE)

#: Below this many *conclusive* measurements a rate is reported with a
#: deliberately wide interval and ``sample_sufficient=False``.  The number is
#: never withheld -- it is the interval and the flag that say "do not conclude
#: anything from this yet".
SAMPLE_FLOOR = 20

#: 1.0 groups only *exactly equal* address sets into one family, which is the
#: copy case the source research actually found.  Lower it to fold near
#: duplicates in as well; the value is reported so a reader knows which rule
#: produced the grouping.
FAMILY_JACCARD_DEFAULT = 1.0

#: Access kinds that describe a commercial arrangement rather than a public
#: list.  They stay visible in the catalog and are never collected, so they
#: appear in a comparison as ``not_collected`` -- never as a source with a zero
#: pass rate, and never with a made-up price.
INERT_ACCESS_KINDS = ('paid', 'temporary_trial', 'free_with_api_key')

#: A run that stopped for one of these reasons measured something, but not
#: necessarily the address: the verdict says nothing about the proxy.
UNKNOWN_CODES_NOT_CONCLUSIVE = frozenset({
    'TARGET_UNAVAILABLE', 'BUDGET_EXHAUSTED', 'INSUFFICIENT_SAMPLE',
    'E_LIMIT_BUDGET', 'DEADLINE_EXCEEDED', 'E_JOB_CANCELLED', 'E_JOB_PAUSED',
    'E_JOB_STOPPED', 'CANCELLED', 'SKIPPED', 'BLOCKED',
})
#: Short alias; the longer name is the one the reports use.
UNKNOWN_CODES = UNKNOWN_CODES_NOT_CONCLUSIVE

_NON_CONCLUSIVE = None


def _non_conclusive_codes():
    """Codes after which the run, not the address, decided the outcome.

    The project's own probe layer already states the rule -- "failures at the
    target stage are evidence about the target, not about the proxy" -- in
    ``probes.TARGET_FAILURE_CODES``.  It is unioned in lazily instead of being
    copied, so the two definitions cannot drift; the local set above covers the
    case where the probe layer is not importable.
    """
    global _NON_CONCLUSIVE
    if _NON_CONCLUSIVE is None:
        codes = set(UNKNOWN_CODES_NOT_CONCLUSIVE)
        try:
            from .probes import TARGET_FAILURE_CODES
        except Exception:  # noqa: BLE001 - the classification must not depend on the import
            pass
        else:
            codes.update(TARGET_FAILURE_CODES)
        _NON_CONCLUSIVE = frozenset(codes)
    return _NON_CONCLUSIVE

#: A country whose source is one of these was *declared by somebody else* -- the
#: list that published the address, or the provider it names.  It is a claim,
#: not our measurement, and the report keeps the two apart.
PUBLISHER_COUNTRY_SOURCES = frozenset({'source', 'provider'})

STATUS_MEASURED = 'measured'
STATUS_NO_DATA = 'no_data'
STATUS_NOT_COLLECTED = 'not_collected'

OUTCOME_PASS = 'pass'
OUTCOME_FAIL = 'fail'
OUTCOME_UNKNOWN = 'unknown'


def _number(value):
    """A finite float, or ``None``.  ``bool`` is not a number here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _positive_int(value):
    number = _number(value)
    return None if number is None or number < 0 else int(number)


def wilson_interval(successes, trials, *, z=1.959963984540054):
    """Wilson score interval for a binomial proportion.

    Returns ``(None, None)`` when there is no trial at all.  That is the whole
    point: a source nobody measured has no interval, and a caller that treats
    ``None`` as ``0.0`` is the bug this function exists to make impossible.
    """
    if not isinstance(trials, int) or trials <= 0:
        return None, None
    successes = max(0, min(int(successes), trials))
    proportion = successes / trials
    denominator = 1.0 + z * z / trials
    centre = proportion + z * z / (2.0 * trials)
    margin = z * math.sqrt(proportion * (1.0 - proportion) / trials + z * z / (4.0 * trials * trials))
    low = (centre - margin) / denominator
    high = (centre + margin) / denominator
    return max(0.0, low), min(1.0, high)


def _verdict_payload(raw):
    """The measurement row stored in ``observations.verdict``.

    A row that is not a JSON object is *unknown*, not a failure: the address was
    measured and the answer was lost, which is a different statement.
    """
    if isinstance(raw, Mapping):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def classify_measurement(payload, error_code=None):
    """Decide what one measurement says: ``pass``, ``fail`` or ``unknown``.

    The three-way split is the honesty requirement.  ``unknown`` covers every
    case where the run did not get far enough to learn anything about the
    address -- the target was down, the budget ran out, the job was stopped, the
    verdict never arrived or is not readable.  Those measurements leave the
    denominator; they are not zeros, and they are not passes either.
    """
    code = str(error_code or '').strip()
    if code in _non_conclusive_codes():
        return OUTCOME_UNKNOWN, 'run_did_not_conclude'
    if code:
        # A code that is not in the non-conclusive set is a real, attributable
        # failure of this address (UNREACHABLE, CONNECT_TIMEOUT, ...).  The code
        # is the evidence on its own, so a payload that did not survive the
        # round-trip does not turn a measured failure into an "unknown".
        return OUTCOME_FAIL, code
    # A caller may hand over the column as it is stored, so the payload is
    # decoded here rather than assumed to be a mapping already.
    payload = _verdict_payload(payload)
    if payload is None:
        return OUTCOME_UNKNOWN, 'no_verdict'
    reliability = _number(payload.get('min_target_reliability'))
    if reliability is None:
        reliability = _number(payload.get('reliability'))
    if reliability is None:
        return OUTCOME_UNKNOWN, 'no_reliability_measured'
    if reliability > 0:
        return OUTCOME_PASS, None
    return OUTCOME_FAIL, 'measured_zero'


def _measurement_cost(payload, started_at, finished_at):
    """Seconds, bytes and attempts one measurement really spent.

    Bytes and attempts come from the samples the probe recorded, so a refused
    connection is not charged with the body it never read.  All three are
    ``None`` when the measurement is unknown -- a run that concluded nothing
    also spent nothing we can attribute.
    """
    state, reason = classify_measurement(payload, None)
    if state == OUTCOME_UNKNOWN:
        return None
    seconds = None
    start = _number(started_at)
    finish = _number(finished_at)
    if start is not None and finish is not None and finish >= start:
        seconds = finish - start
    total_bytes = 0
    saw_bytes = False
    for sample in (payload.get('samples') or ()) if isinstance(payload, Mapping) else ():
        if isinstance(sample, Mapping) and _positive_int(sample.get('bytes')) is not None:
            total_bytes += _positive_int(sample.get('bytes'))
            saw_bytes = True
    speed = payload.get('speed') if isinstance(payload, Mapping) else None
    if isinstance(speed, Mapping) and _positive_int(speed.get('bytes')) is not None:
        total_bytes += _positive_int(speed.get('bytes'))
        saw_bytes = True
    attempts = 0
    samples = payload.get('samples') if isinstance(payload, Mapping) else None
    if isinstance(samples, (list, tuple)):
        attempts = len(samples)
    if not attempts:
        attempts = _positive_int(payload.get('requests')) or 0
    return seconds, (total_bytes if saw_bytes else 0), attempts, reason


@dataclass(frozen=True)
class Cohort:
    """One comparable unit of observation: a window under one profile revision.

    A comparison is only meaningful inside one cohort.  Two cohorts with a
    different ``profile_revision`` or a different window answer a different
    question, and :func:`compare_cohorts` says so instead of averaging them
    together.
    """

    start: float
    end: float
    profile_id: str = ''
    profile_revision: int = 1
    collection_id: str = ''
    min_success: float = 2 / 3
    access_ids: tuple = ()
    label: str = ''

    def __post_init__(self):
        start, end = _number(self.start), _number(self.end)
        if start is None or end is None:
            raise SourceDeskError('E_VALIDATION_FIELD', 'Окно когорты должно быть числами (unix seconds).')
        if end <= start:
            raise SourceDeskError('E_VALIDATION_FIELD', 'Конец окна когорты должен быть позже начала.')
        if not isinstance(self.profile_revision, int) or isinstance(self.profile_revision, bool) \
                or self.profile_revision < 1:
            raise SourceDeskError('E_VALIDATION_FIELD', 'profile_revision должен быть целым >= 1.')
        threshold = _number(self.min_success)
        if threshold is None or not 0 < threshold <= 1:
            raise SourceDeskError('E_VALIDATION_FIELD', 'min_success должен быть долей от 0 до 1.')
        if not isinstance(self.access_ids, tuple) or any(not isinstance(item, str) for item in self.access_ids):
            raise SourceDeskError('E_VALIDATION_FIELD', 'access_ids должен быть кортежем строк.')

    @property
    def key(self):
        return (self.profile_id, self.profile_revision, round(self.start, 3), round(self.end, 3))

    def contains(self, at):
        moment = _number(at)
        return moment is not None and self.start <= moment < self.end

    def as_dict(self):
        return {'start': self.start, 'end': self.end, 'profile_id': self.profile_id,
                'profile_revision': self.profile_revision, 'collection_id': self.collection_id,
                'min_success': self.min_success, 'access_ids': list(self.access_ids),
                'label': self.label}


@dataclass(frozen=True)
class FetchState:
    """What the last fetch of a user feed actually proved.

    ``delivered_nothing`` is its own state on purpose.  A URL that answers
    ``HTTP 200`` with a valid ``ETag`` and an empty body has delivered nothing;
    folding that into "ok" is exactly the quiet success the source research
    found, so it is reported apart from a fetch that returned addresses.
    """

    source_id: str
    known: bool = False
    last_outcome: str = ''
    delivered_nothing: bool = False
    etag: str | None = None
    body_sha256: str | None = None
    last_attempt_at: float | None = None
    last_success_at: float | None = None
    consecutive_failures: int = 0
    quarantined_until: float | None = None

    def as_dict(self):
        return {'source_id': self.source_id, 'known': self.known, 'last_outcome': self.last_outcome,
                'delivered_nothing': self.delivered_nothing, 'etag': self.etag,
                'body_sha256': self.body_sha256, 'last_attempt_at': self.last_attempt_at,
                'last_success_at': self.last_success_at, 'consecutive_failures': self.consecutive_failures,
                'quarantined_until': self.quarantined_until}


@dataclass(frozen=True)
class SourceStats:
    """What one source contributed inside one cohort.

    ``reliability`` is ``None`` -- and stays ``None`` -- when nothing conclusive
    was measured.  ``status`` separates the three honest answers:

    * ``not_collected`` -- the source has no addresses in this database at all;
    * ``no_data``       -- it has addresses, but none of them was measured here;
    * ``measured``      -- at least one conclusive measurement exists.
    """

    source_id: str
    status: str = STATUS_NO_DATA
    offered: int = 0
    measured: int = 0
    passed: int = 0
    failed: int = 0
    unknown: int = 0
    admitted: int = 0
    observations: int = 0
    attempts: int = 0
    seconds: float | None = 0.0
    bytes: int = 0
    unique_offered: int = 0
    unique_admitted: int = 0
    reliability: float | None = None
    reliability_low: float | None = None
    reliability_high: float | None = None
    sample_sufficient: bool = False
    family_id: str = ''
    countries: tuple = ()
    publisher_country_claims: int = 0
    measured_countries: int = 0
    fetch: FetchState | None = None
    notes: tuple = ()

    def as_dict(self):
        return {
            'source_id': self.source_id, 'status': self.status, 'offered': self.offered,
            'measured': self.measured, 'passed': self.passed, 'failed': self.failed,
            'unknown': self.unknown, 'admitted': self.admitted, 'observations': self.observations,
            'attempts': self.attempts, 'seconds': self.seconds, 'bytes': self.bytes,
            'unique_offered': self.unique_offered, 'unique_admitted': self.unique_admitted,
            'reliability': self.reliability, 'reliability_low': self.reliability_low,
            'reliability_high': self.reliability_high, 'sample_sufficient': self.sample_sufficient,
            'family_id': self.family_id,
            'countries': [dict(item) for item in self.countries],
            'publisher_country_claims': self.publisher_country_claims,
            'measured_countries': self.measured_countries,
            'fetch': self.fetch.as_dict() if self.fetch else None,
            'notes': list(self.notes),
        }


@dataclass(frozen=True)
class OverlapPair:
    """How much two publishers hand out in common.

    ``identical`` means the two sets are equal.  That is the copy case: the
    second publisher's addresses are not a second opinion, they are the first
    one again, and its unique contribution is zero by construction.
    """

    left: str
    right: str
    left_size: int
    right_size: int
    shared: int
    jaccard: float | None
    identical: bool
    left_only: int
    right_only: int

    def as_dict(self):
        return {'left': self.left, 'right': self.right, 'left_size': self.left_size,
                'right_size': self.right_size, 'shared': self.shared, 'jaccard': self.jaccard,
                'identical': self.identical, 'left_only': self.left_only, 'right_only': self.right_only}


@dataclass(frozen=True)
class Family:
    """Publishers whose address sets are the same, plus what the group adds.

    ``unique_endpoints`` is measured against *every other source in the
    comparison*, not only against the other members of the family.  A family
    that only re-publishes what a second family already offers has nothing to
    add, and the report must be able to say exactly that.
    """

    family_id: str
    members: tuple
    endpoints: int
    unique_endpoints: int
    unique_admitted: int
    identical_group: bool

    def as_dict(self):
        return {'family_id': self.family_id, 'members': list(self.members), 'endpoints': self.endpoints,
                'unique_endpoints': self.unique_endpoints, 'unique_admitted': self.unique_admitted,
                'identical_group': self.identical_group}


@dataclass(frozen=True)
class CostPerAdmitted:
    """What one admitted address cost, in time, bytes and attempts.

    Every field is ``None`` when nothing was admitted: "no admitted address"
    is not a price of zero, and dividing by it would invent an infinity.  The
    shared-cost note stays because a measurement of an address offered by three
    publishers is charged to all three, so a family's cost is the cost of the
    whole family, not of one member.
    """

    admitted: int = 0
    seconds: float | None = None
    bytes: int | None = None
    attempts: int | None = None
    basis: str = 'admitted'
    reason: str = ''

    def as_dict(self):
        return {'admitted': self.admitted, 'seconds': self.seconds, 'bytes': self.bytes,
                'attempts': self.attempts, 'basis': self.basis, 'reason': self.reason}


@dataclass(frozen=True)
class BiasNote:
    """A named reason the numbers above are not the whole truth."""

    code: str
    detail: str
    sources: tuple = ()

    def as_dict(self):
        return {'code': self.code, 'detail': self.detail, 'sources': list(self.sources)}


@dataclass(frozen=True)
class SurvivalStep:
    """One step of the survival curve, with censoring kept visible."""

    index: int
    start: float
    end: float
    entered: int
    alive: int
    dead: int
    censored: int
    rate: float | None
    rate_of_entered: float | None

    def as_dict(self):
        return {'index': self.index, 'start': self.start, 'end': self.end, 'entered': self.entered,
                'alive': self.alive, 'dead': self.dead, 'censored': self.censored,
                'rate': self.rate, 'rate_of_entered': self.rate_of_entered}


@dataclass(frozen=True)
class SourceComparison:
    """The whole answer for one cohort: rows, overlap, families, cost, bias.

    ``warnings`` is where "these cohorts are not comparable" lives.  It is never
    empty to hide a problem: a comparison across different profile revisions or
    different windows is legitimate to ask for and illegitimate to read as one
    number, so it is answered with a refusal plus the per-cohort breakdown.
    """

    cohort: Cohort
    rows: tuple
    overlaps: tuple
    families: tuple
    cost: tuple
    biases: tuple
    survival: tuple = ()
    warnings: tuple = ()
    family_jaccard: float = FAMILY_JACCARD_DEFAULT
    universe: str = 'compared'

    def row(self, source_id):
        for item in self.rows:
            if item.source_id == source_id:
                return item
        return None

    @property
    def has_data(self):
        return any(item.status == STATUS_MEASURED for item in self.rows)

    def as_dict(self):
        return {
            'cohort': self.cohort.as_dict(),
            'rows': [item.as_dict() for item in self.rows],
            'overlaps': [item.as_dict() for item in self.overlaps],
            'families': [item.as_dict() for item in self.families],
            'cost': [item.as_dict() for item in self.cost],
            'biases': [item.as_dict() for item in self.biases],
            'survival': [item.as_dict() for item in self.survival],
            'warnings': list(self.warnings),
            'family_jaccard': self.family_jaccard,
            'universe': self.universe,
            'has_data': self.has_data,
        }


@dataclass(frozen=True)
class SupplierComparison:
    """Two of the user's own suppliers, measured on identical terms.

    Both sides are read from the same cohort: the same window, the same profile
    revision, the same admission threshold.  ``equal_terms`` is not a promise,
    it is the result of checking, and when it is ``False`` the reason is in
    ``warnings`` -- the comparison is then a description of two different
    experiments, not a verdict on a supplier.
    """

    cohort: Cohort
    left: SourceStats
    right: SourceStats
    overlap: OverlapPair | None
    equal_terms: bool
    warnings: tuple = ()
    comparison: SourceComparison | None = None

    def as_dict(self):
        return {'cohort': self.cohort.as_dict(), 'left': self.left.as_dict(), 'right': self.right.as_dict(),
                'overlap': self.overlap.as_dict() if self.overlap else None,
                'equal_terms': self.equal_terms, 'warnings': list(self.warnings),
                'comparison': self.comparison.as_dict() if self.comparison else None}


@dataclass(frozen=True)
class ProviderNote:
    """A commercial or trial provider: visible, never collected, never priced.

    The comparison must be able to show that these exist without pretending to
    have measured them.  ``status`` is ``not_collected`` for all of them, which
    is the honest answer -- not a zero pass rate and not a hidden row.
    """

    source_id: str
    name: str
    access_kind: str
    collectable: bool
    terms_url: str = ''
    status: str = STATUS_NOT_COLLECTED
    note: str = ''

    def as_dict(self):
        return {'source_id': self.source_id, 'name': self.name, 'access_kind': self.access_kind,
                'collectable': self.collectable, 'terms_url': self.terms_url, 'status': self.status,
                'note': self.note}


def _table_exists(conn, name):
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return row is not None


def _attribution(conn, sources, collection_id=None):
    """``{source_id: frozenset(endpoint_id)}`` from the two provenance tables.

    ``candidate_seen`` is what the catalog collectors delivered and is not
    scoped to a collection; ``membership_source`` is what the user's own feeds
    delivered and is.  Both are many-to-many on purpose: an address offered by
    four lists belongs to all four, and pretending otherwise is the
    first-attribution bias that overlap is here to correct.
    """
    wanted = tuple(sources)
    if not wanted:
        return {}
    placeholders = ', '.join('?' * len(wanted))
    result = {}
    if _table_exists(conn, 'candidate_seen'):
        for source_id, endpoint_id in conn.execute(
                f'SELECT source, endpoint_id FROM candidate_seen WHERE source IN ({placeholders})',
                wanted):
            result.setdefault(source_id, set()).add(endpoint_id)
    if _table_exists(conn, 'membership_source'):
        if collection_id:
            for source_id, endpoint_id in conn.execute(
                    f'SELECT source_id, endpoint_id FROM membership_source '
                    f'WHERE source_id IN ({placeholders}) AND collection_id=?', (*wanted, collection_id)):
                result.setdefault(source_id, set()).add(endpoint_id)
        else:
            for source_id, endpoint_id in conn.execute(
                    f'SELECT source_id, endpoint_id FROM membership_source WHERE source_id IN ({placeholders})',
                    wanted):
                result.setdefault(source_id, set()).add(endpoint_id)
    return {source_id: frozenset(items) for source_id, items in result.items()}


def _country_facts(conn, endpoint_ids):
    """``{endpoint_id: (country, country_source)}`` for the addresses we hold."""
    ids = tuple(endpoint_ids)
    facts = {}
    if not ids or not _table_exists(conn, 'endpoints'):
        return facts
    for start in range(0, len(ids), _SQL_BATCH):
        chunk = ids[start:start + _SQL_BATCH]
        placeholders = ', '.join('?' * len(chunk))
        for endpoint_id, country, country_source in conn.execute(
                f'SELECT id, country, country_source FROM endpoints WHERE id IN ({placeholders})', chunk):
            facts[endpoint_id] = (country, country_source)
    return facts


def _job_conditions(conn, cohort, job_ids):
    """What the runs that produced this cohort actually did.

    Find-N, filters and the country policy are not decorations on a number --
    they decide which addresses were ever measured, and therefore which claims
    the number may support.  They are read from the ``job`` rows the
    observations name, not from settings that may have changed since.  An
    absent row is "no evidence", which is not the same as "the run had no
    filters": only a filter that is actually recorded raises a note.
    """
    conditions = {'filters': {}, 'states': set(), 'jobs': set()}
    if not _table_exists(conn, 'job'):
        return conditions
    ids = tuple(sorted(job_ids))
    if not ids:
        return conditions
    for start in range(0, len(ids), _SQL_BATCH):
        chunk = ids[start:start + _SQL_BATCH]
        placeholders = ', '.join('?' * len(chunk))
        for job_id, scope_json, state in conn.execute(
                f'SELECT id, scope_json, state FROM job WHERE id IN ({placeholders})', chunk):
            conditions['jobs'].add(job_id)
            if state:
                conditions['states'].add(state)
            scope = {}
            if isinstance(scope_json, str) and scope_json.strip():
                try:
                    parsed = json.loads(scope_json)
                except ValueError:
                    parsed = None
                if isinstance(parsed, dict):
                    scope = parsed
            filters = scope.get('filters')
            if isinstance(filters, Mapping):
                merged = dict(conditions['filters'])
                merged.update(filters)
                conditions['filters'] = merged
    return conditions


def _find_n_requested(filters):
    for name in ('want', 'find_n', 'count_what'):
        value = filters.get(name)
        number = _number(value)
        if number is not None and number > 0:
            return True
    return False


def _fetch_states(conn, sources):
    """Feed health for the user's own sources, when that table exists."""
    wanted = tuple(sources)
    if not wanted or not _table_exists(conn, 'source_feed'):
        return {}
    placeholders = ', '.join('?' * len(wanted))
    states = {}
    for row in conn.execute(
            f'SELECT source_id, last_outcome, etag, body_sha256, last_attempt_at, last_success_at, '
            f'consecutive_failures, quarantine_until FROM source_feed WHERE source_id IN ({placeholders})',
            wanted):
        outcome = str(row[1] or '')
        states[row[0]] = FetchState(
            source_id=row[0], known=True, last_outcome=outcome,
            delivered_nothing=outcome == 'empty', etag=row[2], body_sha256=row[3],
            last_attempt_at=row[4], last_success_at=row[5],
            consecutive_failures=int(row[6] or 0), quarantined_until=row[7])
    return states


def _measure_window(conn, cohort, endpoints):
    """Fold the observations of one cohort into one record per address.

    The unit is the address, not the run: an address checked three times in a
    window is one address, and its state is decided by its most recent
    conclusive measurement.  Every run is still counted, because the cost of
    measuring it is real whether or not it changed the answer.
    """
    records = {}
    if not endpoints or not _table_exists(conn, 'observations'):
        return records
    clause = 'profile_revision = ?'
    params = [cohort.profile_revision]
    if cohort.profile_id:
        clause += ' AND profile_id = ?'
        params.append(cohort.profile_id)
    if cohort.access_ids:
        clause += ' AND access_id IN ({})'.format(', '.join('?' * len(cohort.access_ids)))
        params.extend(cohort.access_ids)
    ids = tuple(endpoints)
    job_ids = set()
    # The window is applied in Python, not in SQL: an observation belongs to the
    # window that holds its start *or* its finish, so a run that straddles a
    # window edge is still counted exactly once instead of falling between two
    # predicates and disappearing.
    for start in range(0, len(ids), _SQL_BATCH):
        chunk = ids[start:start + _SQL_BATCH]
        placeholders = ', '.join('?' * len(chunk))
        for endpoint_id, started_at, finished_at, verdict, error_code, job_id in conn.execute(
                f'SELECT endpoint_id, started_at, finished_at, verdict, error_code, job_id '
                f'FROM observations WHERE {clause} AND endpoint_id IN ({placeholders})',
                (*params, *chunk)):
            if not cohort.contains(started_at) and not cohort.contains(finished_at):
                continue
            payload = _verdict_payload(verdict)
            state, reason = classify_measurement(payload, error_code)
            cost = _measurement_cost(payload, started_at, finished_at)
            if job_id:
                job_ids.add(job_id)
            reliability = None
            if payload is not None:
                reliability = _number(payload.get('min_target_reliability'))
                if reliability is None:
                    reliability = _number(payload.get('reliability'))
            record = records.get(endpoint_id)
            if record is None or (started_at or 0) >= (record['at'] or 0):
                records[endpoint_id] = {'state': state, 'reason': reason, 'at': started_at,
                                        'reliability': reliability}
                record = records[endpoint_id]
            record['observations'] = record.get('observations', 0) + 1
            if cost is not None:
                seconds, byte_count, attempts, _ = cost
                record['seconds'] = record.get('seconds', 0.0) + (seconds or 0.0)
                record['bytes'] = record.get('bytes', 0) + byte_count
                record['attempts'] = record.get('attempts', 0) + attempts
    records.setdefault('__jobs__', job_ids)
    return records


def _families(attribution, sources, threshold):
    """Group publishers whose address sets overlap at or above ``threshold``.

    The grouping is a union-find over pairs, so a chain of near-duplicates
    collapses into one group -- a mirror of a mirror is still a mirror.  The
    result is a pure function of the sets: the order the sources arrived in
    cannot change a single membership.
    """
    parent = {source_id: source_id for source_id in sources}

    def find(item):
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(left, right):
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            # Sorted so the representative does not depend on arrival order.
            low, high = sorted((left_root, right_root))
            parent[high] = low

    for index, left in enumerate(sources):
        left_set = attribution.get(left) or frozenset()
        if not left_set:
            continue
        for right in sources[index + 1:]:
            right_set = attribution.get(right) or frozenset()
            if not right_set:
                continue
            shared = len(left_set & right_set)
            union_size = len(left_set | right_set)
            if shared and (shared / union_size if union_size else 0.0) >= threshold:
                union(left, right)

    groups = {}
    for source_id in sources:
        groups.setdefault(find(source_id), []).append(source_id)
    return {root: tuple(sorted(members)) for root, members in groups.items()}


def _overlaps(attribution, sources):
    """Every pair that shares addresses, strongest first, order-independent."""
    pairs = []
    for index, left in enumerate(sources):
        left_set = attribution.get(left) or frozenset()
        for right in sources[index + 1:]:
            right_set = attribution.get(right) or frozenset()
            shared_set = left_set & right_set
            if not shared_set and not (left_set and right_set):
                continue
            union_size = len(left_set | right_set)
            jaccard = (len(shared_set) / union_size) if union_size else None
            pairs.append(OverlapPair(
                left=left, right=right, left_size=len(left_set), right_size=len(right_set),
                shared=len(shared_set), jaccard=jaccard,
                identical=bool(left_set) and left_set == right_set,
                left_only=len(left_set - right_set), right_only=len(right_set - left_set)))
    pairs.sort(key=lambda pair: (-pair.shared, pair.left, pair.right))
    return tuple(pairs)


def compare_sources(conn, *, sources, cohort, family_jaccard=FAMILY_JACCARD_DEFAULT,
                    sample_floor=SAMPLE_FLOOR, with_survival=False):
    """Compare publishers on observed measurements, inside one cohort.

    ``sources`` are the ids as they appear in ``candidate_seen.source`` or
    ``membership_source.source_id`` -- whatever the caller collected under.  Two
    of them is the supplier comparison and works exactly like a larger one: the
    cohort is shared, so the terms are shared by construction rather than by
    promise.

    The result is a pure function of the rows.  Permuting ``sources`` cannot
    change a count, a family, a cost or a bias note: every set operation runs on
    endpoint ids, and every output list is sorted.
    """
    if not isinstance(conn, sqlite3.Connection):
        raise SourceDeskError('E_VALIDATION_FIELD', 'Сравнение источников требует sqlite3.Connection.')
    if not isinstance(cohort, Cohort):
        raise SourceDeskError('E_VALIDATION_FIELD', 'Ожидался Cohort.')
    wanted = tuple(dict.fromkeys(str(item) for item in (sources or ()) if str(item)))
    if not wanted:
        raise SourceDeskError('E_VALIDATION_FIELD', 'Нужен хотя бы один источник для сравнения.')
    threshold = _number(family_jaccard)
    if threshold is None or not 0 < threshold <= 1:
        raise SourceDeskError('E_VALIDATION_FIELD', 'family_jaccard должен быть долей от 0 до 1.')
    floor = _positive_int(sample_floor)
    if floor is None:
        raise SourceDeskError('E_VALIDATION_FIELD', 'sample_floor должен быть неотрицательным целым.')

    ordered = tuple(sorted(wanted))
    attribution = _attribution(conn, ordered, cohort.collection_id or None)
    for source_id in ordered:
        attribution.setdefault(source_id, frozenset())
    universe = set()
    for items in attribution.values():
        universe |= items

    records = _measure_window(conn, cohort, universe) if universe else {}
    job_ids = records.pop('__jobs__', set()) if records else set()
    conditions = _job_conditions(conn, cohort, job_ids)

    # A family is a set of publishers; what it adds is measured against every
    # other source in the comparison, not only against its own members.
    grouped = _families(attribution, ordered, threshold)
    family_of = {source_id: root for root, members in grouped.items() for source_id in members}
    family_endpoints = {}
    for source_id, root in family_of.items():
        family_endpoints.setdefault(root, set()).update(attribution.get(source_id) or frozenset())

    countries = _country_facts(conn, universe) if universe else {}
    fetches = _fetch_states(conn, ordered)

    rows = []
    for source_id in ordered:
        own = attribution.get(source_id) or frozenset()
        if not own:
            rows.append(SourceStats(source_id=source_id, status=STATUS_NOT_COLLECTED, offered=0,
                                    family_id=family_of[source_id], fetch=fetches.get(source_id),
                                    notes=('источник не собирался: ни одного адреса в базе',)))
            continue
        other_sources = set(ordered) - {source_id}
        unique = set(own)
        for name in other_sources:
            unique -= attribution.get(name) or frozenset()
        measured = passed = failed = unknown = admitted = 0
        observations = attempts = 0
        total_seconds = 0.0
        total_bytes = 0
        seen_seconds = False
        histogram = {}
        publisher_claims = ours = 0
        for endpoint_id in own:
            if endpoint_id in countries:
                code, origin = countries[endpoint_id]
                if code:
                    histogram[code] = histogram.get(code, 0) + 1
                    if origin in PUBLISHER_COUNTRY_SOURCES:
                        publisher_claims += 1
                    else:
                        ours += 1
            record = records.get(endpoint_id)
            if record is None:
                continue
            measured += 1
            observations += record.get('observations', 0)
            attempts += record.get('attempts', 0)
            total_bytes += record.get('bytes', 0)
            if 'seconds' in record:
                total_seconds += record.get('seconds', 0.0)
                seen_seconds = True
            state = record['state']
            if state == OUTCOME_PASS:
                passed += 1
                if _admitted(record, cohort.min_success):
                    admitted += 1
            elif state == OUTCOME_FAIL:
                failed += 1
            else:
                unknown += 1
        trials = passed + failed
        low, high = wilson_interval(passed, trials)
        unique_admitted = 0
        for endpoint_id in unique:
            record = records.get(endpoint_id)
            if record and record['state'] == OUTCOME_PASS and _admitted(record, cohort.min_success):
                unique_admitted += 1
        notes = []
        if not measured:
            notes.append('адреса есть, измерений в этом окне нет: данных о качестве нет')
        elif unknown:
            notes.append(f'{unknown} измерений не дали вывода и не входят в знаменатель')
        rows.append(SourceStats(
            source_id=source_id,
            status=STATUS_MEASURED if measured else STATUS_NO_DATA,
            offered=len(own), measured=measured, passed=passed, failed=failed, unknown=unknown,
            admitted=admitted, observations=observations, attempts=attempts,
            seconds=total_seconds if seen_seconds else None, bytes=total_bytes,
            unique_offered=len(unique), unique_admitted=unique_admitted,
            reliability=(passed / trials) if trials else None,
            reliability_low=low, reliability_high=high,
            sample_sufficient=trials >= floor,
            family_id=family_of[source_id],
            countries=tuple({'country': code, 'count': count} for code, count in sorted(histogram.items())),
            publisher_country_claims=publisher_claims, measured_countries=ours,
            fetch=fetches.get(source_id), notes=tuple(notes)))

    families = []
    for root, members in sorted(grouped.items()):
        owned = family_endpoints.get(root) or set()
        outside = set()
        for name in ordered:
            if name in members:
                continue
            outside |= attribution.get(name) or frozenset()
        unique_endpoints = owned - outside
        unique_admitted = 0
        for endpoint_id in unique_endpoints:
            record = records.get(endpoint_id)
            if record and record['state'] == OUTCOME_PASS and _admitted(record, cohort.min_success):
                unique_admitted += 1
        member_sets = [attribution.get(name) or frozenset() for name in members]
        identical = (len(members) > 1 and bool(member_sets[0])
                     and all(item == member_sets[0] for item in member_sets))
        families.append(Family(family_id=root, members=members, endpoints=len(owned),
                               unique_endpoints=len(unique_endpoints), unique_admitted=unique_admitted,
                               identical_group=identical))

    overlaps = _overlaps(attribution, ordered)
    cost = tuple(_cost_for(row) for row in rows)
    biases = _biases(rows, families, overlaps, conditions, ordered, threshold, floor)
    warnings = _cohort_warnings(rows)
    survival = ()
    if with_survival:
        survival = survival_across_windows(conn, sources=ordered, profile_id=cohort.profile_id,
                                           profile_revision=cohort.profile_revision,
                                           collection_id=cohort.collection_id,
                                           min_success=cohort.min_success, start=cohort.start,
                                           end=cohort.end)
    return SourceComparison(cohort=cohort, rows=tuple(rows), overlaps=overlaps, families=tuple(families),
                            cost=cost, biases=biases, survival=survival, warnings=warnings,
                            family_jaccard=threshold, universe='compared')


def _admitted(record, min_success):
    """Whether the address cleared the cohort's admission threshold."""
    number = _number(record.get('reliability'))
    if number is None:
        return False
    return number > 0 and number + 1e-12 >= min_success


def _cost_for(row):
    """Cost per admitted address, or an honest ``None`` with a reason."""
    if not row.admitted:
        return CostPerAdmitted(admitted=0, seconds=None, bytes=None, attempts=None,
                               basis='admitted', reason='нет ни одного пригодного адреса в этом окне')
    return CostPerAdmitted(admitted=row.admitted, seconds=row.seconds, bytes=row.bytes,
                           attempts=row.attempts, basis='admitted', reason='')


def _cohort_warnings(rows):
    """What a reader must be told before reading a number off this report."""
    warnings = []
    if all(row.status == STATUS_NOT_COLLECTED for row in rows):
        warnings.append('ни один источник не собирался: наблюдений нет, сравнивать нечего')
    elif not any(row.measured for row in rows):
        warnings.append('наблюдений в этом окне нет: pass-rate неизвестен, а не нулевой')
    return tuple(warnings)


def _biases(rows, families, overlaps, conditions, sources, threshold, sample_floor):
    """Name every condition the reader must know before trusting a number."""
    notes = []
    measured = [row for row in rows if row.measured]

    if conditions.get('filters') and _find_n_requested(conditions['filters']):
        notes.append(BiasNote(
            BIAS_FIND_N,
            f'задание останавливалось на N={conditions["filters"].get("want")} '
            f'({conditions["filters"].get("count_what") or "endpoint"}): измерен только префикс списка',
            tuple(sorted(sources))))
    if conditions.get('filters'):
        others = {name: value for name, value in conditions['filters'].items()
                  if name not in ('want', 'count_what') and value not in (None, [], {}, '', False)}
        if others:
            notes.append(BiasNote(BIAS_FILTERS,
                                  f'измерения шли под фильтрами {sorted(others)}: доля прошедших '
                                  f'относится к отфильтрованной части, а не ко всему списку',
                                  tuple(sorted(sources))))
    for family in families:
        if len(family.members) > 1:
            notes.append(BiasNote(
                BIAS_ORDER,
                f'семейство {family.family_id}: {", ".join(family.members)} отдают пересекающиеся адреса; '
                f'заслуга за первый увиденный адрес зависит от порядка, уникальный вклад считается отдельно',
                family.members))
    for pair in overlaps:
        if pair.identical:
            notes.append(BiasNote(BIAS_SHARED_COST,
                                  f'{pair.left} и {pair.right} отдают одинаковый набор из {pair.shared} '
                                  f'адресов: это один издатель, посчитанный дважды, а не два независимых',
                                  (pair.left, pair.right)))
    if any(row.publisher_country_claims for row in rows):
        total = sum(row.publisher_country_claims for row in rows)
        notes.append(BiasNote(BIAS_PUBLISHER_GEO,
                              f'{total} стран пришли из чужих метаданных списка, а не из наших измерений',
                              tuple(sorted(row.source_id for row in rows if row.publisher_country_claims))))
    distributions = [row.countries for row in measured if row.countries]
    if len(distributions) > 1:
        tops = {row.source_id: (row.countries[0]['country'] if row.countries else None) for row in measured}
        if len({value for value in tops.values() if value}) > 1:
            notes.append(BiasNote(BIAS_GEOGRAPHY,
                                  f'источники покрывают разные страны ({tops}): сравнение процента прошедших '
                                  f'между ними не apples-to-apples', tuple(sorted(tops))))
    small = [row.source_id for row in measured if not row.sample_sufficient]
    if small:
        notes.append(BiasNote(BIAS_SAMPLE,
                              f'выборка меньше {sample_floor} conclusive измерений у {", ".join(small)}: '
                              f'интервал Уilson широк, выводы о разнице пока преждевременны', tuple(small)))
    unknown_total = sum(row.unknown for row in rows)
    if unknown_total:
        notes.append(BiasNote(BIAS_UNKNOWN,
                              f'{unknown_total} измерений не дали вывода (цель, бюджет, отмена): '
                              f'они не нули и не успехи, знаменатель их не считает', tuple(sorted(sources))))
    if threshold < 1.0:
        notes.append(BiasNote(BIAS_ORDER,
                              f'семейства объединены по порогу jaccard={threshold}, а не только по точному совпадению',
                              tuple(sorted(sources))))
    notes.sort(key=lambda note: (note.code, note.detail))
    return tuple(notes)


def survival_across_windows(conn, *, sources, profile_id='', profile_revision=1, collection_id='',
                            windows=None, min_success=2/3, count=3, start=None, end=None):
    """How many addresses were still working in the windows after the first.

    An address that was simply not re-checked in a later window is *censored*,
    not dead.  Counting it as dead would turn "we did not look" into "the proxy
    died", which is the same mistake as turning unknown into a zero.  The step
    therefore reports ``rate`` over the addresses that were actually judged and
    ``censored`` beside it, so the reader can see how much of the original
    cohort the curve really rests on.
    """
    if not isinstance(conn, sqlite3.Connection):
        raise SourceDeskError('E_VALIDATION_FIELD', 'Survival требует sqlite3.Connection.')
    ordered = tuple(sorted(dict.fromkeys(str(item) for item in (sources or ()) if str(item))))
    if not ordered:
        return ()
    if not isinstance(profile_revision, int) or isinstance(profile_revision, bool) or profile_revision < 1:
        raise SourceDeskError('E_VALIDATION_FIELD', 'profile_revision должен быть целым >= 1.')
    threshold = _number(min_success)
    if threshold is None or not 0 < threshold <= 1:
        raise SourceDeskError('E_VALIDATION_FIELD', 'min_success должен быть долей от 0 до 1.')
    spans = _window_spans(windows, start, end, count)
    if len(spans) < 2:
        raise SourceDeskError('E_VALIDATION_FIELD',
                              'Для survival нужно минимум два окна: переживание нескольких окон и есть смысл.')
    attribution = _attribution(conn, ordered, collection_id or None)
    tracked = set()
    for items in attribution.values():
        tracked |= items
    if not tracked:
        return ()

    alive = None
    steps = []
    for index, (from_at, to_at) in enumerate(spans):
        cohort = Cohort(from_at, to_at, profile_id=profile_id, profile_revision=profile_revision,
                        collection_id=collection_id, min_success=threshold,
                        label=f'window-{index + 1}')
        records = _measure_window(conn, cohort, tracked)
        records.pop('__jobs__', None)
        if alive is None:
            entered = {endpoint_id for endpoint_id, record in records.items()
                       if record['state'] == OUTCOME_PASS and _admitted(record, threshold)}
        else:
            entered = set(alive)
        if not entered:
            steps.append(SurvivalStep(index=index, start=from_at, end=to_at, entered=0, alive=0,
                                      dead=0, censored=0, rate=None, rate_of_entered=None))
            continue
        still = censored = dead = 0
        survivors = set()
        for endpoint_id in entered:
            record = records.get(endpoint_id)
            if record is None or record['state'] == OUTCOME_UNKNOWN:
                censored += 1
            elif record['state'] == OUTCOME_PASS and _admitted(record, threshold):
                still += 1
                survivors.add(endpoint_id)
            else:
                dead += 1
        judged = still + dead
        steps.append(SurvivalStep(index=index, start=from_at, end=to_at, entered=len(entered),
                                  alive=still, dead=dead, censored=censored,
                                  rate=(still / judged) if judged else None,
                                  rate_of_entered=still / len(entered)))
        alive = survivors
    return tuple(steps)


def _window_spans(windows, start, end, count):
    """Explicit windows, or a simple equal split of ``[start, end)``."""
    if windows is not None:
        spans = []
        for item in windows:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                raise SourceDeskError('E_VALIDATION_FIELD', 'Окно должно быть парой (start, end).')
            low, high = _number(item[0]), _number(item[1])
            if low is None or high is None or high <= low:
                raise SourceDeskError('E_VALIDATION_FIELD', 'Окно должно быть (start, end) с end > start.')
            spans.append((low, high))
        return spans
    low, high = _number(start), _number(end)
    if low is None or high is None or high <= low:
        raise SourceDeskError('E_VALIDATION_FIELD', 'Нужны start/end или явные windows для survival.')
    parts = max(2, int(_number(count) or 2))
    step = (high - low) / parts
    return [(low + index * step, low + (index + 1) * step) for index in range(parts)]


def compare_suppliers(conn, left, right, *, cohort, **kwargs):
    """Compare two of the user's own suppliers on identical terms.

    Both sides come from one cohort, so the window, the profile revision and the
    admission threshold are the same by construction.  What is checked rather
    than assumed is whether the two actually have comparable evidence: if one
    side was never measured, the answer says so instead of reading as a loss.
    """
    if not left or not right:
        raise SourceDeskError('E_VALIDATION_FIELD', 'Нужны два источника для сравнения поставщиков.')
    if str(left) == str(right):
        raise SourceDeskError('E_VALIDATION_FIELD', 'Поставщики должны быть разными.')
    comparison = compare_sources(conn, sources=(left, right), cohort=cohort, **kwargs)
    warnings = list(comparison.warnings)
    equal = True
    left_row, right_row = comparison.row(str(left)), comparison.row(str(right))
    if left_row is None or right_row is None:
        raise SourceDeskError('E_VALIDATION_FIELD', 'Один из поставщиков не попал в сравнение.')
    if left_row.status != right_row.status:
        equal = False
        warnings.append(f'статусы источников различаются ({left_row.status} против {right_row.status}): '
                        f'это описание двух разных экспериментов, а не сравнение поставщиков')
    elif left_row.status == STATUS_MEASURED and right_row.status == STATUS_MEASURED:
        if not (left_row.sample_sufficient and right_row.sample_sufficient):
            equal = False
            warnings.append('выборка одного из поставщиков меньше порога: разницу по ней утверждать нельзя')
    if left_row.status == STATUS_NOT_COLLECTED or right_row.status == STATUS_NOT_COLLECTED:
        equal = False
        warnings.append('один из поставщиков не собирался: у него нет ни одного наблюдения')
    overlap = next((pair for pair in comparison.overlaps
                    if {pair.left, pair.right} == {str(left), str(right)}), None)
    if overlap and overlap.identical:
        warnings.append('оба поставщика отдают одинаковый набор адресов: уникальный вклад второго равен нулю, '
                        'это один и тот же список, а не два независимых')
    return SupplierComparison(cohort=cohort, left=left_row, right=right_row, overlap=overlap,
                              equal_terms=equal, warnings=tuple(warnings), comparison=comparison)


def compare_cohorts(conn, *, sources, cohorts):
    """Per-cohort breakdowns plus an explicit warning when they are not comparable.

    Comparing two runs of different profile revisions, or of windows that do not
    overlap, is a legitimate question and an illegitimate average.  Both
    breakdowns are returned; the warning says why they must not be read as one
    number.
    """
    if not isinstance(conn, sqlite3.Connection):
        raise SourceDeskError('E_VALIDATION_FIELD', 'Сравнение когорт требует sqlite3.Connection.')
    items = tuple(cohorts or ())
    if len(items) < 2:
        raise SourceDeskError('E_VALIDATION_FIELD', 'Нужны минимум две когорты для сравнения.')
    for item in items:
        if not isinstance(item, Cohort):
            raise SourceDeskError('E_VALIDATION_FIELD', 'Ожидался Cohort.')
    warnings = []
    revisions = {item.profile_revision for item in items}
    profiles = {item.profile_id for item in items}
    if len(revisions) > 1:
        warnings.append(f'когорты с разной ревизией профиля ({sorted(revisions)}): правила приёмки разные, '
                        f'сравнивать их как одно измерение нельзя')
    if len(profiles) > 1:
        warnings.append(f'когорты разных профилей ({sorted(profiles)}): это разные эксперименты')
    thresholds = {item.min_success for item in items}
    if len(thresholds) > 1:
        warnings.append(f'разный порог приёмки ({sorted(thresholds)}): «пригодный» означает разное')
    windows = [(item.start, item.end) for item in items]
    if any(windows[index][1] <= windows[index + 1][0] for index in range(len(windows) - 1)):
        warnings.append('окна не пересекаются: измерения в них относятся к разным моментам времени')
    breakdowns = tuple(compare_sources(conn, sources=sources, cohort=item) for item in items)
    return tuple(breakdowns), tuple(warnings)


def provider_inventory(catalog, *, sources=None, comparison=None):
    """Commercial and trial providers: visible, never collected, never priced.

    These rows are the honest alternative to a made-up pass rate.  They are
    reported with their access kind, their terms link and ``not_collected`` --
    a state that says "we have no measurement", which is different from "we
    measured and it failed".  No price appears here: a cost per address for a
    plan this application never subscribed to would be an invention.
    """
    if not isinstance(catalog, Mapping):
        raise SourceDeskError('E_VALIDATION_FIELD', 'Ожидался каталог источников.')
    measured = {}
    if comparison is not None:
        measured = {row.source_id: row.status for row in comparison.rows}
    notes = []
    for record in (catalog.get('sources') or ()):
        if not isinstance(record, Mapping):
            continue
        access = record.get('access') if isinstance(record.get('access'), Mapping) else {}
        kind = str(access.get('kind') or 'unknown')
        source_id = str(record.get('id') or '')
        collectable = bool(record.get('collection_allowed', True))
        if not collectable or kind in INERT_ACCESS_KINDS:
            status = measured.get(source_id, STATUS_NOT_COLLECTED)
            notes.append(ProviderNote(
                source_id=source_id, name=str(record.get('name') or source_id), access_kind=kind,
                collectable=False, terms_url=str(record.get('terms_url') or ''),
                status=status if status == STATUS_MEASURED else STATUS_NOT_COLLECTED,
                note='коммерческое предложение: не собирается, локальных измерений нет, '
                     'цена не оценивалась'))
    notes.sort(key=lambda item: (item.access_kind, item.source_id))
    return tuple(notes)
