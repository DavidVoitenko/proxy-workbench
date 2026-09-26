"""One snapshot contract for every export artifact and every reader.

CONTRACTS.ru.md §4 and F28 ask for the same five things everywhere: ``rows``,
``status``, ``scope``, ``profile``, ``policy``, plus an immutable publication
behind an atomic pointer.  This module is the only place that writes them, so
API, gateway, GUI and CLI stop keeping their own copies of the rule.

Three decisions are the contract, not implementation detail:

* **Writing a generation and switching the active pointer are two acts.**  A
  selection, a top-N slice or a search result is written with
  :func:`write_snapshot` and never moves the active pool.  Only
  :func:`publish` moves it, only for ``kind='published'`` and only with an
  explicit confirmation (defect 7, R04).
* **A published generation is immutable.**  It is written once, checksummed
  into a manifest, and read through a pointer swap that either happens or does
  not.  A failure leaves the previous generation as the only published one.
* **A set has a lifetime of its own.**  ``expires_at`` is the newest admitted
  member's deadline, never ``min()`` over rows, so one expired member cannot
  hide the rest (defect 3).

The admission decision itself is **not** here: thresholds, denylist, anonymity
and the clock belong to :mod:`proxy_workbench.core`, and this module calls it
rather than keeping a second copy.  DDL belongs to :mod:`proxy_workbench.db`;
nothing here creates a table.
"""
from __future__ import annotations

import csv
import hashlib
import io
import ipaddress
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit

from . import core, formats
from .i18n import tr
from .proxytool import (PROTOCOL_ALIASES, PROTOCOL_EXPORTS, PROTOCOLS, PROXYCHAINS_TYPES,
                        proxy_protocol, reputation_status)

__all__ = [
    'ARTIFACT_KINDS', 'CLIENT_BINARY_ENV', 'CLIENT_SIDECAR_NAME', 'CLIENT_TARGET_ENV', 'DIAGNOSTIC_POINTER_NAME',
    'EXPORT_CODES', 'GENERATION_PREFIX', 'MERGE_MODES', 'POINTER_NAME', 'ROW_FIELDS',
    'SUPPORTED_SCHEMA_VERSIONS', 'SNAPSHOT_SCHEMA_VERSION', 'Artifact', 'CompatReport', 'ExportError',
    'ExportOptions', 'ExportScope', 'LoadedSnapshot', 'Pointer', 'ResolvedClientTarget', 'SecretGrant',
    'SingBoxTarget', 'SnapshotStatus', 'Unsupported', 'attach_admission', 'build_status', 'client_check',
    'compat_report', 'generation_name', 'load_snapshot', 'prune_generations', 'publish', 'read_client_sidecar',
    'read_pointer', 'record_artifact', 'redact_row', 'render_clash', 'render_csv', 'render_hostport',
    'render_json', 'render_pac', 'render_protocol_files', 'render_proxychains', 'render_singbox',
    'render_snapshot_txt', 'render_txt', 'resolve_client_target', 'singbox_target', 'status_from_dict',
    'write_snapshot',
]

# 1 is what ``proxytool.SNAPSHOT_SCHEMA_VERSION`` writes today; it stays
# readable so an export published by the old engine is never a hard error.
# 2 is this contract: scope/profile/policy digests, kind, compat report,
# credentials policy, merge mode and a set lifetime separate from row TTLs.
SNAPSHOT_SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = (1, 2)

GENERATION_PREFIX = '.generation-'
# ``tempfile.mkdtemp`` may emit ``_``, which is outside the contracted charset
# [A-Za-z0-9-], so the name is drawn here instead of borrowed.
GENERATION_PATTERN = re.compile(r'^\.generation-[A-Za-z0-9-]{8,}$')
POINTER_NAME = 'current.json'
DIAGNOSTIC_POINTER_NAME = 'diagnostic.json'

ARTIFACT_KINDS = ('published', 'selection', 'diagnostic')
# F27: how a refresh of a bound feed is expected to interact with a
# collection.  Recorded in the artifact so a later download can be checked
# against the refresh that produced it; nothing here performs a refresh.
MERGE_MODES = ('none', 'merge', 'replace')

CREDENTIALS_REDACT = 'redact'
CREDENTIALS_REFERENCE = 'reference'
CREDENTIALS_MODES = (CREDENTIALS_REDACT, CREDENTIALS_REFERENCE)
CREDENTIALS_PERMISSION = 'export.secret'

EMPTY_FAIL_CLOSED = 'fail_closed'
EMPTY_ERROR = 'error'
EMPTY_POLICIES = (EMPTY_FAIL_CLOSED, EMPTY_ERROR)

# Fields of a published row.  The legacy list comes first and keeps its names:
# existing readers must not have their columns renamed (§4.4).
ROW_FIELDS = (
    'proxy', 'score', 'latency_ms', 'jitter_ms', 'reliability', 'min_target_reliability',
    'successes', 'requests', 'checked_at', 'valid_until', 'reputation_status',
    'reputation_sources', 'anonymity', 'anonymity_signals', 'country', 'exit_ip',
    'exit_country', 'asn', 'provider', 'hosting', 'mbps', 'listed_in', 'source_keys',
    'recommended', 'checks', 'passes',
    # added by the snapshot contract
    'protocol', 'uptime', 'age_seconds', 'admission_reason', 'time_state', 'tags',
    'access_id', 'access_revision',
)

# Codes outside the §5.4 canon.  They are proposed here and listed for the
# contract owner in docs/integration/HANDOFF/exportsvc.md; the ones already in
# the canon are reused unchanged.
EXPORT_CODES = {
    'E_EXPORT_PROTOCOL_UNSUPPORTED': (
        'Формат {format} не поддерживает протокол {protocol}.',
        'The {format} format does not support the {protocol} protocol.'),
    'E_EXPORT_TLS_UNSUPPORTED': (
        'Формат {format} не поддерживает прокси с TLS ({proxy}).',
        'The {format} format does not support a TLS proxy ({proxy}).'),
    'E_EXPORT_AUTH_UNSUPPORTED': (
        'Формат {format} не может нести учётные данные адреса.',
        'The {format} format cannot carry credentials for this address.'),
    'E_EXPORT_HOSTNAME_UNSUPPORTED': (
        'Формат {format} принимает только IP-адреса, а адрес задан именем хоста.',
        'The {format} format accepts IP addresses only, but the address is a hostname.'),
    'E_EXPORT_IPV6_UNSUPPORTED': (
        'Формат {format} не поддерживает IPv6 ({proxy}).',
        'The {format} format does not support IPv6 ({proxy}).'),
    'E_EXPORT_DNS_LOCAL': (
        'Адрес {proxy} передаёт DNS прокси, а клиент {target} разрешает его локально.',
        'Address {proxy} sends DNS through the proxy, but client {target} resolves locally.'),
    'E_EXPORT_LIMIT_TRUNCATED': (
        'Формат {format} ограничен {limit} строками, {dropped} строк(и) не вошло.',
        'The {format} format is limited to {limit} rows; {dropped} row(s) did not fit.'),
    'E_EXPORT_TARGET_UNKNOWN': (
        'Версия клиента {target!r} не разобрана; совместимость не подтверждена.',
        'Client version {target!r} could not be parsed; compatibility is unverified.'),
    'E_EXPORT_TARGET_UNPINNED': (
        'Версия клиента не задана, поэтому файл не проверён целевым клиентом. Задайте client_target '
        '(например 1.14.0), переменную PROXY_WORKBENCH_SINGBOX_TARGET или client.json рядом со снимками.',
        'No client version is pinned, so the file was not checked by a target client. Set client_target '
        '(e.g. 1.14.0), PROXY_WORKBENCH_SINGBOX_TARGET, or client.json next to the snapshots.'),
    'E_EXPORT_TARGET_LEGACY_OPTIN': (
        'Отказ через outbound block устарел с sing-box 1.11.0 и записан только по явному запросу legacy-block.',
        'The block-outbound reject is deprecated since sing-box 1.11.0 and is written only on an '
        'explicit legacy-block request.'),
    'E_EXPORT_TARGET_UNVERIFIED': (
        'Версия клиента {target} новее проверенной ({verified}); файл не публикуется без проверки.',
        'Client version {target} is newer than the verified one ({verified}); the file is not published unchecked.'),
    'E_EXPORT_TARGET_UNSUPPORTED': (
        'Версия клиента {target} старее проверяемой ({minimum}).',
        'Client version {target} is older than the checkable one ({minimum}).'),
    'E_EXPORT_CREDENTIALS_NOT_GRANTED': (
        'Включение учётных данных требует разрешения {permission}.',
        'Including credentials requires the {permission} permission.'),
    'E_EXPORT_CLIENT_REJECTED': (
        'Клиент {target} отверг файл: {detail}',
        'Client {target} rejected the file: {detail}'),
    'E_EXPORT_DIRECT_FORBIDDEN': (
        'Файл для {target} содержит outbound типа direct; скрытый DIRECT запрещён.',
        'The file for {target} contains a direct outbound; a hidden DIRECT is forbidden.'),
    'E_EXPORT_CLIENT_MISSING': (
        'Проверка совместимости с клиентом {target} не выполнена: {detail}',
        'Compatibility with client {target} was not checked: {detail}'),
    'E_EXPORT_EMPTY_OUTBOUNDS_UNVERIFIED': (
        'Отказ для {target} задан правилом route без списка outbounds; проверьте его установленным клиентом.',
        'The {target} reject is a route rule with no outbounds; verify it with an installed client.'),
    'E_STATE_PUBLISH_UNCONFIRMED': (
        'Публикация меняет активный пул и требует явного подтверждения.',
        'Publication changes the active pool and requires an explicit confirmation.'),
    'E_STATE_SNAPSHOT_IMMUTABLE': (
        'Поколение {generation} уже записано и не переписывается.',
        'Generation {generation} is already written and is never rewritten.'),
    'E_STATE_SNAPSHOT_MIXED': (
        'Указатель {pointer} ссылается на {generation}, а status.json поколения — {declared}.',
        'Pointer {pointer} points at {generation}, but that status.json declares {declared}.'),
    'E_STATE_SNAPSHOT_MANIFEST': (
        'Контрольная сумма {name} в поколении {generation} не совпала.',
        'The checksum of {name} in generation {generation} does not match the manifest.'),
    'E_DATA_MIGRATION_FAILED': (
        'Таблица export_artifact не соответствует контракту (миграция 11): {detail}',
        'Table export_artifact does not match the contract (migration 11): {detail}'),
}

