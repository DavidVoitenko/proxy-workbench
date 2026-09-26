"""Persistent named proxy pools: desired size, reserve, refill budgets and quotas.

A pool is a desired-state controller over endpoints, not a rotating list. The
controller never touches the network itself: candidates arrive through a
``source`` callable and health verdicts through :func:`report_health` or a
``verify`` callable, so a pool can be driven and tested without a scan.

Storage is exactly the pair of tables from CONTRACTS.ru.md §3.3 migration 7
(``pools``, ``pool_member``). This module never writes DDL: the schema comes
from ``db.migrate()`` and :meth:`PoolStore.open` refuses to work without the
columns it needs.

Rules enforced here rather than left to a consumer:

* an empty pool reports ``state='empty'`` with a deficit reason and the next
  attempt time, and never substitutes a direct connection;
* a country filter is never relaxed to fill the pool - an unknown country
  matches no filter and is not a distinct quota value;
* a public discovery endpoint is never admitted into a private collection;
* unknown country/ASN/exit IP is reported as unknown, never quietly counted;
* work per refill is bounded by ``refill_budget`` and ``scan_limit``, and the
  target (``desired``/``minimum``/``reserve``) survives a restart;
* an address that failed is never banned: it rests, it is measured again, and
  after ``retire_seconds`` without a measurement the pool simply stops tracking
  it - the address itself stays in the collection and any tier may offer it again.

Member phases, over the only columns the contract allows:

===========  ==================================================================
``active``   in service, proven by a measurement within ``max_age_seconds``
``reserve``  admitted standby, out of service, promoted when service is short
``probation`` past cooldown or past its own TTL, awaiting re-measurement; not
             in service until a new measurement says it works
``cooldown`` out of service until ``released_at + cooldown_seconds``; after
             that it becomes ``probation`` rather than being dropped forever
===========  ==================================================================
"""
from __future__ import annotations

import contextlib
import json
import re
import sqlite3
import threading
import time
from collections import Counter
from collections.abc import Iterable as _Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

__all__ = [
    'Candidate', 'Clock', 'FindRequest', 'HealthResult', 'Member', 'Policy', 'PoolError',
    'PoolSchemaError', 'PoolSpec', 'PoolStatus', 'PoolStore', 'evict', 'refill', 'refill_all',
    'report_health', 'watch', 'WatchRegistry', 'WATCHES', 'watch_registry',
    # vocabulary a source implementation, a GUI or an API client needs
    'DOMAIN_OWN', 'DOMAIN_PUBLIC', 'DOMAIN_UNKNOWN', 'DOMAINS', 'FIND_UNITS', 'MEMBER_ACTIVE',
    'MEMBER_COOLDOWN', 'MEMBER_PROBATION', 'MEMBER_RESERVE', 'MEMBER_STATES', 'POOL_STATES',
    'QUOTA_ASN', 'QUOTA_COUNTRY', 'QUOTA_DIMENSIONS', 'QUOTA_EXIT_IP', 'QUOTA_PROTOCOL',
    'SOURCE_KNOWN', 'SOURCE_RESERVE', 'SOURCE_SOURCES', 'SOURCE_ORDER', 'STATE_COMPLETE',
    'STATE_DEGRADED', 'STATE_EMPTY', 'STATE_ERROR', 'UNKNOWN_IGNORE', 'UNKNOWN_REJECT', 'transaction',
]

# --- vocabulary -------------------------------------------------------------

STATE_COMPLETE = 'complete'
STATE_DEGRADED = 'degraded'
STATE_EMPTY = 'empty'
STATE_ERROR = 'error'
POOL_STATES = (STATE_COMPLETE, STATE_DEGRADED, STATE_EMPTY, STATE_ERROR)

MEMBER_ACTIVE = 'active'
MEMBER_RESERVE = 'reserve'
MEMBER_PROBATION = 'probation'
MEMBER_COOLDOWN = 'cooldown'
MEMBER_STATES = (MEMBER_ACTIVE, MEMBER_RESERVE, MEMBER_PROBATION, MEMBER_COOLDOWN)

SOURCE_RESERVE = 'reserve'
SOURCE_KNOWN = 'known'
SOURCE_SOURCES = 'sources'
# Refill asks the cheap tiers first: the reserve restores service without any
# new measurement, known endpoints are ones the collection already has, and
# allowed sources are the last and most expensive tier.
SOURCE_ORDER = (SOURCE_RESERVE, SOURCE_KNOWN, SOURCE_SOURCES)

COLLECTION_PRIVATE = ('private', 'trusted_private')

DOMAIN_PUBLIC = 'public'
DOMAIN_OWN = 'own'
DOMAIN_UNKNOWN = 'unknown'
DOMAINS = (DOMAIN_PUBLIC, DOMAIN_OWN, DOMAIN_UNKNOWN)

QUOTA_COUNTRY = 'country'
QUOTA_PROTOCOL = 'protocol'
QUOTA_ASN = 'asn'
QUOTA_EXIT_IP = 'exit_ip'
QUOTA_DIMENSIONS = (QUOTA_COUNTRY, QUOTA_PROTOCOL, QUOTA_ASN, QUOTA_EXIT_IP)

UNKNOWN_REJECT = 'reject'
UNKNOWN_IGNORE = 'ignore'
UNKNOWN_MODES = (UNKNOWN_REJECT, UNKNOWN_IGNORE)

# A measurement stamped in the future is suspicious, not "very fresh"
# (CONTRACTS §2.4). The contract names no number, so it is named here.
CLOCK_TOLERANCE_SECONDS = 60.0

# Deficit reasons, in root-cause order: a supply-side failure explains a quota
# rejection, so it wins the tie when both are reported. The time and scope codes
# are the ones core.REASON_CODES already publishes (CONTRACTS §2.3), so a reason
# means the same thing in admission, in a pool and in the report. Codes with the
# POOL domain are this module's own and are listed in HANDOFF/pools.md.
REASON_SOURCE_ERROR = 'E_POOL_SOURCE_ERROR'
REASON_UNKNOWN_COLLECTION = 'E_POOL_UNKNOWN_COLLECTION'
REASON_UNKNOWN_POOL = 'E_POOL_UNKNOWN'
REASON_NO_CANDIDATES = 'E_POOL_NO_CANDIDATES'
REASON_COOLDOWN = 'E_POOL_COOLDOWN'
REASON_BUDGET = 'E_LIMIT_BUDGET'
REASON_AT_CAPACITY = 'E_POOL_AT_CAPACITY'
REASON_PUBLIC_IN_PRIVATE = 'E_POOL_PUBLIC_IN_PRIVATE'
REASON_UNKNOWN_MEMBER = 'E_POOL_UNKNOWN_MEMBER'
REASON_DENIED = 'E_POOL_DENIED'  # only when the candidate did not say why it was refused
REASON_SCOPE_COLLECTION = 'E_SCOPE_COLLECTION'
REASON_COUNTRY_FILTER = 'E_SCOPE_COUNTRY'
REASON_TIME_UNKNOWN = 'E_TIME_UNKNOWN'
REASON_TIME_FUTURE = 'E_TIME_FUTURE'
REASON_TIME_EXPIRED = 'E_TIME_TTL_EXPIRED'
REASON_TIME_MISSING = 'E_TIME_TTL_MISSING'
REASON_QUOTA = tuple(f'E_POOL_QUOTA_{dimension.upper()}' for dimension in QUOTA_DIMENSIONS)
REASON_QUOTA_UNKNOWN = tuple(f'E_POOL_QUOTA_UNKNOWN_{dimension.upper()}' for dimension in QUOTA_DIMENSIONS)

REASON_PRIORITY = (
    REASON_SOURCE_ERROR, REASON_UNKNOWN_COLLECTION, REASON_NO_CANDIDATES, REASON_COOLDOWN,
    REASON_BUDGET, REASON_AT_CAPACITY, REASON_COUNTRY_FILTER, REASON_SCOPE_COLLECTION,
    REASON_PUBLIC_IN_PRIVATE, *REASON_QUOTA, *REASON_QUOTA_UNKNOWN,
    REASON_TIME_EXPIRED, REASON_TIME_MISSING, REASON_TIME_FUTURE, REASON_TIME_UNKNOWN,
    REASON_DENIED,
)
# A code this module does not know (an admission reason from core, say) is still
# reported, never dropped, and beats the generic E_POOL_DENIED fallback on a tie:
# a concrete reason from the admission contract is more useful than "refused".
REASON_ORDER = {code: index for index, code in enumerate(REASON_PRIORITY)}
UNKNOWN_REASON_ORDER = REASON_ORDER[REASON_DENIED] - 0.5

