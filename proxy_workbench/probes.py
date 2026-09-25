"""Honest check modes, measurement arithmetic and probe parameter validation.

This module is a leaf.  It owns no database, no source fetching and no network
code of its own: every socket call goes through an injected ``Transport`` and
every duration through an injected ``Clock``.  ``proxytool.py`` stays the single
network entry point (CONTRACTS.ru.md §1.2, HANDOFF §2.2), and the integration
wires its own transport to :func:`run_plan` / :func:`run_speed_test`.

Scope: F01 (check modes), F07 (advanced parameters), F20 (measurements) and
defects 13 (judge validation before ``elite``), 14 (DNSBL reverse and outcomes)
and 15 (speed window and insufficient sample).

Two rules run through the whole module:

* an endpoint is only a working proxy when a *transfer* happened, so the mode
  declares the strongest evidence it can ever produce and nothing is promoted
  above it (F01 acceptance: a TCP-only endpoint is not a transferring proxy);
* a measurement that did not complete stays ``unknown``/``insufficient`` and
  never produces a number, a level or a clean verdict.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import math
import re
import ssl
import time
from dataclasses import dataclass, replace
from urllib.parse import urljoin, urlsplit

__all__ = [
    # errors
    'ProbeError', 'E_VALIDATION_SCHEMA', 'E_VALIDATION_FIELD', 'E_VALIDATION_UNKNOWN_FIELD',
    'E_VALIDATION_ANONYMITY_REQUIRED', 'E_VALIDATION_SCOPE', 'E_VALIDATION_TARGET_UNSAFE',
    'E_VALIDATION_RETRY_UNSAFE', 'E_LIMIT_BUDGET',
    'MEASUREMENT_CODES', 'STAGES', 'TARGET_FAILURE_CODES',
    # modes
    'MODES', 'EVIDENCE', 'Mode', 'resolve_mode', 'evidence_for', 'is_working', 'evidence_at_least',
    # options and targets
    'Limit', 'LIMITS', 'ProbeOptions', 'validate_options', 'TimeBudget', 'plan_attempts',
    'SecretRef', 'TargetProfile', 'validate_target', 'MAX_TARGETS', 'SAFE_HEADERS',
    'DNS_MODES', 'build_ssl_context', 'SpeedTarget', 'validate_speed_target',
    # basic probes
    'BASIC_PROBES', 'basic_profiles', 'fallback_chain', 'MAX_BASIC_PROBES', 'probes_manifest',
    'ProbePlan', 'build_plan', 'plan_digest',
    # execution
    'Clock', 'SystemClock', 'ProbeRequest', 'ProbeResponse', 'TargetOutcome', 'PlanOutcome',
    'run_probe', 'run_stage', 'run_plan', 'check_targets', 'BasicSummary', 'summarize_basic',
    'target_failure_signal', 'evaluate_response', 'check_json_assertions', 'plan_digest',
    # speed
    'SpeedLimits', 'SPEED_LIMITS', 'TransferTrace', 'SpeedMeasurement', 'measure_speed',
    'run_speed_test',
    # anonymity
    'JudgeSpec', 'validate_judge_spec', 'AnonymityOutcome', 'classify_echo', 'require_anonymity',
    'validate_min_level', 'anonymity_allows', 'ANONYMITY_LEVELS', 'extract_addresses',
    # dnsbl
    'reverse_ip', 'DnsblZone', 'DnsAnswer', 'ZoneOutcome', 'DnsblReport', 'classify_dnsbl',
    'check_dnsbl', 'dnsbl_blocks', 'generic_zone', 'validate_dnsbl_zones', 'DnsQueryError', 'DNSBL_STATUSES',
    'reverse_host', 'reveal_signals', 'looks_like_challenge', 'labelled_exit_address', 'STAGES',
    # recheck
    'RecheckItem', 'plan_recheck',
    # capability matrix and the self-hosted reference probe (F20)
    'CAPABILITIES', 'capability_matrix', 'serve_reference_probe',
    'REFERENCE_PROBE_MAX_BYTES', 'reference_probe_targets', 'time_budget',
]


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------

# Configuration errors carry the E_* form of CONTRACTS.ru.md §5.4: the
# VALIDATION domain already exists, so no new domain is introduced here.
E_VALIDATION_SCHEMA = 'E_VALIDATION_SCHEMA'
E_VALIDATION_FIELD = 'E_VALIDATION_FIELD'
E_VALIDATION_UNKNOWN_FIELD = 'E_VALIDATION_UNKNOWN_FIELD'
E_VALIDATION_ANONYMITY_REQUIRED = 'E_VALIDATION_ANONYMITY_REQUIRED'
E_VALIDATION_SCOPE = 'E_VALIDATION_SCOPE'
E_VALIDATION_TARGET_UNSAFE = 'E_VALIDATION_TARGET_UNSAFE'
E_VALIDATION_RETRY_UNSAFE = 'E_VALIDATION_RETRY_UNSAFE'
E_LIMIT_BUDGET = 'E_LIMIT_BUDGET'

# Measurement codes stay bare, exactly like the ones already stored in
# ``results.payload.error`` (CONTRACTS §5.4, row UPSTREAM).  This catalogue is
# the reference the diagnostics surface needs for the probe half of it.
UNREACHABLE = 'UNREACHABLE'
CONNECT_TIMEOUT = 'CONNECT_TIMEOUT'
HANDSHAKE_TIMEOUT = 'HANDSHAKE_TIMEOUT'
HANDSHAKE_PROTOCOL = 'HANDSHAKE_PROTOCOL'
READ_TIMEOUT = 'READ_TIMEOUT'
WHOLE_PROBE_TIMEOUT = 'WHOLE_PROBE_TIMEOUT'
DNS_ERROR = 'DNS_ERROR'
INVALID_PROXY = 'INVALID_PROXY'
BODY_TOO_LARGE = 'BODY_TOO_LARGE'
BODY_TOO_SMALL = 'BODY_TOO_SMALL'
CONTENT_MISMATCH = 'CONTENT_MISMATCH'
HASH_MISMATCH = 'HASH_MISMATCH'
CONTENT_TYPE = 'CONTENT_TYPE'
JSON_ASSERT = 'JSON_ASSERT'
REDIRECT_LOOP = 'REDIRECT_LOOP'
REDIRECT_TOO_MANY = 'REDIRECT_TOO_MANY'
JUDGE_INVALID = 'JUDGE_INVALID'
JUDGE_CHALLENGE = 'JUDGE_CHALLENGE'
JUDGE_UNVERIFIED = 'JUDGE_UNVERIFIED'
NO_DNSBL_ZONES = 'NO_DNSBL_ZONES'
DNSBL_TIMEOUT = 'DNSBL_TIMEOUT'
DNSBL_ERROR = 'DNSBL_ERROR'
DNSBL_QUOTA = 'DNSBL_QUOTA'
DNSBL_ACCESS = 'DNSBL_ACCESS'
INSUFFICIENT_SAMPLE = 'INSUFFICIENT_SAMPLE'
BUDGET_EXHAUSTED = 'BUDGET_EXHAUSTED'
TARGET_UNAVAILABLE = 'TARGET_UNAVAILABLE'

MEASUREMENT_CODES = frozenset({
    UNREACHABLE, CONNECT_TIMEOUT, HANDSHAKE_TIMEOUT, HANDSHAKE_PROTOCOL, READ_TIMEOUT,
    WHOLE_PROBE_TIMEOUT, DNS_ERROR, INVALID_PROXY, BODY_TOO_LARGE, BODY_TOO_SMALL,
    CONTENT_MISMATCH, HASH_MISMATCH, CONTENT_TYPE, JSON_ASSERT, REDIRECT_LOOP,
    REDIRECT_TOO_MANY, JUDGE_INVALID, JUDGE_CHALLENGE, JUDGE_UNVERIFIED, NO_DNSBL_ZONES,
    DNSBL_TIMEOUT, DNSBL_ERROR, DNSBL_QUOTA, DNSBL_ACCESS, INSUFFICIENT_SAMPLE,
    BUDGET_EXHAUSTED, TARGET_UNAVAILABLE,
})

# Stages are per F10 ("разделять сеть, DNS, TCP, handshake, TLS, ответ цели…").
STAGES = ('tcp', 'handshake', 'target', 'assert', 'judge', 'dnsbl', 'speed')

# Transport-level failures may be retried.  Assertion failures are
# deterministic, so repeating them only costs the target's budget.
RETRYABLE_CODES = frozenset({
    UNREACHABLE, CONNECT_TIMEOUT, HANDSHAKE_TIMEOUT, READ_TIMEOUT, DNS_ERROR, WHOLE_PROBE_TIMEOUT,
})

# Codes that mean "the target answered, and the answer was not what we asked
# for" or "the target itself could not answer".  Failures at the target stage
# are evidence about the target, not about the proxy.
TARGET_FAILURE_CODES = frozenset({
    TARGET_UNAVAILABLE, DNS_ERROR, INVALID_PROXY, BODY_TOO_LARGE, BODY_TOO_SMALL,
    CONTENT_MISMATCH, HASH_MISMATCH, CONTENT_TYPE, JSON_ASSERT, REDIRECT_LOOP,
    REDIRECT_TOO_MANY, READ_TIMEOUT,
})


class ProbeError(ValueError):
    """Configuration failure with a stable code and an actionable message."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self):
        return f'{self.code}: {self.message}'


# --------------------------------------------------------------------------
# check modes and the evidence ladder
# --------------------------------------------------------------------------

COLLECT_ONLY = 'collect_only'
TCP = 'tcp'
HANDSHAKE = 'handshake'
BASIC = 'basic'
SERVICES = 'services'
CUSTOM = 'custom'
RECHECK = 'recheck'
MONITOR = 'monitor'

# Strongest evidence first.  Nothing may claim a level above the one its mode
# can measure (F01: "TCP-only endpoint не считается передающим HTTP proxy").
EVIDENCE = ('none', 'collected', 'tcp_open', 'handshake_ok', 'transfer_ok')
_EVIDENCE_RANK = {name: index for index, name in enumerate(EVIDENCE)}

MODES = (COLLECT_ONLY, TCP, HANDSHAKE, BASIC, SERVICES, CUSTOM, RECHECK, MONITOR)

_ALIASES = {
    'collect': COLLECT_ONLY, 'collect_only': COLLECT_ONLY, 'collect-only': COLLECT_ONLY,
    'only_collect': COLLECT_ONLY, 'только_собрать': COLLECT_ONLY,
    'tcp': TCP, 'reachable': TCP, 'reachability': TCP, 'tcp_open': TCP, 'доступность': TCP,
    'handshake': HANDSHAKE, 'protocol': HANDSHAKE, 'protocol_handshake': HANDSHAKE, 'протокол': HANDSHAKE,
    'basic': BASIC, 'https': BASIC, 'basic_transfer': BASIC, 'базовый': BASIC,
    'services': SERVICES, 'service': SERVICES, 'наборы': SERVICES,
    'custom': CUSTOM, 'profile': CUSTOM, 'custom_profile': CUSTOM, 'свой_профиль': CUSTOM,
    'recheck': RECHECK, 're-check': RECHECK, 'recheck_passing': RECHECK, 'перепроверка': RECHECK,
    'monitor': MONITOR, 'monitoring': MONITOR, 'watch': MONITOR, 'pool': MONITOR, 'мониторинг': MONITOR,
}

_BASE_MODES = (COLLECT_ONLY, TCP, HANDSHAKE, BASIC, SERVICES, CUSTOM)
_MEASURING_MODES = (TCP, HANDSHAKE, BASIC, SERVICES, CUSTOM)


@dataclass(frozen=True)
class Mode:
    """One user-facing check mode and the strongest claim it can support."""

    name: str
    evidence: str
    network: bool
    needs_targets: bool
    checks_transfer: bool
    monitoring: bool = False
    base: str | None = None
    description: str = ''

    @property
    def collects_only(self) -> bool:
        return self.name == COLLECT_ONLY

    @property
    def is_measurement(self) -> bool:
        return self.name in _MEASURING_MODES

    def to_public(self) -> dict:
        return {'name': self.name, 'evidence': self.evidence, 'network': self.network,
                'needs_targets': self.needs_targets, 'checks_transfer': self.checks_transfer,
                'monitoring': self.monitoring, 'base': self.base, 'description': self.description}


_MODE_SPECS = {
    COLLECT_ONLY: dict(evidence='collected', network=False, needs_targets=False, checks_transfer=False,
                       description='Собрать адреса без сетевых проверок: результат не доказывает работоспособность.'),
    TCP: dict(evidence='tcp_open', network=True, needs_targets=False, checks_transfer=False,
              description='TCP-доступность адреса: доказывает, что порт принимает соединение, но не передачу.'),
    HANDSHAKE: dict(evidence='handshake_ok', network=True, needs_targets=False, checks_transfer=False,
                    description='Рукопожатие протокола прокси (HTTP CONNECT или SOCKS): прокси говорит на языке протокола.'),
    BASIC: dict(evidence='transfer_ok', network=True, needs_targets=True, checks_transfer=True,
                description='Базовая передача HTTP/HTTPS через встроенные небольшие probes без ввода URL.'),
    SERVICES: dict(evidence='transfer_ok', network=True, needs_targets=True, checks_transfer=True,
                   description='Проверка выбранных наборов сервисов по определениям каталога.'),
    CUSTOM: dict(evidence='transfer_ok', network=True, needs_targets=True, checks_transfer=True,
                 description='Собственный target-профиль с явными целями и параметрами.'),
}


