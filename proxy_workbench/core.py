"""Unified admission contract: which measured endpoint a consumer may use right now.

This module is the single place that decides admission.  CONTRACTS.ru.md §2
specifies it, F09 and R01 require it, and every surface (CLI, GUI, API,
gateway, export) is supposed to call the same function instead of keeping its
own selection logic.

    admit(row, scope, access, policy, now) -> Admission
    select(rows, scope, access, policy, now) -> Selection

Design rules that are part of the contract, not implementation detail:

* ``checked_at`` is measurement time and the only age source.  ``published_at``
  is publication time; it is echoed to the consumer and never enters the TTL
  arithmetic (defect 2, second half).
* ``valid_until`` is written once, at measurement time, as
  ``checked_at + policy.max_age_seconds``.  Re-exporting with another policy
  never moves it.
* Anomaly states are explicit.  Unknown time, a future timestamp and a clock
  rollback are all rejections with their own reason code; none of them is
  silently turned into "fresh" (defect 4, §2.4).
* The check order is fixed and therefore the ``reason_code`` is reproducible:
  identity → observation → clock → time → exclusions → quality → capabilities.
* A row is judged on its own.  Selection never takes ``min()`` across rows, so
  one expired member cannot hide the rest of a set (defect 3).
* The last completed measurement is never dropped and history is only ever
  appended; a fresh failure cannot be masked by an older success (F09).

The module is a leaf: standard library only, no database, no DDL, no network
and no import of ``proxytool``.  Proxy normalization, denylist matching and
country lookup stay with their owners and are injected as callables.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

__all__ = [
    'Admission', 'Access', 'AdmissionEngine', 'AdmissionError', 'ClockState',
    'Generation', 'Measurement', 'Policy', 'Scope', 'Selection',
    'CAPABILITIES', 'DEFAULT_MAX_AGE_SECONDS', 'LEGACY_TTL_SECONDS',
    'OBSERVATION_STATES', 'REASON_CODES', 'SELECTION_STATES',
    'STATIC_TTL_NOTICE', 'STATE_DETAILS', 'TIME_STATES',
    'admit', 'apply_measurement', 'capabilities_of', 'history_of',
    'observation_state', 'pin_generation', 'row_country', 'row_protocol',
    'select', 'time_state_of',
]

# Starting value only.  F09 forbids presenting 300 s or 7200 s as a proven
# optimum; the effective number comes from the profile revision and is reported
# to the user through ``Selection.as_status()['max_age_seconds']``.
DEFAULT_MAX_AGE_SECONDS = 900.0

# Mirrors ``proxytool.MIN_FRESHNESS_SECONDS`` (proxytool.py:420) and is used for
# one thing only: giving a legacy row that never recorded a deadline a
# synthetic one, flagged as ``ttl_backfilled`` (§2.4).
LEGACY_TTL_SECONDS = 2 * 60 * 60

TIME_OK = 'time_ok'
TIME_UNKNOWN = 'time_unknown'
TIME_FUTURE = 'time_future'
CLOCK_ROLLBACK = 'clock_rollback'
TIME_TTL_MISSING = 'time_ttl_missing'
TIME_EXPIRED = 'time_expired'
TIME_STATES = frozenset({TIME_OK, TIME_UNKNOWN, TIME_FUTURE, CLOCK_ROLLBACK,
                         TIME_TTL_MISSING, TIME_EXPIRED})
TIME_REASONS = frozenset({'E_TIME_UNKNOWN', 'E_TIME_FUTURE', 'E_TIME_CLOCK_ROLLBACK',
                          'E_TIME_TTL_EXPIRED', 'E_TIME_TTL_MISSING'})

OBSERVATION_MISSING = 'observation_missing'
OBSERVATION_FAILED = 'observation_failed'
OBSERVATION_OK = 'observation_ok'
OBSERVATION_STATES = frozenset({OBSERVATION_MISSING, OBSERVATION_FAILED, OBSERVATION_OK})

SELECTION_COMPLETE = 'complete'
SELECTION_PARTIAL = 'partial'
SELECTION_STALE = 'stale'
SELECTION_EMPTY = 'empty'
SELECTION_STATES = frozenset({SELECTION_COMPLETE, SELECTION_PARTIAL,
                              SELECTION_STALE, SELECTION_EMPTY})

DETAIL_OK = 'ok'
DETAIL_REJECTED = 'rejected'
DETAIL_ALL_EXPIRED = 'all_expired'
DETAIL_ALL_UNTRUSTED = 'all_untrusted'
DETAIL_ALL_FAILED = 'all_failed'
DETAIL_NOTHING_IN_SCOPE = 'nothing_in_scope'
DETAIL_NO_MATCH = 'empty_no_match'
STATE_DETAILS = frozenset({DETAIL_OK, DETAIL_REJECTED, DETAIL_ALL_EXPIRED, DETAIL_ALL_UNTRUSTED,
                           DETAIL_ALL_FAILED, DETAIL_NOTHING_IN_SCOPE, DETAIL_NO_MATCH})

# A published static file cannot take its own TTL back, so the promise is shown
# as text instead of being made silently (F09, §2.4).
STATIC_TTL_NOTICE = 'E_STATE_SNAPSHOT_STATIC_TTL'

# Capability names this module can derive.  A requirement outside the closed set
# is a bug in the caller and is rejected, not ignored (F07).
CAPABILITIES = frozenset({'tcp', 'http', 'https', 'socks4', 'socks5', 'udp',
                          'anonymity:transparent', 'anonymity:anonymous',
                          'anonymity:elite', 'speed', 'exit_ip'})

ANONYMITY_RANK = {'unknown': -1, 'transparent': 0, 'anonymous': 1, 'elite': 2}
ANONYMITY_MINIMUMS = frozenset({'any', 'anonymous', 'elite'})
UNKNOWN_COUNTRIES = frozenset({'exclude', 'include', 'require'})

REASON_CODES = {
    # identity: the row was measured under conditions the request does not accept
    'E_SCOPE_COLLECTION': 'row belongs to another collection',
    'E_SCOPE_PROFILE_REVISION': 'row belongs to another profile revision',
    'E_SCOPE_NETWORK': 'row was measured on another network',
    'E_CONFLICT_ACCESS_REVISION': 'row was measured with another access revision',
    # observation
    'E_STATE_NO_OBSERVATION': 'no completed measurement for this row',
    'E_STATE_MEASUREMENT_FAILED': 'the last completed measurement failed',
    'E_STATE_MIN_SUCCESS': 'reliability below the policy threshold',
    'E_STATE_ANONYMITY': 'anonymity below the policy minimum',
    'E_STATE_REPUTATION_LISTED': 'reputation verdict is listed',
    'E_STATE_REPUTATION_UNKNOWN': 'reputation is unknown and the policy is strict',
    'E_STATE_LATENCY': 'latency above the policy maximum',
    # time
    'E_TIME_UNKNOWN': 'checked_at is missing or not a number',
    'E_TIME_FUTURE': 'checked_at lies in the future',
    'E_TIME_CLOCK_ROLLBACK': 'the clock moved backwards for this endpoint',
    'E_TIME_TTL_EXPIRED': 'the row TTL has expired',
    'E_TIME_TTL_MISSING': 'no TTL was recorded and legacy backfill is off',
    # exclusions
    'E_SCOPE_DENYLIST': 'endpoint is on the local deny list',
    'E_SCOPE_PROTOCOL': 'endpoint protocol is not requested',
    'E_SCOPE_COUNTRY': 'endpoint country is not requested or is unknown',
    'E_SCOPE_HOSTING': 'endpoint is a hosting provider and hosting is excluded',
    'E_SCOPE_CAPABILITY': 'a required capability is not demonstrated',
}


class AdmissionError(ValueError):
    """Invalid contract input or an unusable snapshot; carries a canonical code."""

    def __init__(self, code: str, message: str = ''):
        super().__init__(message or code)
        self.code = code


def _number(value: Any) -> float | None:
    """A strictly positive finite float, or None.  Booleans are not numbers here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number > 0 else None