REQUIRED_TABLES = ('pools', 'pool_member', 'collections')
REQUIRED_COLUMNS = {
    'pools': ('id', 'collection_id', 'profile_id', 'profile_revision', 'policy_json', 'desired',
              'minimum', 'reserve', 'state', 'deficit_reason', 'next_attempt_at'),
    'pool_member': ('pool_id', 'endpoint_id', 'state', 'admitted_at', 'released_at'),
    'collections': ('id', 'kind'),
}

_POOL_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
_COUNTRY = re.compile(r'^[A-Za-z0-9]{2,3}$')
_KEEP = object()


# --- errors -----------------------------------------------------------------

class PoolError(Exception):
    """A pool operation refused; ``code`` is a machine-readable CONTRACTS §5.4 code."""

    def __init__(self, code: str, message: str = ''):
        super().__init__(message or code)
        self.code = code


class PoolSchemaError(PoolError):
    def __init__(self, message: str):
        super().__init__('E_DATA_MIGRATION_FAILED', message)


# --- small validators -------------------------------------------------------

def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number == number and number not in (float('inf'), float('-inf')) else None


def _count(value: Any, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PoolError('E_VALIDATION_FIELD', f'{name} must be an integer, got {value!r}')
    if value < minimum:
        raise PoolError('E_VALIDATION_FIELD', f'{name} must be >= {minimum}, got {value}')
    return value


def _seconds(value: Any, name: str, minimum: float = 0.0) -> float:
    number = _finite(value)
    if number is None or number < minimum:
        raise PoolError('E_VALIDATION_FIELD', f'{name} must be a number >= {minimum}, got {value!r}')
    return number


def _country_code(value: Any) -> str:
    code = str(value or '').strip().upper()
    if not _COUNTRY.match(code):
        raise PoolError('E_VALIDATION_FIELD', f'country code {value!r} is not a two-letter code')
    return code


def _primary_reason(reasons: Mapping[str, int]) -> str | None:
    """The reason to show first: the most frequent one, and the most upstream on a tie."""
    if not reasons:
        return None
    return min(reasons.items(), key=lambda item: (-item[1], REASON_ORDER.get(item[0], UNKNOWN_REASON_ORDER)))[0]


def _ordered_reasons(reasons: Mapping[str, int]) -> tuple:
    order = lambda item: (-item[1], REASON_ORDER.get(item[0], UNKNOWN_REASON_ORDER))  # noqa: E731
    return sorted(reasons.items(), key=order)


# --- policy -----------------------------------------------------------------

@dataclass(frozen=True)
class Policy:
    """Per-pool limits. Unknown keys are rejected: a silent default is a lie.

    ``quota`` maps a dimension to a maximum per value, so ``exit_ip: 1`` is the
    "unique exit IP" quota. ``quota_unknown`` decides what happens to a
    candidate whose value for that dimension is unknown: ``reject`` fails
    closed, ``ignore`` admits it without counting it and reports the count in
    ``PoolStatus.quota_unknown``.
    """

    countries: tuple = ()
    quota: tuple = ()
    quota_unknown: tuple = ()
    cooldown_seconds: float = 300.0
    probation_seconds: float = 120.0
    retire_seconds: float = 86400.0
    max_age_seconds: float = 7200.0
    refill_budget: int = 10
    scan_limit: int = 200
    max_members: int = 0
    interval_seconds: float = 300.0
    retry_interval_seconds: float = 60.0
    require_valid_until: bool = True

    @classmethod
    def from_dict(cls, data: Mapping | None = None) -> 'Policy':
        values = dict(data or {})
        known = {name: getattr(cls, name) for name in _POLICY_FIELDS}
        unknown = sorted(set(values) - set(known))
        if unknown:
            raise PoolError('E_VALIDATION_UNKNOWN_FIELD', f'unknown policy fields: {", ".join(unknown)}')
        merged = {name: values.get(name, default) for name, default in known.items()}

        countries = merged['countries']
        if isinstance(countries, str):
            countries = countries.replace(';', ',').split(',')
        if not isinstance(countries, _Iterable):
            raise PoolError('E_VALIDATION_FIELD', 'countries must be a list of country codes')
        quota = merged['quota'] or {}
        quota_unknown = merged['quota_unknown'] or {}
        if not isinstance(quota, Mapping) or not isinstance(quota_unknown, Mapping):
            raise PoolError('E_VALIDATION_FIELD', 'quota and quota_unknown must be objects')
        for dimension in sorted(set(quota) | set(quota_unknown)):
            if dimension not in QUOTA_DIMENSIONS:
                raise PoolError('E_VALIDATION_FIELD',
                                f'quota dimension {dimension!r} is not one of {QUOTA_DIMENSIONS}')
        modes = {}
        for dimension, mode in quota_unknown.items():
            mode = str(mode).strip().lower()
            if mode not in UNKNOWN_MODES:
                raise PoolError('E_VALIDATION_FIELD',
                                f'quota_unknown.{dimension} must be one of {UNKNOWN_MODES}')
            modes[dimension] = mode
        require_valid_until = merged['require_valid_until']
        if not isinstance(require_valid_until, bool):
            raise PoolError('E_VALIDATION_FIELD', 'require_valid_until must be a boolean')

        return cls(
            countries=tuple(sorted({_country_code(code) for code in countries})),
            quota=tuple(sorted((dimension, _count(quota[dimension], f'quota.{dimension}'))
                               for dimension in quota)),
            quota_unknown=tuple(sorted(modes.items())),
            cooldown_seconds=_seconds(merged['cooldown_seconds'], 'cooldown_seconds'),
            probation_seconds=_seconds(merged['probation_seconds'], 'probation_seconds'),
            retire_seconds=_seconds(merged['retire_seconds'], 'retire_seconds'),
            max_age_seconds=_seconds(merged['max_age_seconds'], 'max_age_seconds', 1.0),
            refill_budget=_count(merged['refill_budget'], 'refill_budget'),
            scan_limit=_count(merged['scan_limit'], 'scan_limit', 1),
            max_members=_count(merged['max_members'], 'max_members'),
            interval_seconds=_seconds(merged['interval_seconds'], 'interval_seconds', 1.0),
            retry_interval_seconds=_seconds(merged['retry_interval_seconds'], 'retry_interval_seconds', 1.0),
            require_valid_until=require_valid_until,
        )

    def to_dict(self) -> dict:
        return {
            'countries': list(self.countries),
            'quota': {dimension: limit for dimension, limit in self.quota},
            'quota_unknown': dict(self.quota_unknown),
            'cooldown_seconds': self.cooldown_seconds,
            'probation_seconds': self.probation_seconds,
            'retire_seconds': self.retire_seconds,
            'max_age_seconds': self.max_age_seconds,
            'refill_budget': self.refill_budget,
            'scan_limit': self.scan_limit,
            'max_members': self.max_members,
            'interval_seconds': self.interval_seconds,
            'retry_interval_seconds': self.retry_interval_seconds,
            'require_valid_until': self.require_valid_until,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False)

    def quota_limit(self, dimension: str) -> int:
        for name, limit in self.quota:
            if name == dimension:
                return limit
        return 0

    def unknown_mode(self, dimension: str) -> str:
        for name, mode in self.quota_unknown:
            if name == dimension:
                return mode
        return UNKNOWN_REJECT

    def member_cap(self, desired: int, reserve: int) -> int:
        """How many members may be in service or on standby at the same time."""
        return self.max_members or desired + reserve


_POLICY_FIELDS = tuple(Policy.__dataclass_fields__)


# --- input and output types -------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    """One endpoint offered to a pool.

    ``allowed`` and ``admission_reason`` come from the shared admission
    contract (CONTRACTS §2.3); this module does not re-admit a row. Country,
    ASN and exit IP stay ``None`` when they are unknown - an unknown value is
    reported as unknown instead of being guessed.
    """

    endpoint_id: str
    canonical: str
    collection_id: str = ''
    origin_domain: str = DOMAIN_UNKNOWN
    protocol: str = ''
    country: str | None = None
    asn: int | None = None
    exit_ip: str | None = None
    checked_at: float | None = None
    valid_until: float | None = None
    allowed: bool = True
    admission_reason: str | None = None
    score: float = 0.0

    def dimension_value(self, dimension: str) -> str | None:
        if dimension == QUOTA_COUNTRY:
            return str(self.country or '').strip().upper() or None
        if dimension == QUOTA_PROTOCOL:
            return (self.protocol or str(self.canonical).partition('://')[0]).strip().lower() or None
        if dimension == QUOTA_ASN:
            return None if self.asn is None else str(int(self.asn))
        if dimension == QUOTA_EXIT_IP:
            if self.exit_ip is None:
                return None
            return str(self.exit_ip).strip() or None
        raise PoolError('E_VALIDATION_FIELD', f'unknown quota dimension {dimension!r}')


@dataclass(frozen=True)
class Member:
    """One row of ``pool_member``."""

    endpoint_id: str
    state: str
    admitted_at: float | None
    released_at: float | None


@dataclass(frozen=True)
class FindRequest:
    """How many more of what the measuring engine should look for, and in what unit.

    This is the pool's half of the answer ``pipeline.FindPolicy`` needs, and it
    exists because the two sides must not each guess the unit.  A pool keeps
    *members*; a member is a row of ``pool_member`` and nothing else — the table
    of CONTRACTS §3.3 migration 7 holds no exit address and no measured country,
    so a count of anything but members cannot survive the next restart.  That is
    the whole reason the unit is derived and named here instead of the pool
    simply asking the engine for exits (HANDOFF/pipeline.md §1.5).

    What the user may still want is *distinctness*, and that is what a quota is
    for: ``quota={'exit_ip': 1}`` with ``desired=5`` is exactly "five proxies that
    do not share an exit", is checked on every admission, and reports
    ``E_POOL_QUOTA_EXIT_IP`` or ``E_POOL_QUOTA_UNKNOWN_EXIT_IP`` when it cannot be
    satisfied.  So the unit the *supply* must be measured in follows from the
    policy rather than from a hardcoded guess: a pool that caps members per exit
    needs the engine to prove distinct exits, and a pool that does not is
    satisfied by ``desired`` working endpoints.

    ``n`` is the shortfall in the chosen unit, not ``desired``: asking for N
    again on every tick makes the engine re-measure what the pool already has.
    """

    n: int
    what: str

    def as_dict(self) -> dict:
        return {'n': self.n, 'what': self.what}

    def find_policy(self, find_policy_cls: type) -> Any:
        """The caller's own ``pipeline.FindPolicy``; this module never imports it."""
        return find_policy_cls(n=self.n, what=self.what)


#: The three units of "N" (F12, CONTRACTS §1.1).  ``ip`` counts the address of a
#: proxy; for a pool it is never the right unit, so it is rejected by name rather
#: than silently treated as an endpoint.
FIND_UNITS = ('endpoint', 'ip', 'exit')
#: A quota dimension and a find-N unit that name the same thing but not the same
#: word: the pool stores ``exit_ip``, the engine counts ``exit``.  Keeping the two
#: vocabularies apart here is what stops a pool from handing the engine a unit it
#: will refuse — the mistake is a `ValidationError` at the call site, not a
#: silently defaulted policy.
FIND_UNIT_FOR_DIMENSION = {QUOTA_EXIT_IP: 'exit', QUOTA_COUNTRY: 'endpoint',
                           QUOTA_ASN: 'endpoint', QUOTA_PROTOCOL: 'endpoint'}


@dataclass(frozen=True)
class PoolSpec:
    """The target state of a named pool. It is the row in ``pools``, so it survives a crash."""

    id: str
    collection_id: str
    profile_id: str
    profile_revision: int
    policy: Policy
    desired: int
    minimum: int
    reserve: int
    state: str = STATE_EMPTY
    deficit_reason: str | None = None
    next_attempt_at: float | None = None

    @property
    def count_unit(self) -> str:
        """The unit ``desired``/``minimum``/``served`` are counted in.

        Members, unless the policy caps members per confirmed exit IP: then the
        pool is deliberately holding N *distinct exits* and every number it
        reports means that.  See :class:`FindRequest` for why this is derived
        rather than configured twice.  ``ip`` is never the answer: a pool holds
        endpoints, and the address of a proxy is not a thing it can count
        across a restart.
        """
        return FIND_UNIT_FOR_DIMENSION[QUOTA_EXIT_IP] if self.policy.quota_limit(QUOTA_EXIT_IP) \
            else 'endpoint'

    def find_request(self, served: int) -> FindRequest:
        """What the measuring engine should still be asked for, in this pool's unit."""
        return FindRequest(n=max(0, int(self.desired) - max(0, int(served))), what=self.count_unit)

    def as_dict(self) -> dict:
        return {
            'id': self.id, 'collection_id': self.collection_id, 'profile_id': self.profile_id,
            'profile_revision': self.profile_revision, 'policy': self.policy.to_dict(),
            'desired': self.desired, 'minimum': self.minimum, 'reserve': self.reserve,
            'state': self.state, 'deficit_reason': self.deficit_reason,
            'next_attempt_at': self.next_attempt_at, 'count_unit': self.count_unit,
        }


@dataclass(frozen=True)
class PoolStatus:
    """What the pool can serve right now, why it cannot serve more, and when it retries.

    ``served`` counts the members that are in service and is what is compared
    against ``desired``. ``probation`` members are deliberately excluded from
    it: they are waiting for a measurement that says they work.
    """

    pool_id: str
    state: str
    at: float
    collection_id: str
    collection_kind: str | None
    profile_id: str
    profile_revision: int
    desired: int
    minimum: int
    reserve: int
    counts: dict
    served: int
    shortfall: int
    below_minimum: bool
    ready_for_clients: bool
    deficit_reason: str | None
    deficit_reasons: tuple
    next_attempt_at: float | None
    budget_limit: int
    budget_spent: int
    admissions: int
    promotions: int
    re_admissions: int
    deferred: bool
    recheck_due: tuple
    probation_overdue: tuple
    quota_unknown: dict
    quota_conflicts: tuple
    source_errors: tuple
    count_unit: str = 'endpoint'
    find: dict | None = None

    def as_dict(self) -> dict:
        return {
            'pool_id': self.pool_id, 'state': self.state, 'at': self.at,
            'collection_id': self.collection_id, 'collection_kind': self.collection_kind,
            'profile_id': self.profile_id, 'profile_revision': self.profile_revision,
            'desired': self.desired, 'minimum': self.minimum, 'reserve': self.reserve,
            'counts': dict(self.counts), 'served': self.served, 'shortfall': self.shortfall,
            'below_minimum': self.below_minimum, 'ready_for_clients': self.ready_for_clients,
            'deficit_reason': self.deficit_reason,
            'deficit_reasons': [{'code': code, 'count': count} for code, count in self.deficit_reasons],
            'next_attempt_at': self.next_attempt_at, 'budget_limit': self.budget_limit,
            'budget_spent': self.budget_spent, 'admissions': self.admissions,
            'promotions': self.promotions, 're_admissions': self.re_admissions,
            'deferred': self.deferred, 'recheck_due': list(self.recheck_due),
            'probation_overdue': list(self.probation_overdue),
            'quota_unknown': dict(self.quota_unknown),
            'quota_conflicts': [{'dimension': dimension, 'value': value, 'count': count}
                                for dimension, value, count in self.quota_conflicts],
            'source_errors': list(self.source_errors),
            'count_unit': self.count_unit, 'find': dict(self.find or {}),
        }

    def find_policy(self, find_policy_cls: type) -> Any:
        """``pipeline.FindPolicy`` for the next supply run, built from the pool's own unit.

        The engine side calls this with ``pipeline.FindPolicy``; this module never
        imports the pipeline, so the two stay independent and a mismatch is a
        wrong argument at the call site rather than an import error.
        """
        return FindRequest(n=max(0, self.shortfall), what=self.count_unit).find_policy(find_policy_cls)


@dataclass(frozen=True)
class HealthResult:
    """Outcome of a health report for one member."""

    pool_id: str
    endpoint_id: str
    applied: bool
    state: str
    reason: str | None = None
    next_try_at: float | None = None


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection):
    """One write transaction, whatever isolation level the connection was opened with.

    ``db.connect`` uses ``isolation_level=None``, where a bare ``with conn`` commits
    nothing at all; a plain ``sqlite3.connect`` opens a transaction by itself.
    Issuing BEGIN explicitly is the only way both are atomic. A transaction the
    caller already opened is joined, and never committed here.
    """
    if conn.in_transaction:
        yield conn
        return
    conn.execute('BEGIN')
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