def resolve_mode(name, *, base=None, monitoring=False) -> Mode:
    """Normalize a mode name, including the two derived modes.

    ``recheck`` re-measures with the mode the stored profile used and
    ``monitor`` is a job/pool mode that re-runs another mode.  Neither one adds
    a verdict of its own: the evidence level is exactly the base mode's, and
    ``monitoring`` is only a flag the scheduler and pools read.
    """
    if not isinstance(name, str) or not name.strip():
        raise ProbeError(E_VALIDATION_FIELD, 'Режим проверки: нужна строка.')
    key = _ALIASES.get(name.strip().lower().replace(' ', '_'))
    if key is None:
        raise ProbeError(E_VALIDATION_FIELD,
                         f'Неизвестный режим проверки: {name}. Доступно: {", ".join(MODES)}.')
    if key == RECHECK:
        base_key = _ALIASES.get(str(base or BASIC).strip().lower().replace(' ', '_'), BASIC)
        if base_key not in _MEASURING_MODES:
            raise ProbeError(E_VALIDATION_FIELD,
                             'Повторная проверка измеряет, а не собирает: нужен базовый режим из '
                             f'{", ".join(_MEASURING_MODES)}.')
        return Mode(name=RECHECK, base=base_key, monitoring=False, **_MODE_SPECS[base_key])
    if key == MONITOR:
        if not monitoring:
            # ``monitor`` without a base is the pool controller itself: it
            # re-runs the job's own mode, never a new kind of verdict.
            monitoring = True
        base_key = _ALIASES.get(str(base or '').strip().lower().replace(' ', '_'))
        if base_key not in _MEASURING_MODES:
            raise ProbeError(E_VALIDATION_FIELD,
                             'Мониторинг — режим задания или пула: укажите базовый режим из '
                             f'{", ".join(_MEASURING_MODES)}, иначе нечем измерять.')
        spec = _MODE_SPECS[base_key]
        return Mode(name=base_key, base=base_key, monitoring=True, **spec)
    if base not in (None, '', key):
        raise ProbeError(E_VALIDATION_FIELD, f'Режим {key} не принимает базовый режим.')
    if monitoring:
        raise ProbeError(E_VALIDATION_FIELD, 'Мониторинг задаётся режимом monitor, а не флагом у другого режима.')
    return Mode(name=key, base=key, monitoring=False, **_MODE_SPECS[key])


def evidence_at_least(evidence, minimum) -> bool:
    return _EVIDENCE_RANK.get(evidence, -1) >= _EVIDENCE_RANK.get(minimum, 0)


def evidence_for(outcome) -> str:
    """Evidence a finished probe actually earned, never more than its mode allows.

    A TCP or handshake mode can only ever reach its own level, and a transfer
    mode reaches ``transfer_ok`` only when a target actually answered through
    the proxy.
    """
    if not getattr(outcome, 'ok', False):
        return 'none'
    spec = _MODE_SPECS.get(getattr(outcome, 'mode', None))
    if spec is None:
        return 'none'
    if not spec['checks_transfer']:
        return spec['evidence']
    return 'transfer_ok' if any(item.ok for item in (getattr(outcome, 'targets', ()) or ())) else 'none'


def is_working(outcome) -> bool:
    """True only for a measurement that carried a payload through the proxy."""
    return bool(getattr(outcome, 'ok', False)) and evidence_for(outcome) == 'transfer_ok'


# --------------------------------------------------------------------------
# parameters with units, limits and one validation for every client
# --------------------------------------------------------------------------

DNS_MODES = ('proxy', 'local')
SCOPES = ('public', 'trusted')
METHODS = ('GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE')
IDEMPOTENT_METHODS = ('GET', 'HEAD')
MAX_TARGETS = 20
MAX_BASIC_PROBES = 3
MAX_REDIRECTS = 5


@dataclass(frozen=True)
class Limit:
    """One parameter's unit and range, in a single place (CONTRACTS §5.5)."""

    unit: str
    minimum: float
    maximum: float
    kind: str = 'float'

    def describe(self) -> str:
        return f'{self.minimum:g}..{self.maximum:g} {self.unit}'.strip()


LIMITS = {
    'connect_timeout_s': Limit('с', 0.1, 30.0),
    'handshake_timeout_s': Limit('с', 0.1, 30.0),
    'read_timeout_s': Limit('с', 0.1, 120.0),
    'whole_probe_timeout_s': Limit('с', 0.5, 600.0),
    'attempts': Limit('попыток', 1, 10, 'int'),
    'backoff_s': Limit('с', 0.0, 30.0),
    'backoff_factor': Limit('×', 1.0, 5.0),
    'backoff_max_s': Limit('с', 0.0, 60.0),
    'max_body_bytes': Limit('байт', 1, 8 * 1024 * 1024, 'int'),
    'max_redirects': Limit('переходов', 0, MAX_REDIRECTS, 'int'),
    'judge_max_bytes': Limit('байт', 1024, 1024 * 1024, 'int'),
    'speed_max_bytes': Limit('байт', 100_000, 200_000_000, 'int'),
}

DEFAULT_OPTIONS = {
    'connect_timeout_s': 4.0,
    'handshake_timeout_s': 6.0,
    'read_timeout_s': 8.0,
    'whole_probe_timeout_s': 30.0,
    'attempts': 2,
    'backoff_s': 0.5,
    'backoff_factor': 2.0,
    'backoff_max_s': 8.0,
    'max_body_bytes': 262_144,
    'max_redirects': 0,
}


def _number(value, name, limit, *, integer=False, default=None, required=False):
    if value is None:
        if required or default is None:
            raise ProbeError(E_VALIDATION_FIELD, f'{name}: значение обязательно.')
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProbeError(E_VALIDATION_FIELD, f'{name}: ожидается число.')
    if isinstance(value, float) and not math.isfinite(value):
        raise ProbeError(E_VALIDATION_FIELD, f'{name}: значение должно быть конечным.')
    if integer and int(value) != value:
        raise ProbeError(E_VALIDATION_FIELD, f'{name}: ожидается целое число.')
    value = int(value) if integer else float(value)
    if not limit.minimum <= value <= limit.maximum:
        raise ProbeError(E_VALIDATION_FIELD, f'{name}: допустимо {limit.describe()}.')
    return value


def _choice(value, name, allowed, default=None, *, required=False):
    if value is None:
        if required:
            raise ProbeError(E_VALIDATION_FIELD, f'{name}: значение обязательно.')
        return default
    if not isinstance(value, str) or value not in allowed:
        raise ProbeError(E_VALIDATION_FIELD, f'{name}: допустимо {", ".join(allowed)}.')
    return value


def _reject_unknown(data, allowed, name):
    unknown = sorted(set(data) - set(allowed))
    if unknown:
        raise ProbeError(E_VALIDATION_UNKNOWN_FIELD,
                         f'{name}: неизвестные параметры {", ".join(unknown)}. Молча игнорировать их нельзя.')


@dataclass(frozen=True)
class ProbeOptions:
    """Connection, timeout and retry policy shared by every mode."""

    connect_timeout_s: float = DEFAULT_OPTIONS['connect_timeout_s']
    handshake_timeout_s: float = DEFAULT_OPTIONS['handshake_timeout_s']
    read_timeout_s: float = DEFAULT_OPTIONS['read_timeout_s']
    whole_probe_timeout_s: float = DEFAULT_OPTIONS['whole_probe_timeout_s']
    attempts: int = DEFAULT_OPTIONS['attempts']
    backoff_s: float = DEFAULT_OPTIONS['backoff_s']
    backoff_factor: float = DEFAULT_OPTIONS['backoff_factor']
    backoff_max_s: float = DEFAULT_OPTIONS['backoff_max_s']
    max_body_bytes: int = DEFAULT_OPTIONS['max_body_bytes']
    max_redirects: int = DEFAULT_OPTIONS['max_redirects']

    def to_public(self) -> dict:
        return {name: getattr(self, name) for name in DEFAULT_OPTIONS}

    def describe(self) -> dict:
        return {name: LIMITS[name].describe() for name in DEFAULT_OPTIONS}


def validate_options(data=None) -> ProbeOptions:
    """The single validation GUI, CLI and API all go through (F07, F18)."""
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ProbeError(E_VALIDATION_SCHEMA, 'Параметры проверки: ожидается объект.')
    _reject_unknown(data, DEFAULT_OPTIONS, 'Параметры проверки')
    values = {}
    values['connect_timeout_s'] = _number(data.get('connect_timeout_s'), 'connect_timeout_s',
                                          LIMITS['connect_timeout_s'], default=DEFAULT_OPTIONS['connect_timeout_s'])
    values['handshake_timeout_s'] = _number(data.get('handshake_timeout_s'), 'handshake_timeout_s',
                                            LIMITS['handshake_timeout_s'], default=DEFAULT_OPTIONS['handshake_timeout_s'])
    values['read_timeout_s'] = _number(data.get('read_timeout_s'), 'read_timeout_s', LIMITS['read_timeout_s'],
                                       default=DEFAULT_OPTIONS['read_timeout_s'])
    values['whole_probe_timeout_s'] = _number(data.get('whole_probe_timeout_s'), 'whole_probe_timeout_s',
                                              LIMITS['whole_probe_timeout_s'],
                                              default=DEFAULT_OPTIONS['whole_probe_timeout_s'])
    values['attempts'] = _number(data.get('attempts'), 'attempts', LIMITS['attempts'], integer=True,
                                 default=DEFAULT_OPTIONS['attempts'])
    values['backoff_s'] = _number(data.get('backoff_s'), 'backoff_s', LIMITS['backoff_s'],
                                  default=DEFAULT_OPTIONS['backoff_s'])
    values['backoff_factor'] = _number(data.get('backoff_factor'), 'backoff_factor', LIMITS['backoff_factor'],
                                       default=DEFAULT_OPTIONS['backoff_factor'])
    values['backoff_max_s'] = _number(data.get('backoff_max_s'), 'backoff_max_s', LIMITS['backoff_max_s'],
                                      default=DEFAULT_OPTIONS['backoff_max_s'])
    values['max_body_bytes'] = _number(data.get('max_body_bytes'), 'max_body_bytes', LIMITS['max_body_bytes'],
                                       integer=True, default=DEFAULT_OPTIONS['max_body_bytes'])
    values['max_redirects'] = _number(data.get('max_redirects'), 'max_redirects', LIMITS['max_redirects'],
                                      integer=True, default=DEFAULT_OPTIONS['max_redirects'])
    options = ProbeOptions(**values)
    time_budget(options)
    return options


@dataclass(frozen=True)
class TimeBudget:
    """The four timeouts of a probe, already checked against each other."""

    connect_s: float
    handshake_s: float
    read_s: float
    whole_s: float

    @property
    def worst_case_s(self) -> float:
        return self.connect_s + self.handshake_s + self.read_s

    def to_public(self) -> dict:
        return {'connect_timeout_s': self.connect_s, 'handshake_timeout_s': self.handshake_s,
                'read_timeout_s': self.read_s, 'whole_probe_timeout_s': self.whole_s,
                'worst_case_s': round(self.worst_case_s, 3)}


def time_budget(options: ProbeOptions) -> TimeBudget:
    """A budget whose worst case still fits the whole-probe limit, or an error."""
    budget = TimeBudget(options.connect_timeout_s, options.handshake_timeout_s,
                        options.read_timeout_s, options.whole_probe_timeout_s)
    if budget.worst_case_s > options.whole_probe_timeout_s:
        raise ProbeError(E_VALIDATION_FIELD,
                         'whole_probe_timeout_s меньше connect+handshake+read: общий срок пробы '
                         f'({options.whole_probe_timeout_s:g} с) короче худшего случая ({budget.worst_case_s:g} с). '
                         'Увеличьте общий срок или уменьшите частные таймауты.')
    return budget


def plan_attempts(options: ProbeOptions) -> tuple[float, ...]:
    """Delays before attempts 2..N, geometric and capped (F07 "attempts/backoff")."""
    delay = float(options.backoff_s)
    waits = []
    for index in range(max(0, options.attempts - 1)):
        waits.append(round(min(delay, float(options.backoff_max_s)), 3))
        delay *= float(options.backoff_factor)
    return tuple(waits)


# --------------------------------------------------------------------------
# targets
# --------------------------------------------------------------------------

# Credential-bearing headers are refused for public probes.  POST/body/API auth
# is only allowed in an explicitly configured own target profile and only as a
# reference to a stored secret, never as a literal value.
SAFE_HEADERS = frozenset({'accept', 'accept-encoding', 'accept-language', 'cache-control', 'pragma',
                          'user-agent', 'x-client-version', 'x-request-id', 'range', 'if-none-match'})
SECRET_HEADERS = frozenset({'authorization', 'proxy-authorization', 'cookie', 'set-cookie', 'x-api-key',
                            'x-auth-token', 'api-key'})