# Version rules, and the pages they were read from (read 2026-09-26, official
# documentation only, four pages):
#
# * https://sing-box.sagernet.org/migration/ — "Legacy special outbounds are
#   deprecated and can be replaced by rule actions"; the documented migration
#   is ``{"outbound": "block"}`` → ``{"action": "reject"}``.  The same page is
#   the one that names 1.15.0, the newest release line the docs describe.
# * https://sing-box.sagernet.org/configuration/route/rule/ — the rule ``action``
#   field appears under "Changes in sing-box 1.11.0" next to ``outbound``, so
#   rule actions exist from 1.11.0 and not from some later release.  (The
#   dedicated rule-action page carries "Since sing-box 1.13.0" for the *fields
#   of an action*, which is a later extension and not the field's origin.)
# * https://sing-box.sagernet.org/configuration/route/ — "Default outbound tag.
#   the first outbound will be used if empty."  That is why an empty
#   ``outbounds`` list with no ``final`` is marked unverified rather than
#   assumed to fail closed, and why a ``direct`` outbound is forbidden outright.
# * https://sing-box.sagernet.org/configuration/outbound/urltest/ — the member
#   list is ``outbounds`` and ``interval`` takes a duration such as ``"5m"``.
#
# The single version-dependent construct this module writes is the *reject* for
# a set with no usable outbound.  ``route.final`` and the ``urltest`` member
# list carry no deprecation notice in any of these pages, so a set that has
# usable outbounds produces the same file at every version.
SINGBOX_RULE_ACTIONS_FROM = (1, 11, 0)
SINGBOX_MIN_VERIFIED = (1, 0, 0)
SINGBOX_MAX_VERIFIED = (1, 15, 0)

#: A named request for the pre-1.11 reject.  It exists so the deprecated form
#: is reachable on purpose instead of by accident: shipping it because nobody
#: said which client the file is for is what defect 20 is about.
SINGBOX_LEGACY_OPTIN = 'legacy-block'

#: Where a pinned target may come from, in the order the resolver reads it.
#: None of these need a change in ``proxytool.py`` or ``api.py``: the snapshot
#: root and the environment are read here, so a CLI flag, a GUI field or an API
#: parameter only has to hand its value to ``ExportOptions``.
CLIENT_TARGET_ENV = 'PROXY_WORKBENCH_SINGBOX_TARGET'
CLIENT_BINARY_ENV = 'PROXY_WORKBENCH_SINGBOX_BIN'
CLIENT_SIDECAR_NAME = 'client.json'
CLIENT_TARGET_SOURCES = ('options', CLIENT_TARGET_ENV, CLIENT_SIDECAR_NAME, 'unconfigured')


class ExportError(Exception):
    """Contract failure with a stable ``E_*`` code and a localizable detail."""

    def __init__(self, code: str, message: str = '', **context: Any):
        self.code = code
        self.context = dict(context)
        self.detail = message or _reason_text(code, **context)
        super().__init__(self.detail)


def _reason_text(code: str, **context: Any) -> str:
    template = EXPORT_CODES.get(code)
    if not template:
        return code
    ru, en = template
    text = tr(ru, en)
    for name, value in context.items():
        # ``str.format`` conversions belong to the template (``{target!r}``).
        # Substituting only the bare name left the placeholder in the message
        # the user is shown, which is the opposite of actionable.
        text = text.replace('{' + name + '!r}', repr(value))
        text = text.replace('{' + name + '!s}', str(value))
        text = text.replace('{' + name + '}', str(value))
    return text


def generation_name() -> str:
    """A fresh generation directory name inside the contracted charset."""
    return GENERATION_PREFIX + secrets.token_hex(8)


def valid_generation_name(name: Any) -> bool:
    """Only the contracted charset; the name becomes part of a filesystem path."""
    return isinstance(name, str) and bool(GENERATION_PATTERN.fullmatch(name))


@dataclass(frozen=True)
class ExportScope:
    """The area a table shows and an artifact was cut from.

    ``identity`` is the admission scope of :mod:`core`; the remaining fields are
    the filters, the explicit selection and the feed binding that decide which
    admitted rows land in this artifact.  ``digest`` covers the *table* scope
    and deliberately ignores the selection, so a top-5 download and the table
    it came from can be proven to describe the same area (§5.3).
    """

    identity: core.Scope
    protocol: str = 'all'
    countries: tuple[str, ...] = ()
    exclude_hosting: bool = False
    query: str = ''
    quick: str = ''
    network_id: str = 'default'
    selection: tuple[str, ...] = ()
    top: int = 0
    merge_mode: str = 'none'
    source_binding: str | None = None

    def __post_init__(self) -> None:
        if self.protocol not in PROTOCOLS:
            raise ExportError('E_VALIDATION_FIELD', f'protocol must be one of {PROTOCOLS}')
        if self.merge_mode not in MERGE_MODES:
            raise ExportError('E_VALIDATION_FIELD', f'merge_mode must be one of {MERGE_MODES}')
        if not isinstance(self.top, int) or isinstance(self.top, bool) or self.top < 0:
            raise ExportError('E_VALIDATION_FIELD', 'top must be a non-negative integer')
        object.__setattr__(self, 'countries', tuple(str(item).upper() for item in self.countries))
        object.__setattr__(self, 'selection', tuple(str(item) for item in self.selection))

    @property
    def core_scope(self) -> core.Scope:
        """The scope object :func:`core.admit` expects."""
        if self.identity.network_id == self.network_id:
            return self.identity
        return core.Scope(self.identity.collection_id, self.identity.profile_id,
                          self.identity.profile_revision, self.network_id)

    def canonical(self) -> dict[str, Any]:
        return {
            'collection_id': self.identity.collection_id,
            'profile_id': self.identity.profile_id,
            'profile_revision': self.identity.profile_revision,
            'network_id': self.network_id,
            'protocol': self.protocol,
            'countries': list(self.countries),
            'exclude_hosting': bool(self.exclude_hosting),
            'query': self.query,
            'quick': self.quick,
            'merge_mode': self.merge_mode,
            'source_binding': self.source_binding,
        }

    def digest(self) -> str:
        return _digest(self.canonical())

    def artifact_digest(self, kind: str, policy: str, rows: Iterable[Mapping[str, Any]]) -> str:
        """Identity of one artifact: its scope plus exactly which rows it holds."""
        return _digest({'scope': self.canonical(), 'kind': kind, 'policy': policy,
                        'selection': list(self.selection), 'top': self.top,
                        'rows': sorted(str(row.get('proxy') or '') for row in rows)})


def _digest(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
                         default=str).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class SecretGrant:
    """Proof that one caller may put credential *references* into an artifact.

    The grant never carries a secret value: what may be included is a reference
    the vault owner resolves later, under its own permission check.  Anything
    that needs a real direct-auth URI is a different, explicitly requested
    action and is not produced here.
    """

    permission: str = CREDENTIALS_PERMISSION
    allowed: bool = False
    scope_digest: str | None = None
    issued_by: str | None = None

    def check(self, scope: 'ExportScope') -> None:
        if not self.allowed or self.permission != CREDENTIALS_PERMISSION:
            raise ExportError('E_EXPORT_CREDENTIALS_NOT_GRANTED', permission=CREDENTIALS_PERMISSION)
        if self.scope_digest and self.scope_digest != scope.digest():
            raise ExportError('E_AUTH_SCOPE',
                              'The grant was issued for another scope',
                              permission=CREDENTIALS_PERMISSION)


@dataclass(frozen=True)
class ExportOptions:
    """Everything about how one artifact is produced, including what it may contain."""

    sort: str = 'quality'
    top: int = 0
    limits: Mapping[str, int] = field(default_factory=dict)
    credentials: str = CREDENTIALS_REDACT
    client_target: str | None = None
    client_binary: str | None = None
    empty_policy: str = EMPTY_FAIL_CLOSED
    published_at: float | None = None
    set_ttl_seconds: float | None = None
    readonly: bool = True
    unsupported_limit: int = 50
    keep_generations: int = 3

    def __post_init__(self) -> None:
        if self.credentials not in CREDENTIALS_MODES:
            raise ExportError('E_VALIDATION_FIELD', f'credentials must be one of {CREDENTIALS_MODES}')
        if self.empty_policy not in EMPTY_POLICIES:
            raise ExportError('E_VALIDATION_FIELD', f'empty_policy must be one of {EMPTY_POLICIES}')
        if not isinstance(self.top, int) or isinstance(self.top, bool) or self.top < 0:
            raise ExportError('E_VALIDATION_FIELD', 'top must be a non-negative integer')
        if not isinstance(self.limits, Mapping):
            raise ExportError('E_VALIDATION_FIELD', 'limits must be a mapping')

    def limit(self, fmt: str) -> int:
        value = self.limits.get(fmt)
        return int(value) if isinstance(value, int) and value > 0 else 0

    @property
    def wants_credentials(self) -> bool:
        return self.credentials == CREDENTIALS_REFERENCE


# --------------------------------------------------------------------------
# Compatibility preview
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Unsupported:
    """One row this format cannot carry, with the reason it cannot."""

    proxy: str
    fmt: str
    reasons: tuple[str, ...]
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {'proxy': self.proxy, 'format': self.fmt, 'reasons': list(self.reasons),
                'detail': self.detail}


@dataclass(frozen=True)
class CompatReport:
    """What a format does with a set of rows, including what it drops and why.

    ``client_check`` is the result of running the pinned client against the
    generated file: ``passed``, ``failed`` or ``not_run``.  It is never reported
    as ``passed`` on the strength of a JSON parse alone (R14, defect 20).
    """

    fmt: str
    total: int
    supported: tuple[str, ...] = ()
    unsupported: tuple[Unsupported, ...] = ()
    warnings: tuple[str, ...] = ()
    reasons: tuple[str, ...] = ()
    dropped_by_limit: int = 0
    limit: int = 0
    fail_closed: bool = False
    state_detail: str | None = None
    target: str | None = None
    target_state: str = 'unconfigured'
    client_check: str = 'not_run'
    client_detail: str = ''

    @property
    def ok(self) -> bool:
        return not self.unsupported and not self.reasons

    def as_dict(self, unsupported_limit: int = 50) -> dict[str, Any]:
        """Status-ready summary; long ``unsupported`` lists are truncated, and say so."""
        total = len(self.unsupported)
        return {
            'format': self.fmt,
            'total': self.total,
            'supported': len(self.supported),
            'unsupported': total,
            'unsupported_total': total,
            'unsupported_shown': min(total, unsupported_limit),
            'unsupported_limit': unsupported_limit,
            'reasons': list(self.reasons),
            'warnings': list(self.warnings),
            'dropped_by_limit': self.dropped_by_limit,
            'limit': self.limit,
            'fail_closed': self.fail_closed,
            'state_detail': self.state_detail,
            'target': self.target,
            'target_state': self.target_state,
            'client_check': self.client_check,
            'client_detail': self.client_detail,
            'rows': [item.as_dict() for item in self.unsupported[:unsupported_limit]],
        }