class Clock:
    """Wall clock; a background run without a tray or a test can substitute its own."""

    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


# --- store ------------------------------------------------------------------

class PoolStore:
    """Read and write access to ``pools`` and ``pool_member``.

    The store holds no pool logic: it stores a target state, the members and
    the last status. Every statement names its columns explicitly, as
    CONTRACTS §3.2 requires, so an additive migration cannot break it.

    ``add_member``, ``set_member_state`` and ``save_status`` do not commit on
    their own: the refill controller and the health helpers wrap them in a
    single transaction, so a caller never sees half a refill.
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.execute('PRAGMA foreign_keys = ON')

    @classmethod
    def open(cls, path, *, migrate: Callable[[Any], Any] | None = None) -> 'PoolStore':
        """Open a store on a database file.

        ``migrate`` is the migrator of ``db.py`` and is called with the path
        (``db.migrate(path)``), so this module never creates a table itself. The
        store keeps its own connection instead of borrowing one, because a refill
        has to be one transaction and a shared connection may be in autocommit.
        """
        target = Path(path)
        if str(target) != ':memory:':
            target.parent.mkdir(parents=True, exist_ok=True)
        if migrate is not None:
            migrate(target)
        conn = sqlite3.connect(str(target))
        try:
            store = cls(conn)
            store.require_schema()
        except BaseException:
            conn.close()
            raise
        return store

    def close(self) -> None:
        self.conn.close()

    def require_schema(self) -> None:
        """Fail loudly when the migrator has not produced the tables of §3.3."""
        problems = []
        for table, columns in REQUIRED_COLUMNS.items():
            present = {row[1] for row in self.conn.execute(f'PRAGMA table_info({table})')}
            if not present:
                problems.append(f'{table}: no such table')
            else:
                absent = [column for column in columns if column not in present]
                if absent:
                    problems.append(f'{table}: no column {", ".join(absent)}')
        if problems:
            raise PoolSchemaError('pool storage is not migrated (' + '; '.join(problems) + ')')

    def collection_kind(self, collection_id: str) -> str | None:
        row = self.conn.execute('SELECT kind FROM collections WHERE id = ?', (collection_id,)).fetchone()
        return row[0] if row else None

    def create(self, pool_id: str, *, collection_id: str, profile_id: str, profile_revision: int = 1,
               desired: int = 0, minimum: int = 0, reserve: int = 0,
               policy: Mapping | None = None) -> PoolSpec:
        """Create a named pool. Changing a target afterwards goes through :meth:`set_target`."""
        if not _POOL_ID.match(str(pool_id or '')):
            raise PoolError('E_VALIDATION_FIELD', f'pool id {pool_id!r} must match {_POOL_ID.pattern}')
        if self.get(pool_id) is not None:
            raise PoolError('E_CONFLICT_IDEMPOTENCY', f'pool {pool_id!r} already exists')
        if not str(collection_id or '').strip():
            raise PoolError('E_VALIDATION_FIELD', 'collection_id is required')
        if self.collection_kind(collection_id) is None:
            raise PoolError(REASON_UNKNOWN_COLLECTION, f'collection {collection_id!r} does not exist')
        if not str(profile_id or '').strip():
            raise PoolError('E_VALIDATION_FIELD', 'profile_id is required')
        target = _count(desired, 'desired')
        low = _count(minimum, 'minimum')
        if low > target:
            raise PoolError('E_VALIDATION_FIELD', f'minimum {low} must not exceed desired {target}')
        revision = _count(profile_revision, 'profile_revision', 1)
        standby = _count(reserve, 'reserve')
        parsed = Policy.from_dict(policy)
        with transaction(self.conn):
            self.conn.execute(
                'INSERT INTO pools (id, collection_id, profile_id, profile_revision, policy_json, desired,'
                ' minimum, reserve, state, deficit_reason, next_attempt_at)'
                ' VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (pool_id, collection_id, profile_id, revision, parsed.to_json(), target, low, standby,
                 STATE_EMPTY, None, None))
        return self.get(pool_id)

    def get(self, pool_id: str) -> PoolSpec | None:
        row = self.conn.execute(
            'SELECT id, collection_id, profile_id, profile_revision, policy_json, desired, minimum, reserve,'
            ' state, deficit_reason, next_attempt_at FROM pools WHERE id = ?', (pool_id,)).fetchone()
        return _spec(row) if row else None

    def list(self) -> list:
        return [self.get(row[0]) for row in self.conn.execute('SELECT id FROM pools ORDER BY id')]

    def require(self, pool_id: str) -> PoolSpec:
        spec = self.get(pool_id)
        if spec is None:
            raise PoolError(REASON_UNKNOWN_POOL, f'pool {pool_id!r} does not exist')
        return spec

    def set_target(self, pool_id: str, *, desired: int | None = None, minimum: int | None = None,
                   reserve: int | None = None) -> PoolSpec:
        spec = self.require(pool_id)
        target = spec.desired if desired is None else _count(desired, 'desired')
        low = spec.minimum if minimum is None else _count(minimum, 'minimum')
        standby = spec.reserve if reserve is None else _count(reserve, 'reserve')
        if low > target:
            raise PoolError('E_VALIDATION_FIELD', f'minimum {low} must not exceed desired {target}')
        with transaction(self.conn):
            self.conn.execute('UPDATE pools SET desired = ?, minimum = ?, reserve = ? WHERE id = ?',
                              (target, low, standby, pool_id))
        return self.get(pool_id)

    def set_policy(self, pool_id: str, policy: Mapping | None) -> PoolSpec:
        self.require(pool_id)
        parsed = Policy.from_dict(policy)
        with transaction(self.conn):
            self.conn.execute('UPDATE pools SET policy_json = ? WHERE id = ?', (parsed.to_json(), pool_id))
        return self.get(pool_id)

    def members(self, pool_id: str) -> list:
        rows = self.conn.execute(
            'SELECT endpoint_id, state, admitted_at, released_at FROM pool_member WHERE pool_id = ?'
            ' ORDER BY endpoint_id', (pool_id,)).fetchall()
        return [Member(*row) for row in rows]

    def member(self, pool_id: str, endpoint_id: str) -> Member | None:
        row = self.conn.execute(
            'SELECT endpoint_id, state, admitted_at, released_at FROM pool_member'
            ' WHERE pool_id = ? AND endpoint_id = ?', (pool_id, endpoint_id)).fetchone()
        return Member(*row) if row else None

    def add_member(self, pool_id: str, endpoint_id: str, state: str, *, now: float) -> None:
        if state not in MEMBER_STATES:
            raise PoolError('E_VALIDATION_FIELD', f'unknown member state {state!r}')
        self.conn.execute(
            'INSERT INTO pool_member (pool_id, endpoint_id, state, admitted_at, released_at)'
            ' VALUES (?, ?, ?, ?, NULL)', (pool_id, endpoint_id, state, now))

    def remove_member(self, pool_id: str, endpoint_id: str) -> bool:
        """Forget a member of this pool. The endpoint itself stays in the collection."""
        return bool(self.conn.execute(
            'DELETE FROM pool_member WHERE pool_id = ? AND endpoint_id = ?',
            (pool_id, endpoint_id)).rowcount)

    def set_member_state(self, pool_id: str, endpoint_id: str, state: str, *,
                         admitted_at: Any = _KEEP, released_at: Any = _KEEP) -> None:
        """Change a member's phase. ``_KEEP`` (the default) leaves a timestamp alone."""
        if state not in MEMBER_STATES:
            raise PoolError('E_VALIDATION_FIELD', f'unknown member state {state!r}')
        assignments, values = ['state = ?'], [state]
        for name, value in (('admitted_at', admitted_at), ('released_at', released_at)):
            if value is _KEEP:
                continue
            assignments.append(f'{name} = ?')
            values.append(value)
        values += [pool_id, endpoint_id]
        self.conn.execute(f'UPDATE pool_member SET {", ".join(assignments)} WHERE pool_id = ? AND endpoint_id = ?',
                          values)

    def save_status(self, pool_id: str, state: str, *, deficit_reason: str | None,
                    next_attempt_at: float | None) -> None:
        if state not in POOL_STATES:
            raise PoolError('E_VALIDATION_FIELD', f'unknown pool state {state!r}')
        self.conn.execute('UPDATE pools SET state = ?, deficit_reason = ?, next_attempt_at = ? WHERE id = ?',
                          (state, deficit_reason, next_attempt_at, pool_id))

    def status(self, pool_id: str, *, now: float | None = None) -> PoolStatus:
        """Read what the pool can serve now, without running a refill.

        This is the ``pools.read`` path for the GUI and the API: the counts come
        from the members, the state and the reason from the last refill, and
        ``deferred`` says plainly that nothing was evaluated in this call. The
        breakdown of a deficit is not in the ``pools`` row, so a read shows the
        primary reason alone; :func:`refill` returns the full breakdown.
        """
        spec = self.require(pool_id)
        controller = _Controller(self, spec, self.collection_kind(spec.collection_id),
                                 now=time.time() if now is None else float(now),
                                 work=_Work(0, spec.policy.scan_limit), verify=None)
        return controller.status(deferred=True)


