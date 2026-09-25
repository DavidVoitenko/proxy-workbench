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
"""
from __future__ import annotations

import hashlib
import json
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
]

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
