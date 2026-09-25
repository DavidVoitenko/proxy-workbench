"""Job and item lifecycle: one persisted run of the workbench.

A job fixes its input — collection, profile revision, item list — when it is
created, and keeps every finished observation across pause, cancel and crash.
A repeat of the same request with the same idempotency key returns the same job
instead of a second one, and ``resume`` works on the items the job was created
with: it never widens the scope and never re-uses membership that has since
left the collection.

State lives in the four tables of CONTRACTS.ru.md §3.3 migration 6 (``job``,
``job_item``, ``job_event``, ``checkpoint``), which this module co-owns with
``db.py``: :func:`install_schema` is the single DDL statement set for them, so
the migrator and the store cannot drift apart. Nothing outside those tables is
written, and no job is ever deleted — a recheck is a new job with new items
(§6.3), so the previous run keeps its history, its last completed result and
its queue.

This module owns state only. Driving items (claiming, probing, publishing) is
`pipeline.py`'s job; a second worker loop is deliberately not introduced here.

Contract: CONTRACTS.ru.md §6 (job and item states), §3.3 (migration 6),
§5.4 (error codes), §5.6 (structured events), §6.4 (one DB writer).
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

#: Migration number that owns the four tables below (CONTRACTS §3.3).
MIGRATION = 6

SCHEMA = (
    'CREATE TABLE IF NOT EXISTS job('
    'id TEXT PRIMARY KEY, kind TEXT, state TEXT, scope_json TEXT, input_digest TEXT,'
    ' profile_id TEXT, profile_revision INTEGER, collection_id TEXT, idempotency_key TEXT,'
    ' created_at REAL, started_at REAL, finished_at REAL)',
    'CREATE TABLE IF NOT EXISTS job_item('
    'job_id TEXT NOT NULL REFERENCES job(id), item_id TEXT NOT NULL, endpoint_id TEXT,'
    ' access_id TEXT, access_revision INTEGER, state TEXT, observation_id TEXT, error_code TEXT,'
    ' PRIMARY KEY(job_id, item_id))',
    'CREATE TABLE IF NOT EXISTS job_event('
    'job_id TEXT, seq INTEGER, at REAL, type TEXT, code TEXT, data_json TEXT,'
    ' PRIMARY KEY(job_id, seq))',
    'CREATE TABLE IF NOT EXISTS checkpoint('
    'job_id TEXT, name TEXT, at REAL, state_json TEXT, PRIMARY KEY(job_id, name))',
)

_JOB_COLUMNS = ('id', 'kind', 'state', 'scope_json', 'input_digest', 'profile_id',
                'profile_revision', 'collection_id', 'idempotency_key', 'created_at',
                'started_at', 'finished_at')
_ITEM_COLUMNS = ('job_id', 'item_id', 'endpoint_id', 'access_id', 'access_revision',
                 'state', 'observation_id', 'error_code')
_EVENT_COLUMNS = ('job_id', 'seq', 'at', 'type', 'code', 'data_json')
_CHECKPOINT_COLUMNS = ('job_id', 'name', 'at', 'state_json')

JOB_STATES = ('created', 'queued', 'running', 'paused', 'succeeded', 'partial',
              'failed', 'cancelled', 'timed_out')
TERMINAL_JOB_STATES = frozenset({'succeeded', 'partial', 'failed', 'cancelled', 'timed_out'})
ACTIVE_JOB_STATES = frozenset({'queued', 'running'})
ITEM_STATES = ('pending', 'prefiltered', 'probing', 'done', 'unreachable', 'blocked',
               'partial', 'failed')
TERMINAL_ITEM_STATES = frozenset({'done', 'unreachable', 'blocked', 'partial', 'failed'})
UNFINISHED_ITEM_STATES = tuple(state for state in ITEM_STATES if state not in TERMINAL_ITEM_STATES)

#: Job state machine of CONTRACTS §6.2.
JOB_TRANSITIONS = {
    'created': frozenset({'queued', 'cancelled', 'failed'}),
    'queued': frozenset({'running', 'paused', 'cancelled', 'timed_out', 'failed'}),
    'running': frozenset({'paused', 'succeeded', 'partial', 'failed', 'cancelled', 'timed_out'}),
    'paused': frozenset({'queued', 'cancelled', 'failed', 'timed_out'}),
    'succeeded': frozenset(),
    'partial': frozenset(),
    'failed': frozenset(),
    'cancelled': frozenset(),
    'timed_out': frozenset(),
}

#: Item state machine of CONTRACTS §6.3.  Terminal states are absorbing here:
#: a repeated check is a new item of a new job, never a rewritten one.
ITEM_TRANSITIONS = {
    'pending': frozenset({'prefiltered', 'probing', 'blocked'}),
    'prefiltered': frozenset({'probing', 'blocked'}),
    'probing': frozenset({'done', 'unreachable', 'blocked', 'partial', 'failed'}),
    'done': frozenset(),
    'unreachable': frozenset(),
    'blocked': frozenset(),
    'partial': frozenset(),
    'failed': frozenset(),
}

#: `state_detail` values shared with the snapshot contract (CONTRACTS §4.3).
DETAIL_COMPLETE = 'complete'
DETAIL_EMPTY = 'empty_no_match'
DETAIL_ALL_FAILED = 'all_failed'
DETAIL_BUDGET = 'budget_exhausted'
DETAIL_STOPPED = 'stopped'
DETAIL_CANCELLED = 'cancelled'
DETAIL_CRASHED = 'crashed'

#: Item state and code for a queued address that left the collection after the
#: job was created: `blocked` means "refused before measuring", never "checked".
CODE_OBSOLETE_MEMBERSHIP = 'OBSOLETE_MEMBERSHIP'
CODE_OK = 'OK'
CODE_INTERRUPTED = 'E_STATE_JOB_INTERRUPTED'

#: Checkpoint rows the store writes itself; callers may not overwrite them.
RESERVED_CHECKPOINTS = ('heartbeat', 'suspend', 'interrupt')

DEFAULT_MAX_QUEUE_ITEMS = 1_000_000
DEFAULT_HEARTBEAT_TIMEOUT_S = 300.0

#: Payload keys refused in events and checkpoints: an event is persisted in
#: cleartext and travels to every consumer, so credentials never enter it
#: (CONTRACTS §5.6, F29).  `access_id` and `secret_ref` stay allowed.
FORBIDDEN_PAYLOAD_KEYS = frozenset({
    'password', 'passwd', 'secret', 'token', 'credentials', 'credential', 'api_key',
    'authorization', 'auth', 'userinfo', 'proxy_auth', 'bearer',
})
_USERINFO_RE = re.compile(r'(?i)[a-z][a-z0-9+.\-]*://[^/\s:@]+:[^/\s@]*@')
_CHECKPOINT_NAME_RE = re.compile(r'[A-Za-z0-9_.\-]{1,64}\Z')


class JobError(Exception):
    """Base class of every refusal this module makes, carrying a §5.4 code."""

    code = 'E_STATE_JOB_TRANSITION'

    def __init__(self, detail: str = '', code: str | None = None):
        super().__init__(f'{code or type(self).code}: {detail}' if detail else (code or type(self).code))
        self.detail = detail
        if code is not None:
            self.code = code


class JobNotFound(JobError):
    code = 'E_STATE_JOB_NOT_FOUND'


class ItemNotFound(JobError):
    code = 'E_STATE_ITEM_NOT_FOUND'


class Busy(JobError):
    code = 'E_CONFLICT_BUSY'


class Conflict(JobError):
    code = 'E_CONFLICT_REVISION'


class IdempotencyConflict(Conflict):
    code = 'E_CONFLICT_IDEMPOTENCY'


class StateConflict(JobError):
    code = 'E_STATE_JOB_TRANSITION'


class Validation(JobError):
    code = 'E_VALIDATION_FIELD'


class QueueLimit(JobError):
    code = 'E_LIMIT_QUEUE'


def _plain(value: Any) -> Any:
    """Materialise frozen containers so ``json`` can serialise them."""
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _freeze(value: Any) -> Any:
    """Deep-freeze a JSON-shaped value so a scope cannot change after creation."""
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _canonical(value: Any) -> str:
    return json.dumps(_plain(value), sort_keys=True, separators=(',', ':'), allow_nan=False)


def _text(value: Any, field_name: str, *, max_length: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip():
        raise Validation(f'{field_name} must be a non-empty string')
    if len(value) > max_length:
        raise Validation(f'{field_name} is longer than {max_length} characters')
    return value


def _check_public_payload(value: Any, path: str = 'data') -> None:
    """Refuse secrets in anything that is persisted or streamed (F29, §5.6).

    Two shapes are detectable and are refused: a key that names a credential,
    and a value that carries URL userinfo.  Free text is the caller's
    responsibility — a diagnostic string must be a message of `diagnostics.py`,
    never a copy of an exception or a response body.
    """
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in FORBIDDEN_PAYLOAD_KEYS:
                raise Validation(f'{path}.{key} must not carry credentials')
            _check_public_payload(item, f'{path}.{key}')
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _check_public_payload(item, f'{path}[{index}]')
    elif isinstance(value, str):
        if _USERINFO_RE.search(value):
            raise Validation(f'{path} must not carry URL userinfo')


def _digest(*parts: Any) -> str:
    return hashlib.sha256(_canonical(list(parts)).encode('utf-8')).hexdigest()


def new_job_id() -> str:
    """Job ids are opaque and unique, like the ones the GUI already hands out."""
    return 'job-' + secrets.token_hex(8)


def _input_digest(kind: str, scope: Scope, queue: Sequence[QueueItem]) -> str:
    """What a job was asked to do: kind, scope and the exact queue of the request."""
    return _digest(kind, scope.canonical(),
                   [[item.item_id, item.endpoint_id, item.access_id, item.access_revision]
                    for item in queue])


@dataclass(frozen=True)
class Scope:
    """The input of a job, fixed when the job is created.

    ``profile_revision`` is part of it, so a job always names the rules it ran
    under; changing the profile afterwards cannot silently retarget a finished
    job (§6.2, `E_CONFLICT_REVISION`).
    """

    collection_id: str
    profile_id: str
    profile_revision: int
    profile_digest: str = ''
    filters: Mapping[str, Any] = field(default_factory=dict)
    budgets: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, 'collection_id', _text(self.collection_id, 'collection_id', max_length=256))
        object.__setattr__(self, 'profile_id', _text(self.profile_id, 'profile_id', max_length=256))
        if not isinstance(self.profile_revision, int) or isinstance(self.profile_revision, bool):
            raise Validation('profile_revision must be an integer')
        if self.profile_revision < 1:
            raise Validation('profile_revision starts at 1')
        object.__setattr__(self, 'profile_digest', str(self.profile_digest or ''))
        filters, budgets = _freeze(self.filters), _freeze(self.budgets)
        try:
            _canonical({'filters': filters, 'budgets': budgets})
        except (TypeError, ValueError) as exc:
            raise Validation(f'scope must be JSON: {exc}') from None
        object.__setattr__(self, 'filters', filters)
        object.__setattr__(self, 'budgets', budgets)

    @property
    def timeout_s(self) -> float | None:
        """Whole-job deadline in seconds (CONTRACTS §6.2), not a per-request one."""
        value = self.budgets.get('timeout_s') if 'timeout_s' in self.budgets else None
        if value is None:
            return None
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise Validation('budgets.timeout_s must be a positive number of seconds')
        return float(value)

    def canonical(self) -> str:
        return _canonical({
            'collection_id': self.collection_id,
            'profile_id': self.profile_id,
            'profile_revision': self.profile_revision,
            'profile_digest': self.profile_digest,
            'filters': self.filters,
            'budgets': self.budgets,
        })

    def digest(self) -> str:
        return _digest(self.canonical())

    def to_json(self) -> str:
        return self.canonical()

    @classmethod
    def from_json(cls, text: str | None) -> 'Scope':
        raw = json.loads(text or '{}')
        return cls(collection_id=raw.get('collection_id') or '', profile_id=raw.get('profile_id') or '',
                   profile_revision=int(raw.get('profile_revision') or 1),
                   profile_digest=raw.get('profile_digest') or '',
                   filters=raw.get('filters') or {}, budgets=raw.get('budgets') or {})


@dataclass(frozen=True)
class QueueItem:
    """One queued unit of work: an endpoint, optionally with its access revision."""

    endpoint_id: str
    item_id: str = ''
    access_id: str | None = None
    access_revision: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, 'endpoint_id', _text(self.endpoint_id, 'endpoint_id', max_length=256))
        object.__setattr__(self, 'item_id', self.item_id or self.endpoint_id)
        _text(self.item_id, 'item_id', max_length=256)
        if self.access_revision is not None:
            if not isinstance(self.access_revision, int) or isinstance(self.access_revision, bool):
                raise Validation('access_revision must be an integer')

    def to_json(self) -> dict:
        return dict(endpoint_id=self.endpoint_id, item_id=self.item_id,
                    access_id=self.access_id, access_revision=self.access_revision)


@dataclass(frozen=True)
class Job:
    """A persisted run.  ``state`` and ``input_digest`` never change after creation."""

    id: str
    kind: str
    state: str
    scope: Scope
    input_digest: str
    profile_id: str
    profile_revision: int
    collection_id: str
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    idempotency_key: str | None = None

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_JOB_STATES

    @property
    def active(self) -> bool:
        return self.state in ACTIVE_JOB_STATES

    def to_json(self) -> dict:
        return {
            'id': self.id, 'kind': self.kind, 'state': self.state, 'stream': stream_id(self.id),
            'scope': json.loads(self.scope.to_json()), 'input_digest': self.input_digest,
            'profile_id': self.profile_id, 'profile_revision': self.profile_revision,
            'collection_id': self.collection_id, 'created_at': self.created_at,
            'started_at': self.started_at, 'finished_at': self.finished_at,
        }


@dataclass(frozen=True)
class JobItem:
    """One unit of the queue.  Identified by ``(job_id, item_id)`` (§6.3)."""

    job_id: str
    item_id: str
    state: str
    endpoint_id: str | None = None
    access_id: str | None = None
    access_revision: int | None = None
    observation_id: str | None = None
    error_code: str | None = None

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_ITEM_STATES

    def to_json(self) -> dict:
        return {
            'job_id': self.job_id, 'item_id': self.item_id, 'stream': stream_id(self.job_id),
            'state': self.state, 'endpoint_id': self.endpoint_id, 'access_id': self.access_id,
            'access_revision': self.access_revision, 'observation_id': self.observation_id,
            'error_code': self.error_code,
        }


@dataclass(frozen=True)
class JobEvent:
    """One structured event (§5.6).  ``seq`` is gapless per job, starting at 1."""

    job_id: str
    seq: int
    at: float
    type: str
    code: str
    data: Mapping[str, Any] = field(default_factory=dict)
    item_id: str | None = None

    def to_json(self) -> dict:
        return {
            'stream': stream_id(self.job_id), 'seq': self.seq, 'at': self.at, 'type': self.type,
            'job_id': self.job_id, 'item_id': self.item_id, 'code': self.code,
            'data': json.loads(_canonical(self.data)),
        }


@dataclass(frozen=True)
class Progress:
    """Structured progress of a job, read in one grouped query."""

    job_id: str
    state: str
    state_detail: str | None
    by_state: Mapping[str, int]
    total: int
    finished: int
    unfinished: int
    last_seq: int
    elapsed_s: float
    remaining_s: float | None
    deadline_at: float | None
    slept_s: float
    last_checkpoint: Mapping[str, Any] | None

    def to_json(self) -> dict:
        return {
            'stream': stream_id(self.job_id), 'job_id': self.job_id, 'state': self.state,
            'state_detail': self.state_detail, 'total': self.total, 'finished': self.finished,
            'unfinished': self.unfinished, 'by_state': dict(self.by_state),
            'last_seq': self.last_seq, 'elapsed_s': self.elapsed_s,
            'remaining_s': self.remaining_s, 'deadline_at': self.deadline_at,
            'slept_s': self.slept_s,
            'last_checkpoint': dict(self.last_checkpoint) if self.last_checkpoint else None,
        }


@dataclass(frozen=True)
class Recovery:
    """What :meth:`JobStore.recover` found after an unclean stop."""

    jobs: tuple[str, ...]
    requeued_items: int
    observations_kept: int

    def to_json(self) -> dict:
        return {'jobs': list(self.jobs), 'requeued_items': self.requeued_items,
                'observations_kept': self.observations_kept}


def stream_id(job_id: str) -> str:
    """Event stream name of a job (§5.7: a stream always exists before its first event)."""
    return f'job:{job_id}'


def install_schema(conn: sqlite3.Connection) -> None:
    """Create the four job tables of migration 6; idempotent and additive.

    `db.py` calls this from migration 6 so there is exactly one DDL statement
    set for these tables.  It writes nothing else and never migrates a column.
    """
    conn.execute('BEGIN IMMEDIATE')
    try:
        for statement in SCHEMA:
            conn.execute(statement)
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            conn.execute('ROLLBACK')
        raise
    conn.execute('COMMIT')


class WriterGate:
    """At most one DB-writing executor at a time (CONTRACTS §6.4).

    In process this is a plain non-reentrant lock.  Across processes the
    integrator passes the existing `workbench.lock` callback as ``os_lock``, so
    this module never grows a second file-lock implementation; that callback is
    expected to raise ``OSError`` when the lock is taken.
    """

    def __init__(self, *, os_lock: Any = None) -> None:
        self._lock = threading.Lock()
        self._owner: str | None = None
        self._os_lock = os_lock

    @property
    def owner(self) -> str | None:
        return self._owner

    @property
    def busy(self) -> bool:
        return self._owner is not None

    @contextlib.contextmanager
    def hold(self, owner: str, path: Any = None):
        owner = _text(owner, 'owner', max_length=256)
        if not self._lock.acquire(blocking=False):
            raise Busy(f'the database is being written by {self._owner}')
        try:
            if self._os_lock is None:
                self._owner = owner
                yield owner
                return
            try:
                with self._os_lock(path):
                    self._owner = owner
                    yield owner
            except OSError as exc:
                raise Busy(f'database is locked: {exc}') from None
        finally:
            self._owner = None
            self._lock.release()


def _summarize_ids(ids: Sequence[str], limit: int = 50) -> dict:
    """Event payload for a potentially huge id list: count plus a bounded sample."""
    ordered = sorted(ids)
    payload: dict[str, Any] = {'count': len(ordered)}
    if ordered:
        payload['items'] = ordered[:limit]
        payload['truncated'] = len(ordered) > limit
    return payload


class JobStore:
    """Persistent job and item state over one SQLite connection.

    The store owns its transactions: every mutation runs in ``BEGIN IMMEDIATE``,
    so with the single-writer rule of §6.4 two callers can never interleave a
    read-modify-write (idempotency, item claims, event sequence numbers).
    """

    def __init__(self, conn: sqlite3.Connection, *, clock=time.time,
                 max_queue_items: int = DEFAULT_MAX_QUEUE_ITEMS) -> None:
        self._conn = conn
        self._clock = clock
        self.max_queue_items = int(max_queue_items)

    # -- plumbing ---------------------------------------------------------

    def _now(self, now: float | None = None) -> float:
        return float(self._clock() if now is None else now)

    @contextlib.contextmanager
    def _write(self):
        conn = self._conn
        saved = conn.isolation_level
        if saved is not None:
            conn.isolation_level = None
        try:
            conn.execute('BEGIN IMMEDIATE')
            try:
                yield conn
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute('ROLLBACK')
                raise
            conn.execute('COMMIT')
        finally:
            if saved is not None:
                conn.isolation_level = saved

    @staticmethod
    def _job_from_row(row: Sequence[Any]) -> Job:
        values = dict(zip(_JOB_COLUMNS, row))
        return Job(
            id=values['id'], kind=values['kind'], state=values['state'],
            scope=Scope.from_json(values['scope_json']), input_digest=values['input_digest'],
            profile_id=values['profile_id'], profile_revision=values['profile_revision'],
            collection_id=values['collection_id'], created_at=values['created_at'],
            started_at=values['started_at'], finished_at=values['finished_at'],
            idempotency_key=values['idempotency_key'],
        )

    @staticmethod
    def _item_from_row(row: Sequence[Any]) -> JobItem:
        values = dict(zip(_ITEM_COLUMNS, row))
        return JobItem(
            job_id=values['job_id'], item_id=values['item_id'], state=values['state'],
            endpoint_id=values['endpoint_id'], access_id=values['access_id'],
            access_revision=values['access_revision'], observation_id=values['observation_id'],
            error_code=values['error_code'],
        )

    def _job(self, conn: sqlite3.Connection, job_id: str) -> Job:
        _text(job_id, 'job_id', max_length=256)
        row = conn.execute(
            f'SELECT {", ".join(_JOB_COLUMNS)} FROM job WHERE id=?', (job_id,)).fetchone()
        if row is None:
            raise JobNotFound(job_id)
        return self._job_from_row(row)

    def _item(self, conn: sqlite3.Connection, job_id: str, item_id: str) -> JobItem:
        row = conn.execute(
            f'SELECT {", ".join(_ITEM_COLUMNS)} FROM job_item WHERE job_id=? AND item_id=?',
            (job_id, item_id)).fetchone()
        if row is None:
            raise ItemNotFound(f'{job_id}/{item_id}')
        return self._item_from_row(row)

    def _emit(self, conn: sqlite3.Connection, job_id: str, type_: str, code: str,
              data: Mapping[str, Any] | None = None, *, item_id: str | None = None,
              at: float | None = None) -> JobEvent:
        payload: dict[str, Any] = dict(data or {})
        if item_id is not None:
            payload['item_id'] = item_id
        _check_public_payload(payload)
        row = conn.execute('SELECT max(seq) FROM job_event WHERE job_id=?', (job_id,)).fetchone()
        seq = 1 if row is None or row[0] is None else int(row[0]) + 1
        stamp = self._now(at)
        conn.execute(
            'INSERT INTO job_event (job_id, seq, at, type, code, data_json) VALUES (?,?,?,?,?,?)',
            (job_id, seq, stamp, type_, code, _canonical(payload)))
        return JobEvent(job_id=job_id, seq=seq, at=stamp, type=type_, code=code,
                        data=MappingProxyType(payload), item_id=item_id)

    def _put_checkpoint(self, conn: sqlite3.Connection, job_id: str, name: str,
                        state: Mapping[str, Any], at: float | None = None) -> dict:
        _check_public_payload(state, f'checkpoint.{name}')
        stamp = self._now(at)
        conn.execute(
            'INSERT INTO checkpoint (job_id, name, at, state_json) VALUES (?,?,?,?)'
            ' ON CONFLICT(job_id, name) DO UPDATE SET at=excluded.at, state_json=excluded.state_json',
            (job_id, name, stamp, _canonical(state)))
        return {'name': name, 'at': stamp, 'state': dict(state)}

    def _get_checkpoint(self, conn: sqlite3.Connection, job_id: str, name: str) -> dict | None:
        row = conn.execute(
            f'SELECT {", ".join(_CHECKPOINT_COLUMNS)} FROM checkpoint WHERE job_id=? AND name=?',
            (job_id, name)).fetchone()
        if row is None:
            return None
        values = dict(zip(_CHECKPOINT_COLUMNS, row))
        return {'name': values['name'], 'at': values['at'], 'state': json.loads(values['state_json'] or '{}')}

    def _control_replayed(self, conn: sqlite3.Connection, job_id: str, action: str,
                          idempotency_key: str) -> bool:
        rows = conn.execute(
            'SELECT data_json FROM job_event WHERE job_id=? AND type=? ORDER BY seq',
            (job_id, 'job.state')).fetchall()
        for (raw,) in rows:
            data = json.loads(raw or '{}')
            if data.get('action') == action and data.get('idempotency_key') == idempotency_key:
                return True
        return False

    def _insert_items(self, conn: sqlite3.Connection, job_id: str, items: Iterable[QueueItem]) -> int:
        seen: set[str] = set()
        rows = []
        for item in items:
            if item.item_id in seen:
                raise Validation(f'duplicate item_id {item.item_id}: one item is checked once')
            seen.add(item.item_id)
            rows.append((job_id, item.item_id, item.endpoint_id, item.access_id,
                         item.access_revision, 'pending'))
        if not rows:
            return 0
        if len(rows) > self.max_queue_items:
            raise QueueLimit(f'{len(rows)} items exceed max_queue_items={self.max_queue_items}')
        try:
            conn.executemany(
                'INSERT INTO job_item (job_id, item_id, endpoint_id, access_id, access_revision, state)'
                ' VALUES (?,?,?,?,?,?)', rows)
        except sqlite3.IntegrityError as exc:
            raise Validation(f'item already queued: {exc}') from None
        return len(rows)

    def _set_state(self, conn: sqlite3.Connection, job: Job, new_state: str, *,
                   at: float | None = None, reason_code: str | None = None,
                   event_data: Mapping[str, Any] | None = None) -> Job:
        if new_state not in JOB_TRANSITIONS.get(job.state, frozenset()):
            raise StateConflict(f'{job.state} -> {new_state} is not a job transition')
        stamp = self._now(at)
        finished = stamp if new_state in TERMINAL_JOB_STATES else None
        conn.execute(
            'UPDATE job SET state=?, started_at=coalesce(started_at, ?), finished_at=? WHERE id=?',
            (new_state, stamp if new_state == 'running' else None, finished, job.id))
        payload = {'from': job.state, 'to': new_state}
        payload.update(event_data or {})
        self._emit(conn, job.id, 'job.state', reason_code or CODE_OK, payload, at=stamp)
        return self._job(conn, job.id)

    def _requeue(self, conn: sqlite3.Connection, job_id: str,
                 states: Sequence[str] = UNFINISHED_ITEM_STATES) -> list[str]:
        """Send unfinished items back to ``pending``; observations stay attached."""
        placeholders = ','.join('?' * len(states))
        rows = conn.execute(
            f'SELECT item_id FROM job_item WHERE job_id=? AND state IN ({placeholders})',
            (job_id, *states)).fetchall()
        ids = [row[0] for row in rows]
        if not ids:
            return []
        conn.execute(
            f'UPDATE job_item SET state=? WHERE job_id=? AND state IN ({placeholders})',
            ('pending', job_id, *states))
        return ids

    # -- creation ---------------------------------------------------------

    def create_job(self, kind: str, scope: Scope, items: Iterable[QueueItem] = (), *,
                   idempotency_key: str | None = None, job_id: str | None = None) -> Job:
        """Create a ``created`` job with its scope and queue fixed.

        With an ``idempotency_key`` the request is idempotent (F11): the same
        key and the same input return the same job, the same key with a
        different input is ``E_CONFLICT_IDEMPOTENCY``.
        """
        kind = _text(kind, 'kind', max_length=64)
        if not isinstance(scope, Scope):
            raise Validation('scope must be a Scope')
        queue = list(items)
        digest = _input_digest(kind, scope, queue)
        with self._write() as conn:
            if idempotency_key is not None:
                _text(idempotency_key, 'idempotency_key', max_length=256)
                row = conn.execute(
                    f'SELECT {", ".join(_JOB_COLUMNS)} FROM job WHERE idempotency_key=?',
                    (idempotency_key,)).fetchone()
                if row is not None:
                    existing = self._job_from_row(row)
                    if existing.input_digest != digest:
                        raise IdempotencyConflict(
                            f'key {idempotency_key!r} is already bound to job {existing.id}')
                    return existing
            job_id = job_id or new_job_id()
            if conn.execute('SELECT 1 FROM job WHERE id=?', (job_id,)).fetchone() is not None:
                raise Conflict(f'job {job_id} already exists')
            stamp = self._now()
            conn.execute(
                'INSERT INTO job (id, kind, state, scope_json, input_digest, profile_id,'
                ' profile_revision, collection_id, idempotency_key, created_at, started_at, finished_at)'
                ' VALUES (?,?,?,?,?,?,?,?,?,?,NULL,NULL)',
                (job_id, kind, 'created', scope.to_json(), digest, scope.profile_id,
                 scope.profile_revision, scope.collection_id, idempotency_key, stamp))
            queued = self._insert_items(conn, job_id, queue)
            self._emit(conn, job_id, 'job.state', CODE_OK,
                       {'from': None, 'to': 'created', 'kind': kind, 'queued': queued,
                        'input_digest': digest}, at=stamp)
            return self._job(conn, job_id)

    def submit(self, kind: str, scope: Scope, items: Iterable[QueueItem] = (), *,
               idempotency_key: str | None = None) -> Job:
        """Create and queue a job in one call — what ``POST /v1/jobs`` needs."""
        return self.queue(self.create_job(kind, scope, items, idempotency_key=idempotency_key).id)

    def enqueue(self, job_id: str, items: Iterable[QueueItem]) -> int:
        """Add items to a job that has not started.  Impossible afterwards: the
        queue of a running job is its fixed input."""
        with self._write() as conn:
            job = self._job(conn, job_id)
            if job.state != 'created':
                raise StateConflict(f'queue is fixed in state {job.state}')
            count = self._insert_items(conn, job_id, items)
            self._emit(conn, job_id, 'job.progress', CODE_OK, {'queued': count})
            return count

    def queue(self, job_id: str) -> Job:
        """``created`` -> ``queued``: waiting for the single DB writer or a slot."""
        with self._write() as conn:
            job = self._job(conn, job_id)
            if job.state == 'queued':
                return job
            return self._set_state(conn, job, 'queued')

    # -- lifecycle --------------------------------------------------------

    def start(self, job_id: str, *, reason_code: str | None = None) -> Job:
        """``queued`` -> ``running``.  Refused while another job is running.

        The refusal is ``E_CONFLICT_BUSY`` rather than a second writer on the
        same database (CONTRACTS §6.4).
        """
        with self._write() as conn:
            job = self._job(conn, job_id)
            if job.state == 'running':
                return job
            if job.state != 'queued':
                raise StateConflict(f'start is not allowed in state {job.state}')
            other = conn.execute(
                'SELECT id FROM job WHERE state=? AND id<>? LIMIT 1', ('running', job_id)).fetchone()
            if other is not None:
                raise Busy(f'job {other[0]} is running')
            started = self._set_state(conn, job, 'running', reason_code=reason_code)
            self._put_checkpoint(conn, job_id, 'heartbeat', {'at': self._now(), 'owner': job_id})
            return started

    def pause(self, job_id: str, *, reason_code: str | None = None,
              idempotency_key: str | None = None) -> Job:
        """Stop cleanly and stay resumable; finished observations stay finished.

        Idempotent both by state (pausing a paused job changes nothing) and by
        key: a replayed pause cannot stop a job that has since resumed.
        """
        with self._write() as conn:
            job = self._job(conn, job_id)
            if idempotency_key is not None and self._control_replayed(conn, job_id, 'pause', idempotency_key):
                return job
            if job.state == 'paused':
                return job
            if job.state not in ('created', 'queued', 'running'):
                raise StateConflict(f'pause is not allowed in state {job.state}')
            requeued = self._requeue(conn, job_id)
            data = {'action': 'pause', 'idempotency_key': idempotency_key,
                    'requeued': _summarize_ids(requeued)}
            if job.state == 'created':
                job = self._set_state(conn, job, 'queued', reason_code=reason_code, event_data=data)
            job = self._set_state(conn, job, 'paused', reason_code=reason_code, event_data=data)
            self._put_checkpoint(conn, job_id, 'interrupt', {'at': self._now(), 'reason': 'paused'})
            return job

    def resume(self, job_id: str, *, scope: Scope | None = None, member_ids: Iterable[str] | None = None,
               membership_epoch: int | None = None, idempotency_key: str | None = None) -> Job:
        """``paused`` -> ``queued`` on exactly the items the job was created with.

        ``scope`` is accepted only to be checked: a different scope is
        ``E_CONFLICT_REVISION``, because a changed profile is a new job (§6.2),
        not a continuation.  ``member_ids`` marks queued items that have left
        the collection as ``blocked`` (defect 6) instead of measuring them.
        """
        with self._write() as conn:
            job = self._job(conn, job_id)
            if idempotency_key is not None and self._control_replayed(conn, job_id, 'resume', idempotency_key):
                return job
            if scope is not None:
                if not isinstance(scope, Scope):
                    raise Validation('scope must be a Scope')
                if scope.canonical() != job.scope.canonical():
                    raise Conflict('resume keeps the recorded scope; submit a new job instead')
            if member_ids is not None:
                self._drop_obsolete(conn, job_id, member_ids, membership_epoch)
                job = self._job(conn, job_id)
            if job.state == 'queued':
                return job
            if job.state != 'paused':
                raise StateConflict(f'resume is not allowed in state {job.state}')
            requeued = self._requeue(conn, job_id)
            data = {'action': 'resume', 'idempotency_key': idempotency_key,
                    'requeued': _summarize_ids(requeued)}
            job = self._set_state(conn, job, 'queued', reason_code=None, event_data=data)
            self._put_checkpoint(conn, job_id, 'interrupt', {'at': self._now(), 'reason': 'resumed'})
            return job

    def cancel(self, job_id: str, *, reason_code: str | None = None,
               idempotency_key: str | None = None) -> Job:
        """Explicit user cancellation.  Items are left exactly as they are, so
        the queue of unfinished work and every finished observation survive."""
        with self._write() as conn:
            job = self._job(conn, job_id)
            if idempotency_key is not None and self._control_replayed(conn, job_id, 'cancel', idempotency_key):
                return job
            if job.state == 'cancelled':
                return job
            if job.terminal:
                raise StateConflict(f'cancel is not allowed in state {job.state}')
            data = {'action': 'cancel', 'idempotency_key': idempotency_key}
            job = self._set_state(conn, job, 'cancelled', reason_code=reason_code, event_data=data)
            return job

    def retry(self, job_id: str, *, items: Iterable[str] | None = None, scope: Scope | None = None,
              kind: str | None = None, idempotency_key: str | None = None) -> Job:
        """Start a new job from what a finished one did not complete (§6.2).

        The old job is never touched: its history, its last completed result and
        its queue stay readable, and the new job gets new items (defect 6).
        """
        with self._write() as conn:
            job = self._job(conn, job_id)
            new_kind = kind or ('recheck' if job.kind in ('scan', 'recheck', 'export') else job.kind)
            if idempotency_key is not None:
                row = conn.execute(
                    f'SELECT {", ".join(_JOB_COLUMNS)} FROM job WHERE idempotency_key=?',
                    (idempotency_key,)).fetchone()
                if row is not None:
                    existing = self._job_from_row(row)
                    if existing.id == job.id or existing.kind != new_kind:
                        raise IdempotencyConflict(
                            f'key {idempotency_key!r} is already bound to job {existing.id}')
                    return existing
            if scope is not None and scope.canonical() != job.scope.canonical():
                raise Conflict('a changed scope is a new job, not a retry of this one')
            if items is None:
                wanted = conn.execute(
                    f'SELECT item_id FROM job_item WHERE job_id=? AND state<>? ORDER BY rowid',
                    (job_id, 'done')).fetchall()
            else:
                wanted = [(item_id,) for item_id in items]
            queue = []
            for (item_id,) in wanted:
                item = self._item(conn, job_id, item_id)
                if item.state == 'done':
                    continue
                queue.append(QueueItem(endpoint_id=item.endpoint_id, item_id=item.item_id,
                                       access_id=item.access_id, access_revision=item.access_revision))
            if not queue:
                raise StateConflict('nothing to retry: every item is done')
            stamp = self._now()
            new_id = new_job_id()
            digest = _input_digest(new_kind, job.scope, queue)
            conn.execute(
                'INSERT INTO job (id, kind, state, scope_json, input_digest, profile_id,'
                ' profile_revision, collection_id, idempotency_key, created_at, started_at, finished_at)'
                ' VALUES (?,?,?,?,?,?,?,?,?,?,NULL,NULL)',
                (new_id, new_kind, 'created', job.scope.to_json(), digest, job.profile_id,
                 job.profile_revision, job.collection_id, idempotency_key, stamp))
            count = self._insert_items(conn, new_id, queue)
            self._emit(conn, new_id, 'job.state', CODE_OK,
                       {'from': None, 'to': 'created', 'kind': new_kind, 'queued': count,
                        'input_digest': digest, 'retry_of': job.id, 'retried_items': count}, at=stamp)
            self._set_state(conn, self._job(conn, new_id), 'queued',
                            event_data={'action': 'retry', 'retry_of': job.id})
            return self._job(conn, new_id)

    def finish(self, job_id: str, *, state: str | None = None, reason_code: str | None = None,
               now: float | None = None) -> Job:
        """End a running job.  Without an explicit state the result follows the
        items: all terminal -> ``succeeded``, some unfinished -> ``partial``."""
        with self._write() as conn:
            job = self._job(conn, job_id)
            if job.terminal:
                return job
            if job.state not in ('running', 'queued'):
                raise StateConflict(f'finish is not allowed in state {job.state}')
            if job.state == 'queued':
                # A queued job has no items in flight: finishing it is a decision
                # of the worker, not something the queue state implies.
                raise StateConflict('a queued job must be started before it can finish')
            if state is None:
                unfinished = conn.execute(
                    f'SELECT count(*) FROM job_item WHERE job_id=? AND state NOT IN'
                    f' ({",".join("?" * len(TERMINAL_ITEM_STATES))})',
                    (job_id, *sorted(TERMINAL_ITEM_STATES))).fetchone()[0]
                state = 'partial' if unfinished else 'succeeded'
            if state not in TERMINAL_JOB_STATES:
                raise Validation(f'{state} is not a terminal job state')
            return self._set_state(conn, job, state, at=now, reason_code=reason_code)

    def fail(self, job_id: str, *, reason_code: str, detail: str = '', now: float | None = None) -> Job:
        """Record an execution error as a diagnosis, keeping everything measured."""
        _text(reason_code, 'reason_code', max_length=64)
        _check_public_payload({'detail': detail}, 'fail')
        with self._write() as conn:
            job = self._job(conn, job_id)
            if job.terminal:
                return job
            return self._set_state(conn, job, 'failed', at=now, reason_code=reason_code,
                                   event_data={'detail': detail} if detail else None)

    def enforce_deadline(self, job_id: str, *, now: float | None = None) -> Job:
        """``timed_out`` when the whole-job budget is spent (§6.2, §5.5).

        Time spent asleep is added back, so closing the lid overnight does not
        turn a healthy job into ``timed_out``.
        """
        stamp = self._now(now)
        with self._write() as conn:
            job = self._job(conn, job_id)
            if job.terminal or not job.active:
                return job
            deadline = self._deadline(conn, job, stamp)
            if deadline is None or stamp <= deadline:
                return job
            return self._set_state(conn, job, 'timed_out', at=stamp, reason_code='E_LIMIT_BUDGET',
                                   event_data={'deadline_at': deadline})

    # -- membership -------------------------------------------------------

    def sync_membership(self, job_id: str, member_ids: Iterable[str], *,
                        membership_epoch: int | None = None) -> dict:
        """Mark queued items that left the collection as ``blocked``.

        Only removals are applied: ``resume`` never gains items it did not have
        (scope is fixed) and never measures an address that is no longer a
        member (defect 6).  Returns the blocked items and the counts behind it.
        """
        with self._write() as conn:
            job = self._job(conn, job_id)
            if job.terminal:
                raise StateConflict(f'membership of a finished job is history, state {job.state}')
            return self._drop_obsolete(conn, job_id, member_ids, membership_epoch)

    def _drop_obsolete(self, conn: sqlite3.Connection, job_id: str, member_ids: Iterable[str],
                       membership_epoch: int | None) -> dict:
        members = set(member_ids)
        placeholders = ','.join('?' * len(UNFINISHED_ITEM_STATES))
        rows = conn.execute(
            f'SELECT item_id, endpoint_id FROM job_item WHERE job_id=? AND state IN ({placeholders})',
            (job_id, *UNFINISHED_ITEM_STATES)).fetchall()
        dropped = [row for row in rows if row[1] not in members]
        if not dropped:
            return {'blocked': 0, 'items': []}
        conn.executemany(
            'UPDATE job_item SET state=?, error_code=? WHERE job_id=? AND item_id=?',
            [('blocked', CODE_OBSOLETE_MEMBERSHIP, job_id, row[0]) for row in dropped])
        payload = _summarize_ids([row[0] for row in dropped])
        payload['membership_epoch'] = membership_epoch
        self._emit(conn, job_id, 'item.state', CODE_OBSOLETE_MEMBERSHIP, payload)
        return {'blocked': len(dropped), 'items': [row[0] for row in dropped]}

    # -- queue and items --------------------------------------------------

    def claim(self, job_id: str, *, item_id: str | None = None) -> JobItem | None:
        """Take the next queued item into ``probing``; ``None`` when the queue is empty.

        A ``prefiltered`` item is claimable as well: the cheap prefilter already
        passed, so the full measurement is what comes next.
        """
        claimable = ('pending', 'prefiltered')
        with self._write() as conn:
            job = self._job(conn, job_id)
            if job.state != 'running':
                raise StateConflict(f'claiming requires a running job, state is {job.state}')
            if item_id is not None:
                item = self._item(conn, job_id, item_id)
                row = (item.job_id, item.item_id, item.endpoint_id, item.access_id,
                       item.access_revision, item.state, item.observation_id, item.error_code)
            else:
                row = conn.execute(
                    f'SELECT {", ".join(_ITEM_COLUMNS)} FROM job_item WHERE job_id=? AND state IN (?,?)'
                    ' ORDER BY rowid LIMIT 1', (job_id, *claimable)).fetchone()
                if row is None:
                    return None
            if row[5] not in claimable:
                raise StateConflict(f'item {row[1]} is {row[5]}, not queued')
            self._item_state(conn, self._item_from_row(row), 'probing')
            return self._item(conn, job_id, row[1])

    def mark_prefiltered(self, job_id: str, item_id: str) -> JobItem:
        """The cheap TCP prefilter passed (§6.3); the item stays resumable."""
        with self._write() as conn:
            return self._item_state(conn, self._item(conn, job_id, item_id), 'prefiltered')

    def record_observation(self, job_id: str, item_id: str, observation_id: str) -> JobItem:
        """Attach a finished measurement to its item before the verdict is known.

        The id is written in the same transaction as the call, so a stop between
        "measured" and "judged" still leaves the observation recoverable.
        """
        _text(observation_id, 'observation_id', max_length=256)
        with self._write() as conn:
            item = self._item(conn, job_id, item_id)
            if item.terminal:
                raise StateConflict(f'item {item_id} is already {item.state}')
            conn.execute('UPDATE job_item SET observation_id=? WHERE job_id=? AND item_id=?',
                         (observation_id, job_id, item_id))
            self._emit(conn, job_id, 'item.observation', CODE_OK, {'observation_id': observation_id},
                       item_id=item_id)
            return self._item(conn, job_id, item_id)

    def finish_item(self, job_id: str, item_id: str, state: str, *, observation_id: str | None = None,
                    error_code: str | None = None, verdict: Mapping[str, Any] | None = None) -> JobItem:
        """Terminal state of an item, written together with its observation id.

        ``done`` requires ``observation_id``: "done" means there is an
        observation with an explicit measurement time (§6.3), and the write is
        atomic, so a refused call leaves the item and its measurement untouched.
        """
        if state not in ITEM_STATES:
            raise Validation(f'unknown item state {state}')
        if state == 'done' and not observation_id:
            raise Validation('done requires the observation_id of a finished measurement')
        if observation_id is not None:
            _text(observation_id, 'observation_id', max_length=256)
        if error_code is not None:
            _text(error_code, 'error_code', max_length=64)
        payload = dict(verdict or {})
        _check_public_payload(payload, 'verdict')
        with self._write() as conn:
            item = self._item(conn, job_id, item_id)
            if item.terminal:
                raise StateConflict(f'item {item_id} is already {item.state}')
            if observation_id is not None:
                conn.execute('UPDATE job_item SET observation_id=? WHERE job_id=? AND item_id=?',
                             (observation_id, job_id, item_id))
            item = self._item_state(conn, item, state, error_code=error_code)
            self._emit(conn, job_id, 'item.verdict', error_code or CODE_OK,
                       {'state': state, 'verdict': payload, 'observation_id': observation_id},
                       item_id=item_id)
            return item

    def _item_state(self, conn: sqlite3.Connection, item: JobItem, new_state: str, *,
                    error_code: str | None = None) -> JobItem:
        if new_state not in ITEM_TRANSITIONS.get(item.state, frozenset()):
            raise StateConflict(f'item {item.item_id}: {item.state} -> {new_state} is not a transition')
        conn.execute('UPDATE job_item SET state=?, error_code=coalesce(?, error_code)'
                     ' WHERE job_id=? AND item_id=?', (new_state, error_code, item.job_id, item.item_id))
        return self._item(conn, item.job_id, item.item_id)

    def items(self, job_id: str, *, state: str | None = None) -> list[JobItem]:
        if state is not None and state not in ITEM_STATES:
            raise Validation(f'unknown item state {state}')
        self.job(job_id)
        if state is None:
            rows = self._conn.execute(
                f'SELECT {", ".join(_ITEM_COLUMNS)} FROM job_item WHERE job_id=? ORDER BY rowid',
                (job_id,)).fetchall()
        else:
            rows = self._conn.execute(
                f'SELECT {", ".join(_ITEM_COLUMNS)} FROM job_item WHERE job_id=? AND state=? ORDER BY rowid',
                (job_id, state)).fetchall()
        return [self._item_from_row(row) for row in rows]

    def item(self, job_id: str, item_id: str) -> JobItem:
        return self._item(self._conn, job_id, item_id)

    def unfinished(self, job_id: str) -> int:
        self.job(job_id)
        placeholders = ','.join('?' * len(UNFINISHED_ITEM_STATES))
        return int(self._conn.execute(
            f'SELECT count(*) FROM job_item WHERE job_id=? AND state IN ({placeholders})',
            (job_id, *UNFINISHED_ITEM_STATES)).fetchone()[0])

    # -- checkpoints, heartbeat, sleep -------------------------------------

    def save_checkpoint(self, job_id: str, name: str, state: Mapping[str, Any], *,
                        emit: bool = True) -> dict:
        """Persist a named checkpoint so a restart does not start from zero."""
        if not isinstance(name, str) or not _CHECKPOINT_NAME_RE.match(name):
            raise Validation('checkpoint name must match [A-Za-z0-9_.-]{1,64}')
        if name in RESERVED_CHECKPOINTS:
            raise Validation(f'checkpoint {name} is written by the store')
        if not isinstance(state, Mapping):
            raise Validation('checkpoint state must be a mapping')
        with self._write() as conn:
            self._job(conn, job_id)
            saved = self._put_checkpoint(conn, job_id, name, state)
            if emit:
                counters = {key: value for key, value in state.items()
                            if isinstance(value, (int, float)) and not isinstance(value, bool)}
                self._emit(conn, job_id, 'job.checkpoint', CODE_OK,
                           {'name': name, 'counters': counters})
            return saved

    def checkpoint(self, job_id: str, name: str | None = None) -> dict | None:
        """One checkpoint, or every checkpoint of the job when ``name`` is None."""
        self.job(job_id)
        if name is not None:
            return self._get_checkpoint(self._conn, job_id, name)
        found = {}
        for row in self._conn.execute(
                f'SELECT {", ".join(_CHECKPOINT_COLUMNS)} FROM checkpoint WHERE job_id=? ORDER BY name',
                (job_id,)).fetchall():
            values = dict(zip(_CHECKPOINT_COLUMNS, row))
            found[values['name']] = {'name': values['name'], 'at': values['at'],
                                     'state': json.loads(values['state_json'] or '{}')}
        return found

    def drop_checkpoint(self, job_id: str, name: str) -> None:
        with self._write() as conn:
            self._job(conn, job_id)
            conn.execute('DELETE FROM checkpoint WHERE job_id=? AND name=?', (job_id, name))

    def heartbeat(self, job_id: str, **fields: Any) -> dict:
        """Record that a live worker still owns the job.

        Recovery uses the age of this row, so a job whose worker died is found
        without parsing any aggregated log (§5.6).
        """
        job = self.job(job_id)
        if job.terminal:
            raise StateConflict(f'a {job.state} job has no live worker')
        state = {'at': self._now()}
        state.update(fields)
        with self._write() as conn:
            return self._put_checkpoint(conn, job_id, 'heartbeat', state)

    def mark_suspended(self, job_id: str) -> dict:
        """Record that the machine is going to sleep (§ F11 sleep handling).

        A sleeping machine sends no heartbeats, so without this marker recovery
        would mistake a pause for a crash.
        """
        with self._write() as conn:
            job = self._job(conn, job_id)
            if job.terminal:
                raise StateConflict(f'a {job.state} job is not suspended')
            previous = self._get_checkpoint(conn, job_id, 'suspend') or {}
            total = float(previous.get('state', {}).get('total_slept_s') or 0.0)
            return self._put_checkpoint(conn, job_id, 'suspend',
                                        {'suspended_at': self._now(), 'total_slept_s': total})

    def mark_awake(self, job_id: str) -> dict:
        """Close a sleep: the time spent asleep is added back to the budget."""
        with self._write() as conn:
            self._job(conn, job_id)
            record = self._get_checkpoint(conn, job_id, 'suspend')
            if not record:
                raise Validation('the job was not marked suspended')
            suspended_at = float(record['state'].get('suspended_at') or self._now())
            slept = max(0.0, self._now() - suspended_at)
            total = float(record['state'].get('total_slept_s') or 0.0) + slept
            saved = self._put_checkpoint(conn, job_id, 'suspend',
                                         {'suspended_at': suspended_at, 'woke_at': self._now(),
                                          'slept_s': slept, 'total_slept_s': total})
            self._emit(conn, job_id, 'job.progress', CODE_OK,
                       {'action': 'awake', 'slept_s': round(slept, 3)})
            return saved

    def slept_s(self, job_id: str) -> float:
        record = self.checkpoint(job_id, 'suspend')
        if not record:
            return 0.0
        return float(record['state'].get('total_slept_s') or 0.0)

    def _deadline(self, conn: sqlite3.Connection, job: Job, now: float) -> float | None:
        timeout = job.scope.timeout_s
        if timeout is None or job.started_at is None:
            return None
        record = self._get_checkpoint(conn, job.id, 'suspend') or {'state': {}}
        return float(job.started_at) + timeout + float(record['state'].get('total_slept_s') or 0.0)

    # -- crash recovery ---------------------------------------------------

    def recover(self, *, heartbeat_timeout_s: float = DEFAULT_HEARTBEAT_TIMEOUT_S,
                now: float | None = None) -> Recovery:
        """Bring jobs of a dead process back to a resumable ``paused`` state.

        Nothing is deleted and no finished observation is touched: unfinished
        items go back to ``pending`` and the job keeps its events, so the queue
        of work that was not done is exactly what ``resume`` will continue.
        A job that was marked suspended before the sleep is left alone.
        """
        stamp = self._now(now)
        with self._write() as conn:
            rows = conn.execute(
                f'SELECT {", ".join(_JOB_COLUMNS)} FROM job WHERE state IN ("queued", "running")'
                ' ORDER BY created_at').fetchall()
            recovered: list[str] = []
            requeued_total = 0
            kept = 0
            for row in rows:
                job = self._job_from_row(row)
                suspend = self._get_checkpoint(conn, job.id, 'suspend') or {'state': {}}
                if suspend.get('state', {}).get('suspended_at') and not suspend['state'].get('woke_at'):
                    continue
                beat = self._get_checkpoint(conn, job.id, 'heartbeat')
                last_seen = float(beat['at']) if beat else float(job.started_at or job.created_at)
                if stamp - last_seen <= float(heartbeat_timeout_s):
                    continue
                requeued = self._requeue(conn, job.id)
                kept += int(conn.execute(
                    f'SELECT count(*) FROM job_item WHERE job_id=? AND observation_id IS NOT NULL',
                    (job.id,)).fetchone()[0])
                self._put_checkpoint(conn, job.id, 'interrupt',
                                     {'at': stamp, 'reason': 'heartbeat_timeout',
                                      'last_seen_at': last_seen})
                self._set_state(conn, job, 'paused', at=stamp, reason_code=CODE_INTERRUPTED,
                                event_data={'action': 'recover', 'last_seen_at': last_seen,
                                            'requeued': _summarize_ids(requeued)})
                recovered.append(job.id)
                requeued_total += len(requeued)
            return Recovery(jobs=tuple(recovered), requeued_items=requeued_total,
                            observations_kept=kept)

    # -- reads ------------------------------------------------------------

    def job(self, job_id: str) -> Job:
        return self._job(self._conn, job_id)

    def jobs(self, *, kind: str | None = None, state: str | None = None,
             limit: int | None = None) -> list[Job]:
        clauses, params = [], []
        if kind is not None:
            clauses.append('kind=?')
            params.append(kind)
        if state is not None:
            if state not in JOB_STATES:
                raise Validation(f'unknown job state {state}')
            clauses.append('state=?')
            params.append(state)
        sql = f'SELECT {", ".join(_JOB_COLUMNS)} FROM job'
        if clauses:
            sql += ' WHERE ' + ' AND '.join(clauses)
        sql += ' ORDER BY created_at, id'
        if limit is not None:
            sql += ' LIMIT ?'
            params.append(int(limit))
        return [self._job_from_row(row) for row in self._conn.execute(sql, params).fetchall()]

    def events(self, job_id: str, *, after_seq: int = 0, limit: int = 500,
               types: Sequence[str] | None = None) -> list[JobEvent]:
        """Events of a job with ``seq > after_seq`` (§5.7 resume rule)."""
        self.job(job_id)
        params: list[Any] = [job_id, int(after_seq)]
        sql = f'SELECT {", ".join(_EVENT_COLUMNS)} FROM job_event WHERE job_id=? AND seq>?'
        if types:
            sql += f' AND type IN ({",".join("?" * len(types))})'
            params.extend(types)
        sql += ' ORDER BY seq LIMIT ?'
        params.append(int(limit))
        events = []
        for row in self._conn.execute(sql, params).fetchall():
            values = dict(zip(_EVENT_COLUMNS, row))
            data = json.loads(values['data_json'] or '{}')
            events.append(JobEvent(job_id=job_id, seq=values['seq'], at=values['at'],
                                   type=values['type'], code=values['code'],
                                   data=MappingProxyType(data), item_id=data.get('item_id')))
        return events

    def emit(self, job_id: str, type: str, code: str = CODE_OK,
             data: Mapping[str, Any] | None = None, *, item_id: str | None = None) -> JobEvent:
        """Append one structured event of a job (no aggregated log lines)."""
        _text(type, 'event type', max_length=64)
        _text(code, 'event code', max_length=64)
        with self._write() as conn:
            self._job(conn, job_id)
            return self._emit(conn, job_id, type, code, data, item_id=item_id)

    def last_seq(self, job_id: str) -> int:
        row = self._conn.execute('SELECT max(seq) FROM job_event WHERE job_id=?',
                                 (job_id,)).fetchone()
        return 0 if row is None or row[0] is None else int(row[0])

    def progress(self, job_id: str, *, now: float | None = None) -> Progress:
        """Counters of a job in one grouped query, plus its budget position."""
        stamp = self._now(now)
        conn = self._conn
        job = self._job(conn, job_id)
        rows = conn.execute('SELECT state, count(*) FROM job_item WHERE job_id=? GROUP BY state',
                            (job_id,)).fetchall()
        counts = {state: 0 for state in ITEM_STATES}
        for state, count in rows:
            counts[state] = int(count)
        total = sum(counts.values())
        finished = sum(counts[state] for state in TERMINAL_ITEM_STATES)
        deadline = self._deadline(conn, job, stamp)
        suspend = self._get_checkpoint(conn, job_id, 'suspend') or {'state': {}}
        slept = float(suspend['state'].get('total_slept_s') or 0.0)
        interrupt = (self._get_checkpoint(conn, job_id, 'interrupt') or {}).get('state') or {}
        started = job.started_at
        return Progress(
            job_id=job_id, state=job.state, state_detail=self._state_detail(job, counts, total,
                                                                           finished, interrupt),
            by_state=MappingProxyType(counts), total=total, finished=finished,
            unfinished=total - finished, last_seq=self.last_seq(job_id),
            elapsed_s=max(0.0, (job.finished_at or stamp) - started) if started else 0.0,
            remaining_s=None if deadline is None else max(0.0, deadline - stamp),
            deadline_at=deadline, slept_s=slept,
            last_checkpoint=self._last_checkpoint(conn, job_id),
        )

    def _last_checkpoint(self, conn: sqlite3.Connection, job_id: str) -> dict | None:
        row = conn.execute('SELECT name, at FROM checkpoint WHERE job_id=? ORDER BY at DESC, name LIMIT 1',
                           (job_id,)).fetchone()
        return None if row is None else {'name': row[0], 'at': row[1]}

    @staticmethod
    def _state_detail(job: Job, counts: Mapping[str, int], total: int, finished: int,
                      interrupt: Mapping[str, Any]) -> str | None:
        """Why a job is in its terminal state, in the §4.3 vocabulary."""
        if job.state in ('created', 'queued', 'running'):
            return None
        if job.state == 'cancelled':
            return DETAIL_CANCELLED
        if job.state == 'timed_out':
            return DETAIL_BUDGET
        if job.state == 'paused':
            return DETAIL_CRASHED if interrupt.get('reason') == 'heartbeat_timeout' else DETAIL_STOPPED
        if job.state == 'failed':
            return DETAIL_ALL_FAILED if counts.get('done', 0) == 0 else DETAIL_STOPPED
        if finished < total:
            return DETAIL_STOPPED
        if total == 0 or counts.get('done', 0) == 0:
            return DETAIL_ALL_FAILED if total else DETAIL_EMPTY
        return DETAIL_COMPLETE