def _spec(row: Sequence) -> PoolSpec:
    return PoolSpec(id=row[0], collection_id=row[1], profile_id=row[2], profile_revision=row[3],
                    policy=Policy.from_dict(json.loads(row[4])), desired=row[5], minimum=row[6],
                    reserve=row[7], state=row[8], deficit_reason=row[9], next_attempt_at=row[10])


# --- the refill controller --------------------------------------------------

class _Work:
    """The hard budgets of one refill call.

    ``limit`` counts the state changes that put an endpoint into service (a new
    admission, a promotion from the reserve, a re-admission after probation);
    ``scan_limit`` counts the candidates looked at and the re-measurements asked
    for. They are separate on purpose: a call that has already fetched three
    candidates is still allowed to admit them.
    """

    def __init__(self, limit: int, scan_limit: int):
        self.limit = max(0, int(limit))
        self.scan_limit = max(1, int(scan_limit))
        self.spent = 0
        self.scanned = 0
        self.examined = 0
        self.admissions = 0
        self.promotions = 0
        self.re_admissions = 0

    @property
    def remaining(self) -> int:
        return self.limit - self.spent

    @property
    def may_spend(self) -> bool:
        return self.remaining > 0

    @property
    def may_scan(self) -> bool:
        return self.scanned < self.scan_limit

    @property
    def may_measure(self) -> bool:
        return self.examined < self.scan_limit

    @property
    def limited(self) -> bool:
        """Whether a budget, rather than the supply, is what stopped this call."""
        return not self.may_spend or not self.may_scan

    def take(self, counter: str) -> bool:
        """Spend one unit of the state-change budget; ``counter`` is the tally to move."""
        if not self.may_spend:
            return False
        self.spent += 1
        setattr(self, counter, getattr(self, counter) + 1)
        return True