def _endpoint(proxy: str) -> tuple[str, str, int] | None:
    """(scheme, host without brackets, port); None when the address is unusable.

    ``urlsplit`` rather than a manual split on the last colon: a proxy string
    that carries userinfo must still yield the host, or redaction would write
    the credentials straight back out.
    """
    raw = str(proxy or '').strip()
    if not raw:
        return None
    try:
        parsed = urlsplit(raw if '://' in raw else 'http://' + raw)
        port = parsed.port
    except (TypeError, ValueError):
        return None
    scheme, host = parsed.scheme, (parsed.hostname or '')
    if not scheme or not host or not port or port <= 0 or port > 65535:
        return None
    return scheme, host.strip('[]'), int(port)


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _is_ipv6(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).version == 6
    except ValueError:
        return False


# Capabilities are derived from the format modules themselves, so there is no
# second protocol table to drift out of sync.  ``credentials`` says whether a
# format can carry an access *reference* at all; the value is never in a file.
def _capabilities(fmt: str) -> dict[str, Any]:
    if fmt in ('json', 'csv'):
        return {'protocols': ('http', 'https', 'socks4', 'socks5'), 'credentials': CREDENTIALS_REFERENCE,
                'hostname': False, 'ipv6': True, 'tls': True}
    if fmt in ('txt', 'protocol'):
        return {'protocols': ('http', 'https', 'socks4', 'socks5'), 'credentials': CREDENTIALS_REDACT,
                'hostname': False, 'ipv6': True, 'tls': True}
    if fmt == 'pac':
        return {'protocols': tuple(formats.PAC_TYPES), 'credentials': CREDENTIALS_REDACT,
                'hostname': True, 'ipv6': True, 'tls': 'https' in formats.PAC_TYPES}
    if fmt == 'clash':
        return {'protocols': tuple(formats.CLASH_TYPES), 'credentials': CREDENTIALS_REFERENCE,
                'hostname': True, 'ipv6': True, 'tls': 'https' in formats.CLASH_TYPES}
    if fmt == 'singbox':
        return {'protocols': tuple(formats.SINGBOX_TYPES), 'credentials': CREDENTIALS_REFERENCE,
                'hostname': True, 'ipv6': True, 'tls': 'https' in formats.SINGBOX_TYPES}
    if fmt == 'proxychains':
        return {'protocols': tuple(PROXYCHAINS_TYPES), 'credentials': CREDENTIALS_REDACT,
                'hostname': True, 'ipv6': True, 'tls': 'https' in PROXYCHAINS_TYPES}
    raise ExportError('E_VALIDATION_FIELD', f'unknown format: {fmt}')


def compat_report(rows: Sequence[Mapping[str, Any]], fmt: str, *, options: ExportOptions | None = None,
                  target: str | None = None, target_state: str = 'unconfigured',
                  client_check_result: str = 'not_run',
                  client_detail: str = '') -> CompatReport:
    """Preview one format against a set of rows before anything is written.

    A row is never dropped quietly: it lands in ``unsupported`` with the reason
    (``E_EXPORT_TLS_UNSUPPORTED`` rather than a generic "wrong protocol"), or it
    is reported as a warning when the format carries it with different semantics.
    """
    options = options or ExportOptions()
    caps = _capabilities(fmt)
    limit = options.limit(fmt)
    supported: list[str] = []
    unsupported: list[Unsupported] = []
    warnings: list[str] = []
    for row in rows:
        proxy = str(row.get('proxy') or '')
        parsed = _endpoint(proxy)
        if parsed is None:
            unsupported.append(Unsupported(proxy, fmt, ('E_VALIDATION_FIELD',),
                                           _reason_text('E_VALIDATION_FIELD')))
            continue
        scheme, host, _ = parsed
        protocol = proxy_protocol(proxy)
        reasons: list[str] = []
        if scheme == 'https' and not caps['tls']:
            reasons.append('E_EXPORT_TLS_UNSUPPORTED')
        elif protocol not in {PROTOCOL_ALIASES.get(item, item) for item in caps['protocols']}:
            reasons.append('E_EXPORT_PROTOCOL_UNSUPPORTED')
        if not caps['hostname'] and not _is_ip(host):
            reasons.append('E_EXPORT_HOSTNAME_UNSUPPORTED')
        if not caps['ipv6'] and _is_ipv6(host):
            reasons.append('E_EXPORT_IPV6_UNSUPPORTED')
        has_access = bool(row.get('access_id') or row.get('access_ref'))
        if has_access and caps['credentials'] == CREDENTIALS_REDACT:
            reasons.append('E_EXPORT_AUTH_UNSUPPORTED')
        if reasons:
            unsupported.append(Unsupported(proxy, fmt, tuple(reasons),
                                           _reason_text(reasons[0], format=fmt, protocol=protocol, proxy=proxy)))
            continue
        if scheme == 'socks5h':
            # Remote DNS is a different behaviour, not a different address: the
            # row is kept, but the difference is stated instead of being dropped.
            note = _reason_text('E_EXPORT_DNS_LOCAL', proxy=proxy, target=target or fmt)
            if note not in warnings:
                warnings.append(note)
        supported.append(proxy)
    dropped = 0
    if limit and len(supported) > limit:
        dropped = len(supported) - limit
        supported = supported[:limit]
        warnings.append(_reason_text('E_EXPORT_LIMIT_TRUNCATED', format=fmt, limit=limit, dropped=dropped))
    return CompatReport(fmt=fmt, total=len(rows), supported=tuple(supported),
                        unsupported=tuple(unsupported), warnings=tuple(warnings),
                        dropped_by_limit=dropped, limit=limit, target=target,
                        target_state=target_state, client_check=client_check_result,
                        client_detail=client_detail)


# --------------------------------------------------------------------------
# sing-box: one reject, expressed the way the target version reads it
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SingBoxTarget:
    """A parsed client version and the one construct that depends on it.

    ``state`` says how much is actually known: ``configured`` (a version the
    user pinned), ``unconfigured`` (none pinned), ``legacy_optin`` (somebody
    asked by name for the pre-1.11 reject), ``unknown`` (unparsable),
    ``unsupported`` (older than anything checkable) and ``unverified`` (newer
    than the newest version whose rules were read).
    """

    version: str
    numeric: tuple[int, ...] | None
    uses_rule_actions: bool = False
    state: str = 'unconfigured'
    reason: str | None = None

    @property
    def supported(self) -> bool:
        return self.reason is None

    @property
    def configured(self) -> bool:
        return self.state == 'configured'

    @property
    def legacy_optin(self) -> bool:
        """Somebody asked for the deprecated reject by name."""
        return self.state == 'legacy_optin'

    @property
    def version_dependent_reject_available(self) -> bool:
        """Whether the version says which reject form this client reads.

        A pinned version always does: below 1.11.0 that is the ``block``
        outbound, from 1.11.0 on it is the rule action.  Only a target that was
        never established leaves the choice open, and then the only shapes left
        are a construct deprecated since 1.11.0 and one whose acceptance the
        docs do not state - so the generator refuses instead of choosing.
        """
        return self.configured or self.legacy_optin

    def require(self) -> 'SingBoxTarget':
        if not self.supported:
            raise ExportError(self.reason or 'E_EXPORT_TARGET_UNVERIFIED', target=self.version,
                              verified=_version_text(SINGBOX_MAX_VERIFIED),
                              minimum=_version_text(SINGBOX_MIN_VERIFIED))
        return self


def _version_text(numeric: Sequence[int]) -> str:
    return '.'.join(str(part) for part in numeric)


def singbox_target(version: str | None) -> SingBoxTarget:
    """Parse ``1.11.0`` / ``v1.11`` / ``latest`` / ``legacy-block`` for the renderer.

    A pinned version produces the reject the pinned client reads.  No pinned
    version produces the version-independent shape, and the artifact says the
    target was unconfigured and that no client check ran.  An unparsable or
    unverified version is refused: falling back silently would mean shipping a
    config for a client nobody checked (R14).  ``legacy-block`` is the one
    named exception, and it is only a named exception.
    """
    raw = (version or '').strip()
    if not raw:
        return SingBoxTarget('legacy', None, False, 'unconfigured')
    if raw.lower() in ('latest', 'current'):
        return SingBoxTarget(_version_text(SINGBOX_MAX_VERIFIED), SINGBOX_MAX_VERIFIED, True, 'configured')
    if raw.lower() == SINGBOX_LEGACY_OPTIN:
        return SingBoxTarget(SINGBOX_LEGACY_OPTIN, None, False, 'legacy_optin')
    match = re.fullmatch(r'v?(\d+)(?:\.(\d+))?(?:\.(\d+))?', raw)
    if not match:
        return SingBoxTarget(raw, None, False, 'unknown', 'E_EXPORT_TARGET_UNKNOWN')
    numeric = tuple(int(part or 0) for part in match.groups())
    if numeric < SINGBOX_MIN_VERIFIED:
        return SingBoxTarget(raw, numeric, False, 'unsupported', 'E_EXPORT_TARGET_UNSUPPORTED')
    if numeric > SINGBOX_MAX_VERIFIED:
        return SingBoxTarget(raw, numeric, True, 'unverified', 'E_EXPORT_TARGET_UNVERIFIED')
    return SingBoxTarget(raw, numeric, numeric >= SINGBOX_RULE_ACTIONS_FROM, 'configured')


@dataclass(frozen=True)
class ResolvedClientTarget:
    """Where a target version and a client binary came from, and their values.

    The value is what :func:`singbox_target` parses; ``source`` is which of
    ``options`` / the environment / the snapshot sidecar supplied it, so an
    artifact can record that its compatibility was pinned by a setting rather
    than by the user typing it into this run.
    """

    target: SingBoxTarget
    binary: str | None = None
    source: str = 'unconfigured'
    binary_source: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {'client_target': self.target.version, 'client_target_state': self.target.state,
                'client_target_source': self.source,
                'client_binary': self.binary, 'client_binary_source': self.binary_source}

    @property
    def verified_by(self) -> bool:
        """Whether a client binary was configured and the file was sent to it."""
        return bool(self.binary)