def _text(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


@dataclass(frozen=True)
class Scope:
    """What a consumer is asking about: collection, profile revision, network.

    ``network_id`` is the measurement environment the consumer is on.  A row
    measured on another network is not evidence for this one, exactly like a
    row measured with another password (F09: смена сети и смена доступа влияют
    на подбор одинаково).
    """
    collection_id: str
    profile_id: str
    profile_revision: int
    network_id: str = 'default'


@dataclass(frozen=True)
class Access:
    """The way to reach the endpoint, plus the revision of that way."""
    access_id: str
    access_revision: int
    endpoint_id: str | None = None


@dataclass(frozen=True)
class Policy:
    """Everything a profile revision fixes about admission.

    All thresholds live here and not in a read-time argument, so one snapshot
    cannot be a pass for one caller and empty for another (CONTRACTS §2.3).
    """
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS
    min_success: float = 2 / 3
    min_anonymity: str = 'any'
    strict: bool = False
    protocol: str | None = None
    countries: frozenset[str] = frozenset()
    unknown_country: str = 'exclude'
    exclude_hosting: bool = False
    denied: frozenset[str] = frozenset()
    max_latency_ms: float | None = None
    required_capabilities: frozenset[str] = frozenset()
    future_tolerance_seconds: float = 0.0
    backfill_legacy_ttl: bool = True
    allow_missing_identity: bool = False
    deny_match: Callable[[str], bool] | None = None
    protocol_of: Callable[[str], str | None] | None = None
    country_of: Callable[[str], str | None] | None = None
    hosting_of: Callable[[str], bool] | None = None

    def __post_init__(self) -> None:
        if not _number(self.max_age_seconds):
            raise AdmissionError('E_VALIDATION_FIELD', 'max_age_seconds must be a positive number')
        if not isinstance(self.min_success, (int, float)) or not 0 < self.min_success <= 1:
            raise AdmissionError('E_VALIDATION_FIELD', 'min_success must be within (0, 1]')
        if self.min_anonymity not in ANONYMITY_MINIMUMS:
            raise AdmissionError('E_VALIDATION_FIELD', 'min_anonymity must be any/anonymous/elite')
        if self.unknown_country not in UNKNOWN_COUNTRIES:
            raise AdmissionError('E_VALIDATION_FIELD', 'unknown_country must be exclude/include/require')
        if self.max_latency_ms is not None and not _number(self.max_latency_ms):
            raise AdmissionError('E_VALIDATION_FIELD', 'max_latency_ms must be a positive number')
        if (isinstance(self.future_tolerance_seconds, bool)
                or not isinstance(self.future_tolerance_seconds, (int, float))
                or self.future_tolerance_seconds < 0):
            raise AdmissionError('E_VALIDATION_FIELD', 'future_tolerance_seconds must not be negative')
        unknown = set(self.required_capabilities) - CAPABILITIES
        if unknown:
            raise AdmissionError('E_VALIDATION_FIELD', f'unknown capabilities: {sorted(unknown)}')


@dataclass(frozen=True)
class ClockState:
    """What the engine remembers about the clock of one endpoint."""
    high_water: float | None = None
    rollback_at: float | None = None


@dataclass(frozen=True)
class Admission:
    """One decision, with everything a consumer needs to explain it."""
    endpoint_id: str
    admitted: bool
    reason_code: str | None
    time_state: str
    observation_state: str
    age_seconds: float | None
    checked_at: float | None
    valid_until: float | None
    published_at: float | None
    max_age_seconds: float
    ttl_backfilled: bool = False
    detail: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """The public row fields: age, admission reason and the applied policy."""
        return {
            'admitted': self.admitted,
            'admission_reason': self.reason_code,
            'time_state': self.time_state,
            'observation_state': self.observation_state,
            'age_seconds': self.age_seconds,
            'checked_at': self.checked_at,
            'valid_until': self.valid_until,
            'published_at': self.published_at,
            'max_age_seconds': self.max_age_seconds,
            'ttl_backfilled': self.ttl_backfilled,
            'detail': dict(self.detail),
        }


@dataclass(frozen=True)
class Selection:
    """The admitted set plus the reason every other row was dropped."""
    admissions: tuple[Admission, ...]
    admitted: tuple[Mapping[str, Any], ...]
    counts: Mapping[str, int]
    state: str
    state_detail: str
    max_age_seconds: float
    now: float
    expires_at: float | None = None
    published_at: float | None = None
    generation: str | None = None
    static: bool = False

    @property
    def considered(self) -> int:
        return len(self.admissions)

    @property
    def rejected(self) -> tuple[Admission, ...]:
        return tuple(item for item in self.admissions if not item.admitted)

    @property
    def parity_pairs(self) -> tuple[tuple[str, str], ...]:
        """Exact definition of "the same set" used by the parity check (§2.3)."""
        return tuple(sorted((item.endpoint_id, item.reason_code or 'OK')
                            for item in self.admissions))

    @property
    def content_digest(self) -> str:
        """Digest of the admitted composition; independent of publication time."""
        payload = '\n'.join(f'{name}\t{reason}' for name, reason in self.parity_pairs)
        return hashlib.sha256(payload.encode('utf-8')).hexdigest()

    def as_status(self) -> dict[str, Any]:
        """Fields for ``status.json`` / ``/v1/status`` (§4.3)."""
        return {
            'state': self.state,
            'state_detail': self.state_detail,
            'max_age_seconds': self.max_age_seconds,
            'expires_at': self.expires_at,
            'checked': len(self.admitted),
            'admitted': len(self.admitted),
            'rejected': len(self.rejected),
            'admission_counts': dict(self.counts),
            'generation': self.generation,
            'published_at': self.published_at,
            'static_ttl_enforced': not self.static,
            'static_ttl_notice': STATIC_TTL_NOTICE if self.static else None,
        }


@dataclass(frozen=True)
class Generation:
    """One published snapshot identity.  A generation name is never a freshness input."""
    name: str
    schema_version: int
    published_at: float | None = None
    state: str = 'complete'

    def __post_init__(self) -> None:
        if not self.name or self.name in ('.', '..') or '/' in self.name or '\\' in self.name:
            raise AdmissionError('E_VALIDATION_FIELD', f'bad generation name: {self.name!r}')
        if not isinstance(self.schema_version, int) or isinstance(self.schema_version, bool):
            raise AdmissionError('E_VALIDATION_FIELD', 'schema_version must be an integer')


@dataclass(frozen=True)
class Measurement:
    """One finished (or explicitly unfinished) attempt at an endpoint."""
    endpoint_id: str
    checked_at: float | None = None
    verdict: Mapping[str, Any] | None = None
    error: Mapping[str, Any] | str | None = None
    observation_id: str | None = None
    job_id: str | None = None
    network_id: str | None = None
    completed: bool = True
    ok: bool | None = None

    @property
    def succeeded(self) -> bool:
        if self.ok is not None:
            return bool(self.ok)
        if not self.completed or self.error:
            return False
        return float((self.verdict or {}).get('min_target_reliability') or 0) > 0


def observation_state(row: Mapping[str, Any] | None) -> str:
    """missing / failed / ok for one row, based on the latest observation only."""
    if not isinstance(row, Mapping) or not row:
        return OBSERVATION_MISSING
    if row.get('error') or row.get('error_code'):
        return OBSERVATION_FAILED
    raw = row.get('min_target_reliability', row.get('reliability'))
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and math.isfinite(raw):
        return OBSERVATION_OK if raw > 0 else OBSERVATION_FAILED
    if row.get('checked_at') is not None or row.get('observation_id') or row.get('history'):
        return OBSERVATION_OK
    return OBSERVATION_MISSING


def time_state_of(row: Mapping[str, Any] | None, now: float, policy: Policy,
                  clock: ClockState = ClockState()) -> dict[str, Any]:
    """Classify the time axis of one row: state, age and effective deadline."""
    checked_at = _number(row.get('checked_at')) if isinstance(row, Mapping) else None
    stored = _number(row.get('valid_until')) if isinstance(row, Mapping) else None
    result = {'checked_at': checked_at, 'valid_until': stored, 'ttl_backfilled': False}
    if clock.rollback_at is not None or (clock.high_water is not None and now < clock.high_water):
        return {**result, 'state': CLOCK_ROLLBACK, 'age': None if checked_at is None else now - checked_at}
    if checked_at is None:
        return {**result, 'state': TIME_UNKNOWN, 'age': None}
    age = now - checked_at
    if checked_at > now + policy.future_tolerance_seconds:
        return {**result, 'state': TIME_FUTURE, 'age': age}
    if stored is None:
        if not policy.backfill_legacy_ttl:
            return {**result, 'state': TIME_TTL_MISSING, 'age': age}
        stored = checked_at + LEGACY_TTL_SECONDS
        result['ttl_backfilled'] = True
    if stored <= now:
        return {**result, 'valid_until': stored, 'state': TIME_EXPIRED, 'age': age}
    return {**result, 'valid_until': stored, 'state': TIME_OK, 'age': age}


def row_protocol(row: Mapping[str, Any], policy: Policy) -> str | None:
    """Protocol from the row; a resolver is injected because core does not normalize."""
    protocol = _text(row.get('protocol'))
    if protocol:
        return protocol
    if policy.protocol_of:
        return _text(policy.protocol_of(str(row.get('proxy') or '')))
    return None


def row_country(row: Mapping[str, Any], policy: Policy) -> str | None:
    """Country from the row; unknown stays unknown and is never counted as verified."""
    country = _text(row.get('country'))
    if country:
        return country.upper()
    if policy.country_of:
        return _text(policy.country_of(str(row.get('proxy') or '')))
    return None


def capabilities_of(row: Mapping[str, Any]) -> frozenset[str]:
    """Only what the row actually demonstrates; nothing is inferred from the address."""
    if not isinstance(row, Mapping) or not row:
        return frozenset()
    found = set()
    error = row.get('error') or row.get('error_code')
    code = error.get('code') if isinstance(error, Mapping) else error
    stage = error.get('stage') if isinstance(error, Mapping) else None
    if code != 'UNREACHABLE' and stage != 'tcp':
        found.add('tcp')
        protocol = _text(row.get('protocol'))
        if protocol in ('http', 'https', 'socks4', 'socks5'):
            found.add(protocol)
    level = (row.get('anonymity') or {}).get('level') if isinstance(row.get('anonymity'), Mapping) else None
    if level in ANONYMITY_RANK:
        found.add(f'anonymity:{level}')
    speed = row.get('speed') if isinstance(row.get('speed'), Mapping) else {}
    if speed.get('state') == 'ok' or _number(speed.get('mbps')) is not None:
        found.add('speed')
    if _text(row.get('exit_ip')):
        found.add('exit_ip')
    declared = row.get('capabilities')
    if isinstance(declared, (list, tuple, set, frozenset)):
        found.update(item for item in declared if item in CAPABILITIES)
    return frozenset(found)


def _identity_reason(row: Mapping[str, Any], scope: Scope, access: Access,
                     policy: Policy) -> tuple[str, dict[str, Any]] | None:
    """Scope, profile revision, network and access revision, in that order."""
    checks = (
        ('collection_id', scope.collection_id, 'E_SCOPE_COLLECTION'),
        ('profile_id', scope.profile_id, 'E_SCOPE_PROFILE_REVISION'),
        ('profile_revision', scope.profile_revision, 'E_SCOPE_PROFILE_REVISION'),
        ('network_id', scope.network_id, 'E_SCOPE_NETWORK'),
        ('access_id', access.access_id, 'E_CONFLICT_ACCESS_REVISION'),
        ('access_revision', access.access_revision, 'E_CONFLICT_ACCESS_REVISION'),
    )
    for field, wanted, code in checks:
        if field in ('profile_revision', 'access_revision'):
            got, expected = row.get(field), wanted
        else:
            got, expected = _text(row.get(field)), _text(wanted)
        if got is None or (isinstance(got, str) and not got):
            if policy.allow_missing_identity:
                continue
            return code, {field: None, 'required': expected, 'missing': True}
        if got != expected:
            return code, {field: got, 'required': expected}
    return None


def _quality_reason(row: Mapping[str, Any], policy: Policy) -> tuple[str, dict[str, Any]] | None:
    """Exclusions first, then reputation, anonymity, latency, reliability, capabilities."""
    proxy = str(row.get('proxy') or '')
    if proxy in policy.denied or (policy.deny_match and policy.deny_match(proxy)):
        return 'E_SCOPE_DENYLIST', {'proxy': proxy}
    if policy.protocol not in (None, 'all'):
        protocol = row_protocol(row, policy)
        if protocol != policy.protocol:
            return 'E_SCOPE_PROTOCOL', {'protocol': protocol, 'required': policy.protocol}
    if policy.countries:
        country = row_country(row, policy)
        if country is None:
            if policy.unknown_country != 'include':
                return 'E_SCOPE_COUNTRY', {'country': None, 'policy': policy.unknown_country}
        elif country not in policy.countries:
            return 'E_SCOPE_COUNTRY', {'country': country, 'required': sorted(policy.countries)}
    if policy.exclude_hosting:
        hosting = row.get('hosting')
        if hosting is None and policy.hosting_of:
            hosting = policy.hosting_of(proxy)
        if hosting:
            return 'E_SCOPE_HOSTING', {'provider': row.get('provider')}
    verdict = row.get('reputation') if isinstance(row.get('reputation'), Mapping) else None
    status = (verdict or {}).get('status')
    if status == 'listed':
        return 'E_STATE_REPUTATION_LISTED', {'status': status}
    if status == 'local_denied':
        return 'E_SCOPE_DENYLIST', {'status': status}
    if policy.strict and status != 'clean':
        return 'E_STATE_REPUTATION_UNKNOWN', {'status': status or 'unknown'}
    if policy.min_anonymity != 'any':
        level = (row.get('anonymity') or {}).get('level', 'unknown') if isinstance(row.get('anonymity'), Mapping) else 'unknown'
        if ANONYMITY_RANK.get(level, -1) < ANONYMITY_RANK[policy.min_anonymity]:
            return 'E_STATE_ANONYMITY', {'level': level, 'required': policy.min_anonymity}
    if policy.max_latency_ms is not None:
        latency = _number(row.get('latency_ms'))
        if latency is None or latency > policy.max_latency_ms:
            return 'E_STATE_LATENCY', {'latency_ms': row.get('latency_ms'),
                                       'required': policy.max_latency_ms}
    reliability = row.get('min_target_reliability', row.get('reliability'))
    if isinstance(reliability, bool):
        return 'E_STATE_MIN_SUCCESS', {'reliability': reliability, 'required': policy.min_success}
    try:
        reliability = float(reliability)
    except (TypeError, ValueError):
        return 'E_STATE_MIN_SUCCESS', {'reliability': reliability, 'required': policy.min_success}
    if reliability <= 0 or reliability + 1e-12 < policy.min_success:
        return 'E_STATE_MIN_SUCCESS', {'reliability': reliability, 'required': policy.min_success}
    missing = set(policy.required_capabilities) - capabilities_of(row)
    if missing:
        return 'E_SCOPE_CAPABILITY', {'missing': sorted(missing)}
    return None


def admit(row: Mapping[str, Any] | None, scope: Scope, access: Access, policy: Policy,
          now: float, *, published_at: float | None = None,
          clock: ClockState = ClockState()) -> Admission:
    """Judge one row.  The check order below is part of the contract.

    no evidence → identity → clock → time → exclusions/quality → capabilities
    """
    data = row if isinstance(row, Mapping) else {}
    endpoint = str(data.get('endpoint_id') or data.get('proxy') or '')
    state = observation_state(data)
    timing = time_state_of(data, now, policy, clock)
    age = timing['age']

    def build(admitted: bool, reason: str | None, detail: Mapping[str, Any] | None = None) -> Admission:
        return Admission(endpoint_id=endpoint, admitted=admitted, reason_code=reason,
                         time_state=timing['state'], observation_state=state,
                         age_seconds=age, checked_at=timing['checked_at'],
                         valid_until=timing['valid_until'], published_at=published_at,
                         max_age_seconds=policy.max_age_seconds,
                         ttl_backfilled=timing['ttl_backfilled'], detail=dict(detail or {}))

    if state == OBSERVATION_MISSING:
        return build(False, 'E_STATE_NO_OBSERVATION', {'endpoint': endpoint})
    mismatch = _identity_reason(data, scope, access, policy)
    if mismatch:
        return build(False, *mismatch)
    if timing['state'] == CLOCK_ROLLBACK:
        return build(False, 'E_TIME_CLOCK_ROLLBACK',
                     {'high_water': clock.high_water, 'rollback_at': clock.rollback_at})
    if timing['state'] == TIME_UNKNOWN:
        return build(False, 'E_TIME_UNKNOWN', {'checked_at': data.get('checked_at')})
    if timing['state'] == TIME_FUTURE:
        return build(False, 'E_TIME_FUTURE', {'checked_at': timing['checked_at'], 'now': now})
    if timing['state'] == TIME_TTL_MISSING:
        return build(False, 'E_TIME_TTL_MISSING', {'checked_at': timing['checked_at']})
    if timing['state'] == TIME_EXPIRED:
        return build(False, 'E_TIME_TTL_EXPIRED', {'valid_until': timing['valid_until']})
    if state == OBSERVATION_FAILED:
        return build(False, 'E_STATE_MEASUREMENT_FAILED',
                     {'error': data.get('error') or data.get('error_code')})
    reason = _quality_reason(data, policy)
    if reason:
        return build(False, *reason)
    return build(True, None)


def select(rows: Iterable[Mapping[str, Any]], scope: Scope, access: Access, policy: Policy,
           now: float, *, published_at: float | None = None, generation: str | None = None,
           static: bool = False, engine: 'AdmissionEngine | None' = None) -> Selection:
    """Filter row by row.  No cross-row ``min()``: one expired member never hides the rest."""
    worker = engine or AdmissionEngine(policy)
    admitted: list[Mapping[str, Any]] = []
    admissions: list[Admission] = []
    counts: dict[str, int] = {}
    for row in rows:
        result = worker.admit(row, scope, access, now, published_at=published_at)
        admissions.append(result)
        if result.admitted:
            admitted.append(row)
        else:
            counts[result.reason_code] = counts.get(result.reason_code, 0) + 1
    if not admitted:
        state, detail = _empty_state(admissions)
    elif counts:
        state, detail = SELECTION_PARTIAL, DETAIL_REJECTED
    else:
        state, detail = SELECTION_COMPLETE, DETAIL_OK
    # The set lives as long as its newest admitted member, never as long as its
    # oldest: ``min()`` here is exactly defect 3.
    expires_at = max((item.valid_until for item in admissions
                      if item.admitted and item.valid_until is not None), default=None)
    return Selection(admissions=tuple(admissions), admitted=tuple(admitted), counts=counts,
                     state=state, state_detail=detail, max_age_seconds=policy.max_age_seconds,
                     now=now, expires_at=expires_at, published_at=published_at,
                     generation=generation, static=static)


def _empty_state(admissions: Sequence[Admission]) -> tuple[str, str]:
    if not admissions:
        return SELECTION_EMPTY, DETAIL_NOTHING_IN_SCOPE
    reasons = {item.reason_code for item in admissions}
    if reasons <= TIME_REASONS:
        # Expired and "cannot be trusted" are different user-facing situations.
        if reasons == {'E_TIME_TTL_EXPIRED'}:
            return SELECTION_STALE, DETAIL_ALL_EXPIRED
        return SELECTION_STALE, DETAIL_ALL_UNTRUSTED
    if reasons == {'E_STATE_MEASUREMENT_FAILED'}:
        return SELECTION_EMPTY, DETAIL_ALL_FAILED
    return SELECTION_EMPTY, DETAIL_NO_MATCH


def pin_generation(available: Iterable[Generation], name: str | None,
                   supported_schema_versions: Iterable[int]) -> Generation:
    """Bind a consumer to one generation; a new publication does not move it (§1.2)."""
    generations = {item.name: item for item in available}
    if not name or name not in generations:
        raise AdmissionError('E_STATE_NO_SNAPSHOT', f'generation {name!r} is not available')
    generation = generations[name]
    if generation.schema_version not in set(supported_schema_versions):
        raise AdmissionError('E_STATE_SNAPSHOT_SCHEMA',
                             f'generation {name} has schema_version {generation.schema_version}')
    return generation


def history_of(previous: Mapping[str, Any] | None, ok: bool, checked_at: float) -> dict[str, Any]:
    """Append one completed measurement to the running history; never reset it."""
    history = dict((previous or {}).get('history') or {})
    try:
        checks = int(history.get('checks') or 0)
    except (TypeError, ValueError):
        checks = 0
    try:
        passes = int(history.get('passes') or 0)
    except (TypeError, ValueError):
        passes = 0
    return {
        'checks': checks + 1,
        'passes': passes + int(bool(ok)),
        'first_checked': history.get('first_checked') or checked_at,
        'last_ok': checked_at if ok else history.get('last_ok'),
    }


def apply_measurement(previous: Mapping[str, Any] | None, measurement: Measurement,
                      policy: Policy, *, access: Access | None = None,
                      collection_id: str | None = None, profile: tuple[str, int] | None = None) -> dict[str, Any]:
    """Fold one measurement into a row.

    An unfinished measurement (cancellation, crash, still in flight) returns the
    previous row unchanged: the last completed result stays until a new one
    exists.  A finished one replaces the verdict, appends to the history and
    writes ``valid_until`` once, as ``checked_at + policy.max_age_seconds``.
    """
    row = dict(previous or {})
    if not measurement.completed or not _number(measurement.checked_at):
        row['last_observation_state'] = 'preserved'
        return row
    checked_at = float(measurement.checked_at)
    row['last_observation_state'] = 'measured'
    row['endpoint_id'] = row.get('endpoint_id') or measurement.endpoint_id
    row['checked_at'] = checked_at
    row['valid_until'] = checked_at + policy.max_age_seconds
    if measurement.observation_id:
        row['observation_id'] = measurement.observation_id
    if measurement.job_id:
        row['job_id'] = measurement.job_id
    if access is not None:
        row['access_id'], row['access_revision'] = access.access_id, access.access_revision
    if collection_id is not None:
        row['collection_id'] = collection_id
    if profile is not None:
        row['profile_id'], row['profile_revision'] = profile
    if measurement.network_id:
        # The network is what the measurement was made on; it is never carried over
        # silently from an older row, and never invented when the writer does not know it.
        row['network_id'] = measurement.network_id
    if measurement.error:
        row['error'] = measurement.error
        row['error_code'] = (measurement.error or {}).get('code') if isinstance(measurement.error, Mapping) else measurement.error
    else:
        row.pop('error', None)
        row.pop('error_code', None)
    for field, value in (measurement.verdict or {}).items():
        if field in ('checked_at', 'valid_until'):
            continue  # the measurement time owns them, not the verdict payload
        row[field] = value
    row['history'] = history_of(row, measurement.succeeded, checked_at)
    return row


class AdmissionEngine:
    """Stateful admission: it remembers the clock of every endpoint it has seen.

    Rollback is only detectable against something seen before, and once detected
    it stays pending until a newer measurement proves the timeline again, so a
    backwards jump cannot hand out fresh rows and then quietly forget it.
    """

    def __init__(self, policy: Policy, state: Mapping[str, Any] | None = None):
        self.policy = policy
        state = state or {}
        self._high_water = {str(key): float(value)
                            for key, value in (state.get('high_water') or {}).items()}
        self._rollback = {str(key): float(value)
                          for key, value in (state.get('rollback') or {}).items()}

    def state_dict(self) -> dict[str, dict[str, float]]:
        """Persistable clock state for a job checkpoint."""
        return {'high_water': dict(self._high_water), 'rollback': dict(self._rollback)}

    def clock_state(self, endpoint_id: str) -> ClockState:
        return ClockState(high_water=self._high_water.get(endpoint_id),
                          rollback_at=self._rollback.get(endpoint_id))

    def rollback_pending(self, endpoint_id: str) -> bool:
        return endpoint_id in self._rollback

    def admit(self, row: Mapping[str, Any] | None, scope: Scope, access: Access, now: float,
              *, published_at: float | None = None) -> Admission:
        data = row if isinstance(row, Mapping) else {}
        endpoint = str(data.get('endpoint_id') or data.get('proxy') or '')
        checked_at = _number(data.get('checked_at'))
        pending = self._rollback.get(endpoint)
        if pending is not None and checked_at is not None and pending <= checked_at <= now:
            # Evidence measured after the high-water mark re-establishes the timeline.
            del self._rollback[endpoint]
        result = admit(data, scope, access, self.policy, now,
                       published_at=published_at, clock=self.clock_state(endpoint))
        if result.time_state == CLOCK_ROLLBACK:
            high_water = self._high_water.get(endpoint, now)
            self._rollback[endpoint] = high_water
        elif now >= self._high_water.get(endpoint, now):
            self._high_water[endpoint] = now
        return result

    def select(self, rows: Iterable[Mapping[str, Any]], scope: Scope, access: Access, now: float,
               *, published_at: float | None = None, generation: str | None = None,
               static: bool = False) -> Selection:
        return select(rows, scope, access, self.policy, now, published_at=published_at,
                      generation=generation, static=static, engine=self)