class _Controller:
    """One refill of one pool. All writes happen inside the caller's transaction."""

    def __init__(self, store: PoolStore, spec: PoolSpec, collection_kind: str | None, *,
                 now: float, work: _Work, verify: Callable[..., bool | None] | None):
        self.store = store
        self.spec = spec
        self.policy = spec.policy
        self.kind = collection_kind
        self.now = now
        self.work = work
        self.verify = verify
        self.members = {member.endpoint_id: member for member in store.members(spec.id)}
        self.attributes: dict = {}
        self.reasons: Counter = Counter()
        self.source_errors: list = []

    # -- counts

    def served(self) -> int:
        return sum(1 for member in self.members.values() if member.state == MEMBER_ACTIVE)

    def standby(self) -> int:
        return sum(1 for member in self.members.values() if member.state == MEMBER_RESERVE)

    def occupancy(self) -> int:
        """Rows that occupy pool capacity.

        A member that is resting does not: a pool whose whole membership is
        cooling down must still be able to take a working endpoint. An explicit
        ``max_members`` is the hard limit on the table itself, and does count
        the resting ones.
        """
        if self.policy.max_members:
            return len(self.members)
        return self.served() + self.standby()

    def projected_demand(self) -> int:
        """Demand as it will be after the phase transitions, computed read-only.

        Candidates are collected before the first write, so the source is asked
        for what the pool will need, counting a member whose proof is already
        older than the max-age as gone.
        """
        served = sum(1 for member in self.members.values()
                     if member.state == MEMBER_ACTIVE and not self.expired(member))
        standby = sum(1 for member in self.members.values()
                      if member.state == MEMBER_RESERVE and not self.expired(member))
        return max(0, self.spec.desired - served) + max(0, self.spec.reserve - standby)

    def expired(self, member: Member) -> bool:
        admitted = _finite(member.admitted_at)
        return admitted is None or self.now > admitted + self.policy.max_age_seconds

    # -- member phases

    def _move(self, member: Member, state: str, **timestamps) -> None:
        """Change a member's phase in the database and in the in-memory view alike."""
        self.store.set_member_state(self.spec.id, member.endpoint_id, state, **timestamps)
        self.members[member.endpoint_id] = Member(
            endpoint_id=member.endpoint_id, state=state,
            admitted_at=timestamps.get('admitted_at', member.admitted_at),
            released_at=timestamps.get('released_at', member.released_at))

    def _expire_cooldowns(self) -> None:
        """A resting member becomes eligible again instead of being dropped forever."""
        for member in self._ordered():
            if member.state != MEMBER_COOLDOWN:
                continue
            released = _finite(member.released_at) or 0.0
            if self.now >= released + self.policy.cooldown_seconds:
                self._move(member, MEMBER_PROBATION)

    def _expire_proofs(self) -> None:
        """A member whose proof is older than the profile max-age stops being served."""
        for member in self._ordered():
            if member.state in (MEMBER_ACTIVE, MEMBER_RESERVE) and self.expired(member):
                self._move(member, MEMBER_PROBATION)

    def _demote(self) -> None:
        """When the target shrinks, extra members wait in the reserve instead of being dropped."""
        active = [member for member in self._ordered() if member.state == MEMBER_ACTIVE]
        for member in active[self.spec.desired:]:
            self._move(member, MEMBER_RESERVE)

    def _trim(self) -> None:
        """Per-pool limit: rows beyond an explicit ``max_members`` are forgotten.

        Forgetting is not a ban: the endpoint can be offered again by any tier of
        the source, exactly like an address that never entered this pool.
        """
        limit = self.policy.max_members
        if not limit or len(self.members) <= limit:
            return
        resting = [member for member in self._ordered() if member.state in (MEMBER_COOLDOWN, MEMBER_PROBATION)]
        for member in resting:
            if len(self.members) <= limit:
                return
            self.store.remove_member(self.spec.id, member.endpoint_id)
            self.members.pop(member.endpoint_id)

    def _retire(self) -> None:
        """A member that rested for ``retire_seconds`` without a measurement is forgotten.

        This is what keeps the table bounded when a pool churns through many
        endpoints, and it is time-boxed rather than permanent: the address itself
        is untouched and a later tier may offer it again.
        """
        for member in self._ordered():
            if member.state not in (MEMBER_COOLDOWN, MEMBER_PROBATION):
                continue
            resting_since = _finite(member.released_at)
            if resting_since is None:
                resting_since = _finite(member.admitted_at)
            if resting_since is None or self.now <= resting_since + self.policy.retire_seconds:
                continue
            self.store.remove_member(self.spec.id, member.endpoint_id)
            self.members.pop(member.endpoint_id)

    def _promote(self) -> None:
        """The reserve restores service without waiting for any new measurement.

        The promotion does not refresh ``admitted_at``: the age of a proof is
        measured from the measurement that produced it (CONTRACTS §2.2), and the
        freshness check has already run in this call.
        """
        for member in self._ordered(state=MEMBER_RESERVE):
            if self.served() >= self.spec.desired:
                return
            if not self.work.take('promotions'):
                self.reasons[REASON_BUDGET] += 1
                return
            self._move(member, MEMBER_ACTIVE)

    def measure(self) -> list:
        """Ask the caller's measurement about every probation member.

        This is the only part of a refill that does I/O, so it is deliberately
        callable *outside* a write transaction: a measurement is a network round
        trip, and holding SQLite's write lock across one would refuse every other
        writer of the database for the length of the round trip (CONTRACTS §6.4
        is about a bounded number of DB-writing executors, not about making the
        rest of the program wait for a proxy answer).  It reads state, changes
        nothing, and returns the verdicts :meth:`apply_measurements` commits.

        A measurement that raised is not proof of a dead proxy: it is recorded as
        a source error and the member keeps waiting.
        """
        if self.verify is None:
            return []
        verdicts = []
        for member in self._ordered(state=MEMBER_PROBATION):
            if not self.work.may_measure or not self.work.may_spend:
                break
            self.work.examined += 1
            try:
                outcome = self.verify(self.spec, member)
            except Exception as exc:  # a measurement that broke is not proof of a dead proxy
                self.source_errors.append(type(exc).__name__)
                continue
            if outcome is True or outcome is False:
                verdicts.append((member.endpoint_id, outcome))
        return verdicts

    def apply_measurements(self, verdicts: Sequence) -> None:
        """Commit verdicts taken by :meth:`measure`, inside the write transaction."""
        for endpoint_id, worked in verdicts:
            member = self.members.get(endpoint_id)
            if member is None or member.state != MEMBER_PROBATION:
                continue  # a concurrent call already moved it; its answer wins
            if worked:
                state = self._placement()
                if state is None:
                    continue  # the pool is full; the member waits in probation and is kept
                if not self.work.take('re_admissions'):
                    self.reasons[REASON_BUDGET] += 1
                    return
                self._move(member, state, admitted_at=self.now, released_at=None)
            else:
                self._move(member, MEMBER_COOLDOWN, released_at=self.now)
                self.reasons[REASON_COOLDOWN] += 1

    def _placement(self) -> str | None:
        """Where a proven endpoint goes: into service while short, into the reserve when full."""
        if self.served() < self.spec.desired:
            return MEMBER_ACTIVE
        if self.standby() < self.spec.reserve:
            return MEMBER_RESERVE
        return None

    def admit(self, candidates: dict) -> None:
        """Offer candidates to the pool, best score first, in a deterministic order.

        What the source told us about the members it repeated becomes the only
        attributes the pool can account quotas with; a member the source did
        not mention stays unknown in the report.
        """
        self.attributes.update(candidates)
        for endpoint_id in sorted(candidates, key=lambda key: (-candidates[key].score, key)):
            candidate = candidates[endpoint_id]
            known = self.members.get(endpoint_id)
            if known is not None:
                # A member that is already resting cannot be admitted again; saying so is
                # what tells the user the pool is short because something is cooling down.
                if known.state == MEMBER_COOLDOWN:
                    self.reasons[REASON_COOLDOWN] += 1
                continue
            code = self._reject_reason(candidate)
            if code is not None:
                self.reasons[code] += 1
                continue
            if self.served() >= self.spec.desired:
                if self.standby() >= self.spec.reserve:
                    self.reasons[REASON_AT_CAPACITY] += 1
                    continue
                state = MEMBER_RESERVE
            elif self.occupancy() >= self.policy.member_cap(self.spec.desired, self.spec.reserve):
                self.reasons[REASON_AT_CAPACITY] += 1
                continue
            else:
                state = MEMBER_ACTIVE
            if not self.work.take('admissions'):
                self.reasons[REASON_BUDGET] += 1
                return
            self.store.add_member(self.spec.id, endpoint_id, state, now=self.now)
            self.members[endpoint_id] = Member(endpoint_id, state, self.now, None)

    def _reject_reason(self, candidate: Candidate) -> str | None:
        if not str(candidate.endpoint_id or '').strip() or not str(candidate.canonical or '').strip():
            return 'E_VALIDATION_FIELD'
        if candidate.collection_id and candidate.collection_id != self.spec.collection_id:
            return REASON_SCOPE_COLLECTION
        if self.kind in COLLECTION_PRIVATE and candidate.origin_domain == DOMAIN_PUBLIC:
            return REASON_PUBLIC_IN_PRIVATE
        if self.policy.countries:
            country = candidate.dimension_value(QUOTA_COUNTRY)
            # An unknown country is not a match: the filter is never relaxed to fill the pool.
            if country is None or country not in self.policy.countries:
                return REASON_COUNTRY_FILTER
        if not candidate.allowed:
            # The shared admission contract already explained itself; keep its code.
            return candidate.admission_reason or REASON_DENIED
        code = _freshness_reason(candidate, self.now, self.policy.require_valid_until)
        if code is not None:
            return code
        return self._quota_reason(candidate)

    def _quota_reason(self, candidate: Candidate) -> str | None:
        for dimension in QUOTA_DIMENSIONS:
            limit = self.policy.quota_limit(dimension)
            if not limit:
                continue
            value = candidate.dimension_value(dimension)
            if value is None:
                if self.policy.unknown_mode(dimension) == UNKNOWN_REJECT:
                    return REASON_QUOTA_UNKNOWN[QUOTA_DIMENSIONS.index(dimension)]
                continue
            if self._known_count(dimension, value) + 1 > limit:
                return REASON_QUOTA[QUOTA_DIMENSIONS.index(dimension)]
        return None

    def _known_count(self, dimension: str, value: str) -> int:
        count = 0
        for endpoint_id, member in self.members.items():
            if member.state not in (MEMBER_ACTIVE, MEMBER_RESERVE, MEMBER_PROBATION):
                continue
            attributes = self.attributes.get(endpoint_id)
            if attributes is not None and attributes.dimension_value(dimension) == value:
                count += 1
        return count

    # -- reporting

    def _quota_report(self) -> tuple:
        """Unknown is reported, not counted as a value of its own."""
        unknown = {dimension: 0 for dimension in QUOTA_DIMENSIONS}
        values = {dimension: Counter() for dimension in QUOTA_DIMENSIONS}
        for endpoint_id, member in self.members.items():
            attributes = self.attributes.get(endpoint_id)
            for dimension in QUOTA_DIMENSIONS:
                value = attributes.dimension_value(dimension) if attributes is not None else None
                if value is None:
                    unknown[dimension] += 1
                else:
                    values[dimension][value] += 1
        conflicts = []
        for dimension in QUOTA_DIMENSIONS:
            limit = self.policy.quota_limit(dimension)
            for value, count in sorted(values[dimension].items()):
                if limit and count > limit:
                    conflicts.append((dimension, value, count))
        return ({dimension: count for dimension, count in unknown.items() if count}, tuple(conflicts))

    def status(self, *, deferred: bool = False) -> PoolStatus:
        served = self.served()
        shortfall = max(0, self.spec.desired - served)
        if deferred:
            # Nothing was evaluated in this call, so the last known state is reported
            # as it stands; claiming a fresh reason here would be a guess.
            state, reasons = self.spec.state, {}
            reason, next_attempt = self.spec.deficit_reason, self.spec.next_attempt_at
        else:
            state = (STATE_COMPLETE if shortfall == 0 else
                     STATE_EMPTY if served == 0 else STATE_DEGRADED)
            reasons = {code: count for code, count in self.reasons.items() if count}
            if shortfall and not reasons:
                # Nothing was admitted and nothing rejected a candidate: either a budget
                # stopped the call, or the source had nothing to give. Say which.
                reasons = {REASON_BUDGET if self.work.limited else REASON_NO_CANDIDATES: 1}
            reason = None if state == STATE_COMPLETE else _primary_reason(reasons)
            next_attempt = self.now + (self.policy.interval_seconds if state == STATE_COMPLETE
                                       else self.policy.retry_interval_seconds)
        quota_unknown, conflicts = self._quota_report()
        probation = self._ordered(state=MEMBER_PROBATION)
        overdue = tuple(member.endpoint_id for member in probation
                        if self.now > (_finite(member.admitted_at) or 0.0) + self.policy.probation_seconds)
        # The unit and the supply request travel with the status, so the caller
        # that starts the next measuring run cannot have to guess either of them.
        request = self.spec.find_request(served)
        return PoolStatus(
            pool_id=self.spec.id, state=state, at=self.now, collection_id=self.spec.collection_id,
            collection_kind=self.kind, profile_id=self.spec.profile_id,
            profile_revision=self.spec.profile_revision, desired=self.spec.desired,
            minimum=self.spec.minimum, reserve=self.spec.reserve,
            counts={phase: sum(1 for member in self.members.values() if member.state == phase)
                    for phase in MEMBER_STATES},
            served=served, shortfall=shortfall, below_minimum=served < self.spec.minimum,
            ready_for_clients=served > 0 and served >= self.spec.minimum,
            deficit_reason=reason, deficit_reasons=_ordered_reasons(reasons), next_attempt_at=next_attempt,
            budget_limit=self.work.limit, budget_spent=self.work.spent, admissions=self.work.admissions,
            promotions=self.work.promotions, re_admissions=self.work.re_admissions, deferred=deferred,
            recheck_due=tuple(member.endpoint_id for member in probation), probation_overdue=overdue,
            quota_unknown=quota_unknown, quota_conflicts=conflicts,
            source_errors=tuple(self.source_errors), count_unit=self.spec.count_unit,
            find=request.as_dict())

    def _ordered(self, state: str | None = None) -> list:
        members = [member for member in self.members.values() if state is None or member.state == state]
        return sorted(members, key=lambda member: member.endpoint_id)