def read_client_sidecar(directory: Any) -> dict[str, Any]:
    """``client.json`` next to the snapshots: a pinned target for every run.

    A file, not a setting, so a portable data directory carries its own
    compatibility contract; a missing or unreadable file is "nothing pinned",
    never an error that stops an export.
    """
    try:
        data = json.loads((Path(directory) / CLIENT_SIDECAR_NAME).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, Mapping) else {}


def resolve_client_target(options: 'ExportOptions | None' = None, *, directory: Any = None,
                          environ: Mapping[str, str] | None = None) -> ResolvedClientTarget:
    """Find the target version and the client binary, in one documented order.

    ``options`` (a CLI flag, a GUI field or an API parameter) wins, then
    ``PROXY_WORKBENCH_SINGBOX_TARGET`` / ``PROXY_WORKBENCH_SINGBOX_BIN``, then
    ``client.json`` in the snapshot root, then nothing at all - which is a
    state, not a failure.  The same value flows into the status of every
    artifact, so "was this file compatibility-checked" is answerable from the
    file itself.
    """
    options = options or ExportOptions()
    environ = os.environ if environ is None else environ
    sidecar = read_client_sidecar(directory) if directory is not None else {}
    version, source = options.client_target, 'options'
    if not version:
        version, source = environ.get(CLIENT_TARGET_ENV) or '', CLIENT_TARGET_ENV
    if not version:
        version, source = str(sidecar.get('target') or sidecar.get('client_target') or ''), CLIENT_SIDECAR_NAME
    if not version:
        source = 'unconfigured'
    binary, binary_source = options.client_binary, 'options'
    if not binary:
        binary, binary_source = environ.get(CLIENT_BINARY_ENV) or '', CLIENT_BINARY_ENV
    if not binary:
        binary, binary_source = str(sidecar.get('binary') or sidecar.get('client_binary') or ''), CLIENT_SIDECAR_NAME
    return ResolvedClientTarget(singbox_target(version), binary or None,
                                source if version else 'unconfigured',
                                binary_source if binary else None)


def render_singbox(rows: Sequence[Mapping[str, Any]], *, options: ExportOptions | None = None,
                   target: str | SingBoxTarget | None = None) -> str:
    """A sing-box config that fails closed for this exact client version.

    For a version that predates rule actions the fail-closed reject is the
    ``block`` outbound, reachable only for a pinned ``<1.11`` client or for an
    explicit ``legacy-block`` request.  From 1.11.0 on that outbound is
    deprecated and the same refusal is a route rule with ``action: reject``.
    Nothing here ever emits a ``direct`` outbound, so an empty or
    unsupported-only set cannot turn into an unproxied connection (defect 20),
    and a set with usable outbounds is version-independent, so it is written
    even when no target was pinned - marked unverified, never marked checked.
    """
    options = options or ExportOptions()
    resolved = target if isinstance(target, SingBoxTarget) else singbox_target(
        options.client_target if target is None else target)
    if not resolved.supported:
        raise ExportError(resolved.reason or 'E_EXPORT_TARGET_UNVERIFIED', target=resolved.version,
                          verified=_version_text(SINGBOX_MAX_VERIFIED),
                          minimum=_version_text(SINGBOX_MIN_VERIFIED))
    cap = options.limit('singbox') or formats.CLASH_LIMIT
    report = compat_report(rows, 'singbox', options=options, target=resolved.version)
    usable = _limit_rows(rows, report.supported, cap)
    if not usable and not resolved.version_dependent_reject_available:
        # The reject is the only construct here whose form depends on the
        # version, and nothing established the version.  Writing either form
        # would be a guess; refusing is the fail-closed answer, and
        # ``_singbox_with_report`` records the reason next to the file that is
        # missing.
        raise ExportError('E_EXPORT_TARGET_UNPINNED', target=resolved.version)
    if resolved.uses_rule_actions:
        return _singbox_rule_actions(usable)
    return formats.singbox(usable, cap, fail_closed='block')


def _singbox_rule_actions(rows: Sequence[Mapping[str, Any]]) -> str:
    """Route-rule reject, valid from 1.11.0 where the block outbound is legacy.

    With no usable row the config has no outbound at all and a catch-all
    ``reject`` rule.  That shape is the documented migration target, but the
    official docs read for this module do not state whether an empty
    ``outbounds`` list is accepted, so the artifact is marked
    ``E_EXPORT_EMPTY_OUTBOUNDS_UNVERIFIED`` and the pinned client decides.
    """
    outbounds: list[dict[str, Any]] = []
    tags: list[str] = []
    for row in rows:
        proxy = str(row.get('proxy') or '')
        parsed = _endpoint(proxy)
        if parsed is None:
            continue
        scheme, host, port = parsed
        kind, version = formats.SINGBOX_TYPES.get(proxy_protocol(proxy), (None, None))
        if not kind:
            continue
        shown = f'[{host}]:{port}' if ':' in host else f'{host}:{port}'
        tag = f"{row.get('country') or '??'} {scheme} {shown}"
        outbound = {'type': kind, 'tag': tag, 'server': host, 'server_port': port}
        if version:
            outbound['version'] = version
        outbounds.append(outbound)
        tags.append(tag)
    route: dict[str, Any] = {'rules': [{'action': 'reject'}]}
    if tags:
        outbounds.insert(0, {'type': 'urltest', 'tag': 'auto', 'outbounds': tags,
                             'url': 'http://www.gstatic.com/generate_204', 'interval': '5m'})
        route['final'] = 'auto'
    config = {'log': {'level': 'warn'},
              'inbounds': [{'type': 'mixed', 'tag': 'in', 'listen': '127.0.0.1', 'listen_port': 2080}],
              'outbounds': outbounds, 'route': route}
    return json.dumps(config, ensure_ascii=False, indent=2) + '\n'


def check_singbox(config: Mapping[str, Any], target: str | SingBoxTarget | None = None) -> tuple[bool, tuple[str, ...]]:
    """Structural check of a generated config against the target version's rules.

    This is not proof that a client accepts the file; it only refuses the two
    shapes the target version is known to reject: a deprecated special outbound
    from 1.11.0 on, and a rule action before it existed.  ``client_check()`` is
    the part that needs the real binary.
    """
    resolved = target if isinstance(target, SingBoxTarget) else singbox_target(target)
    if not resolved.supported:
        return False, (resolved.reason or 'E_EXPORT_TARGET_UNVERIFIED',)
    outbounds = config.get('outbounds')
    outbounds = outbounds if isinstance(outbounds, list) else []
    route = config.get('route') if isinstance(config.get('route'), Mapping) else {}
    rules = route.get('rules') if isinstance(route, Mapping) else None
    tags = {item.get('tag') for item in outbounds if isinstance(item, Mapping)}
    failures: list[str] = []
    if resolved.uses_rule_actions:
        if any(isinstance(item, Mapping) and item.get('type') in ('block', 'dns') for item in outbounds):
            failures.append('E_EXPORT_TARGET_UNVERIFIED')
        has_reject = any(isinstance(rule, Mapping) and rule.get('action') == 'reject'
                         for rule in (rules or []))
        # An empty set has no outbound to reject through, so the refusal is the
        # route rule itself.  Whether an empty outbounds list is accepted is
        # exactly what the pinned client is asked about.
        if not outbounds:
            failures.append('E_EXPORT_EMPTY_OUTBOUNDS_UNVERIFIED')
        if route.get('final') is not None and route.get('final') not in tags:
            failures.append('E_EXPORT_TARGET_UNVERIFIED')
    else:
        if any(isinstance(rule, Mapping) and rule.get('action') for rule in (rules or [])):
            failures.append('E_EXPORT_TARGET_UNVERIFIED')
        if not outbounds:
            failures.append('E_EXPORT_EMPTY_OUTBOUNDS_UNVERIFIED')
    if any(isinstance(item, Mapping) and item.get('type') == 'direct' for item in outbounds):
        failures.append('E_EXPORT_DIRECT_FORBIDDEN')
    return not failures, tuple(dict.fromkeys(failures))