JSON_OPS = ('exists', 'equals', 'not_equals', 'type', 'contains', 'min', 'max')
JSON_TYPES = ('object', 'array', 'string', 'number', 'boolean', 'null')
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
# RFC 9110 media-type, optionally with parameters ("application/json; charset=utf-8").
_MEDIA_TYPE_RE = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+/[A-Za-z0-9!#$%&'*+.^_`|~-]+(\s*;\s*[^,]+)?")


def build_ssl_context(ca_bundle=None):
    """A verifying TLS context; a custom CA adds a root and never turns checks off.

    ``verify=False`` is not reachable from any option in this module: enabling
    a custom CA, a header, a redirect or an API key never disables TLS
    verification (F07 acceptance).
    """
    context = ssl.create_default_context(cafile=ca_bundle) if ca_bundle else ssl.create_default_context()
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


@dataclass(frozen=True)
class SecretRef:
    """A reference to a stored secret.  The value never enters this module."""

    store: str
    name: str

    def to_public(self) -> dict:
        return {'secret_ref': self.name, 'store': self.store}


@dataclass(frozen=True)
class TargetProfile:
    """One target with its assertions, limits and scope."""

    id: str
    name: str
    url: str
    method: str = 'GET'
    statuses: tuple[int, ...] = (200,)
    headers: tuple[tuple[str, str], ...] = ()
    contains: str | None = None
    not_contains: str | None = None
    sha256: str | None = None
    json_assertions: tuple[dict, ...] = ()
    content_type: str | None = None
    min_body_bytes: int = 1
    max_body_bytes: int = 262_144
    max_redirects: int | None = None
    dns_mode: str = 'proxy'
    scope: str = 'public'
    own: bool = False
    body: bytes | None = None
    auth: SecretRef | None = None
    kind: str = 'custom'
    definition_version: int = 1
    verified_at: str | None = None

    @property
    def credentialed(self) -> bool:
        return self.auth is not None or self.body is not None or self.method not in IDEMPOTENT_METHODS

    def header_map(self) -> dict:
        return dict(self.headers)

    def to_public(self) -> dict:
        """Redacted view: no body content and no secret value, ever."""
        value = {
            'id': self.id, 'name': self.name, 'url': self.url, 'method': self.method,
            'statuses': list(self.statuses), 'headers': dict(self.headers),
            'contains': self.contains, 'not_contains': self.not_contains, 'sha256': self.sha256,
            'json_assertions': [dict(item) for item in self.json_assertions],
            'content_type': self.content_type, 'min_body_bytes': self.min_body_bytes,
            'max_body_bytes': self.max_body_bytes, 'max_redirects': self.max_redirects,
            'dns_mode': self.dns_mode, 'scope': self.scope, 'own': self.own, 'kind': self.kind,
            'body_bytes': len(self.body) if self.body is not None else 0,
            'auth_configured': self.auth is not None,
            'definition_version': self.definition_version, 'verified_at': self.verified_at,
        }
        if self.auth is not None:
            value['auth'] = self.auth.to_public()
        return value


def _validate_url(value, name, *, allow_private=False):
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise ProbeError(E_VALIDATION_FIELD, f'{name}: ожидается http(s) URL.')
    url = value.strip()
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ProbeError(E_VALIDATION_FIELD, f'{name}: нужен http(s) URL без userinfo.')
    try:
        port = parsed.port
    except ValueError:
        raise ProbeError(E_VALIDATION_FIELD, f'{name}: некорректный порт.') from None
    if port is not None and not 1 <= port <= 65535:
        raise ProbeError(E_VALIDATION_FIELD, f'{name}: некорректный порт.')
    return url


def _validate_json_assertions(value, name):
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or len(value) > 32:
        raise ProbeError(E_VALIDATION_FIELD, f'{name}: ожидается список до 32 утверждений.')
    clean = []
    for item in value:
        if not isinstance(item, dict):
            raise ProbeError(E_VALIDATION_FIELD, f'{name}: утверждение должно быть объектом.')
        _reject_unknown(item, ('path', 'op', 'value'), f'{name}[]')
        path = item.get('path')
        if not isinstance(path, str) or not path or len(path) > 200:
            raise ProbeError(E_VALIDATION_FIELD, f'{name}.path: ожидается непустой путь до поля JSON.')
        op = _choice(item.get('op'), f'{name}.op', JSON_OPS, required=True)
        entry = {'path': path, 'op': op}
        if op in ('equals', 'not_equals', 'contains'):
            if 'value' not in item:
                raise ProbeError(E_VALIDATION_FIELD, f'{name}.value: операция {op} требует value.')
            entry['value'] = item['value']
        elif op in ('min', 'max'):
            number = item.get('value')
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
                raise ProbeError(E_VALIDATION_FIELD, f'{name}.value: ожидается конечное число.')
            entry['value'] = float(number)
        elif 'value' in item:
            entry['value'] = item['value']
        clean.append(entry)
    return tuple(clean)


def validate_target(data, *, own_profile=False) -> TargetProfile:
    """Normalize one target; POST, body and API auth need an own profile."""
    if not isinstance(data, dict):
        raise ProbeError(E_VALIDATION_SCHEMA, 'Цель проверки: ожидается объект.')
    allowed = ('id', 'name', 'url', 'method', 'statuses', 'headers', 'contains', 'not_contains', 'sha256',
               'json_assertions', 'content_type', 'min_body_bytes', 'max_body_bytes', 'max_redirects',
               'dns_mode', 'scope', 'own', 'body', 'auth', 'kind', 'definition_version', 'verified_at')
    _reject_unknown(data, allowed, 'Цель проверки')
    url = _validate_url(data.get('url'), 'Цель: url')
    method = _choice(str(data.get('method', 'GET')).upper(), 'Цель: method', METHODS)
    if 'own' in data and type(data['own']) is not bool:
        raise ProbeError(E_VALIDATION_FIELD, 'Цель: own должен быть логическим.')
    own = bool(data['own']) if 'own' in data else bool(own_profile)
    scope = _choice(data.get('scope'), 'Цель: scope', SCOPES, default='trusted' if own else 'public')
    if own and scope != 'trusted':
        raise ProbeError(E_VALIDATION_SCOPE, 'Собственный target-профиль должен иметь scope=trusted.')
    headers = data.get('headers') or {}
    if not isinstance(headers, dict):
        raise ProbeError(E_VALIDATION_FIELD, 'Цель: headers должен быть объектом.')
    clean_headers = []
    for name, value in headers.items():
        if not isinstance(name, str) or not isinstance(value, str) or '\n' in name + value or '\r' in name + value:
            raise ProbeError(E_VALIDATION_FIELD, 'Цель: headers — только строковые имена и значения без переводов строк.')
        lowered = name.lower()
        if lowered in SECRET_HEADERS and not own:
            raise ProbeError(E_VALIDATION_TARGET_UNSAFE,
                             f'Цель: заголовок {name} несёт учётные данные и разрешён только в собственном профиле цели.')
        if lowered not in SAFE_HEADERS and lowered not in SECRET_HEADERS:
            raise ProbeError(E_VALIDATION_TARGET_UNSAFE,
                             f'Цель: заголовок {name} не входит в разрешённый список и не может уйти на публичную цель.')
        clean_headers.append((name, value))
    auth = None
    if data.get('auth') is not None:
        if not own:
            raise ProbeError(E_VALIDATION_TARGET_UNSAFE,
                             'Цель: API-auth разрешён только в явно настроенном собственном профиле цели.')
        raw = data['auth']
        if not isinstance(raw, dict):
            raise ProbeError(E_VALIDATION_FIELD, 'Цель: auth должен быть объектом со ссылкой на секрет.')
        if any(key in raw for key in ('value', 'token', 'password', 'secret')):
            raise ProbeError(E_VALIDATION_TARGET_UNSAFE,
                             'Цель: значение секрета передаётся только ссылкой на хранилище, не литералом.')
        _reject_unknown(raw, ('secret_ref', 'store'), 'Цель: auth')
        store = _choice(raw.get('store'), 'Цель: auth.store', ('vault',), default='vault')
        name = raw.get('secret_ref')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,64}', name):
            raise ProbeError(E_VALIDATION_FIELD, 'Цель: auth.secret_ref: имя секрета 1..64 символов A-Z a-z 0-9 . _ -')
        auth = SecretRef(store=store, name=name)
    body = data.get('body')
    if body is not None:
        if not own:
            raise ProbeError(E_VALIDATION_TARGET_UNSAFE,
                             'Цель: тело запроса разрешено только в явно настроенном собственном профиле цели.')
        if isinstance(body, str):
            body = body.encode('utf-8')
        if not isinstance(body, (bytes, bytearray)):
            raise ProbeError(E_VALIDATION_FIELD, 'Цель: body должен быть строкой или байтами.')
        if len(body) > 1_048_576:
            raise ProbeError(E_VALIDATION_FIELD, 'Цель: body: максимум 1048576 байт.')
        body = bytes(body)
    if method not in IDEMPOTENT_METHODS and not own:
        raise ProbeError(E_VALIDATION_TARGET_UNSAFE,
                         f'Цель: метод {method} изменяет состояние и доступен только в собственном профиле цели.')
    statuses = data.get('statuses', [200])
    if not isinstance(statuses, (list, tuple)) or not statuses:
        raise ProbeError(E_VALIDATION_FIELD, 'Цель: statuses: непустой список HTTP-кодов.')
    clean_statuses = []
    for status in statuses:
        if type(status) is not int or not 100 <= status <= 599:
            raise ProbeError(E_VALIDATION_FIELD, 'Цель: statuses: ожидаются целые коды 100..599.')
        if status not in clean_statuses:
            clean_statuses.append(status)
    contains = data.get('contains')
    not_contains = data.get('not_contains')
    for value, label in ((contains, 'contains'), (not_contains, 'not_contains')):
        if value is not None and (not isinstance(value, str) or len(value) > 4096):
            raise ProbeError(E_VALIDATION_FIELD, f'Цель: {label}: строка до 4096 символов.')
    if contains and not_contains and contains in not_contains:
        raise ProbeError(E_VALIDATION_FIELD, 'Цель: not_contains не может содержать подстроку contains.')
    sha256 = data.get('sha256')
    if sha256 is not None and (not isinstance(sha256, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', sha256)):
        raise ProbeError(E_VALIDATION_FIELD, 'Цель: sha256: 64 hex-символа.')
    content_type = data.get('content_type')
    if content_type is not None:
        if not isinstance(content_type, str) or not _MEDIA_TYPE_RE.fullmatch(content_type.strip()):
            raise ProbeError(E_VALIDATION_FIELD, 'Цель: content_type: ожидается media-type, например application/json.')
        content_type = content_type.strip()
    min_body = _number(data.get('min_body_bytes'), 'Цель: min_body_bytes', Limit('байт', 0, 8 * 1024 * 1024, 'int'),
                       integer=True, default=1)
    max_body = _number(data.get('max_body_bytes'), 'Цель: max_body_bytes', Limit('байт', 1, 8 * 1024 * 1024, 'int'),
                       integer=True, default=262_144)
    if min_body > max_body:
        raise ProbeError(E_VALIDATION_FIELD, 'Цель: min_body_bytes не может превышать max_body_bytes.')
    redirects = data.get('max_redirects')
    if redirects is not None:
        redirects = _number(redirects, 'Цель: max_redirects', LIMITS['max_redirects'], integer=True)
    dns_mode = _choice(data.get('dns_mode'), 'Цель: dns_mode', DNS_MODES, default='proxy')
    kind = _choice(data.get('kind'), 'Цель: kind', ('basic', 'service', 'custom', 'speed'), default='custom')
    version = _number(data.get('definition_version'), 'Цель: definition_version', Limit('', 1, 1000, 'int'),
                      integer=True, default=1)
    verified_at = data.get('verified_at')
    if verified_at is not None:
        if not isinstance(verified_at, str) or not _DATE_RE.fullmatch(verified_at.strip()):
            raise ProbeError(E_VALIDATION_FIELD, 'Цель: verified_at: дата проверки определения в формате ГГГГ-ММ-ДД.')
        verified_at = verified_at.strip()
    name = data.get('name') or url
    if not isinstance(name, str) or len(name) > 160:
        raise ProbeError(E_VALIDATION_FIELD, 'Цель: name: максимум 160 символов.')
    return TargetProfile(
        id=str(data.get('id') or name)[:160], name=name, url=url, method=method,
        statuses=tuple(clean_statuses), headers=tuple(sorted(clean_headers)), contains=contains,
        not_contains=not_contains, sha256=sha256.lower() if sha256 else None,
        json_assertions=_validate_json_assertions(data.get('json_assertions'), 'Цель: json_assertions'),
        content_type=content_type, min_body_bytes=min_body, max_body_bytes=max_body,
        max_redirects=redirects, dns_mode=dns_mode, scope=scope, own=own, body=body, auth=auth,
        kind=kind, definition_version=version, verified_at=verified_at,
    )


@dataclass(frozen=True)
class SpeedTarget:
    """A bounded download used only for the throughput measurement."""

    url: str
    max_bytes: int = 5_000_000
    dns_mode: str = 'proxy'
    scope: str = 'public'
    headers: tuple[tuple[str, str], ...] = ()

    def to_public(self) -> dict:
        return {'url': self.url, 'max_bytes': self.max_bytes, 'dns_mode': self.dns_mode,
                'scope': self.scope, 'headers': dict(self.headers)}


def validate_speed_target(data) -> SpeedTarget | None:
    if not data:
        return None
    if not isinstance(data, dict):
        raise ProbeError(E_VALIDATION_SCHEMA, 'Цель измерения скорости: ожидается объект.')
    _reject_unknown(data, ('url', 'max_bytes', 'dns_mode', 'scope', 'headers'), 'Цель измерения скорости')
    if not str(data.get('url') or '').strip():
        return None
    url = _validate_url(data.get('url'), 'Цель измерения скорости: url')
    scope = _choice(data.get('scope'), 'Цель измерения скорости: scope', SCOPES, default='public')
    max_bytes = _number(data.get('max_bytes'), 'Цель измерения скорости: max_bytes', LIMITS['speed_max_bytes'],
                        integer=True, default=5_000_000)
    dns_mode = _choice(data.get('dns_mode'), 'Цель измерения скорости: dns_mode', DNS_MODES, default='proxy')
    headers = data.get('headers') or {}
    if not isinstance(headers, dict):
        raise ProbeError(E_VALIDATION_FIELD, 'Цель измерения скорости: headers должен быть объектом.')
    for name in headers:
        if not isinstance(name, str) or name.lower() not in SAFE_HEADERS:
            raise ProbeError(E_VALIDATION_TARGET_UNSAFE,
                             f'Цель измерения скорости: заголовок {name} не разрешён для публичной цели.')
    return SpeedTarget(url=url, max_bytes=max_bytes, dns_mode=dns_mode, scope=scope,
                       headers=tuple(sorted(headers.items())))


# --------------------------------------------------------------------------
# built-in basic probes
# --------------------------------------------------------------------------

# Small, validation-carrying probes a newcomer gets without typing a URL.
# The definitions are IANA documentation endpoints, they are small, and the
# body budget per probe is 64 KiB, so "basic" never turns into load on a demo
# endpoint.  ``verified_at`` stays None until a human checks the definition
# against a real answer: F01 asks for the honest label, not for a promise.
BASIC_PROBES = (
    {'id': 'basic-https-example', 'name': 'Example Domain (HTTPS через CONNECT)', 'kind': 'basic',
     'url': 'https://example.com/', 'statuses': [200], 'content_type': 'text/html',
     'contains': 'Example Domain', 'min_body_bytes': 64, 'max_body_bytes': 65_536,
     'definition_version': 1, 'verified_at': None},
    {'id': 'basic-http-example', 'name': 'Example Domain (обычный HTTP)', 'kind': 'basic',
     'url': 'http://example.com/', 'statuses': [200], 'content_type': 'text/html',
     'contains': 'Example Domain', 'min_body_bytes': 64, 'max_body_bytes': 65_536,
     'definition_version': 1, 'verified_at': None},
    {'id': 'basic-iana-reserved', 'name': 'IANA: зарезервированные домены', 'kind': 'basic',
     'url': 'https://www.iana.org/domains/reserved', 'statuses': [200], 'content_type': 'text/html',
     'contains': 'reserved', 'min_body_bytes': 64, 'max_body_bytes': 65_536,
     'definition_version': 1, 'verified_at': None},
)


def basic_profiles() -> tuple[TargetProfile, ...]:
    return tuple(validate_target(item) for item in BASIC_PROBES)


def fallback_chain(profiles, limit=2) -> tuple[TargetProfile, ...]:
    """The primary probe plus at most ``limit`` validated fallbacks.

    The chain is bounded on purpose: an unreachable neutral target must not turn
    into a sweep over the whole list for every candidate proxy.
    """
    if limit < 0:
        raise ProbeError(E_VALIDATION_FIELD, 'fallback: limit не может быть отрицательным.')
    chain = tuple(profiles)
    if not chain:
        raise ProbeError(E_VALIDATION_FIELD, 'fallback: пустой список проб.')
    if len(chain) > MAX_BASIC_PROBES:
        raise ProbeError(E_VALIDATION_FIELD, f'Базовый набор ограничен {MAX_BASIC_PROBES} пробами.')
    return chain[:limit + 1]


def probes_manifest() -> dict:
    """What the built-in probes are and how well they are confirmed (F01, F06)."""
    items = []
    for profile in basic_profiles():
        value = profile.to_public()
        value['verified'] = bool(profile.verified_at)
        items.append(value)
    return {'version': 1, 'probes': items,
            'note': 'Встроенные probes описаны явно; дата проверки определения проставляется после ручной сверки ответа.',
            'max_body_bytes_per_probe': max(item['max_body_bytes'] for item in items),
            'verified_count': sum(1 for item in items if item['verified'])}


# --------------------------------------------------------------------------
# clock, requests and outcomes
# --------------------------------------------------------------------------


class Clock:
    """Time source seam; the default is the system monotonic clock."""

    def monotonic(self) -> float:  # pragma: no cover - interface
        raise NotImplementedError

    def time(self) -> float:  # pragma: no cover - interface
        raise NotImplementedError


@dataclass
class SystemClock(Clock):
    def monotonic(self) -> float:
        return time.perf_counter()

    def time(self) -> float:
        return time.time()


@dataclass(frozen=True)
class ProbeRequest:
    """One request the transport must perform through the proxy."""

    url: str
    method: str = 'GET'
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes | None = None
    max_bytes: int = 262_144
    dns_mode: str = 'proxy'
    read_timeout_s: float = 8.0
    connect_timeout_s: float = 4.0
    handshake_timeout_s: float = 6.0
    stage: str = 'target'
    hop: int = 0

    def header_map(self) -> dict:
        return dict(self.headers)


@dataclass(frozen=True)
class ProbeResponse:
    """What the transport reports back.  ``code``/``stage`` are set on failure."""

    status: int | None = None
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes = b''
    url: str | None = None
    code: str | None = None
    stage: str | None = None
    connect_ms: float | None = None
    handshake_ms: float | None = None
    ttfb_ms: float | None = None
    transfer_ms: float | None = None
    total_ms: float | None = None

    def header(self, name) -> str | None:
        lowered = name.lower()
        for key, value in self.headers:
            if key.lower() == lowered:
                return value
        return None


@dataclass(frozen=True)
class TargetOutcome:
    """Result of one target: what was asked, what came back, and why it failed."""

    target_id: str
    ok: bool
    code: str | None = None
    stage: str | None = None
    status: int | None = None
    bytes: int = 0
    attempts: int = 1
    url: str = ''
    ttfb_ms: float | None = None
    transfer_ms: float | None = None
    total_ms: float | None = None
    connect_ms: float | None = None
    handshake_ms: float | None = None
    detail: str | None = None

    def to_public(self) -> dict:
        return {'target_id': self.target_id, 'ok': self.ok, 'code': self.code, 'stage': self.stage,
                'status': self.status, 'bytes': self.bytes, 'attempts': self.attempts, 'url': self.url,
                'ttfb_ms': self.ttfb_ms, 'transfer_ms': self.transfer_ms, 'total_ms': self.total_ms,
                'connect_ms': self.connect_ms, 'handshake_ms': self.handshake_ms, 'detail': self.detail}


@dataclass(frozen=True)
class PlanOutcome:
    """Result of running one plan against one endpoint."""

    endpoint: str
    mode: str
    evidence: str
    ok: bool
    targets: tuple[TargetOutcome, ...] = ()
    code: str | None = None
    stage: str | None = None
    monitoring: bool = False
    started_at: float = 0.0
    finished_at: float = 0.0
    whole_probe_timeout_s: float = 0.0

    @property
    def successes(self) -> int:
        return sum(1 for item in self.targets if item.ok)

    @property
    def bytes(self) -> int:
        return sum(item.bytes for item in self.targets)

    def to_public(self) -> dict:
        return {'endpoint': self.endpoint, 'mode': self.mode, 'evidence': self.evidence, 'ok': self.ok,
                'code': self.code, 'stage': self.stage, 'monitoring': self.monitoring,
                'successes': self.successes, 'bytes': self.bytes,
                'duration_ms': round((self.finished_at - self.started_at) * 1000, 2),
                'whole_probe_timeout_s': self.whole_probe_timeout_s,
                'targets': [item.to_public() for item in self.targets]}


# --------------------------------------------------------------------------
# one target: attempts, redirects and assertions
# --------------------------------------------------------------------------


def _header_matches(content_type, expected) -> bool:
    if not content_type:
        return False
    actual = content_type.split(';', 1)[0].strip().lower()
    wanted = expected.split(';', 1)[0].strip().lower()
    return actual == wanted or actual.startswith(wanted + '+')


def _json_type(value) -> str:
    if isinstance(value, bool):
        return 'boolean'
    if isinstance(value, (int, float)):
        return 'number'
    if isinstance(value, str):
        return 'string'
    if isinstance(value, list):
        return 'array'
    if isinstance(value, dict):
        return 'object'
    if value is None:
        return 'null'
    return 'unknown'


def _json_path(document, path):
    current = document
    for part in path.split('.'):
        if isinstance(current, list):
            if not re.fullmatch(r'-?\d+', part):
                raise KeyError(path)
            index = int(part)
            if not -len(current) <= index < len(current):
                raise KeyError(path)
            current = current[index]
        elif isinstance(current, dict):
            current = current[part]
        else:
            raise KeyError(path)
    return current


def check_json_assertions(body, assertions):
    """Returns ``(ok, detail)``; a body that is not JSON fails the assertion."""
    if not assertions:
        return True, None
    try:
        document = json.loads(body.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False, 'body is not valid JSON'
    for assertion in assertions:
        try:
            value = _json_path(document, assertion['path'])
        except (KeyError, IndexError, TypeError):
            if assertion['op'] == 'exists':
                return False, f"{assertion['path']}: поля нет"
            if assertion['op'] in ('not_equals', 'min', 'max', 'contains'):
                value = None
            else:
                return False, f"{assertion['path']}: поля нет"
        op = assertion['op']
        if op == 'exists':
            continue
        if op == 'equals' and value != assertion['value']:
            return False, f"{assertion['path']} != {assertion['value']!r}"
        if op == 'not_equals' and value == assertion['value']:
            return False, f"{assertion['path']} == {assertion['value']!r}"
        if op == 'type' and _json_type(value) != assertion.get('value'):
            return False, f"{assertion['path']}: тип {_json_type(value)}"
        if op == 'contains':
            expected = assertion['value']
            if isinstance(value, str) and isinstance(expected, str):
                if expected not in value:
                    return False, f"{assertion['path']}: нет {expected!r}"
            elif isinstance(value, list):
                if expected not in value:
                    return False, f"{assertion['path']}: нет {expected!r}"
            else:
                return False, f"{assertion['path']}: contains неприменим к типу {_json_type(value)}"
        if op == 'min' and (isinstance(value, bool) or not isinstance(value, (int, float)) or value < assertion['value']):
            return False, f"{assertion['path']} < {assertion['value']}"
        if op == 'max' and (isinstance(value, bool) or not isinstance(value, (int, float)) or value > assertion['value']):
            return False, f"{assertion['path']} > {assertion['value']}"
    return True, None


def evaluate_response(target: TargetProfile, response: ProbeResponse) -> TargetOutcome:
    """Assert one answer.  The order is fixed so a failure is reproducible."""
    common = dict(target_id=target.id, status=response.status, url=response.url or target.url,
                  bytes=len(response.body or b''), connect_ms=response.connect_ms,
                  handshake_ms=response.handshake_ms, ttfb_ms=response.ttfb_ms,
                  transfer_ms=response.transfer_ms, total_ms=response.total_ms)
    if response.code:
        return TargetOutcome(ok=False, code=response.code, stage=response.stage or 'target', **common)
    if response.status not in target.statuses:
        return TargetOutcome(ok=False, code=f'HTTP_{response.status}', stage='target', **common)
    body = response.body or b''
    if target.content_type and not _header_matches(response.header('content-type'), target.content_type):
        return TargetOutcome(ok=False, code=CONTENT_TYPE, stage='assert',
                             detail=f"ожидался {target.content_type}, получен {response.header('content-type')!r}", **common)
    if len(body) > target.max_body_bytes:
        return TargetOutcome(ok=False, code=BODY_TOO_LARGE, stage='assert',
                             detail=f"тело {len(body)} байт при лимите {target.max_body_bytes}", **common)
    if len(body) < target.min_body_bytes:
        return TargetOutcome(ok=False, code=BODY_TOO_SMALL, stage='assert',
                             detail=f"тело {len(body)} байт при минимуме {target.min_body_bytes}", **common)
    if target.contains is not None and target.contains.encode() not in body:
        return TargetOutcome(ok=False, code=CONTENT_MISMATCH, stage='assert',
                             detail='нет ожидаемой подстроки', **common)
    if target.not_contains is not None and target.not_contains.encode() in body:
        return TargetOutcome(ok=False, code=CONTENT_MISMATCH, stage='assert',
                             detail=f'ответ содержит запрещённую подстроку {target.not_contains!r}', **common)
    if target.sha256 and hashlib.sha256(body).hexdigest() != target.sha256:
        return TargetOutcome(ok=False, code=HASH_MISMATCH, stage='assert', **common)
    ok, detail = check_json_assertions(body, target.json_assertions)
    if not ok:
        return TargetOutcome(ok=False, code=JSON_ASSERT, stage='assert', detail=detail, **common)
    return TargetOutcome(ok=True, stage='target', **common)


async def run_probe(target: TargetProfile, options: ProbeOptions, transport, *, clock=None) -> TargetOutcome:
    """Measure one target through the proxy within the whole-probe budget.

    ``transport.send(request, options=options)`` returns a :class:`ProbeResponse`.
    A transport failure is reported with ``code`` and ``stage`` so a dead proxy
    (``tcp``) is never confused with a live proxy whose target misbehaved
    (``target``).
    """
    clock = clock or SystemClock()
    time_budget(options)
    request = ProbeRequest(
        url=target.url, method=target.method, headers=target.headers, body=target.body,
        max_bytes=target.max_body_bytes, dns_mode=target.dns_mode, read_timeout_s=options.read_timeout_s,
        connect_timeout_s=options.connect_timeout_s, handshake_timeout_s=options.handshake_timeout_s)
    started = clock.monotonic()
    delays = plan_attempts(options)
    outcome = None
    for attempt in range(1, options.attempts + 1):
        remaining = options.whole_probe_timeout_s - (clock.monotonic() - started)
        if attempt > 1:
            delay = delays[attempt - 2]
            if delay >= remaining:
                return _replace(outcome, code=WHOLE_PROBE_TIMEOUT, stage='target', attempts=attempt,
                                detail=f'общий срок пробы исчерпан до попытки {attempt}')
            await asyncio.sleep(delay)
            remaining -= delay
        try:
            async with asyncio.timeout(max(remaining, 0.001)):
                outcome = await _attempt(target, request, options, transport, attempt)
        except (TimeoutError, asyncio.TimeoutError):
            return TargetOutcome(target_id=target.id, ok=False, code=WHOLE_PROBE_TIMEOUT, stage='target',
                                 attempts=attempt, url=target.url,
                                 detail=f"весь срок {options.whole_probe_timeout_s:g} с исчерпан")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a broken proxy raises more than httpx errors
            outcome = TargetOutcome(target_id=target.id, ok=False, code=type(exc).__name__, stage='target',
                                    attempts=attempt, url=target.url,
                                    detail=f'транспорт бросил {type(exc).__name__}')
        if outcome.ok or outcome.code not in RETRYABLE_CODES:
            break
    return replace(outcome, attempts=attempt)


async def run_stage(stage, address, options: ProbeOptions, transport, *, clock=None) -> TargetOutcome:
    """A ``tcp`` or ``handshake`` check of one address with the same retry policy.

    The transport opens the connection to the proxy address itself, so a mode
    without targets still gets a measurement — and it stays a measurement of
    the stage it claims: an open port is never reported as a transfer.
    """
    if stage not in ('tcp', 'handshake'):
        raise ProbeError(E_VALIDATION_FIELD, f'Этап {stage} не является проверкой адреса.')
    clock = clock or SystemClock()
    started = clock.monotonic()
    delays = plan_attempts(options)
    request = ProbeRequest(url=address, stage=stage, max_bytes=0, dns_mode='proxy',
                           read_timeout_s=options.connect_timeout_s,
                           connect_timeout_s=options.connect_timeout_s,
                           handshake_timeout_s=options.handshake_timeout_s)
    outcome = None
    for attempt in range(1, options.attempts + 1):
        remaining = options.whole_probe_timeout_s - (clock.monotonic() - started)
        if attempt > 1:
            delay = delays[attempt - 2]
            if delay >= remaining:
                return _replace(outcome, code=WHOLE_PROBE_TIMEOUT, stage=stage, attempts=attempt, url=address,
                                detail=f'общий срок пробы исчерпан до попытки {attempt}')
            await asyncio.sleep(delay)
            remaining -= delay
        try:
            async with asyncio.timeout(max(remaining, 0.001)):
                response = await transport.send(request, options=options)
        except (TimeoutError, asyncio.TimeoutError):
            return TargetOutcome(target_id=stage, ok=False, code=WHOLE_PROBE_TIMEOUT, stage=stage,
                                 attempts=attempt, url=address,
                                 detail=f"весь срок {options.whole_probe_timeout_s:g} с исчерпан")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            response = ProbeResponse(code=type(exc).__name__, stage=stage)
        if response.code:
            outcome = TargetOutcome(target_id=stage, ok=False, code=response.code,
                                    stage=response.stage or stage, status=response.status,
                                    attempts=attempt, url=address,
                                    connect_ms=response.connect_ms, handshake_ms=response.handshake_ms)
            if response.code not in RETRYABLE_CODES:
                break
            continue
        outcome = TargetOutcome(target_id=stage, ok=True, stage=stage, status=response.status,
                                attempts=attempt, url=address, connect_ms=response.connect_ms,
                                handshake_ms=response.handshake_ms, total_ms=response.total_ms)
        break
    return replace(outcome, attempts=attempt)


async def _attempt(target, request, options, transport, attempt):
    current = request
    seen = [current.url]
    limit = options.max_redirects if target.max_redirects is None else target.max_redirects
    for hop in range(limit + 1):
        current = replace(current, hop=hop)
        response = await transport.send(current, options=options)
        if response.code:
            return TargetOutcome(target_id=target.id, ok=False, code=response.code,
                                 stage=response.stage or 'target', status=response.status, attempts=attempt,
                                 url=current.url, bytes=len(response.body or b''))
        if response.status in _REDIRECT_STATUSES and hop < limit:
            location = response.header('location')
            if not location:
                return TargetOutcome(target_id=target.id, ok=False, code=CONTENT_MISMATCH, stage='assert',
                                     status=response.status, attempts=attempt, url=current.url,
                                     detail=f'редирект {response.status} без заголовка Location')
            nxt = urljoin(current.url, location.strip())
            if nxt in seen:
                return TargetOutcome(target_id=target.id, ok=False, code=REDIRECT_LOOP, stage='target',
                                     status=response.status, attempts=attempt, url=nxt, detail='цикл редиректов')
            seen.append(nxt)
            current = replace(current, url=nxt)
            continue
        if response.status in _REDIRECT_STATUSES:
            detail = ('редиректы для этой цели не разрешены' if not limit
                      else f'цепочка длиннее {limit} переходов')
            return TargetOutcome(target_id=target.id, ok=False, code=REDIRECT_TOO_MANY, stage='target',
                                 status=response.status, attempts=attempt, url=current.url, detail=detail)
        return replace(evaluate_response(target, response), attempts=attempt)
    raise AssertionError('unreachable')


def _replace(outcome, **changes):
    if outcome is None:
        return TargetOutcome(**changes)
    return replace(outcome, **changes)


# --------------------------------------------------------------------------
# plans
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbePlan:
    """A validated check configuration: mode, options, targets and extras."""

    mode: Mode
    options: ProbeOptions
    targets: tuple[TargetProfile, ...] = ()
    judge: JudgeSpec | None = None
    min_anonymity: str = 'any'
    zones: tuple[DnsblZone, ...] = ()
    speed: SpeedTarget | None = None

    def identity(self) -> dict:
        """What is actually measured, without the derived mode wrappers.

        ``recheck`` and ``monitor`` re-run another mode, so they must not mint
        a new profile identity: otherwise every pool tick would look like a new
        configuration and republish the world.
        """
        mode = self.mode
        return {
            'measures': mode.base or mode.name,
            'checks_transfer': mode.checks_transfer,
            'options': self.options.to_public(),
            'targets': [item.to_public() for item in self.targets],
            'judge': self.judge.to_public() if self.judge else None,
            'min_anonymity': self.min_anonymity,
            'dnsbl_zones': [zone.name for zone in self.zones],
            'speed': self.speed.to_public() if self.speed else None,
        }

    def to_public(self) -> dict:
        return {
            'mode': self.mode.to_public(),
            'options': self.options.to_public(),
            'targets': [item.to_public() for item in self.targets],
            'judge': self.judge.to_public() if self.judge else None,
            'min_anonymity': self.min_anonymity,
            'dnsbl_zones': [zone.name for zone in self.zones],
            'speed': self.speed.to_public() if self.speed else None,
        }


def plan_digest(plan: ProbePlan) -> str:
    """Content address of what a plan measures, computed from its public view."""
    encoded = json.dumps(plan.identity(), sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:20]


def build_plan(settings) -> ProbePlan:
    """The one validation entry for GUI, CLI and API (F07, F18).

    Unknown keys are rejected, every number is range-checked against
    :data:`LIMITS`, and a required anonymity level without a judge raises
    instead of being dropped to ``any`` (defect 13).
    """
    if not isinstance(settings, dict):
        raise ProbeError(E_VALIDATION_SCHEMA, 'Настройки проверки: ожидается объект.')
    _reject_unknown(settings, ('mode', 'base_mode', 'monitor', 'options', 'targets', 'anonymity',
                               'min_anonymity', 'dnsbl', 'speed', 'services'), 'Настройки проверки')
    mode = resolve_mode(settings.get('mode'), base=settings.get('base_mode'),
                        monitoring=bool(settings.get('monitor', False)))
    options = validate_options(settings.get('options'))
    time_budget(options)
    raw_targets = settings.get('targets')
    if raw_targets is None and mode.name == BASIC:
        raw_targets = [dict(item) for item in BASIC_PROBES]
    if raw_targets is None and mode.name == RECHECK and mode.base == BASIC:
        # A recheck re-measures with the profile's own targets; the built-in
        # basic probes are the honest fallback when the caller has none.
        raw_targets = [dict(item) for item in BASIC_PROBES]
    if raw_targets is None and mode.name == SERVICES:
        raw_targets = settings.get('services')
    if mode.needs_targets:
        if not isinstance(raw_targets, (list, tuple)) or not raw_targets:
            raise ProbeError(E_VALIDATION_FIELD, f'Режим {mode.name} требует хотя бы одной цели проверки.')
        if len(raw_targets) > MAX_TARGETS:
            raise ProbeError(E_VALIDATION_FIELD, f'Цели: максимум {MAX_TARGETS}.')
        if mode.name == BASIC and len(raw_targets) > MAX_BASIC_PROBES:
            raise ProbeError(E_VALIDATION_FIELD, f'Базовый набор ограничен {MAX_BASIC_PROBES} пробами.')
        targets = tuple(validate_target(item, own_profile=(mode.name == CUSTOM)) for item in raw_targets)
        if options.attempts > 1 and any(target.method not in IDEMPOTENT_METHODS for target in targets):
            raise ProbeError(E_VALIDATION_RETRY_UNSAFE,
                             'Метод, меняющий состояние, нельзя повторять: attempts должен быть 1. '
                             'Небезопасный повтор POST/PUT/PATCH/DELETE запрещён (F07).')
    elif raw_targets:
        raise ProbeError(E_VALIDATION_FIELD, f'Режим {mode.name} не измеряет цели: уберите targets.')
    else:
        targets = ()
    anonymity = settings.get('anonymity')
    judge = validate_judge_spec(anonymity) if anonymity else None
    min_anonymity = validate_min_level(settings.get('min_anonymity', 'any'))
    require_anonymity(min_anonymity, judge)
    zones = validate_dnsbl_zones(settings.get('dnsbl'))
    speed = validate_speed_target(settings.get('speed'))
    return ProbePlan(mode=mode, options=options, targets=targets, judge=judge,
                     min_anonymity=min_anonymity, zones=zones, speed=speed)


async def run_plan(plan: ProbePlan, transport, *, endpoint='', fail_fast=True, clock=None) -> PlanOutcome:
    """Run a plan against one endpoint.  The transport carries the proxy itself."""
    clock = clock or SystemClock()
    if plan.mode.collects_only:
        raise ProbeError(E_VALIDATION_FIELD,
                         'Режим collect_only не измеряет: он только собирает кандидатов и не даёт вердикта.')
    started_at = clock.time()
    started = clock.monotonic()
    if plan.mode.needs_targets:
        if not plan.targets:
            raise ProbeError(E_VALIDATION_FIELD, f'Режим {plan.mode.name} требует хотя бы одной цели проверки.')
        outcomes = []
        for target in plan.targets:
            outcome = await run_probe(target, plan.options, transport, clock=clock)
            outcomes.append(outcome)
            if outcome.ok and fail_fast:
                break
    else:
        outcomes = [await run_stage(plan.mode.name, endpoint, plan.options, transport, clock=clock)]
    finished = clock.monotonic()
    ok = any(item.ok for item in outcomes)
    failed = [item for item in outcomes if not item.ok]
    code = None if ok else (failed[0].code if failed else None)
    stage = None if ok else (failed[0].stage if failed else None)
    outcome = PlanOutcome(endpoint=endpoint, mode=plan.mode.name, evidence='none', ok=ok,
                          targets=tuple(outcomes), code=code, stage=stage, monitoring=plan.mode.monitoring,
                          started_at=started_at, finished_at=started_at + (finished - started),
                          whole_probe_timeout_s=plan.options.whole_probe_timeout_s)
    return replace(outcome, evidence=evidence_for(outcome))


async def check_targets(transport, targets, options, *, limit=2) -> tuple[TargetOutcome, ...]:
    """Bounded direct check of the neutral targets, to separate them from proxies.

    This is the honest way to tell "the target is down" from "all proxies are
    down": the same transport contract is used, only without a proxy, and only
    for the first ``limit`` targets so the control traffic stays small.
    """
    checked = []
    for target in tuple(targets)[:max(0, limit)]:
        checked.append(await run_probe(target, options, transport))
    return tuple(checked)


@dataclass(frozen=True)
class BasicSummary:
    """What a basic run means, including who is to blame for an empty result."""

    working: int
    total: int
    evidence: str
    code: str | None = None
    target_check: tuple[TargetOutcome, ...] = ()

    @property
    def target_unavailable(self) -> bool:
        return self.code == TARGET_UNAVAILABLE

    def to_public(self) -> dict:
        return {'working': self.working, 'total': self.total, 'evidence': self.evidence, 'code': self.code,
                'target_check': [item.to_public() for item in self.target_check]}


def _target_attributable(code) -> bool:
    """Whether a failure code says something about the target, not the proxy.

    Transport failures (refused, timeout, DNS) belong to the proxy path; an
    HTTP answer or a failed assertion is the target's own behaviour.
    """
    if not isinstance(code, str):
        return False
    return code in TARGET_FAILURE_CODES or code.startswith('HTTP_')


def target_failure_signal(outcomes, *, min_examples=2) -> str | None:
    """Attribute a set of failures to the target or to the proxies.

    A target-stage failure is not proof that the target is down, so a code is
    returned only when at least ``min_examples`` independent endpoints failed
    the same way.  Otherwise ``None``: the run simply found no working proxy.
    """
    failures = [item for item in outcomes if not item.ok]
    if not failures:
        return None
    at_target = [item for item in failures
                 if item.stage in ('target', 'assert') and _target_attributable(item.code)]
    if len(at_target) == len(failures) and len(failures) >= max(2, min_examples):
        if len({item.code for item in at_target}) == 1:
            return TARGET_UNAVAILABLE
    return None


def summarize_basic(outcomes, *, evidence='transfer_ok', target_check=()) -> BasicSummary:
    """Summarize a basic run, including who is to blame for an empty result.

    A direct target check is stronger evidence than the "many proxies failed the
    same way" heuristic, so it wins when both are available.
    """
    working = [item for item in outcomes if is_working(item)]
    check = tuple(target_check)
    code = None
    if not working:
        proven = [item for item in check if not item.ok and item.stage in ('target', 'assert')]
        if proven:
            code = TARGET_UNAVAILABLE
        else:
            code = target_failure_signal(outcomes)
            if code is None and all(item.targets for item in outcomes):
                # Every endpoint stopped at tcp/handshake: the proxies are gone.
                stages = {item.stage for item in outcomes if item.code}
                if stages and stages <= {'tcp', 'handshake'}:
                    code = UNREACHABLE
    return BasicSummary(working=len(working), total=len(outcomes), evidence=evidence, code=code,
                        target_check=check)


# --------------------------------------------------------------------------
# speed measurement (defect 15)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SpeedLimits:
    """Minimum sample for an honest number (CONTRACTS §5.5, defect 15)."""

    min_bytes: int = 1 << 20
    min_seconds: float = 0.25
    min_chunks: int = 2
    max_bytes: int = 200_000_000

    def describe(self) -> dict:
        return {'min_bytes': self.min_bytes, 'min_seconds': self.min_seconds,
                'min_chunks': self.min_chunks, 'max_bytes': self.max_bytes}


SPEED_LIMITS = SpeedLimits()


@dataclass
class TransferTrace:
    """Chunk-level timing of one download, filled by the reader.

    The window is measured from the first byte to the last byte of the body, so
    the old "start counting after the first chunk" bug cannot produce a
    plausible-looking number out of a single chunk.
    """

    url: str = ''
    bytes: int = 0
    chunks: int = 0
    started_at: float | None = None
    first_byte_at: float | None = None
    finished_at: float | None = None
    code: str | None = None
    status: int | None = None
    detail: str | None = None

    def begin(self, at):
        self.started_at = at
        return self

    def add(self, size, at):
        if self.started_at is None:
            self.started_at = at
        if self.first_byte_at is None:
            self.first_byte_at = at
        self.chunks += 1
        self.bytes += int(size)
        return self

    def finish(self, at):
        self.finished_at = at
        return self

    def fail(self, code, at=None, detail=None):
        self.code = code
        if detail:
            self.detail = detail
        if at is not None:
            self.finished_at = at
        return self

    @property
    def complete(self) -> bool:
        return self.started_at is not None and self.first_byte_at is not None and self.finished_at is not None

    def to_public(self) -> dict:
        return {'url': self.url, 'bytes': self.bytes, 'chunks': self.chunks, 'status': self.status,
                'code': self.code, 'complete': self.complete, 'detail': self.detail}


@dataclass(frozen=True)
class SpeedMeasurement:
    """Throughput with its state.  ``state != 'ok'`` never carries a number."""

    state: str
    mbps: float | None = None
    bytes: int = 0
    chunks: int = 0
    ttfb_ms: float | None = None
    transfer_ms: float | None = None
    total_ms: float | None = None
    code: str | None = None
    detail: str | None = None

    @property
    def measured(self) -> bool:
        return self.state == 'ok'

    def to_public(self) -> dict:
        return {'state': self.state, 'mbps': self.mbps, 'bytes': self.bytes, 'chunks': self.chunks,
                'ttfb_ms': self.ttfb_ms, 'transfer_ms': self.transfer_ms, 'total_ms': self.total_ms,
                'code': self.code, 'detail': self.detail}


def measure_speed(trace: TransferTrace, limits: SpeedLimits = SPEED_LIMITS) -> SpeedMeasurement:
    """Turn a finished trace into an honest measurement.

    A failed or unfinished transfer is ``error``/``insufficient``; a finished
    but too small, too short or single-chunk transfer is ``insufficient``.  A
    number appears only when all minimums hold (defect 15, R10).
    """
    if trace.code:
        return SpeedMeasurement(state='error', bytes=trace.bytes, chunks=trace.chunks, code=trace.code,
                                detail=trace.detail or f'замер прерван: {trace.code}')
    if not trace.complete:
        missing = 'замер не завершён' if trace.finished_at is None else 'нет отметки первого байта'
        return SpeedMeasurement(state='insufficient', bytes=trace.bytes, chunks=trace.chunks,
                                code=INSUFFICIENT_SAMPLE, detail=f'{missing}: {trace.bytes} байт, {trace.chunks} чанков')
    ttfb_ms = round((trace.first_byte_at - trace.started_at) * 1000, 2)
    transfer_s = trace.finished_at - trace.first_byte_at
    total_ms = round((trace.finished_at - trace.started_at) * 1000, 2)
    transfer_ms = round(transfer_s * 1000, 2)
    def insufficient(reason):
        return SpeedMeasurement(state='insufficient', bytes=trace.bytes, chunks=trace.chunks,
                                ttfb_ms=ttfb_ms, transfer_ms=transfer_ms, total_ms=total_ms,
                                code=INSUFFICIENT_SAMPLE, detail=reason)
    if trace.chunks < max(1, limits.min_chunks):
        return insufficient(f'одного чанка недостаточно: нужно минимум {limits.min_chunks} (окно {transfer_s:.3f} с)')
    if trace.bytes < limits.min_bytes:
        return insufficient(f'мало данных: {trace.bytes} байт при минимуме {limits.min_bytes}')
    if transfer_s < limits.min_seconds:
        return insufficient(f'окно передачи {transfer_s:.3f} с короче минимума {limits.min_seconds} с')
    if trace.bytes > limits.max_bytes:
        return insufficient(f'превышен лимит замера: {trace.bytes} байт при максимуме {limits.max_bytes}')
    mbps = round(trace.bytes * 8 / transfer_s / 1e6, 2)
    return SpeedMeasurement(state='ok', mbps=mbps, bytes=trace.bytes, chunks=trace.chunks, ttfb_ms=ttfb_ms,
                            transfer_ms=transfer_ms, total_ms=total_ms)


async def run_speed_test(target: SpeedTarget, options: ProbeOptions, transport, *, limits=SPEED_LIMITS) -> SpeedMeasurement:
    """Run a bounded download through the transport and measure it.

    The transport provides ``download(target, options=options) -> TransferTrace``
    and owns the socket, mirroring how ``proxytool.measure_speed`` is wired.
    """
    if target is None:
        raise ProbeError(E_VALIDATION_FIELD, 'Нужна цель измерения скорости.')
    time_budget(options)
    started = time.perf_counter()
    try:
        async with asyncio.timeout(options.whole_probe_timeout_s):
            trace = await transport.download(target, options=options)
    except (TimeoutError, asyncio.TimeoutError):
        return SpeedMeasurement(state='error', code=WHOLE_PROBE_TIMEOUT,
                                detail=f'весь срок {options.whole_probe_timeout_s:g} с исчерпан')
    except Exception as exc:  # a broken proxy raises more than httpx errors
        return SpeedMeasurement(state='error', code=type(exc).__name__,
                                detail=f'замер прерван: {type(exc).__name__}')
    if trace is None:
        return SpeedMeasurement(state='error', code=DNS_ERROR, detail='транспорт не вернул замер')
    measurement = measure_speed(trace, limits)
    if measurement.total_ms is None:
        elapsed = round((time.perf_counter() - started) * 1000, 2)
        return replace(measurement, total_ms=elapsed)
    return measurement


# --------------------------------------------------------------------------
# anonymity (defect 13)
# --------------------------------------------------------------------------

ANONYMITY_LEVELS = ('transparent', 'anonymous', 'elite', 'unknown')
MIN_LEVELS = ('any', 'transparent', 'anonymous', 'elite')
LEVEL_RANK = {'unknown': -1, 'transparent': 0, 'anonymous': 1, 'elite': 2}

# Headers a proxy adds and an echo judge returns back.  CGI-style names
# (``HTTP_X_FORWARDED_FOR``) match through the optional "http-" prefix.
PROXY_REVEALING_HEADERS = (
    'via', 'x-forwarded-for', 'x-forwarded', 'forwarded-for', 'forwarded', 'x-real-ip',
    'client-ip', 'x-client-ip', 'x-originating-ip', 'x-proxy-id', 'proxy-connection',
    'x-bluecoat-via',
)
_HEADER_PATTERNS = tuple(
    (name, re.compile(r'(?<![a-z0-9-])(?:http-)?' + re.escape(name) + r'["\']?\s*[:=]'))
    for name in PROXY_REVEALING_HEADERS
)
_IPV4 = re.compile(r'(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])')
_IPV6 = re.compile(r'(?<![0-9a-f:])(?:[0-9a-f]{0,4}:){2,7}[0-9a-f]{0,4}(?![0-9a-f:])', re.I)
_EXIT_IP = re.compile(
    r'(?:remote[-_ ]addr|client[-_ ]ip|origin|visitor|visitor_ip|"ip"|"addr")["\']?\s*(?:=>|[:=])\s*'
    r'["\']?([0-9a-f:.]{3,45})', re.I)
_CHALLENGE_MARKERS = (
    'captcha', 'recaptcha', 'hcaptcha', 'cf-browser-verification', '__cf_chl', 'px-captcha',
    'checking your browser', 'just a moment', 'attention required', 'enable javascript and cookies',
    'verify you are a human', 'unusual traffic from your computer', 'ddos protection by',
    'ray id', 'cf-error-details',
)


@dataclass(frozen=True)
class JudgeSpec:
    """A validated echo judge the user configured themselves."""

    url: str
    max_bytes: int = 65_536
    content_type: str | None = None
    require_echo_address: bool = True

    def to_public(self) -> dict:
        return {'url': self.url, 'max_bytes': self.max_bytes, 'content_type': self.content_type,
                'require_echo_address': self.require_echo_address}


def validate_judge_spec(data) -> JudgeSpec | None:
    if not data:
        return None
    if not isinstance(data, dict):
        raise ProbeError(E_VALIDATION_SCHEMA, 'Judge анонимности: ожидается объект.')
    _reject_unknown(data, ('url', 'max_bytes', 'content_type', 'require_echo_address'),
                    'Judge анонимности')
    url = str(data.get('url') or '').strip()
    if not url:
        return None
    url = _validate_url(url, 'anonymity.judge_url')
    max_bytes = _number(data.get('max_bytes'), 'anonymity.max_bytes', LIMITS['judge_max_bytes'],
                        integer=True, default=65_536)
    content_type = data.get('content_type')
    if content_type is not None:
        if not isinstance(content_type, str) or not _MEDIA_TYPE_RE.fullmatch(content_type.strip()):
            raise ProbeError(E_VALIDATION_FIELD, 'anonymity.content_type: ожидается media-type.')
        content_type = content_type.strip()
    require = data.get('require_echo_address', True)
    if type(require) is not bool:
        raise ProbeError(E_VALIDATION_FIELD, 'anonymity.require_echo_address должен быть логическим.')
    return JudgeSpec(url=url, max_bytes=max_bytes, content_type=content_type, require_echo_address=require)


def validate_min_level(value) -> str:
    if value in (None, ''):
        return 'any'
    if not isinstance(value, str) or value not in MIN_LEVELS:
        raise ProbeError(E_VALIDATION_FIELD, 'Уровень анонимности: any, transparent, anonymous или elite.')
    return value


def require_anonymity(minimum: str, judge: JudgeSpec | None) -> JudgeSpec | None:
    """Return the judge a required level needs, or raise (defect 13).

    A required anonymity level is never silently downgraded to ``any``: the user
    either configures a judge or gets an error that says what to do.
    """
    minimum = validate_min_level(minimum)
    if minimum == 'any':
        return None
    if judge is None:
        raise ProbeError(E_VALIDATION_ANONYMITY_REQUIRED,
                         f'Требование анонимности {minimum} без judge-пробы выполнить нельзя: укажите '
                         'anonymity.url (эхо-адрес, например собственный http-эндпоинт) или снизьте '
                         'min_anonymity до any. Молча проверять без judge нельзя — уровень был бы выдуман.')
    return judge


def extract_addresses(text) -> tuple[str, ...]:
    """Every IP literal in a judge body, private ones included.

    A self-hosted judge may echo a loopback or LAN address, so the usual
    "only global addresses" filter would throw away the very answer the user
    configured.  Whether an address is ours is decided by the bootstrap set.
    """
    if isinstance(text, (bytes, bytearray)):
        text = text.decode('utf-8', errors='replace')
    found = []
    for match in _IPV4.findall(text) + _IPV6.findall(text):
        try:
            address = ipaddress.ip_address(match.strip('.:'))
        except ValueError:
            continue
        value = address.compressed
        if value not in found:
            found.append(value)
    return tuple(found)


def looks_like_challenge(text) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _CHALLENGE_MARKERS)


def reveal_signals(text) -> tuple[str, ...]:
    normalized = text.lower().replace('_', '-')
    return tuple(name for name, pattern in _HEADER_PATTERNS if pattern.search(normalized))


def labelled_exit_address(text) -> str | None:
    for match in _EXIT_IP.findall(text):
        try:
            address = ipaddress.ip_address(match.strip('.:'))
        except ValueError:
            continue
        return address.compressed
    return None


@dataclass(frozen=True)
class AnonymityOutcome:
    """A judge verdict.  ``elite`` is only reachable through a validated echo."""

    level: str
    signals: tuple[str, ...] = ()
    code: str | None = None
    exit_ip: str | None = None
    confirmed: bool = False
    ms: float | None = None

    def to_public(self) -> dict:
        return {'level': self.level, 'signals': list(self.signals), 'code': self.code,
                'exit_ip': self.exit_ip, 'confirmed': self.confirmed, 'ms': self.ms}


def classify_echo(body, own_ips, *, judge_verified=True, spec: JudgeSpec | None = None) -> AnonymityOutcome:
    """Classify one judge echo without ever promoting an unverified answer.

    ``own_ips`` is the bootstrap set from a direct judge request, already
    stripped of the judge's own server addresses.  ``judge_verified`` says that
    this direct request really did show one of our addresses — without it the
    "no leak" branch proves nothing, so the level stays ``unknown``.
    """
    if isinstance(body, (bytes, bytearray)):
        raw = bytes(body)
        text = raw.decode('utf-8', errors='replace')
    else:
        raw = b''
        text = str(body or '')
    if len(raw) > (spec.max_bytes if spec else 65_536) * 4:
        return AnonymityOutcome(level='unknown', code=JUDGE_INVALID, signals=('body_too_large',))
    if not text.strip():
        return AnonymityOutcome(level='unknown', code=JUDGE_INVALID, signals=('empty_body',))
    if looks_like_challenge(text):
        return AnonymityOutcome(level='unknown', code=JUDGE_CHALLENGE, signals=('challenge_page',))
    addresses = extract_addresses(text)
    exit_address = labelled_exit_address(text) or (addresses[0] if addresses else None)
    own = {str(item) for item in (own_ips or ())}
    leaked = [item for item in addresses if item in own]
    if leaked:
        return AnonymityOutcome(level='transparent', signals=('real_ip',), exit_ip=exit_address, confirmed=True)
    signals = reveal_signals(text)
    if signals:
        return AnonymityOutcome(level='anonymous', signals=signals, exit_ip=exit_address, confirmed=True)
    if not own:
        return AnonymityOutcome(level='unknown', code=JUDGE_UNVERIFIED, signals=('no_baseline',),
                                exit_ip=exit_address)
    if not judge_verified:
        return AnonymityOutcome(level='unknown', code=JUDGE_UNVERIFIED, signals=('judge_unverified',),
                                exit_ip=exit_address)
    # An echo that names no address proves nothing, whatever else it contains.
    # Only an explicit ``require_echo_address: false`` relaxes this, and a
    # self-hosted judge that answers without an address is the usual reason.
    require_address = True if spec is None else bool(spec.require_echo_address)
    if require_address and not exit_address:
        return AnonymityOutcome(level='unknown', code=JUDGE_INVALID, signals=('no_echo_address',))
    return AnonymityOutcome(level='elite', signals=(), exit_ip=exit_address, confirmed=True)


def anonymity_allows(value, minimum) -> bool:
    """Whether a verdict (or a row with one) meets the required level."""
    if minimum in (None, 'any'):
        return True
    if isinstance(value, AnonymityOutcome):
        level = value.level
    elif isinstance(value, dict):
        level = (value.get('anonymity') or value).get('level', 'unknown')
    else:
        level = 'unknown'
    return LEVEL_RANK.get(level, -1) >= LEVEL_RANK.get(minimum, 1)


# --------------------------------------------------------------------------
# DNSBL (defect 14)
# --------------------------------------------------------------------------

DNSBL_STATUSES = ('listed', 'clear', 'unknown')


def reverse_ip(address) -> str:
    """The DNSBL query prefix for one address.

    IPv4 is reversed octet by octet.  IPv6 is reversed nibble by nibble as
    ``x.y.z....ip6.arpa``; the old code reversed the ``address.exploded`` string
    with the colons still in it, which can never match a real zone (defect 14).
    """
    try:
        parsed = ipaddress.ip_address(str(address).strip())
    except ValueError:
        raise ValueError(f'Некорректный адрес для DNSBL: {address!r}') from None
    if parsed.version == 4:
        return '.'.join(reversed(parsed.exploded.split('.'))) + '.'
    # ``IPv6Address.exploded`` keeps the colons ("2001:0db8:0000:…"), which is
    # exactly the trap defect 14 describes: reversing it yields "0:0" labels no
    # zone can answer.  The packed 32-nibble form is what ip6.arpa expects.
    packed = f'{int(parsed):032x}'
    return '.'.join(reversed(packed)) + '.'


def reverse_host(proxy_url) -> str:
    """Reverse prefix for the address inside a proxy URL."""
    try:
        host = urlsplit(str(proxy_url)).hostname
    except ValueError:
        host = None
    if not host:
        raise ValueError(f'Некорректный адрес прокси для DNSBL: {proxy_url!r}')
    return reverse_ip(host)


@dataclass(frozen=True)
class DnsblZone:
    """One DNSBL zone with its own answer contract.

    The defaults are deliberately generic and follow RFC 5782: an answer under
    127.0.0.0/8 means the query matched, NXDOMAIN means the address is not
    listed, and 127.255.255.0/24 means the query itself was blocked.  A generic
    zone cannot know an operator's "this code means the query is unauthorized"
    range, so those are declared per zone instead of guessed at — that is what
    makes the outcome zone-specific rather than a single ``127.*`` guess.
    """

    name: str
    listed_codes: tuple[str, ...] = ()
    listed_prefixes: tuple[str, ...] = ('127.',)
    blocked_codes: tuple[str, ...] = ()
    blocked_prefixes: tuple[str, ...] = ('127.255.255.',)
    policy_codes: tuple[str, ...] = ()
    policy_prefixes: tuple[str, ...] = ()
    nxdomain: str = 'clear'

    def to_public(self) -> dict:
        return {'name': self.name, 'listed_codes': list(self.listed_codes),
                'listed_prefixes': list(self.listed_prefixes), 'blocked_prefixes': list(self.blocked_prefixes),
                'policy_prefixes': list(self.policy_prefixes), 'nxdomain': self.nxdomain}


def generic_zone(name) -> DnsblZone:
    if not isinstance(name, str):
        raise ProbeError(E_VALIDATION_FIELD, f'Некорректная DNSBL-зона: {name!r}')
    name = name.strip().lower().rstrip('.')
    if not name or len(name) > 253 or '..' in name:
        raise ProbeError(E_VALIDATION_FIELD, f'Некорректная DNSBL-зона: {name!r}')
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?', name):
        raise ProbeError(E_VALIDATION_FIELD, f'Некорректная DNSBL-зона: {name!r}')
    return DnsblZone(name=name)


def validate_dnsbl_zones(data) -> tuple[DnsblZone, ...]:
    """Accept ``["zone", …]`` or ``{"enabled": true, "zones": […]}``."""
    if data is None:
        return ()
    if isinstance(data, (list, tuple)):
        items = data
    elif isinstance(data, dict):
        _reject_unknown(data, ('enabled', 'zones'), 'DNSBL')
        if data.get('enabled') is False:
            return ()
        items = data.get('zones') or []
    else:
        raise ProbeError(E_VALIDATION_FIELD, 'DNSBL: ожидается список зон или объект с zones.')
    if not isinstance(items, (list, tuple)):
        raise ProbeError(E_VALIDATION_FIELD, 'DNSBL: zones должен быть списком.')
    if len(items) > 12:
        raise ProbeError(E_VALIDATION_FIELD, 'DNSBL: максимум 12 зон.')
    zones = []
    for item in items:
        if isinstance(item, DnsblZone):
            zone = item
        elif isinstance(item, dict):
            _reject_unknown(item, ('name', 'listed_codes', 'listed_prefixes', 'blocked_codes',
                                   'blocked_prefixes', 'policy_codes', 'policy_prefixes', 'nxdomain'), 'DNSBL: зона')
            base = generic_zone(item.get('name'))
            zone = DnsblZone(
                name=base.name,
                listed_codes=tuple(item.get('listed_codes') or ()),
                listed_prefixes=tuple(item.get('listed_prefixes') or ('127.',)),
                blocked_codes=tuple(item.get('blocked_codes') or ()),
                blocked_prefixes=tuple(item.get('blocked_prefixes') or ('127.255.255.',)),
                policy_codes=tuple(item.get('policy_codes') or ()),
                policy_prefixes=tuple(item.get('policy_prefixes') or ()),
                nxdomain=_choice(item.get('nxdomain'), 'DNSBL: nxdomain', ('clear', 'unknown'), default='clear'))
        else:
            zone = generic_zone(item)
        if zone.name not in [value.name for value in zones]:
            zones.append(zone)
    return tuple(zones)


@dataclass(frozen=True)
class DnsAnswer:
    """The answer of one DNSBL query, as the resolver saw it."""

    rcode: str = 'NOERROR'
    addresses: tuple[str, ...] = ()

    @classmethod
    def coerce(cls, value) -> 'DnsAnswer':
        """Accept what a resolver seam is likely to return."""
        if value is None:
            return cls('NXDOMAIN', ())
        if isinstance(value, DnsAnswer):
            return value
        if isinstance(value, str):
            return cls('NOERROR', (value,))
        if isinstance(value, (list, tuple, set)):
            return cls('NOERROR', tuple(str(item) for item in value))
        if isinstance(value, dict):
            return cls(str(value.get('rcode', 'NOERROR')).upper(),
                       tuple(str(item) for item in (value.get('addresses') or ())))
        raise TypeError(f'Непонятный ответ DNSBL: {type(value).__name__}')


def _match_code(zone: DnsblZone, address: str):
    if address in zone.blocked_codes or any(address.startswith(prefix) for prefix in zone.blocked_prefixes):
        return 'blocked'
    if address in zone.policy_codes or any(address.startswith(prefix) for prefix in zone.policy_prefixes):
        return 'policy'
    if address in zone.listed_codes or any(address.startswith(prefix) for prefix in zone.listed_prefixes):
        return 'listed'
    return None


@dataclass(frozen=True)
class ZoneOutcome:
    """One zone's verdict, with the code that explains a non-verdict."""

    zone: str
    status: str
    code: str | None = None
    address: str | None = None
    queried: bool = True

    def to_public(self) -> dict:
        return {'zone': self.zone, 'status': self.status, 'code': self.code,
                'address': self.address, 'queried': self.queried}


def classify_dnsbl(zone: DnsblZone, answer) -> ZoneOutcome:
    """Map one zone's answer to listed / clear / unknown plus a reason code."""
    value = DnsAnswer.coerce(answer)
    rcode = value.rcode.upper()
    if rcode in ('NXDOMAIN', 'NAMEERROR'):
        status = zone.nxdomain
        return ZoneOutcome(zone=zone.name, status=status, code=None if status == 'clear' else NO_DNSBL_ZONES)
    if rcode in ('SERVFAIL', 'REFUSED'):
        return ZoneOutcome(zone=zone.name, status='unknown', code=DNSBL_ERROR if rcode == 'SERVFAIL' else DNSBL_ACCESS)
    if rcode not in ('NOERROR', 'SUCCESS', ''):
        return ZoneOutcome(zone=zone.name, status='unknown', code=DNSBL_ERROR)
    addresses = [item for item in value.addresses if item]
    if not addresses:
        return ZoneOutcome(zone=zone.name, status='unknown', code=DNSBL_ERROR)
    for address in addresses:
        kind = _match_code(zone, address)
        if kind == 'blocked':
            return ZoneOutcome(zone=zone.name, status='unknown', code=DNSBL_ACCESS, address=address)
        if kind == 'policy':
            return ZoneOutcome(zone=zone.name, status='unknown', code=DNSBL_ACCESS, address=address)
        if kind == 'listed':
            return ZoneOutcome(zone=zone.name, status='listed', address=address)
    return ZoneOutcome(zone=zone.name, status='unknown', code=DNSBL_ERROR, address=addresses[0])


@dataclass(frozen=True)
class DnsblReport:
    """All zones for one address, plus the rolled-up status."""

    address: str
    zones: tuple[ZoneOutcome, ...]
    queries: int = 0
    truncated: bool = False

    @property
    def status(self) -> str:
        statuses = {item.status for item in self.zones}
        if 'listed' in statuses:
            return 'listed'
        if 'unknown' in statuses or not self.zones:
            return 'unknown'
        return 'clear'

    @property
    def listed(self) -> bool:
        return any(item.status == 'listed' for item in self.zones)

    def to_public(self) -> dict:
        return {'address': self.address, 'status': self.status, 'queries': self.queries,
                'truncated': self.truncated, 'zones': [item.to_public() for item in self.zones]}


class DnsQueryError(Exception):
    """A resolver-side failure with a stable code (quota, access, timeout)."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


async def check_dnsbl(address, zones, *, resolve, max_queries=None, timeout_s=None) -> DnsblReport:
    """Query every zone with a caller-supplied resolver.

    ``resolve(query)`` returns answers (anything :meth:`DnsAnswer.coerce`
    accepts) and may raise :class:`DnsQueryError` for a transport failure.  The
    resolver seam is what keeps this testable locally: the module itself never
    opens a socket.  ``timeout_s`` only bounds an awaitable resolver, because
    synchronous code cannot be interrupted from here.
    """
    prefix = reverse_ip(address)
    outcomes = []
    queries = 0
    truncated = False
    for zone in zones:
        if max_queries is not None and queries >= max_queries:
            truncated = True
            outcomes.append(ZoneOutcome(zone=zone.name, status='unknown', code=BUDGET_EXHAUSTED, queried=False))
            continue
        query = prefix + zone.name
        queries += 1
        try:
            raw = resolve(query)
            if hasattr(raw, '__await__'):
                raw = await (asyncio.wait_for(raw, timeout_s) if timeout_s is not None else raw)
            outcomes.append(classify_dnsbl(zone, raw))
        except DnsQueryError as exc:
            outcomes.append(ZoneOutcome(zone=zone.name, status='unknown', code=exc.code))
        except (TimeoutError, asyncio.TimeoutError):
            # Since 3.11 TimeoutError is an OSError, so it has to be caught
            # before the generic network clause below.
            outcomes.append(ZoneOutcome(zone=zone.name, status='unknown', code=DNSBL_TIMEOUT))
        except (OSError, ValueError, TypeError):
            outcomes.append(ZoneOutcome(zone=zone.name, status='unknown', code=DNSBL_ERROR))
    return DnsblReport(address=str(address), zones=tuple(outcomes), queries=queries, truncated=truncated)


class DnsQueryError(Exception):
    """A resolver-side failure with a stable code (quota, access, timeout)."""

    def __init__(self, code):
        super().__init__(code)
        self.code = code


def dnsbl_blocks(report, strict=False) -> bool:
    """Whether a DNSBL report forbids the address."""
    if report is None:
        return bool(strict)
    if isinstance(report, dict):
        status = report.get('status')
    else:
        status = report.status
    return status == 'listed' or (strict and status == 'unknown')


# --------------------------------------------------------------------------
# recheck planning
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RecheckItem:
    proxy: str
    reason: str
    age_seconds: float | None = None

    def to_public(self) -> dict:
        return {'proxy': self.proxy, 'reason': self.reason, 'age_seconds': self.age_seconds}


RECHECK_REASONS = ('no_measurement', 'time_unknown', 'clock_anomaly', 'expired', 'forced', 'failed_before')

# Broken time first, then expired, then what was measured and simply needs a
# fresh look.  The order is a contract: it decides what a limited budget covers.
_RECHECK_PRIORITY = {'clock_anomaly': 0, 'time_unknown': 1, 'no_measurement': 1, 'expired': 2,
                     'failed_before': 3, 'forced': 4}


def plan_recheck(rows, *, now, max_age_s, limit=0, remeasure_passing=False, passing_threshold=2 / 3) -> tuple[RecheckItem, ...]:
    """Which rows must be measured again before they may be published.

    A row with no usable measurement time is re-measured instead of being
    treated as fresh, and passing rows are only re-measured when the caller
    asked for it — which is what "повторно проверять истёкшие" means
    (defect 2) without a second scan engine.
    """
    items = []
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        proxy = row.get('proxy')
        if not proxy:
            continue
        checked = row.get('checked_at')
        valid_until = row.get('valid_until')
        try:
            checked_at = float(checked) if checked not in (None, '') else None
        except (TypeError, ValueError):
            checked_at = None
        if checked_at is None:
            items.append(RecheckItem(proxy, 'time_unknown', None))
            continue
        if checked_at > float(now) + 1.0:
            items.append(RecheckItem(proxy, 'clock_anomaly', round(float(now) - checked_at, 3)))
            continue
        age = float(now) - checked_at
        if valid_until not in (None, ''):
            try:
                if float(valid_until) <= float(now):
                    items.append(RecheckItem(proxy, 'expired', round(age, 3)))
                    continue
            except (TypeError, ValueError):
                items.append(RecheckItem(proxy, 'time_unknown', round(age, 3)))
                continue
        elif age > float(max_age_s):
            items.append(RecheckItem(proxy, 'expired', round(age, 3)))
            continue
        if remeasure_passing:
            try:
                reliability = float(row.get('min_target_reliability', 0))
            except (TypeError, ValueError):
                reliability = 0.0
            if reliability + 1e-12 < passing_threshold:
                items.append(RecheckItem(proxy, 'failed_before', round(age, 3)))
            else:
                items.append(RecheckItem(proxy, 'forced', round(age, 3)))
    items.sort(key=lambda item: (_RECHECK_PRIORITY.get(item.reason, 9),
                                 -(item.age_seconds if item.age_seconds is not None else 0.0),
                                 item.proxy))
    if limit and limit > 0:
        items = items[:limit]
    return tuple(items)


# --------------------------------------------------------------------------
# capability matrix and the self-hosted reference probe (F20)
# --------------------------------------------------------------------------

# Every measurement kind gets a row, whether or not this module implements it.
# F20 forbids advertising a capability that a plain HTTP GET does not prove, so
# anything without a controllable endpoint and a budget stays "not supported"
# until an adapter with its own tests exists.
CAPABILITIES = (
    {'id': 'http_transfer', 'supported': True, 'kind': 'target',
     'endpoint': 'any validated target profile', 'budget': 'max_body_bytes, whole_probe_timeout_s',
     'outcome': 'TargetOutcome'},
    {'id': 'https_via_connect', 'supported': True, 'kind': 'target',
     'endpoint': 'an https:// target, TLS verified', 'budget': 'handshake_timeout_s, read_timeout_s',
     'outcome': 'TargetOutcome'},
    {'id': 'bandwidth', 'supported': True, 'kind': 'speed',
     'endpoint': 'SpeedTarget', 'budget': 'max_bytes, SpeedLimits.min_bytes/min_seconds/min_chunks',
     'outcome': 'SpeedMeasurement (ok | insufficient | error)'},
    {'id': 'anonymity_judge', 'supported': True, 'kind': 'judge',
     'endpoint': 'JudgeSpec, validated echo required', 'budget': 'max_bytes, attempts',
     'outcome': 'AnonymityOutcome (elite only after a confirmed echo)'},
    {'id': 'dns_reputation', 'supported': True, 'kind': 'dnsbl',
     'endpoint': 'DnsblZone list', 'budget': 'max 12 zones, max_queries',
     'outcome': 'DnsblReport (listed | clear | unknown + code)'},
    {'id': 'tcp_and_handshake', 'supported': True, 'kind': 'stage',
     'endpoint': 'the proxy address itself', 'budget': 'connect_timeout_s, attempts',
     'outcome': 'TargetOutcome with stage tcp/handshake, never a transfer claim'},
    {'id': 'websocket_handshake', 'supported': False, 'kind': 'target',
     'endpoint': '—', 'budget': '—', 'outcome': 'нет: объявление без измерения запрещено (F20)'},
    {'id': 'long_lived_connection', 'supported': False, 'kind': 'target',
     'endpoint': '—', 'budget': '—', 'outcome': 'нет: нет собственного измерителя длительности соединения'},
    {'id': 'media_manifest_segment', 'supported': False, 'kind': 'target',
     'endpoint': '—', 'budget': '—', 'outcome': 'нет: нужен отдельный контракт и адаптер каталога сервисов'},
    {'id': 'udp_transport', 'supported': False, 'kind': 'stage',
     'endpoint': '—', 'budget': '—', 'outcome': 'нет: HTTP GET не доказывает поддержку UDP'},
    {'id': 'http2_or_http3', 'supported': False, 'kind': 'stage',
     'endpoint': '—', 'budget': '—', 'outcome': 'нет: версия протокола не измеряется этим модулем'},
)


def capability_matrix():
    """Honest list of what is measured; F20 forbids promising more than this."""
    return tuple(dict(item) for item in CAPABILITIES)


REFERENCE_PROBE_MAX_BYTES = 8 * 1024 * 1024
_REFERENCE_CHUNK = 64 * 1024


def _reference_targets(base_url: str) -> tuple[dict, ...]:
    """Target profiles pointing at a running reference probe."""
    return (
        {'id': 'reference-health', 'name': 'Reference probe: health', 'kind': 'custom',
         'url': f'{base_url}/health', 'statuses': [200], 'content_type': 'application/json',
         'min_body_bytes': 2, 'max_body_bytes': 65_536,
         'json_assertions': [{'path': 'status', 'op': 'equals', 'value': 'ok'},
                             {'path': 'service', 'op': 'type', 'value': 'string'}]},
        {'id': 'reference-echo', 'name': 'Reference probe: judge echo', 'kind': 'custom',
         'url': f'{base_url}/echo', 'statuses': [200], 'content_type': 'application/json',
         'min_body_bytes': 2, 'max_body_bytes': 65_536,
         'json_assertions': [{'path': 'origin', 'op': 'exists'},
                             {'path': 'origin', 'op': 'type', 'value': 'string'}]},
    )


def reference_probe_targets(base_url: str) -> tuple[TargetProfile, ...]:
    return tuple(validate_target(item) for item in _reference_targets(base_url.rstrip('/')))


def _reference_handler_class(version: str):
    from http.server import BaseHTTPRequestHandler

    service = f'proxy-workbench-reference/{version}'

    class Handler(BaseHTTPRequestHandler):
        """A tiny local endpoint: health, an echo judge, bounded bytes, redirects."""

        protocol_version = 'HTTP/1.1'
        server_version = service
        sys_version = ''

        def log_message(self, *args):  # the reference probe must not spam the console
            return

        def _send(self, status, body=b'', content_type='application/json', extra=()):
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            for name, value in extra:
                self.send_header(name, value)
            self.end_headers()
            if body and self.command != 'HEAD':
                self.wfile.write(body)

        def do_HEAD(self):
            self._route(body=False)

        def do_GET(self):
            self._route(body=True)

        def _route(self, body):
            path = urlsplit(self.path).path
            if path == '/health':
                payload = json.dumps({'service': service, 'status': 'ok',
                                      'reference_probe': {'version': version}})
                return self._send(200, payload.encode())
            if path == '/echo':
                origin = self.client_address[0] if self.client_address else ''
                payload = json.dumps({'service': service, 'origin': origin, 'path': path,
                                      'headers': {name: value for name, value in self.headers.items()}})
                return self._send(200, payload.encode())
            if path.startswith('/bytes/'):
                raw = path[len('/bytes/'):]
                if not raw.isdigit():
                    return self._send(400, b'{"error":"size"}')
                size = min(int(raw), REFERENCE_PROBE_MAX_BYTES)
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Length', str(size))
                self.end_headers()
                if body:
                    block = b'0' * _REFERENCE_CHUNK
                    left = size
                    while left > 0:
                        self.wfile.write(block[:min(left, _REFERENCE_CHUNK)])
                        left -= _REFERENCE_CHUNK
                return None
            if path.startswith('/redirect/'):
                raw = path[len('/redirect/'):]
                if raw.isdigit() and int(raw) > 0:
                    return self._send(302, b'', 'text/plain', (('Location', f'/redirect/{int(raw) - 1}'),))
                return self._send(302, b'', 'text/plain', (('Location', '/health'),))
            return self._send(404, b'{"error":"not_found"}')

    return Handler


def serve_reference_probe(host='127.0.0.1', port=0, *, version=1):
    """Run the reference probe in a background thread; yields its base URL.

    This is the self-hosted endpoint F20 asks for: a user can point the
    workbench at their own machine and verify a profile without any cloud
    service.  It binds to loopback by default and serves bounded bodies only.
    """
    import contextlib
    import threading
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer((host, port), _reference_handler_class(str(version)))
    server.daemon_threads = True
    # A short poll interval keeps shutdown() from adding half a second to every
    # caller that starts and stops the probe around a single check.
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.02},
                              name='reference-probe', daemon=True)
    thread.start()

    @contextlib.contextmanager
    def running():
        try:
            yield {'url': f'http://{server.server_address[0]}:{server.server_address[1]}',
                   'host': server.server_address[0], 'port': server.server_address[1],
                   'version': str(version), 'service': f'proxy-workbench-reference/{version}'}
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    return running()