def _freshness_reason(candidate: Candidate, now: float, require_valid_until: bool) -> str | None:
    """A pool does not serve an endpoint whose evidence is missing, in the future or expired.

    The admission contract has already judged the row; this is the same rule applied
    to the two timestamps the pool is handed, so a source that reports a row which
    has since expired cannot slip it in between two refills.
    """
    for value in (candidate.checked_at, candidate.valid_until):
        if value is not None and _finite(value) is None:
            return REASON_TIME_UNKNOWN
    checked = _finite(candidate.checked_at)
    if checked is not None and checked > now + CLOCK_TOLERANCE_SECONDS:
        return REASON_TIME_FUTURE
    valid = _finite(candidate.valid_until)
    if valid is None:
        return REASON_TIME_MISSING if require_valid_until else None
    if valid <= now:
        return REASON_TIME_EXPIRED
    return None


# --- public entry points ----------------------------------------------------

def refill(store: PoolStore, pool_id: str, source: Callable[..., Sequence[Candidate]], *,
           now: float | None = None, budget: int | None = None,
           verify: Callable[..., bool | None] | None = None) -> PoolStatus:
    """Move one pool towards its target and return what it can serve afterwards.

    ``source(spec, kind, budget, now)`` is asked for the ``reserve`` tier, then
    ``known``, then ``sources``, with the budget left in this call. It may
    return fewer candidates than asked for and may raise: a failing source is
    reported as ``E_POOL_SOURCE_ERROR`` and the target is left alone.

    ``verify(spec, member)`` re-measures a member in probation. Without it such
    a member is not served and is listed in ``recheck_due`` for whoever runs
    measurements. It is the caller's I/O, so it is called with no write
    transaction open and its verdicts are committed afterwards.

    This is an explicit command and always acts; the cadence guard that keeps a
    background loop from overworking a source lives in :func:`watch`, and the
    time the pool wants to be looked at next is in ``status.next_attempt_at``.

    The call is three steps, and the boundary between them is deliberate.
    Candidates are collected before the first write, so a source that dies
    leaves the committed target and the previous membership intact. The phase
    transitions commit on their own, so a process that dies while measuring
    leaves a pool whose members are all still accounted for — some of them
    resting — and whose target is simply not met yet; the next refill finishes
    it. Admissions and the status land in one transaction, so a caller never
    sees members without a status or a status without its members. What is
    deliberately *not* promised is that a crash between the two commits undoes
    the first one: the alternative would be to hold the write lock for the
    length of a network round trip, which is the worse failure.
    """
    spec = store.require(pool_id)
    at = time.time() if now is None else float(now)
    kind = store.collection_kind(spec.collection_id)
    if kind is None:
        # Fail closed: without a known collection kind the pool cannot tell a
        # private collection from public discovery, so it refuses to run.
        controller = _Controller(store, spec, None, now=at, work=_Work(0, spec.policy.scan_limit), verify=None)
        status = replace(controller.status(),
                         state=STATE_ERROR,
                         deficit_reason=REASON_UNKNOWN_COLLECTION,
                         next_attempt_at=at + spec.policy.retry_interval_seconds)
        with transaction(store.conn):
            store.save_status(pool_id, status.state, deficit_reason=status.deficit_reason,
                              next_attempt_at=status.next_attempt_at)
        return status

    work = _Work(spec.policy.refill_budget if budget is None else budget, spec.policy.scan_limit)
    controller = _Controller(store, spec, kind, now=at, work=work, verify=verify)
    candidates = _collect(spec, source, at, work, controller)
    # Phase 1: everything that only moves members between phases.  It commits on
    # its own, so the pool is in a consistent state before the slow part starts.
    with transaction(store.conn):
        controller._expire_cooldowns()
        controller._expire_proofs()
        controller._retire()
        controller._demote()
        controller._trim()
        controller._promote()
    # Phase 2: the measurements, with no write transaction open.  The lock is the
    # database's, not the pool's: a proxy that takes three seconds to answer must
    # not freeze every other writer of this file for those three seconds.
    verdicts = controller.measure()
    # Phase 3: the verdicts, the admissions and the status, in one transaction.
    with transaction(store.conn):
        controller.apply_measurements(verdicts)
        controller.admit(candidates)
        status = controller.status()
        store.save_status(pool_id, status.state, deficit_reason=status.deficit_reason,
                          next_attempt_at=status.next_attempt_at)
    return status