def client_check(config_text: str, target: str | SingBoxTarget | None = None, *,
                 binary: str | None = None, timeout: float = 20.0) -> tuple[str, str]:
    """Run the pinned client over a generated file: ``passed``, ``failed``, ``not_run``.

    There is no substitute for this in the codebase: a JSON parse proves
    nothing about a client's schema, which is the whole of R14.  When the
    binary is absent the answer is ``not_run`` with the reason, never ``passed``.
    """
    resolved = target if isinstance(target, SingBoxTarget) else singbox_target(target)
    if binary is None:
        return 'not_run', _reason_text('E_EXPORT_CLIENT_MISSING', target=resolved.version,
                                       detail='no pinned client binary is configured')
    try:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'config.json'
            path.write_text(config_text, encoding='utf-8')
            done = subprocess.run([binary, 'check', '-c', str(path), '--log-level', 'error'],
                                  capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        return 'not_run', _reason_text('E_EXPORT_CLIENT_MISSING', target=resolved.version,
                                       detail='the pinned client binary was not found')
    except (OSError, subprocess.SubprocessError) as exc:
        return 'failed', f'{type(exc).__name__}: {exc}'
    if done.returncode == 0:
        return 'passed', (done.stdout or done.stderr or '').strip()[:400]
    return 'failed', (done.stdout or done.stderr or '').strip()[:400]


# --------------------------------------------------------------------------
# Row rendering
# --------------------------------------------------------------------------

def _limit_rows(rows: Sequence[Mapping[str, Any]], supported: Sequence[str], cap: int) -> list[Mapping[str, Any]]:
    wanted = set(supported)
    chosen = [row for row in rows if str(row.get('proxy') or '') in wanted]
    return chosen[:cap] if cap else chosen


def _row_key(row: Mapping[str, Any]) -> str:
    return str(row.get('endpoint_id') or row.get('proxy') or '')


# Keys a row must never carry into an artifact.  ``access_id`` and
# ``access_revision`` are references and stay; anything that smells like a value
# is dropped, because a caller handing us a row with a secret in it is enough to
# leak that secret into a file the user then shares.
REDACTED_KEYS = ('password', 'passwd', 'pass', 'username', 'user', 'login', 'token',
                 'secret', 'api_key', 'apikey', 'credential', 'credentials', 'auth', 'secret_ref')


def redact_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """One row without any value that could authenticate somebody.

    A published row refers to its access (``access_id``) and never carries the
    access itself, and ``proxy`` keeps only ``scheme://host:port`` even if a
    caller passed userinfo in it (F28, §4.4).
    """
    item = {name: value for name, value in row.items() if name.lower() not in REDACTED_KEYS}
    proxy = item.get('proxy')
    parsed = _endpoint(str(proxy)) if proxy else None
    if parsed is not None:
        scheme, host, port = parsed
        # IPv6 keeps its brackets: a bare host:port is not a parseable endpoint.
        item['proxy'] = f'{scheme}://[{host}]:{port}' if ':' in host else f'{scheme}://{host}:{port}'
    return item


def attach_admission(rows: Sequence[Mapping[str, Any]], selection: core.Selection | None) -> list[dict[str, Any]]:
    """Add ``age_seconds``/``admission_reason`` to rows, computed at issue time.

    Both fields are transport, not storage: they come from the admission that
    was actually made, so a snapshot cannot claim a reason it never decided.
    Every row is redacted on the way through.
    """
    decisions = {}
    if selection is not None:
        decisions = {item.endpoint_id: item for item in selection.admissions}
    result = []
    for row in rows:
        item = redact_row(row)
        decision = decisions.get(_row_key(row))
        if decision is not None:
            item.update(decision.as_dict())
            item['admission_reason'] = decision.reason_code or 'OK'
        else:
            item['age_seconds'] = None
            item['admission_reason'] = item.get('admission_reason')
            item['time_state'] = item.get('time_state')
        result.append(item)
    return result


def render_txt(rows: Sequence[Mapping[str, Any]]) -> str:
    """One canonical endpoint per line; no credentials, ever."""
    return ''.join(f"{row.get('proxy')}\n" for row in rows if row.get('proxy'))


def render_hostport(rows: Sequence[Mapping[str, Any]]) -> str:
    return ''.join(f"{_address(row.get('proxy'))}\n" for row in rows if row.get('proxy'))


def render_snapshot_txt(rows: Sequence[Mapping[str, Any]], *, status: Mapping[str, Any]) -> str:
    """The same list, headed by what it is, because a static file cannot expire.

    A plain ``proxies.txt`` cannot take its own TTL back, so the promise is shown
    as text rather than made silently (F09, §2.4).
    """
    lines = [
        '# Proxy Workbench snapshot.',
        f"# generation: {status.get('generation')}",
        f"# kind: {status.get('kind')}",
        f"# profile: {status.get('profile')} revision {status.get('profile_revision')}",
        f"# collection: {status.get('collection_id')}",
        f"# scope_digest: {status.get('scope_digest')}",
        f"# published_at: {status.get('published_at')}",
        f"# expires_at: {status.get('expires_at')}",
        f"# max_age_seconds: {status.get('max_age_seconds')}",
        f"# rows: {len(rows)}",
        '# ' + tr("Этот статический файл не может отозвать сам себя. Проверяйте истёкшие строки заново:",
                  "This static file cannot revoke itself. Re-check rows past expires_at."),
    ]
    return '\n'.join(lines) + '\n' + render_txt(rows)


def render_csv(rows: Sequence[Mapping[str, Any]], fields: Sequence[str] = ROW_FIELDS) -> str:
    buffer = io.StringIO(newline='')
    writer = csv.DictWriter(buffer, fieldnames=list(fields), extrasaction='ignore')
    writer.writeheader()
    for row in rows:
        writer.writerow(_flat(redact_row(row)))
    return buffer.getvalue()


def render_json(rows: Sequence[Mapping[str, Any]]) -> str:
    redacted = [redact_row(row) for row in rows]
    return '[\n' + ',\n'.join(json.dumps(row, ensure_ascii=False, default=str) for row in redacted) + '\n]\n'


def render_protocol_files(rows: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """``host:port`` per protocol; a row of another protocol is simply not here."""
    buckets: dict[str, list[str]] = {scheme: [] for scheme in PROTOCOL_EXPORTS}
    for row in rows:
        proxy = str(row.get('proxy') or '')
        if not proxy:
            continue
        scheme = proxy_protocol(proxy)
        if scheme in buckets:
            buckets[scheme].append(_address(proxy))
    return {name: ''.join(f'{line}\n' for line in buckets[scheme])
            for scheme, name in PROTOCOL_EXPORTS.items()}


def render_proxychains(rows: Sequence[Mapping[str, Any]]) -> str:
    header = ('# Paste into the [ProxyList] section of proxychains.conf '
              '(HTTPS proxies are not supported there).\n')
    lines = []
    for row in rows:
        proxy = str(row.get('proxy') or '')
        if not proxy or proxy_protocol(proxy) not in PROXYCHAINS_TYPES:
            continue
        host, _, port = _address(proxy).rpartition(':')
        lines.append(f'{PROXYCHAINS_TYPES[proxy_protocol(proxy)]} {host.strip("[]")} {port}\n')
    return header + ''.join(lines)


def render_pac(rows: Sequence[Mapping[str, Any]], options: ExportOptions | None = None) -> str:
    """Browser PAC from the usable rows; never a DIRECT fallback."""
    options = options or ExportOptions()
    report = compat_report(rows, 'pac', options=options)
    usable = _limit_rows(rows, report.supported, options.limit('pac'))
    return formats.pac([str(row.get('proxy')) for row in usable])


def render_clash(rows: Sequence[Mapping[str, Any]], options: ExportOptions | None = None) -> str:
    """A Clash/Mihomo config; empty stays ``MATCH,REJECT`` (formats.clash)."""
    options = options or ExportOptions()
    report = compat_report(rows, 'clash', options=options)
    return formats.clash(_limit_rows(rows, report.supported, options.limit('clash')))


def _address(proxy: Any) -> str:
    return str(proxy).partition('://')[2] or str(proxy)


def _flat(row: Mapping[str, Any]) -> dict[str, Any]:
    """CSV view: nested verdict objects become columns, credentials never appear.

    ``reputation_status`` is derived with the engine's own function when the row
    does not carry it, so the column is a verdict and not an empty cell.
    """
    data = dict(row)
    speed = data.get('speed') if isinstance(data.get('speed'), Mapping) else {}
    anonymity = data.get('anonymity') if isinstance(data.get('anonymity'), Mapping) else {}
    history = data.get('history') if isinstance(data.get('history'), Mapping) else {}
    data['speed_bytes'] = speed.get('bytes', '')
    data['speed_ms'] = speed.get('ms', '')
    data['speed_state'] = speed.get('state', '')
    data['anonymity'] = anonymity.get('level', '')
    data['anonymity_signals'] = ','.join(str(item) for item in (anonymity.get('signals') or []))
    data['checks'] = history.get('checks', data.get('checks', ''))
    data['passes'] = history.get('passes', data.get('passes', ''))
    data['mbps'] = speed.get('mbps', data.get('mbps', ''))
    data['reputation_status'] = data.get('reputation_status') or reputation_status(data)
    source_keys = data.get('source_keys')
    if isinstance(source_keys, (list, tuple, set, frozenset)):
        data['source_keys'] = ','.join(str(item) for item in source_keys)
    tags = data.get('tags')
    if isinstance(tags, (list, tuple, set, frozenset)):
        data['tags'] = ','.join(str(item) for item in tags)
    data.pop('samples', None)
    return {name: ('' if data.get(name) is None else data.get(name)) for name in ROW_FIELDS}


# --------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SnapshotStatus:
    """``status.json`` as a value object, with the fields consumers read.

    Legacy names survive verbatim (``generated_at``, ``valid_until``, ``stale``,
    ``empty_export``, the duplicated filter fields); the new contract fields are
    added, not substituted (§4.3).
    """

    kind: str
    generation: str
    state: str
    state_detail: str
    stop_reason: str
    scope: ExportScope
    policy: core.Policy
    options: ExportOptions
    published_at: float
    expires_at: float | None
    counts: Mapping[str, int]
    selection: Mapping[str, Any]
    compat: Mapping[str, Any]
    digest: str
    artifact_digest: str
    deficit_reasons: tuple[str, ...] = ()
    desired: int | None = None
    next_attempt_at: float | None = None
    legacy: bool = False
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def profile(self) -> str:
        return self.scope.identity.profile_id

    @property
    def stale(self) -> bool:
        """Whether the set itself is stale at publication time; never a row TTL."""
        return self.state == core.SELECTION_STALE

    def as_dict(self) -> dict[str, Any]:
        counts = dict(self.counts)
        now = self.published_at
        report: dict[str, Any] = {
            'schema_version': SNAPSHOT_SCHEMA_VERSION,
            'kind': self.kind,
            'profile': self.profile,
            'profile_id': self.scope.identity.profile_id,
            'profile_revision': self.scope.identity.profile_revision,
            'collection_id': self.scope.identity.collection_id,
            'generation': self.generation,
            'state': self.state,
            'state_detail': self.state_detail,
            'stop_reason': self.stop_reason,
            'scope': {'protocol': self.scope.protocol, 'countries': list(self.scope.countries),
                      'exclude_hosting': bool(self.scope.exclude_hosting)},
            'scope_digest': self.digest,
            'artifact_digest': self.artifact_digest,
            'merge_mode': self.scope.merge_mode,
            'source_binding': self.scope.source_binding,
            'max_age_seconds': self.policy.max_age_seconds,
            'min_success': self.policy.min_success,
            'sort': self.options.sort,
            'top': self.options.top,
            'credentials': self.options.credentials,
            'client_target': (self.compat.get('client_target')
                              if self.compat.get('client_target') is not None else self.options.client_target),
            'client_target_state': self.compat.get('client_target_state', 'unconfigured'),
            'client_target_source': self.compat.get('client_target_source', 'unconfigured'),
            'client_binary': self.compat.get('client_binary'),
            'client_target_required': self.compat.get('client_target_required', False),
            'min_anonymity': self.policy.min_anonymity,
            'protocol': self.scope.protocol,
            'countries': list(self.scope.countries),
            'exclude_hosting': bool(self.scope.exclude_hosting),
            'max_latency': self.policy.max_latency_ms,
            'query': self.scope.query,
            'quick': self.scope.quick,
            'watch_minutes': 0.0,
            'generated_at': now,
            'published_at': now,
            'valid_until': self.expires_at,
            'expires_at': self.expires_at,
            'stale': self.stale,
            'empty_export': self.state == core.SELECTION_EMPTY,
            'complete': self.state == core.SELECTION_COMPLETE and counts.get('exported', 0) > 0,
            'exported': counts.get('exported', 0),
            'admitted': counts.get('admitted', counts.get('exported', 0)),
            'rejected': counts.get('rejected', 0),
            'admission_counts': dict(counts.get('admission_counts') or {}),
            'checked': counts.get('checked', 0),
            'pending': counts.get('pending', 0),
            'passed': counts.get('passed', 0),
            'candidates': counts.get('candidates', 0),
            'scope_candidates': counts.get('scope_candidates', 0),
            'local_filtered': counts.get('local_filtered', 0),
            'selection_requested': self.selection.get('requested', 0),
            'selection_exported': self.selection.get('exported', 0),
            'selection_missing': list(self.selection.get('missing') or ()),
            'selection_truncated': self.selection.get('truncated', 0),
            'deficit_reasons': list(self.deficit_reasons),
            'desired': self.desired,
            'next_attempt_at': self.next_attempt_at,
            'compat': dict(self.compat),
            # Profile and measurement detail the engine owns.  They keep their
            # names and are always present, so a consumer never has to test for
            # absence; an engine that reports them overwrites these defaults.
            'targets': [], 'request_profile': '', 'request_profile_digest': '',
            'reputation': {}, 'anonymity': {}, 'source_quality': {},
            'source_health_basis': '', 'breakdown': {'protocols': {}, 'countries': {}},
        }
        if self.legacy:
            report['legacy'] = True
        report.update(dict(self.extra))
        return report


def build_status(*, kind: str, generation: str, scope: ExportScope, policy: core.Policy,
                 options: ExportOptions, rows: Sequence[Mapping[str, Any]],
                 selection: core.Selection | None = None, now: float | None = None,
                 run_state: Mapping[str, Any] | None = None,
                 compat: Mapping[str, Any] | None = None,
                 extra: Mapping[str, Any] | None = None) -> SnapshotStatus:
    """Assemble one status from a scope, a policy and the rows that were published."""
    if kind not in ARTIFACT_KINDS:
        raise ExportError('E_VALIDATION_FIELD', f'kind must be one of {ARTIFACT_KINDS}')
    published_at = float(now if now is not None
                         else (options.published_at if options.published_at is not None else time.time()))
    if selection is not None:
        state, detail = selection.state, selection.state_detail
        expires_at = selection.expires_at
    elif rows:
        state, detail = core.SELECTION_COMPLETE, core.DETAIL_OK
        expires_at = published_at + (options.set_ttl_seconds or policy.max_age_seconds)
    else:
        state, detail = core.SELECTION_EMPTY, core.DETAIL_NO_MATCH
        expires_at = None
    run_state = dict(run_state or {})
    if run_state.get('state') in core.SELECTION_STATES:
        state, detail = str(run_state['state']), str(run_state.get('state_detail') or detail)
    if run_state.get('expires_at') is not None:
        expires_at = float(run_state['expires_at'])
    missing = sorted(set(scope.selection) - {str(row.get('proxy') or '') for row in rows})
    counts = {
        'exported': len(rows),
        'admitted': len(selection.admitted) if selection is not None else len(rows),
        'rejected': len(selection.rejected) if selection is not None else 0,
        'admission_counts': dict(selection.counts) if selection is not None else {},
        'checked': int(run_state.get('checked', len(rows))),
        'scope_candidates': int(run_state.get('scope_candidates', len(rows))),
        'candidates': int(run_state.get('candidates', len(rows))),
        'passed': int(run_state.get('passed', len(rows))),
        'local_filtered': int(run_state.get('local_filtered', 0)),
        'pending': max(0, counts_scope(run_state, len(rows)) - int(run_state.get('checked', len(rows)))),
    }
    compat = dict(compat or {'files': {}, 'unsupported': [], 'warnings': [],
                              'client_checks': {}, 'credentials': options.credentials,
                              'client_target': options.client_target})
    stop_reason = str(run_state.get('stop_reason') or
                      ('complete' if state == core.SELECTION_COMPLETE else
                       'expired' if state == core.SELECTION_STALE else
                       'stopped' if state == core.SELECTION_PARTIAL else 'complete'))
    return SnapshotStatus(kind=kind, generation=generation, state=state, state_detail=detail,
                          stop_reason=stop_reason, scope=scope, policy=policy, options=options,
                          published_at=published_at, expires_at=expires_at, counts=counts,
                          selection={'requested': len(scope.selection), 'exported': len(rows),
                                     'missing': missing,
                                     'truncated': max(0, len(rows) - len(scope.selection))
                                     if scope.selection else 0},
                          compat=compat,
                          digest=scope.digest(),
                          artifact_digest=scope.artifact_digest(
                              kind, _digest({'sort': options.sort, 'top': options.top,
                                             'credentials': options.credentials,
                                             'max_age': policy.max_age_seconds}), rows),
                          deficit_reasons=tuple(run_state.get('deficit_reasons') or ()),
                          desired=run_state.get('desired'),
                          next_attempt_at=run_state.get('next_attempt_at'),
                          extra=dict(extra or {}))


def counts_scope(run_state: Mapping[str, Any], fallback: int) -> int:
    try:
        return int(run_state.get('scope_candidates', fallback))
    except (TypeError, ValueError):
        return fallback


def status_from_dict(data: Mapping[str, Any]) -> SnapshotStatus:
    """Rebuild a status for a reader, refusing what this version cannot serve.

    A version-1 status is accepted and flagged ``legacy``; an unknown version is
    a refusal, because serving an unvalidated snapshot as valid is exactly the
    failure the contract forbids.
    """
    if not isinstance(data, Mapping):
        raise ExportError('E_VALIDATION_SCHEMA', 'status.json is not an object')
    version = data.get('schema_version')
    if not isinstance(version, int) or isinstance(version, bool) or version not in SUPPORTED_SCHEMA_VERSIONS:
        raise ExportError('E_STATE_SNAPSHOT_SCHEMA',
                          f'snapshot schema_version {version!r} is not supported')
    legacy = version < SNAPSHOT_SCHEMA_VERSION
    if not legacy and not data.get('scope_digest'):
        raise ExportError('E_VALIDATION_SCHEMA', 'a version-2 status must carry scope_digest')
    scope = ExportScope(
        identity=core.Scope(_as_text(data.get('collection_id') or ''),
                            _as_text(data.get('profile_id') or data.get('profile') or ''),
                            _as_int(data.get('profile_revision'), 1)),
        protocol=_as_text(data.get('protocol') or 'all'),
        countries=tuple(data.get('countries') or ()),
        exclude_hosting=bool(data.get('exclude_hosting')),
        query=_as_text(data.get('query') or ''),
        quick=_as_text(data.get('quick') or ''),
        merge_mode=_as_text(data.get('merge_mode') or 'none'),
        source_binding=data.get('source_binding'))
    policy = core.Policy(max_age_seconds=_as_float(data.get('max_age_seconds'),
                                                   core.DEFAULT_MAX_AGE_SECONDS),
                         min_success=_as_float(data.get('min_success'), 2 / 3),
                         min_anonymity=_as_text(data.get('min_anonymity') or 'any'))
    options = ExportOptions(sort=_as_text(data.get('sort') or 'quality'), top=_as_int(data.get('top'), 0),
                            credentials=_as_text(data.get('credentials') or CREDENTIALS_REDACT),
                            client_target=data.get('client_target'))
    known = ('schema_version', 'legacy', 'kind', 'profile', 'profile_id', 'profile_revision',
             'collection_id', 'generation', 'state', 'state_detail', 'stop_reason', 'scope',
             'scope_digest', 'artifact_digest', 'merge_mode', 'source_binding', 'max_age_seconds',
             'min_success', 'min_anonymity', 'sort', 'top', 'credentials', 'client_target',
             'client_target_state', 'client_target_source', 'client_binary', 'client_target_required',
             'protocol', 'countries', 'exclude_hosting', 'max_latency', 'query', 'quick',
             'watch_minutes', 'generated_at', 'published_at', 'valid_until', 'expires_at',
             'stale', 'empty_export', 'complete', 'exported', 'admitted', 'rejected',
             'admission_counts', 'checked', 'pending', 'passed', 'candidates',
             'scope_candidates', 'local_filtered', 'selection_requested', 'selection_exported',
             'selection_missing', 'selection_truncated', 'deficit_reasons', 'desired',
             'next_attempt_at', 'compat')
    extra = {name: value for name, value in data.items() if name not in known}
    return SnapshotStatus(
        kind=_as_text(data.get('kind') or 'published'), generation=_as_text(data.get('generation') or ''),
        state=_as_text(data.get('state') or core.SELECTION_PARTIAL),
        state_detail=_as_text(data.get('state_detail') or core.DETAIL_OK),
        stop_reason=_as_text(data.get('stop_reason') or 'stopped'), scope=scope, policy=policy,
        options=options,
        published_at=_as_float(data.get('published_at') or data.get('generated_at'), 0.0),
        expires_at=data.get('expires_at', data.get('valid_until')),
        counts={'exported': _as_int(data.get('exported'), 0), 'admitted': _as_int(data.get('admitted'), 0),
                'rejected': _as_int(data.get('rejected'), 0), 'checked': _as_int(data.get('checked'), 0),
                'pending': _as_int(data.get('pending'), 0), 'passed': _as_int(data.get('passed'), 0),
                'candidates': _as_int(data.get('candidates'), 0),
                'scope_candidates': _as_int(data.get('scope_candidates'), 0),
                'local_filtered': _as_int(data.get('local_filtered'), 0),
                'admission_counts': dict(data.get('admission_counts') or {})},
        selection={'requested': _as_int(data.get('selection_requested'), 0),
                   'exported': _as_int(data.get('selection_exported'), 0),
                   'missing': list(data.get('selection_missing') or ()),
                   'truncated': _as_int(data.get('selection_truncated'), 0)},
        compat=dict(data.get('compat') or {}), digest=_as_text(data.get('scope_digest') or ''),
        artifact_digest=_as_text(data.get('artifact_digest') or ''),
        deficit_reasons=tuple(data.get('deficit_reasons') or ()),
        desired=data.get('desired'), next_attempt_at=data.get('next_attempt_at'),
        legacy=bool(legacy), extra=extra)


def _as_int(value: Any, default: int) -> int:
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ExportError('E_VALIDATION_SCHEMA', f'expected an integer, got {value!r}') from None


def _as_float(value: Any, default: float) -> float:
    if value is None or isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ExportError('E_VALIDATION_SCHEMA', f'expected a number, got {value!r}') from None


def _as_text(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ''
    return str(value)


# --------------------------------------------------------------------------
# Writing and publishing
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Artifact:
    """One written generation.  Its directory is immutable from here on."""

    id: str
    kind: str
    generation: str
    directory: Path
    files: tuple[str, ...]
    manifest: Mapping[str, Mapping[str, int]]
    status: SnapshotStatus

    def as_dict(self) -> dict[str, Any]:
        return {'id': self.id, 'kind': self.kind, 'generation': self.generation,
                'directory': str(self.directory), 'files': list(self.files),
                'manifest': dict(self.manifest), 'status': self.status.as_dict()}


@dataclass(frozen=True)
class Pointer:
    """The atomic switch: which generation consumers must read."""

    generation: str
    kind: str
    schema_version: int
    state: str
    files: tuple[str, ...]
    manifest: Mapping[str, Any]
    scope_digest: str
    published_at: float | None
    expires_at: float | None
    profile_id: str | None
    profile_revision: int | None
    collection_id: str | None

    def as_dict(self) -> dict[str, Any]:
        return {'generation': self.generation, 'kind': self.kind,
                'schema_version': self.schema_version, 'state': self.state,
                'files': list(self.files), 'manifest': dict(self.manifest),
                'scope_digest': self.scope_digest, 'published_at': self.published_at,
                'expires_at': self.expires_at, 'profile_id': self.profile_id,
                'profile_revision': self.profile_revision,
                'collection_id': self.collection_id}


@dataclass(frozen=True)
class LoadedSnapshot:
    """One coherent generation, read through a pinned name.

    ``rows`` keeps expired members and ``fresh_rows`` does not, so a consumer
    chooses; the reader never empties a set because of one stale row (defect 3).
    """

    generation: str
    kind: str
    status: SnapshotStatus
    rows: tuple[Mapping[str, Any], ...]
    manifest: Mapping[str, Any]
    now: float
    client_checks: Mapping[str, str] = field(default_factory=dict)

    @property
    def fresh_rows(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(row for row in self.rows if _row_freshness(row, self.now) == 'fresh')

    @property
    def expired_rows(self) -> tuple[Mapping[str, Any], ...]:
        return tuple(row for row in self.rows if _row_freshness(row, self.now) == 'expired')

    @property
    def unknown_rows(self) -> tuple[Mapping[str, Any], ...]:
        """Rows that never recorded a deadline; they are not fresh and not stale."""
        return tuple(row for row in self.rows if _row_freshness(row, self.now) == 'unknown')

    @property
    def expired(self) -> bool:
        expires_at = self.status.expires_at
        return expires_at is not None and float(expires_at) <= self.now

    def public_rows(self) -> list[dict[str, Any]]:
        """Rows as a consumer sees them, with the age of the measurement.

        ``admission_reason`` is passed through exactly as the admission left
        it.  A row that never went through admission stays ``None`` here rather
        than being labelled admitted by the reader.
        """
        now = self.now
        result = []
        for row in self.rows:
            item = dict(row)
            item['age_seconds'] = _age(item, now)
            item['freshness'] = _row_freshness(item, now)
            item['stale'] = item['freshness'] == 'expired'
            result.append(item)
        return result


def _age(row: Mapping[str, Any], now: float) -> float | None:
    checked = row.get('checked_at')
    if isinstance(checked, bool) or not isinstance(checked, (int, float)):
        return None
    return max(0.0, now - float(checked))


def _row_freshness(row: Mapping[str, Any], now: float) -> str:
    """``fresh`` / ``expired`` / ``unknown`` for one row, as a reader may see it.

    A row without a recorded deadline is ``unknown``, not fresh: defect 1 forbids
    treating a missing TTL as an endless one, and only the admission decides
    whether such a row may be used at all.
    """
    valid_until = row.get('valid_until')
    if isinstance(valid_until, bool) or not isinstance(valid_until, (int, float)):
        return 'unknown'
    return 'expired' if float(valid_until) <= now else 'fresh'


def _atomic_write(path: Path, content: str) -> None:
    """Replace one file in one step; a reader never sees a half-written pointer."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_bytes(content.encode('utf-8'))
    os.replace(temporary, path)


def write_snapshot(directory: Any, rows: Sequence[Mapping[str, Any]], *, scope: ExportScope,
                   options: ExportOptions | None = None, kind: str = 'published',
                   policy: core.Policy | None = None, grant: SecretGrant | None = None,
                   selection: core.Selection | None = None, now: float | None = None,
                   run_state: Mapping[str, Any] | None = None,
                   extra: Mapping[str, Any] | None = None,
                   client: ResolvedClientTarget | None = None) -> Artifact:
    """Write one immutable generation and return it.  The pointer is not touched.

    Nothing here can change the active pool: that is :func:`publish`'s job, and
    only for ``kind='published'``.  A selection, a top-N slice and a search
    result all land here (defect 7).

    ``client`` is the resolved target version and client binary.  Left out, it
    is resolved from the options, the environment and ``client.json`` beside
    the snapshots, so a caller that never mentions a client still gets the
    compatibility decision recorded honestly instead of an unverified file.
    """
    if kind not in ARTIFACT_KINDS:
        raise ExportError('E_VALIDATION_FIELD', f'kind must be one of {ARTIFACT_KINDS}')
    options = options or ExportOptions()
    policy = policy or core.Policy()
    if options.wants_credentials:
        (grant or SecretGrant()).check(scope)
    root = Path(directory)
    resolved_client = client or resolve_client_target(options, directory=root)
    generations = root / 'generations'
    generations.mkdir(parents=True, exist_ok=True)
    published_at = float(now if now is not None
                         else (options.published_at if options.published_at is not None else time.time()))
    if not rows and options.empty_policy == EMPTY_ERROR:
        raise ExportError('E_STATE_NO_PROXIES',
                          tr('Задание не дало ни одной строки, а политика требует явной ошибки.',
                             'The job produced no rows and the policy asks for an explicit error.'))
    usable = list(rows)
    if options.top:
        usable = usable[:options.top]
    granted_rows = list(attach_admission(usable, selection))
    if options.wants_credentials:
        granted_rows = [_with_access_reference(row) for row in granted_rows]

    files: dict[str, str] = {'proxies.txt': render_txt(granted_rows),
                             'hostport.txt': render_hostport(granted_rows),
                             'ranked.json': render_json(granted_rows),
                             'ranked.csv': render_csv(granted_rows),
                             'proxychains.txt': render_proxychains(granted_rows),
                             'proxy.pac': render_pac(granted_rows, options),
                             'clash.yaml': render_clash(granted_rows, options)}
    files.update(render_protocol_files(granted_rows))
    singbox, singbox_report = _singbox_with_report(granted_rows, options, resolved_client.target)
    if singbox is not None:
        files['singbox.json'] = singbox
    compat = _compatibility(granted_rows, options, singbox_report, resolved_client, written=tuple(files))

    name = generation_name()
    generation = generations / name
    manifest: dict[str, dict[str, int]] = {}
    try:
        generation.mkdir()
        status = build_status(kind=kind, generation=name, scope=scope, policy=policy,
                              options=options, rows=granted_rows, selection=selection,
                              now=published_at, run_state=run_state, compat=compat, extra=extra)
        files['snapshot.txt'] = render_snapshot_txt(granted_rows, status=status.as_dict())
        for filename, content in files.items():
            target = generation / filename
            # Manifest hashes describe bytes, so write those exact bytes.
            # Text mode translates LF to CRLF on Windows and otherwise makes
            # every freshly published generation fail its own verification.
            raw = content.encode('utf-8')
            target.write_bytes(raw)
            if options.readonly:
                target.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
            manifest[filename] = {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
        # status.json carries the manifest of everything else, so a reader that
        # pinned this generation can verify it without the pointer.
        report = status.as_dict()
        report['manifest'] = manifest
        target = generation / 'status.json'
        target.write_bytes((json.dumps(report, indent=2, ensure_ascii=False, default=str) + '\n').encode('utf-8'))
        if options.readonly:
            target.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    except BaseException:
        remove_generation(generation)
        raise
    return Artifact(id=secrets.token_hex(8), kind=kind, generation=name, directory=generation,
                    files=(*files, 'status.json'), manifest=manifest, status=status)


def _with_access_reference(row: Mapping[str, Any]) -> dict[str, Any]:
    """Attach the *reference* to the access.  The value lives in the vault.

    A direct-auth URI is deliberately not built: the file says where the
    credential is, not what it is, so a copy of the artifact is not a copy of
    the secret (F28, §5.2).
    """
    item = dict(row)
    access_id = item.get('access_id')
    if access_id:
        item['access_ref'] = f'access:{access_id}@{int(item.get("access_revision") or 1)}'
        item['credentials_state'] = 'reference'
        item['direct_auth_uri'] = None
    return item


def _singbox_with_report(rows: Sequence[Mapping[str, Any]], options: ExportOptions,
                         target: SingBoxTarget) -> tuple[str | None, CompatReport]:
    """Render the file and say, in the report, how far it was checked.

    Returns ``(None, report)`` when the only thing left to write is the
    version-dependent reject and no version was established: the artifact then
    carries every other file, the sing-box entry is marked as not written, and
    the reason plus the action are in ``status.json`` where a GUI or an API can
    show them.  That is a fail-closed generation error, not a silent choice of
    a deprecated shape (defect 20).
    """
    if not target.supported:
        raise ExportError(target.reason or 'E_EXPORT_TARGET_UNVERIFIED', target=target.version,
                          verified=_version_text(SINGBOX_MAX_VERIFIED),
                          minimum=_version_text(SINGBOX_MIN_VERIFIED))
    report = compat_report(rows, 'singbox', options=options, target=target.version,
                           target_state=target.state)
    if not report.supported and not target.version_dependent_reject_available:
        return None, replace(report,
                             reasons=('E_EXPORT_TARGET_UNPINNED',),
                             warnings=tuple(report.warnings) + (_reason_text('E_EXPORT_TARGET_UNPINNED'),),
                             state_detail=core.DETAIL_NO_MATCH)
    text = render_singbox(rows, options=options, target=target)
    if options.client_binary:
        result, detail = client_check(text, target, binary=options.client_binary)
        report = replace(report, client_check=result, client_detail=detail)
        if result == 'failed':
            raise ExportError('E_EXPORT_CLIENT_REJECTED', target=target.version, detail=detail)
    elif not target.configured:
        report = replace(report, warnings=tuple(report.warnings)
                         + (_reason_text('E_EXPORT_TARGET_LEGACY_OPTIN' if target.legacy_optin
                                         else 'E_EXPORT_TARGET_UNPINNED'),))
    return text, report


def _compatibility(rows: Sequence[Mapping[str, Any]], options: ExportOptions,
                   singbox_report: CompatReport, client: ResolvedClientTarget,
                   written: Sequence[str] = ()) -> dict[str, Any]:
    """Per-file preview plus the rows no file could carry, with reasons."""
    files: dict[str, Any] = {}
    unsupported: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    warnings: list[str] = []
    per_file: Sequence[tuple[str, str]] = (('proxies.txt', 'txt'), ('snapshot.txt', 'txt'),
                                           ('hostport.txt', 'txt'), ('ranked.json', 'json'),
                                           ('ranked.csv', 'csv'), ('protocol files', 'protocol'),
                                           ('proxychains.txt', 'proxychains'), ('proxy.pac', 'pac'),
                                           ('clash.yaml', 'clash'), ('singbox.json', 'singbox'))
    for name, fmt in per_file:
        report = singbox_report if fmt == 'singbox' else compat_report(rows, fmt, options=options)
        fail_closed = not report.supported and bool(rows)
        summary = report.as_dict(unsupported_limit=options.unsupported_limit)
        summary['fail_closed'] = fail_closed or summary['fail_closed']
        summary['written'] = bool(written) is False or name in written
        summary['state_detail'] = core.DETAIL_NO_MATCH if fail_closed else summary['state_detail']
        files[name] = summary
        warnings.extend(report.warnings)
        for item in report.unsupported:
            key = (fmt, item.proxy)
            if key in seen:
                continue
            seen.add(key)
            unsupported.append(item.as_dict())
    return {'files': files,
            'unsupported': unsupported[:options.unsupported_limit],
            'unsupported_total': len(unsupported),
            'warnings': sorted(set(warnings)),
            'credentials': options.credentials,
            'client_target': client.target.version,
            'client_target_state': client.target.state,
            'client_target_source': client.source,
            'client_binary': client.binary,
            'client_check': singbox_report.client_check,
            'client_target_required': options.client_target is None and client.source == 'unconfigured'}


def publish(artifact: Artifact, directory: Any, *, confirm: bool = False,
            pointer_name: str = POINTER_NAME) -> Pointer:
    """Switch the active pointer.  The only act that changes the active pool.

    ``kind='selection'`` is refused outright: exporting what the user
    highlighted must never repoint an already connected client (defect 7,
    R04).  ``kind='diagnostic'`` may only reach the diagnostic pointer, never
    the active one.  The confirmation is required because a running client
    cannot be moved back to the previous generation by this call.
    """
    if artifact.kind == 'selection':
        raise ExportError('E_VALIDATION_FIELD',
                          tr('Выделенная выборка публикуется только как отдельный артефакт.',
                             'A selected slice is published only as its own artifact.'))
    if artifact.kind == 'diagnostic' and pointer_name != DIAGNOSTIC_POINTER_NAME:
        raise ExportError('E_VALIDATION_FIELD',
                          tr('Диагностический снимок не переключает активный указатель.',
                             'A diagnostic snapshot never switches the active pointer.'))
    if artifact.kind not in ARTIFACT_KINDS:
        raise ExportError('E_VALIDATION_FIELD', f'kind must be one of {ARTIFACT_KINDS}')
    if not confirm:
        raise ExportError('E_STATE_PUBLISH_UNCONFIRMED')
    root = Path(directory)
    pointer = Pointer(generation=artifact.generation, kind=artifact.kind,
                      schema_version=SNAPSHOT_SCHEMA_VERSION, state=artifact.status.state,
                      files=artifact.files, manifest=dict(artifact.manifest),
                      scope_digest=artifact.status.digest, published_at=artifact.status.published_at,
                      expires_at=artifact.status.expires_at,
                      profile_id=artifact.status.scope.identity.profile_id,
                      profile_revision=artifact.status.scope.identity.profile_revision,
                      collection_id=artifact.status.scope.identity.collection_id)
    _atomic_write(root / pointer_name, json.dumps(pointer.as_dict(), ensure_ascii=False, default=str) + '\n')
    prune_generations(root, keep=artifact.status.options.keep_generations, current=artifact.generation)
    return pointer


def remove_generation(path: Any) -> None:
    """Delete a generation directory, clearing the read-only bit it was given."""
    directory = Path(path)
    if not directory.is_dir():
        return
    for item in directory.iterdir():
        try:
            item.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
    shutil.rmtree(directory, ignore_errors=True)


def prune_generations(directory: Any, *, keep: int = 3, current: str | None = None) -> tuple[list[str], list[str]]:
    """Drop old generations, newest first, never the current one."""
    root = Path(directory) / 'generations'
    if not root.is_dir():
        return [], []
    entries = sorted((item for item in root.iterdir()
                      if item.is_dir() and valid_generation_name(item.name)),
                     key=lambda item: item.stat().st_mtime, reverse=True)
    if current is None:
        pointer = read_pointer(root.parent)
        current = pointer.generation if pointer else None
    others = [item for item in entries if item.name != current]
    keep_others = max(0, int(keep) - (1 if current else 0))
    removed, failed = [], []
    for item in others[keep_others:]:
        try:
            remove_generation(item)
            removed.append(item.name)
        except OSError:
            failed.append(item.name)
    return removed, failed


def read_pointer(directory: Any, pointer_name: str = POINTER_NAME) -> Pointer | None:
    """Read one pointer.  A broken name is not a path, so it is not followed."""
    path = Path(directory) / pointer_name
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, ValueError):
        return None
    if not isinstance(data, Mapping) or not valid_generation_name(data.get('generation')):
        return None
    return Pointer(generation=str(data['generation']), kind=str(data.get('kind') or 'published'),
                   schema_version=int(data.get('schema_version') or 1), state=str(data.get('state') or ''),
                   files=tuple(data.get('files') or ()), manifest=dict(data.get('manifest') or {}),
                   scope_digest=str(data.get('scope_digest') or ''), published_at=data.get('published_at'),
                   expires_at=data.get('expires_at'), profile_id=data.get('profile_id'),
                   profile_revision=data.get('profile_revision'),
                   collection_id=data.get('collection_id'))


def load_snapshot(directory: Any, generation: str | None = None, *, verify: bool = True,
                  now: float | None = None) -> LoadedSnapshot:
    """Read one generation, by name or by pointer, and refuse what cannot be trusted.

    A consumer passes the generation it bound to and keeps reading that one: a
    later publication does not move it (defect 8, §1.2(3)).  ``schema_version``,
    the declared generation and the manifest checksums are all checked; a
    mismatch is a refusal, not an empty list.
    """
    root = Path(directory)
    name = generation
    if name is None:
        pointer = read_pointer(root)
        if pointer is None:
            raise ExportError('E_STATE_NO_SNAPSHOT',
                              tr('Указатель активного поколения отсутствует или повреждён.',
                                 'The active generation pointer is missing or broken.'))
        name = pointer.generation
    if not valid_generation_name(name):
        raise ExportError('E_VALIDATION_FIELD', f'invalid generation name: {name!r}')
    directory_path = root / 'generations' / name
    try:
        status_raw = (directory_path / 'status.json').read_text(encoding='utf-8')
        rows_raw = (directory_path / 'ranked.json').read_text(encoding='utf-8')
    except OSError as exc:
        raise ExportError('E_STATE_NO_SNAPSHOT', f'generation {name} is unreadable: {exc}') from None
    status = status_from_dict(json.loads(status_raw))
    if status.generation and status.generation != name:
        raise ExportError('E_STATE_SNAPSHOT_MIXED', pointer=name, generation=name,
                          declared=status.generation)
    rows = json.loads(rows_raw)
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        raise ExportError('E_VALIDATION_SCHEMA', f'ranked.json of {name} is not a row list')
    manifest = status.extra.get('manifest') or {}
    if verify:
        _verify_manifest(directory_path, name, manifest)
    moment = float(now if now is not None else time.time())
    return LoadedSnapshot(generation=name, kind=status.kind, status=status,
                          rows=tuple(dict(row) for row in rows), manifest=manifest,
                          now=moment, client_checks=dict(status.compat.get('client_checks') or {}))


def _verify_manifest(directory: Path, generation: str, manifest: Mapping[str, Any]) -> None:
    for name, entry in manifest.items():
        described = entry.get('sha256') if isinstance(entry, Mapping) else None
        if not described:
            continue
        try:
            raw = (directory / name).read_bytes()
        except OSError as exc:
            raise ExportError('E_STATE_SNAPSHOT_MANIFEST', name=name, generation=generation) from exc
        if hashlib.sha256(raw).hexdigest() != described:
            raise ExportError('E_STATE_SNAPSHOT_MANIFEST', name=name, generation=generation)


# --------------------------------------------------------------------------
# export_artifact (migration 11)
# --------------------------------------------------------------------------

ARTIFACT_COLUMNS = ('id', 'kind', 'collection_id', 'profile_id', 'profile_revision', 'generation',
                    'published_at', 'expires_at', 'state', 'reason_code', 'manifest_json')


def record_artifact(db: sqlite3.Connection, artifact: Artifact) -> str:
    """Store what the artifact is, so a later download serves the same selection.

    The table belongs to ``db.migrate()`` (migration 11).  This module writes no
    DDL: a missing or reshaped table is reported, never created or patched.
    """
    columns = {row[1] for row in db.execute('PRAGMA table_info(export_artifact)')}
    if not columns:
        raise ExportError('E_DATA_MIGRATION_FAILED', detail='table export_artifact does not exist')
    missing = [name for name in ARTIFACT_COLUMNS if name not in columns]
    if missing:
        raise ExportError('E_DATA_MIGRATION_FAILED', detail=f'missing columns: {missing}')
    status = artifact.status
    report = status.as_dict()
    db.execute(
        'INSERT INTO export_artifact (id, kind, collection_id, profile_id, profile_revision, generation, '
        'published_at, expires_at, state, reason_code, manifest_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
        (artifact.id, artifact.kind, status.scope.identity.collection_id or None,
         status.scope.identity.profile_id, status.scope.identity.profile_revision,
         artifact.generation, status.published_at, status.expires_at, status.state,
         report.get('state_detail'), json.dumps(dict(artifact.manifest), default=str)))
    return artifact.id