def _collect(spec: PoolSpec, source: Callable[..., Sequence[Candidate]], now: float, work: _Work,
             controller: _Controller) -> dict:
    """Ask the source for candidates, cheapest tier first, and keep what it offers.

    A pool with a quota asks even when it is not short, because a quota it cannot
    account for is a quota it cannot report; the walk is then bounded by the scan
    budget alone.
    """
    candidates: dict = {}
    describe = any(spec.policy.quota_limit(dimension) for dimension in QUOTA_DIMENSIONS)
    wanted = lambda: describe or controller.projected_demand() > 0  # noqa: E731
    if not wanted():
        return candidates
    if work.limited:
        controller.reasons[REASON_BUDGET] += 1
        return candidates
    offered = 0
    failed = False
    for kind in SOURCE_ORDER:
        if not wanted() or work.limited:
            break
        asked = min(max(1, work.scan_limit - work.scanned), work.remaining + len(controller.members))
        try:
            batch = list(source(spec, kind, asked, now) or ())
        except Exception as exc:  # a source is an external dependency, not a crash
            controller.source_errors.append(type(exc).__name__)
            controller.reasons[REASON_SOURCE_ERROR] += 1
            failed = True
            break
        work.scanned += len(batch)
        offered += len(batch)
        for candidate in batch:
            if candidate.endpoint_id and candidate.endpoint_id not in candidates:
                candidates[candidate.endpoint_id] = candidate
    if offered == 0 and not failed and not describe:
        controller.reasons[REASON_NO_CANDIDATES] += 1
    elif controller.projected_demand() > 0 and work.limited:
        controller.reasons[REASON_BUDGET] += 1
    return candidates


def report_health(store: PoolStore, pool_id: str, endpoint_id: str, *, ok: bool, now: float | None = None,
                  reason: str | None = None) -> HealthResult:
    """Record a measurement verdict for one member.

    A failure takes the endpoint out of service for ``cooldown_seconds`` and
    never deletes it: after the cooldown it is re-measured, and a success puts
    it back with a fresh ``admitted_at``. That is the difference between a pool
    and a list that only shrinks.

    A success for a member that is already in service only refreshes its proof.
    A success for a member that was resting takes it back into service, or into
    the reserve when the pool is already full, and is refused when both are
    full - the member is kept, not dropped.
    """
    spec = store.require(pool_id)
    at = time.time() if now is None else float(now)
    member = store.member(pool_id, endpoint_id)
    if member is None:
        return HealthResult(pool_id, endpoint_id, False, MEMBER_COOLDOWN, REASON_UNKNOWN_MEMBER)
    with transaction(store.conn):
        if not ok:
            store.set_member_state(pool_id, endpoint_id, MEMBER_COOLDOWN, released_at=at)
            return HealthResult(pool_id, endpoint_id, True, MEMBER_COOLDOWN, reason,
                                next_try_at=at + spec.policy.cooldown_seconds)
        if member.state == MEMBER_COOLDOWN:
            return HealthResult(pool_id, endpoint_id, False, member.state, reason,
                                next_try_at=(_finite(member.released_at) or at) + spec.policy.cooldown_seconds)
        state = MEMBER_ACTIVE
        if member.state != MEMBER_ACTIVE:
            states = [other.state for other in store.members(pool_id) if other.endpoint_id != endpoint_id]
            if states.count(MEMBER_ACTIVE) >= spec.desired:
                if states.count(MEMBER_RESERVE) >= spec.reserve:
                    return HealthResult(pool_id, endpoint_id, False, member.state, REASON_AT_CAPACITY)
                state = MEMBER_RESERVE
        store.set_member_state(pool_id, endpoint_id, state, admitted_at=at, released_at=None)
        return HealthResult(pool_id, endpoint_id, True, state, reason)


def evict(store: PoolStore, pool_id: str, endpoint_id: str, *, now: float | None = None,
          reason: str | None = None) -> HealthResult:
    """Take a member out of service now (denylist hit, profile change, user action)."""
    return report_health(store, pool_id, endpoint_id, ok=False, now=now, reason=reason)


def watch(store: PoolStore, pool_id: str, source: Callable[..., Sequence[Candidate]], *,
          ticks: int | None = None, verify: Callable[..., bool | None] | None = None,
          budget: int | None = None, clock: Clock | None = None,
          on_status: Callable[[PoolStatus], Any] | None = None) -> list:
    """Refill a pool once per ``interval_seconds``, ``ticks`` times or until stopped.

    This replaces the watch cycle that only re-checked the rows that already
    worked (defect 12): every tick re-evaluates the target, re-admits members
    that came back, and reports the reason for any shortfall.

    A tick that arrives before the pool's own ``next_attempt_at`` is not a
    failure and is not retried early: it is reported as deferred, which is the
    hard budget of this loop working. ``ticks=None`` is the background run that
    lasts until the process is interrupted; ``on_status`` returning ``False``
    stops either form.
    """
    if ticks is not None:
        ticks = _count(ticks, 'ticks')
    face = clock or Clock()
    statuses = []
    tick = 0
    while ticks is None or tick < ticks:
        at = face.now()
        stored = store.get(pool_id)
        if stored.next_attempt_at is not None and at < stored.next_attempt_at:
            controller = _Controller(store, stored, store.collection_kind(stored.collection_id), now=at,
                                     work=_Work(0, stored.policy.scan_limit), verify=None)
            status = controller.status(deferred=True)
        else:
            status = refill(store, pool_id, source, now=at, budget=budget, verify=verify)
        statuses.append(status)
        if on_status is not None and on_status(status) is False:
            break
        tick += 1
        if ticks is None or tick < ticks:
            face.sleep(stored.policy.interval_seconds)
    return statuses


class _Stopped(Exception):
    """The watcher was asked to stop; the loop unwinds instead of sleeping."""


class _InterruptibleClock(Clock):
    """A clock whose sleep returns the moment the watcher is asked to stop.

    Without this a ``pause`` would wait out the whole interval before the loop
    noticed, which reads as "pause does nothing" to whoever pressed it.
    """

    def __init__(self, stop: threading.Event, base: Clock):
        self._stop = stop
        self._base = base

    def now(self) -> float:
        return self._base.now()

    def sleep(self, seconds: float) -> None:
        if self._stop.wait(max(0.0, float(seconds))):
            raise _Stopped()


class WatchRegistry:
    """One background :func:`watch` per pool, started and stopped with the pool.

    :func:`watch` was the only half of F14's "watch/refill restores the pool"
    that nothing started: a pool created with ``desired=5`` was refilled once
    by whoever pressed the button and then stayed at whatever it reached.  The
    loop is not a new engine -- it is :func:`watch` unchanged, one thread per
    watched pool, bound to the pool's own lifecycle so it cannot outlive the
    thing it maintains.

    Three things it deliberately does not do:

    * it never runs twice for one pool.  ``start`` on a pool that is already
      watched reports ``already_running`` instead of adding a second loop;
      two loops would double the work and neither would show up in the status.
    * it does not outlive the pool.  ``stop`` -- which the ``pause`` path
      calls -- ends the loop, and a loop that finds its pool in ``error`` ends
      itself rather than retrying forever behind the user's back.
    * it does not spend more than it was given.  Every tick goes through
      :func:`refill`, the same budget-enforcing call the manual ``pool
      refill`` makes, and the pool's own ``next_attempt_at`` still defers a
      tick that arrives early.  A watch therefore spends at most one
      ``refill_budget`` per interval, which is the promise the policy already
      made.
    """

    def __init__(self, open_store: Callable[[], PoolStore] | None = None, *,
                 clock: Clock | None = None):
        self._open_store = open_store
        self._clock = clock or Clock()
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}
        self._stops: dict[str, threading.Event] = {}
        self._ticks: dict[str, int] = {}
        self._errors: dict[str, str] = {}

    def watching(self, pool_id: str) -> bool:
        with self._lock:
            thread = self._threads.get(str(pool_id))
        return bool(thread and thread.is_alive())

    def start(self, pool_id: str, source: Callable[..., Sequence[Candidate]] | None = None, *,
              source_factory: Callable[[Any], Callable[..., Sequence[Candidate]]] | None = None,
              verify: Callable[..., bool | None] | None = None,
              budget: int | None = None) -> dict:
        """Begin watching one pool; report what happened instead of doing it twice.

        ``source_factory(conn)`` is the form a long-running surface uses.  A
        candidate source reads the database, and a SQLite connection belongs to
        the thread that opened it, so a source built on the caller's connection
        raises ``ProgrammingError`` the moment the loop asks it for a tier --
        which reads as "the pool has no candidates" rather than as a threading
        mistake.  The factory is therefore called *inside* the loop, on the
        connection the loop opened itself.  ``source`` stays for a caller that
        has a source which does not touch the database.
        """
        key = str(pool_id)
        if self._open_store is None:
            raise PoolError('E_STATE_NOT_FOUND',
                            'watch_registry(data) must be called before a pool is watched')
        if source is None and source_factory is None:
            raise PoolError('E_VALIDATION_FIELD',
                            'a watch needs a candidate source or a source_factory')
        with self._lock:
            running = self._threads.get(key)
            if running is not None and running.is_alive():
                return {'pool_id': key, 'watching': True, 'started': False,
                        'reason': 'already_running', 'ticks': self._ticks.get(key, 0)}
            stop = threading.Event()
            thread = threading.Thread(target=self._run, args=(key, source, stop),
                                      kwargs={'source_factory': source_factory,
                                              'verify': verify, 'budget': budget},
                                      name=f'pool-watch-{key}', daemon=True)
            self._threads[key] = thread
            self._stops[key] = stop
            self._ticks[key] = 0
            self._errors.pop(key, None)
        thread.start()
        return {'pool_id': key, 'watching': True, 'started': True, 'reason': '',
                'ticks': 0}

    def stop(self, pool_id: str, *, timeout: float = 5.0) -> dict:
        """End the loop for one pool.  Stopping a pool that is not watched is fine."""
        key = str(pool_id)
        with self._lock:
            stop = self._stops.pop(key, None)
            thread = self._threads.get(key)
            ticks = self._ticks.get(key, 0)
        if stop is not None:
            stop.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        with self._lock:
            alive = bool(self._threads.get(key) and self._threads[key].is_alive())
            if not alive:
                self._threads.pop(key, None)
        return {'pool_id': key, 'watching': alive, 'stopped': not alive, 'ticks': ticks}

    def status(self) -> dict:
        with self._lock:
            return {'watching': {key: {'ticks': self._ticks.get(key, 0),
                                       'error': self._errors.get(key, '')}
                                 for key, thread in sorted(self._threads.items())},
                    'errors': dict(self._errors)}

    def stop_all(self, *, timeout: float = 5.0) -> None:
        for key in list(self.status()['watching']):
            self.stop(key, timeout=timeout)

    # -- the loop itself ---------------------------------------------------

    def _run(self, pool_id, source, stop, *, source_factory, verify, budget):
        def on_status(status):
            with self._lock:
                self._ticks[pool_id] = self._ticks.get(pool_id, 0) + 1
            # The stop event is the pause path; an error state is the other
            # honest end: a pool that cannot be filled stays unwatched instead
            # of being retried forever behind the user's back.
            return not (stop.is_set() or status.state == STATE_ERROR)

        try:
            while not stop.is_set():
                store = None
                try:
                    store = self._open_store()
                    # Built here, on this thread's connection: see `start`.
                    ticker = source_factory(store.conn) if source_factory is not None else source
                    watch(store, pool_id, ticker, verify=verify, budget=budget,
                          clock=_InterruptibleClock(stop, self._clock), on_status=on_status)
                finally:
                    if store is not None:
                        with contextlib.suppress(Exception):
                            store.conn.close()
                with self._lock:
                    if self._stops.get(pool_id) is not stop:
                        return
        except _Stopped:
            return
        except Exception as exc:  # noqa: BLE001 - a watcher must not die silently
            with self._lock:
                self._errors[pool_id] = f'{type(exc).__name__}: {exc}'


#: The one registry of the process.  Both ways a user starts a pool -- the
#: ``/v1`` route and the GUI button -- go through this object, so "is the pool
#: being watched?" has one answer instead of one per surface.  It is created
#: without a store: the module must be importable before any database exists,
#: and the first surface to start a pool binds its data folder.
WATCHES = WatchRegistry()


def watch_registry(data=None) -> WatchRegistry:
    """The process-wide registry, pointed at ``data`` the first time it is asked.

    Both the API server and the GUI know their data folder before they serve a
    request, so the binding happens at start-up rather than inside a tick.
    """
    if data is not None:
        target = Path(data) / 'proxies.sqlite3'
        WATCHES._open_store = lambda: PoolStore.open(target)
    return WATCHES


def refill_all(store: PoolStore, source: Callable[..., Sequence[Candidate]], *, now: float | None = None,
               budget: int | None = None, verify: Callable[..., bool | None] | None = None) -> dict:
    """Refill every named pool under one shared budget.

    Each pool is still limited by its own ``refill_budget``; ``budget`` caps the
    total spent across all of them, so a background run cannot quietly do N
    times more work than intended.
    """
    at = time.time() if now is None else float(now)
    total = None if budget is None else max(0, int(budget))
    statuses = {}
    for spec in store.list():
        if total is None:
            statuses[spec.id] = refill(store, spec.id, source, now=at, verify=verify)
            continue
        statuses[spec.id] = refill(store, spec.id, source, now=at, budget=total, verify=verify)
        total -= statuses[spec.id].budget_spent
    return statuses
